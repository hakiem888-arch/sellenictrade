from decimal import Decimal as D
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
import unittest
from unittest.mock import patch

from manual_tracker.config import Config
from manual_tracker.engine import InvalidData
from manual_tracker.market import BinancePollingFeed, PublicHTTP, DataUnavailable, completed_atr
from manual_tracker.store import Store
from manual_tracker.telegram import TelegramAPI, TelegramError
from manual_tracker.worker import Poller, Worker, bind_bot, worker_lock
from tests.helpers import Clock, TrackingCase, details


def candles(clock, period=14):
    boundary = int(clock() // 60) * 60
    return [[(boundary-(period+1-i)*60)*1000, "100", "102", "98", "100", "1", (boundary-(period-i)*60)*1000-1] for i in range(period+2)]


class HTTPFixture:
    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def get(self, instrument, path, params):
        self.calls.append((instrument, path, params))
        if path.endswith("exchangeInfo"):
            return {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "isSpotTradingAllowed": True, "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}]}
        if path.endswith("trades"):
            return [{"price": "110", "time": self.clock()*1000}]
        if path.endswith("premiumIndex"):
            return {"symbol": "BTCUSDT", "markPrice": "110", "time": self.clock()*1000}
        return candles(self.clock)


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cfg = Config()

    def test_completed_candles_only(self):
        rows = candles(self.clock)
        rows[-1][2] = "999999"  # current candle must be ignored
        atr, stamp = completed_atr(rows, 14, 60, self.clock())
        self.assertEqual(atr, D(4))
        self.assertLess(stamp, self.clock())

    def test_gaps_and_invalid_bars_rejected(self):
        rows = candles(self.clock)
        rows.pop(4)
        with self.assertRaises(InvalidData):
            completed_atr(rows, 14, 60, self.clock())
        rows = candles(self.clock)
        rows[4][2] = "0"
        with self.assertRaises(InvalidData):
            completed_atr(rows, 14, 60, self.clock())

    def test_spot_last_and_linear_mark_match_atr_source(self):
        for instrument, expected in [("SPOT", "LAST"), ("LINEAR", "MARK")]:
            http = HTTPFixture(self.clock)
            feed = BinancePollingFeed(self.cfg, http, self.clock)
            obs = feed.observation(details(instrument=instrument))
            self.assertEqual(obs.reference, expected)
            self.assertEqual(obs.price, D(110))
            self.assertTrue(any(path.endswith("markPriceKlines" if instrument == "LINEAR" else "klines") for _, path, _ in http.calls))

    def test_metadata_and_candle_caches_reset_on_reconnect(self):
        http = HTTPFixture(self.clock)
        feed = BinancePollingFeed(self.cfg, http, self.clock)
        feed.observation(details())
        feed.observation(details())
        self.assertEqual(sum(path.endswith("exchangeInfo") for _, path, _ in http.calls), 1)
        feed.reset()
        feed.observation(details())
        self.assertEqual(sum(path.endswith("exchangeInfo") for _, path, _ in http.calls), 2)

    def test_missing_instrument_metadata_fails(self):
        class Empty:
            def get(self, *args):
                return {"symbols": []}
        with self.assertRaises(InvalidData):
            BinancePollingFeed(self.cfg, Empty(), self.clock).observation(details())

    def test_rate_limit_cooldown_and_resume(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            if len(calls) == 1:
                raise HTTPError(req.full_url, 429, "limit", {"Retry-After": "10"}, BytesIO(b"{}"))
            return BytesIO(b'{"price":"100"}')
        http = PublicHTTP(opener=opener, clock=self.clock)
        for _ in range(2):
            with self.assertRaises(DataUnavailable):
                http.get("SPOT", "/api/v3/trades", {"symbol": "BTCUSDT"})
        self.assertEqual(len(calls), 1)
        self.clock.now += 10
        self.assertEqual(http.get("SPOT", "/api/v3/trades", {}), {"price": "100"})

    def test_public_endpoint_allowlist_has_no_orders(self):
        http = PublicHTTP()
        for path in ["/api/v3/order", "/fapi/v1/order", "/api/v3/account"]:
            with self.assertRaises(InvalidData):
                http.get("SPOT", path, {})

    def test_telegram_error_never_contains_token(self):
        token = "123:VERY_SECRET"
        def fail(req, timeout):
            raise OSError(req.full_url)
        with self.assertRaises(TelegramError) as ctx:
            TelegramAPI(token, fail).call("sendMessage", {})
        self.assertNotIn(token, str(ctx.exception))
        self.assertTrue(ctx.exception.ambiguous)


class WorkerTests(TrackingCase):
    def test_threaded_worker_start_monitor_dispatch_and_stop(self):
        import threading
        import time
        from tests.test_monitor import Feed
        p = self.register()
        call = self.api.call
        def fake_long_poll(method, payload):
            if method == "getUpdates":
                time.sleep(0.02)
            return call(method, payload)
        self.api.call = fake_long_poll
        worker = Worker(self.store, self.cfg, self.api, Feed(self.clock))
        failures = []
        def run():
            try:
                worker.run()
            except Exception as e:
                failures.append(e)
        with patch.dict("os.environ", {"MANUAL_LOCK_DIR": str(Path(self.tmp.name) / "worker-locks")}):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if self.store.position(p["id"])["last_notified_stop"]:
                        break
                    time.sleep(0.02)
                self.assertEqual(self.store.position(p["id"])["last_notified_stop"], "100.26")
            finally:
                worker.stop.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(json.loads(self.store.one("SELECT value FROM mt_runtime WHERE key='worker'")["value"])["state"], "STOPPED")

    def test_same_bot_consumer_lock(self):
        root = Path(self.tmp.name) / "locks"
        with worker_lock("123:one", root):
            with self.assertRaises(RuntimeError):
                with worker_lock("123:rotated", root):
                    self.fail("Duplicate worker accepted")

    def test_bot_binding_prevents_cross_bot_messages(self):
        bind_bot(self.store, self.cfg.token)
        bind_bot(self.store, "123:rotated")
        with self.assertRaises(ValueError):
            bind_bot(self.store, "456:other")

    def test_webhook_conflict_not_deleted(self):
        self.api.webhook = "https://example.invalid/hook"
        poller = Poller(self.store, self.cfg, self.api)
        with self.assertRaises(RuntimeError):
            poller.preflight()
        self.assertEqual([m for m, _ in self.api.calls], ["getWebhookInfo"])

    def test_poller_durable_offset(self):
        self.api.updates = [{"update_id": 100, "message": {"from": {"id": 7}, "chat": {"id": 99}, "text": "/positions"}}]
        poller = Poller(self.store, self.cfg, self.api)
        poller.preflight()
        poller.once()
        poller.once()
        requests = [p for m, p in self.api.calls if m == "getUpdates"]
        self.assertEqual([r["offset"] for r in requests], [0, 101])
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_updates")), 1)

    def test_conflict_stops_poller(self):
        poller = Poller(self.store, self.cfg, self.api)
        with patch.object(poller, "preflight"), patch.object(poller, "once", side_effect=TelegramError(409)):
            import threading
            poller.run(threading.Event())
        value = json.loads(self.store.one("SELECT value FROM mt_runtime WHERE key='telegram'")["value"])
        self.assertEqual(value["state"], "BLOCKED_CONFLICT")

    def test_require_allowlist_fail_closed(self):
        with self.assertRaises(ValueError):
            Config(enabled=True, token="123:abc", chat_id="99").require_worker()
        self.cfg.require_worker()

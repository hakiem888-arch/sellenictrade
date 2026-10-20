from decimal import Decimal as D
from unittest.mock import patch

from manual_tracker.engine import InvalidData
from manual_tracker.market import DataUnavailable
from manual_tracker.monitor import Monitor
from manual_tracker.store import Store
from manual_tracker.telegram import Dispatcher
from tests.helpers import TrackingCase, observation


class Feed:
    def __init__(self, clock):
        self.clock, self.fail, self.resets, self.calls = clock, False, 0, []
        self.price, self.atr = "110", "10"

    def reset(self):
        self.resets += 1

    def metadata(self, instrument, symbol):
        if self.fail:
            raise DataUnavailable("OUTAGE")
        return D("0.01")

    def observation(self, details):
        self.calls.append(details["symbol"])
        if self.fail:
            raise DataUnavailable("OUTAGE")
        return observation(self.clock, self.price, self.atr, reference="MARK" if details["instrument"] == "LINEAR" else "LAST")


class MonitorTests(TrackingCase):
    def setUp(self):
        super().setUp()
        self.feed = Feed(self.clock)
        self.monitor = Monitor(self.store, self.cfg, self.feed)

    def test_break_even_delivery_last_stop_and_dedup(self):
        p = self.register()
        for _ in range(3):
            self.monitor.cycle()
            self.dispatch.drain()
        outs = self.store.rows("SELECT * FROM mt_outbox WHERE kind='RECOMMENDATION'")
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0]["state"], "SENT")
        latest = self.store.position(p["id"])
        self.assertEqual(latest["last_notified_stop"], "100.26")
        self.assertEqual(latest["last_event_id"], outs[0]["id"])

    def test_restart_preserves_extremes_and_dedup(self):
        p = self.register()
        self.monitor.cycle()
        self.dispatch.drain()
        restarted = Store(self.store.path, self.clock)
        m = Monitor(restarted, self.cfg, self.feed)
        m.recover()
        m.cycle()
        self.dispatch.drain()
        latest = restarted.position(p["id"])
        self.assertEqual(latest["best_price"], "110")
        self.assertEqual(latest["recommended_stop"], "100.26")
        self.assertEqual(len(restarted.rows("SELECT * FROM mt_outbox WHERE kind='RECOMMENDATION'")), 1)

    def test_stale_quote_prevents_recommendation(self):
        p = self.register()
        with patch.object(self.feed, "observation", return_value=observation(self.clock, timestamp=self.clock()-30)):
            self.monitor.cycle()
        self.assertEqual(self.store.position(p["id"])["state"], "TRACKING_ERROR")
        self.assertEqual(self.store.rows("SELECT * FROM mt_outbox WHERE kind='RECOMMENDATION'"), [])

    def test_outage_recovery_and_error_suppression(self):
        p = self.register()
        self.feed.fail = True
        self.monitor.cycle()
        self.monitor.cycle()
        self.assertEqual(self.store.position(p["id"])["state"], "TRACKING_ERROR")
        self.assertIsNone(self.store.position(p["id"])["closed_at"])
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_outbox WHERE kind='DATA_ERROR'")), 1)
        self.feed.fail = False
        self.monitor.cycle()
        self.assertEqual(self.store.position(p["id"])["state"], "ACTIVE")
        self.assertGreaterEqual(self.feed.resets, 1)

    def test_touch_never_closes_and_is_not_repeated(self):
        p = self.register()
        self.feed.price = "89"
        self.monitor.cycle()
        self.monitor.cycle()
        self.assertEqual(self.store.position(p["id"])["state"], "ACTIVE")
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_outbox WHERE kind='STOP_TOUCH'")), 1)

    def test_paused_closed_and_ignored_not_fetched(self):
        p = self.register()
        self.callback("p", p["id"], self.action_message(p["id"]))
        s = self.signal()
        self.callback("i", s["id"], s["message_id"])
        self.monitor.recover()
        self.monitor.cycle()
        self.assertEqual(self.feed.calls, [])
        self.assertEqual(self.store.position(p["id"])["state"], "PAUSED")

    def test_pause_during_fetch_wins(self):
        p = self.register()
        def fetch(details):
            self.callback("p", p["id"], self.action_message(p["id"]))
            return observation(self.clock)
        with patch.object(self.feed, "observation", side_effect=fetch):
            self.monitor.cycle()
        self.assertEqual(self.store.rows("SELECT * FROM mt_outbox WHERE kind='RECOMMENDATION'"), [])

    def test_stale_queued_recommendation_not_sent(self):
        self.register()
        self.monitor.cycle()
        self.clock.now += 21
        self.dispatch.drain()
        self.assertEqual(self.store.one("SELECT state FROM mt_outbox WHERE kind='RECOMMENDATION'")["state"], "SUPERSEDED")

    def test_pause_suppresses_pending_recommendation(self):
        p = self.register()
        self.monitor.cycle()
        self.callback("p", p["id"], self.action_message(p["id"]))
        self.assertEqual(self.store.one("SELECT state FROM mt_outbox WHERE kind='RECOMMENDATION'")["state"], "SUPERSEDED")

    def test_newer_stop_supersedes_previous_queued_stop(self):
        p = self.register()
        self.monitor.cycle()
        self.feed.price, self.feed.atr = "120", "2"
        self.monitor.cycle()
        self.dispatch.drain()
        rows = self.store.rows("SELECT * FROM mt_outbox WHERE kind='RECOMMENDATION' ORDER BY rowid")
        self.assertEqual([r["state"] for r in rows], ["SUPERSEDED", "SENT"])
        self.assertEqual(self.store.position(p["id"])["last_notified_stop"], "116")

    def test_duplicate_symbols_share_quote_request(self):
        self.register()
        self.register()
        self.monitor.cycle()
        self.assertEqual(len(self.feed.calls), 1)

    def test_metadata_fetched_while_wizard_waits(self):
        with self.store.tx() as con:
            con.execute("DELETE FROM mt_instruments")
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        self.reply("LINEAR")
        self.monitor.cycle()
        meta = self.store.one("SELECT * FROM mt_instruments")
        self.assertEqual(meta["tick"], "0.01")
        self.assertIsNone(meta["last_error"])

    def test_missing_metadata_blocks_final_confirmation(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        for text in ["SPOT", "100", "2", "90", "0", "0"]:
            self.reply(text)
        with self.store.tx() as con:
            con.execute("DELETE FROM mt_instruments")
        ses = self.session()
        out = self.store.one("SELECT * FROM mt_outbox WHERE id=?", (ses["prompt_event"],))
        self.assertEqual(self.callback("c", ses["nonce"], out["message_id"]), "REJECTED")
        self.assertEqual(self.store.rows("SELECT * FROM mt_positions"), [])
        self.monitor.cycle()
        self.assertEqual(self.callback("c", ses["nonce"], out["message_id"]), "ACTIVE")

    def test_off_tick_stop_cannot_register(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        for text in ["SPOT", "100", "2", "90.001", "0", "0"]:
            self.reply(text)
        ses = self.session()
        out = self.store.one("SELECT * FROM mt_outbox WHERE id=?", (ses["prompt_event"],))
        self.assertEqual(self.callback("c", ses["nonce"], out["message_id"]), "REJECTED")
        self.assertEqual(self.store.rows("SELECT * FROM mt_positions"), [])

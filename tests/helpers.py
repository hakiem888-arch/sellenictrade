from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from manual_tracker.config import Config
from manual_tracker.engine import Observation
from manual_tracker.interactions import Interactions
from manual_tracker.store import Store
from manual_tracker.telegram import Dispatcher, publish_signal, TelegramError


class Clock:
    def __init__(self):
        self.now = 1700000040.0

    def __call__(self):
        return self.now


class FakeAPI:
    def __init__(self):
        self.calls, self.count, self.fail = [], 100, None
        self.updates = []
        self.webhook = ""

    def call(self, method, payload):
        self.calls.append((method, dict(payload)))
        if self.fail:
            raise self.fail
        if method == "getWebhookInfo":
            return {"url": self.webhook}
        if method == "getUpdates":
            return list(self.updates)
        if method == "answerCallbackQuery":
            return True
        self.count += 1
        return {"message_id": self.count}


def details(direction="LONG", instrument="LINEAR", **changes):
    return dict(symbol="BTCUSDT", direction=direction, instrument=instrument, entry="100", quantity="2", initial_stop="90" if direction == "LONG" else "110", multiplier="1", leverage="5" if instrument == "LINEAR" else None, entry_fee="0.001", exit_fee="0.001", **changes)


def observation(clock, price="110", atr="2", reference="MARK", **changes):
    d = dict(price=Decimal(price), timestamp=clock(), atr=Decimal(atr), atr_timestamp=clock() - 1, tick=Decimal("0.01"), reference=reference, interval_seconds=60)
    d.update(changes)
    return Observation(**d)


class TrackingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = Store(Path(self.tmp.name) / "test.db", self.clock)
        # Deterministic exchange metadata fixture, never a live lookup.
        with self.store.tx() as con:
            for instrument in ("SPOT", "LINEAR"):
                con.execute("INSERT INTO mt_instruments VALUES(?,?,?, ?,NULL)", (instrument, "BTCUSDT", "0.01", self.clock()))
        self.cfg = Config(enabled=True, token="123:TEST_ONLY", chat_id="99", users=frozenset({7, 8}), chats=frozenset({99}))
        self.api = FakeAPI()
        self.dispatch = Dispatcher(self.store, self.cfg, self.api)
        self.handler = Interactions(self.store, self.cfg)
        self.seq, self.source_seq = 0, 0

    def signal(self, direction="LONG", **changes):
        self.source_seq += 1
        d = dict(id=self.source_seq, created_at="2023-11-14T22:14:00+00:00", analysis_id=str(self.source_seq), symbol="BTCUSDT", timeframe="5m/15m/1h", final_decision=direction, entry=100, stop_loss=90 if direction == "LONG" else 110, take_profit=120 if direction == "LONG" else 80, risk_reward=2)
        d.update(changes)
        key = publish_signal(self.store, self.cfg, d, "Legacy v5.5 format")
        self.dispatch.drain()
        return self.store.one("SELECT * FROM mt_signals WHERE id=?", (key.split(":")[1],))

    def callback(self, action, target, message_id, user=7, chat=99, callback_id=None, drain=True):
        self.seq += 1
        update = {"update_id": self.seq, "callback_query": {"id": callback_id or str(self.seq), "from": {"id": user}, "data": f"mt:{action}:{target}", "message": {"message_id": message_id, "chat": {"id": chat}}}}
        result = self.handler.process(update)
        if drain:
            self.dispatch.drain()
        return result

    def session(self, user=7):
        return self.store.one("SELECT * FROM mt_sessions WHERE user_id=? AND chat_id=99", (user,))

    def reply(self, text, user=7, reply_id=None):
        s = self.session(user)
        out = self.store.one("SELECT * FROM mt_outbox WHERE id=?", (s["prompt_event"],)) if s else None
        self.seq += 1
        update = {"update_id": self.seq, "message": {"from": {"id": user}, "chat": {"id": 99}, "message_id": 50000+self.seq, "date": self.clock(), "text": text}}
        if out:
            update["message"]["reply_to_message"] = {"message_id": reply_id if reply_id is not None else out["message_id"]}
        result = self.handler.process(update)
        self.dispatch.drain()
        return result

    def register(self, direction="LONG", instrument="LINEAR", fee="0.001"):
        with self.store.tx() as con:
            con.execute("UPDATE mt_instruments SET checked_at=?", (self.clock(),))
        s = self.signal(direction)
        self.callback("e", s["id"], s["message_id"])
        fields = [instrument, "100", "2", "90" if direction == "LONG" else "110"]
        if instrument == "LINEAR":
            fields += ["1", "5"]
        for text in fields + [fee, fee]:
            result = self.reply(text)
            self.assertNotEqual(result, "REJECTED")
        session = self.session()
        out = self.store.one("SELECT * FROM mt_outbox WHERE id=?", (session["prompt_event"],))
        self.assertEqual(self.callback("c", session["nonce"], out["message_id"]), "ACTIVE")
        return self.store.one("SELECT * FROM mt_positions WHERE signal_id=?", (s["id"],))

    def action_message(self, pid):
        row = self.store.one("SELECT message_id FROM mt_outbox WHERE position_id=? AND state='SENT' AND method='sendMessage' ORDER BY created_at DESC,rowid DESC LIMIT 1", (pid,))
        return row["message_id"]

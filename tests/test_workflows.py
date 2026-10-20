from dataclasses import replace
import json
import sqlite3
import threading
from unittest.mock import patch

from manual_tracker.engine import InvalidData
from manual_tracker.interactions import Interactions
from manual_tracker.monitor import Monitor
from manual_tracker.store import Store, transition
from manual_tracker.telegram import Dispatcher, TelegramError, publish_signal
from tests.helpers import TrackingCase, FakeAPI, observation


class WorkflowTests(TrackingCase):
    def test_confirmed_long_short_and_spot(self):
        for direction, instrument in [("LONG", "LINEAR"), ("SHORT", "LINEAR"), ("LONG", "SPOT")]:
            p = self.register(direction, instrument)
            self.assertEqual(p["state"], "ACTIVE")
            self.assertEqual(p["details"]["direction"], direction)
            self.assertEqual(p["details"]["execution_source"], "USER_REPORTED")

    def test_reference_price_never_auto_registers(self):
        s = self.signal(entry=123)
        self.callback("e", s["id"], s["message_id"])
        self.reply("LINEAR")
        self.reply("101")
        self.assertEqual(self.session()["data"]["entry"], "101")
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_positions")), 0)

    def test_ignore_terminal_without_tracker(self):
        s = self.signal()
        self.assertEqual(self.callback("i", s["id"], s["message_id"]), "IGNORED")
        self.assertEqual(self.callback("e", s["id"], s["message_id"]), "IGNORED")
        self.assertEqual(self.store.rows("SELECT * FROM mt_positions"), [])
        self.assertEqual(self.store.one("SELECT state FROM mt_signals WHERE id=?", (s["id"],))["state"], "IGNORED")
        self.assertEqual(self.reply("/history"), "HISTORY")
        self.assertTrue(any("IGNORED" in p.get("text", "") for method, p in self.api.calls))

    def test_wait_rejected(self):
        with self.assertRaises(ValueError):
            self.signal("WAIT")
        self.assertEqual(self.store.rows("SELECT * FROM mt_signals"), [])

    def test_unauthorized_callbacks_and_chat(self):
        s = self.signal()
        for user, chat in [(12345, 99), (7, 88)]:
            self.assertEqual(self.callback("e", s["id"], s["message_id"], user=user, chat=chat), "UNAUTHORIZED")
        self.assertEqual(self.store.rows("SELECT * FROM mt_sessions"), [])

    def test_callback_message_and_signal_validation(self):
        s = self.signal()
        self.assertEqual(self.callback("e", s["id"], 999999), "REJECTED")
        self.assertEqual(self.callback("e", "a"*20, s["message_id"]), "REJECTED")
        self.assertEqual(self.store.rows("SELECT * FROM mt_sessions"), [])

    def test_duplicate_callback_and_offset(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"], callback_id="repeat")
        self.assertEqual(self.callback("e", s["id"], s["message_id"], callback_id="repeat"), "DUPLICATE")
        self.assertEqual(json.loads(self.store.one("SELECT value FROM mt_runtime WHERE key='offset:123'")["value"]), self.seq + 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_sessions")), 1)

    def test_distinct_double_click_does_not_restart_wizard(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        self.reply("LINEAR")
        self.assertEqual(self.callback("e", s["id"], s["message_id"]), "AWAITING_ENTRY_DETAILS")
        self.assertEqual(self.session()["step"], "entry")

    def test_duplicate_confirmation_no_second_position(self):
        p = self.register()
        out = self.store.one("SELECT * FROM mt_outbox WHERE id LIKE 'prompt:%' ORDER BY rowid DESC LIMIT 1")
        data = out["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(self.callback("c", data.split(":")[-1], out["message_id"]), "REJECTED")
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_positions")), 1)

    def test_claim_ownership(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"], user=7)
        self.assertEqual(self.callback("i", s["id"], s["message_id"], user=8), "REJECTED")
        self.assertEqual(self.callback("e", s["id"], s["message_id"], user=8), "REJECTED")

    def test_owner_only_pause_close(self):
        p = self.register()
        mid = self.action_message(p["id"])
        self.assertEqual(self.callback("p", p["id"], mid, user=8), "REJECTED")
        self.assertEqual(self.callback("x", p["id"], mid, user=8), "REJECTED")
        self.assertEqual(self.store.position(p["id"])["state"], "ACTIVE")

    def test_wrong_reply_target_and_invalid_data_stay_at_step(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        self.assertEqual(self.reply("LINEAR", reply_id=9999), "REJECTED")
        self.reply("LINEAR")
        for invalid in ["NaN", "0", "-100"]:
            self.assertEqual(self.reply(invalid), "REJECTED")
            self.assertEqual(self.session()["step"], "entry")
        self.reply("100")
        self.assertEqual(self.reply("0"), "REJECTED")
        self.assertEqual(self.session()["step"], "quantity")

    def test_missing_metadata_fee_not_fabricated(self):
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        for text in ["SPOT", "100", "2", "90"]:
            self.reply(text)
        self.assertEqual(self.reply("?"), "REJECTED")
        self.assertEqual(self.store.rows("SELECT * FROM mt_positions"), [])

    def test_signal_and_confirmation_expiry(self):
        s = self.signal()
        self.clock.now += self.cfg.signal_ttl + 1
        self.assertEqual(self.callback("e", s["id"], s["message_id"]), "REJECTED")
        s = self.signal()
        self.callback("e", s["id"], s["message_id"])
        self.clock.now += self.cfg.session_ttl + 1
        self.reply("LINEAR")
        self.assertIsNone(self.session())
        self.assertEqual(self.store.rows("SELECT * FROM mt_positions"), [])

    def test_pause_persists_restart_explicit_resume_only(self):
        p = self.register()
        self.callback("p", p["id"], self.action_message(p["id"]))
        restart = Store(self.store.path, self.clock)
        self.assertEqual(restart.position(p["id"])["state"], "PAUSED")
        self.assertEqual(self.callback("r", p["id"], self.action_message(p["id"])), "ACTIVE")
        self.assertEqual(restart.position(p["id"])["data_quality"], "AWAITING_RESYNC")

    def test_close_confirmation_with_exit_and_without(self):
        for exit_price in ["115", "-"]:
            p = self.register()
            self.assertEqual(self.callback("x", p["id"], self.action_message(p["id"])), "EXIT_REPORTED")
            self.assertIsNone(self.store.position(p["id"])["closed_at"])
            self.reply(exit_price)
            s = self.session()
            out = self.store.one("SELECT * FROM mt_outbox WHERE id=?", (s["prompt_event"],))
            self.assertEqual(self.callback("c", s["nonce"], out["message_id"]), "CLOSED")
            closed = self.store.position(p["id"])
            self.assertEqual(closed["exit_price"], None if exit_price == "-" else "115")
            self.assertEqual(closed["closure_source"], "USER_REPORTED")

    def test_cancel_close_or_expire_stays_paused(self):
        for cancel in [True, False]:
            p = self.register()
            self.callback("x", p["id"], self.action_message(p["id"]))
            if cancel:
                self.reply("/cancel")
            else:
                self.clock.now += self.cfg.session_ttl + 1
                with self.store.tx() as con:
                    self.store.expire_sessions(con)
            self.assertEqual(self.store.position(p["id"])["state"], "PAUSED")

    def test_additive_migration_preserves_legacy_data(self):
        with self.store.tx() as con:
            con.execute("CREATE TABLE signals(id INTEGER PRIMARY KEY, result TEXT)")
            con.execute("CREATE TABLE paper_positions(id INTEGER PRIMARY KEY, equity REAL)")
            con.execute("INSERT INTO signals VALUES(5,'WAIT')")
            con.execute("INSERT INTO paper_positions VALUES(1,123.45)")
        before = self.store.rows("SELECT * FROM signals"), self.store.rows("SELECT * FROM paper_positions")
        self.store.migrate()
        self.register()
        after = self.store.rows("SELECT * FROM signals"), self.store.rows("SELECT * FROM paper_positions")
        self.assertEqual(before, after)

    def test_database_rollback_does_not_acknowledge_update(self):
        s = self.signal()
        with patch.object(self.handler, "_callback", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.callback("e", s["id"], s["message_id"])
        self.assertEqual(self.store.rows("SELECT * FROM mt_updates"), [])
        self.assertEqual(self.store.rows("SELECT * FROM mt_sessions"), [])

    def test_concurrent_claims_have_one_owner(self):
        s = self.signal()
        errors = []
        def claim(user):
            try:
                handler = Interactions(Store(self.store.path, self.clock), self.cfg)
                handler.process({"update_id": user, "callback_query": {"id": str(user), "from": {"id": user}, "data": f"mt:e:{s['id']}", "message": {"message_id": s["message_id"], "chat": {"id": 99}}}})
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=claim, args=(user,)) for user in [7, 8]]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_sessions")), 1)

    def test_invalid_terminal_transitions(self):
        for old, new in [("IGNORED", "ACTIVE"), ("CLOSED", "ACTIVE"), ("SIGNAL_SENT", "ACTIVE"), ("ACTIVE", "CLOSED")]:
            with self.assertRaises(ValueError):
                transition(old, new)


class DeliveryTests(TrackingCase):
    def test_signal_deduplicated_across_restart(self):
        s = self.signal()
        restarted = Store(self.store.path, self.clock)
        publish_signal(restarted, self.cfg, s["snapshot"])
        Dispatcher(restarted, self.cfg, self.api).drain()
        sends = [m for m, _ in self.api.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)

    def test_429_retries_after_delay(self):
        self.api.fail = TelegramError(429, retry_after=5)
        s = self.signal()
        out = self.store.one("SELECT * FROM mt_outbox WHERE kind='SIGNAL'")
        self.assertEqual(out["state"], "PENDING")
        self.api.fail = None
        self.assertFalse(self.dispatch.dispatch_one())
        self.clock.now += 5
        self.dispatch.drain()
        self.assertEqual(self.store.one("SELECT state FROM mt_outbox WHERE id=?", (out["id"],))["state"], "SENT")

    def test_timeout_unknown_never_automatic_retry(self):
        self.api.fail = TelegramError("TIMEOUT", ambiguous=True)
        self.signal()
        self.assertEqual(self.store.one("SELECT state FROM mt_outbox")["state"], "UNKNOWN")
        self.api.fail = None
        self.clock.now += 100
        self.assertFalse(self.dispatch.dispatch_one())

    def test_forbidden_delivery_recorded(self):
        self.api.fail = TelegramError(403)
        self.signal()
        self.assertEqual(self.store.one("SELECT state FROM mt_outbox")["state"], "FAILED")

    def test_crash_during_send_becomes_unknown(self):
        s = self.signal()
        with self.store.tx() as con:
            con.execute("UPDATE mt_outbox SET state='SENDING'")
        self.dispatch.recover_inflight()
        self.assertEqual(self.store.one("SELECT state FROM mt_outbox")["state"], "UNKNOWN")

    def test_signal_format_and_buttons(self):
        s = self.signal()
        out = self.store.one("SELECT * FROM mt_outbox WHERE kind='SIGNAL'")
        for word in ["Signal ID", "Timeframe", "Waktu sinyal", "Entry referensi", "Usulan SL", "TP", "R/R", "belum merupakan transaksi"]:
            self.assertIn(word, out["payload"]["text"])
        self.assertEqual([b["text"] for b in out["payload"]["reply_markup"]["inline_keyboard"][0]], ["Saya Sudah Entry", "Abaikan Sinyal"])

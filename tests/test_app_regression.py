"""Baseline source invariants and real SQLite app integration; HTTP is mocked.

If requests is unavailable in an offline QA environment, pip's vendored
requests supplies the same import interface for these tests only. Production
continues to require the original requirements.txt dependencies.
"""
import ast
from collections import Counter
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import requests
except ImportError:
    import pip._vendor.requests as requests
    sys.modules["requests"] = requests

os.environ["AI_CONSENSUS_NO_AUTORUN"] = "1"
import app
from manual_tracker.store import Store
from manual_tracker.config import Config
from manual_tracker.telegram import Dispatcher
from tests.helpers import FakeAPI, TrackingCase, observation

ROOT = Path(__file__).resolve().parents[1]


def result(direction="LONG", aid="regression-long"):
    return dict(final=direction, symbol="BTCUSDT", analysis_id=aid, idempotency_key=aid,
                scanner_score=80, entry=100, sl=90 if direction == "LONG" else 110,
                tp=120 if direction == "LONG" else 80, rr=2, entry_status="READY",
                portfolio={"decision": direction, "confidence": 80, "reasoning": "Synthetic regression fixture"},
                packet={"market_regime": "TREND", "smc": {"structure": {"bias": "BULLISH", "bos": "BULLISH BOS"}, "smc_score": 80},
                        "order_book": {"spread": 0.01, "imbalance": 0.1}, "candlestick": {"name": "TEST"}},
                gate_report={"approved": True})


class BaselineTests(unittest.TestCase):
    def test_original_strategy_globals_prompts_and_embedded_modules_unchanged(self):
        manifest = json.loads((ROOT / "tests/baseline_v55.json").read_text())
        changed = set(manifest["allowed_modified_functions"] + manifest["added_functions"])
        nodes = []
        for node in ast.parse((ROOT / "app.py").read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in changed:
                continue
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DB_FILE" for t in node.targets):
                continue
            nodes.append(hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())
        self.assertEqual(nodes, manifest["unchanged_nodes_in_order"])

    def test_original_momentum_outputs_24_scenarios(self):
        cases = json.loads((ROOT / "tests/momentum_fixtures_v55.json").read_text())
        for idx, case in enumerate(cases):
            with self.subTest(case=idx):
                self.assertEqual(app._pattern_score(case["frames"]), case["expected"])

    def test_original_risk_controls_remain_active(self):
        self.assertFalse(app.validate_trade_plan_gate("LONG", 100, 110, 120)["valid"])
        self.assertFalse(app.validate_trade_plan_gate("SHORT", 100, 90, 80)["valid"])
        self.assertFalse(app.preflight_market_check({})["passed"])
        self.assertFalse(app.validate_trade_plan_gate("LONG", 100, 99.999, 100.0001)["valid"])


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_db = app.DB_FILE
        self.addCleanup(setattr, app, "DB_FILE", self.old_db)
        app.DB_FILE = str(Path(self.temp.name) / "app.db")
        app.init_db()
        self.values = {"MANUAL_TRACKING_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "123:TEST_ONLY", "TELEGRAM_CHAT_ID": "99", "MANUAL_TELEGRAM_USER_IDS": "7", "MANUAL_TELEGRAM_CHAT_IDS": "99"}

    def test_original_cli_status_and_schema_startup(self):
        out = app.run_cli_entrypoint(["--status", "--db-file", app.DB_FILE])
        self.assertEqual(out["mode"], "status")
        con = app.db()
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        con.close()

    def test_opt_out_retains_existing_signal_format_and_sender(self):
        for direction in ["LONG", "SHORT"]:
            with patch.object(app, "secret", return_value=""), patch.object(app, "telegram", return_value=(True, "OK")) as sender:
                data = result(direction, "legacy-" + direction)
                state = {}
                stored = app.store_analysis_result(data, state=state)
                out = app.save_rendered_analysis(stored["analysis_id"], state=state, tg_enabled=True)
                self.assertTrue(out["tg_sent"])
                sender.assert_called_once_with(app.telegram_message(stored))
        con = app.db()
        self.assertFalse(con.execute("SELECT 1 FROM sqlite_master WHERE name='mt_positions'").fetchone())
        con.close()

    def test_enabled_long_short_signals_queue_buttons(self):
        with patch.object(app, "secret", side_effect=lambda key: self.values.get(key, "")), patch.object(app, "telegram") as legacy:
            for direction in ["LONG", "SHORT"]:
                state = {}
                stored = app.store_analysis_result(result(direction, "enabled-" + direction), state=state)
                out = app.save_rendered_analysis(stored["analysis_id"], state=state, tg_enabled=True)
                self.assertFalse(out["tg_sent"])  # queued is never called delivered
                self.assertIn("PENDING", out["tg_msg"])
            legacy.assert_not_called()
        store = Store(app.DB_FILE)
        self.assertEqual(len(store.rows("SELECT * FROM mt_signals")), 2)
        self.assertEqual(store.rows("SELECT * FROM mt_positions"), [])
        api = FakeAPI()
        Dispatcher(store, Config.load(self.values.get), api).drain()
        self.assertEqual(len([m for m, _ in api.calls if m == "sendMessage"]), 2)

    def test_gate_rejected_saved_wait_has_no_tracker(self):
        data = result()
        data["sl"] = 101
        with patch.object(app, "secret", side_effect=lambda key: self.values.get(key, "")):
            state = {}
            stored = app.store_analysis_result(data, state=state)
            out = app.save_rendered_analysis(stored["analysis_id"], state=state, tg_enabled=True)
        store = Store(app.DB_FILE)
        saved = store.one("SELECT * FROM signals WHERE id=?", (out["signal_id"],))
        self.assertEqual(saved["final_decision"], "WAIT")
        self.assertEqual(store.rows("SELECT * FROM mt_signals"), [])

    def test_restart_saved_signal_no_new_delivery_or_tracker(self):
        with patch.object(app, "secret", side_effect=lambda key: self.values.get(key, "")):
            data = result()
            for _ in range(2):
                state = {}
                stored = app.store_analysis_result(data, state=state)
                app.save_rendered_analysis(stored["analysis_id"], state=state, tg_enabled=True)
        store = Store(app.DB_FILE)
        self.assertEqual(len(store.rows("SELECT * FROM signals")), 1)
        self.assertEqual(len(store.rows("SELECT * FROM mt_outbox WHERE kind='SIGNAL'")), 1)

    def test_app_does_not_import_tracker_when_disabled(self):
        with patch.object(app, "secret", return_value=""), patch.object(app, "telegram", return_value=(True, "OK")), patch("manual_tracker.bridge.queue_saved_signal") as queue:
            app._telegram_for_saved_signal(result(), 1)
            queue.assert_not_called()


class FakeUI:
    def __init__(self, token, clicked=""):
        self.token, self.clicked, self.tables, self.warnings = token, clicked, [], []
    def expander(self, *args, **kwargs): return self
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def text_input(self, *args, **kwargs): return self.token
    def caption(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def warning(self, text): self.warnings.append(text)
    def button(self, *args, **kwargs): return kwargs.get("key") == self.clicked
    def dataframe(self, rows, **kwargs): self.tables.append(rows)
    def selectbox(self, label, options, **kwargs): return options[0]
    def columns(self, *args, **kwargs): return [self, self]


class DashboardTests(TrackingCase):
    def values(self):
        return {"MANUAL_TRACKING_ENABLED": "true", "TELEGRAM_BOT_TOKEN": self.cfg.token, "TELEGRAM_CHAT_ID": "99", "MANUAL_TELEGRAM_USER_IDS": "7", "MANUAL_DASHBOARD_TOKEN": "local-test-password-123"}

    def test_panel_auth_hides_position_details(self):
        from manual_tracker.dashboard import render_panel
        self.register()
        ui = FakeUI("wrong")
        render_panel(ui, self.store.path, self.values().get)
        self.assertEqual(ui.tables, [])

    def test_panel_columns_and_telegram_confirmation_request(self):
        from manual_tracker.dashboard import render_panel
        p = self.register()
        ui = FakeUI("local-test-password-123", "manual_pause_request")
        render_panel(ui, self.store.path, self.values().get)
        self.assertFalse(ui.warnings)
        self.assertIn("SL rekomendasi", ui.tables[0][0])
        self.assertIn("PnL estimasi USDT", ui.tables[0][0])
        self.assertEqual(self.store.position(p["id"])["state"], "ACTIVE")
        self.assertEqual(len(self.store.rows("SELECT * FROM mt_outbox WHERE id LIKE 'dashboard:%'")), 1)

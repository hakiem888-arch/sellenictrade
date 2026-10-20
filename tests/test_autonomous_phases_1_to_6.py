"""Comprehensive Unit, Integration, Regression, and Backtest Quality Gates for Phases 1-6."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from manual_tracker.account import (
    AccountEngine,
    AccountSyncError,
    ExchangeOrder,
    ExchangePosition,
    InstrumentSpec,
    parse_instrument_spec,
    parse_linear_account_response,
    parse_spot_account_response,
)
from manual_tracker.config import AutonomousConfig, Config, redact_text
from manual_tracker.engine import InvalidData, Observation
from manual_tracker.execution import ExecutionEngine, build_client_order_id
from manual_tracker.live_guard import GuardedLiveController, LIVE_CONFIRMATION_PHRASE
from manual_tracker.orchestrator import AutonomousSystemOrchestrator
from manual_tracker.paper_autonomous import (
    AutonomousPaperEngine,
    compute_environment_performance,
    run_chronological_backtest,
)
from manual_tracker.reliability import (
    CoordinatedGroqLimiter,
    RealtimeStreamProcessor,
    RestartRecoveryManager,
    SystemHealthMonitor,
)
from manual_tracker.risk_engine import DynamicRiskEngine
from manual_tracker.store import Store
from manual_tracker.trailing_manager import SmartTrailingManager
from tests.helpers import Clock, FakeAPI


class FakeExchangeTransport:
    """Deterministic mock transport for Binance Spot and USD-M Futures REST endpoints."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.last_latency_ms = 5.0
        self.calls = []
        self.fail_next_post = None
        self.fail_get_order = None
        self.fail_delete_order = None
        self.reject_sl_order = False
        self.enable_withdrawals = False
        self.wallet_balance = "10000"
        self.available_balance = "10000"
        self.unrealized_pnl = "0"
        self.exchange_positions = []
        self.exchange_open_orders = []
        self.orders_by_coid = {}
        self.order_seq = 1000

    def request(self, environment, instrument, method, path, params=None, signed=False, timeout=5.0):
        params = dict(params or {})
        self.calls.append((environment, instrument, method, path, params, signed))

        if path.endswith("exchangeInfo"):
            return {
                "symbols": [{
                    "symbol": params.get("symbol", "BTCUSDT"),
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "contractType": "PERPETUAL" if instrument == "LINEAR" else "SPOT",
                    "isSpotTradingAllowed": True,
                    "contractSize": "1",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "maxLeverage": 50,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }]
            }
        if path.endswith("premiumIndex"):
            return {"symbol": params.get("symbol", "BTCUSDT"), "markPrice": "100", "lastFundingRate": "0.0001", "time": int(self.clock() * 1000)}

        if path == "/fapi/v2/account":
            eq = D(self.wallet_balance) + D(self.unrealized_pnl)
            return {
                "canTrade": True,
                "canWithdraw": self.enable_withdrawals,
                "enableWithdrawals": self.enable_withdrawals,
                "totalWalletBalance": self.wallet_balance,
                "availableBalance": self.available_balance,
                "totalMarginBalance": str(eq),
                "totalUnrealizedProfit": self.unrealized_pnl,
                "totalInitialMargin": str(max(D("0"), eq - D(self.available_balance))),
                "updateTime": int(self.clock() * 1000),
                "positions": list(self.exchange_positions),
            }
        if path == "/api/v3/account":
            return {
                "canTrade": True,
                "canWithdraw": self.enable_withdrawals,
                "enableWithdrawals": self.enable_withdrawals,
                "updateTime": int(self.clock() * 1000),
                "balances": [
                    {"asset": "USDT", "free": self.available_balance, "locked": "0"},
                    {"asset": "BTC", "free": "1.5", "locked": "0.5"},
                ],
            }
        if path in {"/fapi/v1/openOrders", "/api/v3/openOrders"}:
            return list(self.exchange_open_orders)

        if method == "POST" and path in {"/fapi/v1/order", "/api/v3/order"}:
            if self.fail_next_post:
                err = self.fail_next_post
                self.fail_next_post = None
                raise err
            coid = params.get("newClientOrderId", "")
            otype = params.get("type", "MARKET")
            if self.reject_sl_order and ("STOP" in otype):
                raise AccountSyncError("EXCHANGE_HTTP_400", "Filter failure: STOP_PRICE", status_code=400)
            self.order_seq += 1
            qty = params.get("quantity", "0")
            is_mkt = otype == "MARKET"
            resp = {
                "orderId": self.order_seq,
                "clientOrderId": coid,
                "symbol": params.get("symbol", "BTCUSDT"),
                "status": "FILLED" if is_mkt else "NEW",
                "type": otype,
                "side": params.get("side", "BUY"),
                "origQty": qty,
                "executedQty": qty if is_mkt else "0",
                "avgPrice": "100.00" if is_mkt else "0",
                "price": params.get("price", "0"),
                "stopPrice": params.get("stopPrice", "0"),
                "reduceOnly": params.get("reduceOnly") == "true",
                "updateTime": int(self.clock() * 1000),
            }
            self.orders_by_coid[coid] = resp
            if not is_mkt:
                self.exchange_open_orders.append(resp)
            return resp

        if method == "GET" and path in {"/fapi/v1/order", "/api/v3/order"}:
            if self.fail_get_order:
                err = self.fail_get_order
                self.fail_get_order = None
                raise err
            coid = params.get("origClientOrderId", "")
            if coid in self.orders_by_coid:
                return self.orders_by_coid[coid]
            raise AccountSyncError("EXCHANGE_HTTP_400", '{"code":-2013,"msg":"Order does not exist."}', status_code=400)

        if method == "DELETE" and path in {"/fapi/v1/order", "/api/v3/order"}:
            if self.fail_delete_order:
                err = self.fail_delete_order
                self.fail_delete_order = None
                raise err
            coid = params.get("origClientOrderId", "")
            resp = self.orders_by_coid.get(coid, {"orderId": 999, "clientOrderId": coid, "status": "CANCELED"})
            resp["status"] = "CANCELED"
            self.exchange_open_orders = [o for o in self.exchange_open_orders if o.get("clientOrderId") != coid]
            return resp

        raise AccountSyncError("UNHANDLED_MOCK_PATH", path)


class AutonomousPhasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.db_path = Path(self.tmp.name) / "auto_test.db"
        self.store = Store(self.db_path, self.clock)
        self.manual_cfg = Config(enabled=True, token="123:TEST_TOKEN", chat_id="99", users=frozenset({7}), chats=frozenset({99}))
        self.auto_cfg = AutonomousConfig(
            execution_mode="TESTNET",
            live_trading_enabled=False,
            autonomous_enabled=True,
            default_instrument="LINEAR",
            default_leverage=D("3"),
            binance_testnet_api_key="testnet_key_123456",
            binance_testnet_api_secret="testnet_secret_abcdef",
            binance_api_key="live_key_123456",
            binance_api_secret="live_secret_abcdef",
        )
        self.transport = FakeExchangeTransport(self.clock)
        self.orch = AutonomousSystemOrchestrator(
            self.store,
            self.manual_cfg,
            self.auto_cfg,
            transport=self.transport,
            groq_keys=["gsk_key1", "gsk_key2", "gsk_key3", "gsk_key4", "gsk_key5", "gsk_key6", "gsk_key7"],
        )

    def test_phase1_linear_and_spot_account_parsing_and_no_leverage_as_equity(self):
        snap_lin = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        self.assertEqual(snap_lin.account_equity, D("10000"))
        self.assertEqual(snap_lin.available_margin, D("10000"))
        self.assertTrue(snap_lin.is_fresh(self.clock(), 30.0))

        snap_spot = self.orch.account.sync_account("SPOT", environment="TESTNET", price_map={"BTCUSDT": ("50000", "48000")})
        self.assertEqual(snap_spot.available_balance, D("10000"))
        self.assertEqual(snap_spot.account_equity, D("110000"))
        self.assertEqual(snap_spot.unrealized_pnl, D("4000"))

    def test_phase1_stale_and_inconsistent_account_data_rejected(self):
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        self.clock.now += 35.0
        self.assertFalse(snap.is_fresh(self.clock(), 30.0))

        with self.assertRaises(InvalidData):
            parse_linear_account_response({
                "canTrade": True,
                "totalWalletBalance": "10000",
                "availableBalance": "10000",
                "totalMarginBalance": "15000",
                "totalUnrealizedProfit": "0",
            }, [], "TESTNET", self.clock())

    def test_phase1_position_reconciliation_detects_external_open_and_closure(self):
        self.transport.exchange_positions = [{
            "symbol": "BTCUSDT",
            "positionAmt": "0.250",
            "entryPrice": "60000",
            "markPrice": "60500",
            "unrealizedProfit": "125",
            "leverage": "5",
            "isolated": True,
            "isolatedWallet": "3000",
            "updateTime": int(self.clock() * 1000),
        }]
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        rec = self.orch.account.reconcile(snap, trigger_source="STARTUP")
        self.assertEqual(rec["status"], "DISCREPANCY_DETECTED")
        types = [d["type"] for d in rec["discrepancies"]]
        self.assertIn("EXTERNAL_MANUAL_POSITION", types)

        pos_rows = self.store.rows("SELECT * FROM at_positions WHERE symbol='BTCUSDT'")
        self.assertEqual(len(pos_rows), 1)
        self.assertEqual(pos_rows[0]["position_state"], "HALTED_DISCREPANCY")

        with self.store.tx() as con:
            con.execute("UPDATE at_positions SET position_state='PROTECTED_ACTIVE' WHERE id=?", (pos_rows[0]["id"],))
        self.transport.exchange_positions = []
        snap2 = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        rec2 = self.orch.account.reconcile(snap2, trigger_source="PERIODIC")
        self.assertIn("EXTERNAL_MANUAL_CLOSURE", [d["type"] for d in rec2["discrepancies"]])
        self.assertEqual(self.store.at_position(pos_rows[0]["id"])["position_state"], "CLOSED")

    def test_phase2_position_sizing_and_never_increasing_risk_for_min_size(self):
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        spec = self.orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="TESTNET")

        sizing = self.orch.risk.calculate_position_size(snap, spec, "LONG", "100", "90", "120")
        self.assertEqual(sizing.risk_budget_usdt, D("50.000"))
        self.assertLessEqual(sizing.estimated_total_risk_usdt, sizing.risk_budget_usdt)
        self.assertGreater(sizing.rounded_quantity, D("0"))
        self.assertEqual(sizing.rounded_quantity % spec.step_size, D("0"))

        decision = self.orch.risk.validate_entry(
            account=snap,
            spec=spec,
            direction="LONG",
            entry_price="100000",
            stop_loss="10000",
            take_profit="300000",
            market_data_timestamp=self.clock(),
            signal_timestamp=self.clock(),
        )
        self.assertFalse(decision.approved)
        self.assertIn("MIN_SIZE_EXCEEDS_RISK_BUDGET", decision.reason_codes)

    def test_phase2_daily_loss_limit_and_circuit_breaker_persist_across_restart(self):
        self.orch.risk.record_trade_outcome(
            environment="TESTNET",
            gross_pnl=D("-240"),
            net_pnl=D("-250"),
            fees_paid=D("10"),
            current_equity=D("10000"),
        )
        restarted_store = Store(self.db_path, self.clock)
        restarted_risk = DynamicRiskEngine(restarted_store, self.auto_cfg)
        breakers = restarted_risk.active_breakers()
        self.assertTrue(any(b["reason"] == "DAILY_LOSS_LIMIT_BREACH" for b in breakers))

        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        spec = self.orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="TESTNET")
        decision = restarted_risk.validate_entry(
            account=snap,
            spec=spec,
            direction="LONG",
            entry_price="100",
            stop_loss="95",
            take_profit="115",
            market_data_timestamp=self.clock(),
            signal_timestamp=self.clock(),
        )
        self.assertFalse(decision.approved)
        self.assertTrue(any("DAILY_LOSS_LIMIT" in r for r in decision.reason_codes))

    def test_phase3_paper_lifecycle_intrabar_tie_breaker_and_environment_isolation(self):
        sig = {
            "id": "sig_paper_1",
            "symbol": "BTCUSDT",
            "timeframe": "5m/15m/1h",
            "final": "LONG",
            "entry": "100",
            "sl": "90",
            "tp": "120",
            "rr": 2.0,
            "gate_report": {"approved": True},
        }
        res = self.orch.paper.process_signal(sig, market_price=D("100"), market_timestamp=self.clock(), environment="PAPER")
        self.assertEqual(res["status"], "OPENED")
        pid = res["position_id"]
        pos = self.store.at_position(pid)
        self.assertGreater(D(pos["entry_price"]), D("100"))

        self.clock.now += 60
        pos = self.orch.paper.step_candle(
            pid,
            {"open": "101", "high": "112", "low": "100.5", "close": "111", "close_time": self.clock(), "interval_seconds": 60},
            atr_val=D("2"),
        )
        self.assertEqual(pos["position_state"], "PROTECTED_ACTIVE")
        self.assertEqual(pos["activated"], 1)
        self.assertGreater(D(pos["current_stop"]), D("100"))

        self.clock.now += 60
        pos = self.orch.paper.step_candle(
            pid,
            {"open": "110", "high": "125", "low": "95", "close": "115", "close_time": self.clock(), "interval_seconds": 60},
            atr_val=D("2"),
            tie_breaker="SL_FIRST_CONSERVATIVE",
        )
        self.assertEqual(pos["position_state"], "CLOSED")
        self.assertEqual(pos["exit_reason"], "STOP_LOSS_INTRABAR_TIE")

        bt_perf = run_chronological_backtest(
            self.orch.paper,
            [{
                "signal": {"id": "bt_1", "symbol": "BTCUSDT", "final": "SHORT", "entry": "100", "sl": "110", "tp": "80", "gate_report": {"approved": True}},
                "entry_price": "100",
                "timestamp": self.clock() + 100,
                "atr": "2",
                "bars": [
                    {"open": "99", "high": "100", "low": "78", "close": "79", "close_time": self.clock() + 160, "interval_seconds": 60},
                ],
            }],
        )
        paper_perf = compute_environment_performance(self.store, "PAPER")
        live_perf = compute_environment_performance(self.store, "LIVE")
        self.assertEqual(bt_perf["sample_size"], 1)
        self.assertEqual(paper_perf["sample_size"], 1)
        self.assertEqual(live_perf["sample_size"], 0)
        self.assertEqual(bt_perf["by_direction"]["SHORT"]["wins"], 1)

    def test_phase4_order_timeout_reconciliation_prevents_duplicate_submission(self):
        coid = build_client_order_id("TESTNET", "pos_timeout_1", "ENT", 1)
        self.orch.execution.persist_order_intent(
            client_order_id=coid,
            environment="TESTNET",
            position_id="pos_timeout_1",
            signal_id="sig_1",
            instrument="LINEAR",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            role="ENTRY",
            requested_qty=D("0.5"),
        )
        self.transport.fail_next_post = AccountSyncError("EXCHANGE_NETWORK_TIMEOUT", "Read timed out")
        self.transport.orders_by_coid[coid] = {
            "orderId": 5555,
            "clientOrderId": coid,
            "symbol": "BTCUSDT",
            "status": "FILLED",
            "type": "MARKET",
            "side": "BUY",
            "origQty": "0.5",
            "executedQty": "0.5",
            "avgPrice": "100.00",
            "updateTime": int(self.clock() * 1000),
        }
        ord_res = self.orch.execution.submit_exchange_order(coid)
        post_calls = [c for c in self.transport.calls if c[2] == "POST"]
        get_calls = [c for c in self.transport.calls if c[2] == "GET" and c[3].endswith("/order")]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(len(get_calls), 1)
        self.assertEqual(ord_res["order_state"], "FILLED")
        self.assertEqual(ord_res["filled_qty"], "0.5")

    def test_phase4_protective_order_failure_trips_breaker_and_applies_policy(self):
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        spec = self.orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="TESTNET")
        sizing = self.orch.risk.calculate_position_size(snap, spec, "LONG", "100", "90", "120")

        self.transport.reject_sl_order = True
        pos = self.orch.execution.execute_authorized_entry(
            signal_id="sig_prot_fail",
            sizing=sizing,
            spec=spec,
            environment="TESTNET",
        )
        self.assertEqual(pos["position_state"], "PROTECTION_REQUIRED")
        self.assertEqual(pos["protective_order_state"], "PROTECTION_FAILED")
        self.assertEqual(pos["requires_attention"], 1)
        breakers = self.orch.risk.active_breakers()
        self.assertTrue(any(b["reason"] == "MISSING_PROTECTIVE_ORDERS" for b in breakers))

    def test_phase5_live_trading_disabled_by_default_and_requires_explicit_activation(self):
        default_cfg = AutonomousConfig.load(lambda k: None)
        self.assertEqual(default_cfg.execution_mode, "PAPER")
        self.assertFalse(default_cfg.live_trading_enabled)

        live_cfg = replace(self.auto_cfg, execution_mode="LIVE", live_trading_enabled=True)
        live_orch = AutonomousSystemOrchestrator(self.store, self.manual_cfg, live_cfg, transport=self.transport)
        snap = live_orch.account.sync_account("LINEAR", environment="LIVE")
        live_orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="LIVE")
        live_orch.account.reconcile(snap, trigger_source="STARTUP")

        readiness = live_orch.live_guard.evaluate_live_readiness("BTCUSDT", market_data_timestamp=self.clock())
        self.assertTrue(readiness["readiness_passed"])
        self.assertFalse(readiness["explicit_user_activated"])
        self.assertFalse(readiness["live_trading_operational"])

        sig = {
            "id": "sig_live_1",
            "symbol": "BTCUSDT",
            "final": "LONG",
            "entry": "100",
            "sl": "90",
            "tp": "120",
            "signal_timestamp": self.clock(),
            "gate_report": {"approved": True},
            "portfolio": {"decision": "LONG", "confidence": 85},
        }
        blocked_res = live_orch.live_guard.process_candidate_entry(sig, D("100"), self.clock())
        self.assertFalse(blocked_res["authorized"])
        self.assertEqual(blocked_res["failed_step"], "7_EXECUTION_GATE")

        self.transport.enable_withdrawals = True
        live_orch.account.sync_account("LINEAR", environment="LIVE")
        with self.assertRaises(InvalidData):
            live_orch.live_guard.activate_live_trading("operator_7", LIVE_CONFIRMATION_PHRASE, market_data_timestamp=self.clock())

        self.transport.enable_withdrawals = False
        live_orch.account.sync_account("LINEAR", environment="LIVE")
        act = live_orch.live_guard.activate_live_trading("operator_7", LIVE_CONFIRMATION_PHRASE, market_data_timestamp=self.clock())
        self.assertTrue(act["authorized"])

        ok_res = live_orch.live_guard.process_candidate_entry(sig, D("100"), self.clock())
        self.assertTrue(ok_res["authorized"])
        self.assertEqual(ok_res["status"], "PROTECTED_ACTIVE")

    def test_phase5_ai_outage_and_kill_switch_unconfirmed_cancellation(self):
        sig = {
            "id": "sig_ai_outage",
            "symbol": "BTCUSDT",
            "final": "LONG",
            "entry": "100",
            "sl": "90",
            "tp": "120",
            "signal_timestamp": self.clock(),
            "gate_report": {"approved": True},
            "portfolio": {"decision": "LONG", "confidence": 85},
        }
        res = self.orch.live_guard.process_candidate_entry(sig, D("100"), self.clock(), ai_service_available=False)
        self.assertFalse(res["authorized"])
        self.assertEqual(res["reason"], "AI_SERVICE_UNAVAILABLE_FAIL_CLOSED")

        coid = build_client_order_id("TESTNET", "pos_pend", "ENT", 1)
        self.orch.execution.persist_order_intent(
            coid, "TESTNET", "pos_pend", "sig_p", "LINEAR", "BTCUSDT", "BUY", "LIMIT", "ENTRY", D("0.1"), price=D("99")
        )
        self.orch.execution.submit_exchange_order(coid)
        self.transport.fail_delete_order = AccountSyncError("EXCHANGE_NETWORK_TIMEOUT", "Delete timed out")

        ks_rep = self.orch.live_guard.engage_kill_switch(actor="op", reason="TEST_HALT", close_all_positions=False)
        self.assertTrue(ks_rep["kill_switch_active"])
        self.assertEqual(len(ks_rep["unconfirmed_cancellations"]), 1)
        self.assertEqual(ks_rep["canceled_pending_entries"], [])

    def test_section10_advisory_and_automatic_trailing_monotonicity_and_uncertainty(self):
        sig = {
            "id": "sig_trail_1",
            "symbol": "BTCUSDT",
            "final": "LONG",
            "entry": "100",
            "sl": "90",
            "tp": "130",
            "signal_timestamp": self.clock(),
            "gate_report": {"approved": True},
            "portfolio": {"decision": "LONG", "confidence": 85},
        }
        opened = self.orch.live_guard.process_candidate_entry(sig, D("100"), self.clock())
        pid = opened["position_id"]

        self.clock.now += 10
        obs_1r = Observation(price=D("110"), timestamp=self.clock(), atr=D("2"), atr_timestamp=self.clock() - 1, tick=D("0.01"), reference="MARK", interval_seconds=60)
        res_adv = self.orch.trailing.evaluate_autonomous_position(pid, obs_1r)
        self.assertEqual(res_adv["mode"], "ADVISORY")
        self.assertFalse(res_adv["exchange_confirmed"])
        self.assertEqual(self.store.at_position(pid)["last_confirmed_exchange_stop"], "90")

        auto_trail_cfg = replace(self.auto_cfg, trailing_mode="AUTOMATIC", auto_trailing_enabled=True)
        self.orch.trailing.auto_cfg = auto_trail_cfg
        with self.store.tx() as con:
            con.execute("UPDATE at_positions SET trailing_mode='AUTOMATIC' WHERE id=?", (pid,))

        self.clock.now += 10
        obs_atr = Observation(price=D("120"), timestamp=self.clock(), atr=D("2"), atr_timestamp=self.clock() - 1, tick=D("0.01"), reference="MARK", interval_seconds=60)
        res_auto = self.orch.trailing.evaluate_autonomous_position(pid, obs_atr)
        self.assertEqual(res_auto["mode"], "AUTOMATIC")
        self.assertTrue(res_auto["exchange_confirmed"])
        self.assertEqual(res_auto["confirmed_stop"], "116")
        self.assertEqual(self.store.at_position(pid)["current_stop"], "116")

        self.clock.now += 10
        obs_dip = Observation(price=D("118"), timestamp=self.clock(), atr=D("10"), atr_timestamp=self.clock() - 1, tick=D("0.01"), reference="MARK", interval_seconds=60)
        res_dip = self.orch.trailing.evaluate_autonomous_position(pid, obs_dip)
        self.assertFalse(res_dip["trailing_changed"])
        self.assertEqual(self.store.at_position(pid)["current_stop"], "116")

        self.transport.reject_sl_order = True
        self.clock.now += 10
        obs_higher = Observation(price=D("125"), timestamp=self.clock(), atr=D("2"), atr_timestamp=self.clock() - 1, tick=D("0.01"), reference="MARK", interval_seconds=60)
        res_unc = self.orch.trailing.evaluate_autonomous_position(pid, obs_higher)
        self.assertFalse(res_unc["exchange_confirmed"])
        self.assertEqual(res_unc["state"], "MODIFICATION_UNCERTAIN")
        self.assertEqual(self.store.at_position(pid)["last_confirmed_exchange_stop"], "116")

    def test_phase6_websocket_closed_candles_groq_limiter_and_restart_recovery(self):
        ws = self.orch.ws_processor
        ws.on_connect()
        intrabar = ws.process_kline_event("LINEAR", {
            "e": "kline", "E": 1700000010000, "s": "BTCUSDT",
            "k": {"t": 1700000000000, "T": 1700000059999, "s": "BTCUSDT", "i": "1m", "o": "100", "h": "105", "l": "99", "c": "103", "x": False},
        })
        self.assertFalse(intrabar["is_closed"])
        self.assertFalse(intrabar["atr_recalculated"])

        base_ms = 1700000000000
        for i in range(15):
            t_open = base_ms + i * 60000
            t_close = t_open + 59999
            closed_ev = ws.process_kline_event("LINEAR", {
                "e": "kline", "E": t_close, "s": "BTCUSDT",
                "k": {"t": t_open, "T": t_close, "s": "BTCUSDT", "i": "1m", "o": "100", "h": "102", "l": "98", "c": "100", "x": True},
            })
        self.assertTrue(closed_ev["is_closed"])
        self.assertTrue(closed_ev["atr_recalculated"])
        self.assertEqual(closed_ev["atr"], "4")

        with self.assertRaises(InvalidData):
            ws.process_kline_event("LINEAR", {
                "e": "kline", "E": base_ms, "s": "BTCUSDT",
                "k": {"t": base_ms, "T": base_ms + 59999, "s": "BTCUSDT", "i": "1m", "o": "100", "h": "102", "l": "98", "c": "100", "x": True},
            })

        limiter = self.orch.groq_limiter
        self.assertEqual(limiter.key_count(), 7)
        slot1 = limiter.acquire_slot("llama-3.3-70b", signal_timestamp=self.clock())
        self.assertTrue(slot1["allowed"])
        slot2 = limiter.acquire_slot("llama-3.3-70b", signal_timestamp=self.clock())
        self.assertFalse(slot2["allowed"])
        self.assertEqual(slot2["reason"], "MIN_DELAY_THROTTLE")

        limiter.record_rate_limit(retry_after_seconds=25.0)
        self.clock.now += 5.0
        slot3 = limiter.acquire_slot("llama-3.3-70b", signal_timestamp=self.clock())
        self.assertFalse(slot3["allowed"])
        self.assertEqual(slot3["reason"], "ORG_RATE_LIMIT_COOLDOWN")

        rec_rep = self.orch.recovery.recover_on_startup("LINEAR")
        self.assertTrue(rec_rep["recovered"])
        self.assertTrue(rec_rep["new_entries_permitted"])
        self.assertEqual(len(rec_rep["steps"]), 8)

    def test_phase6_telegram_commands_and_secret_redaction(self):
        raw = "Error at https://api.telegram.org/bot123456:SECRET_BOT_TOK/sendMessage?signature=abcdef0123456789abcdef0123456789 key=testnet_secret_abcdef"
        cleaned = redact_text(raw, self.auto_cfg.secrets_tuple())
        self.assertNotIn("SECRET_BOT_TOK", cleaned)
        self.assertNotIn("testnet_secret_abcdef", cleaned)
        self.assertNotIn("abcdef0123456789abcdef0123456789", cleaned)

        from manual_tracker.interactions import Interactions
        handler = Interactions(self.store, self.manual_cfg, self.auto_cfg)
        self.assertEqual(
            handler.process({"update_id": 501, "message": {"from": {"id": 7}, "chat": {"id": 99}, "text": "/status"}}),
            "STATUS",
        )
        self.assertEqual(
            handler.process({"update_id": 502, "message": {"from": {"id": 7}, "chat": {"id": 99}, "text": "/killswitch"}}),
            "KILL_SWITCH_ENGAGED",
        )
        self.assertTrue(self.store.risk_state("kill_switch")["active"])
        self.assertEqual(
            handler.process({"update_id": 503, "message": {"from": {"id": 7}, "chat": {"id": 99}, "text": "/resume_entries"}}),
            "KILL_SWITCH_DISENGAGED",
        )
        self.assertFalse(self.store.risk_state("kill_switch")["active"])



    def test_phase1_rate_limit_429_cooldown_and_retry_on_500_get_only(self):
        from io import BytesIO
        from urllib.error import HTTPError
        from manual_tracker.account import SignedBinanceTransport
        calls = []
        def fake_opener(req, timeout=5.0):
            calls.append((req.get_method(), req.full_url))
            if len(calls) == 1:
                raise HTTPError(req.full_url, 502, "Bad Gateway", {}, BytesIO(b"upstream error"))
            if len(calls) == 2:
                return BytesIO(b'{"serverTime": 1700000040000}')
            raise HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "15"}, BytesIO(b'{"msg":"rate limit"}'))

        t = SignedBinanceTransport(self.auto_cfg, opener=fake_opener, clock=self.clock, max_retries=2)
        # GET retries once on 502 and succeeds on 2nd attempt
        res = t.request("TESTNET", "LINEAR", "GET", "/fapi/v1/time", signed=False)
        self.assertEqual(res["serverTime"], 1700000040000)
        self.assertEqual(len(calls), 2)

        # Next call hits 429 and sets cooldown for 15s
        with self.assertRaises(AccountSyncError) as ctx:
            t.request("TESTNET", "LINEAR", "GET", "/fapi/v1/time", signed=False)
        self.assertEqual(ctx.exception.code, "EXCHANGE_RATE_LIMIT")
        with self.assertRaises(AccountSyncError) as ctx2:
            t.request("TESTNET", "LINEAR", "GET", "/fapi/v1/time", signed=False)
        self.assertEqual(ctx2.exception.code, "RATE_LIMIT_COOLDOWN")

    def test_phase2_short_position_sizing_spot_short_rejection_and_margin_leverage_limits(self):
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        spec = self.orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="TESTNET")

        # Short position sizing: Entry=100, SL=110, TP=80
        short_sz = self.orch.risk.calculate_position_size(snap, spec, "SHORT", "100", "110", "80")
        self.assertEqual(short_sz.direction, "SHORT")
        self.assertGreater(short_sz.rounded_quantity, D("0"))
        self.assertLessEqual(short_sz.estimated_total_risk_usdt, short_sz.risk_budget_usdt)

        # Spot SHORT must be rejected
        spot_spec = replace(spec, instrument="SPOT", max_leverage=1)
        with self.assertRaises(InvalidData):
            self.orch.risk.calculate_position_size(snap, spot_spec, "SHORT", "100", "110", "80")

        # Excessive leverage rejected
        dec_lev = self.orch.risk.validate_entry(
            snap, spec, "LONG", "100", "90", "120", leverage=D("25"),
            market_data_timestamp=self.clock(), signal_timestamp=self.clock(),
        )
        self.assertFalse(dec_lev.approved)
        self.assertIn("LEVERAGE_EXCEEDS_MAXIMUM", dec_lev.reason_codes)

        # Poor R:R rejected (Entry=100, SL=90, TP=105 -> R:R = 0.5 < 1.5)
        dec_rr = self.orch.risk.validate_entry(
            snap, spec, "LONG", "100", "90", "105",
            market_data_timestamp=self.clock(), signal_timestamp=self.clock(),
        )
        self.assertFalse(dec_rr.approved)
        self.assertIn("RISK_REWARD_BELOW_MINIMUM", dec_rr.reason_codes)

    def test_phase2_portfolio_exposure_asset_cap_and_concurrent_positions_limit(self):
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        spec = self.orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="TESTNET")

        # Open 3 positions (max_concurrent_positions = 3)
        for i, sym in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"], start=1):
            s_spec = replace(spec, symbol=sym)
            sz = self.orch.risk.calculate_position_size(snap, s_spec, "LONG", "100", "90", "120")
            self.orch.execution.execute_authorized_entry(f"sig_c_{i}", sz, s_spec, "TESTNET")

        dec = self.orch.risk.validate_entry(
            snap, spec, "LONG", "100", "90", "120",
            market_data_timestamp=self.clock(), signal_timestamp=self.clock(),
        )
        self.assertFalse(dec.approved)
        self.assertIn("MAX_CONCURRENT_POSITIONS_REACHED", dec.reason_codes)

    def test_phase3_partial_fill_funding_accrual_gap_stop_and_limit_orders(self):
        sig = {
            "id": "sig_limit_1",
            "symbol": "BTCUSDT",
            "final": "LONG",
            "entry": "100",
            "sl": "90",
            "tp": "120",
            "gate_report": {"approved": True},
        }
        # Limit order when market price (105) is above limit entry (100) does not fill
        unfilled = self.orch.paper.process_signal(
            sig, market_price=D("105"), market_timestamp=self.clock(), order_type="LIMIT", environment="PAPER"
        )
        self.assertEqual(unfilled["status"], "LIMIT_UNFILLED")

        # Partial fill (50%) market order
        opened = self.orch.paper.process_signal(
            sig, market_price=D("100"), market_timestamp=self.clock(), partial_fill_ratio=D("0.5"), environment="PAPER"
        )
        self.assertEqual(opened["status"], "OPENED")
        pid = opened["position_id"]
        self.assertEqual(opened["position"]["order_state"], "PARTIALLY_FILLED")

        # Step candle with funding accrual and gap open below SL (open=85 < SL=90)
        self.clock.now += 60
        closed = self.orch.paper.step_candle(
            pid,
            {"open": "85", "high": "88", "low": "84", "close": "86", "close_time": self.clock(), "interval_seconds": 60},
            atr_val=D("2"),
            accrue_funding=True,
        )
        self.assertEqual(closed["position_state"], "CLOSED")
        self.assertEqual(closed["exit_reason"], "STOP_LOSS")
        # Exit price reflects the gap-down open (85 minus slippage), not the 90 stop price!
        self.assertLessEqual(D(closed["exit_price"]), D("85"))
        self.assertGreater(D(closed["funding_paid_usdt"]), D("0"))

    def test_phase4_partial_entry_fill_updates_protective_order_quantity_and_emergency_close_policy(self):
        snap = self.orch.account.sync_account("LINEAR", environment="TESTNET")
        spec = self.orch.account.sync_instrument_spec("LINEAR", "BTCUSDT", environment="TESTNET")
        sizing = self.orch.risk.calculate_position_size(snap, spec, "LONG", "100", "90", "120")

        pos = self.orch.execution.execute_authorized_entry("sig_pf_1", sizing, spec, "TESTNET")
        pid = pos["id"]
        ent_coid = build_client_order_id("TESTNET", pid, "ENT", 1)

        # Simulate private WebSocket partial fill update increasing filled quantity to 6.000
        ev_res = self.orch.ws_processor.process_private_order_event(
            "TESTNET",
            {"o": {"c": ent_coid, "X": "FILLED", "z": "6.000", "ap": "100.10", "i": "8888"}},
            execution_engine=self.orch.execution,
        )
        self.assertTrue(ev_res["handled"])
        updated_pos = self.store.at_position(pid)
        self.assertEqual(updated_pos["quantity"], "6")
        self.assertEqual(updated_pos["protective_order_state"], "VERIFIED")

        # Test EMERGENCY_MARKET_CLOSE policy when protective order fails
        em_cfg = replace(self.auto_cfg, emergency_unprotected_policy="EMERGENCY_MARKET_CLOSE")
        em_orch = AutonomousSystemOrchestrator(self.store, self.manual_cfg, em_cfg, transport=self.transport)
        self.transport.reject_sl_order = True
        pos_em = em_orch.execution.execute_authorized_entry("sig_em_close", sizing, spec, "TESTNET")
        self.assertEqual(pos_em["position_state"], "CLOSED")
        self.assertEqual(pos_em["exit_reason"], "EMERGENCY_UNPROTECTED_POLICY_CLOSE")

    def test_phase5_duplicate_signal_prevention_late_ai_rejection_and_emergency_close_all(self):
        sig = {
            "id": "sig_dup_1",
            "symbol": "BTCUSDT",
            "final": "LONG",
            "entry": "100",
            "sl": "90",
            "tp": "120",
            "signal_timestamp": self.clock(),
            "gate_report": {"approved": True},
            "portfolio": {"decision": "LONG", "confidence": 85},
        }
        first = self.orch.live_guard.process_candidate_entry(sig, D("100"), self.clock())
        self.assertTrue(first["authorized"])

        # Duplicate signal ID must be rejected at Step 1 (never reopening existing/previous signal!)
        second = self.orch.live_guard.process_candidate_entry(sig, D("100"), self.clock())
        self.assertFalse(second["authorized"])
        self.assertEqual(second["reason"], "DUPLICATE_SIGNAL_ALREADY_PROCESSED")

        # Late AI response (arriving after max_signal_age_seconds) must be rejected at Step 2
        sig2 = {**sig, "id": "sig_late_ai"}
        late = self.orch.live_guard.process_candidate_entry(
            sig2, D("100"), self.clock(), ai_response_timestamp=self.clock() + 300.0
        )
        self.assertFalse(late["authorized"])
        self.assertEqual(late["reason"], "LATE_AI_RESPONSE_EXPIRED_SIGNAL")

        # Kill Switch with close_all_positions=True closes open positions via market order
        ks_close = self.orch.live_guard.engage_kill_switch(actor="op", reason="EMERGENCY", close_all_positions=True)
        self.assertIn(first["position_id"], ks_close["closed_positions"])
        self.assertEqual(self.store.at_position(first["position_id"])["position_state"], "CLOSED")

    def test_cli_check_migrate_and_status_autonomous_output(self):
        from manual_tracker.worker import main
        import os
        from unittest.mock import patch
        env = {
            "MANUAL_TRACKING_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "123456:ABC_DEF_123456",
            "TELEGRAM_CHAT_ID": "99",
            "MANUAL_TELEGRAM_USER_IDS": "7",
            "MANUAL_TELEGRAM_CHAT_IDS": "99",
            "EXECUTION_MODE": "PAPER",
            "LIVE_TRADING_ENABLED": "false",
        }
        import contextlib, io
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--check", "--db", str(self.db_path)]), 0)
            self.assertEqual(main(["--migrate", "--db", str(self.db_path)]), 0)
            self.assertEqual(main(["--status", "--db", str(self.db_path)]), 0)
        self.assertIn("VALID", buf.getvalue())
        self.assertIn("autonomous", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

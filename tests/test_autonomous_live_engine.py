from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import requests

import app
from ai_consensus.config import (
    DEFAULT_EXECUTION_MODE,
    DEFAULT_LIVE_TRADING_ENABLED,
    get_exchange_credentials,
     redact_secrets,
    resolve_execution_mode_config,
)
from ai_consensus.exchange import (
    AutonomousExecutionEngine,
    BinanceExchangeAdapter,
    ExchangeAccountSnapshot,
    ExchangeOrderRecord,
    ExchangePositionRecord,
    ExecutionMode,
    InstrumentTradingRules,
    LivePositionLifecycleState,
    activate_kill_switch,
    close_live_position,
    deactivate_kill_switch,
    emergency_close_all_positions,
    evaluate_and_persist_circuit_breakers,
    evaluate_live_readiness_gate,
    generate_client_order_id,
    parse_binance_exchange_info,
    parse_binance_futures_account,
    parse_binance_spot_account,
    reconcile_live_exchange_state,
)
from ai_consensus.risk import (
    compute_position_sizing,
    evaluate_dynamic_protective_levels,
)
from ai_consensus.storage import (
    get_circuit_breaker_and_kill_switch_state,
    get_live_order,
    get_live_position,
    list_execution_audit_logs,
    list_live_orders,
    list_live_positions,
    run_migrations,
    upsert_live_position,
)


def make_mock_exchange_info() -> dict:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "contractSize": 1.0,
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100.0"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            },
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "baseAsset": "ETH",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "contractSize": 1.0,
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000.0"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            },
        ]
    }


def make_mock_futures_account(
    wallet: float = 10000.0,
    unrealized: float = 0.0,
    available: float = 10000.0,
    used_margin: float = 0.0,
    positions: list | None = None,
) -> dict:
    return {
        "totalWalletBalance": str(wallet),
        "totalUnrealizedProfit": str(unrealized),
        "totalMarginBalance": str(wallet + unrealized),
        "availableBalance": str(available),
        "totalInitialMargin": str(used_margin),
        "totalRealizedProfit": "0.0",
        "canTrade": True,
        "positions": positions or [],
    }


class MockHTTPResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class TestAutonomousLiveEngineUnit(unittest.TestCase):
    """Master Prompt 2 Section 15: Unit tests for account parsing, dynamic risk sizing, protective SL/TP, and gating."""

    def test_01_spot_and_futures_account_balance_parsing_and_semantics(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Spot account: available funds come strictly from free quote asset (USDT), not locked reservations
        spot_raw = {
            "canTrade": True,
            "permissions": ["SPOT"],
            "balances": [
                {"asset": "USDT", "free": "8500.00", "locked": "1500.00"},
                {"asset": "BTC", "free": "0.05", "locked": "0.0"},
            ],
        }
        spot_snap = parse_binance_spot_account(
            spot_raw,
            price_map={"BTCUSDT": 60000.0},
            api_restrictions={"enableWithdrawals": False},
            sync_ms=now_ms,
            now_ms=now_ms,
        )
        self.assertTrue(spot_snap.sync_ok)
        self.assertTrue(spot_snap.is_consistent)
        self.assertTrue(spot_snap.is_fresh)
        self.assertAlmostEqual(spot_snap.available_balance, 8500.0)
        self.assertAlmostEqual(spot_snap.total_wallet_balance, 8500.0 + 1500.0 + (0.05 * 60000.0))
        self.assertFalse(spot_snap.withdrawal_enabled)
        self.assertTrue(spot_snap.can_open_live_entries)

        # Futures account: distinguishes wallet balance, account equity (wallet + uPnL), available margin, and used margin
        fut_raw = make_mock_futures_account(
            wallet=10000.0,
            unrealized=-250.0,
            available=8250.0,
            used_margin=1500.0,
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.050",
                    "entryPrice": "60000.0",
                    "markPrice": "55000.0",
                    "unRealizedProfit": "-250.0",
                    "leverage": "2",
                    "isolated": True,
                }
            ],
        )
        fut_snap = parse_binance_futures_account(fut_raw, sync_ms=now_ms, now_ms=now_ms)
        self.assertTrue(fut_snap.sync_ok)
        self.assertTrue(fut_snap.is_consistent)
        self.assertAlmostEqual(fut_snap.total_wallet_balance, 10000.0)
        self.assertAlmostEqual(fut_snap.account_equity, 9750.0)
        self.assertAlmostEqual(fut_snap.unrealized_pnl, -250.0)
        self.assertAlmostEqual(fut_snap.available_margin, 8250.0)
        self.assertAlmostEqual(fut_snap.used_margin, 1500.0)
        self.assertEqual(len(fut_snap.open_positions), 1)
        self.assertEqual(fut_snap.open_positions[0].direction, "LONG")

        # Stale balance (> max_freshness_seconds) must block live entries
        stale_snap = parse_binance_futures_account(
            fut_raw,
            sync_ms=now_ms - 120_000,
            now_ms=now_ms,
            max_freshness_seconds=60.0,
        )
        self.assertFalse(stale_snap.is_fresh)
        self.assertFalse(stale_snap.can_open_live_entries)
        self.assertIn("STALE_ACCOUNT_BALANCE", stale_snap.veto_codes)

        # Inconsistent balance (equity != wallet + uPnL) must block live entries
        bad_fut = dict(fut_raw)
        bad_fut["totalMarginBalance"] = "25000.0"  # Fabricated/inconsistent equity
        bad_snap = parse_binance_futures_account(bad_fut, sync_ms=now_ms, now_ms=now_ms)
        self.assertFalse(bad_snap.is_consistent)
        self.assertFalse(bad_snap.can_open_live_entries)
        self.assertIn("INCONSISTENT_ACCOUNT_DATA", bad_snap.veto_codes)

    def test_02_risk_budget_linear_and_inverse_sizing_and_rounding(self):
        # Linear LONG sizing with tick_size=0.10, lot_size=0.001
        res_long = compute_position_sizing(
            "LONG",
            entry=60000.05,
            stop_loss=59000.05,
            take_profit=62500.05,
            symbol="BTCUSDT",
            equity=10000.0,
            risk_fraction=0.01,  # $100 risk budget
            contract_multiplier=1.0,
            tick_size=0.10,
            lot_size=0.001,
            min_qty=0.001,
            min_notional=5.0,
            available_margin=5000.0,
            leverage=2.0,
            max_leverage=5.0,
            contract_type="LINEAR",
        )
        self.assertTrue(res_long.valid)
        self.assertAlmostEqual(res_long.risk_budget, 100.0)
        self.assertLessEqual(res_long.max_loss_net, res_long.risk_budget + 1e-6)

        # Inverse SHORT sizing (1 contract = $100 USD)
        res_inv = compute_position_sizing(
            "SHORT",
            entry=50000.0,
            stop_loss=52000.0,
            take_profit=45000.0,
            symbol="BTCUSD_PERP",
            equity=10000.0,
            risk_fraction=0.01,
            contract_multiplier=100.0,
            tick_size=0.5,
            lot_size=1.0,
            min_qty=1.0,
            min_notional=100.0,
            available_margin=5000.0,
            leverage=2.0,
            max_leverage=5.0,
            contract_type="INVERSE",
        )
        self.assertTrue(res_inv.valid)
        self.assertGreaterEqual(res_inv.quantity, 1.0)
        self.assertEqual(res_inv.quantity % 1.0, 0.0)
        self.assertLessEqual(res_inv.max_loss_net, res_inv.risk_budget + 1e-6)

        # Excessive leverage (> max_leverage) must be rejected
        res_bad_lev = compute_position_sizing(
            "LONG",
            entry=60000.0,
            stop_loss=59000.0,
            take_profit=62500.0,
            symbol="BTCUSDT",
            equity=10000.0,
            risk_fraction=0.01,
            leverage=10.0,
            max_leverage=5.0,
        )
        self.assertFalse(res_bad_lev.valid)
        self.assertIn("MAX_LEVERAGE_EXCEEDED", res_bad_lev.veto_codes)

        # Insufficient available margin must be rejected
        res_no_margin = compute_position_sizing(
            "LONG",
            entry=60000.0,
            stop_loss=59000.0,
            take_profit=62500.0,
            symbol="BTCUSDT",
            equity=10000.0,
            risk_fraction=0.01,
            available_margin=0.0,
        )
        self.assertFalse(res_no_margin.valid)
        self.assertIn("INSUFFICIENT_AVAILABLE_MARGIN", res_no_margin.veto_codes)

    def test_03_dynamic_stop_loss_and_ai_widening_prevention(self):
        # AI attempting to widen stop-loss (moving SL lower on a LONG) must be rejected
        widen_attempt = evaluate_dynamic_protective_levels(
            direction="LONG",
            entry_price=100.0,
            current_stop_loss=95.0,
            current_take_profit=112.0,
            current_market_price=98.0,
            proposed_stop_loss=92.0,
            proposed_by_ai=True,
        )
        self.assertFalse(widen_attempt["valid"])
        self.assertIn("AI_CANNOT_WIDEN_STOP_LOSS", widen_attempt["veto_codes"])
        self.assertEqual(widen_attempt["stop_loss"], 95.0)

        # Deterministic break-even move when market moves +1.2R in favor
        be_move = evaluate_dynamic_protective_levels(
            direction="LONG",
            entry_price=100.0,
            current_stop_loss=95.0,
            current_take_profit=112.0,
            current_market_price=106.5,
            tick_size=0.01,
            enable_break_even=True,
            break_even_trigger_r=1.0,
        )
        self.assertTrue(be_move["valid"])
        self.assertEqual(be_move["action"], "MOVE_TO_BREAK_EVEN")
        self.assertGreater(be_move["stop_loss"], 100.0)
        self.assertTrue(be_move["stop_tightened"])

    def test_04_default_execution_mode_and_credential_separation(self):
        # Verify live trading is strictly disabled by default
        self.assertEqual(DEFAULT_EXECUTION_MODE, "PAPER")
        self.assertFalse(DEFAULT_LIVE_TRADING_ENABLED)

        cfg_default = resolve_execution_mode_config(lambda k: "")
        self.assertEqual(cfg_default["execution_mode"], "PAPER")
        self.assertFalse(cfg_default["live_execution_authorized"])

        # Even if EXECUTION_MODE=LIVE and LIVE_TRADING_ENABLED=true in env, live execution is blocked
        # until explicit runtime_live_confirmed=True is provided!
        env_live = {"EXECUTION_MODE": "LIVE", "LIVE_TRADING_ENABLED": "true"}
        cfg_unconfirmed = resolve_execution_mode_config(lambda k: env_live.get(k, ""), runtime_live_confirmed=False)
        self.assertFalse(cfg_unconfirmed["live_execution_authorized"])
        self.assertEqual(cfg_unconfirmed["authorization_status"], "BLOCKED_AWAITING_EXPLICIT_AUTHORIZATION")

        cfg_confirmed = resolve_execution_mode_config(lambda k: env_live.get(k, ""), runtime_live_confirmed=True)
        self.assertTrue(cfg_confirmed["live_execution_authorized"])

        # Verify TESTNET and LIVE credentials never mix
        env_creds = {
            "BINANCE_TESTNET_API_KEY": "testnet_key_123456",
            "BINANCE_TESTNET_API_SECRET": "testnet_secret_abcdef",
            "BINANCE_API_KEY": "live_key_999999",
            "BINANCE_API_SECRET": "live_secret_xyz999",
        }
        tn_c = get_exchange_credentials("TESTNET", getter=lambda k: env_creds.get(k, ""))
        lv_c = get_exchange_credentials("LIVE", getter=lambda k: env_creds.get(k, ""))
        self.assertEqual(tn_c["api_key"], "testnet_key_123456")
        self.assertEqual(lv_c["api_key"], "live_key_999999")
        self.assertNotIn("testnet_secret_abcdef", redact_secrets("err=testnet_secret_abcdef", extra_secrets=tn_c["extra_secrets"]))


class TestAutonomousLiveEngineIntegration(unittest.TestCase):
    """Master Prompt 2 Section 15: End-to-end integration tests with mocked exchange API for execution, timeouts, partial fills, protection failures, reconciliation, circuit breakers, and kill switch."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_live_engine.db")
        run_migrations(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_sample_signal_and_packet(self, symbol: str = "BTCUSDT", direction: str = "LONG", state: str = "TRIGGERED"):
        signal = {
            "symbol": symbol,
            "final": direction,
            "direction": direction,
            "lifecycle_state": state,
            "signal_id": f"sig_{symbol}_1001",
            "entry": 60000.0,
            "sl": 58800.0,
            "tp": 63000.0,
            "confidence": 82,
        }
        packet = {
            "symbol": symbol,
            "price": 60000.0,
            "order_book": {"spread": 0.02, "imbalance": 0.15},
            "timeframes": {
                "5m": {"price": 60000.0, "ema20": 59900.0, "ema50": 59700.0, "rsi": 60.0, "atr": 250.0, "is_closed": True},
                "15m": {"price": 60000.0, "ema20": 59800.0, "ema50": 59500.0, "rsi": 61.0, "atr": 400.0, "is_closed": True},
                "1h": {"price": 60000.0, "ema20": 59600.0, "ema50": 59100.0, "rsi": 62.0, "atr": 800.0, "is_closed": True},
            },
            "smc_by_tf": {"5m": {}, "15m": {}, "1h": {}},
        }
        return signal, packet

    def test_05_full_testnet_execution_and_duplicate_order_prevention(self):
        placed_orders = []

        def mock_request(method: str, url: str, **kwargs):
            if "/fapi/v1/exchangeInfo" in url:
                return MockHTTPResponse(make_mock_exchange_info())
            if "/fapi/v2/account" in url:
                return MockHTTPResponse(make_mock_futures_account(wallet=10000.0, available=10000.0))
            if "/fapi/v2/positionRisk" in url:
                return MockHTTPResponse([])
            if "/fapi/v1/openOrders" in url:
                return MockHTTPResponse([])
            if method == "POST" and "/fapi/v1/order" in url:
                parsed_q = dict(urllib.parse.parse_qsl( urllib.parse.urlsplit(url).query ))
                placed_orders.append(parsed_q)
                otype = parsed_q.get("type", "MARKET")
                qty = parsed_q.get("quantity", "0.041")
                return MockHTTPResponse({
                    "orderId": 1000 + len(placed_orders),
                    "clientOrderId": parsed_q.get("newClientOrderId", ""),
                    "symbol": parsed_q.get("symbol", "BTCUSDT"),
                    "side": parsed_q.get("side", "BUY"),
                    "type": otype,
                    "status": "FILLED" if otype == "MARKET" else "NEW",
                    "price": parsed_q.get("price", "60000.0"),
                    "stopPrice": parsed_q.get("stopPrice", "0.0"),
                    "origQty": qty,
                    "executedQty": qty if otype == "MARKET" else "0.0",
                    "avgPrice": "60000.0" if otype == "MARKET" else "0.0",
                    "reduceOnly": parsed_q.get("reduceOnly") == "true",
                })
            return MockHTTPResponse({}, 404)

        import urllib.parse
        adapter = BinanceExchangeAdapter(
            execution_mode="TESTNET",
            market_type="FUTURES_USDM",
            api_key="tn_key_123",
            api_secret="tn_sec_456",
            request_fn=mock_request,
        )
        engine = AutonomousExecutionEngine(adapter, db_file=self.db_path)
        sig, pkt = self._make_sample_signal_and_packet()

        res1 = engine.evaluate_and_execute_signal(sig, pkt)
        self.assertTrue(res1["executed"])
        self.assertEqual(res1["status"], LivePositionLifecycleState.PROTECTIVE_ORDERS_CONFIRMED.value)
        self.assertTrue(res1["protection_verified"])
        self.assertEqual(len(placed_orders), 3)  # ENTRY + STOP_LOSS + TAKE_PROFIT

        # Re-submitting the exact same signal must be blocked by idempotent client_order_id check!
        res2 = engine.evaluate_and_execute_signal(sig, pkt)
        self.assertFalse(res2["executed"])
        self.assertEqual(res2["status"], "DUPLICATE_ORDER_PREVENTED")
        self.assertEqual(len(placed_orders), 3)

    def test_06_timeout_recovery_queries_order_before_retry_and_partial_fill_handling(self):
        import urllib.parse
        post_calls = 0

        def mock_request_timeout_then_partial(method: str, url: str, **kwargs):
            nonlocal post_calls
            if "/fapi/v1/exchangeInfo" in url:
                return MockHTTPResponse(make_mock_exchange_info())
            if "/fapi/v2/account" in url:
                return MockHTTPResponse(make_mock_futures_account(wallet=10000.0, available=10000.0))
            if "/fapi/v2/positionRisk" in url:
                return MockHTTPResponse([])
            if "/fapi/v1/openOrders" in url:
                return MockHTTPResponse([])
            if method == "POST" and "/fapi/v1/order" in url:
                post_calls += 1
                parsed_q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                if post_calls == 1:
                    # Simulate HTTP timeout on entry order AFTER exchange actually accepted & partially filled it!
                    raise requests.exceptions.Timeout("Read timed out after 10s")
                # Protective SL/TP order placement
                return MockHTTPResponse({
                    "orderId": 2000 + post_calls,
                    "clientOrderId": parsed_q.get("newClientOrderId", ""),
                    "symbol": "BTCUSDT",
                    "side": parsed_q.get("side", "SELL"),
                    "type": parsed_q.get("type", "STOP_MARKET"),
                    "status": "NEW",
                    "stopPrice": parsed_q.get("stopPrice", "58800.0"),
                    "origQty": parsed_q.get("quantity", "0.020"),
                    "executedQty": "0.0",
                    "reduceOnly": True,
                })
            if method == "GET" and "/fapi/v1/order" in url:
                parsed_q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                # Exchange reports the timed-out entry order was actually PARTIALLY_FILLED (0.020 of 0.039)!
                return MockHTTPResponse({
                    "orderId": 1999,
                    "clientOrderId": parsed_q.get("origClientOrderId", ""),
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "status": "PARTIALLY_FILLED",
                    "origQty": "0.039",
                    "executedQty": "0.020",
                    "avgPrice": "60000.0",
                })
            return MockHTTPResponse({}, 404)

        adapter = BinanceExchangeAdapter(
            execution_mode="TESTNET",
            market_type="FUTURES_USDM",
            api_key="tn_key_123",
            api_secret="tn_sec_456",
            request_fn=mock_request_timeout_then_partial,
        )
        engine = AutonomousExecutionEngine(adapter, db_file=self.db_path)
        sig, pkt = self._make_sample_signal_and_packet()
        sig["signal_id"] = "sig_timeout_partial_01"

        res = engine.evaluate_and_execute_signal(sig, pkt)
        self.assertTrue(res["executed"])
        self.assertEqual(res["status"], LivePositionLifecycleState.ENTRY_PARTIALLY_FILLED.value)
        self.assertTrue(res["protection_verified"])
        # Protective SL order quantity must match the partial fill quantity (0.020), not requested (0.039)!
        self.assertAlmostEqual(res["sl_order"]["requested_qty"], 0.020)

    def test_07_protective_order_failure_triggers_alert_and_circuit_breaker(self):
        import urllib.parse

        def mock_request_sl_fails(method: str, url: str, **kwargs):
            if "/fapi/v1/exchangeInfo" in url:
                return MockHTTPResponse(make_mock_exchange_info())
            if "/fapi/v2/account" in url:
                return MockHTTPResponse(make_mock_futures_account(wallet=10000.0, available=10000.0))
            if "/fapi/v2/positionRisk" in url:
                return MockHTTPResponse([])
            if "/fapi/v1/openOrders" in url:
                return MockHTTPResponse([])
            if method == "POST" and "/fapi/v1/order" in url:
                parsed_q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                otype = parsed_q.get("type", "MARKET")
                if otype == "MARKET":
                    return MockHTTPResponse({
                        "orderId": 3001,
                        "clientOrderId": parsed_q.get("newClientOrderId", ""),
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "type": "MARKET",
                        "status": "FILLED",
                        "origQty": parsed_q.get("quantity", "0.039"),
                        "executedQty": parsed_q.get("quantity", "0.039"),
                        "avgPrice": "60000.0",
                    })
                # Reject STOP_MARKET order
                return MockHTTPResponse({"code": -2021, "msg": "Order would immediately trigger"}, 400)
            return MockHTTPResponse({}, 404)

        adapter = BinanceExchangeAdapter(
            execution_mode="TESTNET",
            market_type="FUTURES_USDM",
            api_key="tn_key_123",
            api_secret="tn_sec_456",
            request_fn=mock_request_sl_fails,
        )
        engine = AutonomousExecutionEngine(
            adapter,
            db_file=self.db_path,
            emergency_close_on_unprotected=False,
            max_protection_retries=2,
        )
        sig, pkt = self._make_sample_signal_and_packet()
        sig["signal_id"] = "sig_sl_fail_01"

        res = engine.evaluate_and_execute_signal(sig, pkt)
        self.assertEqual(res["status"], LivePositionLifecycleState.PROTECTION_MISSING_ALERT.value)
        self.assertFalse(res["protection_verified"])

        # Circuit breaker must now be active and block any subsequent new entry!
        cb_state = get_circuit_breaker_and_kill_switch_state(db_file=self.db_path)
        self.assertTrue(cb_state["circuit_breaker_active"])
        self.assertIn("CB_MISSING_PROTECTIVE_ORDERS", cb_state["active_reasons"])

    def test_08_startup_reconciliation_and_kill_switch_behavior(self):
        import urllib.parse
        canceled_ids = []

        def mock_exchange_with_unprotected_pos_and_orders(method: str, url: str, **kwargs):
            if "/fapi/v1/exchangeInfo" in url:
                return MockHTTPResponse(make_mock_exchange_info())
            if "/fapi/v2/account" in url:
                return MockHTTPResponse(make_mock_futures_account(
                    wallet=10000.0,
                    unrealized=150.0,
                    available=8000.0,
                    used_margin=2000.0,
                ))
            if "/fapi/v2/positionRisk" in url:
                return MockHTTPResponse([
                    {
                        "symbol": "ETHUSDT",
                        "positionAmt": "2.0",
                        "entryPrice": "3000.0",
                        "markPrice": "3075.0",
                        "unRealizedProfit": "150.0",
                        "leverage": "3",
                        "isolated": True,
                    }
                ])
            if "/fapi/v1/openOrders" in url:
                return MockHTTPResponse([
                    # Unfilled entry limit order (reduceOnly=False) -> should be canceled by Kill Switch
                    {
                        "orderId": 5001,
                        "clientOrderId": "sa_ENT_BTCUSDT_unfilled",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "type": "LIMIT",
                        "status": "NEW",
                        "price": "58000.0",
                        "origQty": "0.050",
                        "executedQty": "0.0",
                        "reduceOnly": False,
                    },
                    # Protective SL order on ETHUSDT (reduceOnly=True) -> should be preserved by Kill Switch
                    {
                        "orderId": 5002,
                        "clientOrderId": "sa_SL_ETHUSDT_prot",
                        "symbol": "ETHUSDT",
                        "side": "SELL",
                        "type": "STOP_MARKET",
                        "status": "NEW",
                        "stopPrice": "2900.0",
                        "origQty": "2.0",
                        "executedQty": "0.0",
                        "reduceOnly": True,
                    },
                ])
            if method == "DELETE" and "/fapi/v1/order" in url:
                parsed_q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                coid = parsed_q.get("origClientOrderId", "")
                canceled_ids.append(coid)
                return MockHTTPResponse({
                    "orderId": 5001,
                    "clientOrderId": coid,
                    "symbol": parsed_q.get("symbol", "BTCUSDT"),
                    "status": "CANCELED",
                })
            return MockHTTPResponse({}, 404)

        adapter = BinanceExchangeAdapter(
            execution_mode="TESTNET",
            market_type="FUTURES_USDM",
            api_key="tn_key_123",
            api_secret="tn_sec_456",
            request_fn=mock_exchange_with_unprotected_pos_and_orders,
        )

        # 1. Startup reconciliation detects untracked ETHUSDT exchange position and pauses new entries
        rec = reconcile_live_exchange_state(adapter, db_file=self.db_path)
        self.assertFalse(rec["reconciled"])
        self.assertTrue(rec["new_entries_paused"])
        self.assertTrue(any("UNTRACKED_EXCHANGE_POSITION:ETHUSDT" in d for d in rec["discrepancies"]))

        # Verify ETHUSDT was imported into local live_positions so it can never be duplicated
        loc_eth = list_live_positions(db_file=self.db_path, symbol="ETHUSDT")
        self.assertEqual(len(loc_eth), 1)
        self.assertAlmostEqual(loc_eth[0]["open_qty"], 2.0)

        # 2. Activate Kill Switch: cancels unfilled BTCUSDT entry order while preserving ETHUSDT protective SL!
        ks = activate_kill_switch(
            "Test emergency stop",
            db_file=self.db_path,
            adapter=adapter,
            cancel_unfilled_entries=True,
            preserve_protective_orders=True,
        )
        self.assertTrue(ks["kill_switch_active"])
        self.assertEqual(ks["verification_status"], "VERIFIED_WITH_EXCHANGE")
        self.assertIn("sa_ENT_BTCUSDT_unfilled", ks["canceled_entry_orders"])
        self.assertIn("sa_SL_ETHUSDT_prot", ks["preserved_protective_orders"])
        self.assertEqual(canceled_ids, ["sa_ENT_BTCUSDT_unfilled"])

        # 3. Verify emergency_close_all_positions refuses to run without explicit_user_confirmation=True
        emg_refused = emergency_close_all_positions(adapter, db_file=self.db_path, explicit_user_confirmation=False)
        self.assertFalse(emg_refused["executed"])
        self.assertEqual(emg_refused["status"], "CONFIRMATION_REQUIRED")

    def test_09_daily_loss_circuit_breaker_persistence_across_restart_and_live_readiness_gate(self):
        # Record a daily realized + unrealized loss breaching 3% of $10,000 ($300)
        cb1 = evaluate_and_persist_circuit_breakers(
            db_file=self.db_path,
            equity_reference=10000.0,
            max_daily_loss_fraction=0.03,
            daily_realized_loss_override=220.0,
            daily_unrealized_loss_override=110.0,
            today_utc_str="2026-10-20",
        )
        self.assertTrue(cb1["circuit_breaker_active"])
        self.assertIn("CB_MAX_DAILY_LOSS_BREACHED", cb1["active_reasons"])
        self.assertAlmostEqual(cb1["total_daily_loss_usd"], 330.0)

        # Simulate restart on the same UTC day: daily loss limit remains breached!
        cb_restart = get_circuit_breaker_and_kill_switch_state(db_file=self.db_path, today_utc_str="2026-10-20")
        self.assertTrue(cb_restart["circuit_breaker_active"])
        self.assertAlmostEqual(cb_restart["daily_realized_loss_usd"], 220.0)

        # Evaluate 13-point Live Readiness Gate without explicit user confirmation -> must block LIVE execution
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        snap = parse_binance_futures_account(
            make_mock_futures_account(wallet=10000.0, available=10000.0),
            instrument_rules=parse_binance_exchange_info(make_mock_exchange_info()),
            api_restrictions={"enableWithdrawals": False},
            sync_ms=now_ms,
            now_ms=now_ms,
        )
        # Clear circuit breaker first to test readiness gate
        deactivate_kill_switch(db_file=self.db_path)
        evaluate_and_persist_circuit_breakers(
            db_file=self.db_path,
            account_snapshot=snap,
            daily_realized_loss_override=0.0,
            daily_unrealized_loss_override=0.0,
            today_utc_str="2026-10-21",
        )
        gate_no_auth = evaluate_live_readiness_gate(
            db_file=self.db_path,
            account_snapshot=snap,
            execution_mode="LIVE",
            live_trading_enabled_config=True,
            runtime_user_confirmed=False,
        )
        self.assertTrue(gate_no_auth["technical_readiness_passed"])
        self.assertFalse(gate_no_auth["can_execute_live"])
        self.assertIn("13_EXPLICIT_USER_LIVE_AUTHORIZATION", gate_no_auth["failed_checks"])

        gate_with_auth = evaluate_live_readiness_gate(
            db_file=self.db_path,
            account_snapshot=snap,
            execution_mode="LIVE",
            live_trading_enabled_config=True,
            runtime_user_confirmed=True,
        )
        self.assertTrue(gate_with_auth["can_execute_live"])


if __name__ == "__main__":
    unittest.main()

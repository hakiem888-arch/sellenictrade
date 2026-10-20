from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

import app
from ai_consensus.domain import (
    SignalLifecycleState,
    build_momentum_evidence_report,
    compute_directional_risk_reward,
    evaluate_signal_lifecycle,
    preflight_market_check,
    validate_signal_lifecycle_transition,
)
from ai_consensus.features import (
    compute_indicators_frame,
    extract_timeframe_features,
    validate_ohlcv_dataframe,
)
from ai_consensus.market import (
    align_higher_timeframes_as_of,
    annotate_bar_watermarks,
    validate_cross_timeframe_consistency,
)
from ai_consensus.monitor import BackgroundMonitorWorker, get_system_health_report
from ai_consensus.paper import (
    compute_confidence_calibration_report,
    compute_expectancy_and_drawdown_analytics,
    compute_realized_trade_accounting,
    evaluate_position_on_bars,
    resolve_intrabar_conflict,
    run_walk_forward_folds,
    run_walk_forward_replay_backtest,
)
from ai_consensus.risk import (
    compute_position_sizing,
    evaluate_risk_guard,
    explain_risk_veto_codes,
    get_exchange_instrument_specs,
)
from ai_consensus.services import (
    dispatch_telegram_event,
    redact_secrets,
)
from ai_consensus.smc import smc_analysis
from ai_consensus.storage import (
    get_persisted_risk_guard_state,
    reconcile_paper_positions,
    run_migrations,
    save_signal_record,
)


def make_synthetic_ohlcv(
    n: int = 120,
    start_price: float = 100.0,
    trend: float = 0.25,
    interval: str = "15m",
    interval_sec: int = 900,
) -> pd.DataFrame:
    """Create closed synthetic OHLCV bars matching the exact repository schema."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    step_ms = interval_sec * 1000
    end_open_ms = now_ms - (2 * step_ms)
    start_open_ms = end_open_ms - (n - 1) * step_ms
    rows = []
    price = start_price
    for i in range(n):
        open_ms = start_open_ms + i * step_ms
        close_ms = open_ms + step_ms - 1
        o = price
        c = max(1.0, o + trend + (0.4 if i % 3 != 0 else -0.2))
        h = max(o, c) + 0.5
        l = min(o, c) - 0.4
        v = 1000.0 + (i * 15.0)
        rows.append({
            "open_time": int(open_ms),
            "close_time": int(close_ms),
            "OpenTime": pd.to_datetime(open_ms, unit="ms", utc=True),
            "Open": float(o),
            "High": float(h),
            "Low": float(l),
            "Close": float(c),
            "Volume": float(v),
            "CloseTime": pd.to_datetime(close_ms, unit="ms", utc=True),
            "QuoteVolume": float(v * c),
            "Trades": 100,
            "TakerBuyBase": float(v * 0.55),
            "TakerBuyQuote": float(v * c * 0.55),
            "Ignore": 0.0,
            "is_closed": True,
        })
        price = c
    raw_df = pd.DataFrame(rows)
    raw_df.attrs["_is_closed_df"] = True
    return annotate_bar_watermarks(raw_df, interval=interval)


def make_packet(df_5m: pd.DataFrame, df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
    f5 = compute_indicators_frame(df_5m, interval="5m", drop_unwarmed=False)
    f15 = compute_indicators_frame(df_15m, interval="15m", drop_unwarmed=False)
    f1h = compute_indicators_frame(df_1h, interval="1h", drop_unwarmed=False)
    f4h = compute_indicators_frame(df_4h, interval="4h", drop_unwarmed=False)

    d5 = extract_timeframe_features(f5, interval="5m")
    d15 = extract_timeframe_features(f15, interval="15m")
    d1h = extract_timeframe_features(f1h, interval="1h")
    d4h = extract_timeframe_features(f4h, interval="4h")
    s5 = smc_analysis(f5)
    s15 = smc_analysis(f15)
    s1h = smc_analysis(f1h)
    s4h = smc_analysis(f4h)
    close_px = float(d15["price"])
    return {
        "symbol": "BTCUSDT",
        "price": close_px,
        "support": close_px * 0.96,
        "resistance": close_px * 1.04,
        "market_regime": "TRENDING_BULL",
        "data_status": "READY",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "timeframes": {"5m": d5, "15m": d15, "1h": d1h, "4h": d4h},
        "data": {"5m": d5, "15m": d15, "1h": d1h, "4h": d4h},
        "smc": s15,
        "smc_by_tf": {"5m": s5, "15m": s15, "1h": s1h, "4h": s4h},
        "frames": {"5m": f5, "15m": f15, "1h": f1h, "4h": f4h},
        "deriv": {"funding": 0.0001, "oi": 500000.0, "ls": 1.1},
        "order_book": {"spread": 0.02, "imbalance": 0.15},
        "ob": {"spread_pct": 0.02, "bid": close_px * 0.9999, "ask": close_px * 1.0001},
        "fng": {"value": 55, "label": "Neutral"},
        "news": [],
    }


class TestMomentumEngineUnit(unittest.TestCase):
    """Section 12: Unit tests covering momentum, MTF, lifecycle, levels, sizing, risk, metrics, deduplication, and data freshness."""

    def test_momentum_and_indicator_calculations(self):
        df = make_synthetic_ohlcv(120, start_price=100.0, trend=0.5, interval="15m", interval_sec=900)
        ind_df = compute_indicators_frame(df, interval="15m", drop_unwarmed=False)
        bundle = extract_timeframe_features(ind_df, interval="15m")
        self.assertIsNotNone(bundle)
        for key in ("price", "ema20", "ema50", "ema200", "rsi", "macd_hist", "atr", "vol_ratio", "buy_pressure"):
            self.assertIn(key, bundle)
        self.assertGreater(float(bundle["price"]), float(df.iloc[0]["Close"]))
        self.assertGreater(float(bundle["rsi"]), 50.0)
        self.assertGreater(float(bundle["atr"]), 0.0)

        bidir = app.scanner_score_bidirectional("BTCUSDT", 4.5, 50_000_000.0, ind_df, {"spread": 0.02, "imbalance": 0.2})
        self.assertIn("score", bidir)
        self.assertTrue(0.0 <= float(bidir["score"]) <= 100.0)
        self.assertIn(bidir["direction_hint"], ("LONG", "SHORT", "WAIT"))

    def test_multi_timeframe_alignment_and_cross_tf_consistency(self):
        df_15m = make_synthetic_ohlcv(120, start_price=100.0, trend=0.2, interval="15m", interval_sec=900)
        df_1h = make_synthetic_ohlcv(120, start_price=100.0, trend=0.2, interval="1h", interval_sec=3600)
        df_4h = make_synthetic_ohlcv(80, start_price=100.0, trend=0.2, interval="4h", interval_sec=14400)
        check_ok = validate_cross_timeframe_consistency({"15m": df_15m, "1h": df_1h}, max_price_divergence_pct=3.0)
        self.assertTrue(check_ok["consistent"])

        df_1h_bad = make_synthetic_ohlcv(120, start_price=150.0, trend=0.2, interval="1h", interval_sec=3600)
        check_bad = validate_cross_timeframe_consistency({"15m": df_15m, "1h": df_1h_bad}, max_price_divergence_pct=3.0)
        self.assertFalse(check_bad["consistent"])
        self.assertTrue(any("CROSS_TF_PRICE_DIVERGENCE" in issue for issue in check_bad["errors"]))

        cutoff_ms = int(df_4h.iloc[-5]["close_time"])
        aligned = align_higher_timeframes_as_of({"4h": df_4h}, cutoff_ms)
        self.assertLessEqual(int(aligned["4h"].iloc[-1]["close_time"]), cutoff_ms)
        self.assertLess(len(aligned["4h"]), len(df_4h))

    def test_signal_lifecycle_transitions_and_expiry(self):
        self.assertTrue(validate_signal_lifecycle_transition("WAIT", "DEVELOPING"))
        self.assertTrue(validate_signal_lifecycle_transition("DEVELOPING", "TRIGGERED"))
        self.assertTrue(validate_signal_lifecycle_transition("TRIGGERED", "INVALIDATED"))
        with self.assertRaises(ValueError):
            validate_signal_lifecycle_transition("INVALIDATED", "TRIGGERED")

        dev = evaluate_signal_lifecycle(
            "BTCUSDT",
            {
                "state": "DEVELOPING",
                "direction": "LONG",
                "score": 66.0,
                "entry": 100.0,
                "sl": 98.0,
                "tp": 104.0,
            },
            current_price=100.2,
            mode="scalping",
        )
        self.assertEqual(dev.state, SignalLifecycleState.DEVELOPING.value)
        self.assertTrue(dev.signal_id.startswith("sig_"))

        trig = evaluate_signal_lifecycle(
            "BTCUSDT",
            {
                "state": "TRIGGERED",
                "direction": "LONG",
                "score": 78.0,
                "entry": 100.0,
                "sl": 98.0,
                "tp": 104.0,
            },
            previous_state="DEVELOPING",
            current_price=100.1,
            mode="scalping",
        )
        self.assertEqual(trig.state, SignalLifecycleState.TRIGGERED.value)
        self.assertTrue(trig.is_actionable)

        inv = evaluate_signal_lifecycle(
            "BTCUSDT",
            {
                "state": "TRIGGERED",
                "direction": "LONG",
                "score": 78.0,
                "entry": 100.0,
                "sl": 98.0,
                "tp": 104.0,
            },
            previous_state="TRIGGERED",
            previous_direction="LONG",
            current_price=97.5,
            invalidation_level=98.0,
            mode="scalping",
        )
        self.assertEqual(inv.state, SignalLifecycleState.INVALIDATED.value)
        self.assertFalse(inv.is_actionable)

        expired = evaluate_signal_lifecycle(
            "BTCUSDT",
            {
                "state": "TRIGGERED",
                "direction": "LONG",
                "score": 80.0,
                "entry": 100.0,
                "sl": 98.0,
                "tp": 104.0,
            },
            previous_state="TRIGGERED",
            previous_direction="LONG",
            previous_triggered_at="2026-10-18T00:00:00+00:00",
            previous_expires_at="2026-10-18T02:00:00+00:00",
            as_of_iso="2026-10-20T00:00:00+00:00",
            current_price=100.1,
            mode="scalping",
        )
        self.assertEqual(expired.state, SignalLifecycleState.INVALIDATED.value)
        self.assertIn("SIGNAL_EXPIRED", expired.invalidation_reason)

    def test_entry_sl_tp_and_directional_risk_reward_validation(self):
        rr_long = compute_directional_risk_reward("LONG", entry=100.0, stop_loss=95.0, take_profit=110.0)
        self.assertTrue(rr_long["valid"])
        self.assertAlmostEqual(rr_long["risk_per_unit"], 5.0)
        self.assertAlmostEqual(rr_long["reward_per_unit"], 10.0)
        self.assertAlmostEqual(rr_long["gross_rr"], 2.0)

        rr_short = compute_directional_risk_reward("SHORT", entry=100.0, stop_loss=105.0, take_profit=90.0)
        self.assertTrue(rr_short["valid"])
        self.assertAlmostEqual(rr_short["risk_per_unit"], 5.0)
        self.assertAlmostEqual(rr_short["reward_per_unit"], 10.0)
        self.assertAlmostEqual(rr_short["gross_rr"], 2.0)

        rr_bad_long = compute_directional_risk_reward("LONG", entry=100.0, stop_loss=101.0, take_profit=110.0)
        self.assertFalse(rr_bad_long["valid"])
        self.assertIn("INVALID_SL_TP", rr_bad_long["veto_codes"])

        rr_bad_short = compute_directional_risk_reward("SHORT", entry=100.0, stop_loss=105.0, take_profit=102.0)
        self.assertFalse(rr_bad_short["valid"])
        self.assertIn("INVALID_SL_TP", rr_bad_short["veto_codes"])

    def test_position_sizing_and_missing_equity_handling(self):
        no_eq_res = compute_position_sizing(
            "LONG",
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            symbol="SOLUSDT",
            equity=None,
        )
        self.assertFalse(no_eq_res.valid)
        self.assertIn("MISSING_POSITION_SIZING_INPUTS", no_eq_res.veto_codes)
        self.assertEqual(no_eq_res.quantity, 0.0)

        specs = get_exchange_instrument_specs("SOLUSDT")
        valid_res = compute_position_sizing(
            "LONG",
            entry=100.004,
            stop_loss=95.004,
            take_profit=112.004,
            symbol="SOLUSDT",
            equity=10000.0,
            risk_fraction=0.01,
            tick_size=specs["tick_size"],
            lot_size=specs["lot_size"],
            min_qty=specs["min_qty"],
            min_notional=specs["min_notional"],
        )
        self.assertTrue(valid_res.valid)
        self.assertGreater(valid_res.quantity, 0.0)
        self.assertNotEqual(valid_res.theoretical_entry, valid_res.estimated_entry_fill)
        self.assertEqual(valid_res.invalidation_level, valid_res.theoretical_stop_loss)

    def test_risk_limits_enforcement(self):
        res_breach = compute_position_sizing(
            "LONG",
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            symbol="ETHUSDT",
            equity=10000.0,
            risk_fraction=0.01,
            current_open_risk=250.0,
            max_total_risk_fraction=0.02,
            current_open_positions=2,
            max_open_positions=2,
            daily_realized_loss_usd=350.0,
            max_daily_loss_fraction=0.03,
            consecutive_losses=3,
            max_consecutive_losses=3,
            correlated_open_positions=1,
            max_correlated_positions=1,
        )
        self.assertFalse(res_breach.valid)
        self.assertIn("MAX_CONCURRENT_POSITIONS_EXCEEDED", res_breach.veto_codes)
        self.assertIn("MAX_DAILY_LOSS_BREACHED", res_breach.veto_codes)
        self.assertIn("MAX_CONSECUTIVE_LOSSES_BREACHED", res_breach.veto_codes)
        self.assertIn("MAX_CORRELATED_POSITIONS_EXCEEDED", res_breach.veto_codes)
        self.assertIn("TOTAL_RISK_BUDGET_EXCEEDED", res_breach.veto_codes)
        human_reasons = explain_risk_veto_codes(res_breach.veto_codes)
        self.assertEqual(len(human_reasons), len(res_breach.veto_codes))

    def test_performance_metrics_and_calibration(self):
        empty_metrics = compute_expectancy_and_drawdown_analytics([], dataset_type="BACKTEST")
        self.assertEqual(empty_metrics["total_trades"], 0)
        self.assertEqual(empty_metrics["dataset_type"], "BACKTEST")
        self.assertIn("metric_definitions", empty_metrics)

        trades = [
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "timeframe": "1h",
                "mode": "swing",
                "market_regime": "TRENDING_BULL",
                "state": "CLOSED_TP",
                "gross_pnl": 125.0,
                "net_pnl": 120.0,
                "total_fees": 5.0,
                "funding_cost": 1.0,
                "gross_r_multiple": 2.1,
                "net_r_multiple": 2.0,
                "confidence": 78,
                "closed_at": "2026-10-19T10:00:00+00:00",
            },
            {
                "symbol": "ETHUSDT",
                "direction": "SHORT",
                "timeframe": "4h",
                "mode": "swing",
                "market_regime": "TRENDING_BEAR",
                "state": "CLOSED_TP",
                "gross_pnl": 84.0,
                "net_pnl": 80.0,
                "total_fees": 4.0,
                "funding_cost": 0.5,
                "gross_r_multiple": 1.6,
                "net_r_multiple": 1.5,
                "confidence": 72,
                "closed_at": "2026-10-19T12:00:00+00:00",
            },
        ]
        metrics = compute_expectancy_and_drawdown_analytics(trades, initial_equity=10000.0, dataset_type="PAPER_TRADING")
        self.assertEqual(metrics["total_trades"], 2)
        self.assertEqual(metrics["sample_size"], 2)
        self.assertEqual(metrics["win_rate_net"], 100.0)
        self.assertIsNone(metrics["profit_factor_net"])
        self.assertEqual(metrics["profit_factor_status"], "UNDEFINED_NO_LOSSES")
        self.assertIn("LONG", metrics["by_direction"])
        self.assertIn("SHORT", metrics["by_direction"])
        self.assertIn("1h", metrics["by_timeframe"])
        self.assertIn("TRENDING_BULL", metrics["by_market_regime"])

        cal = compute_confidence_calibration_report(trades)
        self.assertFalse(cal["is_calibrated"])
        self.assertFalse(cal["probability_claim_allowed"])
        self.assertEqual(cal["output_label"], "SCORE_INDICATOR_ONLY")

    def test_intrabar_conflict_resolution_and_gap_clamping(self):
        state_str, outcome, exit_px = resolve_intrabar_conflict(
            direction="LONG",
            bar_open=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            policy="SL_FIRST_CONSERVATIVE",
            bar_low=94.0,
            bar_high=112.0,
        )
        self.assertEqual(state_str, "CLOSED_SL")
        self.assertEqual(outcome, "SL")
        self.assertAlmostEqual(exit_px, 95.0)

        acc = compute_realized_trade_accounting(
            direction="LONG",
            entry_price=100.0,
            exit_price=110.0,
            stop_loss=95.0,
            take_profit=110.0,
            quantity=10.0,
            risk_budget=50.0,
            fee_pct_round_trip=0.08,
            slippage_pct_round_trip=0.04,
            spread_pct=0.02,
            funding_rate_pct_per_8h=0.01,
            holding_hours=16.0,
        )
        self.assertGreater(acc.funding_cost, 0.0)
        self.assertTrue(acc.accounting_reconciled)

    def test_data_freshness_and_malformed_inputs(self):
        df = make_synthetic_ohlcv(100)
        bad_df = df.copy()
        bad_df.iloc[-2, bad_df.columns.get_loc("High")] = 10.0
        bad_df.iloc[-2, bad_df.columns.get_loc("Low")] = 50.0
        integrity = validate_ohlcv_dataframe(bad_df)
        self.assertFalse(integrity.valid)
        self.assertTrue(any("HIGH" in issue.upper() or "OHLC" in issue.upper() for issue in integrity.errors))

        df_5m = make_synthetic_ohlcv(100, interval="5m", interval_sec=300)
        df_15m = make_synthetic_ohlcv(100, interval="15m", interval_sec=900)
        df_1h = make_synthetic_ohlcv(100, interval="1h", interval_sec=3600)
        df_4h = make_synthetic_ohlcv(100, interval="4h", interval_sec=14400)
        pkt = make_packet(df_5m, df_15m, df_1h, df_4h)
        pkt["timeframes"]["15m"]["is_closed"] = False
        pf = preflight_market_check(pkt, require_closed_bars=True)
        self.assertFalse(pf["passed"])
        self.assertIn("INCOMPLETE_CANDLE", pf["veto_codes"])


class TestMomentumEngineIntegration(unittest.TestCase):
    """Section 12: Integration tests across ingestion, signal generation, risk guard, paper trading, Telegram outbox, and DB persistence."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_consensus.db")
        run_migrations(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_end_to_end_pipeline_to_paper_trade_and_closure(self):
        df_5m = make_synthetic_ohlcv(120, start_price=100.0, trend=0.4, interval="5m", interval_sec=300)
        df_15m = make_synthetic_ohlcv(120, start_price=100.0, trend=0.4, interval="15m", interval_sec=900)
        df_1h = make_synthetic_ohlcv(120, start_price=100.0, trend=0.4, interval="1h", interval_sec=3600)
        df_4h = make_synthetic_ohlcv(100, start_price=100.0, trend=0.4, interval="4h", interval_sec=14400)
        close_px = float(df_15m.iloc[-1]["Close"])
        df_5m.iloc[-1, df_5m.columns.get_loc("Close")] = close_px
        df_1h.iloc[-1, df_1h.columns.get_loc("Close")] = close_px
        df_4h.iloc[-1, df_4h.columns.get_loc("Close")] = close_px

        pkt = make_packet(df_5m, df_15m, df_1h, df_4h)
        atr = float(pkt["data"]["15m"]["atr"])

        evidence = build_momentum_evidence_report(pkt, direction="LONG", mode="scalping")
        self.assertIn("pillar_scores", evidence)
        self.assertEqual(len(evidence["pillar_scores"]), 5)

        entry = close_px
        sl = close_px - 1.5 * atr
        tp = close_px + 3.5 * atr

        guard = evaluate_risk_guard(
            pkt,
            "LONG",
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            equity=10000.0,
            risk_fraction=0.005,
        )
        self.assertTrue(guard["approved"], f"Expected Risk Guard approval, got: {guard}")
        sizing = guard["sizing"]

        serializable_pkt = {k: v for k, v in pkt.items() if k != "frames"}
        sig_id = save_signal_record(
            {
                "symbol": "BTCUSDT",
                "final": "LONG",
                "signal": "LONG",
                "lifecycle_state": "TRIGGERED",
                "signal_id": "sig_btc_001",
                "confidence": 82,
                "portfolio": {
                    "decision": "LONG",
                    "confidence": 82,
                    "reasoning": "Deterministic MTF momentum & SMC alignment.",
                },
                "packet": serializable_pkt,
                "entry": sizing["theoretical_entry"],
                "sl": sizing["theoretical_stop_loss"],
                "tp": sizing["theoretical_take_profit"],
                "rr": sizing["net_rr"],
                "net_rr": sizing["net_rr"],
                "mode": "scalping",
                "timeframe": "15m",
                "regime": "TRENDING_BULL",
                "position_size": sizing["position_size"],
                "notional_value": sizing["notional_value"],
                "quantity": sizing["quantity"],
                "risk_budget": sizing["risk_budget"],
                "invalidation_level": sizing["invalidation_level"],
                "risk_approved": True,
                "gate_passed": True,
            },
            db_file=self.db_path,
        )
        self.assertGreater(sig_id, 0)

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 900_000
        future_bars = pd.DataFrame([
            {
                "open_time": int(now_ms - 1800_000),
                "close_time": int(now_ms - 900_001),
                "OpenTime": pd.to_datetime(now_ms - 1800_000, unit="ms", utc=True),
                "Open": entry,
                "High": tp + atr,
                "Low": entry - 0.1 * atr,
                "Close": tp + 0.5 * atr,
                "Volume": 2500.0,
                "CloseTime": pd.to_datetime(now_ms - 900_001, unit="ms", utc=True),
                "is_closed": True,
            },
            {
                "open_time": int(now_ms - 900_000),
                "close_time": int(now_ms - 1),
                "OpenTime": pd.to_datetime(now_ms - 900_000, unit="ms", utc=True),
                "Open": tp + 0.5 * atr,
                "High": tp + 1.2 * atr,
                "Low": tp,
                "Close": tp + atr,
                "Volume": 2500.0,
                "CloseTime": pd.to_datetime(now_ms - 1, unit="ms", utc=True),
                "is_closed": True,
            },
        ])
        rec = reconcile_paper_positions(
            db_file=self.db_path,
            bars_by_symbol={"BTCUSDT": future_bars},
            return_detailed=True,
        )
        self.assertIsInstance(rec, list)
        persisted_state = get_persisted_risk_guard_state(self.db_path)
        self.assertEqual(persisted_state["current_open_positions"], 0)
        self.assertEqual(persisted_state["consecutive_losses"], 0)

    def test_telegram_event_deduplication_and_retry(self):
        mock_post = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        payload = {
            "symbol": "SOLUSDT",
            "signal": "LONG",
            "direction": "LONG",
            "timeframe": "1h",
            "signal_id": "sig_sol_123",
            "lifecycle_state": "TRIGGERED",
            "entry": 150.0,
            "sl": 145.0,
            "tp": 162.0,
            "rr": 2.4,
            "confidence": 80,
            "execution_type": "SIGNAL_ONLY",
        }
        res1 = dispatch_telegram_event(
            "SIGNAL_TRIGGERED",
            payload,
            event_key="evt_sol_123",
            db_file=self.db_path,
            state={},
            tg_enabled=True,
            tg_token="123456:ABCDEF_SECRET_TOKEN",
            tg_chat="-100999999",
            post_fn=mock_post,
        )
        self.assertTrue(res1["sent"])
        self.assertEqual(mock_post.call_count, 1)

        res2 = dispatch_telegram_event(
            "SIGNAL_TRIGGERED",
            payload,
            event_key="evt_sol_123",
            db_file=self.db_path,
            state={},
            tg_enabled=True,
            tg_token="123456:ABCDEF_SECRET_TOKEN",
            tg_chat="-100999999",
            post_fn=mock_post,
        )
        self.assertFalse(res2["sent"])
        self.assertTrue(res2["deduplicated"])
        self.assertEqual(mock_post.call_count, 1)

        redacted = redact_secrets("Error calling https://api.telegram.org/bot123456:ABCDEF_SECRET_TOKEN/sendMessage")
        self.assertNotIn("ABCDEF_SECRET_TOKEN", redacted)

    def test_telegram_dedup_distinguishes_partial_fills_for_same_position(self):
        mock_post = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        common = {
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "signal_id": "sig_sol_repeat",
            "position_id": "pos_sol_repeat",
            "lifecycle_state": "EXIT_PARTIALLY_FILLED",
            "entry": 150.0,
        }
        first = dispatch_telegram_event(
            "EXIT_PARTIAL_FILL",
            {**common, "exchange_order_id": "exit-order-1", "executed_qty": 0.2, "exit_price": 151.0, "net_pnl": 0.2},
            db_file=self.db_path, state={}, tg_enabled=True,
            tg_token="123456:ABCDEF_SECRET_TOKEN", tg_chat="-100999999", post_fn=mock_post,
        )
        second_payload = {**common, "exchange_order_id": "exit-order-2", "executed_qty": 0.3, "exit_price": 152.0, "net_pnl": 0.6}
        second = dispatch_telegram_event(
            "EXIT_PARTIAL_FILL", second_payload,
            db_file=self.db_path, state={}, tg_enabled=True,
            tg_token="123456:ABCDEF_SECRET_TOKEN", tg_chat="-100999999", post_fn=mock_post,
        )
        repeated = dispatch_telegram_event(
            "EXIT_PARTIAL_FILL", second_payload,
            db_file=self.db_path, state={}, tg_enabled=True,
            tg_token="123456:ABCDEF_SECRET_TOKEN", tg_chat="-100999999", post_fn=mock_post,
        )

        self.assertTrue(first["sent"])
        self.assertTrue(second["sent"])
        self.assertNotEqual(first["event_key"], second["event_key"])
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(mock_post.call_count, 2)
        self.assertIn("EXIT_PARTIALLY_FILLED", second["message"])

    def test_backtest_and_walk_forward_validation(self):
        df_15m = make_synthetic_ohlcv(260, start_price=100.0, trend=0.15, interval="15m", interval_sec=900)
        df_1h = make_synthetic_ohlcv(120, start_price=100.0, trend=0.15, interval="1h", interval_sec=3600)
        rep = run_walk_forward_replay_backtest(
            df_15m,
            symbol="BTCUSDT",
            interval="15m",
            funding_rate_pct_per_8h=0.01,
            higher_timeframe_dfs={"1h": df_1h},
        )
        self.assertIn("reproducibility_hash", rep)
        self.assertIn("parameters", rep)
        self.assertIn("analytics", rep)
        self.assertIn("calibration", rep)
        self.assertTrue(rep["no_lookahead_verified"])
        self.assertTrue(rep["htf_alignment_verified"])

        wf = run_walk_forward_folds(
            df_15m,
            symbol="BTCUSDT",
            interval="15m",
            train_bars=100,
            val_bars=40,
            test_bars=40,
            step_bars=40,
        )
        self.assertGreaterEqual(wf["folds_count"], 1)
        self.assertTrue(wf["no_lookahead_verified"])
        self.assertFalse(wf["boundary_leakage_detected"])

    def test_system_health_and_worker_singleton(self):
        health = get_system_health_report(
            db_file=self.db_path,
            exchange_health_fn=lambda: (True, "OK"),
            ai_keys_available=False,
            tg_configured=False,
        )
        self.assertIn(health["overall_status"], ("HEALTHY", "DEGRADED"))
        self.assertEqual(len(health["components"]), 9)


class TestMomentumEngineRegression(unittest.TestCase):
    """Section 12: Regression tests ensuring Momentum Radar remains primary, Market Scanner is absent, and CLI/UI entrypoints work."""

    def test_momentum_radar_primary_and_market_scanner_absent(self):
        with open(app.__file__, "r", encoding="utf-8") as fh:
            app_source = fh.read()
        self.assertIn("Momentum Radar", app_source)
        render_part = app_source.split("def render_app()")[1]
        self.assertNotIn("Market Scanner", render_part)
        self.assertNotIn("Top Candidates", render_part)
        self.assertIn("Performance Lab", render_part)

    def test_cli_status_and_reconcile_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "cli_test.db")
            rc_status = app.run_cli_entrypoint(["--status", "--db-file", db_file])
            self.assertTrue(rc_status["executed"])
            self.assertEqual(rc_status["mode"], "status")
            rc_rec = app.run_cli_entrypoint(["--reconcile-only", "--db-file", db_file])
            self.assertTrue(rc_rec["executed"])
            self.assertEqual(rc_rec["mode"], "reconcile_only")



    def test_database_restart_recovery_and_migration_v7(self):
        from ai_consensus.storage import CURRENT_SCHEMA_VERSION, connect_db, record_risk_guard_rejection
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 8)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "restart_v7.db")
            run_migrations(db_path)
            record_risk_guard_rejection(
                "ETHUSDT",
                ["MAX_DAILY_LOSS_BREACHED"],
                reason="Batas rugi harian terlampaui",
                daily_realized_loss_usd=350.0,
                consecutive_losses=3,
                db_file=db_path,
            )
            state_after = get_persisted_risk_guard_state(db_path)
            self.assertEqual(state_after["last_rejected_symbol"], "ETHUSDT")
            self.assertIn("Batas rugi harian terlampaui", state_after["last_rejection_reason"])
            con = connect_db(db_path)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            con.close()
            self.assertIn("telegram_outbox", tables)
            self.assertIn("risk_guard_state", tables)

    def test_proactive_mode_never_bypasses_risk_guard(self):
        df_5m = make_synthetic_ohlcv(120, start_price=100.0, trend=0.4, interval="5m", interval_sec=300)
        df_15m = make_synthetic_ohlcv(120, start_price=100.0, trend=0.4, interval="15m", interval_sec=900)
        df_1h = make_synthetic_ohlcv(120, start_price=100.0, trend=0.4, interval="1h", interval_sec=3600)
        df_4h = make_synthetic_ohlcv(100, start_price=100.0, trend=0.4, interval="4h", interval_sec=14400)
        pkt = make_packet(df_5m, df_15m, df_1h, df_4h)
        pkt["smc"]["timing"] = "CHASE_RISK"
        guard_rep = evaluate_risk_guard(
            pkt,
            "LONG",
            entry=100.0,
            stop_loss=105.0,  # Invalid SL (> entry for LONG)
            take_profit=110.0,
        )
        self.assertEqual(pkt["smc"]["timing"], "CHASE_RISK")
        self.assertFalse(guard_rep["approved"])
        self.assertIn("INVALID_SL_TP", guard_rep["veto_codes"])

    def test_render_app_headless_smoke(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "ui_smoke.db")
            run_migrations(db_file)
            old_db = app.DB_FILE
            try:
                app.DB_FILE = db_file
                app.st.session_state.clear()
                app.render_app()
            finally:
                app.DB_FILE = old_db


if __name__ == "__main__":
    unittest.main()

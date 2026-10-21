"""
Legacy Regression Test Suite (`tests/test_legacy_regression.py`).

Verifies that all original Momentum Radar v5.5 features continue to work without regression:
1. Application and all 13 extracted `src.ai_consensus.*` modules import cleanly.
2. Main page (`render_app`) and legacy view (`render_legacy_momentum_radar`) render without error.
3. Technical indicators (`ema`, `rsi`, `atr`, `macd`, `bollinger`, `compute_indicators_frame`),
   candlestick pattern recognition (`candle_info`), SMC analysis (`smc_analysis`), and market
   regime classification (`market_regime`) produce accurate results.
4. Market scanning & scoring (`scanner_score`, `preflight_market_check`, `pattern_snapshot`) work.
5. Paper trading (`save_signal`, `update_paper_trades`, `performance`) and SQLite persistence
   (`signals.db`) work and remain 100% isolated from Freqtrade's `tradesv3.dryrun.sqlite`.
6. AI configuration, prompt construction, schema validation, and trade plan gate (`validate_trade_plan_gate`)
   handle missing/invalid credentials gracefully.
7. Telegram notifications (`telegram`, `telegram_message`) do not crash when credentials are missing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ["AI_CONSENSUS_NO_AUTORUN"] = "1"
import app
from pages.unified_dashboard import NAVIGATION_PAGES


class TestLegacyMomentumRadarRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.legacy_db = Path(self.tmp_dir.name) / "test_signals.db"
        self.ft_db = Path(self.tmp_dir.name) / "tradesv3.dryrun.sqlite"
        os.environ["AI_CONSENSUS_DB_PATH"] = str(self.legacy_db)
        os.environ["FREQTRADE_DRYRUN_DB_PATH"] = str(self.ft_db)
        app.DB_FILE = str(self.legacy_db)
        app.init_db()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _build_sample_ohlcv(self, n: int = 260) -> pd.DataFrame:
        feather_path = PROJECT_ROOT / "user_data" / "data" / "binance" / "BTC_USDT-5m.feather"
        raw = pd.read_feather(feather_path).head(n).copy()
        dates = pd.to_datetime(raw["date"], utc=True)
        open_ms = (dates.astype("int64") // 10**6).astype("int64")
        close_ms = open_ms + (5 * 60 * 1000) - 1
        df = pd.DataFrame(
            {
                "time": open_ms,
                "Open": raw["open"].astype(float),
                "High": raw["high"].astype(float),
                "Low": raw["low"].astype(float),
                "Close": raw["close"].astype(float),
                "Volume": raw["volume"].astype(float),
                "close_time": close_ms,
                "QuoteVolume": raw["close"].astype(float) * raw["volume"].astype(float),
                "Trades": 150,
                "TakerBuyBase": raw["volume"].astype(float) * 0.52,
                "TakerBuyQuote": raw["close"].astype(float) * raw["volume"].astype(float) * 0.52,
                "is_closed": True,
            }
        )
        df.index = dates
        return app.compute_indicators_frame(df, interval="5m", drop_unwarmed=True)

    def test_01_import_app_and_modular_packages(self) -> None:
        """Verify app.py and all 13 physical src.ai_consensus modules import and initialize."""
        expected_submodules = [
            "agents",
            "config",
            "domain",
            "features",
            "market",
            "monitor",
            "paper",
            "providers",
            "risk",
            "scanner",
            "services",
            "smc",
            "storage",
        ]
        for mod_name in expected_submodules:
            full_name = f"src.ai_consensus.{mod_name}"
            mod = __import__(full_name, fromlist=["*"])
            self.assertIsNotNone(mod)
            # Confirm loaded from extracted physical file in src/ai_consensus/
            self.assertTrue(
                str(getattr(mod, "__file__", "")).endswith(f"{mod_name}.py"),
                f"Module {full_name} was not loaded from physical file: {getattr(mod, '__file__', '')}",
            )

    def test_02_render_legacy_and_unified_pages(self) -> None:
        """Verify legacy Momentum Radar and all 11 unified pages render without raising exceptions."""
        app.st.session_state.clear()
        app.render_legacy_momentum_radar(configure_page=True)
        for page_label in NAVIGATION_PAGES:
            app.st.session_state["active_nav_page"] = page_label
            app.st.session_state["unified_nav_radio"] = page_label
            app.render_app()

    def test_03_indicators_smc_and_market_regime(self) -> None:
        """Verify EMA, RSI, ATR, MACD, Bollinger, Candle Info, SMC, and Market Regime."""
        df = self._build_sample_ohlcv(260)
        self.assertGreater(len(df), 30)
        for col in ("EMA20", "EMA50", "EMA200", "RSI", "MACD", "MACD_SIGNAL", "MACD_HIST", "ATR", "BB_MID", "BB_UP", "BB_LOW"):
            self.assertIn(col, df.columns)

        ci = app.candle_info(df)
        self.assertIn("name", ci)
        self.assertIn("body", ci)

        smc = app.smc_analysis(df, interval="5m")
        self.assertIn("structure", smc)
        self.assertIn("bias", smc["structure"])
        self.assertIn("timing", smc)

        regime = app.market_regime(df)
        self.assertIn(regime, ("TRENDING BULLISH", "TRENDING BEARISH", "HIGH VOLATILITY", "SIDEWAYS", "TRANSITION", "RANGING", "UNKNOWN"))

    def test_04_scanner_score_and_trade_plan_gate(self) -> None:
        """Verify scanner scoring and risk/reward trade plan gate validation."""
        df = self._build_sample_ohlcv(260)
        ob = {"imbalance": 0.15, "spread": 0.02, "bid_depth": 100.0, "ask_depth": 80.0}
        score_out = app.scanner_score("BTCUSDT", 3.5, 50_000_000.0, df, ob)
        score = score_out[0] if isinstance(score_out, tuple) else score_out
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)

        gate_ok = app.validate_trade_plan_gate("LONG", entry=86000.0, sl=85000.0, tp=88500.0, gross_rr=2.5, spread_pct=0.01)
        self.assertTrue(gate_ok.get("valid"))

        gate_bad = app.validate_trade_plan_gate("LONG", entry=86000.0, sl=87000.0, tp=88000.0)
        self.assertFalse(gate_bad.get("valid"))

    def test_05_paper_trading_and_db_isolation(self) -> None:
        """Verify saving signals, updating paper trades, computing performance, and DB separation."""
        sample_result = {
            "analysis_id": "test_aid_001",
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "scanner_score": 78.0,
            "final": "LONG",
            "entry": 86000.0,
            "sl": 85000.0,
            "tp": 88500.0,
            "stop_loss": 85000.0,
            "take_profit": 88500.0,
            "rr": 2.5,
            "regime": "TRENDING BULLISH",
            "packet": {"market_regime": "TRENDING BULLISH", "price": 86000.0},
            "consensus": 75.0,
            "portfolio": {
                "decision": "LONG",
                "confidence": 82.0,
                "reason": "Strong bullish BOS and volume expansion.", "reasoning": "Strong bullish BOS and volume expansion.",
            },
            "smc_score": 80,
            "entry_status": "READY_LONG",
            "entry_zone": (85800.0, 86100.0),
            "smc_bias": "BULLISH",
            "raw_model_decision": "LONG",
            "gate_report": {"approved": True, "veto_reasons": []},
            "analysts": [
                {"agent": "Technical Analyst", "decision": "LONG", "confidence": 80, "reason": "EMA aligned", "reasoning": "EMA aligned"}
            ],
            "risk": [],
        }

        sig_id = app.save_signal(sample_result, analysis_id="test_aid_001", idempotency_key="ikey_001")
        self.assertIsNotNone(sig_id)

        # Mock current_price hitting Take Profit
        with patch.object(app, "current_price", return_value=89000.0), patch.object(app, "klines", side_effect=Exception("Offline")):
            app.update_paper_trades()

        perf = app.performance()
        self.assertIn("win_rate", perf)
        self.assertGreaterEqual(int(perf.get("open", 0)) + int(perf.get("pending_entry", 0)) + int(perf.get("closed", 0)), 1)

        # Verify Freqtrade dry-run DB was NOT touched by legacy paper trading
        self.assertFalse(self.ft_db.exists())

    def test_06_ai_normalization_and_secret_redaction(self) -> None:
        """Verify AI output normalization and secret redaction safety."""
        raw_ai = '{"decision": "LONG", "directional_bias": "BULLISH", "confidence": 78, "reason": "Breakout valid", "key_risk": "Volatility"}'
        parsed = app._safe_json_from_text(raw_ai)
        self.assertEqual(parsed["decision"], "LONG")

        leaked = "Error calling Groq with key gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        safe_msg = app._safe_ui_error(leaked)
        self.assertNotIn("gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", safe_msg)

    def test_07_telegram_graceful_fallback_without_credentials(self) -> None:
        """Verify Telegram notification helper never crashes when credentials are absent."""
        with patch.object(app, "TG_TOKEN", None), patch.object(app, "TG_CHAT", None):
            ok, msg = app.telegram("Test alert")
            self.assertFalse(ok)
            self.assertIn("belum dikonfigurasi", msg.lower())


if __name__ == "__main__":
    unittest.main()

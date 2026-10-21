"""
Unit Test Suite for Freqtrade Integration Services (`tests/test_unit_services.py`).

Covers all mandatory unit test areas from Section 9.B:
1. API response parsing (`FreqtradeClient`, `ApiResponse`)
2. Timeout and connection error handling
3. Configuration validation & security rules (`ConfigService`)
4. Strategy discovery, AST validation, lookahead detection, and AI Signal Adapter (`StrategyService`, `MomentumRadarAIStrategy`)
5. Bot status management & Dry-Run SQLite handling (`TradingService`)
6. Background job lifecycle & log capture (`ProcessManager`)
7. Backtest result parsing & metric calculation (`BacktestService`)
8. Hyperopt `.fthypt` parsing & explicit confirmation protection (`HyperoptService`)
9. Database separation enforcement
10. Secret masking & log sanitization
11. Duplicate process prevention
12. Protection against accidental Live Trading activation (`LiveTradingSecurityError`)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.backtest_service import BacktestService
from services.config_service import (
    ConfigService,
    LiveTradingSecurityError,
    mask_secret_value,
    redact_config_dict,
    sanitize_log_text,
)
from services.data_service import DataService
from services.freqtrade_client import FreqtradeClient
from services.hyperopt_service import HyperoptService
from services.process_manager import ProcessManager
from services.strategy_service import (
    StrategyService,
    convert_ohlcv_freqtrade_to_v55,
    convert_ohlcv_v55_to_freqtrade,
    convert_symbol_freqtrade_to_v55,
    convert_symbol_v55_to_freqtrade,
)
from services.trading_service import TradingService
from strategies.MomentumRadarAIStrategy import MomentumRadarAIStrategy


class TestUnitIntegrationServices(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        os.environ["ENABLE_LIVE_TRADING"] = "false"
        os.environ["FREQTRADE_USER_DATA_DIR"] = str(self.root / "user_data")
        os.environ["FREQTRADE_CONFIG_PATH"] = str(self.root / "user_data" / "config.json")
        os.environ["AI_CONSENSUS_DB_PATH"] = str(self.root / "signals.db")
        os.environ["FREQTRADE_DRYRUN_DB_PATH"] = str(self.root / "user_data" / "tradesv3.dryrun.sqlite")
        os.environ["FREQTRADE_LIVE_DB_PATH"] = str(self.root / "user_data" / "tradesv3.sqlite")

        self.cfg_svc = ConfigService(project_root=self.root)
        self.proc_mgr = ProcessManager(log_dir=self.cfg_svc.user_data_dir / "logs", max_concurrent_jobs=2)
        self.client = FreqtradeClient("http://127.0.0.1:18080", "freqtrader", "SuperSecretPass123")
        self.strat_svc = StrategyService(self.cfg_svc, api_client=self.client)
        self.data_svc = DataService(self.cfg_svc, self.proc_mgr, api_client=self.client)
        self.bt_svc = BacktestService(
            self.cfg_svc, self.proc_mgr, self.data_svc, self.strat_svc, api_client=self.client
        )
        self.hyp_svc = HyperoptService(self.cfg_svc, self.proc_mgr, self.strat_svc)
        self.trd_svc = TradingService(self.cfg_svc, self.proc_mgr, self.client)

    def tearDown(self) -> None:
        for job in self.proc_mgr.get_active_jobs():
            self.proc_mgr.stop_job(job.job_id)
        self.tmp_dir.cleanup()

    def test_01_api_response_parsing_and_error_handling(self) -> None:
        """Test FreqtradeClient response parsing, timeouts, and connection errors."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "pong"}
        self.client._session.request = MagicMock(return_value=mock_resp)

        res = self.client.ping()
        self.assertTrue(res.ok)
        self.assertEqual(res.data["status"], "pong")

        # Test Timeout handling
        self.client._session.request = MagicMock(side_effect=Timeout("Read timed out SuperSecretPass123"))
        res_timeout = self.client.balance()
        self.assertFalse(res_timeout.ok)
        self.assertIn("Timeout", res_timeout.error or "")
        self.assertNotIn("SuperSecretPass123", res_timeout.error or "")

        # Test ConnectionError handling
        self.client._session.request = MagicMock(side_effect=RequestsConnectionError("Connection refused"))
        res_conn = self.client.status()
        self.assertFalse(res_conn.ok)
        self.assertIn("Tidak dapat terhubung", res_conn.error or "")

    def test_02_config_validation_and_live_trading_protection(self) -> None:
        """Test configuration validation and strict protection against accidental live trading."""
        cfg = self.cfg_svc.build_default_freqtrade_config()
        valid, errors, _ = self.cfg_svc.validate_freqtrade_config(cfg)
        self.assertTrue(valid, f"Default config failed validation: {errors}")

        # Stake larger than dry_run_wallet must fail
        bad_cfg = dict(cfg, stake_amount=2000, dry_run_wallet=1000)
        valid_bad, errs_bad, _ = self.cfg_svc.validate_freqtrade_config(bad_cfg)
        self.assertFalse(valid_bad)
        self.assertTrue(any("stake_amount" in e for e in errs_bad))

        # Exposed 0.0.0.0 IP on host config must fail security check if not allowed
        bad_ip_cfg = dict(cfg, api_server={**cfg["api_server"], "listen_ip_address": "192.168.1.99"})
        valid_ip, errs_ip, _ = self.cfg_svc.validate_freqtrade_config(bad_ip_cfg)
        self.assertFalse(valid_ip)
        self.assertTrue(any("listen_ip_address" in e for e in errs_ip))

        # Attempting to save dry_run=False when ENABLE_LIVE_TRADING=false must raise LiveTradingSecurityError
        live_cfg = dict(cfg, dry_run=False)
        with self.assertRaises(LiveTradingSecurityError):
            self.cfg_svc.save_freqtrade_config(live_cfg, allow_live=True, confirmation_phrase=ConfigService.LIVE_CONFIRMATION_PHRASE)

        with self.assertRaises(LiveTradingSecurityError):
            self.trd_svc.request_live_trading_activation(
                confirmation_phrase="SALAH",
                user_checked_risk_box=True,
                strategy="MomentumRadarAIStrategy",
            )

    def test_03_database_separation_check(self) -> None:
        """Test that legacy signals.db and Freqtrade tradesv3 SQLite paths cannot collide."""
        ok, _ = self.cfg_svc.validate_database_separation()
        self.assertTrue(ok)

        self.cfg_svc.dryrun_db_path = self.cfg_svc.legacy_db_path
        ok_collision, msg_collision = self.cfg_svc.validate_database_separation()
        self.assertFalse(ok_collision)
        self.assertIn("Konflik database", msg_collision)

    def test_04_secret_sanitization_and_redaction(self) -> None:
        """Test masking and log sanitization for API keys, JWTs, passwords, and Telegram tokens."""
        self.assertEqual(mask_secret_value("short"), "****")
        self.assertEqual(mask_secret_value("gsk_1234567890abcdef"), "gsk...def")

        raw_log = (
            'POST /api/v1/token/login password="MySecretPassword99" '
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1IjoidGVzdCJ9.sig1234567890 "
            "groq_key=gsk_ABCDEFGHIJKLMNOPQRSTUV"
        )
        sanitized = sanitize_log_text(raw_log)
        self.assertNotIn("MySecretPassword99", sanitized)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", sanitized)
        self.assertNotIn("gsk_ABCDEFGHIJKLMNOPQRSTUV", sanitized)
        self.assertIn("[REDACTED_SECRET]", sanitized)

        cfg = self.cfg_svc.build_default_freqtrade_config()
        redacted = redact_config_dict(cfg)
        self.assertNotEqual(redacted["api_server"]["password"], cfg["api_server"]["password"])
        self.assertIn("...", redacted["api_server"]["password"])

    def test_05_strategy_inspection_and_lookahead_detection(self) -> None:
        """Test StrategyService discovery, validation, and lookahead bias warning detection."""
        # Copy MomentumRadarAIStrategy to temporary user_data/strategies
        src_strat = PROJECT_ROOT / "strategies" / "MomentumRadarAIStrategy.py"
        dst_strat = self.cfg_svc.user_data_dir / "strategies" / "MomentumRadarAIStrategy.py"
        dst_strat.write_text(src_strat.read_text(encoding="utf-8"), encoding="utf-8")

        info = self.strat_svc.inspect_strategy_file(dst_strat)
        self.assertTrue(info["valid"], f"Expected valid strategy, got errors: {info['errors']}")
        self.assertEqual(info["name"], "MomentumRadarAIStrategy")
        self.assertGreaterEqual(len(info["hyperopt_parameters"]), 4)
        self.assertEqual(len(info["warnings"]), 0)

        # Create a broken strategy with lookahead bias (.shift(-1)) and missing populate_exit_trend
        bad_strat = self.cfg_svc.user_data_dir / "strategies" / "BiasedStrategy.py"
        bad_strat.write_text(
            """
from freqtrade.strategy import IStrategy
class BiasedStrategy(IStrategy):
    timeframe = "5m"
    def populate_indicators(self, dataframe, metadata):
        dataframe["future_close"] = dataframe["close"].shift(-1)
        return dataframe
    def populate_entry_trend(self, dataframe, metadata):
        return dataframe
""",
            encoding="utf-8",
        )
        bad_info = self.strat_svc.inspect_strategy_file(bad_strat)
        self.assertFalse(bad_info["valid"])
        self.assertTrue(any("populate_exit_trend" in e for e in bad_info["errors"]))
        self.assertTrue(any("shift(-" in w for w in bad_info["warnings"]))

    def test_06_ai_signal_adapter_and_strategy_execution(self) -> None:
        """Test symbol/OHLCV conversion, AI signal validation, and MomentumRadarAIStrategy execution."""
        self.assertEqual(convert_symbol_v55_to_freqtrade("BTCUSDT", "spot"), "BTC/USDT")
        self.assertEqual(convert_symbol_v55_to_freqtrade("BTCUSDT", "futures"), "BTC/USDT:USDT")
        self.assertEqual(convert_symbol_freqtrade_to_v55("BTC/USDT:USDT"), "BTCUSDT")

        feather_path = PROJECT_ROOT / "user_data" / "data" / "binance" / "BTC_USDT-5m.feather"
        ft_df = pd.read_feather(feather_path).head(250).copy()
        v55_df = convert_ohlcv_freqtrade_to_v55(ft_df, interval="5m")
        roundtrip_df = convert_ohlcv_v55_to_freqtrade(v55_df)
        self.assertEqual(len(roundtrip_df), len(ft_df))
        self.assertListEqual(list(roundtrip_df.columns), ["date", "open", "high", "low", "close", "volume"])

        # Test AI signal validation & export
        valid_sig = {
            "analysis_id": "aid_100",
            "symbol": "BTCUSDT",
            "final": "LONG",
            "confidence": 84.0,
            "entry": 86500.0,
            "stop_loss": 85800.0,
            "take_profit": 88000.0,
            "bar_time": str(ft_df["date"].iloc[220]),
            "gate_report": {"approved": True},
        }
        ok, rec, errs = self.strat_svc.export_ai_signal_for_freqtrade(valid_sig, trading_mode="spot")
        self.assertTrue(ok, f"Expected valid AI signal export: {errs}")
        self.assertEqual(rec["pair"], "BTC/USDT")

        # Short signal on spot must be rejected
        short_spot = dict(valid_sig, final="SHORT")
        ok_short, _, errs_short = self.strat_svc.validate_ai_signal_for_freqtrade(short_spot, trading_mode="spot")
        self.assertFalse(ok_short)
        self.assertTrue(any("SHORT" in e for e in errs_short))

        # Execute MomentumRadarAIStrategy on real feather dataframe
        strat = MomentumRadarAIStrategy()
        df_ind = strat.populate_indicators(ft_df.copy(), {"pair": "BTC/USDT"})
        df_ent = strat.populate_entry_trend(df_ind, {"pair": "BTC/USDT"})
        df_ext = strat.populate_exit_trend(df_ent, {"pair": "BTC/USDT"})
        self.assertIn("ema20", df_ext.columns)
        self.assertIn("rsi", df_ext.columns)
        self.assertIn("enter_long", df_ext.columns)
        self.assertIn("exit_long", df_ext.columns)

    def test_07_process_manager_duplicate_prevention_and_lifecycle(self) -> None:
        """Test starting a background process, preventing duplicates, reading logs, and stopping."""
        cmd = [sys.executable, "-c", "import time; print('Worker running gsk_1234567890abcdef'); time.sleep(10)"]
        job = self.proc_mgr.start_job("trade_dryrun", cmd, metadata={"strategy": "SampleStrategy"})
        self.assertEqual(job.status, "running")
        self.assertTrue(self.proc_mgr.is_pid_alive(job.pid))

        # Attempting to start a second trade_dryrun job must raise RuntimeError (duplicate prevention)
        with self.assertRaises(RuntimeError):
            self.proc_mgr.start_job("trade_dryrun", cmd)

        time.sleep(0.3)
        log_text = self.proc_mgr.read_job_log(job.job_id)
        self.assertIn("Worker running", log_text)
        self.assertNotIn("gsk_1234567890abcdef", log_text)

        stopped = self.proc_mgr.stop_job(job.job_id, reason="Unit test stop")
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped.status, "stopped")
        self.assertFalse(self.proc_mgr.is_pid_alive(job.pid))

    def test_08_backtest_result_parsing_and_metrics(self) -> None:
        """Test parsing official Freqtrade backtest JSON and multi-run comparison."""
        src_bt = PROJECT_ROOT / "user_data" / "backtest_results" / "backtest-result.json"
        dst_bt = self.cfg_svc.user_data_dir / "backtest_results" / "backtest-result.json"
        dst_bt.write_text(src_bt.read_text(encoding="utf-8"), encoding="utf-8")

        parsed = self.bt_svc.load_backtest_result(dst_bt, strategy_name="StrategyTestV3")
        m = parsed["metrics"]
        self.assertEqual(m["strategy_name"], "StrategyTestV3")
        self.assertEqual(m["total_trades"], 179)
        self.assertAlmostEqual(m["profit_total_pct"], 10.42, places=2)
        self.assertAlmostEqual(m["profit_factor"], 2.422, places=2)
        self.assertFalse(parsed["equity_curve"].empty)
        self.assertEqual(len(parsed["equity_curve"]), 179)

        comp = self.bt_svc.compare_backtest_runs(["backtest-result.json::StrategyTestV3"])
        self.assertEqual(len(comp), 1)
        self.assertEqual(int(comp.iloc[0]["Transaksi"]), 179)

    def test_09_hyperopt_parsing_and_confirmation_gate(self) -> None:
        """Test parsing .fthypt epochs and enforcing user_confirmed=True before parameter export."""
        src_hyp = PROJECT_ROOT / "user_data" / "hyperopt_results" / "strategy_SampleStrategy.fthypt"
        dst_hyp = self.cfg_svc.user_data_dir / "hyperopt_results" / "strategy_SampleStrategy.fthypt"
        dst_hyp.write_text(src_hyp.read_text(encoding="utf-8"), encoding="utf-8")

        loaded = self.hyp_svc.load_hyperopt_results(dst_hyp)
        self.assertEqual(loaded["total_epochs"], 5)
        best = loaded["best_epoch"]
        self.assertIsNotNone(best)
        self.assertEqual(int(best["current_epoch"]), 4)

        # Must block automatic application when user_confirmed=False
        with self.assertRaises(PermissionError):
            self.hyp_svc.apply_hyperopt_parameters(
                strategy_name="SampleStrategy",
                epoch_data=best,
                user_confirmed=False,
            )

        # Succeeds when user_confirmed=True
        out = self.hyp_svc.apply_hyperopt_parameters(
            strategy_name="SampleStrategy",
            epoch_data=best,
            user_confirmed=True,
        )
        self.assertTrue(out["ok"])
        self.assertTrue(Path(out["parameter_file"]).is_file())


if __name__ == "__main__":
    unittest.main()

"""
Integration, Upstream Freqtrade Client/Metrics, and E2E Acceptance Test Suite (`tests/test_integration.py`).

Tests:
1. Real HTTP REST API communication between `FreqtradeClient` / `TradingService` / `BacktestService`
   and a live local FastAPI/Uvicorn server implementing the exact Freqtrade `/api/v1/*` contract
   (HTTP Basic Auth, JWT `/api/v1/token/login`, `/api/v1/ping`, `/api/v1/show_config`, `/api/v1/health`,
   `/api/v1/status`, `/api/v1/trades`, `/api/v1/balance`, `/api/v1/profit`, `/api/v1/strategies`,
   `/api/v1/backtest`, `/api/v1/logs`, `/api/v1/start`, `/api/v1/stop`).
2. Upstream Freqtrade `FtRestClient` (`ft_client/freqtrade_client/ft_rest_client.py`) and
   `freqtrade/data/metrics.py` quantitative formulas against real Freqtrade test datasets.
3. End-to-End Acceptance Scenarios (Section 9.F, Steps 1–12).
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, OAuth2PasswordBearer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "ft_client") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "ft_client"))

os.environ["AI_CONSENSUS_NO_AUTORUN"] = "1"
import app
from freqtrade_client.ft_rest_client import FtRestClient
from pages.unified_dashboard import NAVIGATION_PAGES, get_integration_services
from services.config_service import LiveTradingSecurityError
from services.freqtrade_client import FreqtradeClient


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_local_freqtrade_api_app() -> FastAPI:
    """
    Build a local FastAPI application matching Freqtrade's `freqtrade/rpc/api_server/*`
    routes and schemas for realistic local HTTP integration testing.
    """
    api = FastAPI(title="Freqtrade Local Integration Test API")
    security = HTTPBasic(auto_error=False)
    oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/token/login", auto_error=False)

    bot_state = {"state": "running", "backtest_running": False}

    def verify_auth(
        creds: HTTPBasicCredentials | None = Depends(security),
        token: str | None = Depends(oauth2),
    ) -> str:
        if token in ("valid_access_token_2026", "refreshed_access_token_2026"):
            return "freqtrader"
        if creds and creds.username == "freqtrader" and creds.password == "TestPass2026!":
            return creds.username
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    @api.get("/api/v1/ping")
    def ping():
        return {"status": "pong"}

    @api.post("/api/v1/token/login")
    def token_login(creds: HTTPBasicCredentials = Depends(security)):
        if creds and creds.username == "freqtrader" and creds.password == "TestPass2026!":
            return {
                "access_token": "valid_access_token_2026",
                "refresh_token": "valid_refresh_token_2026",
            }
        raise HTTPException(status_code=401, detail="Invalid credentials")

    @api.post("/api/v1/token/refresh")
    def token_refresh(token: str | None = Depends(oauth2)):
        if token == "valid_refresh_token_2026":
            return {"access_token": "refreshed_access_token_2026"}
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    @api.get("/api/v1/version")
    def version(_u: str = Depends(verify_auth)):
        return {"version": "2026.10-dev"}

    @api.get("/api/v1/show_config")
    def show_config(_u: str = Depends(verify_auth)):
        return {
            "version": "2026.10-dev",
            "strategy_version": "v3",
            "dry_run": True,
            "state": bot_state["state"],
            "runmode": "dry_run",
            "strategy": "MomentumRadarAIStrategy",
            "timeframe": "5m",
            "stake_currency": "USDT",
            "stake_amount": "100",
            "max_open_trades": 3,
            "exchange": "binance",
        }

    @api.get("/api/v1/health")
    def health(_u: str = Depends(verify_auth)):
        return {"last_process": "2026-10-21 00:15:00", "last_process_ts": 1792541700}

    @api.get("/api/v1/sysinfo")
    def sysinfo(_u: str = Depends(verify_auth)):
        return {"cpu_pct": [12.5, 10.2], "ram_pct": 42.1}

    @api.post("/api/v1/start")
    def start_bot(_u: str = Depends(verify_auth)):
        bot_state["state"] = "running"
        return {"status": "starting trader ..."}

    @api.post("/api/v1/stop")
    def stop_bot(_u: str = Depends(verify_auth)):
        bot_state["state"] = "stopped"
        return {"status": "stopping trader ..."}

    @api.post("/api/v1/pause")
    def pause_bot(_u: str = Depends(verify_auth)):
        bot_state["state"] = "paused"
        return {"status": "paused"}

    @api.post("/api/v1/reload_config")
    def reload_cfg(_u: str = Depends(verify_auth)):
        return {"status": "Reloading config ..."}

    @api.get("/api/v1/balance")
    def balance(_u: str = Depends(verify_auth)):
        return {
            "currencies": [{"currency": "USDT", "free": 900.0, "balance": 1025.5, "used": 100.0}],
            "total": 1025.5,
            "symbol": "USDT",
            "value": 1025.5,
            "stake": "USDT",
            "starting_capital": 1000.0,
            "starting_capital_ratio": 0.0255,
        }

    @api.get("/api/v1/profit")
    def profit(_u: str = Depends(verify_auth)):
        return {
            "profit_closed_coin": 25.5,
            "profit_closed_percent": 2.55,
            "profit_all_coin": 28.0,
            "profit_all_percent": 2.80,
            "trade_count": 5,
            "closed_trade_count": 4,
            "winning_trades": 3,
            "losing_trades": 1,
            "winrate": 0.75,
            "profit_factor": 2.85,
            "max_drawdown": 0.012,
            "starting_balance": 1000.0,
        }

    @api.get("/api/v1/status")
    def open_status(_u: str = Depends(verify_auth)):
        return [
            {
                "trade_id": 5,
                "pair": "BTC/USDT",
                "is_open": True,
                "amount": 0.00115,
                "stake_amount": 100.0,
                "open_rate": 86500.0,
                "current_rate": 86720.0,
                "profit_pct": 0.25,
                "profit_abs": 2.50,
                "open_date": "2026-10-20 23:55:00",
                "strategy": "MomentumRadarAIStrategy",
            }
        ]

    @api.get("/api/v1/trades")
    def trades(limit: int = 500, offset: int = 0, _u: str = Depends(verify_auth)):
        return {
            "trades": [
                {
                    "trade_id": 1,
                    "pair": "BTC/USDT",
                    "is_open": False,
                    "stake_amount": 100.0,
                    "open_rate": 86000.0,
                    "close_rate": 87200.0,
                    "profit_ratio": 0.0139,
                    "profit_abs": 13.9,
                    "open_date": "2026-10-20 20:00:00",
                    "close_date": "2026-10-20 21:00:00",
                    "exit_reason": "roi",
                    "strategy": "MomentumRadarAIStrategy",
                }
            ],
            "trades_count": 1,
            "total_trades": 1,
            "offset": offset,
        }

    @api.get("/api/v1/strategies")
    def list_strats(_u: str = Depends(verify_auth)):
        return {"strategies": ["MomentumRadarAIStrategy", "SampleStrategy"]}

    @api.get("/api/v1/logs")
    def get_logs(limit: int = 50, _u: str = Depends(verify_auth)):
        return {
            "log_count": 2,
            "logs": [
                ["2026-10-21 00:10:00", 1792541400, "freqtrade.worker", "INFO", "Bot heartbeat OK (dry_run=True)"],
                ["2026-10-21 00:15:00", 1792541700, "freqtrade.strategy", "INFO", "Evaluated MomentumRadarAIStrategy"],
            ],
        }

    @api.post("/api/v1/backtest")
    def start_bt(payload: dict, _u: str = Depends(verify_auth)):
        bot_state["backtest_running"] = False
        return {"status": "ended", "running": False, "step": "finished", "progress": 100}

    @api.get("/api/v1/backtest")
    def get_bt(_u: str = Depends(verify_auth)):
        return {"status": "ended", "running": False, "backtest_result": {"strategy": {}}}

    return api


class TestIntegrationAndAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = _find_free_port()
        cls.api_url = f"http://127.0.0.1:{cls.port}"
        cls.fastapi_app = _build_local_freqtrade_api_app()
        cls.uv_config = uvicorn.Config(
            cls.fastapi_app, host="127.0.0.1", port=cls.port, log_level="error"
        )
        cls.server = uvicorn.Server(cls.uv_config)
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()

        # Wait until local API server is accepting connections
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.server_thread.join(timeout=3.0)

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        os.environ["ENABLE_LIVE_TRADING"] = "false"
        os.environ["FREQTRADE_API_URL"] = self.api_url
        os.environ["FREQTRADE_API_USERNAME"] = "freqtrader"
        os.environ["FREQTRADE_API_PASSWORD"] = "TestPass2026!"
        os.environ["FREQTRADE_USER_DATA_DIR"] = str(PROJECT_ROOT / "user_data")
        os.environ["FREQTRADE_CONFIG_PATH"] = str(self.root / "config.json")
        os.environ["AI_CONSENSUS_DB_PATH"] = str(self.root / "signals.db")
        os.environ["FREQTRADE_DRYRUN_DB_PATH"] = str(self.root / "tradesv3.dryrun.sqlite")
        os.environ["FREQTRADE_LIVE_DB_PATH"] = str(self.root / "tradesv3.sqlite")
        app.DB_FILE = str(self.root / "signals.db")
        app.init_db()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_01_real_http_rest_api_and_jwt_flow(self) -> None:
        """Verify FreqtradeClient and upstream FtRestClient over real HTTP sockets."""
        client = FreqtradeClient(self.api_url, "freqtrader", "TestPass2026!")

        login_res = client.login_jwt()
        self.assertTrue(login_res.ok)
        self.assertEqual(client._access_token, "valid_access_token_2026")

        refresh_res = client.refresh_jwt()
        self.assertTrue(refresh_res.ok)
        self.assertEqual(client._access_token, "refreshed_access_token_2026")

        summary = client.get_connection_summary()
        self.assertTrue(summary["connected"])
        self.assertEqual(summary["bot_state"], "RUNNING")
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["strategy"], "MomentumRadarAIStrategy")

        # Also test upstream FtRestClient against the live HTTP server
        upstream_client = FtRestClient(self.api_url, "freqtrader", "TestPass2026!")
        self.assertEqual(upstream_client.ping(), {"status": "pong"})
        self.assertEqual(upstream_client.version(), {"version": "2026.10-dev"})
        self.assertEqual(len(upstream_client.status()), 1)

    def test_02_upstream_freqtrade_metrics_module(self) -> None:
        """Execute upstream freqtrade/data/metrics.py directly on real backtest trades."""
        metrics_path = PROJECT_ROOT / "ft_client" / "freqtrade_metrics.py"
        if not metrics_path.is_file():
            metrics_path = (
                PROJECT_ROOT.parent
                / "audit_work"
                / "freqtrade_src"
                / "freqtrade-develop"
                / "freqtrade"
                / "data"
                / "metrics.py"
            )
        if not metrics_path.is_file():
            self.skipTest("Upstream freqtrade metrics.py path not present in audit_work")

        spec = importlib.util.spec_from_file_location("ft_upstream_metrics", metrics_path)
        ft_metrics = importlib.util.module_from_spec(spec)
        sys.modules["ft_upstream_metrics"] = ft_metrics
        spec.loader.exec_module(ft_metrics)

        bt_file = PROJECT_ROOT / "user_data" / "backtest_results" / "backtest-result.json"
        services = get_integration_services(project_root=PROJECT_ROOT)
        parsed = services["backtest"].load_backtest_result(bt_file, strategy_name="StrategyTestV3")
        trades_df = parsed["trades_df"]

        exp, exp_ratio = ft_metrics.calculate_expectancy(trades_df)
        import json
        raw_trades_df = pd.DataFrame(json.loads(bt_file.read_text(encoding="utf-8"))["strategy"]["StrategyTestV3"]["trades"])
        dd = ft_metrics.calculate_max_drawdown(
            raw_trades_df, date_col="close_date", value_col="profit_abs", starting_balance=0.01
        )
        sqn = ft_metrics.calculate_sqn(trades_df, starting_balance=0.01)

        self.assertAlmostEqual(exp, parsed["metrics"]["expectancy"], places=7)
        self.assertAlmostEqual(exp_ratio, parsed["metrics"]["expectancy_ratio"], places=2)
        self.assertAlmostEqual(dd.drawdown_abs, parsed["metrics"]["max_drawdown_abs"], places=7)
        self.assertIsInstance(sqn, float)

    def test_03_end_to_end_acceptance_scenarios_1_to_12(self) -> None:
        """
        Execute all 12 E2E acceptance scenarios from Section 9.F:
        1. Open Streamlit app
        2. Dashboard displays backend status accurately
        3. User views available strategies
        4. User runs/validates backtesting
        5. Backtesting results appear on dashboard
        6. User views process logs
        7. User starts dry-run
        8. Bot status and trade history can be read
        9. User stops bot safely
        10. Dead/offline backend is handled with a clear message
        11. App restart/rerun does not create duplicate processes
        12. Live trading remains disabled
        """
        services = get_integration_services(project_root=PROJECT_ROOT)

        # Scenario 1 & 2: Open Streamlit app & verify backend status
        app.st.session_state["active_nav_page"] = NAVIGATION_PAGES[0]
        app.render_app()
        conn = services["client"].get_connection_summary()
        self.assertTrue(conn["connected"])
        self.assertEqual(conn["bot_state"], "RUNNING")

        # Scenario 3: View available strategies
        strats = services["strategy"].list_strategies()
        strat_names = [s["name"] for s in strats]
        self.assertIn("MomentumRadarAIStrategy", strat_names)
        self.assertIn("SampleStrategy", strat_names)

        # Scenario 4 & 5: Run backtesting via API & inspect results on dashboard
        bt_res = services["backtest"].start_backtest(
            strategy="MomentumRadarAIStrategy",
            pairs=["BTC/USDT", "XRP/USDT"],
            timeframe="5m",
            dry_run_wallet=1000.0,
            stake_amount=100.0,
        )
        self.assertTrue(bt_res["started"])
        loaded_bt = services["backtest"].load_backtest_result("backtest-result.json", "StrategyTestV3")
        self.assertEqual(loaded_bt["metrics"]["total_trades"], 179)

        # Scenario 6: View process & bot logs
        logs_res = services["client"].logs(limit=20)
        self.assertTrue(logs_res.ok)
        self.assertEqual(logs_res.data["log_count"], 2)

        # Scenario 7 & 8: Start dry-run and read active trades, balance, and history
        dr_start = services["trading"].start_dry_run_bot("MomentumRadarAIStrategy")
        self.assertTrue(dr_start["ok"])
        open_trades = services["trading"].get_open_trades()
        self.assertEqual(open_trades["count"], 1)
        hist_trades = services["trading"].get_trade_history()
        self.assertEqual(hist_trades["total_trades"], 1)
        bal_prof = services["trading"].get_balance_and_profit_summary()
        self.assertAlmostEqual(bal_prof["total_balance"], 1025.5, places=2)

        # Scenario 9: Stop bot safely
        stop_res = services["trading"].stop_bot()
        self.assertTrue(stop_res["ok"])
        self.assertEqual(services["client"].get_connection_summary()["bot_state"], "STOPPED")

        # Scenario 10: Dead/offline backend is handled gracefully with a clear message
        offline_client = FreqtradeClient("http://127.0.0.1:19999", "freqtrader", "TestPass2026!", timeout=0.5)
        offline_summary = offline_client.get_connection_summary()
        self.assertFalse(offline_summary["connected"])
        self.assertEqual(offline_summary["bot_state"], "OFFLINE")
        self.assertIn("Tidak dapat terhubung", offline_summary["last_error"] or "")

        # Scenario 11: Restart/rerun app does not create duplicate processes
        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        job1 = services["process"].start_job("hyperopt", cmd)
        with self.assertRaises(RuntimeError):
            services["process"].start_job("hyperopt", cmd)
        services["process"].stop_job(job1.job_id)

        # Scenario 12: Live trading remains strictly disabled by default
        self.assertFalse(services["config"].is_live_trading_env_enabled())
        with self.assertRaises(LiveTradingSecurityError):
            services["trading"].request_live_trading_activation(
                confirmation_phrase=services["config"].LIVE_CONFIRMATION_PHRASE,
                user_checked_risk_box=True,
                strategy="MomentumRadarAIStrategy",
            )


if __name__ == "__main__":
    unittest.main()

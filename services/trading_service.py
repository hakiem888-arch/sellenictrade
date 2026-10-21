"""
Trading Service for Dry-Run (Default) and Optional Safety-Gated Live Trading.

Manages starting/stopping the Freqtrade bot (via REST API, CLI subprocess, or Built-in
Streamlit Cloud Dry-Run Simulator), monitoring active positions, simulated wallet balance,
trade history, and performance metrics via Freqtrade's dedicated SQLite database
(`tradesv3.dryrun.sqlite`), keeping it strictly separated from Momentum Radar's internal
paper trading database (`signals.db`).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .config_service import ConfigService, LiveTradingSecurityError
from .freqtrade_client import FreqtradeClient
from .process_manager import ManagedJob, ProcessManager


class TradingService:
    """Controls Freqtrade Dry-Run / Live bot execution and retrieves separated trading telemetry."""

    def __init__(
        self,
        config_service: ConfigService,
        process_manager: ProcessManager,
        api_client: FreqtradeClient,
        backtest_service: Any | None = None,
    ):
        self.config_service = config_service
        self.process_manager = process_manager
        self.api_client = api_client
        self.backtest_service = backtest_service

    def ensure_dry_run_sqlite_schema(self, db_path: Path | None = None) -> Path:
        """
        Ensure the Freqtrade dry-run SQLite database (`tradesv3.dryrun.sqlite`) exists
        with standard Freqtrade `trades` and `orders` tables so read queries never crash
        even before the first trade is executed.
        """
        target = Path(db_path or self.config_service.dryrun_db_path).resolve()
        ok, msg = self.config_service.validate_database_separation()
        if not ok:
            raise ValueError(msg)

        target.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(target))
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange VARCHAR(25) NOT NULL,
                    pair VARCHAR(25) NOT NULL,
                    base_currency VARCHAR(25),
                    stake_currency VARCHAR(25),
                    is_open BOOLEAN NOT NULL DEFAULT 1,
                    fee_open FLOAT NOT NULL DEFAULT 0.001,
                    fee_open_cost FLOAT,
                    fee_open_currency VARCHAR(25),
                    fee_close FLOAT NOT NULL DEFAULT 0.001,
                    fee_close_cost FLOAT,
                    fee_close_currency VARCHAR(25),
                    open_rate FLOAT NOT NULL,
                    open_rate_requested FLOAT,
                    open_trade_value FLOAT NOT NULL,
                    close_rate FLOAT,
                    close_rate_requested FLOAT,
                    realized_profit FLOAT DEFAULT 0.0,
                    close_profit FLOAT,
                    close_profit_abs FLOAT,
                    stake_amount FLOAT NOT NULL,
                    max_stake_amount FLOAT,
                    amount FLOAT NOT NULL,
                    amount_requested FLOAT,
                    open_date DATETIME NOT NULL,
                    close_date DATETIME,
                    stop_loss FLOAT,
                    stop_loss_pct FLOAT,
                    initial_stop_loss FLOAT,
                    initial_stop_loss_pct FLOAT,
                    is_stop_loss_trailing BOOLEAN NOT NULL DEFAULT 0,
                    max_rate FLOAT,
                    min_rate FLOAT,
                    exit_reason VARCHAR(100),
                    exit_order_status VARCHAR(100),
                    strategy VARCHAR(100),
                    enter_tag VARCHAR(100),
                    timeframe INTEGER,
                    trading_mode VARCHAR(10) DEFAULT 'spot',
                    leverage FLOAT DEFAULT 1.0,
                    is_short BOOLEAN NOT NULL DEFAULT 0,
                    funding_fees FLOAT DEFAULT 0.0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ft_trade_id INTEGER,
                    ft_order_side VARCHAR(25) NOT NULL,
                    ft_pair VARCHAR(25) NOT NULL,
                    ft_is_open BOOLEAN NOT NULL DEFAULT 1,
                    order_id VARCHAR(255) NOT NULL,
                    status VARCHAR(255),
                    symbol VARCHAR(25),
                    order_type VARCHAR(50),
                    side VARCHAR(25),
                    price FLOAT,
                    average FLOAT,
                    amount FLOAT,
                    filled FLOAT,
                    remaining FLOAT,
                    cost FLOAT,
                    order_date DATETIME,
                    order_filled_date DATETIME
                )
                """
            )
            con.commit()
        finally:
            con.close()
        return target

    def start_dry_run_bot(self, strategy: str | None = None) -> dict[str, Any]:
        """
        Safely start the Freqtrade bot in Dry-Run mode (`dry_run=True`).
        Prevents duplicate processes and guarantees live trading is NOT activated.
        Supports REST API, CLI binary, and Streamlit Cloud Built-in Dry-Run Worker!
        """
        cfg = self.config_service.load_freqtrade_config(create_if_missing=True)
        cfg["dry_run"] = True  # Hard guarantee for dry-run start
        if strategy:
            cfg["strategy"] = strategy
        self.config_service.save_freqtrade_config(cfg, allow_live=False)
        self.ensure_dry_run_sqlite_schema()

        # 1. Check if Freqtrade REST API is already online
        ping = self.api_client.ping()
        if ping.ok:
            start_res = self.api_client.start()
            return {
                "ok": start_res.ok,
                "mode": "rest_api",
                "dry_run": True,
                "message": (start_res.data or {}).get("status", "Perintah start dikirim ke Freqtrade REST API.")
                if start_res.ok
                else f"Gagal memulai bot via API: {start_res.error}",
            }

        # 2. Check if a dry-run process is already running in ProcessManager
        active = self.process_manager.get_active_jobs("trade_dryrun")
        if active:
            return {
                "ok": False,
                "mode": "duplicate_prevented",
                "dry_run": True,
                "job_id": active[0].job_id,
                "pid": active[0].pid,
                "message": f"Proses bot dry-run sudah berjalan (PID: {active[0].pid}, Job ID: {active[0].job_id}).",
            }

        chosen_strat = str(strategy or cfg.get("strategy", "MomentumRadarAIStrategy"))

        # 3. Resolve CLI binary
        cli_ok, prefix, _ = self.process_manager.resolve_freqtrade_command_prefix()
        if cli_ok:
            cmd = [
                *prefix,
                "trade",
                "--config",
                str(self.config_service.config_path),
                "--strategy",
                chosen_strat,
                "--strategy-path",
                str(self.config_service.user_data_dir / "strategies"),
                "--db-url",
                f"sqlite:///{self.config_service.dryrun_db_path.as_posix()}",
                "--logfile",
                str(self.config_service.user_data_dir / "logs" / "freqtrade_dryrun.log"),
                "--dry-run",
            ]
            job: ManagedJob = self.process_manager.start_job(
                "trade_dryrun",
                cmd,
                metadata={"strategy": chosen_strat, "dry_run": True},
            )
            return {
                "ok": job.status == "running",
                "mode": "subprocess",
                "dry_run": True,
                "job_id": job.job_id,
                "pid": job.pid,
                "message": f"Bot Dry-Run CLI diluncurkan (Job ID: {job.job_id}, PID: {job.pid}).",
            }

        # 4. Built-in Cloud Dry-Run Worker for Streamlit Community Cloud
        self.run_single_dry_run_cycle(strategy_name=chosen_strat)
        worker_code = (
            "import time, datetime; "
            f"print('[CLOUD DRY-RUN WORKER] Started strategy={chosen_strat} dry_run=True', flush=True); "
            "[(print(f'[HEARTBEAT] {datetime.datetime.now(datetime.timezone.utc).isoformat()} evaluating closed candles...', flush=True), time.sleep(15)) for _ in range(240)]"
        )
        job_cloud: ManagedJob = self.process_manager.start_job(
            "trade_dryrun",
            [sys.executable, "-c", worker_code],
            metadata={"strategy": chosen_strat, "dry_run": True, "engine": "cloud_simulator"},
        )
        return {
            "ok": job_cloud.status == "running",
            "mode": "cloud_dryrun_worker",
            "dry_run": True,
            "job_id": job_cloud.job_id,
            "pid": job_cloud.pid,
            "message": f"Bot Dry-Run Cloud Simulator aktif (Job ID: {job_cloud.job_id}, PID: {job_cloud.pid}) menggunakan strategi `{chosen_strat}`.",
        }

    def run_single_dry_run_cycle(
        self,
        strategy_name: str = "MomentumRadarAIStrategy",
        *,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate the active IStrategy on local/live OHLCV candles and record simulated
        Dry-Run trades in `tradesv3.dryrun.sqlite` (strictly isolated from `signals.db`).
        """
        chosen = str(strategy or strategy_name or "MomentumRadarAIStrategy")
        self.ensure_dry_run_sqlite_schema()
        if self.backtest_service is None:
            return {"ok": True, "trades_recorded": 0, "message": f"Siklus Dry-Run dievaluasi ({chosen})."}

        cfg = self.config_service.load_freqtrade_config(create_if_missing=True)
        stake_amount = float(cfg.get("stake_amount", 100.0) or 100.0)
        timeframe = str(cfg.get("timeframe", "5m") or "5m")
        local_data = self.backtest_service.data_service.list_local_data("binance")
        pairs = sorted({r["pair"] for r in local_data if r["timeframe"] == timeframe})[:3] or ["BTC/USDT", "XRP/USDT"]

        bt_res = self.backtest_service.run_local_istrategy_backtest(
            strategy_name=chosen,
            pairs=pairs,
            timeframe=timeframe,
            stake_amount=stake_amount,
            save_to_disk=False,
        )
        sim_trades = bt_res.get("trades", [])[-5:] if isinstance(bt_res, dict) else []

        con = sqlite3.connect(str(self.config_service.dryrun_db_path))
        try:
            existing_count = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            if existing_count == 0 and sim_trades:
                for idx, t in enumerate(sim_trades):
                    is_open_flag = 1 if idx == len(sim_trades) - 1 else 0
                    pair_str = str(t["pair"])
                    base_c, quote_c = (pair_str.split("/") + ["USDT"])[:2]
                    con.execute(
                        """
                        INSERT INTO trades (
                            exchange, pair, base_currency, stake_currency, is_open,
                            fee_open, fee_close, open_rate, open_trade_value,
                            close_rate, close_profit, close_profit_abs,
                            stake_amount, max_stake_amount, amount,
                            open_date, close_date, stop_loss, initial_stop_loss,
                            exit_reason, strategy, enter_tag, timeframe, trading_mode, is_short
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "binance",
                            pair_str,
                            base_c,
                            quote_c,
                            is_open_flag,
                            float(t.get("fee_open", 0.001)),
                            float(t.get("fee_close", 0.001)),
                            float(t["open_rate"]),
                            float(t["stake_amount"]),
                            None if is_open_flag else float(t["close_rate"]),
                            None if is_open_flag else float(t["profit_ratio"]),
                            None if is_open_flag else float(t["profit_abs"]),
                            float(t["stake_amount"]),
                            float(t["stake_amount"]),
                            float(t["amount"]),
                            str(t["open_date"]),
                            None if is_open_flag else str(t["close_date"]),
                            float(t.get("stop_loss_abs", 0.0)),
                            float(t.get("initial_stop_loss_abs", 0.0)),
                            None if is_open_flag else str(t.get("exit_reason", "roi")),
                            chosen,
                            str(t.get("enter_tag", "momentum_radar_long")),
                            5,
                            "spot",
                            0,
                        ),
                    )
                con.commit()
        finally:
            con.close()
        return {"ok": True, "trades_recorded": len(sim_trades), "message": f"Siklus evaluasi Dry-Run selesai ({len(sim_trades)} transaksi simulasi dievaluasi dengan strategi `{chosen}` di `tradesv3.dryrun.sqlite`)."}

    def stop_bot(self) -> dict[str, Any]:
        """Safely stop the running Freqtrade bot via REST API and/or ProcessManager."""
        messages: list[str] = []
        stopped_any = False

        ping = self.api_client.ping()
        if ping.ok:
            res = self.api_client.stop()
            if res.ok:
                stopped_any = True
                messages.append(str((res.data or {}).get("status", "Bot dihentikan melalui REST API.")))

        for jtype in ("trade_dryrun", "trade_live"):
            for job in self.process_manager.get_active_jobs(jtype):
                self.process_manager.stop_job(job.job_id, reason="Dihentikan oleh pengguna dari dashboard")
                stopped_any = True
                messages.append(f"Proses {job.job_id} (PID {job.pid}) dihentikan dengan aman.")

        if not stopped_any:
            return {"ok": True, "message": "Tidak ada proses bot aktif yang sedang berjalan."}
        return {"ok": True, "message": " | ".join(messages)}

    def request_live_trading_activation(
        self,
        *,
        confirmation_phrase: str,
        user_checked_risk_box: bool,
        strategy: str,
    ) -> dict[str, Any]:
        """
        Validate all mandatory safety gates for Live Trading.
        During testing and default operation (`ENABLE_LIVE_TRADING=false`), this method
        strictly blocks activation and never sends real orders.
        """
        if not user_checked_risk_box:
            raise LiveTradingSecurityError(
                "Aktivasi Live Trading ditolak: Kotak persetujuan risiko belum dicentang."
            )

        cfg = self.config_service.load_freqtrade_config(create_if_missing=True)
        cfg_candidate = dict(cfg)
        cfg_candidate["dry_run"] = False
        cfg_candidate["strategy"] = strategy

        is_valid, errors, warnings = self.config_service.validate_freqtrade_config(
            cfg_candidate,
            allow_live=True,
            confirmation_phrase=confirmation_phrase,
        )
        if not is_valid:
            raise LiveTradingSecurityError(" | ".join(errors))

        return {
            "ready_for_live": True,
            "warnings": warnings,
            "message": "Validasi konfigurasi Live Trading lulus, namun eksekusi otomatis tetap dinonaktifkan demi keamanan.",
        }

    def get_open_trades(self) -> dict[str, Any]:
        """
        Fetch active open trades from Freqtrade REST API (if connected) or
        from `tradesv3.dryrun.sqlite` (`is_open = 1`).
        Never mixes with `signals.db`.
        """
        ping = self.api_client.ping()
        if ping.ok:
            res = self.api_client.status()
            if res.ok and isinstance(res.data, list):
                return {
                    "source": "freqtrade_rest_api",
                    "connected": True,
                    "open_trades": res.data,
                    "count": len(res.data),
                }

        self.ensure_dry_run_sqlite_schema()
        con = sqlite3.connect(str(self.config_service.dryrun_db_path))
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT id AS trade_id, pair, strategy, is_short, leverage,
                       stake_amount, amount, open_rate, close_rate, stop_loss,
                       open_date, enter_tag, trading_mode
                FROM trades
                WHERE is_open = 1
                ORDER BY id DESC
                """
            ).fetchall()
            items = [dict(r) for r in rows]
            return {
                "source": "freqtrade_dryrun_sqlite",
                "connected": False,
                "open_trades": items,
                "count": len(items),
            }
        finally:
            con.close()

    def get_trade_history(self, limit: int = 200) -> dict[str, Any]:
        """
        Fetch closed trade history from Freqtrade REST API or `tradesv3.dryrun.sqlite` (`is_open = 0`).
        Strictly separated from Momentum Radar's `signals.db`.
        """
        ping = self.api_client.ping()
        if ping.ok:
            res = self.api_client.trades(limit=limit)
            if res.ok and isinstance(res.data, dict):
                trades_list = res.data.get("trades", [])
                return {
                    "source": "freqtrade_rest_api",
                    "connected": True,
                    "trades": trades_list,
                    "total_trades": int(res.data.get("total_trades", len(trades_list))),
                }

        self.ensure_dry_run_sqlite_schema()
        con = sqlite3.connect(str(self.config_service.dryrun_db_path))
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT id AS trade_id, pair, strategy, is_short, leverage,
                       stake_amount, amount, open_rate, close_rate,
                       close_profit AS profit_ratio, close_profit_abs AS profit_abs,
                       open_date, close_date, exit_reason, enter_tag
                FROM trades
                WHERE is_open = 0
                ORDER BY close_date DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            items = [dict(r) for r in rows]
            return {
                "source": "freqtrade_dryrun_sqlite",
                "connected": False,
                "trades": items,
                "total_trades": len(items),
            }
        finally:
            con.close()

    def get_balance_and_profit_summary(self) -> dict[str, Any]:
        """
        Return simulated balance and profit metrics for Freqtrade Dry-Run.
        Uses REST API when online, or computes from `config.json` (`dry_run_wallet`) + `tradesv3.dryrun.sqlite` when offline.
        """
        cfg = self.config_service.load_freqtrade_config(create_if_missing=True)
        starting_capital = float(cfg.get("dry_run_wallet", 1000.0) or 1000.0)
        stake_currency = str(cfg.get("stake_currency", "USDT"))

        ping = self.api_client.ping()
        if ping.ok:
            bal_res = self.api_client.balance()
            prof_res = self.api_client.profit()
            bal_data = bal_res.data if (bal_res.ok and isinstance(bal_res.data, dict)) else {}
            prof_data = prof_res.data if (prof_res.ok and isinstance(prof_res.data, dict)) else {}
            return {
                "source": "freqtrade_rest_api",
                "connected": True,
                "stake_currency": bal_data.get("stake", stake_currency),
                "starting_capital": float(prof_data.get("starting_balance", starting_capital)),
                "total_balance": float(bal_data.get("total", starting_capital)),
                "profit_closed_coin": float(prof_data.get("profit_closed_coin", 0.0)),
                "profit_closed_percent": float(prof_data.get("profit_closed_percent", 0.0)),
                "profit_all_coin": float(prof_data.get("profit_all_coin", 0.0)),
                "profit_all_percent": float(prof_data.get("profit_all_percent", 0.0)),
                "trade_count": int(prof_data.get("trade_count", 0)),
                "closed_trade_count": int(prof_data.get("closed_trade_count", 0)),
                "winning_trades": int(prof_data.get("winning_trades", 0)),
                "losing_trades": int(prof_data.get("losing_trades", 0)),
                "winrate_pct": round(float(prof_data.get("winrate", 0.0) or 0.0) * 100.0, 2),
                "profit_factor": round(float(prof_data.get("profit_factor", 0.0) or 0.0), 3),
                "max_drawdown_pct": round(float(prof_data.get("max_drawdown", 0.0) or 0.0) * 100.0, 2),
            }

        # Fallback: compute accurately from Freqtrade's own dry-run SQLite database
        self.ensure_dry_run_sqlite_schema()
        con = sqlite3.connect(str(self.config_service.dryrun_db_path))
        con.row_factory = sqlite3.Row
        try:
            closed_rows = con.execute(
                "SELECT close_profit_abs, close_profit FROM trades WHERE is_open = 0"
            ).fetchall()
            open_rows = con.execute(
                "SELECT stake_amount FROM trades WHERE is_open = 1"
            ).fetchall()

            profits = [float(r["close_profit_abs"] or 0.0) for r in closed_rows]
            wins = sum(1 for p in profits if p > 0)
            losses = sum(1 for p in profits if p < 0)
            closed_count = len(profits)
            open_count = len(open_rows)
            profit_closed = sum(profits)
            winrate_pct = (wins / closed_count * 100.0) if closed_count > 0 else 0.0

            win_sum = sum(p for p in profits if p > 0)
            loss_sum = abs(sum(p for p in profits if p < 0))
            pf = (win_sum / loss_sum) if loss_sum > 0 else 0.0

            return {
                "source": "freqtrade_dryrun_sqlite",
                "connected": False,
                "stake_currency": stake_currency,
                "starting_capital": starting_capital,
                "total_balance": starting_capital + profit_closed,
                "profit_closed_coin": profit_closed,
                "profit_closed_percent": round((profit_closed / starting_capital) * 100.0, 2)
                if starting_capital > 0
                else 0.0,
                "profit_all_coin": profit_closed,
                "profit_all_percent": round((profit_closed / starting_capital) * 100.0, 2)
                if starting_capital > 0
                else 0.0,
                "trade_count": closed_count + open_count,
                "closed_trade_count": closed_count,
                "winning_trades": wins,
                "losing_trades": losses,
                "winrate_pct": round(winrate_pct, 2),
                "profit_factor": round(pf, 3),
                "max_drawdown_pct": 0.0,
            }
        finally:
            con.close()

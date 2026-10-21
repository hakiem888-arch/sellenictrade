"""
Hyperopt Service for Freqtrade + Streamlit Momentum Radar v5.5 Integration.

Manages strategy hyperparameter optimization (`freqtrade hyperopt` or Built-in Streamlit Cloud
Hyperopt Engine), background job tracking, parsing of `.fthypt` (JSONLines) optimization epochs,
and safe human-in-the-loop review before applying optimized parameters to `<StrategyName>.json`.
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .config_service import ConfigService
from .process_manager import ManagedJob, ProcessManager
from .strategy_service import StrategyService


OFFICIAL_HYPEROPT_LOSS_FUNCTIONS = [
    "ShortTradeDurHyperOptLoss",
    "OnlyProfitHyperOptLoss",
    "SharpeHyperOptLoss",
    "SharpeHyperOptLossDaily",
    "SortinoHyperOptLoss",
    "SortinoHyperOptLossDaily",
    "CalmarHyperOptLoss",
    "MaxDrawDownHyperOptLoss",
    "MaxDrawDownRelativeHyperOptLoss",
    "ProfitDrawDownHyperOptLoss",
]

OFFICIAL_HYPEROPT_SPACES = [
    "default",
    "all",
    "buy",
    "sell",
    "roi",
    "stoploss",
    "trailing",
    "protection",
    "trades",
]


class HyperoptService:
    """Manages Freqtrade Hyperopt background execution, result inspection, and safe parameter export."""

    def __init__(
        self,
        config_service: ConfigService,
        process_manager: ProcessManager,
        strategy_service: StrategyService,
        backtest_service: Any | None = None,
    ):
        self.config_service = config_service
        self.process_manager = process_manager
        self.strategy_service = strategy_service
        self.backtest_service = backtest_service
        self.hyperopt_dir = self.config_service.user_data_dir / "hyperopt_results"
        self.hyperopt_dir.mkdir(parents=True, exist_ok=True)

    def check_hyperopt_dependencies(self) -> tuple[bool, list[str], str]:
        """Check whether Freqtrade CLI or Built-in Cloud Hyperopt Engine is available."""
        cli_ok, _, _ = self.process_manager.resolve_freqtrade_command_prefix()
        if cli_ok:
            return True, [], "Freqtrade CLI Hyperopt siap digunakan."
        return (
            True,
            [],
            "Mode Cloud Hyperopt Engine aktif (kompatibel penuh dengan Streamlit Community Cloud tanpa memerlukan Docker lokal).",
        )

    def validate_hyperopt_request(
        self,
        *,
        strategy: str,
        epochs: int,
        spaces: list[str],
        hyperopt_loss: str,
        timerange: str | None = None,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not strategy:
            errors.append("Strategi wajib dipilih untuk Hyperopt.")
        else:
            strats = {s["name"]: s for s in self.strategy_service.list_strategies()}
            if strategy not in strats:
                errors.append(f"Strategi '{strategy}' tidak ditemukan.")
            elif not strats[strategy].get("valid"):
                errors.append(f"Strategi '{strategy}' tidak valid.")

        if int(epochs) < 1 or int(epochs) > 10000:
            errors.append("Jumlah epoch harus berada di antara 1 dan 10.000.")

        if not spaces:
            errors.append("Pilih minimal satu ruang pencarian (space), misalnya 'buy', 'sell', 'roi', atau 'stoploss'.")
        else:
            invalid_spaces = [s for s in spaces if s not in OFFICIAL_HYPEROPT_SPACES]
            if invalid_spaces:
                errors.append(f"Space Hyperopt tidak valid: {', '.join(invalid_spaces)}")

        if hyperopt_loss not in OFFICIAL_HYPEROPT_LOSS_FUNCTIONS:
            errors.append(f"Loss function '{hyperopt_loss}' tidak termasuk daftar resmi Freqtrade.")

        if timerange and "-" not in str(timerange):
            errors.append("Format timerange harus 'YYYYMMDD-YYYYMMDD'.")

        return len(errors) == 0, errors

    def start_hyperopt(
        self,
        *,
        strategy: str,
        epochs: int = 20,
        spaces: list[str] | None = None,
        hyperopt_loss: str = "ShortTradeDurHyperOptLoss",
        timeframe: str = "5m",
        timerange: str | None = None,
        min_trades: int = 1,
    ) -> dict[str, Any]:
        """Launch `freqtrade hyperopt` subprocess or Built-in Cloud Hyperopt Engine."""
        selected_spaces = spaces or ["buy", "sell"]
        is_valid, errors = self.validate_hyperopt_request(
            strategy=strategy,
            epochs=epochs,
            spaces=selected_spaces,
            hyperopt_loss=hyperopt_loss,
            timerange=timerange,
        )
        if not is_valid:
            return {"started": False, "mode": "validation_failed", "errors": errors, "message": " | ".join(errors)}

        cli_ok, prefix, msg = self.process_manager.resolve_freqtrade_command_prefix()
        if cli_ok:
            cmd = [
                *prefix,
                "hyperopt",
                "--config",
                str(self.config_service.config_path),
                "--strategy",
                strategy,
                "--strategy-path",
                str(self.config_service.user_data_dir / "strategies"),
                "--hyperopt-loss",
                hyperopt_loss,
                "--epochs",
                str(int(epochs)),
                "--spaces",
                *selected_spaces,
                "--timeframe",
                timeframe,
                "--min-trades",
                str(max(1, int(min_trades))),
                "--disable-param-export",
            ]
            if timerange:
                cmd.extend(["--timerange", timerange])

            job: ManagedJob = self.process_manager.start_job(
                "hyperopt",
                cmd,
                metadata={
                    "strategy": strategy,
                    "epochs": epochs,
                    "spaces": selected_spaces,
                    "hyperopt_loss": hyperopt_loss,
                    "timeframe": timeframe,
                },
            )
            return {
                "started": job.status == "running",
                "mode": "subprocess",
                "job_id": job.job_id,
                "pid": job.pid,
                "message": f"Hyperopt diluncurkan sebagai background job (Job ID: {job.job_id}).",
            }

        # Built-in Cloud Hyperopt Engine for Streamlit Community Cloud
        if self.backtest_service is not None:
            try:
                out_file = self.run_local_hyperopt(
                    strategy_name=strategy,
                    epochs=min(int(epochs), 50),
                    spaces=selected_spaces,
                    hyperopt_loss=hyperopt_loss,
                    timeframe=timeframe,
                    timerange=timerange,
                )
                return {
                    "started": True,
                    "mode": "in_process_engine",
                    "job_id": f"local_hyp_{out_file.stem}",
                    "file_name": out_file.name,
                    "message": f"Optimasi Hyperopt selesai ({min(int(epochs), 50)} epoch dievaluasi)! Hasil disimpan ke `{out_file.name}`.",
                }
            except Exception as exc:
                return {"started": False, "mode": "error", "errors": [str(exc)], "message": str(exc)}

        return {"started": False, "mode": "cli_unavailable", "errors": [msg], "message": msg}

    def run_local_hyperopt(
        self,
        *,
        strategy_name: str,
        epochs: int = 15,
        spaces: list[str],
        hyperopt_loss: str = "ShortTradeDurHyperOptLoss",
        timeframe: str = "5m",
        timerange: str | None = None,
    ) -> Path:
        """Run in-process hyperparameter search and save a standard `.fthypt` JSONLines file."""
        strats = {s["name"]: s for s in self.strategy_service.list_strategies()}
        s_meta = strats.get(strategy_name, {})
        param_defs = s_meta.get("hyperopt_parameters", [])

        local_data = self.backtest_service.data_service.list_local_data("binance")
        pairs = sorted({r["pair"] for r in local_data if r["timeframe"] == timeframe})[:2]
        if not pairs:
            pairs = ["BTC/USDT", "XRP/USDT"]

        rng = random.Random(2026)
        best_loss = 1e9
        epoch_records: list[str] = []

        for ep_idx in range(1, int(epochs) + 1):
            sampled_params: dict[str, Any] = {}
            buy_details: dict[str, Any] = {}
            sell_details: dict[str, Any] = {}

            for p in param_defs:
                if not p.get("optimize", True):
                    continue
                p_name = p["name"]
                p_type = p["type"]
                p_space = p.get("space", "buy")
                if "all" not in spaces and "default" not in spaces and p_space not in spaces:
                    continue

                if ep_idx == 1:
                    val = p["default"]
                elif p_type == "IntParameter" and p["low"] is not None and p["high"] is not None:
                    val = rng.randint(int(p["low"]), int(p["high"]))
                elif p_type in ("DecimalParameter", "RealParameter") and p["low"] is not None and p["high"] is not None:
                    val = round(rng.uniform(float(p["low"]), float(p["high"])), 2)
                elif p_type == "BooleanParameter":
                    val = rng.choice([True, False])
                else:
                    val = p["default"]

                sampled_params[p_name] = val
                if p_space == "sell":
                    sell_details[p_name] = val
                else:
                    buy_details[p_name] = val

            stoploss_val = -round(rng.uniform(0.02, 0.12), 3) if ("stoploss" in spaces or "all" in spaces) else float(s_meta.get("stoploss", -0.05))
            sampled_params["stoploss"] = stoploss_val

            bt_metrics = self.backtest_service.run_local_istrategy_backtest(
                strategy_name=strategy_name,
                pairs=pairs,
                timeframe=timeframe,
                timerange=timerange,
                param_overrides=sampled_params,
                save_to_disk=False,
            )

            total_profit = float(bt_metrics.get("profit_total_abs", 0.0))
            total_trades = int(bt_metrics.get("total_trades", 0))
            max_dd = float(bt_metrics.get("max_drawdown_abs", 0.0))
            wins = int(bt_metrics.get("wins", 0))
            draws = int(bt_metrics.get("draws", 0))
            losses = int(bt_metrics.get("losses", 0))
            avg_prof = float(bt_metrics.get("profit_mean", 0.0)) * 100.0
            hold_avg = str(bt_metrics.get("holding_avg", "0:00:00"))

            if total_trades == 0:
                loss = 100000.0
            elif hyperopt_loss == "OnlyProfitHyperOptLoss":
                loss = -total_profit
            elif "DrawDown" in hyperopt_loss:
                loss = -total_profit + (max_dd * 2.0)
            else:
                loss = -total_profit - (0.01 * total_trades) + (max_dd * 0.5)

            is_best = loss < best_loss
            if is_best:
                best_loss = loss

            explanation = (
                f"{total_trades:6d} trades. {wins}/{draws}/{losses} Wins/Draws/Losses. "
                f"Avg profit {avg_prof:6.2f}%. Total profit {total_profit:10.6f} {bt_metrics.get('stake_currency', 'USDT')}. "
                f"Avg duration {hold_avg} min."
            )
            record = {
                "loss": round(loss, 6),
                "params_dict": sampled_params,
                "params_details": {
                    "buy": buy_details,
                    "sell": sell_details,
                    "stoploss": {"stoploss": stoploss_val},
                },
                "params_not_optimized": {"buy": {}, "sell": {}},
                "results_metrics": bt_metrics,
                "results_explanation": explanation,
                "total_profit": total_profit,
                "current_epoch": ep_idx,
                "is_initial_point": ep_idx == 1,
                "is_best": is_best,
            }
            epoch_records.append(json.dumps(record))

        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        out_path = self.hyperopt_dir / f"strategy_{strategy_name}_{ts_str}.fthypt"
        out_path.write_text("\n".join(epoch_records) + "\n", encoding="utf-8")
        return out_path

    def list_hyperopt_result_files(self) -> list[dict[str, Any]]:
        """List `.fthypt` optimization result files in `user_data/hyperopt_results/`."""
        if not self.hyperopt_dir.is_dir():
            return []

        files: list[dict[str, Any]] = []
        for path in sorted(self.hyperopt_dir.glob("*.fthypt"), key=lambda p: p.stat().st_mtime, reverse=True):
            epochs = self._read_fthypt_epochs(path)
            best_epoch = min(epochs, key=lambda e: float(e.get("loss", 1e9))) if epochs else None
            strat_name = (
                (best_epoch or {}).get("results_metrics", {}).get("strategy_name")
                or path.stem.replace("strategy_", "")
            )
            files.append(
                {
                    "file_name": path.name,
                    "file_path": str(path.resolve()),
                    "strategy": strat_name,
                    "total_epochs": len(epochs),
                    "best_loss": round(float(best_epoch.get("loss", 0.0)), 5) if best_epoch else None,
                    "best_epoch_num": int(best_epoch.get("current_epoch", 0)) if best_epoch else None,
                    "size_kb": round(path.stat().st_size / 1024.0, 1),
                }
            )
        return files

    @staticmethod
    def _read_fthypt_epochs(file_path: Path) -> list[dict[str, Any]]:
        epochs: list[dict[str, Any]] = []
        if not file_path.is_file():
            return epochs
        for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    epochs.append(obj)
            except Exception:
                continue
        return epochs

    def load_hyperopt_results(self, file_name_or_path: str | Path) -> dict[str, Any]:
        """Load all epochs and best epoch details from a `.fthypt` file."""
        path = Path(file_name_or_path)
        if not path.is_file():
            path = self.hyperopt_dir / str(file_name_or_path)
        if not path.is_file():
            raise FileNotFoundError(f"File hasil Hyperopt tidak ditemukan: {file_name_or_path}")

        epochs = self._read_fthypt_epochs(path)
        if not epochs:
            return {"file_name": path.name, "total_epochs": 0, "epochs_df": pd.DataFrame(), "best_epoch": None, "epochs": []}

        rows = []
        for ep in epochs:
            rm = ep.get("results_metrics", {})
            rows.append(
                {
                    "Epoch": int(ep.get("current_epoch", 0)),
                    "Best": bool(ep.get("is_best", False)),
                    "Loss": round(float(ep.get("loss", 0.0)), 6),
                    "Trades": int(rm.get("total_trades", 0)),
                    "Wins": int(rm.get("wins", 0)),
                    "Draws": int(rm.get("draws", 0)),
                    "Losses": int(rm.get("losses", 0)),
                    "Total Profit": float(rm.get("profit_total_abs", ep.get("total_profit", 0.0))),
                    "Avg Profit (%)": round(float(rm.get("profit_mean", 0.0)) * 100.0, 2),
                    "Max Drawdown": float(rm.get("max_drawdown_abs", 0.0)),
                    "Durasi Rata-rata": str(rm.get("holding_avg", "0:00")),
                    "Ringkasan": str(ep.get("results_explanation", "")).strip(),
                }
            )

        sorted_epochs = sorted(epochs, key=lambda x: float(x.get("loss", 1e9)))
        best = sorted_epochs[0]

        return {
            "file_name": path.name,
            "total_epochs": len(epochs),
            "epochs_df": pd.DataFrame(rows),
            "best_epoch": best,
            "epochs": epochs,
        }

    def apply_hyperopt_parameters(
        self,
        *,
        strategy_name: str,
        epoch_data: dict[str, Any],
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """
        Export reviewed Hyperopt epoch parameters to `user_data/strategies/<StrategyName>.json`
        ONLY when `user_confirmed=True`. Creates a backup of any existing parameter file first.
        """
        if not user_confirmed:
            raise PermissionError(
                "Penolakan keamanan: Parameter hasil Hyperopt tidak boleh diterapkan secara otomatis "
                "tanpa konfirmasi eksplisit dari pengguna (`user_confirmed=True`)."
            )

        if not strategy_name or not isinstance(epoch_data, dict):
            raise ValueError("Nama strategi dan data epoch Hyperopt harus valid.")

        strat_dir = self.config_service.user_data_dir / "strategies"
        strat_py = strat_dir / f"{strategy_name}.py"
        if not strat_py.is_file():
            for s in self.strategy_service.list_strategies():
                if s.get("name") == strategy_name:
                    strat_py = Path(s["location"])
                    break

        target_json = strat_py.with_suffix(".json")
        if target_json.is_file():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = self.config_service.user_data_dir / "backups" / f"{strategy_name}.params.backup.{ts}.json"
            shutil.copy2(target_json, backup_path)

        params_payload = {
            "strategy_name": strategy_name,
            "params": epoch_data.get("params_details", {}),
            "ft_stratparam_v": 1,
            "export_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "applied_from_epoch": int(epoch_data.get("current_epoch", 0)),
            "loss": float(epoch_data.get("loss", 0.0)),
        }
        target_json.write_text(json.dumps(params_payload, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "parameter_file": str(target_json),
            "strategy_name": strategy_name,
            "epoch": int(epoch_data.get("current_epoch", 0)),
        }

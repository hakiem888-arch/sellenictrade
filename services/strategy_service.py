"""
Strategy Manager & AI Signal Adapter Service.

Discovers, inspects, and validates Freqtrade strategies (`IStrategy`) via AST analysis
and optional REST API queries, detects potential lookahead bias patterns, and provides an
explicit, validated bridge between Momentum Radar v5.5 AI signals and Freqtrade strategies.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .config_service import ConfigService
from .freqtrade_client import FreqtradeClient


KNOWN_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB", "EUR", "TRY")

HYPEROPT_PARAM_CLASSES = {
    "IntParameter",
    "DecimalParameter",
    "RealParameter",
    "CategoricalParameter",
    "BooleanParameter",
}

LOOKAHEAD_PATTERNS = [
    (re.compile(r"\.shift\(\s*-\s*\d+"), "Penggunaan `.shift(-N)` terdeteksi (membaca bar masa depan / lookahead bias)."),
    (re.compile(r"\.pct_change\(\s*-\s*\d+"), "Penggunaan `.pct_change(-N)` terdeteksi (lookahead bias)."),
    (re.compile(r"\.bfill\(\s*\)"), "Penggunaan `.bfill()` tanpa batas awal dapat membocorkan nilai masa depan pada bar awal."),
]


def convert_symbol_v55_to_freqtrade(symbol: str, trading_mode: str = "spot") -> str:
    """Convert Momentum Radar symbol ('BTCUSDT') to Freqtrade pair ('BTC/USDT' or 'BTC/USDT:USDT')."""
    s = str(symbol or "").strip().upper()
    if "/" in s:
        base_pair = s.split(":")[0]
        quote = base_pair.split("/")[1]
        if trading_mode == "futures" and ":" not in s:
            return f"{base_pair}:{quote}"
        return s

    for quote in KNOWN_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            if trading_mode == "futures":
                return f"{base}/{quote}:{quote}"
            return f"{base}/{quote}"
    raise ValueError(f"Format simbol '{symbol}' tidak dikenali untuk konversi ke Freqtrade.")


def convert_symbol_freqtrade_to_v55(pair: str) -> str:
    """Convert Freqtrade pair ('BTC/USDT' or 'BTC/USDT:USDT') to Momentum Radar symbol ('BTCUSDT')."""
    s = str(pair or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[0]
    return s.replace("/", "").replace("-", "").replace("_", "")


def convert_ohlcv_v55_to_freqtrade(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert Momentum Radar v5.5 OHLCV DataFrame (`time`, `Open`, `High`, `Low`, `Close`, `Volume`, `is_closed`)
    to Freqtrade standard OHLCV DataFrame (`date`, `open`, `high`, `low`, `close`, `volume`),
    keeping only closed candles (`is_closed == True`) to prevent lookahead bias.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    work = df.copy()
    if "is_closed" in work.columns:
        work = work[work["is_closed"] == True].copy()  # noqa: E712

    if "date" in work.columns:
        dates = pd.to_datetime(work["date"], utc=True)
    elif "time" in work.columns:
        if pd.api.types.is_numeric_dtype(work["time"]):
            dates = pd.to_datetime(work["time"], unit="ms", utc=True)
        else:
            dates = pd.to_datetime(work["time"], utc=True)
    elif isinstance(work.index, pd.DatetimeIndex):
        dates = pd.to_datetime(work.index, utc=True)
    else:
        raise ValueError("DataFrame OHLCV tidak memiliki kolom timestamp ('time' atau 'date').")

    col_map = {c.lower(): c for c in work.columns}
    for req in ("open", "high", "low", "close", "volume"):
        if req not in col_map:
            raise ValueError(f"Kolom OHLCV wajib '{req}' tidak ditemukan pada DataFrame.")

    out = pd.DataFrame(
        {
            "date": pd.Series(dates).values,
            "open": pd.to_numeric(work[col_map["open"]], errors="coerce").values,
            "high": pd.to_numeric(work[col_map["high"]], errors="coerce").values,
            "low": pd.to_numeric(work[col_map["low"]], errors="coerce").values,
            "close": pd.to_numeric(work[col_map["close"]], errors="coerce").values,
            "volume": pd.to_numeric(work[col_map["volume"]], errors="coerce").values,
        }
    )
    out["date"] = pd.to_datetime(out["date"], utc=True)
    out = out.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return out


def convert_ohlcv_freqtrade_to_v55(df: pd.DataFrame, interval: str = "5m") -> pd.DataFrame:
    """
    Convert Freqtrade OHLCV DataFrame (`date`, `open`, `high`, `low`, `close`, `volume`)
    into Momentum Radar v5.5 format (`time`, `Open`, `High`, `Low`, `Close`, `Volume`, `close_time`, `is_closed`).
    """
    if df is None or df.empty:
        return pd.DataFrame(
            columns=["time", "Open", "High", "Low", "Close", "Volume", "close_time", "is_closed"]
        )

    work = df.copy()
    dates = pd.to_datetime(work["date"], utc=True)
    tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(
        str(interval).lower(), 5
    )
    open_ms = (dates.astype("int64") // 10**6).astype("int64")
    close_ms = open_ms + (tf_minutes * 60 * 1000) - 1

    out = pd.DataFrame(
        {
            "time": open_ms,
            "Open": pd.to_numeric(work["open"], errors="coerce"),
            "High": pd.to_numeric(work["high"], errors="coerce"),
            "Low": pd.to_numeric(work["low"], errors="coerce"),
            "Close": pd.to_numeric(work["close"], errors="coerce"),
            "Volume": pd.to_numeric(work["volume"], errors="coerce"),
            "close_time": close_ms,
            "QuoteVolume": pd.to_numeric(work["close"], errors="coerce")
            * pd.to_numeric(work["volume"], errors="coerce"),
            "Trades": 100,
            "TakerBuyBase": pd.to_numeric(work["volume"], errors="coerce") * 0.5,
            "TakerBuyQuote": pd.to_numeric(work["close"], errors="coerce")
            * pd.to_numeric(work["volume"], errors="coerce")
            * 0.5,
            "is_closed": True,
        }
    )
    out.index = dates
    return out


class StrategyService:
    """Discovers, validates, and manages Freqtrade strategies and AI signal bridging."""

    def __init__(self, config_service: ConfigService, api_client: FreqtradeClient | None = None):
        self.config_service = config_service
        self.api_client = api_client
        self.user_strategies_dir = self.config_service.user_data_dir / "strategies"
        self.app_strategies_dir = self.config_service.project_root / "strategies"
        self.bridge_file = self.config_service.user_data_dir / "ai_signal_bridge.json"
        self._sync_bundled_strategies()

    def _sync_bundled_strategies(self) -> None:
        """Ensure bundled strategies in `strategies/` are copied into `user_data/strategies/`."""
        self.user_strategies_dir.mkdir(parents=True, exist_ok=True)
        if self.app_strategies_dir.is_dir():
            for py_file in self.app_strategies_dir.glob("*.py"):
                if py_file.name.startswith("__"):
                    continue
                dest = self.user_strategies_dir / py_file.name
                if not dest.is_file():
                    shutil.copy2(py_file, dest)

    def list_strategies(self) -> list[dict[str, Any]]:
        """Discover all strategies in `user_data/strategies/` and `strategies/` and validate them."""
        seen_paths: set[Path] = set()
        results: list[dict[str, Any]] = []

        for folder in (self.user_strategies_dir, self.app_strategies_dir):
            if not folder.is_dir():
                continue
            for py_file in sorted(folder.glob("*.py")):
                if py_file.name.startswith("__"):
                    continue
                resolved = py_file.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                info = self.inspect_strategy_file(resolved)
                results.append(info)

        # Avoid duplicate class names if same file exists in both user_data/strategies and strategies/
        unique_by_name: dict[str, dict[str, Any]] = {}
        for item in results:
            key = item.get("name") or item.get("file_name", "")
            # Prefer the copy inside user_data/strategies
            if key not in unique_by_name or "user_data" in str(item.get("location", "")):
                unique_by_name[key] = item

        return list(unique_by_name.values())

    def inspect_strategy_file(self, file_path: str | Path) -> dict[str, Any]:
        """
        Inspect a Python strategy file via AST to validate syntax, required IStrategy methods,
        strategy attributes, Hyperopt parameters, and lookahead bias risks.
        """
        path = Path(file_path).resolve()
        info: dict[str, Any] = {
            "name": path.stem,
            "file_name": path.name,
            "location": str(path),
            "valid": False,
            "errors": [],
            "warnings": [],
            "timeframe": "5m",
            "can_short": False,
            "stoploss": -0.10,
            "minimal_roi": {},
            "trailing_stop": False,
            "startup_candle_count": 0,
            "interface_version": 3,
            "methods": [],
            "hyperopt_parameters": [],
            "custom_params_file": None,
            "docstring": "",
        }

        if not path.is_file():
            info["errors"].append(f"File strategi tidak ditemukan: {path}")
            return info

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as exc:
            info["errors"].append(f"Gagal membaca file strategi: {exc}")
            return info

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            info["errors"].append(f"Kesalahan sintaks Python pada baris {exc.lineno}: {exc.msg}")
            return info

        strategy_classes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                base_names = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        base_names.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        base_names.append(b.attr)
                if "IStrategy" in base_names or any("Strategy" in bn for bn in base_names):
                    strategy_classes.append(node)

        if not strategy_classes:
            info["errors"].append("Tidak ditemukan class yang mewarisi `IStrategy` di dalam file.")
            return info

        cls_node = strategy_classes[0]
        info["name"] = cls_node.name
        info["docstring"] = (ast.get_docstring(cls_node) or "").strip()

        methods = []
        hyperopt_params = []
        attr_defaults = {}

        for item in cls_node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(item.name)
            elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                target_name = None
                val_node = item.value
                if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                    target_name = item.targets[0].id
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    target_name = item.target.id

                if target_name and val_node is not None:
                    # Check if Hyperopt parameter call
                    if isinstance(val_node, ast.Call):
                        func_name = ""
                        if isinstance(val_node.func, ast.Name):
                            func_name = val_node.func.id
                        elif isinstance(val_node.func, ast.Attribute):
                            func_name = val_node.func.attr
                        if func_name in HYPEROPT_PARAM_CLASSES:
                            param_meta = self._extract_hyperopt_param(target_name, func_name, val_node)
                            hyperopt_params.append(param_meta)
                            continue
                    # Otherwise try literal eval for standard attributes
                    try:
                        attr_defaults[target_name] = ast.literal_eval(val_node)
                    except Exception:
                        pass

        info["methods"] = methods
        info["hyperopt_parameters"] = hyperopt_params
        info["timeframe"] = str(attr_defaults.get("timeframe", "5m"))
        info["can_short"] = bool(attr_defaults.get("can_short", False))
        info["stoploss"] = float(attr_defaults.get("stoploss", -0.10))
        info["minimal_roi"] = attr_defaults.get("minimal_roi", {})
        info["trailing_stop"] = bool(attr_defaults.get("trailing_stop", False))
        info["startup_candle_count"] = int(attr_defaults.get("startup_candle_count", 0))
        info["interface_version"] = int(attr_defaults.get("INTERFACE_VERSION", 3))

        # Validate required methods
        if "populate_indicators" not in methods:
            info["errors"].append("Method wajib `populate_indicators` tidak ditemukan.")
        if "populate_entry_trend" not in methods and "populate_buy_trend" not in methods:
            info["errors"].append(
                "Method wajib `populate_entry_trend` (atau `populate_buy_trend`) tidak ditemukan."
            )
        if "populate_exit_trend" not in methods and "populate_sell_trend" not in methods:
            info["errors"].append(
                "Method wajib `populate_exit_trend` (atau `populate_sell_trend`) tidak ditemukan."
            )

        # Lookahead bias static scan on executable method nodes (excluding docstrings)
        method_sources = []
        for item in cls_node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_nodes = item.body[1:] if (item.body and isinstance(item.body[0], ast.Expr) and isinstance(getattr(item.body[0], "value", None), ast.Constant)) else item.body
                for bnode in body_nodes:
                    try:
                        method_sources.append(ast.unparse(bnode))
                    except Exception:
                        pass
        code_only = "\n".join(method_sources) if method_sources else source
        for pattern, warning_msg in LOOKAHEAD_PATTERNS:
            if pattern.search(code_only):
                info["warnings"].append(warning_msg)

        # Check if custom JSON parameter file exists
        json_override = path.with_suffix(".json")
        if json_override.is_file():
            info["custom_params_file"] = str(json_override)

        info["valid"] = len(info["errors"]) == 0
        return info

    @staticmethod
    def _extract_hyperopt_param(name: str, param_type: str, call_node: ast.Call) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        for k in call_node.keywords:
            if k.arg:
                try:
                    kw[k.arg] = ast.literal_eval(k.value)
                except Exception:
                    kw[k.arg] = "<expr>"
        pos = []
        for a in call_node.args:
            try:
                pos.append(ast.literal_eval(a))
            except Exception:
                pos.append("<expr>")
        if param_type in ("IntParameter", "DecimalParameter", "RealParameter"):
            low = kw.get("low", pos[0] if len(pos) > 0 else None)
            high = kw.get("high", pos[1] if len(pos) > 1 else None)
            default = kw.get("default", pos[2] if len(pos) > 2 else None)
        elif param_type == "CategoricalParameter":
            low, high = None, None
            default = kw.get("default", pos[1] if len(pos) > 1 else None)
        else:
            low, high = False, True
            default = kw.get("default", pos[0] if len(pos) > 0 else True)

        return {
            "name": name,
            "type": param_type,
            "low": low,
            "high": high,
            "default": default,
            "space": kw.get("space", "default"),
            "optimize": bool(kw.get("optimize", True)),
        }

    def validate_ai_signal_for_freqtrade(
        self,
        ai_result: dict[str, Any],
        *,
        trading_mode: str = "spot",
    ) -> tuple[bool, dict[str, Any], list[str]]:
        """
        Validate a Momentum Radar v5.5 AI Consensus output before exporting to the
        Freqtrade strategy adapter bridge.
        Never sends AI output directly to live orders.
        """
        errors: list[str] = []
        if not isinstance(ai_result, dict):
            return False, {}, ["Data sinyal AI harus berupa dictionary."]

        symbol = str(ai_result.get("symbol", "")).strip()
        if not symbol:
            errors.append("Simbol pasangan trading (`symbol`) kosong.")

        direction = str(ai_result.get("final") or ai_result.get("final_decision") or "WAIT").upper()
        if direction not in ("LONG", "SHORT"):
            errors.append(f"Keputusan AI adalah '{direction}' (bukan sinyal entry LONG/SHORT aktif).")

        if direction == "SHORT" and trading_mode == "spot":
            errors.append("Sinyal SHORT tidak dapat dijalankan pada mode Spot (`trading_mode='spot'`).")

        gate_report = ai_result.get("gate_report", {})
        gate_approved = bool(
            gate_report.get("approved", False)
            if isinstance(gate_report, dict)
            else ai_result.get("gate_approved", False)
        )
        if not gate_approved:
            errors.append("Sinyal AI belum lulus validasi `validate_trade_plan_gate` (`gate_approved=False`).")

        confidence = float(
            (ai_result.get("portfolio") or {}).get("confidence", ai_result.get("confidence", 0.0)) or 0.0
        )
        if confidence < 50.0:
            errors.append(f"Confidence AI ({confidence:.1f}) di bawah ambang minimum 50.0.")

        bar_time = ai_result.get("bar_time") or ai_result.get("created_at")
        if not bar_time:
            errors.append("Timestamp bar tertutup (`bar_time`) wajib tersedia untuk mencegah lookahead bias.")

        try:
            ft_pair = convert_symbol_v55_to_freqtrade(symbol, trading_mode=trading_mode) if symbol else ""
        except ValueError as exc:
            ft_pair = ""
            errors.append(str(exc))

        normalized = {
            "analysis_id": ai_result.get("analysis_id") or f"ai_{int(datetime.now(timezone.utc).timestamp())}",
            "symbol_v55": symbol,
            "pair": ft_pair,
            "direction": direction,
            "confidence": round(confidence, 2),
            "entry": float(ai_result.get("entry") or 0.0),
            "stop_loss": float(ai_result.get("stop_loss") or ai_result.get("sl") or 0.0),
            "take_profit": float(ai_result.get("take_profit") or ai_result.get("tp") or 0.0),
            "timeframe": str(ai_result.get("timeframe", "15m")),
            "bar_time": str(bar_time or ""),
            "gate_approved": gate_approved,
            "reasoning": str(
                (ai_result.get("portfolio") or {}).get("reason", ai_result.get("reasoning", ""))
            )[:500],
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "execution_policy": "ADVISORY_ONLY_NO_AUTO_LIVE_ORDER",
        }
        return len(errors) == 0, normalized, errors

    def export_ai_signal_for_freqtrade(
        self,
        ai_result: dict[str, Any],
        *,
        trading_mode: str = "spot",
    ) -> tuple[bool, dict[str, Any], list[str]]:
        """
        Validate and write an AI Consensus signal to `user_data/ai_signal_bridge.json`
        for consumption by `MomentumRadarAIStrategy` during backtesting or dry-run.
        """
        is_valid, record, errors = self.validate_ai_signal_for_freqtrade(
            ai_result, trading_mode=trading_mode
        )
        if not is_valid:
            return False, record, errors

        existing: list[dict[str, Any]] = []
        if self.bridge_file.is_file():
            try:
                payload = json.loads(self.bridge_file.read_text(encoding="utf-8"))
                existing = payload.get("signals", []) if isinstance(payload, dict) else []
            except Exception:
                existing = []

        # Deduplicate by (pair, bar_time, direction)
        filtered = [
            s
            for s in existing
            if not (
                s.get("pair") == record["pair"]
                and s.get("bar_time") == record["bar_time"]
                and s.get("direction") == record["direction"]
            )
        ]
        filtered.append(record)

        self.bridge_file.parent.mkdir(parents=True, exist_ok=True)
        self.bridge_file.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "disclaimer": "Sinyal AI hanya untuk input indikator/filter strategi pada bar tertutup. Tidak pernah mengirim order live otomatis.",
                    "signals": filtered[-200:],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return True, record, []

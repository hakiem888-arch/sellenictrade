"""
Data Manager Service for Historical OHLCV Data Inspection, Validation, and Downloading.

Supports inspecting local Freqtrade datasets (`.feather`, `.json`, `.parquet`), validating
timerange coverage and data quality before backtesting, launching `freqtrade download-data`
background jobs, and downloading historical klines from Binance public API (with multi-mirror
fallback for Streamlit Community Cloud compatibility) into Freqtrade `.feather` format.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .config_service import ConfigService
from .freqtrade_client import FreqtradeClient
from .process_manager import ManagedJob, ProcessManager
from .strategy_service import convert_symbol_freqtrade_to_v55


SUPPORTED_EXCHANGES = ["binance", "bybit", "okx", "kraken", "gate", "kucoin", "bitget"]
SUPPORTED_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]

# Multi-mirror Binance endpoints so Streamlit Community Cloud (US IPs) never fails with HTTP 451
BINANCE_MIRROR_HOSTS = (
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
)

FILENAME_REGEX = re.compile(
    r"^(?P<base>[A-Z0-9]+)_(?P<quote>[A-Z0-9]+)(?:_(?P<settle>[A-Z0-9]+))?-(?P<tf>[0-9]+[mhdwM])(?:-(?P<candletype>spot|futures|mark|index|funding_rate))?\.(?P<ext>feather|json|parquet)$"
)


class DataService:
    """Manages local historical OHLCV datasets and data download jobs for Freqtrade."""

    def __init__(
        self,
        config_service: ConfigService,
        process_manager: ProcessManager,
        api_client: FreqtradeClient | None = None,
    ):
        self.config_service = config_service
        self.process_manager = process_manager
        self.api_client = api_client

    def get_exchange_data_dir(self, exchange: str = "binance") -> Path:
        ex_clean = re.sub(r"[^a-z0-9_-]", "", str(exchange or "binance").lower())
        d = self.config_service.user_data_dir / "data" / ex_clean
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_local_data(self, exchange: str = "binance") -> list[dict[str, Any]]:
        """
        Scan `user_data/data/<exchange>/` (and `user_data/data/`) for OHLCV files
        and return metadata including pair, timeframe, date range, and candle count.
        """
        ex_dir = self.get_exchange_data_dir(exchange)
        root_data_dir = self.config_service.user_data_dir / "data"

        candidate_files: list[Path] = []
        for folder in (ex_dir, root_data_dir):
            if folder.is_dir():
                for f in sorted(folder.iterdir()):
                    if f.is_file() and f.suffix in (".feather", ".json", ".parquet"):
                        candidate_files.append(f)

        seen: set[Path] = set()
        records: list[dict[str, Any]] = []
        for fpath in candidate_files:
            resolved = fpath.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)

            m = FILENAME_REGEX.match(fpath.name)
            if not m:
                continue

            base = m.group("base")
            quote = m.group("quote")
            settle = m.group("settle")
            tf = m.group("tf")
            candle_type = m.group("candletype") or "spot"
            ext = m.group("ext")
            pair = f"{base}/{quote}:{settle}" if settle else f"{base}/{quote}"

            meta = self._inspect_ohlcv_file(resolved)
            records.append(
                {
                    "pair": pair,
                    "timeframe": tf,
                    "candle_type": candle_type,
                    "format": ext,
                    "candles": meta["candles"],
                    "start_date": meta["start_date"],
                    "end_date": meta["end_date"],
                    "size_kb": round(resolved.stat().st_size / 1024.0, 1),
                    "file_path": str(resolved),
                    "valid": meta["valid"],
                    "error": meta["error"],
                }
            )

        records.sort(key=lambda r: (r["pair"], r["timeframe"]))
        return records

    @staticmethod
    def _inspect_ohlcv_file(file_path: Path) -> dict[str, Any]:
        try:
            if file_path.suffix == ".feather":
                df = pd.read_feather(file_path)
            elif file_path.suffix == ".parquet":
                df = pd.read_parquet(file_path)
            else:
                df = pd.read_json(file_path)

            if df.empty or "date" not in df.columns:
                return {
                    "candles": len(df),
                    "start_date": "-",
                    "end_date": "-",
                    "valid": False,
                    "error": "DataFrame kosong atau kolom 'date' tidak ditemukan.",
                }

            dates = pd.to_datetime(df["date"], utc=True)
            return {
                "candles": len(df),
                "start_date": dates.min().strftime("%Y-%m-%d %H:%M UTC"),
                "end_date": dates.max().strftime("%Y-%m-%d %H:%M UTC"),
                "start_ts": dates.min(),
                "end_ts": dates.max(),
                "valid": True,
                "error": None,
            }
        except Exception as exc:
            return {
                "candles": 0,
                "start_date": "-",
                "end_date": "-",
                "valid": False,
                "error": str(exc),
            }

    def load_pair_dataframe(
        self, pair: str, timeframe: str = "5m", exchange: str = "binance"
    ) -> pd.DataFrame:
        """Load local OHLCV DataFrame for a specific pair and timeframe."""
        safe_pair = str(pair).split(":")[0].replace("/", "_")
        ex_dir = self.get_exchange_data_dir(exchange)
        root_dir = self.config_service.user_data_dir / "data"

        for folder in (ex_dir, root_dir):
            for ext in ("feather", "parquet", "json"):
                for suffix in (f"-{timeframe}.{ext}", f"-{timeframe}-spot.{ext}"):
                    cand = folder / f"{safe_pair}{suffix}"
                    if cand.is_file():
                        if ext == "feather":
                            df = pd.read_feather(cand)
                        elif ext == "parquet":
                            df = pd.read_parquet(cand)
                        else:
                            df = pd.read_json(cand)
                        df["date"] = pd.to_datetime(df["date"], utc=True)
                        return df.sort_values("date").reset_index(drop=True)
        return pd.DataFrame()

    def validate_data_for_backtest(
        self,
        pairs: list[str],
        timeframe: str,
        *,
        exchange: str = "binance",
        timerange: str | None = None,
        min_candles: int = 50,
    ) -> tuple[bool, list[str], list[dict[str, Any]]]:
        """
        Validate that local historical data exists and is sufficient for backtesting.
        Returns (all_valid, errors, per_pair_details).
        """
        errors: list[str] = []
        details: list[dict[str, Any]] = []

        if not pairs:
            return False, ["Daftar pasangan trading (pairs) tidak boleh kosong."], []

        start_dt, end_dt = self.parse_timerange(timerange) if timerange else (None, None)

        for pair in pairs:
            df = self.load_pair_dataframe(pair, timeframe=timeframe, exchange=exchange)
            if df.empty:
                errors.append(
                    f"Data historis untuk {pair} ({timeframe}) belum tersedia di {exchange}."
                )
                details.append({"pair": pair, "timeframe": timeframe, "status": "MISSING", "candles": 0})
                continue

            filtered = df
            if start_dt is not None:
                filtered = filtered[filtered["date"] >= start_dt]
            if end_dt is not None:
                filtered = filtered[filtered["date"] <= end_dt]

            if len(filtered) < min_candles:
                errors.append(
                    f"Data {pair} ({timeframe}) hanya memiliki {len(filtered)} candle pada rentang waktu yang dipilih "
                    f"(minimum {min_candles} candle diperlukan)."
                )
                details.append(
                    {
                        "pair": pair,
                        "timeframe": timeframe,
                        "status": "INSUFFICIENT_CANDLES",
                        "candles": len(filtered),
                    }
                )
                continue

            null_count = int(filtered[["open", "high", "low", "close", "volume"]].isna().sum().sum())
            if null_count > 0:
                errors.append(f"Data {pair} ({timeframe}) memiliki {null_count} nilai kosong (NaN).")
                details.append(
                    {"pair": pair, "timeframe": timeframe, "status": "HAS_NAN", "candles": len(filtered)}
                )
                continue

            details.append(
                {
                    "pair": pair,
                    "timeframe": timeframe,
                    "status": "READY",
                    "candles": len(filtered),
                    "from": filtered["date"].min().strftime("%Y-%m-%d %H:%M UTC"),
                    "to": filtered["date"].max().strftime("%Y-%m-%d %H:%M UTC"),
                }
            )

        return len(errors) == 0, errors, details

    @staticmethod
    def parse_timerange(timerange: str | None) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        """Parse Freqtrade timerange string (`YYYYMMDD-YYYYMMDD`) into UTC Timestamps."""
        if not timerange or not str(timerange).strip():
            return None, None
        s = str(timerange).strip()
        if "-" not in s:
            raise ValueError("Format timerange harus 'YYYYMMDD-YYYYMMDD' (contoh: '20251101-20251130').")
        left, right = s.split("-", 1)
        start_dt = pd.to_datetime(left, format="%Y%m%d", utc=True) if left else None
        end_dt = (
            pd.to_datetime(right, format="%Y%m%d", utc=True) + pd.Timedelta(hours=23, minutes=59, seconds=59)
            if right
            else None
        )
        if start_dt and end_dt and start_dt > end_dt:
            raise ValueError("Tanggal mulai timerange tidak boleh lebih besar dari tanggal akhir.")
        return start_dt, end_dt

    def start_download_data_job(
        self,
        *,
        exchange: str = "binance",
        pairs: list[str],
        timeframes: list[str],
        days: int | None = 30,
        timerange: str | None = None,
    ) -> dict[str, Any]:
        """
        Start historical data download via Freqtrade REST API (if connected),
        `freqtrade download-data` subprocess (if CLI installed), or built-in
        Binance Multi-Mirror Cloud Downloader (for Streamlit Community Cloud).
        """
        if not pairs:
            raise ValueError("Pilih minimal satu pasangan trading untuk diunduh.")
        if not timeframes:
            raise ValueError("Pilih minimal satu timeframe untuk diunduh.")
        if days and timerange:
            raise ValueError("Parameter `days` dan `timerange` tidak boleh digunakan bersamaan (aturan resmi Freqtrade).")

        # 1. Try REST API if webserver is online
        if self.api_client is not None:
            ping = self.api_client.ping()
            if ping.ok:
                payload: dict[str, Any] = {
                    "pairs": pairs,
                    "timeframes": timeframes,
                    "exchange": exchange,
                }
                if timerange:
                    payload["timerange"] = timerange
                elif days:
                    payload["days"] = int(days)
                res = self.api_client.download_data(payload)
                if res.ok:
                    return {
                        "started": True,
                        "mode": "rest_api",
                        "job_id": (res.data or {}).get("job_id", "api_bg_download"),
                        "message": "Unduhan data dimulai di background melalui Freqtrade REST API.",
                    }

        # 2. Check if freqtrade CLI is available for background subprocess
        cli_ok, prefix, msg = self.process_manager.resolve_freqtrade_command_prefix()
        if cli_ok:
            datadir = self.get_exchange_data_dir(exchange)
            cmd = [
                *prefix,
                "download-data",
                "--config",
                str(self.config_service.config_path),
                "--exchange",
                exchange,
                "--datadir",
                str(datadir),
                "--pairs",
                *pairs,
                "--timeframes",
                *timeframes,
            ]
            if timerange:
                cmd.extend(["--timerange", timerange])
            elif days:
                cmd.extend(["--days", str(int(days))])

            job: ManagedJob = self.process_manager.start_job(
                "download_data",
                cmd,
                metadata={"exchange": exchange, "pairs": pairs, "timeframes": timeframes},
            )
            return {
                "started": job.status == "running",
                "mode": "subprocess",
                "job_id": job.job_id,
                "pid": job.pid,
                "message": f"Pekerjaan unduh data diluncurkan (Job ID: {job.job_id}).",
            }

        # 3. Built-in Streamlit Cloud Downloader (via Binance multi-mirror public API)
        downloaded = []
        errors = []
        for p in pairs:
            for tf in timeframes:
                out = self.download_binance_public_klines_to_feather(p, timeframe=tf, limit=750)
                if out.get("ok"):
                    downloaded.append(f"{p} ({tf}: {out['candles']} bar)")
                else:
                    errors.append(f"{p} ({tf}): {out.get('error')}")

        if downloaded:
            return {
                "started": True,
                "mode": "cloud_direct",
                "job_id": "cloud_binance_download",
                "message": f"Berhasil mengunduh data langsung ke format Freqtrade: {', '.join(downloaded)}.",
            }
        return {
            "started": False,
            "mode": "unavailable",
            "job_id": None,
            "message": ("Gagal mengunduh data: " + " | ".join(errors)) if errors else msg,
        }

    def download_binance_public_klines_to_feather(
        self,
        pair: str,
        timeframe: str = "5m",
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        Explicit user-controlled helper to fetch public Binance klines (`GET /api/v3/klines`)
        across mirror hosts (`data-api.binance.vision`, `api-gcp.binance.com`, `api1`–`api3`)
        and store them as a standard Freqtrade `.feather` (or `.json` fallback) file.
        """
        symbol = convert_symbol_freqtrade_to_v55(pair)
        limit_clamped = max(50, min(int(limit), 1000))
        headers = {"User-Agent": "MomentumRadar-Freqtrade-Integration/5.5", "Accept": "application/json"}

        rows = None
        last_err = None
        for host in BINANCE_MIRROR_HOSTS:
            try:
                resp = requests.get(
                    f"{host}/api/v3/klines",
                    params={"symbol": symbol, "interval": timeframe, "limit": limit_clamped},
                    headers=headers,
                    timeout=10.0,
                )
                if resp.status_code in (451, 403, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code} from {host}"
                    continue
                resp.raise_for_status()
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    break
            except Exception as exc:
                last_err = str(exc)
                continue

        if not isinstance(rows, list) or len(rows) < 2:
            return {"ok": False, "error": f"Gagal mengambil klines untuk {pair} ({timeframe}): {last_err}"}

        df = pd.DataFrame(
            [
                {
                    "date": pd.to_datetime(int(r[0]), unit="ms", utc=True),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                }
                for r in rows[:-1]  # Drop last unclosed candle to prevent lookahead
            ]
        )
        ex_dir = self.get_exchange_data_dir("binance")
        safe_pair = str(pair).split(":")[0].replace("/", "_")
        try:
            target = ex_dir / f"{safe_pair}-{timeframe}.feather"
            df.to_feather(target)
        except Exception:
            target = ex_dir / f"{safe_pair}-{timeframe}.json"
            df.to_json(target, orient="records", date_format="iso")

        return {
            "ok": True,
            "file_path": str(target),
            "candles": len(df),
            "start_date": df["date"].min().strftime("%Y-%m-%d %H:%M UTC"),
            "end_date": df["date"].max().strftime("%Y-%m-%d %H:%M UTC"),
        }

"""
Immutable closed-candle market data snapshot service, bar watermarks, OHLCV validation,
cross-timeframe consistency checks, live-quote separation, and TTL/watermark-aware caching
for AI Consensus Trading (PH03-01..PH03-04 / F04, F08, F14, F28, F29, T06, T14).
Strictly zero side-effects on import.
"""

import copy
import math
import numbers
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .domain import is_finite_number

INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}

DEFAULT_CACHE_TTL_SECONDS: Dict[str, float] = {
    "1m": 5.0,
    "5m": 15.0,
    "15m": 30.0,
    "1h": 60.0,
    "4h": 180.0,
    "1d": 300.0,
    "depth": 15.0,
    "ticker24h": 15.0,
    "price": 3.0,
}


def normalize_as_of_ms(as_of: Any = None) -> int:
    if as_of is None:
        return int(time.time() * 1000)
    if isinstance(as_of, pd.Timestamp):
        ts = as_of if as_of.tzinfo is not None else as_of.tz_localize("UTC")
        return int(ts.timestamp() * 1000)
    if isinstance(as_of, datetime):
        dt = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(as_of, numbers.Real) and not isinstance(as_of, bool):
        val = float(as_of)
        if not math.isfinite(val):
            raise ValueError(f"as_of harus finite, diterima: {as_of!r}")
        if abs(val) < 1e11:
            return int(round(val * 1000.0))
        return int(round(val))
    if isinstance(as_of, str):
        s = as_of.strip()
        if s.isdigit():
            return normalize_as_of_ms(int(s))
        ts = pd.Timestamp(s)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return int(ts.timestamp() * 1000)
    raise TypeError(f"Tipe as_of tidak didukung: {type(as_of)!r}")


def ms_to_iso(epoch_ms: Optional[int]) -> Optional[str]:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class BarWatermark:
    interval: str
    open_time: Optional[int]
    close_time: Optional[int]
    is_closed: bool
    bar_time_iso: Optional[str]
    closed_bar_count: int
    total_bar_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interval": self.interval,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "is_closed": self.is_closed,
            "bar_time_iso": self.bar_time_iso,
            "closed_bar_count": self.closed_bar_count,
            "total_bar_count": self.total_bar_count,
        }


@dataclass(frozen=True)
class LiveBarQuote:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool
    as_of_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "is_closed": self.is_closed,
            "as_of_ms": self.as_of_ms,
        }


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    is_monotonic: bool = True
    has_duplicates: bool = False
    has_gaps: bool = False
    gap_count: int = 0
    non_finite_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "is_monotonic": self.is_monotonic,
            "has_duplicates": self.has_duplicates,
            "has_gaps": self.has_gaps,
            "gap_count": self.gap_count,
            "non_finite_count": self.non_finite_count,
        }


@dataclass(frozen=True)
class TimeframeSnapshot:
    symbol: str
    interval: str
    as_of_ms: int
    as_of_iso: str
    closed_df: pd.DataFrame
    raw_df: pd.DataFrame
    live_bar: Optional[LiveBarQuote]
    watermark: BarWatermark
    validation: ValidationReport
    freshness_seconds: float
    ttl_seconds: float
    is_fresh: bool
    freshness_status: str
    cache_key: str
    from_cache: bool = False

    def copy_closed_df(self) -> pd.DataFrame:
        out = self.closed_df.copy(deep=True)
        out.attrs["_is_closed_df"] = True
        out.attrs["as_of_ms"] = self.as_of_ms
        out.attrs["interval"] = self.interval
        out.attrs["symbol"] = self.symbol
        return out


def _infer_open_and_close_ms(df: pd.DataFrame, interval: str = "15m") -> Tuple[pd.Series, pd.Series]:
    interval_ms = INTERVAL_SECONDS.get(interval, 900) * 1000
    if "open_time" in df.columns:
        open_ms = pd.to_numeric(df["open_time"], errors="coerce")
    elif "time" in df.columns:
        open_ms = pd.to_numeric(df["time"], errors="coerce")
    elif isinstance(df.index, pd.DatetimeIndex):
        idx_utc = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
        open_ms = pd.Series((idx_utc.asi8 // 1_000_000), index=df.index, dtype="int64")
    else:
        base_ms = 1_700_000_000_000
        open_ms = pd.Series(
            [base_ms + i * interval_ms for i in range(len(df))],
            index=df.index,
            dtype="int64",
        )

    if "close_time" in df.columns:
        close_ms = pd.to_numeric(df["close_time"], errors="coerce")
    else:
        close_ms = open_ms + interval_ms - 1

    return open_ms.astype("int64"), close_ms.astype("int64")


def annotate_bar_watermarks(
    df: pd.DataFrame,
    interval: str = "15m",
    as_of: Any = None,
) -> pd.DataFrame:
    if df is None or len(df) == 0:
        out = pd.DataFrame() if df is None else df.copy()
        out.attrs["_is_closed_df"] = True
        return out

    out = df.copy(deep=True)
    open_ms, close_ms = _infer_open_and_close_ms(out, interval=interval)
    out["open_time"] = open_ms
    out["close_time"] = close_ms

    if out.attrs.get("_is_closed_df") is True and as_of is None:
        out["is_closed"] = True
        return out

    ref_ms = normalize_as_of_ms(as_of)
    if as_of is not None or (close_ms > ref_ms).any():
        out["is_closed"] = close_ms <= ref_ms
    else:
        is_closed_flags = [True] * len(out)
        if len(out) > 8 and not out.attrs.get("_is_closed_df", False):
            is_closed_flags[-1] = False
        out["is_closed"] = is_closed_flags

    return out


def _lock_dataframe_buffers(df: pd.DataFrame) -> pd.DataFrame:
    for blk in getattr(getattr(df, "_mgr", None), "blocks", ()):
        try:
            blk.values.flags.writeable = False
        except Exception:
            pass
    return df


def extract_closed_df(
    df: pd.DataFrame,
    as_of: Any = None,
    interval: str = "15m",
) -> pd.DataFrame:
    if df is None:
        empty = pd.DataFrame()
        empty.attrs["_is_closed_df"] = True
        return empty
    if len(df) == 0:
        out = df.copy()
        out.attrs["_is_closed_df"] = True
        return out

    already_closed = bool(df.attrs.get("_is_closed_df", False))

    if "close_time" in df.columns:
        close_ms = pd.to_numeric(df["close_time"], errors="coerce")
        if as_of is not None:
            ref_ms = normalize_as_of_ms(as_of)
            out = df.loc[close_ms <= ref_ms].copy(deep=True)
            out["is_closed"] = True
            out.attrs["_is_closed_df"] = True
            out.attrs["as_of_ms"] = ref_ms
            return out

        if already_closed:
            out = df.copy(deep=True)
            out.attrs["_is_closed_df"] = True
            return out

        if "is_closed" in df.columns:
            out = df.loc[df["is_closed"].astype(bool)].copy(deep=True)
            out.attrs["_is_closed_df"] = True
            return out

        now_ms = normalize_as_of_ms(None)
        if (close_ms > now_ms).any():
            out = df.loc[close_ms <= now_ms].copy(deep=True)
            out["is_closed"] = True
            out.attrs["_is_closed_df"] = True
            out.attrs["as_of_ms"] = now_ms
            return out

    if already_closed:
        out = df.copy(deep=True)
        out.attrs["_is_closed_df"] = True
        return out

    if "is_closed" in df.columns and as_of is None:
        out = df.loc[df["is_closed"].astype(bool)].copy(deep=True)
        out.attrs["_is_closed_df"] = True
        return out

    if len(df) > 8:
        out = df.iloc[:-1].copy(deep=True)
    else:
        out = df.copy(deep=True)
    out.attrs["_is_closed_df"] = True
    return out


def validate_ohlcv_dataframe(
    df: pd.DataFrame,
    interval: str = "15m",
) -> ValidationReport:
    if df is None or len(df) == 0:
        return ValidationReport(
            valid=False,
            errors=("EMPTY_DATAFRAME",),
            warnings=(),
            is_monotonic=True,
            has_duplicates=False,
            has_gaps=False,
            gap_count=0,
            non_finite_count=0,
        )

    errors: List[str] = []
    warnings: List[str] = []
    required_cols = ("Open", "High", "Low", "Close", "Volume")
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        return ValidationReport(
            valid=False,
            errors=(f"MISSING_COLUMNS:{','.join(missing_cols)}",),
            warnings=(),
        )

    open_ms, _ = _infer_open_and_close_ms(df, interval=interval)
    diffs = open_ms.diff().dropna()
    has_duplicates = bool((diffs == 0).any())
    is_monotonic = bool((diffs > 0).all())
    if has_duplicates:
        errors.append("DUPLICATE_TIMESTAMPS")
    if not is_monotonic and not has_duplicates:
        errors.append("NON_MONOTONIC_TIMESTAMPS")

    expected_step = INTERVAL_SECONDS.get(interval, 900) * 1000
    gap_mask = diffs > (expected_step * 1.5)
    gap_count = int(gap_mask.sum())
    has_gaps = gap_count > 0
    if has_gaps:
        warnings.append(f"TIME_GAPS_DETECTED:{gap_count}")

    non_finite_count = 0
    for col in required_cols:
        s = df[col]
        bad_mask = s.isna() | (~s.map(is_finite_number))
        non_finite_count += int(bad_mask.sum())

    if non_finite_count > 0:
        errors.append(f"NON_FINITE_OHLCV:{non_finite_count}")
    else:
        if (df["Open"] <= 0).any() or (df["High"] <= 0).any() or (df["Low"] <= 0).any() or (df["Close"] <= 0).any():
            errors.append("NON_POSITIVE_PRICE")
        if (df["Volume"] < 0).any():
            errors.append("NEGATIVE_VOLUME")
        high_ok = (df["High"] >= df[["Open", "Close", "Low"]].max(axis=1)).all()
        low_ok = (df["Low"] <= df[["Open", "Close", "High"]].min(axis=1)).all()
        if not (high_ok and low_ok):
            errors.append("INVALID_HIGH_LOW_ENVELOPE")

    return ValidationReport(
        valid=len(errors) == 0,
        errors=tuple(errors),
        warnings=tuple(warnings),
        is_monotonic=is_monotonic,
        has_duplicates=has_duplicates,
        has_gaps=has_gaps,
        gap_count=gap_count,
        non_finite_count=non_finite_count,
    )


def validate_cross_timeframe_consistency(
    frames_or_snapshots: Dict[str, Any],
    *,
    as_of: Any = None,
    max_price_divergence_pct: float = 15.0,
) -> Dict[str, Any]:
    """
    Validate consistency across multiple timeframes (e.g. 5m, 15m, 1h, 4h, 1d) (Phase 1.5):
    - Checks that each timeframe has valid closed candles.
    - Checks that latest closed prices across core timeframes do not diverge by more than max_price_divergence_pct.
    - Checks that no higher-timeframe closed candle has a close_time in the future relative to as_of.
    """
    if not isinstance(frames_or_snapshots, dict) or not frames_or_snapshots:
        return {
            "consistent": False,
            "errors": ["EMPTY_TIMEFRAME_MAP"],
            "warnings": [],
            "price_spread_pct": 0.0,
        }

    ref_ms = normalize_as_of_ms(as_of) if as_of is not None else None
    errors: List[str] = []
    warnings: List[str] = []
    core_prices: Dict[str, float] = {}

    for tf, item in frames_or_snapshots.items():
        if isinstance(item, TimeframeSnapshot):
            df = item.closed_df
            if not item.validation.valid:
                errors.append(f"INVALID_TF_{tf}:{','.join(item.validation.errors)}")
            if not item.is_fresh:
                warnings.append(f"STALE_TF_{tf}:{item.freshness_status}")
            wm_close = item.watermark.close_time
        elif isinstance(item, pd.DataFrame):
            df = extract_closed_df(item, as_of=as_of, interval=tf)
            val = validate_ohlcv_dataframe(df, interval=tf)
            if not val.valid:
                errors.append(f"INVALID_TF_{tf}:{','.join(val.errors)}")
            _, close_ms_s = _infer_open_and_close_ms(df, interval=tf) if len(df) > 0 else (pd.Series(dtype="int64"), pd.Series(dtype="int64"))
            wm_close = int(close_ms_s.iloc[-1]) if len(close_ms_s) > 0 else None
        elif isinstance(item, dict) and is_finite_number(item.get("price")):
            p_v = float(item["price"])
            if p_v <= 0:
                errors.append(f"NON_POSITIVE_PRICE_{tf}")
            elif tf in ("5m", "15m", "1h"):
                core_prices[tf] = p_v
            wm_close = normalize_as_of_ms(item["close_time"]) if item.get("close_time") is not None else None
            df = None
        else:
            errors.append(f"UNSUPPORTED_TF_ITEM_{tf}")
            continue

        if df is not None:
            if len(df) == 0:
                errors.append(f"EMPTY_CLOSED_BARS_{tf}")
                continue
            last_close = float(df["Close"].iloc[-1])
            if not is_finite_number(last_close) or last_close <= 0:
                errors.append(f"NON_POSITIVE_PRICE_{tf}")
            elif tf in ("5m", "15m", "1h"):
                core_prices[tf] = last_close

        if ref_ms is not None and wm_close is not None and wm_close > ref_ms:
            errors.append(f"FUTURE_BAR_LEAKAGE_{tf}:close_time={wm_close}>as_of={ref_ms}")

    spread_pct = 0.0
    if len(core_prices) >= 2:
        vals = list(core_prices.values())
        min_p = min(vals)
        max_p = max(vals)
        if min_p > 0:
            spread_pct = round(((max_p - min_p) / min_p) * 100.0, 4)
            if spread_pct > float(max_price_divergence_pct):
                errors.append(f"CROSS_TF_PRICE_DIVERGENCE:{spread_pct:.2f}%>{max_price_divergence_pct:.2f}%")

    return {
        "consistent": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "price_spread_pct": spread_pct,
        "core_prices": core_prices,
    }


def align_higher_timeframes_as_of(
    htf_frames: Dict[str, pd.DataFrame],
    decision_close_ms: Any,
) -> Dict[str, pd.DataFrame]:
    """
    Strictly align higher-timeframe DataFrames (e.g., 1h, 4h, 1d) to `decision_close_ms`
    without future-data leakage (Phase 2.1):
    Only candles whose `close_time <= decision_close_ms` are included.
    An unclosed higher-timeframe candle that is still forming at `decision_close_ms` is excluded!
    """
    ref_ms = normalize_as_of_ms(decision_close_ms)
    aligned: Dict[str, pd.DataFrame] = {}
    for tf, df in (htf_frames or {}).items():
        if not isinstance(df, pd.DataFrame) or len(df) == 0:
            empty = pd.DataFrame()
            empty.attrs["_is_closed_df"] = True
            aligned[tf] = empty
            continue
        _, close_ms = _infer_open_and_close_ms(df, interval=tf)
        sliced = df.loc[close_ms <= ref_ms].copy(deep=True)
        sliced["is_closed"] = True
        sliced.attrs["_is_closed_df"] = True
        sliced.attrs["as_of_ms"] = ref_ms
        sliced.attrs["interval"] = tf
        aligned[tf] = sliced
    return aligned


def build_timeframe_snapshot(
    df: pd.DataFrame,
    symbol: str,
    interval: str = "15m",
    as_of: Any = None,
    ttl_seconds: Optional[float] = None,
    limit: Optional[int] = None,
) -> TimeframeSnapshot:
    as_of_ms = normalize_as_of_ms(as_of)
    as_of_iso = ms_to_iso(as_of_ms) or ""
    ttl = float(ttl_seconds if ttl_seconds is not None else DEFAULT_CACHE_TTL_SECONDS.get(interval, 30.0))

    if df is None or len(df) == 0:
        empty_df = pd.DataFrame()
        empty_df.attrs["_is_closed_df"] = True
        wm = BarWatermark(
            interval=interval,
            open_time=None,
            close_time=None,
            is_closed=False,
            bar_time_iso=None,
            closed_bar_count=0,
            total_bar_count=0,
        )
        val = validate_ohlcv_dataframe(empty_df, interval=interval)
        return TimeframeSnapshot(
            symbol=symbol,
            interval=interval,
            as_of_ms=as_of_ms,
            as_of_iso=as_of_iso,
            closed_df=empty_df,
            raw_df=empty_df,
            live_bar=None,
            watermark=wm,
            validation=val,
            freshness_seconds=float("inf"),
            ttl_seconds=ttl,
            is_fresh=False,
            freshness_status="EMPTY",
            cache_key=f"kline|{symbol}|{interval}|{limit or 0}|empty",
            from_cache=False,
        )

    annotated = annotate_bar_watermarks(df, interval=interval, as_of=as_of)
    closed_only = extract_closed_df(annotated, as_of=as_of, interval=interval)
    closed_only.attrs["_is_closed_df"] = True
    closed_only.attrs["as_of_ms"] = as_of_ms
    closed_only.attrs["interval"] = interval
    closed_only.attrs["symbol"] = symbol

    unclosed_rows = annotated.loc[~annotated["is_closed"].astype(bool)]
    live_quote: Optional[LiveBarQuote] = None
    if len(unclosed_rows) > 0:
        last_live = unclosed_rows.iloc[-1]
        live_quote = LiveBarQuote(
            symbol=symbol,
            interval=interval,
            open_time=int(last_live["open_time"]),
            close_time=int(last_live["close_time"]),
            open=float(last_live["Open"]) if is_finite_number(last_live.get("Open")) else 0.0,
            high=float(last_live["High"]) if is_finite_number(last_live.get("High")) else 0.0,
            low=float(last_live["Low"]) if is_finite_number(last_live.get("Low")) else 0.0,
            close=float(last_live["Close"]) if is_finite_number(last_live.get("Close")) else 0.0,
            volume=float(last_live["Volume"]) if is_finite_number(last_live.get("Volume")) else 0.0,
            is_closed=False,
            as_of_ms=as_of_ms,
        )

    val_report = validate_ohlcv_dataframe(closed_only if len(closed_only) > 0 else annotated, interval=interval)

    if len(closed_only) > 0:
        last_closed = closed_only.iloc[-1]
        wm_open = int(last_closed["open_time"])
        wm_close = int(last_closed["close_time"])
        wm_iso = ms_to_iso(wm_close)
        age_sec = max(0.0, (as_of_ms - wm_close) / 1000.0)
        max_stale_sec = max(INTERVAL_SECONDS.get(interval, 900) * 2.5, ttl * 2.0)
        is_fresh = bool(val_report.valid and age_sec <= max_stale_sec)
        status_str = "FRESH" if is_fresh else ("INVALID" if not val_report.valid else "STALE")
    else:
        wm_open = None
        wm_close = None
        wm_iso = None
        age_sec = float("inf")
        is_fresh = False
        status_str = "EMPTY"

    wm = BarWatermark(
        interval=interval,
        open_time=wm_open,
        close_time=wm_close,
        is_closed=len(closed_only) > 0,
        bar_time_iso=wm_iso,
        closed_bar_count=len(closed_only),
        total_bar_count=len(annotated),
    )

    eff_limit = limit if limit is not None else len(annotated)
    c_key = f"kline|{symbol}|{interval}|{eff_limit}|{wm_close or 0}"

    preserved_attrs = dict(getattr(df, "attrs", {}) or {})
    locked_closed = _lock_dataframe_buffers(closed_only.copy(deep=True))
    locked_closed.attrs.update(preserved_attrs)
    locked_closed.attrs["_is_closed_df"] = True
    locked_closed.attrs["as_of_ms"] = as_of_ms
    locked_closed.attrs["interval"] = interval
    locked_closed.attrs["symbol"] = symbol

    locked_raw = _lock_dataframe_buffers(annotated.copy(deep=True))

    return TimeframeSnapshot(
        symbol=symbol,
        interval=interval,
        as_of_ms=as_of_ms,
        as_of_iso=as_of_iso,
        closed_df=locked_closed,
        raw_df=locked_raw,
        live_bar=live_quote,
        watermark=wm,
        validation=val_report,
        freshness_seconds=round(age_sec, 3) if math.isfinite(age_sec) else age_sec,
        ttl_seconds=ttl,
        is_fresh=is_fresh,
        freshness_status=status_str,
        cache_key=c_key,
        from_cache=False,
    )


def filter_bars_since(
    df_or_snapshot: Any,
    since_time: Any,
    include_unclosed: bool = False,
    interval: str = "15m",
) -> pd.DataFrame:
    if isinstance(df_or_snapshot, TimeframeSnapshot):
        base_df = df_or_snapshot.raw_df if include_unclosed else df_or_snapshot.copy_closed_df()
    elif isinstance(df_or_snapshot, pd.DataFrame):
        base_df = df_or_snapshot.copy() if include_unclosed else extract_closed_df(df_or_snapshot, interval=interval)
    else:
        return pd.DataFrame()

    if len(base_df) == 0:
        return base_df

    since_ms = normalize_as_of_ms(since_time)
    _, close_ms = _infer_open_and_close_ms(base_df, interval=interval)
    out = base_df.loc[close_ms >= since_ms].copy(deep=True)
    out.attrs["_is_closed_df"] = not include_unclosed
    return out


class MarketSnapshotCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._klines: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
        self._order_books: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._tickers_24h: Optional[Dict[str, Any]] = None
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._hits = 0
        self._misses = 0

    def _is_entry_valid(self, entry: Dict[str, Any], curr_ms: int) -> bool:
        fetched_at_ms = int(entry["fetched_at_ms"])
        ttl_ms = float(entry["ttl_seconds"]) * 1000.0
        next_close_ms = entry.get("next_bar_close_ms")
        if curr_ms - fetched_at_ms > ttl_ms:
            return False
        if next_close_ms is not None and curr_ms > int(next_close_ms):
            return False
        return True

    def get_kline_snapshot(
        self,
        symbol: str,
        interval: str,
        limit: int,
        now_ms: Optional[int] = None,
        allow_superset: bool = True,
    ) -> Optional[TimeframeSnapshot]:
        curr_ms = normalize_as_of_ms(now_ms)
        sym_u = symbol.upper().strip()
        tf_s = interval.strip()
        req_lim = int(limit)
        exact_key = (sym_u, tf_s, req_lim)

        with self._lock:
            entry = self._klines.get(exact_key)
            if entry is not None:
                if not self._is_entry_valid(entry, curr_ms):
                    self._klines.pop(exact_key, None)
                    entry = None

            if entry is None and allow_superset:
                candidates = []
                expired_keys = []
                for k, v in self._klines.items():
                    if k[0] == sym_u and k[1] == tf_s:
                        if self._is_entry_valid(v, curr_ms):
                            if k[2] >= req_lim:
                                candidates.append((k[2], v))
                        else:
                            expired_keys.append(k)
                for ek in expired_keys:
                    self._klines.pop(ek, None)
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    entry = candidates[0][1]

            if entry is None:
                self._misses += 1
                return None

            self._hits += 1
            snap: TimeframeSnapshot = entry["snapshot"]
            fetched_at_ms = int(entry["fetched_at_ms"])
            age_sec = max(0.0, (curr_ms - fetched_at_ms) / 1000.0)
            return TimeframeSnapshot(
                symbol=snap.symbol,
                interval=snap.interval,
                as_of_ms=snap.as_of_ms,
                as_of_iso=snap.as_of_iso,
                closed_df=snap.closed_df,
                raw_df=snap.raw_df,
                live_bar=snap.live_bar,
                watermark=snap.watermark,
                validation=snap.validation,
                freshness_seconds=round(age_sec, 3),
                ttl_seconds=snap.ttl_seconds,
                is_fresh=True,
                freshness_status="FRESH",
                cache_key=snap.cache_key,
                from_cache=True,
            )

    def put_kline_snapshot(
        self,
        snapshot: TimeframeSnapshot,
        limit: int,
        fetched_at_ms: Optional[int] = None,
        ttl_seconds: Optional[float] = None,
    ) -> TimeframeSnapshot:
        now_ms = normalize_as_of_ms(fetched_at_ms if fetched_at_ms is not None else snapshot.as_of_ms)
        ttl = float(ttl_seconds if ttl_seconds is not None else snapshot.ttl_seconds)
        interval_ms = INTERVAL_SECONDS.get(snapshot.interval, 900) * 1000
        next_close_ms: Optional[int] = None
        if snapshot.live_bar is not None:
            next_close_ms = snapshot.live_bar.close_time
        elif snapshot.watermark.close_time is not None:
            next_close_ms = snapshot.watermark.close_time + interval_ms

        key = (snapshot.symbol.upper().strip(), snapshot.interval.strip(), int(limit))
        with self._lock:
            self._klines[key] = {
                "snapshot": snapshot,
                "fetched_at_ms": now_ms,
                "ttl_seconds": ttl,
                "next_bar_close_ms": next_close_ms,
                "cache_key": snapshot.cache_key,
            }
        return snapshot

    def get_order_book(
        self,
        symbol: str,
        limit: int = 20,
        now_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        curr_ms = normalize_as_of_ms(now_ms)
        key = (symbol.upper().strip(), int(limit))
        with self._lock:
            entry = self._order_books.get(key)
            if entry is None:
                self._misses += 1
                return None
            if curr_ms - int(entry["fetched_at_ms"]) > float(entry["ttl_seconds"]) * 1000.0:
                self._order_books.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            out = copy.deepcopy(entry["data"])
            out["from_cache"] = True
            return out

    def put_order_book(
        self,
        symbol: str,
        data: Dict[str, Any],
        limit: int = 20,
        fetched_at_ms: Optional[int] = None,
        ttl_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_ms = normalize_as_of_ms(fetched_at_ms)
        ttl = float(ttl_seconds if ttl_seconds is not None else DEFAULT_CACHE_TTL_SECONDS["depth"])
        key = (symbol.upper().strip(), int(limit))
        with self._lock:
            self._order_books[key] = {
                "data": copy.deepcopy(data),
                "fetched_at_ms": now_ms,
                "ttl_seconds": ttl,
            }
        return data

    def get_tickers_24h(self, now_ms: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
        curr_ms = normalize_as_of_ms(now_ms)
        with self._lock:
            if self._tickers_24h is None:
                return None
            if curr_ms - int(self._tickers_24h["fetched_at_ms"]) > float(self._tickers_24h["ttl_seconds"]) * 1000.0:
                self._tickers_24h = None
                return None
            self._hits += 1
            return copy.deepcopy(self._tickers_24h["data"])

    def put_tickers_24h(
        self,
        data: List[Dict[str, Any]],
        fetched_at_ms: Optional[int] = None,
        ttl_seconds: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now_ms = normalize_as_of_ms(fetched_at_ms)
        ttl = float(ttl_seconds if ttl_seconds is not None else DEFAULT_CACHE_TTL_SECONDS["ticker24h"])
        with self._lock:
            self._tickers_24h = {
                "data": copy.deepcopy(data),
                "fetched_at_ms": now_ms,
                "ttl_seconds": ttl,
            }
        return data

    def invalidate(self, symbol: Optional[str] = None, interval: Optional[str] = None) -> None:
        with self._lock:
            if symbol is None and interval is None:
                self._klines.clear()
                self._order_books.clear()
                self._tickers_24h = None
                self._quotes.clear()
                return
            sym_u = symbol.upper().strip() if symbol else None
            tf_s = interval.strip() if interval else None
            keys_to_del = [
                k for k in self._klines
                if (sym_u is None or k[0] == sym_u) and (tf_s is None or k[1] == tf_s)
            ]
            for k in keys_to_del:
                self._klines.pop(k, None)
            if sym_u is not None and tf_s is None:
                ob_del = [k for k in self._order_books if k[0] == sym_u]
                for k in ob_del:
                    self._order_books.pop(k, None)
                self._quotes.pop(sym_u, None)

    def clear(self) -> None:
        with self._lock:
            self._klines.clear()
            self._order_books.clear()
            self._tickers_24h = None
            self._quotes.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "kline_entries": len(self._klines),
                "order_book_entries": len(self._order_books),
            }


GLOBAL_MARKET_CACHE = MarketSnapshotCache()


def get_monitor_runtime_status(
    execution_mode: str = "session_only",
    auto_enabled: bool = False,
    last_heartbeat_ms: Optional[int] = None,
    interval_seconds: int = 45,
    *,
    now_ms: Optional[int] = None,
    worker_id: Optional[str] = None,
    worker_status: str = "RUNNING",
    cycles_completed: int = 0,
    candidates_scanned: int = 0,
    ai_validations_run: int = 0,
    signals_saved: int = 0,
    paper_trades_reconciled: int = 0,
    last_cycle_at: Optional[str] = None,
    last_error: str = "",
) -> Dict[str, Any]:
    mode_norm = str(execution_mode or "session_only").strip().lower()
    curr_ms = normalize_as_of_ms(now_ms)
    hb_age_sec: Optional[float] = None
    if last_heartbeat_ms is not None and int(last_heartbeat_ms) > 0:
        hb_age_sec = max(0.0, (curr_ms - int(last_heartbeat_ms)) / 1000.0)

    is_worker = mode_norm == "worker"
    w_status_norm = str(worker_status or ("RUNNING" if is_worker else "IDLE")).strip().upper()
    max_healthy_age = max(float(interval_seconds) * 2.5, 120.0)
    worker_alive = bool(
        is_worker
        and w_status_norm in {"RUNNING", "IDLE"}
        and hb_age_sec is not None
        and hb_age_sec <= max_healthy_age
    )
    if worker_alive:
        runtime_label = "BACKGROUND_WORKER_ACTIVE"
        disclaimer_txt = (
            "Worker independen aktif di latar belakang (CLI/Daemon): pemantauan momentum dan "
            "rekonsiliasi paper trade tetap berjalan meskipun tab browser ditutup."
        )
    else:
        runtime_label = "SESSION_ONLY_FRAGMENT" if auto_enabled else "MANUAL_ONLY"
        disclaimer_txt = (
            "Mode Session-Only (Streamlit UI): pemantauan otomatis hanya berjalan selama tab browser aktif. "
            "Gunakan CLI '--worker' / BackgroundMonitorWorker untuk pemantauan independen di latar belakang."
        )
    return {
        "execution_mode": "worker" if is_worker else "session_only",
        "runtime_label": runtime_label,
        "auto_fragment_enabled": bool(auto_enabled),
        "worker_alive": worker_alive,
        "worker_id": worker_id or ("default_worker" if is_worker else None),
        "worker_status": w_status_norm,
        "interval_seconds": int(interval_seconds),
        "cycles_completed": int(cycles_completed or 0),
        "candidates_scanned": int(candidates_scanned or 0),
        "ai_validations_run": int(ai_validations_run or 0),
        "signals_saved": int(signals_saved or 0),
        "paper_trades_reconciled": int(paper_trades_reconciled or 0),
        "last_heartbeat_ms": last_heartbeat_ms,
        "heartbeat_age_seconds": round(hb_age_sec, 2) if hb_age_sec is not None else None,
        "last_cycle_at": last_cycle_at,
        "last_error": str(last_error or ""),
        "disclaimer": disclaimer_txt,
    }
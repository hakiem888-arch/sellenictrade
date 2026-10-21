"""
Deterministic technical indicators, RSI edge-case policy, selective warm-up/dropna handling,
EMA200 readiness diagnostics, and safe empty/short-frame extractors for AI Consensus Trading
(PH03-02 / F07, F09, F10, T07, T08).
Strictly zero side-effects on import.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .domain import is_finite_number
from .market import (
    extract_closed_df,
    validate_ohlcv_dataframe,
)

INDICATOR_VERSION: str = "v6.0-wilder-ewm-1"
DEFAULT_KLINES_LIMIT: int = 260
MIN_BARS_CORE_INDICATORS: int = 20
MIN_BARS_EMA50: int = 50
MIN_BARS_EMA200: int = 200
RECOMMENDED_BARS_EMA200: int = 250

WARMUP_REQUIREMENTS: Dict[str, int] = {
    "RSI": 14,
    "ATR": 14,
    "BB": 20,
    "VOL_RATIO": 20,
    "MACD": 35,
    "EMA20": 20,
    "EMA50": 50,
    "EMA200": 200,
}


@dataclass(frozen=True)
class WarmupAndQualityReport:
    bar_count: int
    usable_bar_count: int
    warmup_status: str
    quality_status: str
    core_indicators_ready: bool
    ema50_ready: bool
    ema200_ready: bool
    indicator_readiness: Dict[str, bool] = field(default_factory=dict)
    quality_errors: Tuple[str, ...] = field(default_factory=tuple)
    quality_warnings: Tuple[str, ...] = field(default_factory=tuple)
    indicator_version: str = INDICATOR_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bar_count": self.bar_count,
            "usable_bar_count": self.usable_bar_count,
            "warmup_status": self.warmup_status,
            "quality_status": self.quality_status,
            "core_indicators_ready": self.core_indicators_ready,
            "ema50_ready": self.ema50_ready,
            "ema200_ready": self.ema200_ready,
            "indicator_readiness": dict(self.indicator_readiness),
            "quality_errors": list(self.quality_errors),
            "quality_warnings": list(self.quality_warnings),
            "indicator_version": self.indicator_version,
        }


def ema(s: pd.Series, n: int) -> pd.Series:
    if s is None or len(s) == 0:
        return pd.Series(dtype="float64")
    clean = pd.to_numeric(s, errors="coerce")
    return clean.ewm(span=int(n), adjust=False).mean()


def rsi(
    s: pd.Series,
    n: int = 14,
    min_periods: Optional[int] = None,
    flat_value: float = 50.0,
) -> pd.Series:
    if s is None or len(s) == 0:
        return pd.Series(dtype="float64")

    clean = pd.to_numeric(s, errors="coerce").astype("float64")
    d = clean.diff()

    gain_raw = d.where(d > 0.0, 0.0).fillna(0.0)
    loss_raw = (-d.where(d < 0.0, 0.0)).fillna(0.0)

    if len(clean) > 1:
        gain = gain_raw.iloc[1:].ewm(alpha=1.0 / float(n), adjust=False).mean()
        loss = loss_raw.iloc[1:].ewm(alpha=1.0 / float(n), adjust=False).mean()
        gain = pd.concat([pd.Series([0.0], index=clean.index[:1]), gain])
        loss = pd.concat([pd.Series([0.0], index=clean.index[:1]), loss])
    else:
        gain = pd.Series([0.0], index=clean.index, dtype="float64")
        loss = pd.Series([0.0], index=clean.index, dtype="float64")

    g_arr = gain.to_numpy(dtype="float64")
    l_arr = loss.to_numpy(dtype="float64")
    out_arr = np.full(len(clean), float(flat_value), dtype="float64")

    eps = 1e-15
    both_zero = (g_arr <= eps) & (l_arr <= eps)
    only_gain = (g_arr > eps) & (l_arr <= eps)
    only_loss = (g_arr <= eps) & (l_arr > eps)
    mixed = (g_arr > eps) & (l_arr > eps)

    out_arr[both_zero] = float(flat_value)
    out_arr[only_gain] = 100.0
    out_arr[only_loss] = 0.0
    if np.any(mixed):
        rs = g_arr[mixed] / l_arr[mixed]
        out_arr[mixed] = 100.0 - (100.0 / (1.0 + rs))

    nan_input = clean.isna().to_numpy()
    out_arr[nan_input] = np.nan

    if min_periods is not None and int(min_periods) > 1:
        cutoff = min(len(out_arr), int(min_periods) - 1)
        out_arr[:cutoff] = np.nan

    return pd.Series(out_arr, index=clean.index, dtype="float64")


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    if df is None or len(df) == 0:
        return pd.Series(dtype="float64")
    high = pd.to_numeric(df["High"], errors="coerce")
    low = pd.to_numeric(df["Low"], errors="coerce")
    close = pd.to_numeric(df["Close"], errors="coerce")
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(int(n)).mean()


def macd(s: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    if s is None or len(s) == 0:
        empty = pd.Series(dtype="float64")
        return empty, empty, empty
    line = ema(s, 12) - ema(s, 26)
    signal = ema(line, 9)
    return line, signal, line - signal


def bollinger(s: pd.Series, n: int = 20) -> Tuple[pd.Series, pd.Series, pd.Series]:
    if s is None or len(s) == 0:
        empty = pd.Series(dtype="float64")
        return empty, empty, empty
    clean = pd.to_numeric(s, errors="coerce")
    mid = clean.rolling(int(n)).mean()
    sd = clean.rolling(int(n)).std().fillna(0.0)
    return mid, mid + 2.0 * sd, mid - 2.0 * sd


def evaluate_warmup_and_quality(
    df: pd.DataFrame,
    interval: str = "15m",
) -> WarmupAndQualityReport:
    if df is None or len(df) == 0:
        return WarmupAndQualityReport(
            bar_count=0,
            usable_bar_count=0,
            warmup_status="EMPTY_DATA",
            quality_status="EMPTY_DATA",
            core_indicators_ready=False,
            ema50_ready=False,
            ema200_ready=False,
            indicator_readiness={k: False for k in WARMUP_REQUIREMENTS},
            quality_errors=("EMPTY_DATAFRAME",),
            quality_warnings=(),
        )

    val_report = validate_ohlcv_dataframe(df, interval=interval)
    n_bars = len(df)
    usable_bars = max(0, n_bars - (MIN_BARS_CORE_INDICATORS - 1)) if val_report.valid else 0

    readiness = {
        name: bool(val_report.valid and n_bars >= req_bars)
        for name, req_bars in WARMUP_REQUIREMENTS.items()
    }
    core_ready = bool(
        val_report.valid
        and readiness["RSI"]
        and readiness["ATR"]
        and readiness["BB"]
        and readiness["VOL_RATIO"]
        and readiness["EMA20"]
    )
    ema50_ok = bool(readiness["EMA50"])
    ema200_ok = bool(readiness["EMA200"])

    if not val_report.valid:
        warmup_status = "INVALID_DATA"
        quality_status = "INVALID_DATA"
    else:
        quality_status = "WARNING_GAPS" if val_report.has_gaps else "OK"
        if n_bars < MIN_BARS_CORE_INDICATORS:
            warmup_status = "INSUFFICIENT_HISTORY"
        elif n_bars < MIN_BARS_EMA200:
            warmup_status = "PARTIAL_WARMUP_NO_EMA200"
        else:
            warmup_status = "READY"

    return WarmupAndQualityReport(
        bar_count=n_bars,
        usable_bar_count=usable_bars,
        warmup_status=warmup_status,
        quality_status=quality_status,
        core_indicators_ready=core_ready,
        ema50_ready=ema50_ok,
        ema200_ready=ema200_ok,
        indicator_readiness=readiness,
        quality_errors=val_report.errors,
        quality_warnings=val_report.warnings,
    )


def compute_indicators_frame(
    df: pd.DataFrame,
    interval: str = "15m",
    drop_unwarmed: bool = True,
) -> pd.DataFrame:
    if df is None or len(df) == 0:
        empty = pd.DataFrame() if df is None else df.copy()
        rep = evaluate_warmup_and_quality(empty, interval=interval)
        empty.attrs["warmup_report"] = rep.to_dict()
        empty.attrs["ema200_ready"] = False
        empty.attrs["indicator_version"] = INDICATOR_VERSION
        return empty

    d = df.copy(deep=True)
    preserved_attrs = dict(getattr(df, "attrs", {}) or {})

    for col in ("Open", "High", "Low", "Close", "Volume", "QuoteVolume", "TakerBuyBase", "TakerBuyQuote"):
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    rep = evaluate_warmup_and_quality(d, interval=interval)

    d["EMA20"] = ema(d["Close"], 20)
    d["EMA50"] = ema(d["Close"], 50)
    d["EMA200"] = ema(d["Close"], 200)
    d["EMA200_READY"] = bool(rep.ema200_ready)
    d["RSI"] = rsi(d["Close"], 14)
    d["MACD"], d["MACD_SIGNAL"], d["MACD_HIST"] = macd(d["Close"])
    d["ATR"] = atr(d, 14)
    d["BB_MID"], d["BB_UP"], d["BB_LOW"] = bollinger(d["Close"], 20)

    vol_mean = d["Volume"].rolling(20).mean()
    vol_ratio = np.where(
        vol_mean.isna(),
        np.nan,
        np.where(vol_mean > 0.0, d["Volume"] / vol_mean, 0.0),
    )
    d["VOL_RATIO"] = pd.Series(vol_ratio, index=d.index, dtype="float64")

    if "TakerBuyQuote" in d.columns and "QuoteVolume" in d.columns:
        qvol = d["QuoteVolume"]
        tbq = d["TakerBuyQuote"].fillna(0.0)
        bp = np.where(
            qvol > 0.0,
            np.clip(tbq / qvol, 0.0, 1.0),
            0.5,
        )
        d["BUY_PRESSURE"] = pd.Series(bp, index=d.index, dtype="float64")
    elif "BUY_PRESSURE" not in d.columns:
        d["BUY_PRESSURE"] = pd.Series(0.5, index=d.index, dtype="float64")

    if drop_unwarmed:
        required_valid_cols = ["Open", "High", "Low", "Close", "Volume", "EMA20", "RSI", "ATR", "BB_MID", "VOL_RATIO"]
        present_req = [c for c in required_valid_cols if c in d.columns]
        d = d.dropna(subset=present_req).copy()

    d.attrs.update(preserved_attrs)
    d.attrs["warmup_report"] = rep.to_dict()
    d.attrs["ema200_ready"] = bool(rep.ema200_ready)
    d.attrs["warmup_status"] = rep.warmup_status
    d.attrs["indicator_version"] = INDICATOR_VERSION
    return d


def safe_candle_info(d: pd.DataFrame) -> Dict[str, Any]:
    if d is None or len(d) == 0:
        return {
            "name": "INSUFFICIENT_DATA",
            "body": 0.0,
            "upper_wick": 0.0,
            "lower_wick": 0.0,
        }
    a = d.iloc[-1]
    p = d.iloc[-2] if len(d) >= 2 else a

    body = abs(float(a.Close) - float(a.Open))
    rng = max(float(a.High) - float(a.Low), 1e-12)
    upper = float(a.High) - max(float(a.Open), float(a.Close))
    lower = min(float(a.Open), float(a.Close)) - float(a.Low)
    body_ratio = body / rng

    if body_ratio < 0.10:
        name = "DOJI"
    elif lower > body * 2 and upper < max(body, 1e-12):
        name = "HAMMER BULLISH"
    elif upper > body * 2 and lower < max(body, 1e-12):
        name = "SHOOTING STAR BEARISH"
    elif (
        len(d) >= 2
        and float(a.Close) > float(a.Open)
        and float(p.Close) < float(p.Open)
        and float(a.Open) <= float(p.Close)
        and float(a.Close) >= float(p.Open)
    ):
        name = "BULLISH ENGULFING"
    elif (
        len(d) >= 2
        and float(a.Close) < float(a.Open)
        and float(p.Close) > float(p.Open)
        and float(a.Open) >= float(p.Close)
        and float(a.Close) <= float(p.Open)
    ):
        name = "BEARISH ENGULFING"
    elif float(a.Close) > float(a.Open):
        name = "BULLISH"
    elif float(a.Close) < float(a.Open):
        name = "BEARISH"
    else:
        name = "NETRAL"

    return {
        "name": name,
        "body": body_ratio,
        "upper_wick": upper / rng,
        "lower_wick": lower / rng,
    }


def safe_market_regime(d: pd.DataFrame) -> str:
    if d is None or len(d) == 0:
        return "SIDEWAYS"
    a = d.iloc[-1]
    close_v = float(a.Close) if is_finite_number(getattr(a, "Close", None)) else 0.0
    if close_v <= 0:
        return "SIDEWAYS"
    atr_v = float(a.ATR) if is_finite_number(getattr(a, "ATR", None)) else 0.0
    ema20_v = float(a.EMA20) if is_finite_number(getattr(a, "EMA20", None)) else close_v
    ema50_v = float(a.EMA50) if is_finite_number(getattr(a, "EMA50", None)) else close_v

    vol = atr_v / close_v
    ema_gap = abs(ema20_v - ema50_v) / close_v
    if vol >= 0.03:
        return "VOLATILE"
    if ema_gap < 0.005:
        return "SIDEWAYS"
    return "TRENDING"


def extract_timeframe_features(
    df: pd.DataFrame,
    interval: str = "15m",
) -> Dict[str, Any]:
    if df is None or len(df) == 0:
        return {
            "price": None,
            "ema20": None,
            "ema50": None,
            "ema200": None,
            "ema200_ready": False,
            "warmup_status": "EMPTY_DATA",
            "rsi": None,
            "macd_hist": None,
            "atr": None,
            "vol_ratio": None,
            "buy_pressure": None,
            "bb_upper": None,
            "bb_lower": None,
            "indicator_version": INDICATOR_VERSION,
        }

    x = df.iloc[-1]
    rep_dict = df.attrs.get("warmup_report")
    ema200_ready = bool(df.attrs.get("ema200_ready", len(df) >= MIN_BARS_EMA200))
    warmup_status = str(
        df.attrs.get(
            "warmup_status",
            rep_dict.get("warmup_status") if isinstance(rep_dict, dict) else ("READY" if ema200_ready else "PARTIAL_WARMUP_NO_EMA200"),
        )
    )
    return {
        "price": float(x.Close),
        "ema20": float(x.EMA20),
        "ema50": float(x.EMA50),
        "ema200": float(x.EMA200),
        "ema200_ready": ema200_ready,
        "warmup_status": warmup_status,
        "rsi": float(x.RSI),
        "macd_hist": float(x.MACD_HIST),
        "atr": float(x.ATR),
        "vol_ratio": float(x.VOL_RATIO),
        "buy_pressure": float(x.BUY_PRESSURE),
        "bb_upper": float(x.BB_UP),
        "bb_lower": float(x.BB_LOW),
        "indicator_version": INDICATOR_VERSION,
    }

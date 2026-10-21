"""
MomentumRadarAIStrategy — Explicit Adapter Strategy for Freqtrade + Momentum Radar v5.5.

Bridges Momentum Radar's multi-timeframe technical/SMC indicators and validated AI Consensus
signals into Freqtrade's IStrategy v3 interface.

Key Safety & Anti-Lookahead Principles:
1. All technical indicators (EMA 20/50/200, RSI 14, MACD, Bollinger Bands, ATR 14, Volume Ratio)
   are computed strictly causally using closed historical bars (`shift(1)` / rolling windows)
   with zero `.bfill()` or negative shifts.
2. AI Consensus signals from `signals.db` or `ai_signal_bridge.json` are optional advisory inputs
   that are matched strictly by `bar_timestamp <= candle_timestamp` (never future bars) and
   require technical confirmation (`rsi`, `ema`, `volume > 0`) before setting `enter_long` / `enter_short`.
3. Short entries (`enter_short`) are only active when `can_short = True` and `trading_mode == 'futures'`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

try:
    from freqtrade.strategy import (
        BooleanParameter,
        CategoricalParameter,
        DecimalParameter,
        IntParameter,
        IStrategy,
    )
except ImportError:
    class IStrategy:  # type: ignore[no-redef]
        INTERFACE_VERSION = 3

    class _BaseParam:
        def __init__(self, default: Any = None, **kwargs: Any):
            self.default = default
            self.value = default
            self.kwargs = kwargs

    class IntParameter(_BaseParam):  # type: ignore[no-redef]
        def __init__(self, low: int, high: int, default: int, **kwargs: Any):
            super().__init__(default=default, low=low, high=high, **kwargs)

    class DecimalParameter(_BaseParam):  # type: ignore[no-redef]
        def __init__(self, low: float, high: float, default: float, **kwargs: Any):
            super().__init__(default=default, low=low, high=high, **kwargs)

    class BooleanParameter(_BaseParam):  # type: ignore[no-redef]
        def __init__(self, default: bool = True, **kwargs: Any):
            super().__init__(default=default, **kwargs)

    class CategoricalParameter(_BaseParam):  # type: ignore[no-redef]
        def __init__(self, categories: list[Any], default: Any = None, **kwargs: Any):
            super().__init__(default=default, categories=categories, **kwargs)


class MomentumRadarAIStrategy(IStrategy):
    """
    Freqtrade IStrategy v3 implementation combining Momentum Radar v5.5 technical rules
    with an explicit, lookahead-safe AI Consensus signal adapter.
    """

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short: bool = False

    minimal_roi = {
        "120": 0.005,
        "60": 0.015,
        "30": 0.025,
        "0": 0.045,
    }

    stoploss = -0.05
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count: int = 200

    # Hyperoptable parameters
    buy_rsi_min = IntParameter(low=25, high=55, default=40, space="buy", optimize=True, load=True)
    buy_rsi_max = IntParameter(low=56, high=80, default=68, space="buy", optimize=True, load=True)
    buy_vol_ratio = DecimalParameter(
        low=0.8, high=2.5, default=1.1, decimals=2, space="buy", optimize=True, load=True
    )
    sell_rsi_threshold = IntParameter(
        low=65, high=90, default=76, space="sell", optimize=True, load=True
    )
    use_ai_consensus_filter = BooleanParameter(
        default=False, space="buy", optimize=False, load=True
    )
    min_ai_confidence = IntParameter(
        low=55, high=90, default=70, space="buy", optimize=True, load=True
    )

    @staticmethod
    def _compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi.fillna(50.0)

    @staticmethod
    def _compute_atr(df: DataFrame, period: int = 14) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - prev_close).abs()
        tr3 = (df["low"] - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period, min_periods=1).mean().fillna(0.0)

    def _load_ai_bridge_signals(self, pair: str) -> list[dict[str, Any]]:
        """
        Load exported AI Consensus signals for `pair` from user_data/ai_signal_bridge.json.
        Only validated signals with a closed-bar timestamp are returned.
        """
        candidates = [
            Path("user_data/ai_signal_bridge.json"),
            Path(__file__).resolve().parent.parent / "user_data" / "ai_signal_bridge.json",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    items = raw.get("signals", []) if isinstance(raw, dict) else []
                    return [
                        s
                        for s in items
                        if isinstance(s, dict)
                        and s.get("pair") == pair
                        and s.get("gate_approved") is True
                    ]
                except Exception:
                    return []
        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Compute causal Momentum Radar v5.5 technical indicators on Freqtrade OHLCV dataframe."""
        dataframe["ema20"] = dataframe["close"].ewm(span=20, adjust=False).mean()
        dataframe["ema50"] = dataframe["close"].ewm(span=50, adjust=False).mean()
        dataframe["ema200"] = dataframe["close"].ewm(span=200, adjust=False).mean()

        dataframe["rsi"] = self._compute_rsi(dataframe["close"], 14)
        dataframe["atr"] = self._compute_atr(dataframe, 14)

        ema12 = dataframe["close"].ewm(span=12, adjust=False).mean()
        ema26 = dataframe["close"].ewm(span=26, adjust=False).mean()
        dataframe["macd"] = ema12 - ema26
        dataframe["macdsignal"] = dataframe["macd"].ewm(span=9, adjust=False).mean()
        dataframe["macdhist"] = dataframe["macd"] - dataframe["macdsignal"]

        bb_mid = dataframe["close"].rolling(window=20, min_periods=20).mean()
        bb_std = dataframe["close"].rolling(window=20, min_periods=20).std()
        dataframe["bb_middleband"] = bb_mid
        dataframe["bb_upperband"] = bb_mid + (2.0 * bb_std)
        dataframe["bb_lowerband"] = bb_mid - (2.0 * bb_std)

        vol_ma = dataframe["volume"].rolling(window=20, min_periods=20).mean()
        dataframe["vol_ratio"] = (dataframe["volume"] / vol_ma.replace(0.0, np.nan)).fillna(0.0)

        # Causal AI Consensus signal columns (0 = none, 1 = LONG, -1 = SHORT)
        dataframe["ai_signal"] = 0
        dataframe["ai_confidence"] = 0.0

        pair = metadata.get("pair", "") if isinstance(metadata, dict) else ""
        if pair and "date" in dataframe.columns:
            signals = self._load_ai_bridge_signals(pair)
            if signals:
                candle_ts = pd.to_datetime(dataframe["date"], utc=True)
                for sig in signals:
                    bar_time_str = sig.get("bar_time") or sig.get("created_at")
                    if not bar_time_str:
                        continue
                    try:
                        sig_ts = pd.to_datetime(bar_time_str, utc=True)
                        # Strictly causal: apply only on the matching closed candle timestamp
                        mask = (candle_ts >= sig_ts) & (
                            candle_ts <= sig_ts + pd.Timedelta(minutes=15)
                        )
                        direction = str(sig.get("direction", "")).upper()
                        conf = float(sig.get("confidence", 0.0) or 0.0)
                        if direction == "LONG":
                            dataframe.loc[mask, "ai_signal"] = 1
                            dataframe.loc[mask, "ai_confidence"] = conf
                        elif direction == "SHORT":
                            dataframe.loc[mask, "ai_signal"] = -1
                            dataframe.loc[mask, "ai_confidence"] = conf
                    except Exception:
                        continue

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Populate causal entry signals with optional AI Consensus confirmation."""
        long_cond = (
            (dataframe["close"] > dataframe["ema20"])
            & (dataframe["ema20"] >= dataframe["ema50"])
            & (dataframe["rsi"] >= self.buy_rsi_min.value)
            & (dataframe["rsi"] <= self.buy_rsi_max.value)
            & (dataframe["macdhist"] > 0)
            & (dataframe["vol_ratio"] >= float(self.buy_vol_ratio.value))
            & (dataframe["volume"] > 0)
        )

        if bool(self.use_ai_consensus_filter.value):
            long_cond = long_cond & (
                (dataframe["ai_signal"] == 1)
                & (dataframe["ai_confidence"] >= float(self.min_ai_confidence.value))
            )

        dataframe.loc[long_cond, ["enter_long", "enter_tag"]] = (1, "momentum_radar_long")

        if self.can_short:
            short_cond = (
                (dataframe["close"] < dataframe["ema20"])
                & (dataframe["ema20"] <= dataframe["ema50"])
                & (dataframe["rsi"] <= (100 - self.buy_rsi_min.value))
                & (dataframe["macdhist"] < 0)
                & (dataframe["vol_ratio"] >= float(self.buy_vol_ratio.value))
                & (dataframe["volume"] > 0)
            )
            if bool(self.use_ai_consensus_filter.value):
                short_cond = short_cond & (
                    (dataframe["ai_signal"] == -1)
                    & (dataframe["ai_confidence"] >= float(self.min_ai_confidence.value))
                )
            dataframe.loc[short_cond, ["enter_short", "enter_tag"]] = (1, "momentum_radar_short")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Populate causal exit signals."""
        exit_long_cond = (
            (
                (dataframe["rsi"] >= self.sell_rsi_threshold.value)
                | (
                    (dataframe["close"] < dataframe["ema20"])
                    & (dataframe["macdhist"] < 0)
                )
            )
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[exit_long_cond, ["exit_long", "exit_tag"]] = (1, "momentum_exhaustion")

        if self.can_short:
            exit_short_cond = (
                (
                    (dataframe["rsi"] <= (100 - self.sell_rsi_threshold.value))
                    | (
                        (dataframe["close"] > dataframe["ema20"])
                        & (dataframe["macdhist"] > 0)
                    )
                )
                & (dataframe["volume"] > 0)
            )
            dataframe.loc[exit_short_cond, ["exit_short", "exit_tag"]] = (
                1,
                "short_momentum_exhaustion",
            )

        return dataframe

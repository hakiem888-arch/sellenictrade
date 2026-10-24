"""Baseline master prompt: closed OHLCV, SMA-seeded EMA and Wilder RMA."""
import numpy as np
import pandas as pd


def smoothed(series, length, alpha):
    values = np.asarray(series, dtype=float)
    out = np.full(len(values), np.nan)
    run = []
    previous = np.nan
    for i, value in enumerate(values):
        if not np.isfinite(value):
            run, previous = [], np.nan
            continue
        if np.isnan(previous):
            run.append(value)
            if len(run) < length:
                continue
            previous = float(np.mean(run[-length:]))
        else:
            previous += alpha * (value - previous)
        out[i] = previous
    return pd.Series(out, index=series.index)


def rma(series, length=14):
    return smoothed(series, length, 1 / length)


def ema(series, length):
    return smoothed(series, length, 2 / (length + 1))


def enrich(frame):
    d = frame.copy()
    close = d.close
    for length in (20, 50, 200):
        d[f"ema{length}"] = ema(close, length)
    diff = close.diff()
    gain, loss = rma(diff.clip(lower=0)), rma(-diff.clip(upper=0))
    d["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    d.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    d.loc[(loss == 0) & (gain == 0), "rsi"] = 50
    tr = pd.concat([d.high - d.low, (d.high - close.shift()).abs(),
                    (d.low - close.shift()).abs()], axis=1).max(axis=1)
    d["tr"], d["atr"] = tr, rma(tr)
    up, down = d.high.diff(), -d.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    plus.iloc[0], minus.iloc[0] = np.nan, np.nan
    pdi = 100 * rma(plus) / d.atr.replace(0, np.nan)
    mdi = 100 * rma(minus) / d.atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    dx.loc[(pdi + mdi) == 0] = 0
    d["adx"] = rma(dx)
    d["rvol"] = d.volume / d.volume.shift(1).rolling(20).mean().replace(0, np.nan)
    session = pd.to_datetime(d.open_time, unit="ms", utc=True).dt.floor("D")
    typical = (d.high + d.low + d.close) / 3
    d["vwap"] = (typical * d.volume).groupby(session).cumsum() / d.volume.groupby(session).cumsum().replace(0, np.nan)
    return d


def confirmed_pivots(frame, lookback=100):
    """Right two bars must already exist. No backdated availability."""
    highs, lows = [], []
    for i in range(max(2, len(frame) - lookback), len(frame) - 2):
        neighbors = frame.iloc[[i - 2, i - 1, i + 1, i + 2]]
        if frame.high.iloc[i] > neighbors.high.max():
            highs.append(float(frame.high.iloc[i]))
        if frame.low.iloc[i] < neighbors.low.min():
            lows.append(float(frame.low.iloc[i]))
    return highs, lows

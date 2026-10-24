"""Deterministic technical scanner; a candidate is not an account-approved order."""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import pandas as pd
from indicators import enrich, confirmed_pivots

MODES = {"SCALPING": ("1h", "5m"), "INTRADAY": ("4h", "15m"), "SWING": ("1d", "4h")}
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
               "4h": 14_400_000, "1d": 86_400_000}
DAY = INTERVAL_MS["1d"]


def quantize(value, step, up=False):
    v, s = Decimal(str(value)), Decimal(str(step))
    if s <= 0:
        raise ValueError("Tick/step harus positif")
    return float((v / s).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * s)


@dataclass(frozen=True)
class Contract:
    symbol: str
    tick: float
    step: float
    min_qty: float
    min_notional: float
    max_qty: float = float("inf")
    market_step: float = 0.0
    market_min_qty: float = 0.0
    market_max_qty: float = float("inf")


@dataclass
class Result:
    symbol: str
    mode: str
    status: str = "WAIT"
    direction: str = "—"
    candidate: bool = False
    reasons: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    signal_close: int = 0
    checked_at: int = 0

    @property
    def key(self):
        return f"baseline-v1|{self.symbol}|{self.mode}|{self.direction}|{self.signal_close}"


def closed_frame(raw, interval, now, minimum=500):
    required = ["open_time", "close_time", "open", "high", "low", "close", "volume"]
    if raw is None or not set(required).issubset(raw.columns):
        raise ValueError(f"OHLCV {interval} tidak lengkap")
    d = raw.loc[raw.close_time < now, required].copy().reset_index(drop=True)
    if len(d) < minimum:
        raise ValueError(f"{interval}: perlu {minimum} candle tertutup; tersedia {len(d)}")
    if not all(math.isfinite(float(v)) for v in d.to_numpy().ravel()):
        raise ValueError(f"{interval}: nilai OHLCV tidak valid")
    width = INTERVAL_MS[interval]
    if (d.open_time.diff().dropna() != width).any() or (d.close_time != d.open_time + width - 1).any():
        raise ValueError(f"{interval}: candle duplikat, tidak urut, atau berlubang")
    if int(d.open_time.iloc[-1]) != (now // width - 1) * width:
        raise ValueError(f"{interval}: candle terakhir yang selesai belum tersedia")
    if (d[["open", "high", "low", "close"]] <= 0).any().any() or (d.volume < 0).any():
        raise ValueError(f"{interval}: harga/volume tidak valid")
    if ((d.high < d[["open", "close", "low"]].max(axis=1)) |
            (d.low > d[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError(f"{interval}: rentang OHLC tidak valid")
    return d


def build_levels(execution, bias, previous_day, contract, direction, mode):
    s = execution.iloc[-1]
    tick, atr = Decimal(str(contract.tick)), Decimal(str(float(s.atr)))
    long = direction == "LONG"
    trigger_d = Decimal(str(float(s.high if long else s.low))) + (tick if long else -tick)
    trigger = quantize(trigger_d, tick, up=long)
    limit = quantize(Decimal(str(trigger)) + (1 if long else -1) * Decimal("0.10") * atr,
                     tick, up=not long)
    structure = execution.low.iloc[-5:].min() if long else execution.high.iloc[-5:].max()
    stop = quantize(Decimal(str(float(structure))) + (-1 if long else 1) * Decimal("0.25") * atr,
                    tick, up=not long)
    distance = float(abs(Decimal(str(limit)) - Decimal(str(stop))))
    if distance <= 0 or (long and stop >= limit) or (not long and stop <= limit):
        raise ValueError("SL tidak berada di sisi struktur yang benar")
    sign = 1 if long else -1
    tp1 = quantize(Decimal(str(limit)) + sign * 2 * Decimal(str(distance)), tick, up=long)
    tp2 = quantize(Decimal(str(limit)) + sign * 3 * Decimal(str(distance)), tick, up=long)
    highs1, lows1 = confirmed_pivots(execution)
    highs2, lows2 = confirmed_pivots(bias)
    obstacles = (highs1 + highs2 + [float(previous_day.high)]) if long else (lows1 + lows2 + [float(previous_day.low)])
    ahead = [x for x in obstacles if x > limit] if long else [x for x in obstacles if x < limit]
    obstacle = (min(ahead) if long else max(ahead)) if ahead else None
    close_ms = int(s.close_time) + 1
    expiry = close_ms + 3 * INTERVAL_MS[MODES[mode][1]]
    if mode != "SWING":
        # Signal belongs to its opening-day session, including the last bar before midnight.
        expiry = min(expiry, (int(s.open_time) // DAY + 1) * DAY)
    return dict(trigger=trigger, entry=limit, stop=stop, distance=distance, atr=float(atr),
                tp1=tp1, tp2=tp2, obstacle=obstacle, expires_at=expiry)


def assess(symbol, mode, bias_raw, execution_raw, daily_raw, contract, now, quote=None):
    result = Result(symbol, mode, checked_at=now)
    def check(name, value, rule, passed):
        result.checks.append({"Kriteria": name, "Nilai": str(value), "Syarat": rule,
                              "Hasil": "LULUS" if passed else "GAGAL"})
        if not passed:
            result.reasons.append(name)
        return bool(passed)
    try:
        bt, et = MODES[mode]
        b = enrich(closed_frame(bias_raw, bt, now))
        e = enrich(closed_frame(execution_raw, et, now))
        daily = closed_frame(daily_raw, "1d", now, minimum=1)
    except (ValueError, KeyError, TypeError) as exc:
        result.reasons = [str(exc)]
        return result
    s, prev, trend = e.iloc[-1], e.iloc[-2], b.iloc[-1]
    result.signal_close = int(s.close_time)
    result.values = dict(rsi=float(s.rsi), adx=float(trend.adx), atr=float(s.atr),
                         rvol=float(s.rvol), vwap=float(s.vwap), close=float(s.close),
                         bias_ema50=float(trend.ema50), bias_ema200=float(trend.ema200),
                         ema20=float(s.ema20), ema50=float(s.ema50))
    if not all(math.isfinite(v) for v in result.values.values()) or s.atr <= 0:
        result.reasons = ["Indikator belum tersedia atau volume/ATR nol"]
        return result
    long = trend.close > trend.ema50 > trend.ema200 and trend.ema50 > b.ema50.iloc[-6]
    short = trend.close < trend.ema50 < trend.ema200 and trend.ema50 < b.ema50.iloc[-6]
    check("Bias EMA 50/200 + slope 5 candle", "LONG" if long else "SHORT" if short else "NETRAL", "tren searah", long or short)
    check("ADX bias", round(trend.adx, 3), ">= 20", trend.adx >= 20)
    if result.reasons:
        result.status = "NO TRADE"
        return result
    result.direction = "LONG" if long else "SHORT"
    check("EMA eksekusi", f"{s.ema20:.8g} / {s.ema50:.8g}", "20 > 50" if long else "20 < 50", s.ema20 > s.ema50 if long else s.ema20 < s.ema50)
    prior = e.iloc[-6:-1]
    touched = (prior.low <= prior.ema20 + .25 * prior.atr) & (prior.high >= prior.ema20 - .25 * prior.atr)
    check("Pullback 5 candle sebelum sinyal", int(touched.sum()), "minimal 1 irisan EMA20 ± 0,25 ATR", touched.any())
    intact = (prior.close >= prior.ema50).all() if long else (prior.close <= prior.ema50).all()
    check("Struktur pullback", bool(intact), "tidak close menembus EMA50 berlawanan", intact)
    confirm = (s.close > s.open and s.close > prev.high and s.close > s.ema20) if long else (s.close < s.open and s.close < prev.low and s.close < s.ema20)
    check("Candle konfirmasi", f"O {s.open:.8g} / C {s.close:.8g}", "bullish + break high sebelumnya" if long else "bearish + break low sebelumnya", confirm)
    check("RSI 14", round(s.rsi, 3), "50–65" if long else "35–50", 50 <= s.rsi <= 65 if long else 35 <= s.rsi <= 50)
    check("Volume relatif", round(s.rvol, 3), ">= 1,2; basis 20 candle sebelumnya", s.rvol >= 1.2)
    if mode != "SWING":
        check("VWAP 00.00 UTC", f"{s.vwap:.8g}", "close di atas" if long else "close di bawah", s.close > s.vwap if long else s.close < s.vwap)
    check("Lonjakan true range", round(s.tr / prev.atr, 3), "<= 2,5 × ATR sebelumnya", s.tr <= 2.5 * prev.atr)
    if result.reasons:
        result.status = "NO TRADE"
        return result
    result.levels = build_levels(e, b, daily.iloc[-1], contract, result.direction, mode)
    p = result.levels
    check("Jarak SL struktural", round(p["distance"] / s.atr, 4), "1–3 ATR", s.atr <= p["distance"] + 1e-12 and p["distance"] <= 3 * s.atr + 1e-12)
    room = abs(p["obstacle"] - p["entry"]) if p["obstacle"] is not None else None
    check("Ruang target", f"{room:.8g}" if room is not None else "tidak teridentifikasi dalam cakupan data", ">= 2D + 0,10 ATR", room is None or room + 1e-12 >= 2 * p["distance"] + .1 * s.atr)
    if result.reasons:
        result.status = "NO TRADE"
        return result
    if now >= p["expires_at"]:
        result.reasons = ["Setup kedaluwarsa / pergantian sesi UTC"]
        return result
    if quote is None:
        result.reasons = ["Bid/ask, mark, last belum tersedia"]
        return result
    for field in ("bid", "ask", "mark", "last"):
        if not math.isfinite(float(quote.get(field, 0))) or quote.get(field, 0) <= 0:
            result.reasons = ["Snapshot harga tidak lengkap"]
            return result
    times = [quote.get(k, 0) for k in ("book_time", "mark_time", "last_time", "fetched_at")]
    fresh = all(-1000 <= now - t <= 30_000 for t in times)
    check("Kesegaran snapshot", f"{max(now - t for t in times) / 1000:.1f} detik", "<= 30 detik", fresh)
    spread = quote["ask"] - quote["bid"]
    check("Spread", f"{spread:.8g}", "0–0,05 ATR", 0 <= spread <= .05 * s.atr)
    check("Deviasi Mark–Last", f"{abs(quote['mark'] - quote['last']):.8g}", "<= 0,25 ATR", abs(quote["mark"] - quote["last"]) <= .25 * s.atr)
    live = execution_raw[execution_raw.open_time > s.open_time]
    reached = quote["last"] >= p["trigger"] if long else quote["last"] <= p["trigger"]
    invalid = quote["last"] <= p["stop"] if long else quote["last"] >= p["stop"]
    if not live.empty:
        reached |= live.high.max() >= p["trigger"] if long else live.low.min() <= p["trigger"]
        invalid |= live.low.min() <= p["stop"] if long else live.high.max() >= p["stop"]
    check("Trigger belum tersentuh", quote["last"], "jangan mengejar trigger yang sudah dilewati", not reached)
    check("SL belum tersentuh sebelum entry", quote["last"], "setup belum batal", not invalid)
    result.values.update(last=quote["last"], mark=quote["mark"], spread=spread,
                         funding_rate=quote.get("funding_rate"), next_funding=quote.get("next_funding"))
    if result.reasons:
        result.status = "WAIT" if not fresh or reached or invalid else "NO TRADE"
        return result
    result.candidate = True
    result.status = "WAIT"
    result.reasons = ["Teknis lolos; verifikasi akun, biaya, likuidasi, likuiditas ukuran order, dan kalender berita"]
    return result

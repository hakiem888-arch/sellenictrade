"""
Deterministic Smart Money Concepts (SMC) engine with last-opposite Order Block selection,
historical FVG/OB lifecycle tracking (FRESH -> PARTIAL_FILL / MITIGATED -> INVALIDATED / EXPIRED),
pivot confirmation timestamps without look-ahead bias, overlap-aware zone quality,
and newest-first zone_id ordering for AI Consensus Trading (PH03-03 / F11, F12, F13, F14, F15, F37, T09..T12).
Strictly zero side-effects on import.
"""

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .market import extract_closed_df

SMC_ENGINE_VERSION: str = "v6.1-smc-proactive-lifecycle-1"


def pct_distance(a: float, b: float) -> float:
    """Compute percentage distance |a - b| / max(|b|, 1e-12) * 100."""
    return abs(float(a) - float(b)) / max(abs(float(b)), 1e-12) * 100.0


def compute_zone_boundary_distance_pct(price: float, zone_low: float, zone_high: float) -> float:
    """
    Compute percentage distance from price to the nearest zone boundary (T12):
    - 0.0 when zone_low <= price <= zone_high (inside zone)
    - pct_distance(price, zone_high) when price > zone_high
    - pct_distance(price, zone_low) when price < zone_low
    """
    lo = min(float(zone_low), float(zone_high))
    hi = max(float(zone_low), float(zone_high))
    p = float(price)
    if lo <= p <= hi:
        return 0.0
    if p > hi:
        return pct_distance(p, hi)
    return pct_distance(p, lo)


def compute_zone_overlap(
    ob: Optional[Dict[str, Any]],
    fvg: Optional[Dict[str, Any]],
    min_overlap_ratio: float = 0.0,
) -> Dict[str, Any]:
    """
    Compute actual price intersection between an Order Block and an FVG (F15, T11, T12).
    Only grants confluence when the intersection interval [inter_low, inter_high] is valid
    and satisfies the overlap threshold.
    """
    if not isinstance(ob, dict) or not isinstance(fvg, dict):
        return {
            "confluence": False,
            "overlap_low": None,
            "overlap_high": None,
            "overlap_ratio": 0.0,
        }
    if ob.get("direction") != fvg.get("direction"):
        return {
            "confluence": False,
            "overlap_low": None,
            "overlap_high": None,
            "overlap_ratio": 0.0,
        }

    ob_lo, ob_hi = min(float(ob["low"]), float(ob["high"])), max(float(ob["low"]), float(ob["high"]))
    fvg_lo, fvg_hi = min(float(fvg["low"]), float(fvg["high"])), max(float(fvg["low"]), float(fvg["high"]))

    inter_lo = max(ob_lo, fvg_lo)
    inter_hi = min(ob_hi, fvg_hi)
    if inter_lo > inter_hi:
        return {
            "confluence": False,
            "overlap_low": None,
            "overlap_high": None,
            "overlap_ratio": 0.0,
        }

    inter_width = max(0.0, inter_hi - inter_lo)
    ob_span = max(ob_hi - ob_lo, 1e-12)
    fvg_span = max(fvg_hi - fvg_lo, 1e-12)
    smaller_span = max(min(ob_span, fvg_span), 1e-12)
    overlap_ratio = round(min(1.0, inter_width / smaller_span), 4) if inter_width > 0 else 0.0

    has_confluence = bool(inter_lo <= inter_hi and overlap_ratio >= float(min_overlap_ratio) and inter_width >= 0.0)
    if min_overlap_ratio > 0.0 and overlap_ratio < float(min_overlap_ratio):
        has_confluence = False

    return {
        "confluence": has_confluence,
        "overlap_low": inter_lo if has_confluence else None,
        "overlap_high": inter_hi if has_confluence else None,
        "overlap_ratio": overlap_ratio if has_confluence else 0.0,
    }


def find_pivots(
    df: pd.DataFrame,
    left: int = 2,
    right: int = 2,
    lookback: int = 100,
    as_of: Any = None,
    interval: str = "15m",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Detect swing high and swing low pivots on closed candles without look-ahead bias (F37, T12).
    A pivot at index i is only confirmed once bar (i + right) has closed.
    """
    closed = extract_closed_df(df, as_of=as_of, interval=interval)
    if closed is None or len(closed) == 0:
        return [], []

    d = closed.tail(int(lookback)).copy()
    d["timestamp"] = d.index.astype(str)
    d = d.reset_index(drop=True)

    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    n = len(d)
    l_win = max(1, int(left))
    r_win = max(1, int(right))

    for i in range(l_win, n - r_win):
        h = float(d.loc[i, "High"])
        l = float(d.loc[i, "Low"])
        window_high = float(d.loc[i - l_win : i + r_win, "High"].max())
        window_low = float(d.loc[i - l_win : i + r_win, "Low"].min())
        created_ts = str(d.loc[i, "timestamp"])
        confirmed_idx = i + r_win
        confirmed_ts = str(d.loc[confirmed_idx, "timestamp"])

        prev_high_max = float(d.loc[i - l_win : i - 1, "High"].max())
        prev_low_min = float(d.loc[i - l_win : i - 1, "Low"].min())

        if h >= window_high and h > prev_high_max - 1e-12:
            highs.append({
                "i": i,
                "confirmed_i": confirmed_idx,
                "price": h,
                "time": created_ts,
                "created_at": created_ts,
                "confirmed_at": confirmed_ts,
            })
        if l <= window_low and l < prev_low_min + 1e-12:
            lows.append({
                "i": i,
                "confirmed_i": confirmed_idx,
                "price": l,
                "time": created_ts,
                "created_at": created_ts,
                "confirmed_at": confirmed_ts,
            })

    return highs, lows


def detect_structure(
    df: pd.DataFrame,
    as_of: Any = None,
    interval: str = "15m",
) -> Dict[str, Any]:
    """Detect market structure (bias, BOS, CHOCH, swing highs/lows, and pivot confirmation times) on closed bars."""
    d = extract_closed_df(df, as_of=as_of, interval=interval)
    if d is None or len(d) == 0:
        return {
            "bias": "RANGE",
            "bos": "NONE",
            "choch": "NONE",
            "last_swing_high": 0.0,
            "last_swing_low": 0.0,
            "previous_swing_high": 0.0,
            "previous_swing_low": 0.0,
            "last_swing_high_confirmed_at": None,
            "last_swing_low_confirmed_at": None,
        }

    highs, lows = find_pivots(d, left=2, right=2, lookback=120, as_of=None, interval=interval)
    close = float(d.Close.iloc[-1])
    last_high = highs[-1]["price"] if highs else float(d.High.tail(20).max())
    prev_high = highs[-2]["price"] if len(highs) >= 2 else last_high
    last_low = lows[-1]["price"] if lows else float(d.Low.tail(20).min())
    prev_low = lows[-2]["price"] if len(lows) >= 2 else last_low

    bos = "NONE"
    if close > last_high:
        bos = "BULLISH BOS"
    elif close < last_low:
        bos = "BEARISH BOS"

    if last_high > prev_high and last_low > prev_low:
        bias = "BULLISH"
    elif last_high < prev_high and last_low < prev_low:
        bias = "BEARISH"
    elif bos == "BULLISH BOS":
        bias = "BULLISH"
    elif bos == "BEARISH BOS":
        bias = "BEARISH"
    elif "EMA20" in d.columns and "EMA50" in d.columns and pd.notna(d.EMA20.iloc[-1]) and pd.notna(d.EMA50.iloc[-1]):
        ema20_v = float(d.EMA20.iloc[-1])
        ema50_v = float(d.EMA50.iloc[-1])
        mid_swing = (last_high + last_low) / 2.0
        if close >= ema20_v and ema20_v >= ema50_v:
            bias = "BULLISH"
        elif close <= ema20_v and ema20_v <= ema50_v:
            bias = "BEARISH"
        elif close >= ema20_v and close >= mid_swing:
            bias = "BULLISH"
        elif close <= ema20_v and close <= mid_swing:
            bias = "BEARISH"
        else:
            bias = "RANGE"
    else:
        bias = "RANGE"

    prior_bias = "RANGE"
    if len(highs) >= 3 and len(lows) >= 3:
        hh = highs[-2]["price"] > highs[-3]["price"]
        hl = lows[-2]["price"] > lows[-3]["price"]
        lh = highs[-2]["price"] < highs[-3]["price"]
        ll = lows[-2]["price"] < lows[-3]["price"]
        if hh and hl:
            prior_bias = "BULLISH"
        elif lh and ll:
            prior_bias = "BEARISH"

    choch = "NONE"
    if bos == "BULLISH BOS" and prior_bias == "BEARISH":
        choch = "BULLISH CHOCH"
    elif bos == "BEARISH BOS" and prior_bias == "BULLISH":
        choch = "BEARISH CHOCH"

    return {
        "bias": bias,
        "bos": bos,
        "choch": choch,
        "last_swing_high": last_high,
        "last_swing_low": last_low,
        "previous_swing_high": prev_high,
        "previous_swing_low": prev_low,
        "last_swing_high_confirmed_at": highs[-1]["confirmed_at"] if highs else None,
        "last_swing_low_confirmed_at": lows[-1]["confirmed_at"] if lows else None,
    }


def detect_liquidity(
    df: pd.DataFrame,
    as_of: Any = None,
    interval: str = "15m",
) -> Dict[str, Any]:
    """Detect buy-side/sell-side liquidity sweeps and equal highs/lows on closed bars."""
    d = extract_closed_df(df, as_of=as_of, interval=interval)
    if d is None or len(d) < 25:
        return {"sweep_high": False, "sweep_low": False, "equal_high": None, "equal_low": None}

    c = d.iloc[-1]
    look = d.iloc[-21:-1]
    prev_high = float(look.High.max())
    prev_low = float(look.Low.min())

    sweep_high = float(c.High) > prev_high and float(c.Close) < prev_high
    sweep_low = float(c.Low) < prev_low and float(c.Close) > prev_low

    eq_high = None
    eq_low = None
    recent_highs = sorted([float(x) for x in look.High.tail(15)], reverse=True)
    recent_lows = sorted([float(x) for x in look.Low.tail(15)])
    if len(recent_highs) >= 2 and pct_distance(recent_highs[0], recent_highs[1]) <= 0.15:
        eq_high = sum(recent_highs[:2]) / 2.0
    if len(recent_lows) >= 2 and pct_distance(recent_lows[0], recent_lows[1]) <= 0.15:
        eq_low = sum(recent_lows[:2]) / 2.0

    return {
        "sweep_high": bool(sweep_high),
        "sweep_low": bool(sweep_low),
        "equal_high": eq_high,
        "equal_low": eq_low,
        "recent_high": prev_high,
        "recent_low": prev_low,
    }


def detect_fvgs(
    df: pd.DataFrame,
    max_age: int = 80,
    as_of: Any = None,
    interval: str = "15m",
    max_zones: int = 12,
) -> List[Dict[str, Any]]:
    """
    Detect Fair Value Gaps (FVGs) and track their full historical lifecycle across all subsequent closed bars
    (F11, F13, F14, T10, T11).
    """
    closed = extract_closed_df(df, as_of=as_of, interval=interval)
    if closed is None or len(closed) < 3:
        return []

    d = closed.tail(int(max_age) + 15).copy()
    d["timestamp"] = d.index.astype(str)
    d = d.reset_index(drop=True)
    n = len(d)

    zones: List[Dict[str, Any]] = []
    for i in range(2, n):
        c1 = d.iloc[i - 2]
        c3 = d.iloc[i]
        c1_hi = float(c1.High)
        c1_lo = float(c1.Low)
        c3_hi = float(c3.High)
        c3_lo = float(c3.Low)

        direction: Optional[str] = None
        z_low = 0.0
        z_high = 0.0
        if c3_lo > c1_hi:
            direction = "BULLISH"
            z_low = c1_hi
            z_high = c3_lo
        elif c3_hi < c1_lo:
            direction = "BEARISH"
            z_low = c3_hi
            z_high = c1_lo

        if direction is None:
            continue

        created_ts = str(d.iloc[i - 1]["timestamp"])
        confirmed_ts = str(c3["timestamp"])
        age = n - 1 - i
        width = max(z_high - z_low, 1e-12)

        state = "FRESH"
        fill_pct = 0.0
        first_touch_at: Optional[str] = None
        mitigated_at: Optional[str] = None
        invalidated_at: Optional[str] = None

        for k in range(i + 1, n):
            bar = d.iloc[k]
            b_lo = float(bar.Low)
            b_hi = float(bar.High)
            b_cl = float(bar.Close)
            b_ts = str(bar["timestamp"])

            if direction == "BULLISH":
                if b_lo <= z_high:
                    if first_touch_at is None:
                        first_touch_at = b_ts
                    pen = min(width, max(0.0, z_high - b_lo))
                    fill_pct = max(fill_pct, round((pen / width) * 100.0, 2))
                    if b_cl < z_low:
                        state = "INVALIDATED"
                        fill_pct = 100.0
                        if mitigated_at is None:
                            mitigated_at = b_ts
                        if invalidated_at is None:
                            invalidated_at = b_ts
                    elif b_lo <= z_low:
                        fill_pct = 100.0
                        if state != "INVALIDATED":
                            state = "MITIGATED"
                        if mitigated_at is None:
                            mitigated_at = b_ts
                    else:
                        if state == "FRESH":
                            state = "PARTIAL_FILL"
            else:
                if b_hi >= z_low:
                    if first_touch_at is None:
                        first_touch_at = b_ts
                    pen = min(width, max(0.0, b_hi - z_low))
                    fill_pct = max(fill_pct, round((pen / width) * 100.0, 2))
                    if b_cl > z_high:
                        state = "INVALIDATED"
                        fill_pct = 100.0
                        if mitigated_at is None:
                            mitigated_at = b_ts
                        if invalidated_at is None:
                            invalidated_at = b_ts
                    elif b_hi >= z_high:
                        fill_pct = 100.0
                        if state != "INVALIDATED":
                            state = "MITIGATED"
                        if mitigated_at is None:
                            mitigated_at = b_ts
                    else:
                        if state == "FRESH":
                            state = "PARTIAL_FILL"

        expired = False
        if age > int(max_age) and state not in ("INVALIDATED", "MITIGATED"):
            state = "EXPIRED"
            expired = True

        filled_flag = state in ("MITIGATED", "INVALIDATED")
        invalidated_flag = state == "INVALIDATED"
        zone_id = f"FVG_{direction}_{confirmed_ts}_{z_low:.8f}_{z_high:.8f}"

        zones.append({
            "zone_id": zone_id,
            "kind": "FVG",
            "direction": direction,
            "low": z_low,
            "high": z_high,
            "created_at": confirmed_ts,
            "imbalance_bar_time": created_ts,
            "confirmed_at": confirmed_ts,
            "age": age,
            "state": state,
            "filled": filled_flag,
            "mitigated": filled_flag,
            "invalidated": invalidated_flag,
            "expired": expired,
            "fill_pct": fill_pct,
            "first_touch_at": first_touch_at,
            "mitigated_at": mitigated_at,
            "invalidated_at": invalidated_at,
            "size_pct": pct_distance(z_high, z_low),
        })

    newest_first = list(reversed(zones))
    return newest_first[: int(max_zones)] if max_zones else newest_first


def detect_order_blocks(
    df: pd.DataFrame,
    max_age: int = 80,
    as_of: Any = None,
    interval: str = "15m",
    max_zones: int = 12,
) -> List[Dict[str, Any]]:
    """
    Detect Order Blocks (OBs) selecting the LAST opposite candle before displacement (F12, T09)
    and track their historical lifecycle across all subsequent closed candles (F13, F14, T10, T11).
    """
    closed = extract_closed_df(df, as_of=as_of, interval=interval)
    if closed is None or len(closed) < 3:
        return []

    d = closed.tail(int(max_age) + 15).copy()
    d["timestamp"] = d.index.astype(str)
    d = d.reset_index(drop=True)
    n = len(d)

    zones: List[Dict[str, Any]] = []
    for i in range(2, n):
        c = d.iloc[i]
        atr_val = float(c.ATR) if ("ATR" in d.columns and pd.notna(c.ATR)) else 0.0
        if atr_val <= 0.0:
            continue

        c_open = float(c.Open)
        c_close = float(c.Close)
        c_high = float(c.High)
        c_low = float(c.Low)
        rng = max(c_high - c_low, 1e-12)
        body = abs(c_close - c_open)
        body_ratio = body / rng
        vol_ratio = float(c.VOL_RATIO) if ("VOL_RATIO" in d.columns and pd.notna(c.VOL_RATIO)) else 0.0

        displacement = body >= 1.15 * atr_val and body_ratio >= 0.55 and vol_ratio >= 1.15
        if not displacement:
            continue

        selected_j: Optional[int] = None
        direction: Optional[str] = None
        z_low = 0.0
        z_high = 0.0

        for j in range(i - 1, max(-1, i - 4), -1):
            prev = d.iloc[j]
            p_open = float(prev.Open)
            p_close = float(prev.Close)
            if c_close > c_open and p_close < p_open:
                selected_j = j
                direction = "BULLISH"
                z_low = float(prev.Low)
                z_high = p_open
                break
            if c_close < c_open and p_close > p_open:
                selected_j = j
                direction = "BEARISH"
                z_low = p_open
                z_high = float(prev.High)
                break

        if selected_j is None or direction is None:
            continue

        ob_candle = d.iloc[selected_j]
        ob_created_ts = str(ob_candle["timestamp"])
        disp_confirmed_ts = str(c["timestamp"])
        age = n - 1 - i

        state = "FRESH"
        first_touch_at: Optional[str] = None
        mitigated_at: Optional[str] = None
        invalidated_at: Optional[str] = None

        for k in range(i + 1, n):
            bar = d.iloc[k]
            b_lo = float(bar.Low)
            b_hi = float(bar.High)
            b_cl = float(bar.Close)
            b_ts = str(bar["timestamp"])

            if direction == "BULLISH":
                if b_lo <= z_high:
                    if first_touch_at is None:
                        first_touch_at = b_ts
                    if b_cl < z_low:
                        state = "INVALIDATED"
                        if mitigated_at is None:
                            mitigated_at = b_ts
                        if invalidated_at is None:
                            invalidated_at = b_ts
                    else:
                        if state == "FRESH":
                            state = "MITIGATED"
                        if mitigated_at is None:
                            mitigated_at = b_ts
            else:
                if b_hi >= z_low:
                    if first_touch_at is None:
                        first_touch_at = b_ts
                    if b_cl > z_high:
                        state = "INVALIDATED"
                        if mitigated_at is None:
                            mitigated_at = b_ts
                        if invalidated_at is None:
                            invalidated_at = b_ts
                    else:
                        if state == "FRESH":
                            state = "MITIGATED"
                        if mitigated_at is None:
                            mitigated_at = b_ts

        expired = False
        if age > int(max_age) and state != "INVALIDATED":
            state = "EXPIRED"
            expired = True

        zone_id = f"OB_{direction}_{disp_confirmed_ts}_{z_low:.8f}_{z_high:.8f}"
        zones.append({
            "zone_id": zone_id,
            "kind": "OB",
            "direction": direction,
            "low": z_low,
            "high": z_high,
            "ob_candle_time": ob_created_ts,
            "created_at": disp_confirmed_ts,
            "confirmed_at": disp_confirmed_ts,
            "age": age,
            "displacement": True,
            "volume_ratio": vol_ratio,
            "state": state,
            "mitigated": state in ("MITIGATED", "INVALIDATED"),
            "invalidated": state == "INVALIDATED",
            "expired": expired,
            "first_touch_at": first_touch_at,
            "mitigated_at": mitigated_at,
            "invalidated_at": invalidated_at,
        })

    newest_first = list(reversed(zones))
    return newest_first[: int(max_zones)] if max_zones else newest_first


def compute_zone_quality(
    zone: Dict[str, Any],
    fvg: Optional[Dict[str, Any]] = None,
    structure: Optional[Dict[str, Any]] = None,
    liquidity: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Compute heuristic setup quality (0..100) for a zone (F15, T11, T12).
    Only awards the FVG confluence bonus when the OB and FVG actually overlap in price!
    """
    if not isinstance(zone, dict):
        return 0
    if zone.get("invalidated") or zone.get("expired") or zone.get("state") in ("INVALIDATED", "EXPIRED"):
        return 0

    score = 45.0
    if zone.get("displacement"):
        score += 20.0
    if float(zone.get("volume_ratio", 0.0) or 0.0) >= 1.5:
        score += 10.0

    if isinstance(fvg, dict) and not fvg.get("invalidated") and not fvg.get("expired") and not fvg.get("filled"):
        if zone.get("kind") == "FVG" or ("low" not in zone or "high" not in zone):
            score += 10.0
        else:
            ov = compute_zone_overlap(zone, fvg)
            if ov["confluence"]:
                score += 15.0

    if isinstance(structure, dict):
        if zone.get("direction") == "BULLISH" and structure.get("bias") == "BULLISH":
            score += 10.0
        elif zone.get("direction") == "BEARISH" and structure.get("bias") == "BEARISH":
            score += 10.0

    if isinstance(liquidity, dict):
        if zone.get("direction") == "BULLISH" and liquidity.get("sweep_low"):
            score += 10.0
        if zone.get("direction") == "BEARISH" and liquidity.get("sweep_high"):
            score += 10.0

    return int(max(0, min(100, round(score))))


def _is_ob_eligible(ob: Dict[str, Any]) -> bool:
    if not isinstance(ob, dict):
        return False
    if ob.get("invalidated") or ob.get("expired"):
        return False
    return str(ob.get("state", "FRESH")).upper() not in ("INVALIDATED", "EXPIRED")


def _is_fvg_eligible(fvg: Dict[str, Any]) -> bool:
    if not isinstance(fvg, dict):
        return False
    if fvg.get("filled") or fvg.get("invalidated") or fvg.get("expired"):
        return False
    return str(fvg.get("state", "FRESH")).upper() not in ("MITIGATED", "INVALIDATED", "EXPIRED")


def _ensure_zone_id(z: Dict[str, Any], prefix: str, idx: int) -> Dict[str, Any]:
    out = dict(z)
    if not out.get("zone_id"):
        direction = out.get("direction", "UNKNOWN")
        ts = out.get("created_at", f"idx{idx}")
        lo = float(out.get("low", 0.0) or 0.0)
        hi = float(out.get("high", 0.0) or 0.0)
        out["zone_id"] = f"{prefix}_{direction}_{ts}_{lo:.8f}_{hi:.8f}"
    return out


def build_entry_context(
    df: pd.DataFrame,
    structure: Dict[str, Any],
    liquidity: Dict[str, Any],
    obs: List[Dict[str, Any]],
    fvgs: List[Dict[str, Any]],
    as_of: Any = None,
    interval: str = "15m",
    max_display_zones: int = 8,
) -> Dict[str, Any]:
    """
    Build directional entry context from newest-first OBs and FVGs (F11, F13, F14, F15, T10, T11, T12)
    plus multi-factor momentum/structure trigger evaluation for live market responsiveness.
    """
    d = extract_closed_df(df, as_of=as_of, interval=interval)
    if d is None or len(d) == 0:
        empty_cand = {
            "direction": "NONE",
            "zone_low": None,
            "zone_high": None,
            "quality": 0,
            "confluence": False,
            "overlap_ratio": 0.0,
            "status": "NO_ZONE",
            "distance_pct": None,
            "selected_ob_id": None,
            "selected_fvg_id": None,
            "selected_zone_id": None,
        }
        return {
            "structure": structure,
            "liquidity": liquidity,
            "order_blocks": [],
            "fvgs": [],
            "bullish_entry": {**empty_cand, "direction": "BULLISH"},
            "bearish_entry": {**empty_cand, "direction": "BEARISH"},
            "bullish_trigger": False,
            "bearish_trigger": False,
            "timing": "WAIT_RETEST",
            "smc_score": 0,
            "directional_scores": {"LONG": 0, "SHORT": 0},
            "displacement": {"body_ratio": 0.0, "volume_ratio": 0.0, "bullish": False, "bearish": False},
        }

    price = float(d.Close.iloc[-1])
    atr_value = float(d.ATR.iloc[-1]) if ("ATR" in d.columns and pd.notna(d.ATR.iloc[-1])) else 0.0
    has_live_indicators = bool("EMA20" in d.columns and "EMA50" in d.columns and "RSI" in d.columns)

    norm_obs = [_ensure_zone_id(z, "OB", idx) for idx, z in enumerate(obs or [])]
    norm_fvgs = [_ensure_zone_id(z, "FVG", idx) for idx, z in enumerate(fvgs or [])]

    display_obs = norm_obs[: int(max_display_zones)]
    display_fvgs = norm_fvgs[: int(max_display_zones)]

    bull_obs = [x for x in display_obs if x.get("direction") == "BULLISH" and _is_ob_eligible(x)]
    bear_obs = [x for x in display_obs if x.get("direction") == "BEARISH" and _is_ob_eligible(x)]
    bull_fvgs = [x for x in display_fvgs if x.get("direction") == "BULLISH" and _is_fvg_eligible(x)]
    bear_fvgs = [x for x in display_fvgs if x.get("direction") == "BEARISH" and _is_fvg_eligible(x)]

    bull_obs.sort(key=lambda z: (0 if z.get("state", "FRESH") == "FRESH" else 1, int(z.get("age", 0))))
    bear_obs.sort(key=lambda z: (0 if z.get("state", "FRESH") == "FRESH" else 1, int(z.get("age", 0))))

    def candidate(direction: str, obs_list: List[Dict[str, Any]], fvg_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        ob = obs_list[0] if obs_list else None
        fvg = fvg_list[0] if fvg_list else None

        best_overlap = {"confluence": False, "overlap_low": None, "overlap_high": None, "overlap_ratio": 0.0}
        for cand_ob in obs_list:
            for cand_fvg in fvg_list:
                ov = compute_zone_overlap(cand_ob, cand_fvg)
                if ov["confluence"]:
                    ob = cand_ob
                    fvg = cand_fvg
                    best_overlap = ov
                    break
            if best_overlap["confluence"]:
                break

        zone: Optional[Tuple[float, float]] = None
        confluence = False
        overlap_ratio = 0.0
        selected_ob_id: Optional[str] = None
        selected_fvg_id: Optional[str] = None

        if best_overlap["confluence"] and ob is not None and fvg is not None:
            zone = (float(best_overlap["overlap_low"]), float(best_overlap["overlap_high"]))
            confluence = True
            overlap_ratio = float(best_overlap["overlap_ratio"])
            selected_ob_id = ob.get("zone_id")
            selected_fvg_id = fvg.get("zone_id")
        elif ob is not None:
            zone = (float(ob["low"]), float(ob["high"]))
            selected_ob_id = ob.get("zone_id")
        elif fvg is not None:
            zone = (float(fvg["low"]), float(fvg["high"]))
            selected_fvg_id = fvg.get("zone_id")

        if zone is None:
            if has_live_indicators and atr_value > 0.0:
                ema20_val = float(d.EMA20.iloc[-1]) if pd.notna(d.EMA20.iloc[-1]) else price
                anchor = min(price, ema20_val) if direction == "BULLISH" else max(price, ema20_val)
                dyn_lo = anchor - 0.35 * atr_value if direction == "BULLISH" else anchor - 0.15 * atr_value
                dyn_hi = anchor + 0.15 * atr_value if direction == "BULLISH" else anchor + 0.35 * atr_value
                dyn_lo, dyn_hi = min(dyn_lo, dyn_hi), max(dyn_lo, dyn_hi)
                in_dyn = dyn_lo <= price <= dyn_hi
                dyn_q = 65 if structure.get("bias") == direction else 52
                return {
                    "direction": direction,
                    "zone_low": round(dyn_lo, 8),
                    "zone_high": round(dyn_hi, 8),
                    "quality": dyn_q,
                    "confluence": False,
                    "overlap_ratio": 0.0,
                    "status": "IN_ZONE" if in_dyn else "NEAR_ZONE",
                    "distance_pct": compute_zone_boundary_distance_pct(price, dyn_lo, dyn_hi),
                    "selected_ob_id": None,
                    "selected_fvg_id": None,
                    "selected_zone_id": f"DYN_EMA_{direction}",
                }
            return {
                "direction": direction,
                "zone_low": None,
                "zone_high": None,
                "quality": 0,
                "confluence": False,
                "overlap_ratio": 0.0,
                "status": "NO_ZONE",
                "distance_pct": None,
                "selected_ob_id": None,
                "selected_fvg_id": None,
                "selected_zone_id": None,
            }

        lo, hi = min(zone), max(zone)
        quality = compute_zone_quality(
            ob if ob is not None else {**(fvg or {}), "direction": direction, "kind": "FVG"},
            fvg if confluence else None,
            structure,
            liquidity,
        )
        if confluence:
            quality = min(100, quality + 10)

        late_limit = max(atr_value * 3.0 if has_live_indicators else atr_value * 0.75, price * (0.015 if has_live_indicators else 0.002))
        if lo <= price <= hi:
            status = "IN_ZONE"
        elif direction == "BULLISH" and price > hi:
            status = "LATE" if price - hi > late_limit else "WAIT_RETEST"
        elif direction == "BEARISH" and price < lo:
            status = "LATE" if lo - price > late_limit else "WAIT_RETEST"
        else:
            status = "WAIT_RETEST"

        distance_pct = compute_zone_boundary_distance_pct(price, lo, hi)
        primary_zone_id = selected_ob_id or selected_fvg_id

        return {
            "direction": direction,
            "zone_low": lo,
            "zone_high": hi,
            "quality": quality,
            "confluence": confluence,
            "overlap_ratio": overlap_ratio,
            "status": status,
            "distance_pct": distance_pct,
            "selected_ob_id": selected_ob_id,
            "selected_fvg_id": selected_fvg_id,
            "selected_zone_id": primary_zone_id,
        }

    bull = candidate("BULLISH", bull_obs, bull_fvgs)
    bear = candidate("BEARISH", bear_obs, bear_fvgs)

    last = d.iloc[-1]
    body = abs(float(last.Close) - float(last.Open))
    rng = max(float(last.High) - float(last.Low), 1e-12)
    body_ratio = body / rng
    vol_ratio = float(last.VOL_RATIO) if ("VOL_RATIO" in d.columns and pd.notna(last.VOL_RATIO)) else 0.0

    classic_bull_trigger = (
        bull["status"] == "IN_ZONE"
        and (
            liquidity.get("sweep_low")
            or (float(last.Close) > float(last.Open) and body_ratio >= 0.45)
        )
        and vol_ratio >= 1.05
    )
    classic_bear_trigger = (
        bear["status"] == "IN_ZONE"
        and (
            liquidity.get("sweep_high")
            or (float(last.Close) < float(last.Open) and body_ratio >= 0.45)
        )
        and vol_ratio >= 1.05
    )

    bull_signal_score = float(bull["quality"])
    bear_signal_score = float(bear["quality"])

    if has_live_indicators:
        ema20_v = float(last.EMA20) if pd.notna(last.EMA20) else price
        ema50_v = float(last.EMA50) if pd.notna(last.EMA50) else price
        rsi_v = float(last.RSI) if pd.notna(last.RSI) else 50.0
        macd_h = float(last.MACD_HIST) if ("MACD_HIST" in d.columns and pd.notna(last.MACD_HIST)) else 0.0
        buy_p = float(last.BUY_PRESSURE) if ("BUY_PRESSURE" in d.columns and pd.notna(last.BUY_PRESSURE)) else 0.5

        m_bull = 0.0
        m_bear = 0.0
        if structure.get("bias") == "BULLISH":
            m_bull += 22.0
        elif structure.get("bias") == "BEARISH":
            m_bear += 22.0

        if structure.get("bos") == "BULLISH BOS" or structure.get("choch") == "BULLISH CHOCH":
            m_bull += 14.0
        elif structure.get("bos") == "BEARISH BOS" or structure.get("choch") == "BEARISH CHOCH":
            m_bear += 14.0

        if price >= ema20_v:
            m_bull += 12.0
        else:
            m_bear += 12.0

        if ema20_v >= ema50_v:
            m_bull += 12.0
        else:
            m_bear += 12.0

        if macd_h > 0:
            m_bull += 8.0
        elif macd_h < 0:
            m_bear += 8.0

        if 51.0 <= rsi_v <= 74.0:
            m_bull += 10.0
        elif 26.0 <= rsi_v <= 49.0:
            m_bear += 10.0

        if buy_p >= 0.52:
            m_bull += 8.0
        elif buy_p <= 0.48:
            m_bear += 8.0

        if float(last.Close) > float(last.Open):
            m_bull += 6.0
        elif float(last.Close) < float(last.Open):
            m_bear += 6.0

        if liquidity.get("sweep_low"):
            m_bull += 12.0
        if liquidity.get("sweep_high"):
            m_bear += 12.0

        bull_signal_score = max(bull_signal_score, min(100.0, round(m_bull)))
        bear_signal_score = max(bear_signal_score, min(100.0, round(m_bear)))

        proactive_bull = bool(m_bull >= m_bear + 6.0 and m_bull >= 36.0 and bull["status"] != "LATE")
        proactive_bear = bool(m_bear >= m_bull + 6.0 and m_bear >= 36.0 and bear["status"] != "LATE")
        bullish_trigger = bool(classic_bull_trigger or proactive_bull)
        bearish_trigger = bool(classic_bear_trigger or proactive_bear)
        if bullish_trigger and bearish_trigger:
            if m_bull >= m_bear:
                bearish_trigger = False
            else:
                bullish_trigger = False
    else:
        bullish_trigger = bool(classic_bull_trigger)
        bearish_trigger = bool(classic_bear_trigger)

    if bullish_trigger:
        timing = "READY_LONG"
    elif bearish_trigger:
        timing = "READY_SHORT"
    elif bull["status"] == "LATE" or bear["status"] == "LATE":
        timing = "LATE"
    else:
        timing = "WAIT_RETEST"

    smc_score = int(max(bull_signal_score, bear_signal_score, bull["quality"], bear["quality"]))

    return {
        "structure": structure,
        "liquidity": liquidity,
        "order_blocks": display_obs,
        "fvgs": display_fvgs,
        "bullish_entry": bull,
        "bearish_entry": bear,
        "bullish_trigger": bool(bullish_trigger),
        "bearish_trigger": bool(bearish_trigger),
        "timing": timing,
        "smc_score": int(smc_score),
        "directional_scores": {
            "LONG": int(bull_signal_score),
            "SHORT": int(bear_signal_score),
        },
        "displacement": {
            "body_ratio": body_ratio,
            "volume_ratio": vol_ratio,
            "bullish": bool(float(last.Close) > float(last.Open) and body_ratio >= 0.55 and vol_ratio >= 1.15),
            "bearish": bool(float(last.Close) < float(last.Open) and body_ratio >= 0.55 and vol_ratio >= 1.15),
        },
        "engine_version": SMC_ENGINE_VERSION,
    }


def smc_analysis(
    df: pd.DataFrame,
    as_of: Any = None,
    interval: str = "15m",
) -> Dict[str, Any]:
    """Run full deterministic SMC analysis on closed candles up to as_of without look-ahead bias."""
    closed = extract_closed_df(df, as_of=as_of, interval=interval)
    structure = detect_structure(closed, as_of=None, interval=interval)
    liquidity = detect_liquidity(closed, as_of=None, interval=interval)
    fvgs = detect_fvgs(closed, as_of=None, interval=interval)
    obs = detect_order_blocks(closed, as_of=None, interval=interval)
    return build_entry_context(closed, structure, liquidity, obs, fvgs, as_of=None, interval=interval)

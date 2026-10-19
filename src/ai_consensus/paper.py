import copy
"""
Realistic paper trading ledger, position lifecycle state machine, OHLC candle-path
simulator since opened_at, intrabar TP/SL tie-breaker policy, gap-open fill clamping,
funding cost modeling, walk-forward train/validation/out-of-sample evaluation,
multi-timeframe causal alignment, and segmented performance/calibration analytics
for AI Consensus Trading (Phase 2 / PH06-01..PH06-02 / F04, F33, F36 / T23, T24, T25).
Strictly zero UI, DB, or network side-effects on import.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from .domain import is_finite_number
from .market import (
    INTERVAL_SECONDS,
    align_higher_timeframes_as_of,
    extract_closed_df,
    filter_bars_since,
    normalize_as_of_ms,
)

PAPER_ENGINE_VERSION: str = "v6.2-realistic-paper-backtest-1"
REALISTIC_ACCOUNTING_MODE: str = "V6_REALISTIC_PAPER_LEDGER"
REALISTIC_ACCOUNTING_QUALITY: str = "v6_realistic_ledger"


class PaperPositionState(str, Enum):
    """Explicit lifecycle states for signals and paper positions (F04, F33, T23)."""
    OBSERVED_WAIT = "OBSERVED_WAIT"            # WAIT observation — never an open position
    PENDING_ENTRY = "PENDING_ENTRY"            # Armed limit/zone order awaiting fill before expiry
    OPEN = "OPEN"                              # Active filled paper position with reserved margin/risk
    CLOSED_TP = "CLOSED_TP"                    # Closed at Take Profit
    CLOSED_SL = "CLOSED_SL"                    # Closed at Stop Loss
    CLOSED_TIME_STOP = "CLOSED_TIME_STOP"      # Closed at bar Close due to max_hold_bars / time_stop
    CLOSED_INVALIDATED = "CLOSED_INVALIDATED"  # Closed/aborted due to setup invalidation
    EXPIRED_UNFILLED = "EXPIRED_UNFILLED"      # Pending entry expired without filling
    CANCELLED = "CANCELLED"                    # Explicitly cancelled before exit


TERMINAL_POSITION_STATES = frozenset({
    PaperPositionState.OBSERVED_WAIT.value,
    PaperPositionState.CLOSED_TP.value,
    PaperPositionState.CLOSED_SL.value,
    PaperPositionState.CLOSED_TIME_STOP.value,
    PaperPositionState.CLOSED_INVALIDATED.value,
    PaperPositionState.EXPIRED_UNFILLED.value,
    PaperPositionState.CANCELLED.value,
})

ACTIVE_POSITION_STATES = frozenset({
    PaperPositionState.PENDING_ENTRY.value,
    PaperPositionState.OPEN.value,
})

CLOSED_TRADE_STATES = frozenset({
    PaperPositionState.CLOSED_TP.value,
    PaperPositionState.CLOSED_SL.value,
    PaperPositionState.CLOSED_TIME_STOP.value,
    PaperPositionState.CLOSED_INVALIDATED.value,
})

ALLOWED_POSITION_TRANSITIONS: Dict[str, frozenset] = {
    PaperPositionState.OBSERVED_WAIT.value: frozenset(),
    PaperPositionState.PENDING_ENTRY.value: frozenset({
        PaperPositionState.OPEN.value,
        PaperPositionState.EXPIRED_UNFILLED.value,
        PaperPositionState.CLOSED_INVALIDATED.value,
        PaperPositionState.CANCELLED.value,
    }),
    PaperPositionState.OPEN.value: frozenset({
        PaperPositionState.CLOSED_TP.value,
        PaperPositionState.CLOSED_SL.value,
        PaperPositionState.CLOSED_TIME_STOP.value,
        PaperPositionState.CLOSED_INVALIDATED.value,
        PaperPositionState.CANCELLED.value,
    }),
    PaperPositionState.CLOSED_TP.value: frozenset(),
    PaperPositionState.CLOSED_SL.value: frozenset(),
    PaperPositionState.CLOSED_TIME_STOP.value: frozenset(),
    PaperPositionState.CLOSED_INVALIDATED.value: frozenset(),
    PaperPositionState.EXPIRED_UNFILLED.value: frozenset(),
    PaperPositionState.CANCELLED.value: frozenset(),
}


class IntrabarTieBreaker(str, Enum):
    """Deterministic tie-breaker when a single candle touches both TP and SL (F04, T23)."""
    SL_FIRST_CONSERVATIVE = "SL_FIRST_CONSERVATIVE"
    TP_FIRST = "TP_FIRST"
    OPEN_PROXIMITY = "OPEN_PROXIMITY"


def validate_position_transition(from_state: str, to_state: str) -> bool:
    """
    Validate that transitioning from `from_state` to `to_state` is legal in the state machine.
    Raises ValueError on any illegal transition.
    """
    src = str(from_state or "").upper().strip()
    dst = str(to_state or "").upper().strip()
    if src == dst:
        return True
    allowed = ALLOWED_POSITION_TRANSITIONS.get(src)
    if allowed is None or dst not in allowed:
        raise ValueError(f"ILLEGAL_POSITION_TRANSITION: Transisi state paper trade tidak sah dari '{src}' ke '{dst}'.")
    return True


@dataclass(frozen=True)
class RealizedTradeAccounting:
    """
    Exact realized trade PnL, fee, slippage, spread, funding cost, and R-multiple breakdown (Phase 2.1, F04, F36, T23).
    Distinguishes theoretical levels from executed fill prices.
    """
    direction: str
    quantity: float
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: Optional[float]
    entry_fill_price: float
    exit_fill_price: float
    entry_notional: float
    exit_notional: float
    initial_risk_gross: float
    risk_budget: float
    entry_fee: float
    exit_fee: float
    total_fees: float
    slippage_cost: float
    spread_cost: float
    total_cost: float
    gross_pnl: float
    net_pnl: float
    gross_r_multiple: float
    net_r_multiple: float
    accounting_reconciled: bool
    funding_cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "entry_fill_price": self.entry_fill_price,
            "exit_fill_price": self.exit_fill_price,
            "entry_notional": self.entry_notional,
            "exit_notional": self.exit_notional,
            "initial_risk_gross": self.initial_risk_gross,
            "risk_budget": self.risk_budget,
            "entry_fee": self.entry_fee,
            "exit_fee": self.exit_fee,
            "total_fees": self.total_fees,
            "slippage_cost": self.slippage_cost,
            "spread_cost": self.spread_cost,
            "funding_cost": self.funding_cost,
            "total_cost": self.total_cost,
            "gross_pnl": self.gross_pnl,
            "net_pnl": self.net_pnl,
            "gross_r_multiple": self.gross_r_multiple,
            "net_r_multiple": self.net_r_multiple,
            "accounting_reconciled": self.accounting_reconciled,
        }


def compute_realized_trade_accounting(
    direction: str,
    entry_price: float,
    exit_price: float,
    stop_loss: float,
    *,
    take_profit: Optional[float] = None,
    quantity: float = 1.0,
    risk_budget: Optional[float] = None,
    fee_pct_round_trip: float = 0.08,
    slippage_pct_round_trip: float = 0.04,
    spread_pct: float = 0.0,
    funding_rate_pct_per_8h: float = 0.0,
    holding_hours: float = 0.0,
) -> RealizedTradeAccounting:
    """
    Compute exact realized gross/net PnL, fees, slippage, spread, funding costs, and gross/net R-multiples (Phase 2.1, F04, T23).
    """
    dir_norm = str(direction or "WAIT").upper().strip()
    if dir_norm not in {"LONG", "SHORT"}:
        raise ValueError(f"Direction harus LONG atau SHORT untuk akuntansi trade, diterima: {direction!r}")

    for name, val in (
        ("entry_price", entry_price),
        ("exit_price", exit_price),
        ("stop_loss", stop_loss),
        ("quantity", quantity),
        ("fee_pct_round_trip", fee_pct_round_trip),
        ("slippage_pct_round_trip", slippage_pct_round_trip),
        ("spread_pct", spread_pct),
        ("funding_rate_pct_per_8h", funding_rate_pct_per_8h),
        ("holding_hours", holding_hours),
    ):
        if not is_finite_number(val):
            raise ValueError(f"Parameter non-finite pada {name}: {val!r}")

    entry_f = float(entry_price)
    exit_f = float(exit_price)
    sl_f = float(stop_loss)
    tp_f = float(take_profit) if is_finite_number(take_profit) else None
    qty_f = max(0.0, float(quantity))
    fee_rt = max(0.0, float(fee_pct_round_trip))
    slip_rt = max(0.0, float(slippage_pct_round_trip))
    spread_f = max(0.0, float(spread_pct))
    funding_rate_8h = max(0.0, float(funding_rate_pct_per_8h))
    hold_hrs = max(0.0, float(holding_hours))

    stop_dist = abs(entry_f - sl_f)
    if entry_f <= 0.0 or exit_f <= 0.0 or sl_f <= 0.0 or stop_dist <= 1e-12:
        raise ValueError("entry_price, exit_price, dan stop_loss harus positif dan entry != stop_loss.")

    # Effective fill prices including per-leg slippage + half-spread
    leg_slip_pct = (slip_rt / 2.0) + (spread_f / 2.0)
    if dir_norm == "LONG":
        entry_fill = entry_f * (1.0 + leg_slip_pct / 100.0)
        exit_fill = exit_f * (1.0 - leg_slip_pct / 100.0)
        unit_gross_pnl = exit_f - entry_f
    else:
        entry_fill = entry_f * (1.0 - leg_slip_pct / 100.0)
        exit_fill = exit_f * (1.0 + leg_slip_pct / 100.0)
        unit_gross_pnl = entry_f - exit_f

    entry_notional = qty_f * entry_f
    exit_notional = qty_f * exit_f
    initial_risk_gross = qty_f * stop_dist

    leg_fee_pct = fee_rt / 2.0
    entry_fee = round(entry_notional * (leg_fee_pct / 100.0), 4)
    exit_fee = round(exit_notional * (leg_fee_pct / 100.0), 4)
    total_fees = round(entry_fee + exit_fee, 4)

    slip_leg_pct = slip_rt / 2.0
    slippage_cost = round((entry_notional + exit_notional) * (slip_leg_pct / 100.0), 4)
    spread_cost = round(entry_notional * (spread_f / 100.0), 4)
    funding_intervals = hold_hrs / 8.0 if hold_hrs > 0.0 else 0.0
    funding_cost = round(entry_notional * (funding_rate_8h / 100.0) * funding_intervals, 4) if funding_rate_8h > 0.0 else 0.0
    total_cost = round(total_fees + slippage_cost + spread_cost + funding_cost, 4)

    gross_pnl = round(qty_f * unit_gross_pnl, 4)
    net_pnl = round(gross_pnl - total_cost, 4)

    gross_r = unit_gross_pnl / stop_dist

    eff_risk_budget = (
        float(risk_budget)
        if (is_finite_number(risk_budget) and float(risk_budget) > 0.0)
        else (initial_risk_gross + entry_notional * ((fee_rt + slip_rt + spread_f) / 100.0))
    )
    net_r = (net_pnl / eff_risk_budget) if eff_risk_budget > 0.0 else 0.0

    reconciled = bool(
        math.isclose(total_fees, entry_fee + exit_fee, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(total_cost, total_fees + slippage_cost + spread_cost + funding_cost, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(net_pnl, gross_pnl - total_cost, rel_tol=1e-9, abs_tol=1e-9)
    )

    return RealizedTradeAccounting(
        direction=dir_norm,
        quantity=round(qty_f, 8),
        entry_price=round(entry_f, 8),
        exit_price=round(exit_f, 8),
        stop_loss=round(sl_f, 8),
        take_profit=round(tp_f, 8) if tp_f is not None else None,
        entry_fill_price=round(entry_fill, 8),
        exit_fill_price=round(exit_fill, 8),
        entry_notional=round(entry_notional, 4),
        exit_notional=round(exit_notional, 4),
        initial_risk_gross=round(initial_risk_gross, 4),
        risk_budget=round(eff_risk_budget, 4),
        entry_fee=round(entry_fee, 4),
        exit_fee=round(exit_fee, 4),
        total_fees=round(total_fees, 4),
        slippage_cost=round(slippage_cost, 4),
        spread_cost=round(spread_cost, 4),
        total_cost=round(total_cost, 4),
        gross_pnl=round(gross_pnl, 4),
        net_pnl=round(net_pnl, 4),
        gross_r_multiple=round(gross_r, 4),
        net_r_multiple=round(net_r, 4),
        accounting_reconciled=reconciled,
        funding_cost=round(funding_cost, 4),
    )


def _clamp_price_to_bar(price: float, bar_low: float, bar_high: float) -> float:
    """Ensure simulated execution price is physically possible within the candle [bar_low, bar_high] (Phase 2.1)."""
    lo = min(float(bar_low), float(bar_high))
    hi = max(float(bar_low), float(bar_high))
    return max(lo, min(hi, float(price)))


def resolve_intrabar_conflict(
    direction: str,
    bar_open: float,
    stop_loss: float,
    take_profit: float,
    policy: str = IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value,
    *,
    bar_low: Optional[float] = None,
    bar_high: Optional[float] = None,
) -> Tuple[str, str, float]:
    """
    Resolve intrabar ambiguity when a single candle's High and Low touch both TP and SL (Phase 2.1, F04, T23).
    Defaults to documented conservative rule `SL_FIRST_CONSERVATIVE`.
    Returns (new_state, result_code, exit_price).
    """
    pol = str(policy or IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value).upper().strip()
    dir_u = str(direction or "LONG").upper().strip()

    def _realistic_sl() -> float:
        sl_f = float(stop_loss)
        if is_finite_number(bar_open) and is_finite_number(bar_low) and is_finite_number(bar_high):
            bo = float(bar_open)
            # If bar gapped open past stop loss, fill at gap open price clamped to bar range
            if dir_u == "LONG" and bo < sl_f:
                return _clamp_price_to_bar(bo, float(bar_low), float(bar_high))
            if dir_u == "SHORT" and bo > sl_f:
                return _clamp_price_to_bar(bo, float(bar_low), float(bar_high))
            return _clamp_price_to_bar(sl_f, float(bar_low), float(bar_high))
        return sl_f

    def _realistic_tp() -> float:
        tp_f = float(take_profit)
        if is_finite_number(bar_open) and is_finite_number(bar_low) and is_finite_number(bar_high):
            return _clamp_price_to_bar(tp_f, float(bar_low), float(bar_high))
        return tp_f

    if pol == IntrabarTieBreaker.TP_FIRST.value:
        return PaperPositionState.CLOSED_TP.value, "TP", _realistic_tp()
    if pol == IntrabarTieBreaker.OPEN_PROXIMITY.value and is_finite_number(bar_open):
        dist_sl = abs(float(bar_open) - float(stop_loss))
        dist_tp = abs(float(bar_open) - float(take_profit))
        if dist_tp < dist_sl:
            return PaperPositionState.CLOSED_TP.value, "TP", _realistic_tp()
        return PaperPositionState.CLOSED_SL.value, "SL", _realistic_sl()
    # Default: SL_FIRST_CONSERVATIVE (worst-case conservative tie-breaker)
    return PaperPositionState.CLOSED_SL.value, "SL", _realistic_sl()


def _normalize_bars_for_simulation(
    bars: Any,
    since_time: Any = None,
    interval: str = "15m",
) -> List[Dict[str, Any]]:
    """
    Convert a DataFrame, TimeframeSnapshot, or list of bar dicts into a chronological list of
    closed-bar dicts strictly after/at since_time.
    """
    if bars is None:
        return []

    if isinstance(bars, list):
        since_ms = normalize_as_of_ms(since_time) if since_time is not None else None
        out_list: List[Dict[str, Any]] = []
        for idx, b in enumerate(bars):
            if not isinstance(b, dict):
                continue
            b_ts = b.get("close_time") or b.get("time") or b.get("timestamp")
            b_ms = normalize_as_of_ms(b_ts) if b_ts is not None else None
            if since_ms is not None and b_ms is not None and b_ms < since_ms:
                continue
            out_list.append({
                "open": float(b.get("Open", b.get("open", 0.0))),
                "high": float(b.get("High", b.get("high", 0.0))),
                "low": float(b.get("Low", b.get("low", 0.0))),
                "close": float(b.get("Close", b.get("close", 0.0))),
                "timestamp": str(b.get("timestamp") or b.get("close_time") or b.get("time") or f"bar_{idx}"),
                "close_ms": b_ms,
            })
        return out_list

    if since_time is not None:
        df = filter_bars_since(bars, since_time=since_time, interval=interval, include_unclosed=False)
    elif isinstance(bars, pd.DataFrame):
        df = extract_closed_df(bars, interval=interval)
    else:
        df = filter_bars_since(bars, since_time=0, interval=interval, include_unclosed=False)

    if df is None or len(df) == 0:
        return []

    records: List[Dict[str, Any]] = []
    for idx_val, row in df.iterrows():
        ts_str = str(idx_val)
        c_ms = normalize_as_of_ms(row["close_time"]) if ("close_time" in df.columns and pd.notna(row["close_time"])) else normalize_as_of_ms(idx_val)
        records.append({
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "timestamp": ts_str,
            "close_ms": c_ms,
        })
    return records


def evaluate_position_on_bars(
    position: Dict[str, Any],
    bars: Any = None,
    *,
    fallback_spot_price: Optional[float] = None,
    fill_at_spot: bool = False,
    tie_breaker: str = IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value,
    max_wait_bars: Optional[int] = None,
    max_hold_bars: Optional[int] = None,
    expires_at: Any = None,
    time_stop_at: Any = None,
    interval: str = "15m",
    now_iso: Optional[str] = None,
    funding_rate_pct_per_8h: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Evaluate a PENDING_ENTRY or OPEN paper position across all closed OHLC bars since
    its creation/open timestamp (Phase 2.1, F04, F33, F36, T23):
    - Never misses a historical High/Low touch between UI polls.
    - Resolves same-bar TP & SL touches deterministically via `tie_breaker` (default `SL_FIRST_CONSERVATIVE`).
    - Prevents impossible execution prices (gap opens past SL/limit fill at realistic clamped prices within [Low, High]).
    - Includes funding costs when applicable.
    """
    pos = dict(position)
    state = str(pos.get("state") or PaperPositionState.OPEN.value).upper().strip()
    direction = str(pos.get("direction") or pos.get("final_decision") or "WAIT").upper().strip()

    if direction == "WAIT" or state == PaperPositionState.OBSERVED_WAIT.value:
        pos["state"] = PaperPositionState.OBSERVED_WAIT.value
        pos["changed"] = False
        return pos

    if state in TERMINAL_POSITION_STATES:
        pos["changed"] = False
        return pos

    entry_p = float(pos["entry"])
    sl_p = float(pos["stop_loss"])
    tp_p = float(pos["take_profit"])
    qty = float(pos.get("quantity") or pos.get("position_size") or 1.0)
    risk_budget = pos.get("risk_budget")
    fee_rt = float(pos.get("fee_pct_round_trip", 0.08) or 0.0)
    slip_rt = float(pos.get("slippage_pct_round_trip", 0.04) or 0.0)
    spread_pct = float(pos.get("spread_pct", 0.0) or 0.0)
    eff_funding_rate = (
        float(funding_rate_pct_per_8h)
        if funding_rate_pct_per_8h is not None
        else float(pos.get("funding_rate_pct_per_8h", 0.0) or 0.0)
    )

    eff_max_wait = int(max_wait_bars) if max_wait_bars is not None else (int(pos["max_wait_bars"]) if pos.get("max_wait_bars") is not None else None)
    eff_max_hold = int(max_hold_bars) if max_hold_bars is not None else (int(pos["max_hold_bars"]) if pos.get("max_hold_bars") is not None else None)
    exp_ms = normalize_as_of_ms(expires_at if expires_at is not None else pos.get("expires_at")) if (expires_at is not None or pos.get("expires_at") is not None) else None
    tstop_ms = normalize_as_of_ms(time_stop_at if time_stop_at is not None else pos.get("time_stop_at")) if (time_stop_at is not None or pos.get("time_stop_at") is not None) else None

    since_anchor = pos.get("last_evaluated_bar_time") or pos.get("opened_at") or pos.get("created_at")
    norm_bars = _normalize_bars_for_simulation(bars, since_time=since_anchor, interval=interval)

    bars_waited = int(pos.get("bars_waited", 0) or 0)
    bars_held = int(pos.get("bars_held", 0) or 0)
    changed = False
    intrabar_conflict = False
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    closed_at_ts: Optional[str] = None
    target_state = state

    z_low = float(pos["entry_zone_low"]) if is_finite_number(pos.get("entry_zone_low")) else entry_p
    z_high = float(pos["entry_zone_high"]) if is_finite_number(pos.get("entry_zone_high")) else entry_p
    zone_lo, zone_hi = min(z_low, z_high, entry_p), max(z_low, z_high, entry_p)

    for b in norm_bars:
        b_open = b["open"]
        b_high = b["high"]
        b_low = b["low"]
        b_close = b["close"]
        b_ts = b["timestamp"]
        b_ms = b["close_ms"]
        pos["last_evaluated_bar_time"] = b_ts

        # Stage 1: PENDING_ENTRY evaluation
        if target_state == PaperPositionState.PENDING_ENTRY.value:
            if exp_ms is not None and b_ms is not None and b_ms > exp_ms:
                validate_position_transition(target_state, PaperPositionState.EXPIRED_UNFILLED.value)
                target_state = PaperPositionState.EXPIRED_UNFILLED.value
                exit_reason = "EXPIRED"
                closed_at_ts = b_ts
                changed = True
                break

            entry_touched = (b_low <= entry_p <= b_high) or (b_low <= zone_hi and b_high >= zone_lo)
            sl_breached = (b_low <= sl_p) if direction == "LONG" else (b_high >= sl_p)

            if sl_breached and not entry_touched:
                validate_position_transition(target_state, PaperPositionState.CLOSED_INVALIDATED.value)
                target_state = PaperPositionState.CLOSED_INVALIDATED.value
                exit_reason = "INVALIDATED_BEFORE_FILL"
                closed_at_ts = b_ts
                changed = True
                break

            if entry_touched:
                validate_position_transition(target_state, PaperPositionState.OPEN.value)
                target_state = PaperPositionState.OPEN.value
                pos["opened_at"] = b_ts
                pos["entry_fill_bar_time"] = b_ts
                # Ensure entry price is physically inside the fill candle's [b_low, b_high]
                entry_p = _clamp_price_to_bar(entry_p, b_low, b_high)
                pos["entry"] = entry_p
                changed = True
            else:
                bars_waited += 1
                if eff_max_wait is not None and bars_waited >= eff_max_wait:
                    validate_position_transition(target_state, PaperPositionState.EXPIRED_UNFILLED.value)
                    target_state = PaperPositionState.EXPIRED_UNFILLED.value
                    exit_reason = "EXPIRED"
                    closed_at_ts = b_ts
                    changed = True
                    break
                continue

        # Stage 2: OPEN position evaluation on bar High / Low / Close
        if target_state == PaperPositionState.OPEN.value:
            bars_held += 1
            if direction == "LONG":
                tp_hit = b_high >= tp_p
                sl_hit = b_low <= sl_p
            else:
                tp_hit = b_low <= tp_p
                sl_hit = b_high >= sl_p

            if tp_hit and sl_hit:
                intrabar_conflict = True
                next_st, res_code, ex_p = resolve_intrabar_conflict(
                    direction,
                    b_open,
                    sl_p,
                    tp_p,
                    policy=tie_breaker,
                    bar_low=b_low,
                    bar_high=b_high,
                )
                validate_position_transition(target_state, next_st)
                target_state = next_st
                exit_reason = res_code
                exit_price = ex_p
                closed_at_ts = b_ts
                changed = True
                break
            elif sl_hit:
                validate_position_transition(target_state, PaperPositionState.CLOSED_SL.value)
                target_state = PaperPositionState.CLOSED_SL.value
                exit_reason = "SL"
                # Prevent impossible fill on gap open beyond stop loss
                if direction == "LONG" and b_open < sl_p:
                    exit_price = _clamp_price_to_bar(b_open, b_low, b_high)
                elif direction == "SHORT" and b_open > sl_p:
                    exit_price = _clamp_price_to_bar(b_open, b_low, b_high)
                else:
                    exit_price = _clamp_price_to_bar(sl_p, b_low, b_high)
                closed_at_ts = b_ts
                changed = True
                break
            elif tp_hit:
                validate_position_transition(target_state, PaperPositionState.CLOSED_TP.value)
                target_state = PaperPositionState.CLOSED_TP.value
                exit_reason = "TP"
                exit_price = _clamp_price_to_bar(tp_p, b_low, b_high)
                closed_at_ts = b_ts
                changed = True
                break
            elif (eff_max_hold is not None and bars_held >= eff_max_hold) or (tstop_ms is not None and b_ms is not None and b_ms >= tstop_ms):
                validate_position_transition(target_state, PaperPositionState.CLOSED_TIME_STOP.value)
                target_state = PaperPositionState.CLOSED_TIME_STOP.value
                exit_reason = "TIME_STOP"
                exit_price = _clamp_price_to_bar(b_close, b_low, b_high)
                closed_at_ts = b_ts
                changed = True
                break

    # Fallback spot price check when still OPEN
    if target_state == PaperPositionState.OPEN.value and is_finite_number(fallback_spot_price) and float(fallback_spot_price) > 0:
        sp = float(fallback_spot_price)
        now_s = now_iso or datetime.now(timezone.utc).isoformat()
        if direction == "LONG":
            if sp >= tp_p:
                validate_position_transition(target_state, PaperPositionState.CLOSED_TP.value)
                target_state = PaperPositionState.CLOSED_TP.value
                exit_reason = "TP"
                exit_price = sp if fill_at_spot else tp_p
                closed_at_ts = now_s
                changed = True
            elif sp <= sl_p:
                validate_position_transition(target_state, PaperPositionState.CLOSED_SL.value)
                target_state = PaperPositionState.CLOSED_SL.value
                exit_reason = "SL"
                exit_price = sp if fill_at_spot else sl_p
                closed_at_ts = now_s
                changed = True
        elif direction == "SHORT":
            if sp <= tp_p:
                validate_position_transition(target_state, PaperPositionState.CLOSED_TP.value)
                target_state = PaperPositionState.CLOSED_TP.value
                exit_reason = "TP"
                exit_price = sp if fill_at_spot else tp_p
                closed_at_ts = now_s
                changed = True
            elif sp >= sl_p:
                validate_position_transition(target_state, PaperPositionState.CLOSED_SL.value)
                target_state = PaperPositionState.CLOSED_SL.value
                exit_reason = "SL"
                exit_price = sp if fill_at_spot else sl_p
                closed_at_ts = now_s
                changed = True

    pos["state"] = target_state
    pos["bars_waited"] = bars_waited
    pos["bars_held"] = bars_held
    pos["intrabar_conflict"] = bool(intrabar_conflict)
    pos["tie_breaker_policy"] = str(tie_breaker)
    pos["changed"] = bool(changed)

    tf_seconds = INTERVAL_SECONDS.get(interval, 900)
    holding_hours = (bars_held * tf_seconds) / 3600.0

    if target_state in {
        PaperPositionState.CLOSED_TP.value,
        PaperPositionState.CLOSED_SL.value,
        PaperPositionState.CLOSED_TIME_STOP.value,
    } and exit_price is not None:
        acc = compute_realized_trade_accounting(
            direction=direction,
            entry_price=entry_p,
            exit_price=exit_price,
            stop_loss=sl_p,
            take_profit=tp_p,
            quantity=qty,
            risk_budget=risk_budget,
            fee_pct_round_trip=fee_rt,
            slippage_pct_round_trip=slip_rt,
            spread_pct=spread_pct,
            funding_rate_pct_per_8h=eff_funding_rate,
            holding_hours=holding_hours,
        )
        pos["result"] = exit_reason
        pos["exit_reason"] = exit_reason
        pos["exit_price"] = acc.exit_price
        pos["entry_fill_price"] = acc.entry_fill_price
        pos["exit_fill_price"] = acc.exit_fill_price
        pos["closed_at"] = closed_at_ts or now_iso or datetime.now(timezone.utc).isoformat()
        pos["entry_fee"] = acc.entry_fee
        pos["exit_fee"] = acc.exit_fee
        pos["total_fees"] = acc.total_fees
        pos["slippage_cost"] = acc.slippage_cost
        pos["spread_cost"] = acc.spread_cost
        pos["funding_cost"] = acc.funding_cost
        pos["total_cost"] = acc.total_cost
        pos["gross_pnl"] = acc.gross_pnl
        pos["net_pnl"] = acc.net_pnl
        pos["gross_r_multiple"] = acc.gross_r_multiple
        pos["net_r_multiple"] = acc.net_r_multiple
        pos["r_multiple"] = acc.gross_r_multiple
        pos["accounting"] = acc.to_dict()
    elif target_state in {
        PaperPositionState.EXPIRED_UNFILLED.value,
        PaperPositionState.CLOSED_INVALIDATED.value,
        PaperPositionState.CANCELLED.value,
    }:
        pos["result"] = exit_reason or target_state
        pos["exit_reason"] = exit_reason or target_state
        pos["closed_at"] = closed_at_ts or now_iso or datetime.now(timezone.utc).isoformat()
        pos["exit_price"] = None
        pos["entry_fee"] = 0.0
        pos["exit_fee"] = 0.0
        pos["total_fees"] = 0.0
        pos["slippage_cost"] = 0.0
        pos["spread_cost"] = 0.0
        pos["funding_cost"] = 0.0
        pos["total_cost"] = 0.0
        pos["gross_pnl"] = 0.0
        pos["net_pnl"] = 0.0
        pos["gross_r_multiple"] = 0.0
        pos["net_r_multiple"] = 0.0
        pos["r_multiple"] = 0.0

    return pos


# ---------------------------------------------------------
# Sample-Size-Aware Expectancy, Drawdown, Segmented Breakdown & Cost-Drag Analytics (Phase 2.3)
# ---------------------------------------------------------
DEFAULT_MIN_SIGNIFICANT_SAMPLE: int = 30

CONFIDENCE_CALIBRATION_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("0-59", 0.0, 59.9999),
    ("60-69", 60.0, 69.9999),
    ("70-79", 70.0, 79.9999),
    ("80-89", 80.0, 89.9999),
    ("90-100", 90.0, 100.0),
)

METRIC_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "win_rate_net": {
        "formula": "count(net_pnl > 0) / total_closed_trades * 100",
        "denominator": "total_closed_trades (n)",
        "edge_case": "0.0 when n == 0",
    },
    "net_return_pct": {
        "formula": "(final_equity - initial_equity) / initial_equity * 100",
        "denominator": "initial_equity",
        "edge_case": "0.0 when initial_equity <= 0 or n == 0",
    },
    "profit_factor_net": {
        "formula": "sum(net_r for net_r > 0) / abs(sum(net_r for net_r < 0))",
        "denominator": "abs(sum(net_r for net_r < 0))",
        "edge_case": "None (status='UNDEFINED_NO_LOSSES') when losses == 0 and wins > 0; None (status='NO_CLOSED_TRADES') when n == 0",
    },
    "expectancy_net_usd": {
        "formula": "sum(net_pnl) / total_closed_trades",
        "denominator": "total_closed_trades (n)",
        "edge_case": "0.0 when n == 0",
    },
    "expectancy_net_r": {
        "formula": "sum(net_r_multiple) / total_closed_trades",
        "denominator": "total_closed_trades (n)",
        "edge_case": "0.0 when n == 0",
    },
    "max_drawdown_pct": {
        "formula": "max_t((peak_equity_t - equity_t) / peak_equity_t * 100)",
        "denominator": "running peak_equity_t",
        "edge_case": "0.0 when equity never drops below peak",
    },
}


def _compute_segment_slice_metrics(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute concise net performance summary for a subset slice (LONG/SHORT, timeframe, strategy, regime)."""
    cnt = len(items)
    if cnt == 0:
        return {
            "total_trades": 0,
            "sample_size": 0,
            "wins_net": 0,
            "losses_net": 0,
            "win_rate_net": 0.0,
            "total_net_pnl": 0.0,
            "expectancy_net_usd": 0.0,
            "expectancy_net_r": 0.0,
            "avg_win_net_r": 0.0,
            "avg_loss_net_r": 0.0,
            "profit_factor_net": None,
            "profit_factor_status": "NO_CLOSED_TRADES",
        }
    net_pnls: List[float] = []
    net_rs: List[float] = []
    for p in items:
        gr = float(p.get("gross_r_multiple") if p.get("gross_r_multiple") is not None else (p.get("r_multiple") or 0.0))
        nr = float(p.get("net_r_multiple") if p.get("net_r_multiple") is not None else gr)
        gp = float(p.get("gross_pnl") or 0.0)
        tc = float(p.get("total_cost") or 0.0)
        np_v = float(p.get("net_pnl") if p.get("net_pnl") is not None else (gp - tc))
        net_pnls.append(np_v)
        net_rs.append(nr)

    win_rs = [r for r in net_rs if r > 0.0]
    loss_rs = [abs(r) for r in net_rs if r < 0.0]
    wins_n = sum(1 for idx_i, np_v in enumerate(net_pnls) if np_v > 0.0 or (np_v == 0.0 and net_rs[idx_i] > 0.0))
    losses_n = sum(1 for idx_i, np_v in enumerate(net_pnls) if np_v < 0.0 or (np_v == 0.0 and net_rs[idx_i] < 0.0))
    pos_r = sum(win_rs)
    neg_r = sum(loss_rs)
    if neg_r > 0 and pos_r > 0:
        pf_n: Optional[float] = round(pos_r / neg_r, 4)
        pf_st = "MEASURED_NET_RATIO"
    elif neg_r > 0 and pos_r == 0:
        pf_n = 0.0
        pf_st = "ZERO_WINS"
    elif pos_r > 0 and neg_r == 0:
        pf_n = None
        pf_st = "UNDEFINED_NO_LOSSES"
    else:
        pf_n = None
        pf_st = "ZERO_ACTIVITY"

    return {
        "total_trades": cnt,
        "sample_size": cnt,
        "wins_net": wins_n,
        "losses_net": losses_n,
        "win_rate_net": round((wins_n / cnt) * 100.0, 2),
        "total_net_pnl": round(sum(net_pnls), 4),
        "expectancy_net_usd": round(sum(net_pnls) / cnt, 4),
        "expectancy_net_r": round(sum(net_rs) / cnt, 4),
        "avg_win_net_r": round(sum(win_rs) / len(win_rs), 4) if win_rs else 0.0,
        "avg_loss_net_r": round(sum(loss_rs) / len(loss_rs), 4) if loss_rs else 0.0,
        "profit_factor_net": pf_n,
        "profit_factor_status": pf_st,
    }


def compute_expectancy_and_drawdown_analytics(
    closed_positions: List[Dict[str, Any]],
    *,
    initial_equity: float = 10_000.0,
    min_significant_sample: int = DEFAULT_MIN_SIGNIFICANT_SAMPLE,
    dataset_type: str = "PAPER_TRADING",
) -> Dict[str, Any]:
    """
    Compute sample-size-aware performance, expectancy (gross/net R and USD), profit factor,
    equity curve, max drawdown (USD, %, R), consecutive losses, cost-drag analytics,
    and segmented breakdowns by LONG/SHORT, timeframe, strategy/mode, and market regime
    (Phase 2.3 / PH06-02 / F04, F33, F36 / T24).
    Handles n=0, all-win, all-loss, small-sample, and mixed-sample edge cases without NaN/Infinity.
    """
    init_eq = float(initial_equity) if (is_finite_number(initial_equity) and float(initial_equity) > 0) else 10_000.0
    min_n = max(1, int(min_significant_sample or DEFAULT_MIN_SIGNIFICANT_SAMPLE))

    realized = [
        dict(p)
        for p in (closed_positions or [])
        if isinstance(p, dict)
        and str(p.get("state") or p.get("result") or "").upper().strip()
        in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP", "TP", "SL", "TIME_STOP"}
    ]
    realized.sort(
        key=lambda x: (
            str(x.get("closed_at") or x.get("created_at") or ""),
            str(x.get("position_id") or x.get("id") or ""),
        )
    )

    n = len(realized)
    if n == 0:
        return {
            "dataset_type": str(dataset_type),
            "total_trades": 0,
            "sample_size": 0,
            "min_significant_sample": min_n,
            "sample_sufficiency": "NO_SAMPLE",
            "is_statistically_significant": False,
            "sample_warning": f"Belum ada trade tertutup (n=0 < {min_n}); metrik performa belum tersedia.",
            "wins_gross": 0,
            "losses_gross": 0,
            "wins_net": 0,
            "losses_net": 0,
            "breakeven_net": 0,
            "win_rate_gross": 0.0,
            "win_rate_net": 0.0,
            "avg_win_gross_r": 0.0,
            "avg_loss_gross_r": 0.0,
            "avg_win_net_r": 0.0,
            "avg_loss_net_r": 0.0,
            "avg_win_net_usd": 0.0,
            "avg_loss_net_usd": 0.0,
            "payoff_ratio_gross": None,
            "payoff_ratio_net": None,
            "expectancy_gross_r": 0.0,
            "expectancy_net_r": 0.0,
            "expectancy_net_usd": 0.0,
            "profit_factor_gross": None,
            "profit_factor_net": None,
            "profit_factor_status": "NO_CLOSED_TRADES",
            "total_gross_pnl": 0.0,
            "total_fees": 0.0,
            "total_slippage_cost": 0.0,
            "total_spread_cost": 0.0,
            "total_funding_cost": 0.0,
            "total_cost": 0.0,
            "total_net_pnl": 0.0,
            "cost_drag_usd": 0.0,
            "cost_drag_r": 0.0,
            "cost_drag_pct_of_gross": None,
            "initial_equity": round(init_eq, 4),
            "final_equity": round(init_eq, 4),
            "peak_equity": round(init_eq, 4),
            "net_return_pct": 0.0,
            "max_drawdown_usd": 0.0,
            "max_drawdown_pct": 0.0,
            "max_drawdown_r": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "current_consecutive_losses": 0,
            "by_direction": {
                "LONG": _compute_segment_slice_metrics([]),
                "SHORT": _compute_segment_slice_metrics([]),
            },
            "by_timeframe": {},
            "by_strategy": {},
            "by_market_regime": {},
            "metric_definitions": METRIC_DEFINITIONS,
            "equity_curve": [],
        }

    gross_rs: List[float] = []
    net_rs: List[float] = []
    gross_pnls: List[float] = []
    net_pnls: List[float] = []
    fees_list: List[float] = []
    slip_list: List[float] = []
    spread_list: List[float] = []
    funding_list: List[float] = []
    costs_list: List[float] = []

    equity = init_eq
    peak_eq = init_eq
    max_dd_usd = 0.0
    max_dd_pct = 0.0

    cum_net_r = 0.0
    peak_net_r = 0.0
    max_dd_r = 0.0

    cur_win_streak = 0
    cur_loss_streak = 0
    max_win_streak = 0
    max_loss_streak = 0

    equity_curve: List[Dict[str, Any]] = []

    for idx, p in enumerate(realized, 1):
        gr = float(p.get("gross_r_multiple") if p.get("gross_r_multiple") is not None else (p.get("r_multiple") or 0.0))
        nr = float(p.get("net_r_multiple") if p.get("net_r_multiple") is not None else gr)
        gp = float(p.get("gross_pnl") or 0.0)
        fee_v = float(p.get("total_fees") or 0.0)
        slip_v = float(p.get("slippage_cost") or 0.0)
        spr_v = float(p.get("spread_cost") or 0.0)
        fund_v = float(p.get("funding_cost") or 0.0)
        tc_v = float(p.get("total_cost") if p.get("total_cost") is not None else (fee_v + slip_v + spr_v + fund_v))
        np_v = float(p.get("net_pnl") if p.get("net_pnl") is not None else (gp - tc_v))

        gross_rs.append(gr)
        net_rs.append(nr)
        gross_pnls.append(gp)
        net_pnls.append(np_v)
        fees_list.append(fee_v)
        slip_list.append(slip_v)
        spread_list.append(spr_v)
        funding_list.append(fund_v)
        costs_list.append(tc_v)

        if np_v > 0.0 or (np_v == 0.0 and nr > 0.0):
            cur_win_streak += 1
            cur_loss_streak = 0
            if cur_win_streak > max_win_streak:
                max_win_streak = cur_win_streak
        elif np_v < 0.0 or (np_v == 0.0 and nr < 0.0):
            cur_loss_streak += 1
            cur_win_streak = 0
            if cur_loss_streak > max_loss_streak:
                max_loss_streak = cur_loss_streak
        else:
            cur_win_streak = 0
            cur_loss_streak = 0

        equity = round(equity + np_v, 4)
        if equity > peak_eq:
            peak_eq = equity
        dd_usd = round(max(0.0, peak_eq - equity), 4)
        dd_pct = round((dd_usd / peak_eq) * 100.0, 4) if peak_eq > 0 else 0.0
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

        cum_net_r = round(cum_net_r + nr, 4)
        if cum_net_r > peak_net_r:
            peak_net_r = cum_net_r
        dd_r = round(max(0.0, peak_net_r - cum_net_r), 4)
        if dd_r > max_dd_r:
            max_dd_r = dd_r

        equity_curve.append({
            "step": idx,
            "position_id": p.get("position_id") or f"sig_{p.get('id', idx)}",
            "symbol": p.get("symbol", "UNKNOWN"),
            "state": p.get("state") or p.get("result"),
            "closed_at": p.get("closed_at"),
            "gross_pnl": round(gp, 4),
            "total_cost": round(tc_v, 4),
            "net_pnl": round(np_v, 4),
            "gross_r": round(gr, 4),
            "net_r": round(nr, 4),
            "equity": equity,
            "peak_equity": peak_eq,
            "drawdown_usd": dd_usd,
            "drawdown_pct": dd_pct,
            "cum_net_r": cum_net_r,
            "drawdown_r": dd_r,
        })

    win_gross_rs = [r for r in gross_rs if r > 0.0]
    loss_gross_rs = [abs(r) for r in gross_rs if r < 0.0]
    win_net_rs = [r for r in net_rs if r > 0.0]
    loss_net_rs = [abs(r) for r in net_rs if r < 0.0]
    win_net_pnls = [p for p in net_pnls if p > 0.0]
    loss_net_pnls = [abs(p) for p in net_pnls if p < 0.0]
    be_net = sum(1 for r in net_rs if r == 0.0)

    wins_gross = len(win_gross_rs)
    losses_gross = len(loss_gross_rs)
    wins_net = len(win_net_rs)
    losses_net = len(loss_net_rs)

    win_rate_gross = round((wins_gross / n) * 100.0, 2)
    win_rate_net = round((wins_net / n) * 100.0, 2)

    avg_win_gross_r = round(sum(win_gross_rs) / wins_gross, 4) if wins_gross > 0 else 0.0
    avg_loss_gross_r = round(sum(loss_gross_rs) / losses_gross, 4) if losses_gross > 0 else 0.0
    avg_win_net_r = round(sum(win_net_rs) / wins_net, 4) if wins_net > 0 else 0.0
    avg_loss_net_r = round(sum(loss_net_rs) / losses_net, 4) if losses_net > 0 else 0.0
    avg_win_net_usd = round(sum(win_net_pnls) / len(win_net_pnls), 4) if win_net_pnls else 0.0
    avg_loss_net_usd = round(sum(loss_net_pnls) / len(loss_net_pnls), 4) if loss_net_pnls else 0.0

    payoff_gross = round(avg_win_gross_r / avg_loss_gross_r, 4) if (wins_gross > 0 and avg_loss_gross_r > 0) else None
    payoff_net = round(avg_win_net_r / avg_loss_net_r, 4) if (wins_net > 0 and avg_loss_net_r > 0) else None

    exp_gross_r = round(sum(gross_rs) / n, 4)
    exp_net_r = round(sum(net_rs) / n, 4)
    exp_net_usd = round(sum(net_pnls) / n, 4)

    sum_pos_gross_r = sum(win_gross_rs)
    sum_neg_gross_r = sum(loss_gross_rs)
    if sum_neg_gross_r > 0:
        pf_gross: Optional[float] = round(sum_pos_gross_r / sum_neg_gross_r, 4)
    elif sum_pos_gross_r > 0:
        pf_gross = None
    else:
        pf_gross = 0.0

    sum_pos_net_r = sum(win_net_rs)
    sum_neg_net_r = sum(loss_net_rs)
    if sum_neg_net_r > 0 and sum_pos_net_r > 0:
        pf_net: Optional[float] = round(sum_pos_net_r / sum_neg_net_r, 4)
        pf_status = "MEASURED_NET_RATIO"
    elif sum_neg_net_r > 0 and sum_pos_net_r == 0:
        pf_net = 0.0
        pf_status = "ZERO_WINS"
    elif sum_pos_net_r > 0 and sum_neg_net_r == 0:
        pf_net = None
        pf_status = "UNDEFINED_NO_LOSSES"
    else:
        pf_net = None
        pf_status = "ZERO_ACTIVITY"

    tot_gross_pnl = round(sum(gross_pnls), 4)
    tot_fees = round(sum(fees_list), 4)
    tot_slip = round(sum(slip_list), 4)
    tot_spread = round(sum(spread_list), 4)
    tot_funding = round(sum(funding_list), 4)
    tot_cost = round(sum(costs_list), 4)
    tot_net_pnl = round(sum(net_pnls), 4)

    cost_drag_r = round(exp_gross_r - exp_net_r, 4)
    cost_drag_pct = round((tot_cost / abs(tot_gross_pnl)) * 100.0, 2) if abs(tot_gross_pnl) > 1e-9 else None

    # Segmented performance breakdowns (Phase 2.3)
    long_trades = [p for p in realized if str(p.get("direction") or p.get("final_decision") or "").upper() == "LONG"]
    short_trades = [p for p in realized if str(p.get("direction") or p.get("final_decision") or "").upper() == "SHORT"]
    by_direction = {
        "LONG": _compute_segment_slice_metrics(long_trades),
        "SHORT": _compute_segment_slice_metrics(short_trades),
    }

    tf_groups: Dict[str, List[Dict[str, Any]]] = {}
    strat_groups: Dict[str, List[Dict[str, Any]]] = {}
    regime_groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in realized:
        tf_k = str(p.get("timeframe") or "15m")
        tf_groups.setdefault(tf_k, []).append(p)
        st_k = str(p.get("mode") or p.get("strategy_version") or "scalping")
        strat_groups.setdefault(st_k, []).append(p)
        rg_k = str(p.get("market_regime") or "UNKNOWN")
        regime_groups.setdefault(rg_k, []).append(p)

    by_timeframe = {k: _compute_segment_slice_metrics(v) for k, v in sorted(tf_groups.items())}
    by_strategy = {k: _compute_segment_slice_metrics(v) for k, v in sorted(strat_groups.items())}
    by_market_regime = {k: _compute_segment_slice_metrics(v) for k, v in sorted(regime_groups.items())}

    is_sig = bool(n >= min_n)
    sufficiency = "SUFFICIENT_SAMPLE" if is_sig else "LOW_SAMPLE_WARNING"
    warning_txt = (
        f"Sampel memadai (n={n} >= {min_n})."
        if is_sig
        else f"PERINGATAN SAMPEL KECIL: n={n} (< {min_n} trade tertutup); win rate dan expectancy belum signifikan secara statistik."
    )

    return {
        "dataset_type": str(dataset_type),
        "total_trades": n,
        "sample_size": n,
        "min_significant_sample": min_n,
        "sample_sufficiency": sufficiency,
        "is_statistically_significant": is_sig,
        "sample_warning": warning_txt,
        "wins_gross": wins_gross,
        "losses_gross": losses_gross,
        "wins_net": wins_net,
        "losses_net": losses_net,
        "breakeven_net": be_net,
        "win_rate_gross": win_rate_gross,
        "win_rate_net": win_rate_net,
        "avg_win_gross_r": avg_win_gross_r,
        "avg_loss_gross_r": avg_loss_gross_r,
        "avg_win_net_r": avg_win_net_r,
        "avg_loss_net_r": avg_loss_net_r,
        "avg_win_net_usd": avg_win_net_usd,
        "avg_loss_net_usd": avg_loss_net_usd,
        "payoff_ratio_gross": payoff_gross,
        "payoff_ratio_net": payoff_net,
        "expectancy_gross_r": exp_gross_r,
        "expectancy_net_r": exp_net_r,
        "expectancy_net_usd": exp_net_usd,
        "profit_factor_gross": pf_gross,
        "profit_factor_net": pf_net,
        "profit_factor_status": pf_status,
        "total_gross_pnl": tot_gross_pnl,
        "total_fees": tot_fees,
        "total_slippage_cost": tot_slip,
        "total_spread_cost": tot_spread,
        "total_funding_cost": tot_funding,
        "total_cost": tot_cost,
        "total_net_pnl": tot_net_pnl,
        "cost_drag_usd": tot_cost,
        "cost_drag_r": cost_drag_r,
        "cost_drag_pct_of_gross": cost_drag_pct,
        "initial_equity": round(init_eq, 4),
        "final_equity": equity,
        "peak_equity": peak_eq,
        "net_return_pct": round(((equity - init_eq) / init_eq) * 100.0, 4) if init_eq > 0 else 0.0,
        "max_drawdown_usd": max_dd_usd,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_r": max_dd_r,
        "max_consecutive_wins": max_win_streak,
        "max_consecutive_losses": max_loss_streak,
        "current_consecutive_losses": cur_loss_streak,
        "by_direction": by_direction,
        "by_timeframe": by_timeframe,
        "by_strategy": by_strategy,
        "by_market_regime": by_market_regime,
        "metric_definitions": METRIC_DEFINITIONS,
        "equity_curve": equity_curve,
    }


def compute_confidence_calibration_report(
    closed_records: List[Dict[str, Any]],
    *,
    min_bucket_sample: int = 5,
    min_total_sample: int = DEFAULT_MIN_SIGNIFICANT_SAMPLE,
    evaluation_scope: str = "OUT_OF_SAMPLE",
) -> Dict[str, Any]:
    """
    Compute empirical confidence calibration across buckets (0-59, 60-69, 70-79, 80-89, 90-100),
    including Brier score, Expected Calibration Error (ECE), explicit output_label
    ('SCORE_INDICATOR_ONLY' vs 'CALIBRATED_PROBABILITY'), and miscalibration/sample warnings
    (Phase 2.4 / PH06-02 / F33, F36 / T24).
    Never fabricates calibration results.
    """
    min_b = max(1, int(min_bucket_sample or 5))
    min_tot = max(1, int(min_total_sample or DEFAULT_MIN_SIGNIFICANT_SAMPLE))

    valid_items: List[Tuple[float, int, float]] = []  # (confidence_0_100, win_0_1, net_r)
    for rec in closed_records or []:
        if not isinstance(rec, dict):
            continue
        state_or_res = str(rec.get("state") or rec.get("result") or "").upper().strip()
        if state_or_res not in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP", "TP", "SL", "TIME_STOP"}:
            continue
        raw_c = rec.get("confidence")
        if raw_c is None and isinstance(rec.get("portfolio"), dict):
            raw_c = rec["portfolio"].get("confidence")
        if not is_finite_number(raw_c):
            continue
        c_val = max(0.0, min(100.0, float(raw_c)))

        if rec.get("net_pnl") is not None and is_finite_number(rec.get("net_pnl")):
            is_win = 1 if float(rec["net_pnl"]) > 0.0 else 0
        elif rec.get("net_r_multiple") is not None and is_finite_number(rec.get("net_r_multiple")):
            is_win = 1 if float(rec["net_r_multiple"]) > 0.0 else 0
        else:
            is_win = 1 if state_or_res in {"CLOSED_TP", "TP"} else 0

        nr_val = float(
            rec.get("net_r_multiple")
            if (rec.get("net_r_multiple") is not None and is_finite_number(rec.get("net_r_multiple")))
            else (rec.get("r_multiple") or (2.0 if is_win else -1.0))
        )
        valid_items.append((c_val, is_win, nr_val))

    total_n = len(valid_items)
    buckets_out: List[Dict[str, Any]] = []
    weighted_abs_gap = 0.0
    non_empty_win_rates: List[float] = []

    for label, lo, hi in CONFIDENCE_CALIBRATION_BUCKETS:
        b_items = [(c, w, nr) for (c, w, nr) in valid_items if lo <= c <= hi]
        b_n = len(b_items)
        if b_n == 0:
            buckets_out.append({
                "bucket": label,
                "min_confidence": round(lo, 2),
                "max_confidence": round(hi, 2),
                "sample_size": 0,
                "wins": 0,
                "losses": 0,
                "mean_confidence_pct": None,
                "empirical_win_rate_pct": None,
                "calibration_gap_pct": None,
                "avg_net_r": 0.0,
                "bucket_status": "EMPTY",
            })
            continue

        wins_b = sum(w for _, w, _ in b_items)
        losses_b = b_n - wins_b
        mean_c = sum(c for c, _, _ in b_items) / b_n
        emp_wr = (wins_b / b_n) * 100.0
        gap = emp_wr - mean_c
        avg_nr = sum(nr for _, _, nr in b_items) / b_n
        b_status = "SUFFICIENT" if b_n >= min_b else "LOW_SAMPLE"

        weighted_abs_gap += (b_n / max(total_n, 1)) * abs(gap)
        non_empty_win_rates.append(emp_wr)

        buckets_out.append({
            "bucket": label,
            "min_confidence": round(lo, 2),
            "max_confidence": round(hi, 2),
            "sample_size": b_n,
            "wins": wins_b,
            "losses": losses_b,
            "mean_confidence_pct": round(mean_c, 2),
            "empirical_win_rate_pct": round(emp_wr, 2),
            "calibration_gap_pct": round(gap, 2),
            "avg_net_r": round(avg_nr, 4),
            "bucket_status": b_status,
        })

    if total_n == 0:
        return {
            "evaluation_scope": str(evaluation_scope),
            "total_samples": 0,
            "min_total_sample": min_tot,
            "min_bucket_sample": min_b,
            "brier_score": None,
            "expected_calibration_error_pct": None,
            "mean_confidence_pct": None,
            "empirical_win_rate_pct": None,
            "overall_calibration_gap_pct": None,
            "monotonic_win_rate": None,
            "is_calibrated": False,
            "probability_claim_allowed": False,
            "output_label": "SCORE_INDICATOR_ONLY",
            "calibration_status": "NO_DATA",
            "buckets": buckets_out,
            "limitations": [
                "Belum ada observasi out-of-sample tertutup (n=0).",
                "Skor 0-100 hanya merepresentasikan kekuatan relatif indikator/konsensus, bukan probabilitas menang.",
            ],
            "disclaimer": (
                "Confidence adalah skor indikator internal model (0-100), BUKAN probabilitas menang terkalibrasi "
                "(belum ada sampel trade tertutup n=0)."
            ),
        }

    brier = sum(((c / 100.0) - float(w)) ** 2 for c, w, _ in valid_items) / total_n
    ece_pct = weighted_abs_gap
    overall_mean_c = sum(c for c, _, _ in valid_items) / total_n
    overall_emp_wr = (sum(w for _, w, _ in valid_items) / total_n) * 100.0
    overall_gap = overall_emp_wr - overall_mean_c

    monotonic = all(
        non_empty_win_rates[i] <= non_empty_win_rates[i + 1] + 1e-6
        for i in range(len(non_empty_win_rates) - 1)
    ) if len(non_empty_win_rates) >= 2 else True

    if total_n < min_tot:
        cal_status = "INSUFFICIENT_SAMPLE_UNVERIFIED"
        is_cal = False
    elif ece_pct <= 12.0:
        cal_status = "CALIBRATED"
        is_cal = True
    elif overall_gap < -10.0:
        cal_status = "MISCALIBRATED_OVERCONFIDENT"
        is_cal = False
    else:
        cal_status = "MISCALIBRATED"
        is_cal = False

    output_label = "CALIBRATED_PROBABILITY" if is_cal else "SCORE_INDICATOR_ONLY"

    return {
        "evaluation_scope": str(evaluation_scope),
        "total_samples": total_n,
        "min_total_sample": min_tot,
        "min_bucket_sample": min_b,
        "brier_score": round(brier, 4),
        "expected_calibration_error_pct": round(ece_pct, 2),
        "mean_confidence_pct": round(overall_mean_c, 2),
        "empirical_win_rate_pct": round(overall_emp_wr, 2),
        "overall_calibration_gap_pct": round(overall_gap, 2),
        "monotonic_win_rate": bool(monotonic),
        "is_calibrated": is_cal,
        "probability_claim_allowed": is_cal,
        "output_label": output_label,
        "calibration_status": cal_status,
        "buckets": buckets_out,
        "limitations": [
            f"Evaluasi menggunakan {total_n} sampel ({evaluation_scope}); minimum signifikan adalah {min_tot} sampel.",
            "Distribusi rezim pasar kripto bersifat non-stasioner sehingga kalibrasi historis dapat bergeser.",
        ],
        "disclaimer": (
            "Confidence adalah skor indikator internal model (0-100), bukan probabilitas terkalibrasi secara otomatis; "
            f"status kalibrasi empiris saat ini: {cal_status} (label={output_label}, n={total_n}, ECE={ece_pct:.1f}%)."
        ),
    }


# ---------------------------------------------------------
# Deterministic Walk-Forward / Candle-by-Candle Replay Backtest Harness (Phase 2.1 & 2.2)
# ---------------------------------------------------------
def _default_replay_signal_generator(
    visible_df: pd.DataFrame,
    smc_ctx: Dict[str, Any],
    bar_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Default deterministic SMC + indicator signal generator for walk-forward replay backtesting.
    Evaluates only the closed prefix `visible_df` at the current bar and optional aligned higher timeframes.
    """
    timing = str(smc_ctx.get("timing", "WAIT_RETEST")).upper()
    bias = str(smc_ctx.get("structure", {}).get("bias", "RANGE")).upper()
    smc_score = float(smc_ctx.get("smc_score", 0) or 0)
    close_p = float(bar_info["close"])
    atr_v = float(bar_info["atr"])
    min_score_threshold = float(bar_info.get("min_score_threshold", 0.0) or 0.0)

    if smc_score < min_score_threshold:
        return {"decision": "WAIT", "confidence": 35.0, "reason": f"Score {smc_score} < threshold {min_score_threshold}"}

    htf_smcs = bar_info.get("aligned_htf_smc") or {}
    if isinstance(htf_smcs, dict) and "1h" in htf_smcs:
        h1_bias = str(htf_smcs["1h"].get("structure", {}).get("bias", "RANGE")).upper()
        if timing == "READY_LONG" and h1_bias == "BEARISH":
            return {"decision": "WAIT", "confidence": 40.0, "reason": "1H HTF conflict (BEARISH)"}
        if timing == "READY_SHORT" and h1_bias == "BULLISH":
            return {"decision": "WAIT", "confidence": 40.0, "reason": "1H HTF conflict (BULLISH)"}

    if timing == "READY_LONG" and bias == "BULLISH" and atr_v > 0:
        z_lo = smc_ctx.get("bullish_entry", {}).get("zone_low")
        sl_anchor = float(z_lo) if is_finite_number(z_lo) and float(z_lo) > 0 else (close_p - atr_v)
        sl_p = min(close_p - 0.5 * atr_v, sl_anchor - 0.1 * atr_v)
        risk_d = max(close_p - sl_p, 0.25 * atr_v)
        tp_p = close_p + 2.0 * risk_d
        return {
            "decision": "LONG",
            "confidence": min(95.0, max(60.0, smc_score)),
            "entry": close_p,
            "stop_loss": sl_p,
            "take_profit": tp_p,
            "reason": "SMC READY_LONG + Bullish structure",
        }

    if timing == "READY_SHORT" and bias == "BEARISH" and atr_v > 0:
        z_hi = smc_ctx.get("bearish_entry", {}).get("zone_high")
        sl_anchor = float(z_hi) if is_finite_number(z_hi) and float(z_hi) > 0 else (close_p + atr_v)
        sl_p = max(close_p + 0.5 * atr_v, sl_anchor + 0.1 * atr_v)
        risk_d = max(sl_p - close_p, 0.25 * atr_v)
        tp_p = max(1e-8, close_p - 2.0 * risk_d)
        return {
            "decision": "SHORT",
            "confidence": min(95.0, max(60.0, smc_score)),
            "entry": close_p,
            "stop_loss": sl_p,
            "take_profit": tp_p,
            "reason": "SMC READY_SHORT + Bearish structure",
        }

    return {"decision": "WAIT", "confidence": 40.0, "reason": f"Timing={timing}, Bias={bias}"}


def run_walk_forward_replay_backtest(
    df: pd.DataFrame,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    mode: str = "scalping",
    initial_equity: float = 10_000.0,
    risk_fraction: float = 0.005,
    fee_pct_round_trip: float = 0.08,
    slippage_pct_round_trip: float = 0.04,
    spread_pct: float = 0.02,
    funding_rate_pct_per_8h: float = 0.0,
    instrument_mode: str = "spot_reference",
    tie_breaker: str = IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value,
    max_wait_bars: int = 4,
    max_hold_bars: int = 12,
    require_ema200_warmup: Optional[bool] = None,
    min_warmup_bars: Optional[int] = None,
    start_bar_index: Optional[int] = None,
    end_bar_index: Optional[int] = None,
    higher_timeframe_dfs: Optional[Dict[str, pd.DataFrame]] = None,
    min_score_threshold: float = 0.0,
    strategy_version: str = "v6.2-momentum-smc",
    signal_generator_fn: Optional[Callable[[pd.DataFrame, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Deterministic candle-by-candle walk-forward replay backtesting harness (Phase 2.1 / PH06-02 / F04, F10, F36 / T25):
    - Strictly replays closed candles sequentially from `start_bar_index` to `end_bar_index` with zero look-ahead.
    - At each bar `i`, only `df.iloc[:i+1]` is visible to indicator computation, SMC analysis, and signal generation.
    - Aligns any `higher_timeframe_dfs` (`1h`, `4h`, `1d`) via `align_higher_timeframes_as_of(bar_close_ms)` so unclosed
      higher-timeframe candles never leak future data!
    - Enforces EMA200 warm-up gating (`require_ema200_warmup` defaults to True for intraday/swing).
    - Uses `compute_position_sizing` and `evaluate_position_on_bars` with full fee/slippage/spread/funding accounting.
    - Records reproducible backtest parameters, strategy version, indicator version, and data range.
    """
    from .features import INDICATOR_VERSION, MIN_BARS_CORE_INDICATORS, MIN_BARS_EMA200, compute_indicators_frame, safe_market_regime
    from .risk import compute_position_sizing
    from .smc import smc_analysis

    eff_funding = float(funding_rate_pct_per_8h) if funding_rate_pct_per_8h > 0 else (0.01 if str(instrument_mode).lower() == "futures_perp" else 0.0)
    params_record = {
        "symbol": symbol,
        "interval": interval,
        "mode": mode,
        "initial_equity": float(initial_equity),
        "risk_fraction": float(risk_fraction),
        "fee_pct_round_trip": float(fee_pct_round_trip),
        "slippage_pct_round_trip": float(slippage_pct_round_trip),
        "spread_pct": float(spread_pct),
        "funding_rate_pct_per_8h": eff_funding,
        "instrument_mode": instrument_mode,
        "tie_breaker": tie_breaker,
        "max_wait_bars": int(max_wait_bars),
        "max_hold_bars": int(max_hold_bars),
        "min_score_threshold": float(min_score_threshold),
        "strategy_version": strategy_version,
        "indicator_version": INDICATOR_VERSION,
    }

    if df is None or len(df) == 0:
        empty_an = compute_expectancy_and_drawdown_analytics([], initial_equity=initial_equity, dataset_type="BACKTEST")
        empty_cal = compute_confidence_calibration_report([], evaluation_scope="OUT_OF_SAMPLE")
        repro_hash = hashlib.sha256(json.dumps(params_record, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return {
            "backtest_id": f"bt_{symbol}_{repro_hash}",
            "dataset_type": "BACKTEST",
            "symbol": symbol,
            "interval": interval,
            "mode": mode,
            "strategy_version": strategy_version,
            "indicator_version": INDICATOR_VERSION,
            "parameters": params_record,
            "reproducibility_hash": repro_hash,
            "data_range": {"start_time": None, "end_time": None, "total_bars": 0},
            "total_bars": 0,
            "evaluated_bars": 0,
            "warmup_gated_bars": 0,
            "signals_generated": 0,
            "wait_observations": 0,
            "closed_trades": [],
            "open_positions": [],
            "ledger_events": [],
            "analytics": empty_an,
            "calibration": empty_cal,
            "initial_equity": round(float(initial_equity), 4),
            "final_equity": round(float(initial_equity), 4),
            "net_return_pct": 0.0,
            "accounting_reconciled": True,
            "no_lookahead_verified": True,
            "htf_alignment_verified": True,
        }

    raw_df = df.copy(deep=True)
    raw_df.attrs["_is_closed_df"] = True
    n_total = len(raw_df)

    mode_norm = str(mode or "scalping").lower().strip()
    enforce_ema200 = (
        bool(require_ema200_warmup)
        if require_ema200_warmup is not None
        else (mode_norm in {"intraday", "swing"})
    )
    eff_warmup_bars = (
        int(min_warmup_bars)
        if min_warmup_bars is not None
        else (MIN_BARS_EMA200 if enforce_ema200 else MIN_BARS_CORE_INDICATORS)
    )

    full_ind_df = compute_indicators_frame(raw_df, interval=interval, drop_unwarmed=False)
    full_ind_df.attrs["_is_closed_df"] = True

    s_idx = max(0, int(start_bar_index)) if start_bar_index is not None else 0
    e_idx = min(n_total - 1, int(end_bar_index)) if end_bar_index is not None else (n_total - 1)

    data_range_info = {
        "start_time": str(full_ind_df.index[s_idx]),
        "end_time": str(full_ind_df.index[e_idx]),
        "start_bar_index": s_idx,
        "end_bar_index": e_idx,
        "total_bars": n_total,
    }
    repro_payload = {**params_record, "data_range": data_range_info}
    repro_hash = hashlib.sha256(json.dumps(repro_payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    running_equity = float(initial_equity)
    active_pos: Optional[Dict[str, Any]] = None
    closed_trades: List[Dict[str, Any]] = []
    ledger_events: List[Dict[str, Any]] = []
    warmup_gated_bars = 0
    signals_generated = 0
    wait_observations = 0
    evaluated_bars = 0
    htf_leakage_detected = False
    gen_fn = signal_generator_fn or _default_replay_signal_generator

    for i in range(s_idx, e_idx + 1):
        evaluated_bars += 1
        bar_row = full_ind_df.iloc[i]
        bar_ts = str(full_ind_df.index[i])
        bar_close_ms = (
            normalize_as_of_ms(bar_row["close_time"])
            if ("close_time" in full_ind_df.columns and pd.notna(bar_row["close_time"]))
            else normalize_as_of_ms(full_ind_df.index[i])
        )
        single_bar_list = [{
            "Open": float(bar_row["Open"]),
            "High": float(bar_row["High"]),
            "Low": float(bar_row["Low"]),
            "Close": float(bar_row["Close"]),
            "timestamp": bar_ts,
            "close_time": bar_close_ms,
        }]

        # Step 1: If there is an active position opened on an earlier bar, evaluate it on bar `i`
        if active_pos is not None:
            eval_pos = evaluate_position_on_bars(
                active_pos,
                bars=single_bar_list,
                tie_breaker=tie_breaker,
                max_wait_bars=max_wait_bars,
                max_hold_bars=max_hold_bars,
                interval=interval,
                now_iso=bar_ts,
                funding_rate_pct_per_8h=eff_funding,
            )
            active_pos = eval_pos
            if str(active_pos["state"]) in TERMINAL_POSITION_STATES:
                if str(active_pos["state"]) in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP"}:
                    net_pnl_v = float(active_pos.get("net_pnl") or 0.0)
                    eq_before = running_equity
                    running_equity = round(eq_before + net_pnl_v, 4)
                    ledger_events.append({
                        "event_type": "POSITION_CLOSED",
                        "position_id": active_pos["position_id"],
                        "bar_index": i,
                        "timestamp": bar_ts,
                        "equity_before": eq_before,
                        "equity_after": running_equity,
                        "gross_pnl": float(active_pos.get("gross_pnl") or 0.0),
                        "total_cost": float(active_pos.get("total_cost") or 0.0),
                        "funding_cost": float(active_pos.get("funding_cost") or 0.0),
                        "net_pnl": net_pnl_v,
                    })
                    closed_trades.append(copy.deepcopy(active_pos))
                active_pos = None
                continue
            else:
                continue

        # Step 2: Check warm-up gate (F10, T25) — bar `i` has `i + 1` closed bars available
        bars_available = i + 1
        atr_val = float(bar_row["ATR"]) if ("ATR" in full_ind_df.columns and pd.notna(bar_row["ATR"])) else 0.0
        rsi_val = float(bar_row["RSI"]) if ("RSI" in full_ind_df.columns and pd.notna(bar_row["RSI"])) else 50.0
        ema200_ready = bars_available >= MIN_BARS_EMA200

        if bars_available < eff_warmup_bars or (enforce_ema200 and not ema200_ready) or atr_val <= 0.0:
            warmup_gated_bars += 1
            wait_observations += 1
            continue

        # Step 3: Build strictly causal prefix slice `visible_df` (bars 0..i inclusive)
        visible_df = full_ind_df.iloc[max(0, i - 140) : i + 1].copy()
        visible_df.attrs["_is_closed_df"] = True
        visible_df.attrs["ema200_ready"] = ema200_ready
        visible_df.attrs["warmup_status"] = "READY" if ema200_ready else "PARTIAL_WARMUP_NO_EMA200"

        # Align higher timeframes without future-data leakage (Phase 2.1)
        aligned_htf_smc: Dict[str, Any] = {}
        if isinstance(higher_timeframe_dfs, dict) and higher_timeframe_dfs:
            aligned_htfs = align_higher_timeframes_as_of(higher_timeframe_dfs, bar_close_ms)
            for htf_name, htf_slice in aligned_htfs.items():
                if len(htf_slice) > 0:
                    if "close_time" in htf_slice.columns and (htf_slice["close_time"] > bar_close_ms).any():
                        htf_leakage_detected = True
                    htf_ind = compute_indicators_frame(htf_slice, interval=htf_name, drop_unwarmed=False)
                    htf_ind.attrs["_is_closed_df"] = True
                    aligned_htf_smc[htf_name] = smc_analysis(htf_ind, interval=htf_name)

        smc_ctx = smc_analysis(visible_df, interval=interval)
        regime_now = safe_market_regime(visible_df)
        bar_info = {
            "bar_index": i,
            "bars_available": bars_available,
            "timestamp": bar_ts,
            "close_ms": bar_close_ms,
            "open": float(bar_row["Open"]),
            "high": float(bar_row["High"]),
            "low": float(bar_row["Low"]),
            "close": float(bar_row["Close"]),
            "atr": atr_val,
            "rsi": rsi_val,
            "ema200_ready": ema200_ready,
            "market_regime": regime_now,
            "min_score_threshold": min_score_threshold,
            "aligned_htf_smc": aligned_htf_smc,
        }

        proposal = gen_fn(visible_df, smc_ctx, bar_info) or {}
        dec = str(proposal.get("decision", "WAIT")).upper().strip()
        if dec not in {"LONG", "SHORT"}:
            wait_observations += 1
            continue

        entry_p = float(proposal.get("entry") or bar_info["close"])
        sl_p = float(proposal.get("stop_loss") or 0.0)
        tp_p = float(proposal.get("take_profit") or 0.0)
        conf_v = float(proposal.get("confidence", 75.0) or 75.0)
        order_type = str(proposal.get("order_state") or PaperPositionState.OPEN.value).upper().strip()

        sizing = compute_position_sizing(
            dec,
            entry_p,
            sl_p,
            tp_p,
            symbol=symbol,
            equity=running_equity,
            risk_fraction=risk_fraction,
            fee_pct_round_trip=fee_pct_round_trip,
            slippage_pct_round_trip=slippage_pct_round_trip,
            spread_pct=spread_pct,
            instrument_mode=instrument_mode,
        )
        if not sizing.valid:
            wait_observations += 1
            continue

        signals_generated += 1
        pos_id = f"bt_{symbol}_{i}"
        active_pos = {
            "position_id": pos_id,
            "symbol": symbol,
            "timeframe": interval,
            "mode": mode_norm,
            "strategy_version": strategy_version,
            "market_regime": regime_now,
            "direction": dec,
            "state": (
                PaperPositionState.PENDING_ENTRY.value
                if order_type == PaperPositionState.PENDING_ENTRY.value
                else PaperPositionState.OPEN.value
            ),
            "confidence": conf_v,
            "entry": sizing.entry,
            "stop_loss": sizing.stop_loss,
            "take_profit": sizing.take_profit,
            "entry_zone_low": proposal.get("entry_zone_low"),
            "entry_zone_high": proposal.get("entry_zone_high"),
            "quantity": sizing.quantity,
            "notional_value": sizing.notional_value,
            "margin_required": sizing.margin_required,
            "risk_budget": sizing.risk_budget,
            "fee_pct_round_trip": fee_pct_round_trip,
            "slippage_pct_round_trip": slippage_pct_round_trip,
            "spread_pct": spread_pct,
            "funding_rate_pct_per_8h": eff_funding,
            "max_wait_bars": max_wait_bars,
            "max_hold_bars": max_hold_bars,
            "tie_breaker_policy": tie_breaker,
            "created_at": bar_ts,
            "opened_at": bar_ts if order_type != PaperPositionState.PENDING_ENTRY.value else None,
            "last_evaluated_bar_time": bar_ts,
            "bar_index_opened": i,
            "origin": "v6_backtest",
            "accounting_quality": REALISTIC_ACCOUNTING_QUALITY,
        }

    analytics = compute_expectancy_and_drawdown_analytics(
        closed_trades, initial_equity=initial_equity, dataset_type="BACKTEST"
    )
    calibration = compute_confidence_calibration_report(
        closed_trades, evaluation_scope="OUT_OF_SAMPLE"
    )
    reconciled = bool(
        math.isclose(running_equity, float(initial_equity) + analytics["total_net_pnl"], rel_tol=1e-9, abs_tol=1e-5)
        and math.isclose(analytics["total_net_pnl"], analytics["total_gross_pnl"] - analytics["total_cost"], rel_tol=1e-9, abs_tol=1e-5)
    )

    return {
        "backtest_id": f"bt_{symbol}_{repro_hash}",
        "dataset_type": "BACKTEST",
        "symbol": symbol,
        "interval": interval,
        "mode": mode_norm,
        "strategy_version": strategy_version,
        "indicator_version": INDICATOR_VERSION,
        "parameters": params_record,
        "reproducibility_hash": repro_hash,
        "data_range": data_range_info,
        "total_bars": n_total,
        "evaluated_bars": evaluated_bars,
        "warmup_gated_bars": warmup_gated_bars,
        "signals_generated": signals_generated,
        "wait_observations": wait_observations,
        "closed_trades": closed_trades,
        "open_positions": [active_pos] if active_pos is not None else [],
        "ledger_events": ledger_events,
        "analytics": analytics,
        "calibration": calibration,
        "initial_equity": round(float(initial_equity), 4),
        "final_equity": running_equity,
        "net_return_pct": analytics["net_return_pct"],
        "accounting_reconciled": reconciled,
        "no_lookahead_verified": not htf_leakage_detected,
        "htf_alignment_verified": not htf_leakage_detected,
    }


def run_walk_forward_folds(
    df: pd.DataFrame,
    *,
    train_bars: int = 200,
    val_bars: int = 0,
    test_bars: int = 40,
    step_bars: Optional[int] = None,
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    mode: str = "scalping",
    initial_equity: float = 10_000.0,
    risk_fraction: float = 0.005,
    fee_pct_round_trip: float = 0.08,
    slippage_pct_round_trip: float = 0.04,
    spread_pct: float = 0.02,
    funding_rate_pct_per_8h: float = 0.0,
    tie_breaker: str = IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value,
    max_hold_bars: int = 12,
    require_ema200_warmup: Optional[bool] = None,
    parameter_grid: Optional[List[Dict[str, Any]]] = None,
    signal_generator_fn: Optional[Callable[[pd.DataFrame, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Execute multi-fold walk-forward evaluation separating:
      1. Training period (`train_bars`)
      2. Validation / parameter-selection period (`val_bars`)
      3. Out-of-sample evaluation period (`test_bars`)
    Prevents information leakage across boundaries and never optimizes strategy parameters on the
    out-of-sample evaluation window (Phase 2.2 / PH06-02 / T25).
    """
    t_train = max(20, int(train_bars))
    t_val = max(0, int(val_bars or 0))
    t_test = max(5, int(test_bars))
    min_required = t_train + t_val + t_test

    methodology_limitations = [
        "Data historis OHLCV tidak mencakup antrean mikrostruktur order-book L2 secara penuh di dalam bar.",
        "Ketika TP dan SL tersentuh pada candle yang sama, sistem menerapkan aturan konservatif SL_FIRST_CONSERVATIVE.",
        "Pemilihan parameter (jika diaktifkan) dibatasi ketat pada jendela Train + Validation sebelum diuji secara terkunci pada Out-of-Sample.",
    ]

    if df is None or len(df) < min_required:
        return {
            "dataset_type": "WALK_FORWARD_OUT_OF_SAMPLE",
            "symbol": symbol,
            "folds_count": 0,
            "folds": [],
            "validation_trades": [],
            "out_of_sample_trades": [],
            "out_of_sample_analytics": compute_expectancy_and_drawdown_analytics([], initial_equity=initial_equity, dataset_type="WALK_FORWARD_OUT_OF_SAMPLE"),
            "out_of_sample_calibration": compute_confidence_calibration_report([], evaluation_scope="OUT_OF_SAMPLE"),
            "initial_equity": round(float(initial_equity), 4),
            "final_equity": round(float(initial_equity), 4),
            "accounting_reconciled": True,
            "no_lookahead_verified": True,
            "boundary_leakage_detected": False,
            "methodology_limitations": methodology_limitations,
        }

    t_step = max(1, int(step_bars if step_bars is not None else t_test))
    n_total = len(df)

    folds: List[Dict[str, Any]] = []
    val_trades_all: List[Dict[str, Any]] = []
    oos_trades: List[Dict[str, Any]] = []
    running_eq = float(initial_equity)
    boundary_leakage = False

    fold_idx = 0
    val_start = t_train
    while val_start + t_val + t_test <= n_total:
        fold_idx += 1
        train_start_idx = max(0, val_start - t_train)
        train_end_idx = val_start - 1
        val_end_idx = val_start + t_val - 1 if t_val > 0 else train_end_idx
        test_start_idx = val_start + t_val
        test_end_idx = test_start_idx + t_test - 1

        if not (train_end_idx < test_start_idx and val_end_idx < test_start_idx):
            boundary_leakage = True

        # Optional parameter selection on Train/Validation window ONLY (never touching test_start_idx..test_end_idx)
        selected_params = {"min_score_threshold": 0.0, "max_hold_bars": max_hold_bars}
        val_net_pnl = 0.0
        if t_val > 0:
            val_df = df.iloc[: val_end_idx + 1].copy()
            cand_grid = parameter_grid if (isinstance(parameter_grid, list) and parameter_grid) else [selected_params]
            best_score = float("-inf")
            best_val_trades: List[Dict[str, Any]] = []
            for cand_p in cand_grid:
                cand_hold = int(cand_p.get("max_hold_bars", max_hold_bars))
                cand_thresh = float(cand_p.get("min_score_threshold", 0.0))
                v_res = run_walk_forward_replay_backtest(
                    val_df,
                    symbol=symbol,
                    interval=interval,
                    mode=mode,
                    initial_equity=initial_equity,
                    risk_fraction=risk_fraction,
                    fee_pct_round_trip=fee_pct_round_trip,
                    slippage_pct_round_trip=slippage_pct_round_trip,
                    spread_pct=spread_pct,
                    funding_rate_pct_per_8h=funding_rate_pct_per_8h,
                    tie_breaker=tie_breaker,
                    max_hold_bars=cand_hold,
                    require_ema200_warmup=require_ema200_warmup,
                    start_bar_index=val_start,
                    end_bar_index=val_end_idx,
                    min_score_threshold=cand_thresh,
                    signal_generator_fn=signal_generator_fn,
                )
                v_metric = float(v_res["analytics"]["total_net_pnl"])
                if v_metric > best_score:
                    best_score = v_metric
                    selected_params = {"min_score_threshold": cand_thresh, "max_hold_bars": cand_hold}
                    best_val_trades = v_res["closed_trades"]
                    val_net_pnl = v_metric
            for vt in best_val_trades:
                vt_c = dict(vt)
                vt_c["fold"] = fold_idx
                vt_c["period"] = "VALIDATION"
                val_trades_all.append(vt_c)

        # Locked Out-of-Sample Evaluation on [test_start_idx .. test_end_idx]
        fold_df = df.iloc[: test_end_idx + 1].copy()
        fold_res = run_walk_forward_replay_backtest(
            fold_df,
            symbol=symbol,
            interval=interval,
            mode=mode,
            initial_equity=running_eq,
            risk_fraction=risk_fraction,
            fee_pct_round_trip=fee_pct_round_trip,
            slippage_pct_round_trip=slippage_pct_round_trip,
            spread_pct=spread_pct,
            funding_rate_pct_per_8h=funding_rate_pct_per_8h,
            tie_breaker=tie_breaker,
            max_hold_bars=int(selected_params.get("max_hold_bars", max_hold_bars)),
            require_ema200_warmup=require_ema200_warmup,
            start_bar_index=test_start_idx,
            end_bar_index=test_end_idx,
            min_score_threshold=float(selected_params.get("min_score_threshold", 0.0)),
            signal_generator_fn=signal_generator_fn,
        )
        running_eq = fold_res["final_equity"]
        for tr in fold_res["closed_trades"]:
            tr_copy = dict(tr)
            tr_copy["fold"] = fold_idx
            tr_copy["period"] = "OUT_OF_SAMPLE"
            oos_trades.append(tr_copy)

        folds.append({
            "fold": fold_idx,
            "train_start_index": train_start_idx,
            "train_end_index": train_end_idx,
            "validation_start_index": val_start if t_val > 0 else None,
            "validation_end_index": val_end_idx if t_val > 0 else None,
            "test_start_index": test_start_idx,
            "test_end_index": test_end_idx,
            "selected_parameters": selected_params,
            "validation_net_pnl": val_net_pnl,
            "closed_trades_count": len(fold_res["closed_trades"]),
            "signals_generated": fold_res["signals_generated"],
            "fold_net_pnl": fold_res["analytics"]["total_net_pnl"],
            "fold_final_equity": fold_res["final_equity"],
        })
        val_start += t_step

    oos_analytics = compute_expectancy_and_drawdown_analytics(
        oos_trades, initial_equity=initial_equity, dataset_type="WALK_FORWARD_OUT_OF_SAMPLE"
    )
    oos_calibration = compute_confidence_calibration_report(
        oos_trades, evaluation_scope="OUT_OF_SAMPLE"
    )
    reconciled = math.isclose(running_eq, float(initial_equity) + oos_analytics["total_net_pnl"], rel_tol=1e-9, abs_tol=1e-4)

    return {
        "dataset_type": "WALK_FORWARD_OUT_OF_SAMPLE",
        "symbol": symbol,
        "interval": interval,
        "mode": mode,
        "folds_count": len(folds),
        "folds": folds,
        "validation_trades": val_trades_all,
        "out_of_sample_trades": oos_trades,
        "out_of_sample_analytics": oos_analytics,
        "out_of_sample_calibration": oos_calibration,
        "initial_equity": round(float(initial_equity), 4),
        "final_equity": running_eq,
        "accounting_reconciled": bool(reconciled),
        "no_lookahead_verified": not boundary_leakage,
        "boundary_leakage_detected": boundary_leakage,
        "methodology_limitations": methodology_limitations,
    }

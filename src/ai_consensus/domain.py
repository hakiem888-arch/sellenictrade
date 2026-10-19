"""
Core domain contracts, enums, pure numeric validators, signal lifecycle state machine,
orthogonal momentum evidence builder, and deterministic gate evaluation logic.
Strictly decoupled from Streamlit UI, database migrations, and HTTP networks
(PH01..PH06 / F16, F17, F34, F38).
Strictly zero side-effects on import.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


class DecisionAction(str, Enum):
    """Gated decision action for proposals and signals."""
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class SignalLifecycleState(str, Enum):
    """Canonical signal lifecycle states (Phase 1.2)."""
    DEVELOPING = "DEVELOPING"
    TRIGGERED = "TRIGGERED"
    WAIT = "WAIT"
    INVALIDATED = "INVALIDATED"


class DirectionalBias(str, Enum):
    """Directional market structure or agent bias."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class MarketRegime(str, Enum):
    """Market classification."""
    TRENDING = "TRENDING"
    VOLATILE = "VOLATILE"
    SIDEWAYS = "SIDEWAYS"


class TimingStatus(str, Enum):
    """SMC setup timing status."""
    READY_LONG = "READY_LONG"
    READY_SHORT = "READY_SHORT"
    WAIT_RETEST = "WAIT_RETEST"
    LATE = "LATE"


class AgentStatus(str, Enum):
    """Execution status of an AI agent role."""
    OK = "OK"
    SKIPPED = "SKIPPED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    FAILED = "FAILED"


class DataStatus(str, Enum):
    """Data readiness status."""
    COMPLETE = "COMPLETE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class TradingMode(str, Enum):
    """Trading strategy time horizon modes."""
    SCALPING = "scalping"
    INTRADAY = "intraday"
    SWING = "swing"


CORE_MARKET_TIMEFRAMES: Tuple[str, ...] = ("5m", "15m", "1h")
HIGHER_MARKET_TIMEFRAMES: Tuple[str, ...] = ("4h", "1d")
ALL_MARKET_TIMEFRAMES: Tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")

TIMEFRAME_SECONDS_MAP: Dict[str, int] = {
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

# Mode timeframe requirement matrix (PH01-03 / PH02-01 / PH04-01 / F17 / T15)
MODE_TIMEFRAME_MATRIX: Dict[str, Dict[str, Any]] = {
    "scalping": {
        "bias_tf": "1h",
        "setup_tf": "15m",
        "trigger_tf": "5m",
        "required_timeframes": ("5m", "15m", "1h"),
        "min_warmup_bars": 200,
        "signal_expiry_bars": 6,
    },
    "intraday": {
        "bias_tf": "4h",
        "setup_tf": "1h",
        "trigger_tf": "15m",
        "required_timeframes": ("15m", "1h", "4h"),
        "min_warmup_bars": 200,
        "signal_expiry_bars": 6,
    },
    "swing": {
        "bias_tf": "1d",
        "setup_tf": "4h",
        "trigger_tf": "1h",
        "required_timeframes": ("1h", "4h", "1d"),
        "min_warmup_bars": 200,
        "signal_expiry_bars": 6,
    },
}

_ALLOWED_DECISIONS = {DecisionAction.LONG.value, DecisionAction.SHORT.value, DecisionAction.WAIT.value}
_ALLOWED_MODES = (TradingMode.SCALPING.value, TradingMode.INTRADAY.value, TradingMode.SWING.value)
_REQUIRED_AI_FIELDS = ("decision", "confidence", "reasoning", "key_risk", "evidence")
_ALLOWED_AI_FIELDS = {"decision", "confidence", "reasoning", "key_risk", "evidence", "modes"}
_REQUIRED_MODE_FIELDS = {"decision", "confidence", "reason"}

# Explicit Signal Lifecycle transition table (Phase 1.2)
ALLOWED_SIGNAL_LIFECYCLE_TRANSITIONS: Dict[str, frozenset] = {
    SignalLifecycleState.WAIT.value: frozenset({
        SignalLifecycleState.WAIT.value,
        SignalLifecycleState.DEVELOPING.value,
        SignalLifecycleState.TRIGGERED.value,
        SignalLifecycleState.INVALIDATED.value,
    }),
    SignalLifecycleState.DEVELOPING.value: frozenset({
        SignalLifecycleState.DEVELOPING.value,
        SignalLifecycleState.TRIGGERED.value,
        SignalLifecycleState.WAIT.value,
        SignalLifecycleState.INVALIDATED.value,
    }),
    SignalLifecycleState.TRIGGERED.value: frozenset({
        SignalLifecycleState.TRIGGERED.value,
        SignalLifecycleState.WAIT.value,
        SignalLifecycleState.INVALIDATED.value,
    }),
    SignalLifecycleState.INVALIDATED.value: frozenset({
        SignalLifecycleState.INVALIDATED.value,
    }),
}


def normalize_signal_lifecycle_state(raw_state: Any) -> str:
    """
    Map any legacy or raw setup state ('WATCH', 'QUIET', 'OBSERVED_WAIT', etc.)
    to the canonical SignalLifecycleState ('DEVELOPING', 'TRIGGERED', 'WAIT', 'INVALIDATED').
    """
    s = str(raw_state or "").upper().strip()
    if s == SignalLifecycleState.TRIGGERED.value:
        return SignalLifecycleState.TRIGGERED.value
    if s == SignalLifecycleState.DEVELOPING.value:
        return SignalLifecycleState.DEVELOPING.value
    if s in {SignalLifecycleState.INVALIDATED.value, "CLOSED_INVALIDATED", "EXPIRED", "EXPIRED_UNFILLED"}:
        return SignalLifecycleState.INVALIDATED.value
    return SignalLifecycleState.WAIT.value


def validate_signal_lifecycle_transition(from_state: str, to_state: str) -> bool:
    """
    Validate that a signal lifecycle transition is permitted by the state machine (Phase 1.2).
    Raises ValueError on invalid transitions (e.g., INVALIDATED -> TRIGGERED or TRIGGERED -> DEVELOPING).
    """
    src_raw = str(from_state or "").upper().strip()
    dst_raw = str(to_state or "").upper().strip()
    valid_states = {s.value for s in SignalLifecycleState}
    if src_raw in {"WATCH", "QUIET", "OBSERVED_WAIT"}:
        src = SignalLifecycleState.WAIT.value
    elif src_raw in valid_states:
        src = src_raw
    else:
        raise ValueError(f"UNKNOWN_SIGNAL_STATE: State asal tidak dikenal '{from_state}'.")

    if dst_raw in {"WATCH", "QUIET", "OBSERVED_WAIT"}:
        dst = SignalLifecycleState.WAIT.value
    elif dst_raw in valid_states:
        dst = dst_raw
    else:
        raise ValueError(f"UNKNOWN_SIGNAL_STATE: State tujuan tidak dikenal '{to_state}'.")

    allowed = ALLOWED_SIGNAL_LIFECYCLE_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        raise ValueError(
            f"ILLEGAL_SIGNAL_STATE_TRANSITION: Transisi signal lifecycle tidak sah dari '{src}' ke '{dst}'."
        )
    return True


# ---------------------------------------------------------
# Numeric & finite validators
# ---------------------------------------------------------
def is_finite_number(value: Any) -> bool:
    """Return True iff value is an int or float (not boolean) and is finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def is_valid_confidence(value: Any) -> bool:
    """Strictly validate confidence: finite int/float in [0.0, 100.0], rejecting bool, str, NaN, inf."""
    if not is_finite_number(value):
        return False
    val = float(value)
    return 0.0 <= val <= 100.0


def sanitize_confidence(value: Any) -> float:
    """Sanitize confidence for compact representations, falling back safely to 0."""
    if is_valid_confidence(value):
        return round(float(value), 2)
    return 0.0


# Backward compatibility aliases
_is_finite_number = is_finite_number
_is_valid_finite_confidence = is_valid_confidence
_sanitize_confidence_for_summary = sanitize_confidence


# ---------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------
@dataclass
class TradePlan:
    """Validated theoretical trade levels, invalidation level, and risk/reward metrics."""
    direction: str
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    gross_rr: float
    net_rr: float
    valid: bool
    veto_codes: List[str] = field(default_factory=list)
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    tp_targets: List[float] = field(default_factory=list)
    risk_per_unit: float = 0.0
    reward_per_unit: float = 0.0
    invalidation_level: Optional[float] = None
    invalidation_reason: str = ""
    level_type: str = "THEORETICAL_QUOTE"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "tp_targets": list(self.tp_targets),
            "risk_per_unit": self.risk_per_unit,
            "reward_per_unit": self.reward_per_unit,
            "invalidation_level": self.invalidation_level,
            "invalidation_reason": self.invalidation_reason,
            "gross_rr": self.gross_rr,
            "net_rr": self.net_rr,
            "valid": self.valid,
            "veto_codes": list(self.veto_codes),
            "level_type": self.level_type,
        }


@dataclass
class GateReport:
    """Audit report from deterministic preflight and post-AI gates."""
    approved: bool
    stage: str
    raw_model_decision: str
    gated_decision: str
    veto_codes: List[str]
    veto_summary: str
    net_rr: float


@dataclass
class ModeDecisionData:
    """Per-mode strategy evaluation."""
    mode: str
    required_timeframes: List[str]
    missing_timeframes: List[str]
    data_status: str
    directional_bias: str
    setup_state: str
    raw_model_decision: str
    entry_action: str
    decision: str
    risk_approved: bool
    veto_codes: List[str]
    confidence: float
    reason: str
    unwarmed_timeframes: List[str] = field(default_factory=list)


@dataclass
class SignalLifecycleRecord:
    """Explicit signal lifecycle state record with timestamps, IDs, and expiry/invalidation rules (Phase 1.2)."""
    signal_id: str
    symbol: str
    mode: str
    state: str
    previous_state: Optional[str]
    direction: str
    score: float
    created_at: str
    updated_at: str
    bar_time: str
    triggered_at: Optional[str] = None
    expires_at: Optional[str] = None
    invalidated_at: Optional[str] = None
    invalidation_level: Optional[float] = None
    invalidation_reason: str = ""
    transition_valid: bool = True
    is_expired: bool = False
    is_actionable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "mode": self.mode,
            "state": self.state,
            "previous_state": self.previous_state,
            "direction": self.direction,
            "score": self.score,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "bar_time": self.bar_time,
            "triggered_at": self.triggered_at,
            "expires_at": self.expires_at,
            "invalidated_at": self.invalidated_at,
            "invalidation_level": self.invalidation_level,
            "invalidation_reason": self.invalidation_reason,
            "transition_valid": self.transition_valid,
            "is_expired": self.is_expired,
            "is_actionable": self.is_actionable,
        }


# ---------------------------------------------------------
# Pure Domain Logic: Entry, SL, TP, R:R & Lifecycle
# ---------------------------------------------------------
def compute_directional_risk_reward(
    direction: str,
    entry: Any,
    stop_loss: Any,
    take_profit: Any,
) -> Dict[str, Any]:
    """
    Compute exact directional risk per unit and potential reward per unit (Phase 1.4):
    - LONG:  Risk per unit = Entry - Stop-Loss; Reward per unit = Take-Profit - Entry
    - SHORT: Risk per unit = Stop-Loss - Entry; Reward per unit = Entry - Take-Profit
    """
    dir_norm = str(direction or "WAIT").upper().strip()
    if dir_norm not in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return {
            "valid": False,
            "direction": dir_norm,
            "risk_per_unit": 0.0,
            "reward_per_unit": 0.0,
            "gross_rr": 0.0,
            "veto_codes": ["WAIT_DECISION"],
        }

    for v in (entry, stop_loss, take_profit):
        if not is_finite_number(v):
            return {
                "valid": False,
                "direction": dir_norm,
                "risk_per_unit": 0.0,
                "reward_per_unit": 0.0,
                "gross_rr": 0.0,
                "veto_codes": ["INVALID_NUMERIC"],
            }

    entry_f = float(entry)
    sl_f = float(stop_loss)
    tp_f = float(take_profit)
    vetoes: List[str] = []

    if entry_f <= 0.0 or sl_f <= 0.0 or tp_f <= 0.0:
        vetoes.append("NON_POSITIVE_PRICE")
        vetoes.append("INVALID_SL_TP")
        return {
            "valid": False,
            "direction": dir_norm,
            "risk_per_unit": 0.0,
            "reward_per_unit": 0.0,
            "gross_rr": 0.0,
            "veto_codes": vetoes,
        }

    if dir_norm == DecisionAction.LONG.value:
        risk_per_unit = entry_f - sl_f
        reward_per_unit = tp_f - entry_f
    else:
        risk_per_unit = sl_f - entry_f
        reward_per_unit = entry_f - tp_f

    if risk_per_unit <= 1e-12 or reward_per_unit <= 1e-12:
        vetoes.append("INVALID_SL_TP")
        return {
            "valid": False,
            "direction": dir_norm,
            "risk_per_unit": round(risk_per_unit, 8),
            "reward_per_unit": round(reward_per_unit, 8),
            "gross_rr": 0.0,
            "veto_codes": vetoes,
        }

    gross_rr = reward_per_unit / risk_per_unit
    return {
        "valid": True,
        "direction": dir_norm,
        "risk_per_unit": round(risk_per_unit, 8),
        "reward_per_unit": round(reward_per_unit, 8),
        "gross_rr": round(gross_rr, 4),
        "veto_codes": [],
    }


def validate_trade_plan_gate(
    direction: str,
    entry: Any,
    sl: Any,
    tp: Any,
    gross_rr: Any = 2.0,
    spread_pct: Any = 0.0,
    min_net_rr: float = 1.5,
    round_trip_cost_pct: float = 0.12,
) -> Dict[str, Any]:
    """Deterministic post-AI validation of entry/SL/TP ordering, positive finite numbers, and net R:R (F38, Phase 1.4)."""
    veto_codes: List[str] = []
    if direction not in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return {"valid": False, "veto_codes": [], "net_rr": 0.0, "risk_per_unit": 0.0, "reward_per_unit": 0.0}

    for v in (entry, sl, tp, gross_rr, spread_pct):
        if not is_finite_number(v):
            veto_codes.append("INVALID_NUMERIC")
            return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0, "risk_per_unit": 0.0, "reward_per_unit": 0.0}

    entry_f, sl_f, tp_f = float(entry), float(sl), float(tp)
    if entry_f <= 0 or sl_f <= 0 or tp_f <= 0:
        veto_codes.append("NON_POSITIVE_PRICE")
        veto_codes.append("INVALID_SL_TP")
        return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0, "risk_per_unit": 0.0, "reward_per_unit": 0.0}

    rr_calc = compute_directional_risk_reward(direction, entry_f, sl_f, tp_f)
    if not rr_calc["valid"]:
        for vc in rr_calc["veto_codes"]:
            if vc not in veto_codes:
                veto_codes.append(vc)
        return {
            "valid": False,
            "veto_codes": veto_codes,
            "net_rr": 0.0,
            "risk_per_unit": rr_calc["risk_per_unit"],
            "reward_per_unit": rr_calc["reward_per_unit"],
        }

    risk_per_unit = rr_calc["risk_per_unit"]
    reward_per_unit = rr_calc["reward_per_unit"]
    cost_abs = entry_f * ((max(0.0, float(spread_pct)) + max(0.0, float(round_trip_cost_pct))) / 100.0)
    net_reward = reward_per_unit - cost_abs
    net_risk = risk_per_unit + cost_abs
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0
    if net_rr < float(min_net_rr):
        veto_codes.append("INSUFFICIENT_NET_RR")

    return {
        "valid": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "net_rr": round(float(net_rr), 4),
        "gross_rr": rr_calc["gross_rr"],
        "risk_per_unit": risk_per_unit,
        "reward_per_unit": reward_per_unit,
    }


def is_timeframe_warmed_up(tf_info: Any) -> Tuple[bool, str]:
    """
    Check whether a timeframe dictionary in packet['timeframes'][tf] is loaded, valid,
    and has satisfied EMA200 warm-up requirements (PH04-01 / F17 / T15).
    Returns (is_ready, status_reason).
    """
    if not isinstance(tf_info, dict):
        return False, "MISSING_TIMEFRAME"
    price = tf_info.get("price")
    if not is_finite_number(price) or float(price) <= 0:
        return False, "INVALID_PRICE"
    warmup_status = str(tf_info.get("warmup_status", "READY") or "READY").upper().strip()
    if warmup_status in {"EMPTY_DATA", "INVALID_DATA", "INSUFFICIENT_HISTORY"}:
        return False, warmup_status
    if "ema200_ready" in tf_info and tf_info.get("ema200_ready") is False:
        return False, "PARTIAL_WARMUP_NO_EMA200"
    if warmup_status != "READY":
        return False, warmup_status
    return True, "READY"


def preflight_market_check(
    packet: Any,
    max_spread_pct: float = 0.35,
    max_freshness_seconds: Optional[float] = None,
    require_closed_bars: bool = True,
) -> Dict[str, Any]:
    """
    Deterministic preflight & data quality validation before spending AI tokens or generating signals
    (Phase 1.5 / PH01-03 / F38).
    Validates:
    - Required core timeframes (5m, 15m, 1h) present
    - Finite positive prices, ATR, support/resistance, and spread
    - OHLCV candle validation report (missing/malformed candles, duplicate/out-of-order timestamps)
    - Closed-candle confirmation (rejects unclosed/incomplete candles when require_closed_bars=True)
    - Data freshness / stale price detection
    - Indicator input completeness (EMA20, EMA50, RSI, ATR, MACD_HIST, VOL_RATIO)
    - Cross-timeframe price consistency
    """
    veto_codes: List[str] = []
    if not isinstance(packet, dict):
        return {"passed": False, "veto_codes": ["INSUFFICIENT_DATA"], "reason": "Packet pasar kosong/bukan dict."}

    tfs = packet.get("timeframes")
    smcs = packet.get("smc_by_tf")
    if not isinstance(tfs, dict) or not isinstance(smcs, dict):
        veto_codes.append("INSUFFICIENT_DATA")
    else:
        for req_tf in CORE_MARKET_TIMEFRAMES:
            if req_tf not in tfs or req_tf not in smcs:
                if "INSUFFICIENT_DATA" not in veto_codes:
                    veto_codes.append("INSUFFICIENT_DATA")
                break

    price = packet.get("price")
    support = packet.get("support")
    resistance = packet.get("resistance")
    ob = packet.get("order_book") if isinstance(packet.get("order_book"), dict) else {}
    spread = ob.get("spread", 0.0)
    tf15 = tfs.get("15m", {}) if isinstance(tfs, dict) else {}
    atr15 = tf15.get("atr")
    rsi15 = tf15.get("rsi")

    for val in (price, support, resistance, spread, atr15, rsi15):
        if not is_finite_number(val):
            if "INVALID_NUMERIC" not in veto_codes:
                veto_codes.append("INVALID_NUMERIC")

    if "INVALID_NUMERIC" not in veto_codes:
        if float(price) <= 0 or float(atr15) <= 0 or float(support) <= 0 or float(resistance) <= 0 or float(support) > float(resistance):
            veto_codes.append("NON_POSITIVE_PRICE")
        if float(spread) < 0:
            veto_codes.append("INVALID_NUMERIC")
        elif float(spread) > float(max_spread_pct):
            veto_codes.append("SPREAD_TOO_WIDE")

    # Validate OHLCV validation reports if present in packet
    val_map = packet.get("validation") if isinstance(packet.get("validation"), dict) else {}
    for req_tf in CORE_MARKET_TIMEFRAMES:
        v_rep = val_map.get(req_tf)
        if isinstance(v_rep, dict):
            if v_rep.get("valid") is False:
                errs = v_rep.get("errors") or []
                if any("DUPLICATE_TIMESTAMPS" in str(e) or "NON_MONOTONIC" in str(e) for e in errs):
                    if "TIMESTAMP_INTEGRITY_ERROR" not in veto_codes:
                        veto_codes.append("TIMESTAMP_INTEGRITY_ERROR")
                if "MALFORMED_CANDLES" not in veto_codes:
                    veto_codes.append("MALFORMED_CANDLES")

    # Validate closed-candle confirmation & indicator completeness across core timeframes
    if isinstance(tfs, dict):
        core_prices: List[float] = []
        for req_tf in CORE_MARKET_TIMEFRAMES:
            tf_d = tfs.get(req_tf)
            if not isinstance(tf_d, dict):
                continue
            if require_closed_bars and tf_d.get("is_closed") is False:
                if "INCOMPLETE_CANDLE" not in veto_codes:
                    veto_codes.append("INCOMPLETE_CANDLE")
            if str(tf_d.get("quality_status", "OK")).upper() == "INVALID_DATA":
                if "MALFORMED_CANDLES" not in veto_codes:
                    veto_codes.append("MALFORMED_CANDLES")
            for ind_key in ("price", "ema20", "ema50", "rsi", "atr"):
                if ind_key in tf_d and not is_finite_number(tf_d.get(ind_key)):
                    if "MISSING_INDICATOR_INPUTS" not in veto_codes:
                        veto_codes.append("MISSING_INDICATOR_INPUTS")
            if is_finite_number(tf_d.get("price")) and float(tf_d["price"]) > 0:
                core_prices.append(float(tf_d["price"]))

        if len(core_prices) >= 2:
            min_cp = min(core_prices)
            max_cp = max(core_prices)
            if min_cp > 0 and ((max_cp - min_cp) / min_cp) > 0.20:
                if "INCONSISTENT_TIMEFRAME_DATA" not in veto_codes:
                    veto_codes.append("INCONSISTENT_TIMEFRAME_DATA")

    # Validate data freshness if present in packet
    fresh_map = packet.get("freshness") if isinstance(packet.get("freshness"), dict) else {}
    for req_tf in CORE_MARKET_TIMEFRAMES:
        f_rep = fresh_map.get(req_tf)
        if isinstance(f_rep, dict):
            f_status = str(f_rep.get("status", "FRESH")).upper()
            f_age = f_rep.get("freshness_seconds")
            if f_rep.get("is_fresh") is False or f_status in {"STALE", "INVALID", "EMPTY"}:
                if "STALE_MARKET_DATA" not in veto_codes:
                    veto_codes.append("STALE_MARKET_DATA")
            elif max_freshness_seconds is not None and is_finite_number(f_age) and float(f_age) > float(max_freshness_seconds):
                if "STALE_MARKET_DATA" not in veto_codes:
                    veto_codes.append("STALE_MARKET_DATA")

    return {
        "passed": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "reason": "OK" if not veto_codes else f"Preflight veto: {', '.join(veto_codes)}",
    }


def build_momentum_evidence_report(
    packet: Dict[str, Any],
    direction: Optional[str] = None,
    mode: str = "scalping",
) -> Dict[str, Any]:
    """
    Build a deterministic, explainable Momentum Evidence Report (Phase 1.3).
    Separates deterministic technical validation from AI commentary and avoids double-counting
    correlated indicators by aggregating into 5 orthogonal technical pillars:
      1. Trend & Market Structure (EMA20/50/200 + HH/HL or LH/LL bias) — max 25 pts
      2. Momentum & Displacement (RSI regime + MACD hist + impulse body ratio) — max 20 pts
      3. Volume & Order-Flow Confirmation (Volume Ratio + Taker Buy/Sell + Order-Book Imbalance) — max 20 pts
      4. Multi-Timeframe Alignment (Bias TF -> Setup TF -> Trigger TF) — max 20 pts
      5. Technical Level & SMC Zone Confluence (OB/FVG overlap + Liquidity Sweep) — max 15 pts
    Exposes explicit trigger conditions and setup invalidation conditions/levels.
    """
    if not isinstance(packet, dict):
        return {
            "valid": False,
            "direction": "WAIT",
            "momentum_score": 0,
            "score_label": "SCORE_INDICATOR_ONLY",
            "trend_direction": {"bias": "UNKNOWN", "ema_alignment": "UNKNOWN", "score": 0},
            "momentum_strength": {"strength": "NONE", "rsi": None, "macd_hist": None, "score": 0},
            "volume_confirmation": {"confirmed": False, "vol_ratio": 0.0, "buy_pressure": 0.5, "score": 0},
            "volatility_conditions": {"regime": "UNKNOWN", "atr_pct": 0.0, "spread_pct": 0.0, "acceptable": False},
            "mtf_alignment": {"aligned": False, "summary": "INSUFFICIENT_DATA", "score": 0},
            "technical_levels": {},
            "trigger_conditions": {"met": [], "missing": ["DATA_UNAVAILABLE"], "is_triggered": False},
            "invalidation_conditions": {"invalidation_level": None, "conditions": ["Data pasar tidak tersedia"]},
            "pillar_scores": {},
            "summary_bullets": ["Data pasar tidak tersedia atau belum lengkap."],
        }

    tfs = packet.get("timeframes") if isinstance(packet.get("timeframes"), dict) else {}
    smc_by_tf = packet.get("smc_by_tf") if isinstance(packet.get("smc_by_tf"), dict) else {}
    smc15 = packet.get("smc") if isinstance(packet.get("smc"), dict) else smc_by_tf.get("15m", {})
    ob_info = packet.get("order_book") if isinstance(packet.get("order_book"), dict) else {}

    mode_key = str(mode or "scalping").lower().strip()
    if mode_key not in MODE_TIMEFRAME_MATRIX:
        mode_key = "scalping"
    spec = MODE_TIMEFRAME_MATRIX[mode_key]
    bias_tf = spec["bias_tf"] if spec["bias_tf"] in smc_by_tf else "1h"
    setup_tf = spec["setup_tf"] if spec["setup_tf"] in smc_by_tf else "15m"
    trig_tf = spec["trigger_tf"] if spec["trigger_tf"] in smc_by_tf else "5m"

    tf15 = tfs.get("15m") or tfs.get(setup_tf) or {}
    tf_trig = tfs.get(trig_tf) or tf15
    smc_bias = smc_by_tf.get(bias_tf, {}).get("structure", {})
    smc_setup = smc_by_tf.get(setup_tf, {}).get("structure", {})
    smc_trig = smc_by_tf.get(trig_tf, {})

    price = float(packet.get("price") or tf15.get("price") or 0.0)
    support = float(packet.get("support") or 0.0)
    resistance = float(packet.get("resistance") or 0.0)
    atr15 = float(tf15.get("atr") or 0.0)
    rsi15 = float(tf15.get("rsi") or 50.0)
    macd15 = float(tf15.get("macd_hist") or 0.0)
    ema20_15 = float(tf15.get("ema20") or price)
    ema50_15 = float(tf15.get("ema50") or price)
    ema200_15 = float(tf15.get("ema200") or price)
    vr15 = float(tf15.get("vol_ratio") or 0.0)
    bp15 = float(tf15.get("buy_pressure") or 0.5)
    imb = float(ob_info.get("imbalance") or 0.0)
    spread_pct = float(ob_info.get("spread") or 0.0)
    regime = str(packet.get("market_regime") or "SIDEWAYS")

    # Resolve target direction if not explicitly passed
    dir_norm = str(direction or "").upper().strip()
    if dir_norm not in {"LONG", "SHORT"}:
        d_scores = smc15.get("directional_scores", {}) if isinstance(smc15, dict) else {}
        l_s = float(d_scores.get("LONG", 0) or 0)
        s_s = float(d_scores.get("SHORT", 0) or 0)
        if l_s > s_s:
            dir_norm = "LONG"
        elif s_s > l_s:
            dir_norm = "SHORT"
        else:
            dir_norm = "LONG" if price >= ema20_15 else "SHORT"

    is_long = dir_norm == "LONG"
    expected_bias = "BULLISH" if is_long else "BEARISH"

    # Pillar 1: Trend & Structure (max 25, single orthogonal pillar — no double counting EMA + MACD)
    trend_pts = 0
    ema_aligned = (price >= ema20_15 >= ema50_15) if is_long else (price <= ema20_15 <= ema50_15)
    ema200_aligned = (price >= ema200_15) if is_long else (price <= ema200_15)
    struct_bias_15 = str(smc_setup.get("bias") or "RANGE").upper()
    bos_15 = str(smc_setup.get("bos") or "NONE").upper()
    if struct_bias_15 == expected_bias:
        trend_pts += 12
    if ema_aligned:
        trend_pts += 8
    if ema200_aligned and bool(tf15.get("ema200_ready", True)):
        trend_pts += 5
    trend_pts = min(25, trend_pts)

    # Pillar 2: Momentum & Displacement (max 20, capped so RSI + MACD + displacement don't double-count)
    mom_pts = 0
    rsi_healthy = (52.0 <= rsi15 <= 72.0) if is_long else (28.0 <= rsi15 <= 48.0)
    rsi_extreme = (rsi15 > 78.0) if is_long else (rsi15 < 22.0)
    macd_aligned = (macd15 > 0.0) if is_long else (macd15 < 0.0)
    disp_info = smc_trig.get("displacement", {}) if isinstance(smc_trig.get("displacement"), dict) else {}
    has_disp = bool(disp_info.get("bullish") if is_long else disp_info.get("bearish"))
    if rsi_healthy and macd_aligned:
        mom_pts += 12
    elif rsi_healthy or macd_aligned:
        mom_pts += 7
    if has_disp:
        mom_pts += 8
    if rsi_extreme:
        mom_pts = max(0, mom_pts - 6)
    mom_pts = min(20, mom_pts)
    mom_label = "STRONG" if mom_pts >= 15 else ("MODERATE" if mom_pts >= 8 else "WEAK")

    # Pillar 3: Volume & Order-Flow Confirmation (max 20)
    vol_pts = 0
    flow_aligned = (bp15 >= 0.53) if is_long else (bp15 <= 0.47)
    book_aligned = (imb >= 0.05) if is_long else (imb <= -0.05)
    if vr15 >= 1.5:
        vol_pts += 10
    elif vr15 >= 1.1:
        vol_pts += 6
    if flow_aligned:
        vol_pts += 6
    if book_aligned:
        vol_pts += 4
    vol_pts = min(20, vol_pts)
    vol_confirmed = bool(vr15 >= 1.05 and (flow_aligned or book_aligned))

    # Pillar 4: Multi-Timeframe Alignment (max 20)
    mtf_pts = 0
    b_bias = str(smc_bias.get("bias") or "UNKNOWN").upper()
    s_bias = str(smc_setup.get("bias") or "UNKNOWN").upper()
    t_timing = str(smc_trig.get("timing") or "WAIT_RETEST").upper()
    expected_ready = "READY_LONG" if is_long else "READY_SHORT"
    if b_bias == expected_bias:
        mtf_pts += 8
    if s_bias == expected_bias:
        mtf_pts += 6
    if t_timing == expected_ready:
        mtf_pts += 6
    mtf_aligned = bool(b_bias == expected_bias and s_bias == expected_bias)
    mtf_summary = f"{bias_tf.upper()} {b_bias} → {setup_tf.upper()} {s_bias} → {trig_tf.upper()} {t_timing}"

    # Pillar 5: Technical Levels & Zone Confluence (max 15)
    zone_pts = 0
    entry_Side_key = "bullish_entry" if is_long else "bearish_entry"
    zone_ctx = smc15.get(entry_Side_key, {}) if isinstance(smc15.get(entry_Side_key), dict) else {}
    liq_ctx = smc_trig.get("liquidity", {}) if isinstance(smc_trig.get("liquidity"), dict) else {}
    has_sweep = bool(liq_ctx.get("sweep_low") if is_long else liq_ctx.get("sweep_high"))
    if zone_ctx.get("confluence"):
        zone_pts += 8
    elif zone_ctx.get("status") in {"IN_ZONE", "NEAR_ZONE"}:
        zone_pts += 5
    if has_sweep:
        zone_pts += 7
    zone_pts = min(15, zone_pts)

    total_score = int(max(0, min(100, trend_pts + mom_pts + vol_pts + mtf_pts + zone_pts)))

    # Volatility conditions
    atr_pct = round((atr15 / price) * 100.0, 4) if price > 0 else 0.0
    vol_acceptable = bool(0.05 <= atr_pct <= 6.0 and spread_pct <= 0.35)

    # Trigger conditions (met vs missing)
    met_triggers: List[str] = []
    missing_triggers: List[str] = []
    if b_bias == expected_bias:
        met_triggers.append(f"Bias {bias_tf.upper()} {expected_bias}")
    else:
        missing_triggers.append(f"Bias {bias_tf.upper()} belum {expected_bias} (saat ini {b_bias})")

    if s_bias == expected_bias or bos_15 == f"{expected_bias} BOS":
        met_triggers.append(f"Struktur {setup_tf.upper()} {s_bias} ({bos_15})")
    else:
        missing_triggers.append(f"Struktur {setup_tf.upper()} belum konfirmasi {expected_bias}")

    if t_timing == expected_ready or bool(smc_trig.get("bullish_trigger" if is_long else "bearish_trigger")):
        met_triggers.append(f"Trigger {trig_tf.upper()} aktif ({t_timing})")
    else:
        missing_triggers.append(f"Trigger {trig_tf.upper()} masih {t_timing}")

    if vol_confirmed:
        met_triggers.append(f"Konfirmasi volume ({vr15:.2f}x) & order-flow ({bp15:.0%} taker buy)")
    else:
        missing_triggers.append(f"Volume/order-flow belum ekspansif (vol={vr15:.2f}x)")

    # Setup invalidation level & conditions
    swing_low = float(smc_setup.get("last_swing_low") or support or 0.0)
    swing_high = float(smc_setup.get("last_swing_high") or resistance or 0.0)
    z_low = zone_ctx.get("zone_low")
    z_high = zone_ctx.get("zone_high")
    if is_long:
        inv_candidates = [x for x in (z_low, swing_low, support) if is_finite_number(x) and 0 < float(x) < price]
        invalidation_level = round(min(inv_candidates) if inv_candidates else max(price * 0.05, price - 1.15 * atr15), 8)
        inv_conditions = [
            f"Harga penutupan candle 15m menembus di bawah level invalidasi ${invalidation_level:,.8f}",
            f"Struktur {setup_tf.upper()} berbalik menjadi BEARISH BOS / CHOCH",
            "Zona Order Block / FVG bullish termitigasi penuh (state=INVALIDATED)",
        ]
    else:
        inv_candidates = [x for x in (z_high, swing_high, resistance) if is_finite_number(x) and float(x) > price]
        invalidation_level = round(max(inv_candidates) if inv_candidates else (price + 1.15 * atr15), 8)
        inv_conditions = [
            f"Harga penutupan candle 15m menembus di atas level invalidasi ${invalidation_level:,.8f}",
            f"Struktur {setup_tf.upper()} berbalik menjadi BULLISH BOS / CHOCH",
            "Zona Order Block / FVG bearish termitigasi penuh (state=INVALIDATED)",
        ]

    is_triggered = bool(total_score >= 58 and t_timing == expected_ready and len(met_triggers) >= 3)

    return {
        "valid": True,
        "direction": dir_norm,
        "mode": mode_key,
        "momentum_score": total_score,
        "score_label": "SCORE_INDICATOR_ONLY",
        "trend_direction": {
            "direction": dir_norm,
            "bias_tf_structure": b_bias,
            "setup_tf_structure": s_bias,
            "bos": bos_15,
            "ema_alignment": "BULLISH_STACK" if (price >= ema20_15 >= ema50_15) else ("BEARISH_STACK" if (price <= ema20_15 <= ema50_15) else "MIXED"),
            "above_ema200": bool(price >= ema200_15),
            "score": trend_pts,
        },
        "momentum_strength": {
            "strength": mom_label,
            "rsi_15m": round(rsi15, 2),
            "macd_hist_15m": round(macd15, 8),
            "displacement_active": has_disp,
            "correlated_oscillators_deduplicated": True,
            "score": mom_pts,
        },
        "volume_confirmation": {
            "confirmed": vol_confirmed,
            "vol_ratio_15m": round(vr15, 2),
            "taker_buy_pressure_15m": round(bp15, 4),
            "order_book_imbalance": round(imb, 4),
            "score": vol_pts,
        },
        "volatility_conditions": {
            "regime": regime,
            "atr_15m": round(atr15, 8),
            "atr_pct": atr_pct,
            "spread_pct": round(spread_pct, 4),
            "acceptable": vol_acceptable,
        },
        "mtf_alignment": {
            "aligned": mtf_aligned,
            "bias_tf": bias_tf,
            "setup_tf": setup_tf,
            "trigger_tf": trig_tf,
            "bias_state": b_bias,
            "setup_state": s_bias,
            "trigger_state": t_timing,
            "summary": mtf_summary,
            "score": mtf_pts,
        },
        "technical_levels": {
            "current_price": round(price, 8),
            "support_30b": round(support, 8),
            "resistance_30b": round(resistance, 8),
            "last_swing_high": round(swing_high, 8),
            "last_swing_low": round(swing_low, 8),
            "entry_zone_low": round(float(z_low), 8) if is_finite_number(z_low) else None,
            "entry_zone_high": round(float(z_high), 8) if is_finite_number(z_high) else None,
            "selected_zone_id": zone_ctx.get("selected_zone_id"),
            "ob_fvg_confluence": bool(zone_ctx.get("confluence", False)),
            "invalidation_level": invalidation_level,
        },
        "trigger_conditions": {
            "is_triggered": is_triggered,
            "met": met_triggers,
            "missing": missing_triggers,
        },
        "invalidation_conditions": {
            "invalidation_level": invalidation_level,
            "conditions": inv_conditions,
        },
        "pillar_scores": {
            "trend_structure": trend_pts,
            "momentum_displacement": mom_pts,
            "volume_orderflow": vol_pts,
            "mtf_alignment": mtf_pts,
            "zone_liquidity_confluence": zone_pts,
        },
        "summary_bullets": met_triggers + ([f"Invalidasi: ${invalidation_level:,.8f}"] if invalidation_level else []),
    }


def evaluate_signal_lifecycle(
    symbol: str,
    pattern_or_result: Dict[str, Any],
    *,
    previous_state: Optional[str] = None,
    previous_direction: Optional[str] = None,
    previous_triggered_at: Optional[str] = None,
    previous_expires_at: Optional[str] = None,
    current_price: Optional[float] = None,
    invalidation_level: Optional[float] = None,
    bar_time: Optional[str] = None,
    mode: str = "scalping",
    as_of_iso: Optional[str] = None,
    expiry_bars: int = 6,
) -> SignalLifecycleRecord:
    """
    Evaluate deterministic Signal Lifecycle state transitions (Phase 1.2):
    States: DEVELOPING, TRIGGERED, WAIT, INVALIDATED.
    - Enforces valid state transitions (`validate_signal_lifecycle_transition`).
    - Prevents a signal from remaining TRIGGERED indefinitely after its underlying conditions or TTL expire.
    - Immediately transitions to INVALIDATED if `current_price` breaches `invalidation_level` or if a
      previously TRIGGERED/DEVELOPING setup's directional structure flips against the setup.
    """
    now_iso = str(as_of_iso or datetime.now(timezone.utc).isoformat())
    sym_u = str(symbol or pattern_or_result.get("symbol") or "UNKNOWN").upper().strip()
    mode_norm = str(mode or pattern_or_result.get("mode") or "scalping").lower().strip()
    spec = MODE_TIMEFRAME_MATRIX.get(mode_norm, MODE_TIMEFRAME_MATRIX["scalping"])
    trig_tf = spec.get("trigger_tf", "5m")
    tf_sec = TIMEFRAME_SECONDS_MAP.get(trig_tf, 300)
    eff_expiry_sec = max(1, int(expiry_bars or spec.get("signal_expiry_bars", 6))) * tf_sec

    raw_state = (
        pattern_or_result.get("lifecycle_state")
        or pattern_or_result.get("state")
        or pattern_or_result.get("pattern", {}).get("state")
        or "WAIT"
    )
    cand_state = normalize_signal_lifecycle_state(raw_state)
    direction = str(
        pattern_or_result.get("direction")
        or pattern_or_result.get("final")
        or pattern_or_result.get("pattern", {}).get("direction")
        or "WAIT"
    ).upper().strip()
    if direction not in {"LONG", "SHORT"}:
        direction = "WAIT"

    score_val = float(
        pattern_or_result.get("score")
        or pattern_or_result.get("scanner_score")
        or pattern_or_result.get("pattern", {}).get("score")
        or 0.0
    )
    b_time = str(bar_time or pattern_or_result.get("bar_time") or now_iso[:19])

    inv_lvl = invalidation_level
    if inv_lvl is None:
        inv_lvl = (
            pattern_or_result.get("invalidation_level")
            or pattern_or_result.get("sl")
            or pattern_or_result.get("stop_loss")
        )
    inv_f = float(inv_lvl) if (is_finite_number(inv_lvl) and float(inv_lvl) > 0) else None
    price_f = float(current_price) if (is_finite_number(current_price) and float(current_price) > 0) else None
    if price_f is None and is_finite_number(pattern_or_result.get("price")):
        price_f = float(pattern_or_result["price"])

    prev_norm = normalize_signal_lifecycle_state(previous_state) if previous_state else None
    invalidation_reason = ""
    is_expired = False

    # 1. Check expiry of previously TRIGGERED or DEVELOPING signal
    if prev_norm in {SignalLifecycleState.TRIGGERED.value, SignalLifecycleState.DEVELOPING.value} and previous_expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(previous_expires_at).replace("Z", "+00:00"))
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            if now_dt > exp_dt:
                is_expired = True
                cand_state = SignalLifecycleState.INVALIDATED.value
                invalidation_reason = f"SIGNAL_EXPIRED: Melewati batas waktu validitas ({previous_expires_at})"
        except Exception:
            pass

    # 2. Check price breach of invalidation level
    eff_dir = previous_direction if (previous_direction in {"LONG", "SHORT"} and prev_norm in {"TRIGGERED", "DEVELOPING"}) else direction
    if not is_expired and inv_f is not None and price_f is not None and eff_dir in {"LONG", "SHORT"}:
        if eff_dir == "LONG" and price_f <= inv_f:
            cand_state = SignalLifecycleState.INVALIDATED.value
            invalidation_reason = f"PRICE_BREACHED_INVALIDATION: Harga {price_f:.8f} <= invalidasi LONG {inv_f:.8f}"
        elif eff_dir == "SHORT" and price_f >= inv_f:
            cand_state = SignalLifecycleState.INVALIDATED.value
            invalidation_reason = f"PRICE_BREACHED_INVALIDATION: Harga {price_f:.8f} >= invalidasi SHORT {inv_f:.8f}"

    # 3. Check directional flip or loss of trigger conditions when previously TRIGGERED
    if not invalidation_reason and prev_norm == SignalLifecycleState.TRIGGERED.value:
        if previous_direction in {"LONG", "SHORT"} and direction in {"LONG", "SHORT"} and direction != previous_direction:
            cand_state = SignalLifecycleState.INVALIDATED.value
            invalidation_reason = f"DIRECTION_FLIPPED: Setup {previous_direction} berbalik menjadi {direction}"
        elif cand_state == SignalLifecycleState.DEVELOPING.value:
            # A TRIGGERED signal cannot regress to DEVELOPING; if trigger conditions weaken, downgrade to WAIT or INVALIDATED
            cand_state = SignalLifecycleState.WAIT.value if score_val >= 40.0 else SignalLifecycleState.INVALIDATED.value
            if cand_state == SignalLifecycleState.INVALIDATED.value:
                invalidation_reason = f"TRIGGER_CONDITIONS_LOST: Skor turun ke {score_val:.0f}/100"

    # 4. Enforce transition rules if previous_state is terminal INVALIDATED for the same signal
    if prev_norm == SignalLifecycleState.INVALIDATED.value:
        cand_state = SignalLifecycleState.INVALIDATED.value
        if not invalidation_reason:
            invalidation_reason = "ALREADY_INVALIDATED"

    transition_ok = True
    if prev_norm is not None:
        try:
            validate_signal_lifecycle_transition(prev_norm, cand_state)
        except ValueError:
            transition_ok = False
            cand_state = SignalLifecycleState.WAIT.value

    triggered_at_out = previous_triggered_at
    expires_at_out = previous_expires_at
    invalidated_at_out = None

    if cand_state == SignalLifecycleState.TRIGGERED.value:
        if not triggered_at_out:
            triggered_at_out = now_iso
        if not expires_at_out:
            try:
                now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                exp_epoch = now_dt.timestamp() + eff_expiry_sec
                expires_at_out = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).isoformat()
            except Exception:
                expires_at_out = now_iso
    elif cand_state == SignalLifecycleState.DEVELOPING.value:
        if not expires_at_out:
            try:
                now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                exp_epoch = now_dt.timestamp() + (eff_expiry_sec * 2)
                expires_at_out = datetime.fromtimestamp(exp_epoch, tz=timezone.utc).isoformat()
            except Exception:
                expires_at_out = now_iso
    elif cand_state == SignalLifecycleState.INVALIDATED.value:
        invalidated_at_out = now_iso

    raw_sig_key = f"{sym_u}|{mode_norm}|{eff_dir if eff_dir in {'LONG', 'SHORT'} else direction}|{b_time}"
    sig_hash = hashlib.sha256(raw_sig_key.encode("utf-8")).hexdigest()[:10]
    signal_id = f"sig_{sym_u}_{mode_norm}_{sig_hash}"

    is_actionable = bool(
        cand_state == SignalLifecycleState.TRIGGERED.value
        and direction in {"LONG", "SHORT"}
        and not is_expired
    )

    return SignalLifecycleRecord(
        signal_id=signal_id,
        symbol=sym_u,
        mode=mode_norm,
        state=cand_state,
        previous_state=prev_norm,
        direction=direction if cand_state != SignalLifecycleState.WAIT.value else "WAIT",
        score=round(score_val, 2),
        created_at=now_iso,
        updated_at=now_iso,
        bar_time=b_time,
        triggered_at=triggered_at_out,
        expires_at=expires_at_out,
        invalidated_at=invalidated_at_out,
        invalidation_level=inv_f,
        invalidation_reason=invalidation_reason,
        transition_valid=transition_ok,
        is_expired=is_expired,
        is_actionable=is_actionable,
    )


def _infer_proactive_mode_direction(
    packet: Dict[str, Any],
    spec: Dict[str, Any],
    raw_m_dec: str,
    gated_final: str,
) -> str:
    """Infer LONG or SHORT for a trading mode using AI proposal + multi-timeframe indicators & SMC."""
    if raw_m_dec in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return raw_m_dec
    if gated_final in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return gated_final

    tfs = packet.get("timeframes", {}) if isinstance(packet, dict) else {}
    smc_by_tf = packet.get("smc_by_tf", {}) if isinstance(packet, dict) else {}
    long_pts = 0.0
    short_pts = 0.0

    for tf_name in spec.get("required_timeframes", ("5m", "15m", "1h")):
        tf_d = tfs.get(tf_name, {}) if isinstance(tfs.get(tf_name), dict) else {}
        smc_d = smc_by_tf.get(tf_name, {}) if isinstance(smc_by_tf.get(tf_name), dict) else {}
        st_bias = smc_d.get("structure", {}).get("bias", "RANGE")
        if st_bias == "BULLISH":
            long_pts += 15.0
        elif st_bias == "BEARISH":
            short_pts += 15.0

        dir_sc = smc_d.get("directional_scores", {}) if isinstance(smc_d.get("directional_scores"), dict) else {}
        long_pts += float(dir_sc.get("LONG", 0) or 0) * 0.20
        short_pts += float(dir_sc.get("SHORT", 0) or 0) * 0.20

        p_v = tf_d.get("price")
        e20 = tf_d.get("ema20")
        e50 = tf_d.get("ema50")
        rsi_v = tf_d.get("rsi")
        bp_v = tf_d.get("buy_pressure")
        if is_finite_number(p_v) and is_finite_number(e20):
            if float(p_v) >= float(e20):
                long_pts += 8.0
            else:
                short_pts += 8.0
        if is_finite_number(e20) and is_finite_number(e50):
            if float(e20) >= float(e50):
                long_pts += 8.0
            else:
                short_pts += 8.0
        if is_finite_number(rsi_v):
            if float(rsi_v) >= 50.0:
                long_pts += 5.0
            else:
                short_pts += 5.0
        if is_finite_number(bp_v):
            if float(bp_v) >= 0.50:
                long_pts += 5.0
            else:
                short_pts += 5.0

    return DecisionAction.LONG.value if long_pts >= short_pts else DecisionAction.SHORT.value


def _compute_mode_trade_levels(
    packet: Dict[str, Any],
    spec: Dict[str, Any],
    direction: str,
) -> Dict[str, Any]:
    """Compute horizon-appropriate Entry, Stop Loss, Take Profit, and R:R for a specific trading mode."""
    if direction not in {DecisionAction.LONG.value, DecisionAction.SHORT.value} or not isinstance(packet, dict):
        return {"entry": None, "sl": None, "tp": None, "rr": 0.0}

    tfs = packet.get("timeframes", {}) if isinstance(packet.get("timeframes"), dict) else {}
    trig_tf = spec.get("trigger_tf", "15m")
    setup_tf = spec.get("setup_tf", "15m")
    tf_info = tfs.get(setup_tf) or tfs.get(trig_tf) or tfs.get("15m") or {}
    price = packet.get("price") or tf_info.get("price")
    atr_v = tf_info.get("atr") or tfs.get("15m", {}).get("atr")
    if not is_finite_number(price) or float(price) <= 0:
        return {"entry": None, "sl": None, "tp": None, "rr": 0.0}

    p_f = float(price)
    atr_f = float(atr_v) if (is_finite_number(atr_v) and float(atr_v) > 0) else p_f * 0.008
    mode_mult = 0.85 if spec.get("trigger_tf") == "5m" else (1.15 if spec.get("trigger_tf") == "15m" else 1.50)
    risk_dist = max(atr_f * mode_mult, p_f * 0.0045)
    reward_dist = risk_dist * 2.1

    if direction == DecisionAction.LONG.value:
        sl_p = max(p_f * 0.05, p_f - risk_dist)
        tp_p = p_f + reward_dist
    else:
        sl_p = p_f + risk_dist
        tp_p = max(p_f * 0.05, p_f - reward_dist)

    return {
        "entry": round(p_f, 8),
        "sl": round(sl_p, 8),
        "tp": round(tp_p, 8),
        "rr": 2.1,
    }


def evaluate_mode_decisions(
    packet: Dict[str, Any],
    raw_modes: Dict[str, Any],
    gated_final: str,
    global_veto_codes: Optional[List[str]] = None,
    base_confidence: float = 0.0,
    proactive_mode: Optional[bool] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Evaluate per-mode decisions across scalping (1h->15m->5m), intraday (4h->1h->15m),
    and swing (1d->4h->1h) separating directional_bias from entry_action and gating to WAIT
    (INSUFFICIENT_DATA) whenever a required timeframe is missing or has not met EMA200 warm-up
    (F16, F17, T05, T15).
    """
    smc_by_tf = packet.get("smc_by_tf", {}) if isinstance(packet, dict) else {}
    tfs = packet.get("timeframes", {}) if isinstance(packet, dict) else {}
    raw_modes = raw_modes if isinstance(raw_modes, dict) else {}
    global_vetoes = list(global_veto_codes or [])
    base_conf = float(base_confidence) if is_valid_confidence(base_confidence) else 0.0
    is_proactive = bool(
        proactive_mode
        if proactive_mode is not None
        else (isinstance(packet, dict) and bool(packet.get("live_market_packet", False)))
    )

    out: Dict[str, Dict[str, Any]] = {}
    for mode_name in _ALLOWED_MODES:
        spec = MODE_TIMEFRAME_MATRIX[mode_name]
        req_tfs = spec["required_timeframes"]
        missing_tfs: List[str] = []
        unwarmed_tfs: List[str] = []

        for tf in req_tfs:
            if tf not in smc_by_tf or tf not in tfs or not isinstance(smc_by_tf.get(tf), dict) or not isinstance(tfs.get(tf), dict):
                missing_tfs.append(tf)
                continue
            tf_ready, tf_reason = is_timeframe_warmed_up(tfs[tf])
            if not tf_ready:
                if tf_reason in {"MISSING_TIMEFRAME", "INVALID_PRICE", "EMPTY_DATA", "INVALID_DATA", "INSUFFICIENT_HISTORY"}:
                    missing_tfs.append(tf)
                else:
                    unwarmed_tfs.append(tf)

        has_insufficient_data = bool(missing_tfs or unwarmed_tfs)
        data_status = DataStatus.COMPLETE.value if not has_insufficient_data else DataStatus.INSUFFICIENT_DATA.value

        bias_tf = spec["bias_tf"] if spec["bias_tf"] in smc_by_tf else ("1h" if "1h" in smc_by_tf else "15m")
        setup_tf = spec["setup_tf"] if spec["setup_tf"] in smc_by_tf else ("15m" if "15m" in smc_by_tf else bias_tf)
        trigger_tf = spec["trigger_tf"] if spec["trigger_tf"] in smc_by_tf else ("5m" if "5m" in smc_by_tf else setup_tf)

        bias_structure = smc_by_tf.get(bias_tf, {}).get("structure", {}).get("bias", DirectionalBias.UNKNOWN.value)
        setup_structure = smc_by_tf.get(setup_tf, {}).get("structure", {}).get("bias", DirectionalBias.UNKNOWN.value)
        trigger_timing = smc_by_tf.get(trigger_tf, {}).get("timing", TimingStatus.WAIT_RETEST.value)

        raw_m = raw_modes.get(mode_name) if isinstance(raw_modes.get(mode_name), dict) else {}
        raw_m_dec = str(raw_m.get("decision", "")).upper().strip()
        if raw_m_dec not in _ALLOWED_DECISIONS:
            if trigger_timing == TimingStatus.READY_LONG.value and bias_structure == DirectionalBias.BULLISH.value and setup_structure == DirectionalBias.BULLISH.value:
                raw_m_dec = DecisionAction.LONG.value
            elif trigger_timing == TimingStatus.READY_SHORT.value and bias_structure == DirectionalBias.BEARISH.value and setup_structure == DirectionalBias.BEARISH.value:
                raw_m_dec = DecisionAction.SHORT.value
            else:
                raw_m_dec = DecisionAction.WAIT.value

        raw_m_conf = raw_m.get("confidence", base_conf)
        m_conf = float(raw_m_conf) if is_valid_confidence(raw_m_conf) else base_conf

        if bias_structure in {DirectionalBias.BULLISH.value, DirectionalBias.BEARISH.value}:
            directional_bias = bias_structure
        elif raw_m_dec == DecisionAction.LONG.value:
            directional_bias = DirectionalBias.BULLISH.value
        elif raw_m_dec == DecisionAction.SHORT.value:
            directional_bias = DirectionalBias.BEARISH.value
        elif bias_structure == "RANGE":
            directional_bias = DirectionalBias.NEUTRAL.value
        else:
            directional_bias = DirectionalBias.UNKNOWN.value

        mode_vetoes: List[str] = []
        if has_insufficient_data:
            mode_vetoes.append(DataStatus.INSUFFICIENT_DATA.value)
            if unwarmed_tfs:
                mode_vetoes.append("WARMUP_INCOMPLETE")

        for gv in global_vetoes:
            if gv not in mode_vetoes:
                mode_vetoes.append(gv)

        if is_proactive and not has_insufficient_data and not mode_vetoes:
            desired_action = _infer_proactive_mode_direction(packet, spec, raw_m_dec, gated_final)
            if directional_bias not in {DirectionalBias.BULLISH.value, DirectionalBias.BEARISH.value}:
                directional_bias = DirectionalBias.BULLISH.value if desired_action == DecisionAction.LONG.value else DirectionalBias.BEARISH.value
            entry_action = desired_action
            risk_approved = True
            effective_conf = max(m_conf, base_conf, 62.0)
        else:
            if trigger_timing == TimingStatus.WAIT_RETEST.value and "WAIT_RETEST" not in mode_vetoes:
                mode_vetoes.append("WAIT_RETEST")
            elif trigger_timing == TimingStatus.LATE.value and "LATE_ENTRY" not in mode_vetoes:
                mode_vetoes.append("LATE_ENTRY")

            desired_action = raw_m_dec if raw_m_dec in {DecisionAction.LONG.value, DecisionAction.SHORT.value} else (
                DecisionAction.LONG.value if directional_bias == DirectionalBias.BULLISH.value and setup_structure == DirectionalBias.BULLISH.value and trigger_timing == TimingStatus.READY_LONG.value
                else DecisionAction.SHORT.value if directional_bias == DirectionalBias.BEARISH.value and setup_structure == DirectionalBias.BEARISH.value and trigger_timing == TimingStatus.READY_SHORT.value
                else DecisionAction.WAIT.value
            )

            if desired_action == DecisionAction.LONG.value:
                if bias_structure != DirectionalBias.BULLISH.value or setup_structure != DirectionalBias.BULLISH.value:
                    if "TIMEFRAME_CONFLICT" not in mode_vetoes:
                        mode_vetoes.append("TIMEFRAME_CONFLICT")
                if trigger_timing != TimingStatus.READY_LONG.value:
                    if "NO_TRIGGER" not in mode_vetoes and "WAIT_RETEST" not in mode_vetoes and "LATE_ENTRY" not in mode_vetoes:
                        mode_vetoes.append("NO_TRIGGER")
            elif desired_action == DecisionAction.SHORT.value:
                if bias_structure != DirectionalBias.BEARISH.value or setup_structure != DirectionalBias.BEARISH.value:
                    if "TIMEFRAME_CONFLICT" not in mode_vetoes:
                        mode_vetoes.append("TIMEFRAME_CONFLICT")
                if trigger_timing != TimingStatus.READY_SHORT.value:
                    if "NO_TRIGGER" not in mode_vetoes and "WAIT_RETEST" not in mode_vetoes and "LATE_ENTRY" not in mode_vetoes:
                        mode_vetoes.append("NO_TRIGGER")

            if gated_final == DecisionAction.WAIT.value and desired_action in {DecisionAction.LONG.value, DecisionAction.SHORT.value} and not mode_vetoes and not raw_modes:
                mode_vetoes.append("GLOBAL_WAIT_GATE")
            elif gated_final in {DecisionAction.LONG.value, DecisionAction.SHORT.value} and desired_action in {DecisionAction.LONG.value, DecisionAction.SHORT.value} and desired_action != gated_final:
                if "TIMEFRAME_CONFLICT" not in mode_vetoes:
                    mode_vetoes.append("TIMEFRAME_CONFLICT")

            if desired_action in {DecisionAction.LONG.value, DecisionAction.SHORT.value} and not mode_vetoes:
                entry_action = desired_action
                risk_approved = True
                effective_conf = m_conf
            else:
                entry_action = DecisionAction.WAIT.value
                risk_approved = False
                effective_conf = m_conf

        levels = _compute_mode_trade_levels(packet, spec, entry_action)
        raw_reason = str(raw_m.get("reason", "") or "").strip()
        veto_str = f"Veto: {', '.join(mode_vetoes)}" if mode_vetoes else "Approved"
        missing_str = f" (TF belum tersedia: {', '.join(missing_tfs)})" if missing_tfs else ""
        unwarmed_str = f" (TF belum warm-up EMA200: {', '.join(unwarmed_tfs)})" if unwarmed_tfs else ""
        reason_parts = [
            f"[{spec['bias_tf']}→{spec['setup_tf']}→{spec['trigger_tf']}] Bias {directional_bias}; Setup {setup_structure}; Timing {trigger_timing}{missing_str}{unwarmed_str}.",
            f"Action={entry_action} ({veto_str}).",
        ]
        if raw_reason:
            reason_parts.append(raw_reason)

        out[mode_name] = {
            "mode": mode_name,
            "bias_tf": spec["bias_tf"],
            "setup_tf": spec["setup_tf"],
            "trigger_tf": spec["trigger_tf"],
            "required_timeframes": list(req_tfs),
            "missing_timeframes": missing_tfs,
            "unwarmed_timeframes": unwarmed_tfs,
            "data_status": data_status,
            "directional_bias": directional_bias,
            "bias_structure": bias_structure if not has_insufficient_data else DirectionalBias.UNKNOWN.value,
            "setup_structure": setup_structure if not has_insufficient_data else DirectionalBias.UNKNOWN.value,
            "trigger_timing": trigger_timing if not has_insufficient_data else DataStatus.INSUFFICIENT_DATA.value,
            "setup_state": trigger_timing if not has_insufficient_data else DataStatus.INSUFFICIENT_DATA.value,
            "raw_model_decision": raw_m_dec,
            "entry_action": entry_action,
            "decision": entry_action,
            "risk_approved": risk_approved,
            "veto_codes": mode_vetoes,
            "confidence": round(effective_conf, 2),
            "entry": levels["entry"],
            "sl": levels["sl"],
            "tp": levels["tp"],
            "rr": levels["rr"],
            "reason": " ".join(reason_parts)[:360],
        }
    return out


def compute_signal_idempotency_key(result: Dict[str, Any], mode: str = "scalping") -> Optional[str]:
    """Compute a durable setup idempotency key so manual UI and monitor cannot save duplicate signals (F24)."""
    if not isinstance(result, dict):
        return None
    if result.get("idempotency_key"):
        return str(result["idempotency_key"])
    symbol = str(result.get("symbol", "UNKNOWN")).upper().strip()
    final_dec = str(result.get("final", DecisionAction.WAIT.value)).upper().strip()
    mode_str = str(result.get("mode") or mode or "scalping").lower().strip()
    bar_ts = str(result.get("bar_time") or result.get("analyzed_at", "")[:16] or "no_bar_time")
    entry_status = str(
        result.get("entry_status")
        or result.get("packet", {}).get("smc", {}).get("timing", "NONE")
    ).upper().strip()
    z_low = result.get("entry_zone_low")
    z_high = result.get("entry_zone_high")
    z_low_s = f"{float(z_low):.8f}" if is_finite_number(z_low) else "none"
    z_high_s = f"{float(z_high):.8f}" if is_finite_number(z_high) else "none"
    return f"sig|{symbol}|{mode_str}|{final_dec}|{bar_ts}|{entry_status}|{z_low_s}|{z_high_s}"


def compact_json(obj: Any, max_chars: int = 1400) -> str:
    """Serialize a compact summary as guaranteed-valid JSON within max_chars by trimming fields prior to json.dumps (F19)."""
    max_chars = max(72, int(max_chars))
    if not isinstance(obj, dict):
        obj = {"decision": DecisionAction.WAIT.value, "confidence": 0, "reasoning": str(obj or ""), "key_risk": "", "evidence": []}

    raw_decision = obj.get("decision", DecisionAction.WAIT.value)
    decision = str(raw_decision).upper().strip() if isinstance(raw_decision, str) else DecisionAction.WAIT.value
    if decision not in _ALLOWED_DECISIONS:
        decision = DecisionAction.WAIT.value

    confidence = sanitize_confidence(obj.get("confidence", 0))
    reasoning = str(obj.get("reasoning", "") or "")
    key_risk = str(obj.get("key_risk", "") or "")
    raw_ev = obj.get("evidence", [])
    if not isinstance(raw_ev, list):
        raw_ev = [str(raw_ev)] if raw_ev else []
    evidence = [str(x) for x in raw_ev[:3] if x is not None]

    def _dump(r_text: str, k_text: str, ev_list: List[str]) -> str:
        payload = {
            "decision": decision,
            "confidence": confidence,
            "reasoning": r_text,
            "key_risk": k_text,
            "evidence": ev_list,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    full = _dump(reasoning, key_risk, evidence)
    if len(full) <= max_chars:
        return full

    if len(_dump("", "", [])) > max_chars and isinstance(confidence, float):
        confidence = int(round(confidence))

    trimmed_ev = list(evidence)
    while trimmed_ev and len(_dump("", "", trimmed_ev)) > max(max_chars - 20, 72):
        if len(trimmed_ev[-1]) > 16:
            trimmed_ev[-1] = trimmed_ev[-1][: max(1, len(trimmed_ev[-1]) // 2)]
        else:
            trimmed_ev.pop()

    if len(_dump(reasoning, key_risk, trimmed_ev)) <= max_chars:
        return _dump(reasoning, key_risk, trimmed_ev)

    while trimmed_ev and len(_dump(reasoning[:12], key_risk[:12], trimmed_ev)) > max_chars:
        trimmed_ev.pop()

    lo = 0
    hi = max(len(reasoning), len(key_risk), 1)
    best_json = _dump("", "", [])
    while lo <= hi:
        mid = (lo + hi) // 2
        r_slice = reasoning[:mid]
        k_slice = key_risk[: max(0, mid // 2)]
        candidate_str = _dump(r_slice, k_slice, trimmed_ev)
        if len(candidate_str) <= max_chars:
            best_json = candidate_str
            lo = mid + 1
        else:
            hi = mid - 1

    json.loads(best_json)
    return best_json


def format_signal_history_status(row: Any) -> str:
    """Format signal history status so WAIT observations are never displayed as OPEN positions (F33)."""
    keys = row.keys() if hasattr(row, "keys") else ()
    p_state = str(row["position_state"] if ("position_state" in keys and row["position_state"]) else "").upper().strip()
    res = row["result"] if "result" in keys else None
    if p_state in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP", "CLOSED_INVALIDATED", "EXPIRED_UNFILLED", "CANCELLED"}:
        return f"{res} ({p_state})" if res and res != p_state else p_state
    if res:
        return str(res)
    if p_state == "PENDING_ENTRY":
        return "PENDING_ENTRY"
    dec = str(row["final_decision"] if "final_decision" in keys else "").upper().strip()
    if dec in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return "OPEN (LEGACY PAPER)"
    return "OBSERVED (WAIT)"

"""
Core domain contracts, enums, pure numeric validators, and gate evaluation logic.
Strictly decoupled from Streamlit UI, database migrations, and HTTP networks (PH02-01 / PH04-01 / F16, F17, F34, F38).
"""

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


class DecisionAction(str, Enum):
    """Gated decision action for proposals and signals."""
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


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

# Mode timeframe requirement matrix (PH01-03 / PH02-01 / PH04-01 / F17 / T15)
MODE_TIMEFRAME_MATRIX: Dict[str, Dict[str, Any]] = {
    "scalping": {
        "bias_tf": "1h",
        "setup_tf": "15m",
        "trigger_tf": "5m",
        "required_timeframes": ("5m", "15m", "1h"),
        "min_warmup_bars": 200,
    },
    "intraday": {
        "bias_tf": "4h",
        "setup_tf": "1h",
        "trigger_tf": "15m",
        "required_timeframes": ("15m", "1h", "4h"),
        "min_warmup_bars": 200,
    },
    "swing": {
        "bias_tf": "1d",
        "setup_tf": "4h",
        "trigger_tf": "1h",
        "required_timeframes": ("1h", "4h", "1d"),
        "min_warmup_bars": 200,
    },
}

_ALLOWED_DECISIONS = {DecisionAction.LONG.value, DecisionAction.SHORT.value, DecisionAction.WAIT.value}
_ALLOWED_MODES = (TradingMode.SCALPING.value, TradingMode.INTRADAY.value, TradingMode.SWING.value)
_REQUIRED_AI_FIELDS = ("decision", "confidence", "reasoning", "key_risk", "evidence")
_ALLOWED_AI_FIELDS = {"decision", "confidence", "reasoning", "key_risk", "evidence", "modes"}
_REQUIRED_MODE_FIELDS = {"decision", "confidence", "reason"}


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
    """Validated trade levels and metrics."""
    direction: str
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    gross_rr: float
    net_rr: float
    valid: bool
    veto_codes: List[str] = field(default_factory=list)


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


# ---------------------------------------------------------
# Pure Domain Logic
# ---------------------------------------------------------
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
    """Deterministic post-AI validation of entry/SL/TP ordering, positive finite numbers, and net R:R (F38)."""
    veto_codes: List[str] = []
    if direction not in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return {"valid": False, "veto_codes": [], "net_rr": 0.0}

    for v in (entry, sl, tp, gross_rr, spread_pct):
        if not is_finite_number(v):
            veto_codes.append("INVALID_NUMERIC")
            return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0}

    entry_f, sl_f, tp_f = float(entry), float(sl), float(tp)
    if entry_f <= 0 or sl_f <= 0 or tp_f <= 0:
        veto_codes.append("NON_POSITIVE_PRICE")
        veto_codes.append("INVALID_SL_TP")
        return {"valid": False, "veto_codes": veto_codes, "net_rr": 0.0}

    if direction == DecisionAction.LONG.value and not (sl_f < entry_f < tp_f):
        veto_codes.append("INVALID_SL_TP")
    elif direction == DecisionAction.SHORT.value and not (tp_f < entry_f < sl_f):
        veto_codes.append("INVALID_SL_TP")

    cost_abs = entry_f * ((max(0.0, float(spread_pct)) + max(0.0, float(round_trip_cost_pct))) / 100.0)
    net_reward = abs(tp_f - entry_f) - cost_abs
    net_risk = abs(entry_f - sl_f) + cost_abs
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0
    if net_rr < float(min_net_rr):
        veto_codes.append("INSUFFICIENT_NET_RR")

    return {
        "valid": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "net_rr": round(float(net_rr), 4),
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
) -> Dict[str, Any]:
    """Deterministic preflight validation before spending AI tokens (PH01-03 / F38)."""
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

    return {
        "passed": len(veto_codes) == 0,
        "veto_codes": veto_codes,
        "reason": "OK" if not veto_codes else f"Preflight veto: {', '.join(veto_codes)}",
    }


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
    When `proactive_mode` is True (default for live market packets), complete timeframes resolve
    actively to LONG or SHORT with mode-specific Entry/SL/TP levels instead of blocking on WAIT_RETEST.
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
    res = row["result"] if "result" in keys else None
    if res:
        return str(res)
    dec = str(row["final_decision"] if "final_decision" in keys else "").upper().strip()
    if dec in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return "OPEN (LEGACY PAPER)"
    return "OBSERVED (WAIT)"

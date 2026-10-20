"""
Application service boundary, event-based Telegram notification dispatcher with
durable deduplication and bounded retry/backoff, and use-case orchestration
for AI Consensus Trading (Phase 4 & Phase 5 / PH02-03 / F34, F35, T00).
Strictly zero side-effects on import.
"""

import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, MutableMapping, Optional, Tuple

import requests

from .config import (
    DEFAULT_DB_FILE,
    LEGACY_ACCOUNTING_MODE,
    TG_CHAT,
    TG_TOKEN,
    get_groq_keys,
    redact_secrets,
)
from .domain import (
    SignalLifecycleState,
    compute_signal_idempotency_key,
    is_finite_number,
    normalize_signal_lifecycle_state,
)
from .storage import (
    check_and_record_telegram_outbox,
    compute_dual_performance_metrics,
    connect_db,
    mark_telegram_outbox_status,
    reconcile_paper_positions,
    run_migrations,
    save_signal_record,
)

EXECUTION_CLASSIFICATION_BADGES: Dict[str, str] = {
    "GENERATED_SIGNAL": "📡 [GENERATED SIGNAL — NOT AN EXECUTED ORDER]",
    "PAPER_TRADE_FILL": "[SIMULATED PAPER-TRADE FILL — SIMULASI INTERNAL / BUKAN EKSEKUSI BURSA]",
    "SUBMITTED_ORDER": "📤 [EXCHANGE ORDER SUBMITTED — AWAITING FILL CONFIRMATION]",
    "CONFIRMED_EXCHANGE_FILL": "✅ [CONFIRMED EXCHANGE FILL / ACTIVE POSITION]",
    "CLOSED_TRADE": "🏁 [CLOSED EXCHANGE POSITION — REALIZED PNL RECONCILED]",
    "EXECUTION_GUARD_ALERT": "🛡️ [RISK GUARD / CIRCUIT BREAKER / KILL SWITCH ALERT]",
    "LIVE_EXECUTION": "🚫 [LIVE EXECUTION: DISABLED BY DEFAULT UNLESS EXPLICITLY AUTHORIZED]",
}


def sanitize_ui_error(err_or_msg: Any, extra_secrets: Optional[Iterable[str]] = None) -> str:
    raw_text = str(err_or_msg or "")
    return str(redact_secrets(raw_text, extra_secrets=extra_secrets))


def clone_analysis_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    snap: Dict[str, Any] = {}
    for k, v in result.items():
        if k == "frames":
            snap[k] = dict(v) if isinstance(v, dict) else v
        else:
            snap[k] = copy.deepcopy(v)
    if not snap.get("analysis_id"):
        snap["analysis_id"] = f"ana_{snap.get('symbol', 'UNKNOWN')}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    if not snap.get("analyzed_at"):
        snap["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    if not snap.get("idempotency_key"):
        snap["idempotency_key"] = compute_signal_idempotency_key(snap)
    return snap


def ensure_session_state(state: Optional[MutableMapping[str, Any]] = None) -> MutableMapping[str, Any]:
    target: MutableMapping[str, Any] = state if state is not None else {}
    if "scan" not in target:
        target["scan"] = []
    if "last_result" not in target:
        target["last_result"] = None
    if "analyses_by_id" not in target:
        target["analyses_by_id"] = {}
    if "candidate_analysis_ids" not in target:
        target["candidate_analysis_ids"] = {}
    if "saved_analysis_ids" not in target:
        target["saved_analysis_ids"] = {}
    if "notified_analysis_ids" not in target:
        target["notified_analysis_ids"] = set()
    if "notified_event_keys" not in target:
        target["notified_event_keys"] = set()
    if "v56_processed_fingerprints" not in target:
        target["v56_processed_fingerprints"] = set()
    return target


def store_analysis_snapshot(
    result: Dict[str, Any],
    state: Optional[MutableMapping[str, Any]] = None,
) -> Dict[str, Any]:
    target = ensure_session_state(state)
    snap = clone_analysis_snapshot(result)
    aid = snap["analysis_id"]
    target["analyses_by_id"][aid] = snap
    target["candidate_analysis_ids"][snap["symbol"]] = aid
    target["last_result"] = snap
    return snap


def get_rendered_analysis_snapshot(
    symbol: Optional[str] = None,
    state: Optional[MutableMapping[str, Any]] = None,
    analysis_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    target = ensure_session_state(state)
    aid = analysis_id
    if not aid and symbol:
        aid = target["candidate_analysis_ids"].get(symbol)
    if not aid:
        return None
    return target["analyses_by_id"].get(aid)


def run_and_store_analysis_service(
    symbol: str,
    scanner_score_value: float,
    runner: Callable[[str, float], Dict[str, Any]],
    state: Optional[MutableMapping[str, Any]] = None,
) -> Dict[str, Any]:
    target = ensure_session_state(state)
    raw_result = runner(symbol, scanner_score_value)
    return store_analysis_snapshot(raw_result, state=target)


def analyze_candidates_batch_service(
    candidates: List[Dict[str, Any]],
    top_n: int,
    runner: Callable[[str, float], Dict[str, Any]],
    state: Optional[MutableMapping[str, Any]] = None,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    target = ensure_session_state(state)
    results: List[Dict[str, Any]] = []
    for candidate in (candidates or [])[: max(int(top_n), 0)]:
        symbol = candidate["symbol"]
        existing = get_rendered_analysis_snapshot(symbol=symbol, state=target)
        if existing is not None and not force_refresh:
            results.append(existing)
            continue
        res = run_and_store_analysis_service(
            symbol,
            candidate["score"],
            runner=runner,
            state=target,
        )
        results.append(res)
    return results


def format_telegram_signal_message(result: Dict[str, Any]) -> str:
    """
    Format a validated signal message for Telegram (Phase 4).
    Always includes asset, direction, signal state, timeframe, timestamp, risk levels,
    and an explicit distinction between a generated signal, simulated paper fill, and live order.
    """
    final = str(result.get("final", "WAIT")).upper().strip()
    icon = "🚀" if final == "LONG" else ("🩸" if final == "SHORT" else "⏸️")
    sig_state = normalize_signal_lifecycle_state(
        result.get("signal_lifecycle_state")
        or ("TRIGGERED" if final in {"LONG", "SHORT"} else "WAIT")
    )
    sig_id = str(result.get("idempotency_key") or result.get("analysis_id") or "-")
    ts_str = str(result.get("analyzed_at") or result.get("bar_time") or datetime.now(timezone.utc).isoformat())[:19].replace("T", " ") + " UTC"
    tf_str = str(result.get("timeframe") or "5m/15m/1h")
    entry_v = result.get("entry")
    tp_v = result.get("tp")
    sl_v = result.get("sl")
    inv_v = result.get("invalidation_level") or sl_v
    rr_v = float(result.get("rr") or 0.0)
    net_rr_v = float(result.get("net_rr") or rr_v)
    conf_v = float((result.get("portfolio") or {}).get("confidence") or 0.0)
    scan_v = result.get("scanner_score", 0)
    packet = result.get("packet") or {}
    smc = packet.get("smc") or {}
    stc = smc.get("structure") or {}
    ob = packet.get("order_book") or {}
    c_info = packet.get("candlestick") or {}

    entry_line = f"${float(entry_v):,.8f}" if is_finite_number(entry_v) else "N/A"
    tp_line = f"${float(tp_v):,.8f}" if is_finite_number(tp_v) else "N/A"
    sl_line = f"${float(sl_v):,.8f}" if is_finite_number(sl_v) else "N/A"
    inv_line = f"${float(inv_v):,.8f}" if is_finite_number(inv_v) else "N/A"

    raw_msg = f"""🚨 SECRETAGENT MOMENTUM SIGNAL
{EXECUTION_CLASSIFICATION_BADGES['GENERATED_SIGNAL']}

{icon} Asset: {result.get('symbol', 'UNKNOWN')}
📌 Direction: {final} | State: {sig_state}
⏱️ Timeframe: {tf_str} | Timestamp: {ts_str}
🆔 Signal ID: {sig_id}
📊 Score Indicator: {conf_v:.0f}/100 (bukan probabilitas) | Radar: {scan_v}/100

💰 Theoretical Entry: {entry_line}
🎯 Take-Profit (TP): {tp_line}
🛑 Stop-Loss (SL): {sl_line}
⚠️ Invalidation Level: {inv_line}
⚖️ R:R (Gross / Net): 1:{rr_v:.2f} / 1:{net_rr_v:.2f}

🧠 Market Regime: {packet.get('market_regime', '-')}
🏗️ Structure: {stc.get('bias', '-')} | BOS={stc.get('bos', '-')}
🧱 SMC Score: {smc.get('smc_score', 0)}/100 | Timing: {result.get('entry_status', '-')}
📚 Order Imbalance: {float(ob.get('imbalance', 0.0) or 0.0):+.1%} | Candle: {c_info.get('name', '-')}

👨‍⚖️ AI Council Commentary:
{(result.get('portfolio') or {}).get('reasoning', '-')}

⚠️ Simulasi riset & paper-trading saja. Tidak ada order live ke exchange."""
    return sanitize_ui_error(raw_msg)


def format_telegram_event_message(
    event_type: str,
    payload: Dict[str, Any],
    extra_secrets: Optional[Iterable[str]] = None,
) -> str:
    """
    Format structured event-based Telegram notification messages (Phase 4):
    Supports:
    - SIGNAL_TRIGGERED
    - SETUP_MATERIAL_CHANGE
    - SETUP_INVALIDATED
    - PAPER_TRADE_OPENED / PAPER_TRADE_CLOSED / PAPER_TRADE_TP_HIT / PAPER_TRADE_SL_HIT / PAPER_TRADE_TIME_STOP
    - RISK_GUARD_REJECTED
    - SYSTEM_CRITICAL_FAILURE
    Every message includes asset, direction, signal state, timeframe, timestamp, risk levels,
    and execution classification badge.
    """
    ev_norm = str(event_type or "SIGNAL_TRIGGERED").upper().strip()
    sym = str(payload.get("symbol") or "SYSTEM").upper().strip()
    direction = str(payload.get("direction") or payload.get("final") or "WAIT").upper().strip()
    sig_state = normalize_signal_lifecycle_state(
        payload.get("signal_state")
        or payload.get("state")
        or payload.get("lifecycle_state")
        or ("INVALIDATED" if "INVALID" in ev_norm else ("TRIGGERED" if direction in {"LONG", "SHORT"} else "WAIT"))
    )
    tf_str = str(payload.get("timeframe") or "5m/15m/1h")
    ts_str = str(payload.get("timestamp") or payload.get("closed_at") or payload.get("created_at") or datetime.now(timezone.utc).isoformat())[:19].replace("T", " ") + " UTC"
    sig_id = str(payload.get("signal_id") or payload.get("position_id") or payload.get("idempotency_key") or f"evt_{sym}_{ev_norm}")

    exec_mode = str(payload.get("execution_mode") or "PAPER").upper().strip()
    ord_status = str(
        payload.get("order_status") or payload.get("status") or payload.get("lifecycle_state") or sig_state
    ).upper().strip()

    if "PAPER_TRADE" in ev_norm:
        badge = EXECUTION_CLASSIFICATION_BADGES["PAPER_TRADE_FILL"]
    elif ev_norm in {"LIVE_ENTRY_SUBMITTED", "ORDER_SUBMITTED"}:
        badge = EXECUTION_CLASSIFICATION_BADGES["SUBMITTED_ORDER"]
    elif ev_norm in {"ENTRY_FILLED_PROTECTED", "ENTRY_PARTIAL_FILL", "EXIT_PARTIAL_FILL", "SL_TP_STATUS_UPDATED"}:
        badge = EXECUTION_CLASSIFICATION_BADGES["CONFIRMED_EXCHANGE_FILL"]
    elif ev_norm in {"LIVE_POSITION_CLOSED", "POSITION_CLOSED"}:
        badge = EXECUTION_CLASSIFICATION_BADGES["CLOSED_TRADE"]
    elif ev_norm in {
        "ORDER_REJECTED",
        "RISK_GUARD_REJECTED",
        "CIRCUIT_BREAKER_ACTIVATED",
        "RECONCILIATION_MISMATCH",
        "CRITICAL_PROTECTION_FAILURE",
        "KILL_SWITCH_ACTIVATED",
    }:
        badge = EXECUTION_CLASSIFICATION_BADGES["EXECUTION_GUARD_ALERT"]
    elif "SYSTEM" in ev_norm:
        badge = EXECUTION_CLASSIFICATION_BADGES["LIVE_EXECUTION"]
    else:
        badge = EXECUTION_CLASSIFICATION_BADGES["GENERATED_SIGNAL"]

    entry_v = payload.get("entry") or payload.get("entry_price")
    sl_v = payload.get("stop_loss") or payload.get("sl")
    tp_v = payload.get("take_profit") or payload.get("tp")
    inv_v = payload.get("invalidation_level") or sl_v
    exit_v = payload.get("exit_price")
    rr_v = payload.get("net_rr") or payload.get("rr") or payload.get("gross_r_multiple")
    pos_qty = payload.get("quantity") or payload.get("position_size") or payload.get("open_qty")
    risk_usd = payload.get("risk_budget") or payload.get("risk_budget_usd") or payload.get("net_pnl")

    levels_parts: List[str] = []
    if is_finite_number(entry_v):
        levels_parts.append(f"Entry: ${float(entry_v):,.8f}")
    if is_finite_number(sl_v):
        levels_parts.append(f"SL: ${float(sl_v):,.8f}")
    if is_finite_number(tp_v):
        levels_parts.append(f"TP: ${float(tp_v):,.8f}")
    if is_finite_number(inv_v):
        levels_parts.append(f"Invalidation: ${float(inv_v):,.8f}")
    if is_finite_number(exit_v):
        levels_parts.append(f"Exit Price: ${float(exit_v):,.8f}")
    if is_finite_number(pos_qty) and float(pos_qty) > 0:
        levels_parts.append(f"Position Size: {float(pos_qty):.6f}")
    if is_finite_number(risk_usd):
        levels_parts.append(f"Estimated/Realized Risk ($): ${float(risk_usd):,.2f}")
    if is_finite_number(rr_v):
        levels_parts.append(f"R:R / R-Mult: {float(rr_v):+.2f}")
    levels_block = "\n".join(f"• {line}" for line in levels_parts) if levels_parts else "• Risk levels: N/A"

    detail = str(payload.get("reason") or payload.get("detail") or payload.get("invalidation_reason") or "-")
    if ev_norm == "RISK_GUARD_REJECTED":
        vetoes = payload.get("veto_codes") or []
        detail = f"Risk Guard memblokir setup ({', '.join(str(v) for v in vetoes) if vetoes else detail})"
    elif "PAPER_TRADE" in ev_norm and is_finite_number(payload.get("net_pnl")):
        detail = (
            f"Hasil Paper Trade: {payload.get('result', ev_norm)} | "
            f"Net PnL: ${float(payload['net_pnl']):+,.2f} | "
            f"Net R: {float(payload.get('net_r_multiple', 0.0) or 0.0):+.2f}R"
        )

    footer_note = (
        f"⚠️ Mode Eksekusi: {exec_mode} | Status Order/Setup: {ord_status}"
        if exec_mode in {"TESTNET", "LIVE"}
        else "⚠️ Mode PAPER — simulasi & analisis saja, tidak ada order live ke exchange."
    )

    msg = (
        f"🔔 SECRETAGENT EVENT: {ev_norm}\n"
        f"{badge}\n\n"
        f"📌 Asset: {sym} | Mode: {exec_mode}\n"
        f"🧭 Direction: {direction} | State/Status: {ord_status}\n"
        f"⏱️ Timeframe: {tf_str} | Timestamp: {ts_str}\n"
        f"🆔 Event/Signal ID: {sig_id}\n\n"
        f"📐 Key Risk & Execution Levels:\n{levels_block}\n\n"
        f"📝 Detail:\n{detail}\n\n"
        f"{footer_note}"
    )
    return sanitize_ui_error(msg, extra_secrets=extra_secrets)


def send_telegram_notification(
    message: str,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
    extra_secrets: Optional[Iterable[str]] = None,
    *,
    max_retries: int = 2,
    timeout_seconds: float = 10.0,
    backoff_seconds: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Tuple[bool, str]:
    """
    Send a Telegram message with secret redaction, explicit timeout, and bounded retry/backoff
    on transient network/HTTP 5xx/429 errors without duplicate delivery once succeeded (Phase 4).
    """
    token = tg_token if tg_token is not None else TG_TOKEN
    chat = tg_chat if tg_chat is not None else TG_CHAT
    if not token or not chat:
        return False, "Telegram belum dikonfigurasi."

    secrets_list = list(extra_secrets or []) + [token]
    safe_message = sanitize_ui_error(message, extra_secrets=secrets_list)
    http_post = post_fn or requests.post
    max_attempts = max(1, min(4, int(max_retries) + 1))
    last_err = "UNKNOWN_ERROR"

    for attempt in range(1, max_attempts + 1):
        try:
            r = http_post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": safe_message},
                timeout=max(1.0, float(timeout_seconds)),
            )
            if r.status_code == 200:
                data = r.json()
                ok_flag = bool(data.get("ok"))
                desc = sanitize_ui_error(data.get("description", "OK"), extra_secrets=secrets_list)
                if ok_flag:
                    return True, desc
                last_err = desc
                break
            last_err = sanitize_ui_error((r.text or f"HTTP {r.status_code}")[:300], extra_secrets=secrets_list)
            # Only retry on transient 429 or 5xx status codes
            if r.status_code not in (429, 500, 502, 503, 504) or attempt >= max_attempts:
                return False, last_err
        except Exception as exc:
            last_err = sanitize_ui_error(exc, extra_secrets=secrets_list)
            if attempt >= max_attempts:
                return False, last_err

        if backoff_seconds > 0 and attempt < max_attempts:
            sleep_fn(float(backoff_seconds) * (2 ** (attempt - 1)))

    return False, last_err


def dispatch_telegram_event(
    event_type: str,
    payload: Dict[str, Any],
    *,
    event_key: Optional[str] = None,
    db_file: str = DEFAULT_DB_FILE,
    state: Optional[MutableMapping[str, Any]] = None,
    tg_enabled: bool = True,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
    extra_secrets: Optional[Iterable[str]] = None,
    max_retries: int = 2,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """
    Deduplicated, event-based Telegram notification dispatcher (Phase 4).
    Computes a deterministic `event_key` from `(event_type, symbol, signal_id/bar_time, state, direction)`
    and verifies against both session state and SQLite `telegram_outbox` so unchanged signals
    or repeated polling cycles NEVER produce duplicate notifications.
    """
    target = ensure_session_state(state)
    ev_norm = str(event_type or "SIGNAL_TRIGGERED").upper().strip()
    sym = str(payload.get("symbol") or "SYSTEM").upper().strip()
    direction = str(payload.get("direction") or payload.get("final") or "WAIT").upper().strip()
    sig_state = normalize_signal_lifecycle_state(
        payload.get("signal_state") or payload.get("state") or payload.get("lifecycle_state") or "WAIT"
    )
    tf_str = str(payload.get("timeframe") or "5m/15m/1h")
    sig_id = str(
        payload.get("signal_id")
        or payload.get("position_id")
        or payload.get("idempotency_key")
        or payload.get("analysis_id")
        or payload.get("bar_time")
        or "default"
    )

    if not event_key:
        # Keep one alert per unchanged logical event, but distinguish multiple exchange orders/fills
        # belonging to the same signal or position. Without these fields, a later partial exit could
        # be incorrectly suppressed forever by the first partial-exit event for that position.
        event_identity_fields = (
            "event_id", "execution_event_id", "fill_id", "trade_id",
            "exchange_order_id", "order_id", "client_order_id",
            "executed_qty", "filled_qty", "cumulative_filled_qty", "quantity", "open_qty",
            "avg_fill_price", "entry", "exit_price", "net_pnl",
            "order_status", "status", "lifecycle_state", "veto_codes", "reason", "order_ids", "fills",
            "event_timestamp", "timestamp", "created_at", "closed_at",
        )
        event_identity = {
            key: payload[key]
            for key in event_identity_fields
            if key in payload and payload[key] is not None
        }
        identity_json = json.dumps(event_identity, sort_keys=True, separators=(",", ":"), default=str)
        identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:16] if event_identity else "base"
        raw_k = f"{ev_norm}|{sym}|{sig_id}|{sig_state}|{direction}|{identity_hash}"
        event_key = f"tgevt_{hashlib.sha256(raw_k.encode('utf-8')).hexdigest()[:20]}"

    if not tg_enabled:
        return {
            "sent": False,
            "deduplicated": False,
            "status": "DISABLED",
            "event_key": event_key,
            "event_type": ev_norm,
            "message": "",
        }

    if event_key in target["notified_event_keys"]:
        return {
            "sent": False,
            "deduplicated": True,
            "status": "ALREADY_SENT_SESSION",
            "event_key": event_key,
            "event_type": ev_norm,
            "message": "Duplikat event diabaikan (session deduplication).",
        }

    msg_text = format_telegram_event_message(ev_norm, payload, extra_secrets=extra_secrets)
    msg_hash = hashlib.sha256(msg_text.encode("utf-8")).hexdigest()[:16]

    outbox_check = check_and_record_telegram_outbox(
        event_key=event_key,
        event_type=ev_norm,
        signal_id=sig_id,
        symbol=sym,
        direction=direction,
        signal_state=sig_state,
        timeframe=tf_str,
        message_hash=msg_hash,
        db_file=db_file,
    )
    if outbox_check.get("already_sent"):
        target["notified_event_keys"].add(event_key)
        return {
            "sent": False,
            "deduplicated": True,
            "status": "ALREADY_SENT_DB",
            "event_key": event_key,
            "event_type": ev_norm,
            "message": "Duplikat event diabaikan (SQLite outbox deduplication).",
        }

    ok, resp_msg = send_telegram_notification(
        msg_text,
        tg_token=tg_token,
        tg_chat=tg_chat,
        post_fn=post_fn,
        extra_secrets=extra_secrets,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
    )
    mark_telegram_outbox_status(
        event_key=event_key,
        status="SENT" if ok else "FAILED_RETRYABLE",
        last_error="" if ok else resp_msg,
        db_file=db_file,
        extra_secrets=extra_secrets,
    )
    if ok:
        target["notified_event_keys"].add(event_key)

    return {
        "sent": bool(ok),
        "deduplicated": False,
        "status": "SENT" if ok else "FAILED_RETRYABLE",
        "event_key": event_key,
        "event_type": ev_norm,
        "response": resp_msg,
        "message": msg_text,
    }


def save_rendered_analysis_service(
    analysis_id: str,
    state: Optional[MutableMapping[str, Any]] = None,
    db_file: str = DEFAULT_DB_FILE,
    tg_enabled: bool = False,
    notifier_fn: Optional[Callable[[str], Tuple[bool, str]]] = None,
    message_formatter: Optional[Callable[[Dict[str, Any]], str]] = None,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    target = ensure_session_state(state)
    stored = get_rendered_analysis_snapshot(state=target, analysis_id=analysis_id)
    if stored is None:
        raise KeyError(f"Analysis ID tidak ditemukan di session state: {analysis_id}")

    ikey = stored.get("idempotency_key") or compute_signal_idempotency_key(stored)
    already_in_session = analysis_id in target["saved_analysis_ids"]

    run_migrations(db_file)
    con = connect_db(db_file)
    try:
        existing_row = None
        if analysis_id:
            existing_row = con.execute("SELECT id FROM signals WHERE analysis_id=? LIMIT 1", (analysis_id,)).fetchone()
        if existing_row is None and ikey:
            existing_row = con.execute("SELECT id FROM signals WHERE idempotency_key=? LIMIT 1", (ikey,)).fetchone()
        already_in_db = existing_row is not None
    finally:
        con.close()

    signal_id = save_signal_record(
        stored,
        db_file=db_file,
        analysis_id=analysis_id,
        idempotency_key=ikey,
        extra_secrets=extra_secrets,
    )
    target["saved_analysis_ids"][analysis_id] = signal_id
    created_new = not (already_in_session or already_in_db)

    tg_sent = False
    tg_msg = ""
    if (
        tg_enabled
        and stored.get("final") in {"LONG", "SHORT"}
        and analysis_id not in target["notified_analysis_ids"]
        and ikey not in target["notified_analysis_ids"]
    ):
        fmt_fn = message_formatter or format_telegram_signal_message
        send_fn = notifier_fn or send_telegram_notification
        ok, msg = send_fn(fmt_fn(stored))
        tg_sent = bool(ok)
        tg_msg = sanitize_ui_error(msg, extra_secrets=extra_secrets)
        if ok:
            target["notified_analysis_ids"].add(analysis_id)
            if ikey:
                target["notified_analysis_ids"].add(ikey)

    return {
        "signal_id": signal_id,
        "analysis_id": analysis_id,
        "idempotency_key": ikey,
        "created_new": created_new,
        "tg_sent": tg_sent,
        "tg_msg": tg_msg,
    }


def update_paper_trades_service(
    db_file: str = DEFAULT_DB_FILE,
    price_fetcher: Optional[Callable[[str], float]] = None,
    kline_fetcher: Optional[Callable[[str, str, int], Any]] = None,
    bars_by_symbol: Optional[Dict[str, Any]] = None,
    tie_breaker: str = "SL_FIRST_CONSERVATIVE",
    max_wait_bars: Optional[int] = None,
    max_hold_bars: Optional[int] = None,
    return_detailed: bool = False,
) -> List[Any]:
    if price_fetcher is None and kline_fetcher is None and bars_by_symbol is None:
        raise ValueError("price_fetcher, kline_fetcher, atau bars_by_symbol wajib disediakan untuk memeriksa paper trade.")

    return reconcile_paper_positions(
        db_file=db_file,
        bars_by_symbol=bars_by_symbol,
        kline_fetcher=kline_fetcher,
        price_fetcher=price_fetcher,
        tie_breaker=tie_breaker,
        max_wait_bars=max_wait_bars,
        max_hold_bars=max_hold_bars,
        return_detailed=return_detailed,
    )


def compute_performance_service(db_file: str = DEFAULT_DB_FILE, initial_equity: float = 10_000.0) -> Dict[str, Any]:
    return compute_dual_performance_metrics(db_file=db_file, initial_equity=initial_equity)


def reset_trading_state_service(
    state: Optional[MutableMapping[str, Any]] = None,
    db_file: str = DEFAULT_DB_FILE,
    confirmed: bool = False,
    backup_before_reset: bool = True,
    on_clear_callbacks: Optional[Iterable[Callable[[], None]]] = None,
) -> Dict[str, Any]:
    if not confirmed:
        raise PermissionError("Reset database memerlukan konfirmasi eksplisit (confirmed=True).")

    import sqlite3

    run_migrations(db_file)
    backup_path = None
    con = connect_db(db_file)
    try:
        existing_tables = {
            str(r[0])
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        target_tables = [
            tbl
            for tbl in (
                "telegram_outbox",
                "risk_guard_state",
                "paper_account_ledger",
                "paper_positions",
                "agent_votes",
                "memories",
                "signals",
                "pattern_events",
                "usage_events",
                "agent_runs",
                "analysis_runs",
                "dag_checkpoints",
                "budget_reservations",
                "quota_scopes",
                "monitor_runtime_state",
            )
            if tbl in existing_tables
        ]
        if backup_before_reset and db_file != ":memory:":
            try:
                row_count = sum(
                    int(con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
                    for tbl in target_tables
                )
                if row_count > 0:
                    backup_path = f"{db_file}.reset_backup"
                    bcon = sqlite3.connect(backup_path)
                    con.backup(bcon)
                    bcon.close()
            except Exception:
                backup_path = None

        for tbl in target_tables:
            con.execute(f"DELETE FROM {tbl}")
        con.commit()
    finally:
        con.close()

    target = ensure_session_state(state)
    target["saved_analysis_ids"] = {}
    target["notified_analysis_ids"] = set()
    target["notified_event_keys"] = set()
    target["v56_processed_fingerprints"] = set()
    target["analyses_by_id"] = {}
    target["candidate_analysis_ids"] = {}
    target["last_result"] = None
    target["confirm_reset"] = False

    if on_clear_callbacks:
        for cb in on_clear_callbacks:
            cb()

    return {"reset": True, "backup_path": backup_path}

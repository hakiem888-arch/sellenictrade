"""
Application service boundary & use-case orchestration for AI Consensus Trading (PH02-03 / F34, F35, T00).
"""

import copy
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
    compute_signal_idempotency_key,
    is_finite_number,
)
from .storage import (
    compute_dual_performance_metrics,
    connect_db,
    reconcile_paper_positions,
    run_migrations,
    save_signal_record,
)


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
    final = result.get("final", "WAIT")
    icon = "🚀" if final == "LONG" else "🩸"
    raw_msg = f"""🚨 AI TRADING V5.5

{icon} {result['symbol']}
SIGNAL: {final}
CONFIDENCE: {result['portfolio']['confidence']:.0f}/100
SCANNER: {result['scanner_score']}/100

💰 Entry: ${result['entry']:,.8f}
🎯 TP: ${result['tp']:,.8f}
🛑 SL: ${result['sl']:,.8f}
⚖️ R:R: 1:{result['rr']:.1f}

🧠 Market: {result['packet']['market_regime']}
🏗️ Structure: {result['packet']['smc']['structure']['bias']} | BOS={result['packet']['smc']['structure']['bos']}
🧱 SMC Score: {result['packet']['smc']['smc_score']}/100
⏱️ Entry Timing: {result['entry_status']}
📚 Order Imbalance: {result['packet']['order_book']['imbalance']:+.1%}
🕯 Candle: {result['packet']['candlestick']['name']}

👨‍⚖️ Final Judge:
{result['portfolio']['reasoning']}

⚠️ Paper-analysis only. Tidak ada order otomatis."""
    return sanitize_ui_error(raw_msg)


def send_telegram_notification(
    message: str,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Tuple[bool, str]:
    token = tg_token if tg_token is not None else TG_TOKEN
    chat = tg_chat if tg_chat is not None else TG_CHAT
    if not token or not chat:
        return False, "Telegram belum dikonfigurasi."

    secrets_list = list(extra_secrets or []) + [token]
    safe_message = sanitize_ui_error(message, extra_secrets=secrets_list)
    http_post = post_fn or requests.post

    try:
        r = http_post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": safe_message},
            timeout=12,
        )
        if r.status_code != 200:
            return False, sanitize_ui_error(r.text[:300], extra_secrets=secrets_list)
        data = r.json()
        return bool(data.get("ok")), sanitize_ui_error(data.get("description", "OK"), extra_secrets=secrets_list)
    except Exception as exc:
        return False, sanitize_ui_error(exc, extra_secrets=secrets_list)


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
    target["v56_processed_fingerprints"] = set()
    target["analyses_by_id"] = {}
    target["candidate_analysis_ids"] = {}
    target["last_result"] = None
    target["confirm_reset"] = False

    if on_clear_callbacks:
        for cb in on_clear_callbacks:
            cb()

    return {"reset": True, "backup_path": backup_path}

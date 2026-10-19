"""
Independent background monitor worker, CLI monitor runner, single-cycle radar + paper
reconciliation orchestrator, duplicate-worker prevention, and comprehensive 9-component
System Health Monitor for AI Consensus Trading (Phase 5 / PH07-01 / F29, F30, F31, F32, F33, F35).
Strictly zero UI, DB, or network side-effects on import.
"""

import argparse
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, MutableMapping, Optional, Tuple

from .config import DEFAULT_DB_FILE, redact_secrets
from .market import GLOBAL_MARKET_CACHE, get_monitor_runtime_status, normalize_as_of_ms
from .services import ensure_session_state, sanitize_ui_error
from .storage import (
    clear_worker_heartbeats,
    get_persisted_risk_guard_state,
    get_worker_heartbeat_state,
    reconcile_paper_positions,
    record_worker_heartbeat,
    verify_database_integrity,
)

MONITOR_ENGINE_VERSION: str = "v6.2-independent-health-monitor-1"

_ACTIVE_WORKER_REGISTRY_LOCK = threading.Lock()
_ACTIVE_WORKERS: Dict[str, "BackgroundMonitorWorker"] = {}


@dataclass
class MonitorCycleReport:
    """Structured report for a single headless or UI monitor cycle (PH07-01 / F28, F29, T14)."""
    cycle_id: str
    worker_id: str
    execution_mode: str
    started_at: str
    completed_at: str
    as_of_ms: int
    monitor_n: int
    max_validations: int
    candidates_count: int = 0
    developing_count: int = 0
    triggered_count: int = 0
    interesting_count: int = 0
    ai_validations_run: int = 0
    signals_saved: int = 0
    telegram_sent_count: int = 0
    paper_trades_updated: int = 0
    paper_updates: List[Any] = field(default_factory=list)
    candidate_rows: List[Dict[str, Any]] = field(default_factory=list)
    workflow_outcomes: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    runtime_status: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "worker_id": self.worker_id,
            "execution_mode": self.execution_mode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "as_of_ms": self.as_of_ms,
            "monitor_n": self.monitor_n,
            "max_validations": self.max_validations,
            "candidates_count": self.candidates_count,
            "developing_count": self.developing_count,
            "triggered_count": self.triggered_count,
            "interesting_count": self.interesting_count,
            "ai_validations_run": self.ai_validations_run,
            "signals_saved": self.signals_saved,
            "telegram_sent_count": self.telegram_sent_count,
            "paper_trades_updated": self.paper_trades_updated,
            "paper_updates": list(self.paper_updates),
            "candidate_rows": [dict(r) for r in self.candidate_rows],
            "workflow_outcomes": [dict(o) for o in self.workflow_outcomes],
            "diagnostics": dict(self.diagnostics),
            "runtime_status": dict(self.runtime_status),
            "errors": list(self.errors),
            "monitor_engine_version": MONITOR_ENGINE_VERSION,
        }


def get_durable_monitor_runtime_status(
    db_file: str = DEFAULT_DB_FILE,
    *,
    auto_enabled: bool = False,
    interval_seconds: int = 45,
    worker_id: Optional[str] = "default_worker",
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Inspect SQLite `monitor_runtime_state` and compute honest runtime status (PH07-01 / F29):
    - If an independent background/CLI worker has recorded a fresh `RUNNING` heartbeat within
      `max(interval_seconds * 2.5, 120.0)` seconds, reports `execution_mode='worker'` and `worker_alive=True`.
    - Otherwise reports `execution_mode='session_only'` and `worker_alive=False` with an explicit
      Indonesian disclaimer explaining that Streamlit UI fragment monitoring only runs while the browser
      tab remains open, and showing how to run the independent CLI/background worker.
    """
    curr_ms = normalize_as_of_ms(now_ms)
    hb = get_worker_heartbeat_state(worker_id=worker_id, db_file=db_file)

    if hb is not None:
        hb_ms = int(hb.get("last_heartbeat_ms") or 0)
        hb_status = str(hb.get("status") or "IDLE").upper().strip()
        hb_interval = int(hb.get("interval_seconds") or interval_seconds or 45)
        eff_interval = max(1, int(interval_seconds or hb_interval))
        max_age_s = max(float(eff_interval) * 2.5, 120.0)
        age_s = max(0.0, (curr_ms - hb_ms) / 1000.0) if hb_ms > 0 else float("inf")
        is_alive = bool(hb_status == "RUNNING" and hb_ms > 0 and age_s <= max_age_s)

        eff_mode = "worker" if is_alive else "session_only"
        base_status = get_monitor_runtime_status(
            execution_mode=eff_mode,
            auto_enabled=auto_enabled,
            last_heartbeat_ms=hb_ms if hb_ms > 0 else None,
            interval_seconds=eff_interval,
            now_ms=curr_ms,
            worker_id=str(hb.get("worker_id") or worker_id or "default_worker"),
            worker_status=hb_status,
            cycles_completed=int(hb.get("cycles_completed") or 0),
            candidates_scanned=int(hb.get("candidates_scanned") or 0),
            ai_validations_run=int(hb.get("ai_validations_run") or 0),
            signals_saved=int(hb.get("signals_saved") or 0),
            paper_trades_reconciled=int(hb.get("paper_trades_reconciled") or 0),
            last_cycle_at=hb.get("last_cycle_at"),
            last_error=str(hb.get("last_error") or ""),
        )
        if not is_alive and hb_ms > 0:
            base_status["worker_stale_or_stopped"] = True
            base_status["runtime_label"] = (
                "SESSION_ONLY_FRAGMENT" if auto_enabled else "WORKER_STALE_OR_STOPPED"
            )
        return base_status

    return get_monitor_runtime_status(
        execution_mode="session_only",
        auto_enabled=auto_enabled,
        last_heartbeat_ms=None,
        interval_seconds=interval_seconds,
        now_ms=curr_ms,
        worker_id=worker_id or "default_worker",
        worker_status="NOT_STARTED",
    )


def get_system_health_report(
    *,
    db_file: str = DEFAULT_DB_FILE,
    exchange_health_fn: Optional[Callable[[], Tuple[bool, str]]] = None,
    ai_keys_available: bool = False,
    ai_quota_exhausted: bool = False,
    tg_configured: bool = False,
    last_scan_diagnostics: Optional[Dict[str, Any]] = None,
    auto_enabled: bool = False,
    interval_seconds: int = 45,
    worker_id: str = "default_worker",
    now_ms: Optional[int] = None,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Lightweight, non-blocking System Health Monitor across all 9 operational components (Phase 5):
    1. exchange_connectivity
    2. market_data_freshness
    3. candle_processing
    4. signal_pipeline
    5. ai_provider
    6. database_connectivity
    7. background_worker
    8. telegram_delivery
    9. paper_and_backtest_engine
    Exposes HEALTHY, DEGRADED, or UNAVAILABLE states clearly and ensures critical failures
    cannot silently produce apparently valid signals (`can_generate_actionable_signals`).
    """
    curr_ms = normalize_as_of_ms(now_ms)
    components: Dict[str, Dict[str, Any]] = {}

    # 1. Exchange connectivity
    diag = last_scan_diagnostics if isinstance(last_scan_diagnostics, dict) else {}
    rate_limited = bool(diag.get("rate_limited", False))
    if exchange_health_fn is not None:
        try:
            ex_ok, ex_msg = exchange_health_fn()
            ex_msg_safe = sanitize_ui_error(ex_msg, extra_secrets=extra_secrets)
            components["exchange_connectivity"] = {
                "status": "DEGRADED" if (ex_ok and rate_limited) else ("HEALTHY" if ex_ok else "UNAVAILABLE"),
                "detail": f"{ex_msg_safe} (Rate-limit cooldown aktif)" if (ex_ok and rate_limited) else ex_msg_safe,
            }
        except Exception as ex_exc:
            components["exchange_connectivity"] = {
                "status": "UNAVAILABLE",
                "detail": sanitize_ui_error(ex_exc, extra_secrets=extra_secrets),
            }
    else:
        if rate_limited:
            components["exchange_connectivity"] = {
                "status": "DEGRADED",
                "detail": f"Rate-limit HTTP 429 cooldown aktif ({diag.get('retry_after_seconds', 0)}s).",
            }
        elif diag.get("succeeded_count", 0) > 0:
            components["exchange_connectivity"] = {
                "status": "HEALTHY",
                "detail": f"OK ({diag.get('succeeded_count', 0)} simbol berhasil dipindai).",
            }
        elif diag.get("failed_count", 0) > 0 and diag.get("succeeded_count", 0) == 0:
            components["exchange_connectivity"] = {
                "status": "UNAVAILABLE",
                "detail": "Semua permintaan market data pada siklus terakhir gagal.",
            }
        else:
            components["exchange_connectivity"] = {
                "status": "HEALTHY",
                "detail": "Siap (menunggu siklus pemindaian).",
            }

    # 2. Market-data freshness
    cache_stats = GLOBAL_MARKET_CACHE.stats()
    if diag.get("failed_count", 0) > 0 and diag.get("succeeded_count", 0) == 0:
        components["market_data_freshness"] = {
            "status": "UNAVAILABLE",
            "detail": "Tidak ada snapshot candle segar yang tersedia.",
        }
    elif rate_limited or diag.get("failed_count", 0) > 0:
        components["market_data_freshness"] = {
            "status": "DEGRADED",
            "detail": f"Sebagian simbol gagal ({diag.get('failed_count', 0)} gagal, {diag.get('succeeded_count', 0)} segar).",
        }
    else:
        components["market_data_freshness"] = {
            "status": "HEALTHY",
            "detail": f"Snapshot closed-candle tervalidasi (Cache entries: {cache_stats.get('kline_entries', 0)}).",
        }

    # 3. Candle processing
    components["candle_processing"] = {
        "status": "HEALTHY" if components["market_data_freshness"]["status"] != "UNAVAILABLE" else "DEGRADED",
        "detail": "Validasi OHLCV, filter closed-candle, dan warm-up EMA200 aktif.",
    }

    # 4. Database connectivity & integrity
    db_ok = False
    try:
        db_rep = verify_database_integrity(db_file)
        db_ok = bool(db_rep.get("ok", False))
        components["database_connectivity"] = {
            "status": "HEALTHY" if db_ok else "UNAVAILABLE",
            "detail": f"SQLite schema v{db_rep.get('schema_version', 0)} OK" if db_ok else f"Integritas DB bermasalah: {db_rep.get('integrity_messages')}",
        }
    except Exception as db_exc:
        components["database_connectivity"] = {
            "status": "UNAVAILABLE",
            "detail": sanitize_ui_error(db_exc, extra_secrets=extra_secrets),
        }

    # 5. AI provider availability
    if ai_quota_exhausted:
        components["ai_provider"] = {
            "status": "DEGRADED",
            "detail": "Kuota harian AI habis (DAILY_QUOTA_EXHAUSTED); hanya radar deterministik yang aktif.",
        }
    elif ai_keys_available:
        components["ai_provider"] = {
            "status": "HEALTHY",
            "detail": "Provider AI terkonfigurasi dan siap.",
        }
    else:
        components["ai_provider"] = {
            "status": "DEGRADED",
            "detail": "API key AI belum dikonfigurasi; validasi AI Council dinonaktifkan.",
        }

    # 6. Signal-generation pipeline
    sig_pipe_ok = (
        components["exchange_connectivity"]["status"] != "UNAVAILABLE"
        and components["market_data_freshness"]["status"] != "UNAVAILABLE"
        and db_ok
    )
    components["signal_pipeline"] = {
        "status": "HEALTHY" if sig_pipe_ok else "UNAVAILABLE",
        "detail": "Pipeline Momentum Radar -> MTF -> Risk Guard aktif." if sig_pipe_ok else "Pipeline diblokir karena data pasar atau DB tidak tersedia.",
    }

    # 7. Background workers
    rt_status = get_durable_monitor_runtime_status(
        db_file=db_file,
        auto_enabled=auto_enabled,
        interval_seconds=interval_seconds,
        worker_id=worker_id,
        now_ms=curr_ms,
    )
    if rt_status.get("worker_alive"):
        components["background_worker"] = {
            "status": "HEALTHY",
            "detail": f"Worker aktif ({rt_status.get('worker_id')}, siklus={rt_status.get('cycles_completed', 0)}).",
        }
    elif rt_status.get("worker_stale_or_stopped"):
        components["background_worker"] = {
            "status": "DEGRADED",
            "detail": f"Worker terakhir berhenti/stale ({rt_status.get('runtime_label')}).",
        }
    else:
        components["background_worker"] = {
            "status": "HEALTHY" if auto_enabled else "IDLE",
            "detail": f"Mode: {rt_status.get('runtime_label', 'SESSION_ONLY')}.",
        }

    # 8. Telegram delivery
    components["telegram_delivery"] = {
        "status": "HEALTHY" if tg_configured else "IDLE",
        "detail": "Notifikasi event-based deduplicated siap." if tg_configured else "Telegram opsional belum dikonfigurasi.",
    }

    # 9. Paper-trading & Risk Guard engine
    try:
        rg_state = get_persisted_risk_guard_state(db_file=db_file) if db_ok else {}
        halted = bool(rg_state.get("daily_loss_limit_breached") or rg_state.get("consecutive_loss_limit_breached"))
        components["paper_and_backtest_engine"] = {
            "status": "DEGRADED" if halted else ("HEALTHY" if db_ok else "UNAVAILABLE"),
            "detail": (
                "Risk Guard menghentikan pembukaan posisi baru karena batas kerugian harian/beruntun tercapai."
                if halted
                else f"Siap (Open paper trades: {rg_state.get('current_open_positions', 0)})."
            ),
        }
    except Exception as rg_exc:
        components["paper_and_backtest_engine"] = {
            "status": "DEGRADED",
            "detail": sanitize_ui_error(rg_exc, extra_secrets=extra_secrets),
        }

    statuses = [c["status"] for c in components.values()]
    if any(s == "UNAVAILABLE" for k, c in components.items() for s in [c["status"]] if k in {"exchange_connectivity", "market_data_freshness", "database_connectivity", "signal_pipeline"}):
        overall = "UNAVAILABLE"
    elif any(s in {"DEGRADED", "UNAVAILABLE"} for s in statuses):
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    return {
        "overall_status": overall,
        "can_generate_actionable_signals": bool(overall != "UNAVAILABLE" and sig_pipe_ok),
        "timestamp_ms": curr_ms,
        "timestamp_iso": datetime.fromtimestamp(curr_ms / 1000.0, tz=timezone.utc).isoformat(),
        "components": components,
        "runtime_status": rt_status,
    }


def run_monitor_cycle(
    *,
    monitor_n: int = 8,
    max_validations: int = 1,
    interval_seconds: int = 45,
    execution_mode: str = "worker",
    worker_id: str = "default_worker",
    db_file: str = DEFAULT_DB_FILE,
    tg_enabled: bool = False,
    ai_available: bool = True,
    state: Optional[MutableMapping[str, Any]] = None,
    momentum_fetcher: Optional[Callable[..., Any]] = None,
    workflow_processor: Optional[Callable[..., Dict[str, Any]]] = None,
    pattern_saver: Optional[Callable[..., Tuple[int, bool]]] = None,
    reconcile_fn: Optional[Callable[..., List[Any]]] = None,
    as_of: Any = None,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Execute one complete, headless monitoring & paper-trade reconciliation cycle (PH07-01 / F29, T14, T26).
    """
    target_state = ensure_session_state(state)
    now_ms = normalize_as_of_ms(as_of)
    started_iso = datetime.now(timezone.utc).isoformat()
    cycle_id = f"cyc_{worker_id}_{now_ms}_{uuid.uuid4().hex[:6]}"
    mode_norm = "worker" if str(execution_mode or "").strip().lower() == "worker" else "session_only"

    errors: List[str] = []
    paper_updates: List[Any] = []

    # Step 1: Reconcile open/pending paper positions
    if reconcile_fn is not None:
        try:
            upd = reconcile_fn()
            if isinstance(upd, list):
                paper_updates = upd
        except Exception as rec_exc:
            errors.append(f"reconcile_paper_positions: {sanitize_ui_error(rec_exc, extra_secrets=extra_secrets)}")

    # Step 2: Fetch momentum candidates + diagnostics
    snaps: List[Dict[str, Any]] = []
    diagnostics: Dict[str, Any] = {}
    if momentum_fetcher is not None:
        try:
            try:
                raw_mom = momentum_fetcher(int(monitor_n), return_report=True)
            except TypeError:
                raw_mom = momentum_fetcher(int(monitor_n))
            if isinstance(raw_mom, dict) and "candidates" in raw_mom:
                snaps = list(raw_mom.get("candidates") or [])
                diagnostics = dict(raw_mom.get("diagnostics") or {})
            elif isinstance(raw_mom, list):
                snaps = list(raw_mom)
        except Exception as mom_exc:
            errors.append(f"momentum_candidates: {sanitize_ui_error(mom_exc, extra_secrets=extra_secrets)}")

    candidate_rows: List[Dict[str, Any]] = []
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        p = snap.get("pattern") or {}
        smc = snap.get("smc") or {}
        candidate_rows.append({
            "Symbol": snap.get("symbol", "UNKNOWN"),
            "State": p.get("state", "WAIT"),
            "Direction": p.get("direction", "WAIT"),
            "Score": p.get("score", 0),
            "5M": smc.get("5m", {}).get("timing", "-"),
            "15M": smc.get("15m", {}).get("timing", "-"),
            "1H": smc.get("1h", {}).get("structure", {}).get("bias", "-"),
            "24h": f"{float(snap.get('change', 0.0) or 0.0):+.2f}%",
        })

    _state_rank = {"TRIGGERED": 4, "DEVELOPING": 3, "WAIT": 2, "WATCH": 1, "QUIET": 1, "INVALIDATED": 0}
    snaps = sorted(
        [x for x in snaps if isinstance(x, dict)],
        key=lambda x: (
            _state_rank.get(str((x.get("pattern") or {}).get("state", "WAIT")).upper(), 0),
            float((x.get("pattern") or {}).get("score", 0) or 0),
        ),
        reverse=True,
    )
    interesting = [
        x for x in snaps
        if str((x.get("pattern") or {}).get("state", "")).upper() in {"DEVELOPING", "TRIGGERED"}
    ]
    developing_cnt = sum(1 for x in interesting if str((x.get("pattern") or {}).get("state", "")).upper() == "DEVELOPING")
    triggered_cnt = sum(1 for x in interesting if str((x.get("pattern") or {}).get("state", "")).upper() == "TRIGGERED")

    # Step 3: Process interesting setups with bounded AI validations
    validations_run = 0
    signals_saved = 0
    tg_sent_cnt = 0
    outcomes: List[Dict[str, Any]] = []

    for snap in interesting:
        p = snap.get("pattern") or {}
        p_state = str(p.get("state", "WAIT")).upper()
        sym = str(snap.get("symbol", "UNKNOWN"))

        if p_state == "DEVELOPING":
            ev_id = None
            if pattern_saver is not None:
                try:
                    ev_id, _ = pattern_saver(snap)
                except Exception as ps_exc:
                    errors.append(f"save_pattern_event({sym}): {sanitize_ui_error(ps_exc, extra_secrets=extra_secrets)}")
            outcomes.append({
                "symbol": sym,
                "pattern_state": p_state,
                "score": p.get("score", 0),
                "direction": p.get("direction", "WAIT"),
                "event_id": ev_id,
                "ai_called": False,
                "action": "DEFERRED_DEVELOPING",
                "reason": "Setup berkembang; AI belum dipanggil untuk menghemat token.",
            })
            continue

        if p_state == "TRIGGERED":
            if not ai_available:
                if pattern_saver is not None:
                    try:
                        pattern_saver(snap)
                    except Exception:
                        pass
                outcomes.append({
                    "symbol": sym,
                    "pattern_state": p_state,
                    "score": p.get("score", 0),
                    "direction": p.get("direction", "WAIT"),
                    "ai_called": False,
                    "action": "SKIPPED_NO_AI_KEY",
                    "reason": "Setup terpicu, tetapi API key AI belum tersedia.",
                })
                continue

            if validations_run >= int(max_validations):
                outcomes.append({
                    "symbol": sym,
                    "pattern_state": p_state,
                    "score": p.get("score", 0),
                    "direction": p.get("direction", "WAIT"),
                    "ai_called": False,
                    "action": "DEFERRED_MAX_VALIDATIONS",
                    "reason": f"Batas validasi AI per siklus ({max_validations}) tercapai.",
                })
                continue

            if workflow_processor is not None:
                try:
                    wf_out = workflow_processor(
                        snap,
                        state=target_state,
                        tg_enabled=tg_enabled,
                    )
                    if wf_out.get("ai_called"):
                        validations_run += 1
                    if wf_out.get("persistence_status") == "SAVED" and wf_out.get("signal_id"):
                        signals_saved += 1
                    if wf_out.get("notification_status") == "SENT" and wf_out.get("ai_called"):
                        tg_sent_cnt += 1
                    outcomes.append({
                        "symbol": sym,
                        "pattern_state": p_state,
                        "score": p.get("score", 0),
                        "direction": p.get("direction", "WAIT"),
                        **wf_out,
                    })
                except Exception as wf_exc:
                    safe_err = sanitize_ui_error(wf_exc, extra_secrets=extra_secrets)
                    errors.append(f"workflow({sym}): {safe_err}")
                    outcomes.append({
                        "symbol": sym,
                        "pattern_state": p_state,
                        "score": p.get("score", 0),
                        "direction": p.get("direction", "WAIT"),
                        "ai_called": False,
                        "action": "ERROR",
                        "error": safe_err,
                    })

    completed_iso = datetime.now(timezone.utc).isoformat()
    err_summary = " | ".join(errors[:3]) if errors else ""

    if mode_norm == "worker":
        record_worker_heartbeat(
            worker_id=worker_id,
            execution_mode="worker",
            status="RUNNING",
            interval_seconds=int(interval_seconds),
            monitor_n=int(monitor_n),
            max_validations=int(max_validations),
            cycles_delta=1,
            candidates_delta=len(snaps),
            validations_delta=validations_run,
            signals_delta=signals_saved,
            reconciled_delta=len(paper_updates),
            heartbeat_ms=now_ms,
            last_cycle_at=completed_iso,
            last_error=err_summary,
            db_file=db_file,
            extra_secrets=extra_secrets,
        )

    rt_status = get_durable_monitor_runtime_status(
        db_file=db_file,
        auto_enabled=(mode_norm == "session_only"),
        interval_seconds=int(interval_seconds),
        worker_id=worker_id,
        now_ms=now_ms,
    )

    report = MonitorCycleReport(
        cycle_id=cycle_id,
        worker_id=worker_id,
        execution_mode=mode_norm,
        started_at=started_iso,
        completed_at=completed_iso,
        as_of_ms=now_ms,
        monitor_n=int(monitor_n),
        max_validations=int(max_validations),
        candidates_count=len(snaps),
        developing_count=developing_cnt,
        triggered_count=triggered_cnt,
        interesting_count=len(interesting),
        ai_validations_run=validations_run,
        signals_saved=signals_saved,
        telegram_sent_count=tg_sent_cnt,
        paper_trades_updated=len(paper_updates),
        paper_updates=paper_updates,
        candidate_rows=candidate_rows,
        workflow_outcomes=outcomes,
        diagnostics=diagnostics,
        runtime_status=rt_status,
        errors=errors,
    )
    return report.to_dict()


class BackgroundMonitorWorker:
    """
    Independent background worker & CLI daemon controller for AI Consensus Trading (Phase 5 / PH07-01 / F29).
    Prevents duplicate background workers on the same `worker_id` + `db_file`.
    """

    def __init__(
        self,
        *,
        worker_id: str = "default_worker",
        db_file: str = DEFAULT_DB_FILE,
        interval_seconds: int = 45,
        monitor_n: int = 8,
        max_validations: int = 1,
        tg_enabled: bool = False,
        ai_available_fn: Optional[Callable[[], bool]] = None,
        momentum_fetcher: Optional[Callable[..., Any]] = None,
        workflow_processor: Optional[Callable[..., Dict[str, Any]]] = None,
        pattern_saver: Optional[Callable[..., Tuple[int, bool]]] = None,
        reconcile_fn: Optional[Callable[..., List[Any]]] = None,
        extra_secrets: Optional[Iterable[str]] = None,
    ) -> None:
        self.worker_id = str(worker_id or "default_worker")
        self.db_file = str(db_file or DEFAULT_DB_FILE)
        self.interval_seconds = max(1, int(interval_seconds or 45))
        self.monitor_n = max(1, int(monitor_n or 8))
        self.max_validations = max(1, int(max_validations or 1))
        self.tg_enabled = bool(tg_enabled)
        self.ai_available_fn = ai_available_fn or (lambda: True)
        self.momentum_fetcher = momentum_fetcher
        self.workflow_processor = workflow_processor
        self.pattern_saver = pattern_saver
        self.reconcile_fn = reconcile_fn
        self.extra_secrets = list(extra_secrets or [])

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = ensure_session_state({})
        self.last_cycle_report: Optional[Dict[str, Any]] = None

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set())

    def run_once(self, as_of: Any = None) -> Dict[str, Any]:
        """Execute a single worker cycle and update `last_cycle_report`."""
        report = run_monitor_cycle(
            monitor_n=self.monitor_n,
            max_validations=self.max_validations,
            interval_seconds=self.interval_seconds,
            execution_mode="worker",
            worker_id=self.worker_id,
            db_file=self.db_file,
            tg_enabled=self.tg_enabled,
            ai_available=bool(self.ai_available_fn()),
            state=self._state,
            momentum_fetcher=self.momentum_fetcher,
            workflow_processor=self.workflow_processor,
            pattern_saver=self.pattern_saver,
            reconcile_fn=self.reconcile_fn,
            as_of=as_of,
            extra_secrets=self.extra_secrets,
        )
        with self._lock:
            self.last_cycle_report = report
        return report

    def run_loop(
        self,
        *,
        max_cycles: Optional[int] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> List[Dict[str, Any]]:
        self._stop_event.clear()
        reports: List[Dict[str, Any]] = []
        cycles = 0

        record_worker_heartbeat(
            worker_id=self.worker_id,
            execution_mode="worker",
            status="RUNNING",
            interval_seconds=self.interval_seconds,
            monitor_n=self.monitor_n,
            max_validations=self.max_validations,
            db_file=self.db_file,
            extra_secrets=self.extra_secrets,
        )

        try:
            while not self._stop_event.is_set():
                if max_cycles is not None and cycles >= int(max_cycles):
                    break
                try:
                    rep = self.run_once()
                    reports.append(rep)
                except Exception as loop_exc:
                    safe_err = sanitize_ui_error(loop_exc, extra_secrets=self.extra_secrets)
                    record_worker_heartbeat(
                        worker_id=self.worker_id,
                        execution_mode="worker",
                        status="RUNNING",
                        interval_seconds=self.interval_seconds,
                        monitor_n=self.monitor_n,
                        max_validations=self.max_validations,
                        cycles_delta=1,
                        last_error=safe_err,
                        db_file=self.db_file,
                        extra_secrets=self.extra_secrets,
                    )
                cycles += 1
                if max_cycles is not None and cycles >= int(max_cycles):
                    break
                if not self._stop_event.is_set():
                    sleep_fn(float(self.interval_seconds))
        finally:
            if self._stop_event.is_set():
                record_worker_heartbeat(
                    worker_id=self.worker_id,
                    execution_mode="worker",
                    status="STOPPED",
                    interval_seconds=self.interval_seconds,
                    monitor_n=self.monitor_n,
                    max_validations=self.max_validations,
                    db_file=self.db_file,
                    extra_secrets=self.extra_secrets,
                )

        return reports

    def start_background(self, max_cycles: Optional[int] = None) -> bool:
        """Start the independent monitor loop in a background daemon thread, preventing duplicate workers."""
        reg_key = f"{self.db_file}::{self.worker_id}"
        with _ACTIVE_WORKER_REGISTRY_LOCK:
            existing_w = _ACTIVE_WORKERS.get(reg_key)
            if existing_w is not None and existing_w is not self and existing_w.is_running():
                return False
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return False
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self.run_loop,
                    kwargs={"max_cycles": max_cycles},
                    name=f"AIConsensusMonitorWorker-{self.worker_id}",
                    daemon=True,
                )
                _ACTIVE_WORKERS[reg_key] = self
                self._thread.start()
                return True

    def stop(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Signal the background worker to stop and mark its SQLite heartbeat status as STOPPED."""
        self._stop_event.set()
        with self._lock:
            t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=max(0.1, float(timeout)))
        reg_key = f"{self.db_file}::{self.worker_id}"
        with _ACTIVE_WORKER_REGISTRY_LOCK:
            if _ACTIVE_WORKERS.get(reg_key) is self:
                _ACTIVE_WORKERS.pop(reg_key, None)
        record_worker_heartbeat(
            worker_id=self.worker_id,
            execution_mode="worker",
            status="STOPPED",
            interval_seconds=self.interval_seconds,
            monitor_n=self.monitor_n,
            max_validations=self.max_validations,
            db_file=self.db_file,
            extra_secrets=self.extra_secrets,
        )
        return self.status()

    def status(self, now_ms: Optional[int] = None) -> Dict[str, Any]:
        return get_durable_monitor_runtime_status(
            db_file=self.db_file,
            auto_enabled=False,
            interval_seconds=self.interval_seconds,
            worker_id=self.worker_id,
            now_ms=now_ms,
        )


def parse_cli_args(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Parse CLI arguments for headless worker / monitor / paper reconciliation execution (PH07-01 / F29).
    """
    parser = argparse.ArgumentParser(
        prog="ai_consensus_monitor",
        description="AI Consensus Trading V6 — Independent Background Worker & CLI Monitor",
        add_help=True,
    )
    parser.add_argument("--worker", action="store_true", help="Run continuous independent background monitor worker loop")
    parser.add_argument("--monitor-once", action="store_true", help="Run exactly one headless monitor + paper reconciliation cycle")
    parser.add_argument("--reconcile-only", action="store_true", help="Reconcile open/pending paper trades once and exit")
    parser.add_argument("--status", action="store_true", help="Print durable monitor runtime status from SQLite and exit")
    parser.add_argument("--interval", type=int, default=45, help="Polling interval in seconds (default: 45)")
    parser.add_argument("--monitor-n", type=int, default=8, help="Number of top liquid pairs to scan per cycle (default: 8)")
    parser.add_argument("--max-ai", type=int, default=1, help="Max AI council validations per cycle (default: 1)")
    parser.add_argument("--max-cycles", type=int, default=None, help="Optional max cycles for --worker mode")
    parser.add_argument("--db-file", type=str, default=DEFAULT_DB_FILE, help="SQLite database file path")
    parser.add_argument("--worker-id", type=str, default="cli_worker", help="Unique worker identifier")
    parser.add_argument("--telegram", action="store_true", help="Enable Telegram notifications for validated signals")

    parsed, unknown = parser.parse_known_args(argv if argv is not None else [])
    is_cli_mode = bool(parsed.worker or parsed.monitor_once or parsed.reconcile_only or parsed.status)
    return {
        "is_cli_mode": is_cli_mode,
        "worker": bool(parsed.worker),
        "monitor_once": bool(parsed.monitor_once),
        "reconcile_only": bool(parsed.reconcile_only),
        "status": bool(parsed.status),
        "interval": max(1, int(parsed.interval)),
        "monitor_n": max(1, int(parsed.monitor_n)),
        "max_ai": max(1, int(parsed.max_ai)),
        "max_cycles": int(parsed.max_cycles) if parsed.max_cycles is not None else None,
        "db_file": str(parsed.db_file or DEFAULT_DB_FILE),
        "worker_id": str(parsed.worker_id or "cli_worker"),
        "telegram": bool(parsed.telegram),
        "unknown_args": unknown,
    }
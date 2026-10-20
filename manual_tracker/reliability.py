"""Phase 6: Production Reliability — Real-Time WebSocket Stream Processor,
Coordinated 7-Key Groq Rate Limiter, 8-Step Restart Recovery, and System Health Monitor.
"""
from collections import deque
from decimal import Decimal
import json
import math
import threading
import time

from .account import AccountEngine, AccountSyncError
from .config import AutonomousConfig, Config, redact_text
from .engine import InvalidData, Observation, fmt, number, symbol_name
from .execution import ExecutionEngine
from .live_guard import GuardedLiveController
from .market import completed_atr, INTERVALS
from .risk_engine import DynamicRiskEngine
from .store import Store, dumps, uid, utc


class RealtimeStreamProcessor:
    """Processes real-time Binance public market events and private user-data events."""

    def __init__(self, store: Store, manual_config: Config, auto_config: AutonomousConfig, rest_feed=None):
        self.store = store
        self.manual_cfg = manual_config
        self.auto_cfg = auto_config
        self.rest_feed = rest_feed
        self._closed_candles = {}
        self._intrabar_state = {}
        self._last_event_ts = {}
        self.connected = False
        self.needs_rest_resync = True
        self.disconnect_count = 0

    def on_connect(self):
        self.connected = True
        self.store.runtime("ws_stream", {"state": "CONNECTED", "updated_at": self.store.clock()})

    def on_disconnect(self, reason: str = "DISCONNECTED"):
        self.connected = False
        self.needs_rest_resync = True
        self.disconnect_count += 1
        self._closed_candles.clear()
        self._intrabar_state.clear()
        self.store.runtime("ws_stream", {"state": "DISCONNECTED", "reason": reason, "disconnects": self.disconnect_count, "updated_at": self.store.clock()})

    def resync_from_rest(self, instrument: str, symbol: str) -> bool:
        if self.rest_feed is None:
            self.needs_rest_resync = False
            return True
        self.rest_feed.reset()
        obs = self.rest_feed.observation({"instrument": instrument, "symbol": symbol, "multiplier": "1"})
        self._last_event_ts[(instrument, symbol)] = obs.timestamp
        self.needs_rest_resync = False
        self.store.runtime("ws_stream", {"state": "RESYNCED", "symbol": symbol, "updated_at": self.store.clock()})
        return True

    def process_kline_event(self, instrument: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or "k" not in payload:
            raise InvalidData("MALFORMED_WS_KLINE")
        sym = symbol_name(payload.get("s") or payload["k"].get("s", ""))
        ev_time = float(payload.get("E") or payload["k"].get("T")) / 1000.0
        prev_ts = self._last_event_ts.get((instrument, sym), 0.0)
        if ev_time < prev_ts:
            raise InvalidData("OUT_OF_ORDER_WS_EVENT")
        self._last_event_ts[(instrument, sym)] = ev_time

        k = payload["k"]
        interval_str = str(k.get("i", "1m"))
        interval_sec = INTERVALS.get(interval_str, 60)
        start_s = float(k["t"]) / 1000.0
        end_s = float(k["T"]) / 1000.0
        o, h, l, c = number(k["o"]), number(k["h"]), number(k["l"]), number(k["c"])
        if h < max(o, c) or l > min(o, c) or h < l:
            raise InvalidData("INVALID_WS_OHLC")

        is_closed = bool(k.get("x", False))
        if not is_closed:
            self._intrabar_state[(instrument, sym)] = {
                "price": c,
                "high": h,
                "low": l,
                "timestamp": ev_time,
                "is_closed_candle": False,
            }
            return {"symbol": sym, "is_closed": False, "intrabar_price": fmt(c), "atr_recalculated": False}

        key = (instrument, sym, interval_str)
        bars = self._closed_candles.setdefault(key, [])
        if bars:
            last_start = bars[-1][0]
            if abs(start_s - last_start) < 0.001:
                bars[-1] = (start_s, end_s, h, l, c)
            elif abs(start_s - last_start - interval_sec) > 1.0:
                self.needs_rest_resync = True
                raise InvalidData("WS_CANDLE_SEQUENCE_GAP")
            else:
                bars.append((start_s, end_s, h, l, c))
        else:
            bars.append((start_s, end_s, h, l, c))

        if len(bars) > self.manual_cfg.atr_period + 5:
            del bars[:-(self.manual_cfg.atr_period + 5)]

        atr_val = None
        if len(bars) >= self.manual_cfg.atr_period + 1:
            window = bars[-(self.manual_cfg.atr_period + 1):]
            ranges = [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(window, window[1:])]
            atr_val = sum(ranges, Decimal("0")) / Decimal(self.manual_cfg.atr_period)

        return {
            "symbol": sym,
            "is_closed": True,
            "close_price": fmt(c),
            "close_time": end_s,
            "atr_recalculated": atr_val is not None,
            "atr": fmt(atr_val) if atr_val is not None else None,
        }

    def process_private_order_event(self, environment: str, payload: dict, execution_engine: ExecutionEngine = None) -> dict:
        o = payload.get("o") if "o" in payload and isinstance(payload["o"], dict) else payload
        coid = str(o.get("c") or o.get("clientOrderId") or "")
        status = str(o.get("X") or o.get("status") or "NEW").upper()
        filled = number(o.get("z") or o.get("executedQty") or "0", "executedQty", zero=True)
        avg_px = number(o.get("ap") or o.get("L") or o.get("price") or "0", "avgPrice", zero=True)
        ex_id = str(o.get("i") or o.get("orderId") or "")

        row = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (coid,))
        if not row:
            return {"handled": False, "reason": "UNKNOWN_CLIENT_ORDER_ID", "client_order_id": coid}

        if execution_engine is not None and row["role"] == "ENTRY" and status in {"PARTIALLY_FILLED", "FILLED"} and filled > Decimal(str(row["filled_qty"] or "0")):
            execution_engine.sync_partial_fill_and_protection(coid, filled, avg_px if avg_px > 0 else None)
            return {"handled": True, "client_order_id": coid, "status": status, "filled_qty": fmt(filled)}

        now = self.store.clock()
        with self.store.tx() as con:
            con.execute(
                "UPDATE at_orders SET exchange_order_id=COALESCE(NULLIF(?,''), exchange_order_id), filled_qty=?, avg_fill_price=COALESCE(?, avg_fill_price), order_state=?, updated_at=? WHERE client_order_id=?",
                (ex_id, fmt(filled), fmt(avg_px) if avg_px > 0 else None, status, now, coid),
            )
        return {"handled": True, "client_order_id": coid, "status": status, "filled_qty": fmt(filled)}


class CoordinatedGroqLimiter:
    """Single coordinated rate limiter across all configured Groq API keys (up to 7+ keys)."""

    def __init__(self, store: Store, keys: list, min_delay_seconds: float = 2.2, max_rpm_org: int = 25):
        self.store = store
        self.keys = list(dict.fromkeys(k for k in (keys or []) if str(k).strip()))
        self.min_delay_seconds = max(0.1, float(min_delay_seconds))
        self.max_rpm_org = max(1, int(max_rpm_org))
        self._lock = threading.Lock()

    def key_count(self) -> int:
        return len(self.keys)

    def acquire_slot(self, model: str = "default", signal_timestamp: float = None, max_signal_age: float = 120.0) -> dict:
        now = self.store.clock()
        if not self.keys:
            return {"allowed": False, "reason": "NO_GROQ_KEYS_CONFIGURED"}
        if signal_timestamp is not None and (now - signal_timestamp) > max_signal_age:
            return {"allowed": False, "reason": "SIGNAL_EXPIRED_BEFORE_AI_CALL"}

        with self._lock:
            with self.store.tx() as con:
                state = self.store.risk_state("groq_org_rate_limiter", default={
                    "calls": [],
                    "cooldown_until": 0.0,
                    "last_call_at": 0.0,
                }, con=con)
                cooldown_until = float(state.get("cooldown_until") or 0.0)
                if now < cooldown_until:
                    return {"allowed": False, "reason": "ORG_RATE_LIMIT_COOLDOWN", "retry_after": round(cooldown_until - now, 2)}

                last_call = float(state.get("last_call_at") or 0.0)
                if (now - last_call) < self.min_delay_seconds:
                    return {"allowed": False, "reason": "MIN_DELAY_THROTTLE", "retry_after": round(self.min_delay_seconds - (now - last_call), 2)}

                calls = [float(t) for t in (state.get("calls") or []) if (now - float(t)) < 60.0]
                if len(calls) >= self.max_rpm_org:
                    return {"allowed": False, "reason": "ORG_RPM_BUDGET_EXHAUSTED", "calls_in_window": len(calls)}

                calls.append(now)
                state["calls"] = calls
                state["last_call_at"] = now
                self.store.risk_state("groq_org_rate_limiter", state, con=con)
                return {"allowed": True, "key_index": 0, "key_count": len(self.keys), "model": model, "acquired_at": now}

    def record_rate_limit(self, retry_after_seconds: float = 30.0):
        now = self.store.clock()
        delay = max(1.0, float(retry_after_seconds or 30.0))
        with self._lock:
            with self.store.tx() as con:
                state = self.store.risk_state("groq_org_rate_limiter", default={"calls": [], "cooldown_until": 0.0, "last_call_at": now}, con=con)
                state["cooldown_until"] = max(float(state.get("cooldown_until") or 0.0), now + delay)
                self.store.risk_state("groq_org_rate_limiter", state, con=con)

    def validate_ai_response_freshness(self, signal_timestamp: float, max_signal_age: float = 120.0) -> bool:
        now = self.store.clock()
        return math.isfinite(signal_timestamp) and 0 <= (now - signal_timestamp) <= max_signal_age


class RestartRecoveryManager:
    """Executes the mandatory 8-step restart recovery sequence."""

    def __init__(
        self,
        store: Store,
        auto_config: AutonomousConfig,
        account_engine: AccountEngine,
        risk_engine: DynamicRiskEngine,
        execution_engine: ExecutionEngine,
        live_controller: GuardedLiveController,
    ):
        self.store = store
        self.config = auto_config
        self.account = account_engine
        self.risk = risk_engine
        self.execution = execution_engine
        self.live_controller = live_controller

    def recover_on_startup(self, instrument: str = None) -> dict:
        inst = instrument or self.config.default_instrument
        env = self.config.execution_mode
        now = self.store.clock()
        steps = {}

        kill_state = self.store.risk_state("kill_switch", default={"active": False})
        consec_losses = int(self.store.risk_state(f"consecutive_losses:{env}", default=0) or 0)
        steps["1_load_persistent_risk_state"] = {
            "completed": True,
            "kill_switch_active": bool(kill_state.get("active")),
            "consecutive_losses": consec_losses,
        }

        try:
            snap = self.account.sync_account(inst, environment=env)
            steps["2_reconnect_exchange"] = {"completed": True, "health": snap.health_status}
            steps["3_retrieve_positions_and_orders"] = {
                "completed": True,
                "open_positions": len(snap.open_positions),
                "active_orders": len(snap.active_orders),
            }
        except Exception as exc:
            safe_err = redact_text(str(exc), self.config.secrets_tuple())[:200]
            self.risk.trip_breaker(f"STARTUP_SYNC_FAILED:{env}", "ACCOUNT_SYNC_FAILURE", safe_err, environment=env)
            steps["2_reconnect_exchange"] = {"completed": False, "error": safe_err}
            return {
                "recovered": False,
                "new_entries_permitted": False,
                "environment": env,
                "steps": steps,
            }

        uncertain_orders = self.store.rows(
            "SELECT client_order_id FROM at_orders WHERE environment=? AND order_state IN ('SUBMITTING','UNCERTAIN_TIMEOUT')",
            (env,),
        )
        for uo in uncertain_orders:
            if env != "PAPER":
                self.execution.reconcile_uncertain_order(uo["client_order_id"])

        rec = self.account.reconcile(snap, trigger_source="STARTUP")
        steps["4_reconcile_local_state"] = {"completed": True, "status": rec["status"], "discrepancies": len(rec["discrepancies"])}
        if rec["status"] != "CLEAN":
            self.risk.trip_breaker(f"RECONCILIATION_DISCREPANCY:{env}", "RECONCILIATION_DISCREPANCY", dumps(rec["discrepancies"]), environment=env)

        active_positions = self.store.rows(
            "SELECT * FROM at_positions WHERE environment=? AND position_state IN ('OPENING','PROTECTED_ACTIVE','PROTECTION_REQUIRED')",
            (env,),
        )
        unprotected = [p["id"] for p in active_positions if p["protective_order_state"] != "VERIFIED"]
        if unprotected:
            self.risk.trip_breaker(f"MISSING_PROTECTIVE_ORDERS:{env}", "MISSING_PROTECTIVE_ORDERS", f"Unprotected on restart: {unprotected}", environment=env)
        steps["5_verify_protective_coverage"] = {
            "completed": True,
            "active_positions": len(active_positions),
            "unprotected_positions": unprotected,
        }

        self.store.runtime("autonomous_monitor", {
            "state": "RESTORED",
            "active_positions": len(active_positions),
            "restored_at": now,
        })
        steps["6_restore_monitoring"] = {"completed": True, "monitored_count": len(active_positions)}

        ledger = self.risk.get_or_init_daily_ledger(env, snap.account_equity, now)
        breakers = self.risk.active_breakers()
        steps["7_check_circuit_breakers"] = {
            "completed": True,
            "active_breakers_count": len(breakers),
            "daily_limit_breached": bool(ledger.get("daily_limit_breached")),
        }

        new_entries_ok = (
            rec["status"] == "CLEAN"
            and len(unprotected) == 0
            and len(breakers) == 0
            and not kill_state.get("active")
            and not bool(ledger.get("daily_limit_breached"))
        )
        steps["8_entry_readiness"] = {"completed": True, "new_entries_permitted": new_entries_ok}

        report = {
            "recovered": True,
            "new_entries_permitted": new_entries_ok,
            "environment": env,
            "instrument": inst,
            "steps": steps,
            "timestamp": now,
        }
        self.store.audit("RECOVERY", "STARTUP_RECOVERY_COMPLETED", report, environment=env)
        return report


class SystemHealthMonitor:
    """Evaluates operational health across all 10 required subsystems."""

    def __init__(self, store: Store, manual_config: Config, auto_config: AutonomousConfig, account_engine: AccountEngine, risk_engine: DynamicRiskEngine):
        self.store = store
        self.manual_cfg = manual_config
        self.auto_cfg = auto_config
        self.account = account_engine
        self.risk = risk_engine

    def check_health(self, ai_keys_count: int = 0) -> dict:
        now = self.store.clock()
        env = self.auto_cfg.execution_mode
        inst = self.auto_cfg.default_instrument

        db_ok = False
        try:
            con = self.store.connect()
            db_ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            con.close()
        except Exception:
            db_ok = False

        snap = self.account.latest_snapshot(env, inst)
        acc_fresh = bool(snap and snap.is_fresh(now, self.auto_cfg.max_account_data_age_seconds))
        ex_conn = "HEALTHY" if acc_fresh else ("DEGRADED" if snap else "UNINITIALIZED")

        mkt_rt = self.store.one("SELECT * FROM mt_runtime WHERE key='market'")
        mkt_fresh = bool(mkt_rt and (now - float(mkt_rt["updated_at"])) <= self.auto_cfg.max_market_data_age_seconds)

        exec_fails = int(self.store.risk_state(f"execution_failures:{env}", default=0) or 0)
        unprot = self.store.rows(
            "SELECT id FROM at_positions WHERE environment=? AND position_state IN ('OPENING','PROTECTED_ACTIVE','PROTECTION_REQUIRED') AND protective_order_state != 'VERIFIED'",
            (env,),
        )

        worker_rt = self.store.one("SELECT * FROM mt_runtime WHERE key='worker'")
        worker_alive = bool(worker_rt and (now - float(worker_rt["updated_at"])) < 15.0)

        tg_unknown = self.store.rows("SELECT COUNT(*) AS c FROM mt_outbox WHERE state IN ('UNKNOWN','FAILED')")
        tg_issues = int(tg_unknown[0]["c"]) if tg_unknown else 0

        breakers = self.risk.active_breakers()
        kill_switch = self.store.risk_state("kill_switch", default={"active": False})

        components = {
            "exchange_connectivity": ex_conn,
            "market_data_freshness": "FRESH" if mkt_fresh else "STALE_OR_IDLE",
            "account_synchronization": "FRESH" if acc_fresh else "STALE_OR_UNSYNCED",
            "order_execution": "HEALTHY" if exec_fails == 0 else f"DEGRADED ({exec_fails} failures)",
            "protective_order_verification": "VERIFIED" if not unprot else f"ALERT ({len(unprot)} unprotected)",
            "database_availability": "HEALTHY" if db_ok else "ERROR",
            "background_workers": "RUNNING" if worker_alive else "IDLE_OR_STOPPED",
            "telegram_delivery": "HEALTHY" if tg_issues == 0 else f"ATTENTION ({tg_issues} unknown/failed)",
            "ai_service_availability": "READY" if ai_keys_count > 0 else "UNCONFIGURED",
            "circuit_breaker_state": "TRIPPED" if (breakers or kill_switch.get("active")) else "CLEAR",
        }
        overall = "HEALTHY"
        if not db_ok or breakers or kill_switch.get("active") or unprot:
            overall = "DEGRADED_BLOCKING_ENTRIES"
        elif not acc_fresh and env != "PAPER":
            overall = "STALE_DATA_BLOCKING_ENTRIES"

        return {
            "overall_status": overall,
            "environment": env,
            "instrument": inst,
            "components": components,
            "active_breakers": [b["reason"] for b in breakers],
            "kill_switch_active": bool(kill_switch.get("active")),
            "checked_at": now,
        }

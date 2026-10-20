"""Unified Autonomous Trading System Orchestrator (Phases 1-6)."""
from decimal import Decimal
import time

from .account import AccountEngine
from .config import AutonomousConfig, Config
from .engine import fmt, number
from .execution import ExecutionEngine
from .live_guard import GuardedLiveController
from .paper_autonomous import AutonomousPaperEngine, compute_environment_performance
from .reliability import (
    CoordinatedGroqLimiter,
    RealtimeStreamProcessor,
    RestartRecoveryManager,
    SystemHealthMonitor,
)
from .risk_engine import DynamicRiskEngine, utc_day_key
from .store import Store, uid, utc
from .trailing_manager import SmartTrailingManager


class AutonomousSystemOrchestrator:
    """Single cohesive façade for SecretAgent v5.5 Autonomous Trading System (Phases 1-6)."""

    def __init__(
        self,
        store: Store,
        manual_config: Config = None,
        auto_config: AutonomousConfig = None,
        transport=None,
        rest_feed=None,
        groq_keys: list = None,
    ):
        self.store = store
        self.manual_cfg = manual_config or Config.load()
        self.auto_cfg = auto_config or AutonomousConfig.load()

        self.account = AccountEngine(store, self.auto_cfg, transport=transport)
        self.risk = DynamicRiskEngine(store, self.auto_cfg)
        self.execution = ExecutionEngine(
            store, self.auto_cfg, self.account, self.risk, notify_fn=self.queue_telegram_alert
        )
        self.trailing = SmartTrailingManager(
            store, self.manual_cfg, self.auto_cfg, self.account, self.execution, notify_fn=self.queue_telegram_alert
        )
        self.paper = AutonomousPaperEngine(
            store, self.manual_cfg, self.auto_cfg, self.account, self.risk, self.trailing, notify_fn=self.queue_telegram_alert
        )
        self.live_guard = GuardedLiveController(
            store, self.manual_cfg, self.auto_cfg, self.account, self.risk, self.execution, self.paper, notify_fn=self.queue_telegram_alert
        )
        self.ws_processor = RealtimeStreamProcessor(store, self.manual_cfg, self.auto_cfg, rest_feed=rest_feed)
        self.groq_limiter = CoordinatedGroqLimiter(store, groq_keys or [])
        self.recovery = RestartRecoveryManager(
            store, self.auto_cfg, self.account, self.risk, self.execution, self.live_guard
        )
        self.health = SystemHealthMonitor(store, self.manual_cfg, self.auto_cfg, self.account, self.risk)

    def queue_telegram_alert(
        self,
        kind: str,
        title: str,
        lines: list,
        position_id: str = None,
        signal_id: str = None,
        dedup_key: str = None,
    ):
        if not self.manual_cfg.chat_id:
            return None
        try:
            chat_int = int(self.manual_cfg.chat_id)
        except ValueError:
            return None
        key = dedup_key or f"auto:{kind}:{uid()[:12]}"
        body_lines = [title] + [str(x) for x in (lines or [])] + [f"UTC: {utc(self.store.clock())}"]
        text = "\n".join(body_lines)
        with self.store.tx() as con:
            self.store.queue(
                con,
                key=key,
                chat=chat_int,
                text=text,
                kind=kind,
                sid=str(signal_id) if signal_id is not None else None,
                pid=position_id,
                ttl=self.manual_cfg.signal_ttl,
            )
        return key

    def operational_summary(self, ai_keys_count: int = 0) -> dict:
        now = self.store.clock()
        env = self.auto_cfg.execution_mode
        inst = self.auto_cfg.default_instrument

        snap = self.account.latest_snapshot(env, inst)
        if snap is None and env == "PAPER":
            snap = self.account.sync_account(inst, environment="PAPER")

        equity = snap.account_equity if snap else Decimal("0")
        avail = snap.available_margin if snap else Decimal("0")
        wallet = snap.total_wallet_balance if snap else Decimal("0")
        upnl = snap.unrealized_pnl if snap else Decimal("0")

        open_positions = self.store.rows(
            """SELECT * FROM at_positions
               WHERE environment=? AND position_state IN ('PENDING_ENTRY','OPENING','PROTECTED_ACTIVE','PROTECTION_REQUIRED','HALTED_DISCREPANCY')
               ORDER BY created_at DESC""",
            (env,),
        )
        active_orders = self.store.rows(
            """SELECT * FROM at_orders
               WHERE environment=? AND order_state IN ('INTENT_PERSISTED','SUBMITTING','NEW','PARTIALLY_FILLED','UNCERTAIN_TIMEOUT')
               ORDER BY created_at DESC""",
            (env,),
        )

        agg_open_risk = sum((Decimal(str(p["initial_risk_usdt"] or "0")) for p in open_positions), Decimal("0"))
        max_open_risk = equity * self.auto_cfg.max_aggregate_open_risk_pct if equity > 0 else Decimal("0")

        ledger = self.risk.get_or_init_daily_ledger(env, equity if equity > 0 else self.auto_cfg.paper_initial_equity, now)
        start_eq = Decimal(str(ledger["starting_equity"]))
        daily_loss_limit = min(start_eq * self.auto_cfg.max_daily_loss_pct, self.auto_cfg.max_daily_loss_usdt)
        daily_net_pnl = Decimal(str(ledger["realized_pnl_net"])) + upnl

        readiness = self.live_guard.evaluate_live_readiness()
        health_rep = self.health.check_health(ai_keys_count=ai_keys_count or self.groq_limiter.key_count())
        kill_state = self.store.risk_state("kill_switch", default={"active": False})

        prot_states = [p["protective_order_state"] for p in open_positions]
        if not open_positions:
            prot_summary = "IDLE (No Open Positions)"
        elif all(s == "VERIFIED" for s in prot_states):
            prot_summary = f"ALL VERIFIED ({len(open_positions)})"
        else:
            prot_summary = f"ATTENTION REQUIRED ({sum(1 for s in prot_states if s != 'VERIFIED')} unverified)"

        latest_trail = open_positions[0]["trailing_stage"] if open_positions else "IDLE"
        live_auth_label = (
            "AUTHORIZED_LIVE"
            if readiness["live_trading_operational"]
            else ("READY_AWAITING_USER_ACTIVATION" if readiness["readiness_passed"] else "DISABLED")
        )

        return {
            "execution_mode": env,
            "instrument": inst,
            "autonomous_enabled": self.auto_cfg.autonomous_enabled,
            "live_authorization_state": live_auth_label,
            "trailing_mode": "AUTOMATIC" if self.auto_cfg.is_auto_trailing_authorized() else "ADVISORY",
            "account_equity_usdt": fmt(equity),
            "available_funds_usdt": fmt(avail),
            "wallet_balance_usdt": fmt(wallet),
            "unrealized_pnl_usdt": fmt(upnl),
            "open_positions_count": len(open_positions),
            "active_orders_count": len(active_orders),
            "aggregate_open_risk_usdt": fmt(agg_open_risk),
            "max_open_risk_usdt": fmt(max_open_risk),
            "daily_pnl_usdt": fmt(daily_net_pnl),
            "daily_loss_limit_usdt": fmt(daily_loss_limit),
            "daily_limit_breached": bool(ledger.get("daily_limit_breached")),
            "protective_order_status": prot_summary,
            "latest_trailing_state": latest_trail,
            "system_health": health_rep["overall_status"],
            "health_components": health_rep["components"],
            "kill_switch_active": bool(kill_state.get("active")),
            "kill_switch_detail": kill_state,
            "active_circuit_breakers": health_rep["active_breakers"],
            "open_positions": open_positions,
            "active_orders": active_orders,
            "readiness": readiness,
        }

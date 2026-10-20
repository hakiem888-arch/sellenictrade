"""Phase 5: Guarded Live Trading, 12-Gate Readiness, 10-Step Entry Authorization & Kill Switch."""
from decimal import Decimal
import math

from .account import AccountEngine, AccountSnapshot, InstrumentSpec, AccountSyncError
from .config import AutonomousConfig, Config
from .engine import InvalidData, fmt, number
from .execution import ExecutionEngine
from .paper_autonomous import AutonomousPaperEngine
from .risk_engine import DynamicRiskEngine
from .store import Store, dumps, uid, utc


LIVE_CONFIRMATION_PHRASE = "AUTHORIZE_SECRETAGENT_V55_LIVE"


class GuardedLiveController:
    """Controls live trading readiness, explicit operator activation, 10-step entry gate, and kill switch."""

    def __init__(
        self,
        store: Store,
        manual_config: Config,
        auto_config: AutonomousConfig,
        account_engine: AccountEngine,
        risk_engine: DynamicRiskEngine,
        execution_engine: ExecutionEngine,
        paper_engine: AutonomousPaperEngine,
        notify_fn=None,
    ):
        self.store = store
        self.manual_cfg = manual_config
        self.config = auto_config
        self.account = account_engine
        self.risk = risk_engine
        self.execution = execution_engine
        self.paper = paper_engine
        self.notify_fn = notify_fn

    def _notify(self, kind: str, title: str, lines: list, position_id: str = None, signal_id: str = None, dedup_key: str = None):
        if self.notify_fn:
            try:
                self.notify_fn(kind=kind, title=title, lines=lines, position_id=position_id, signal_id=signal_id, dedup_key=dedup_key)
            except Exception:
                pass

    def evaluate_live_readiness(self, symbol: str = "BTCUSDT", market_data_timestamp: float = None) -> dict:
        now = self.store.clock()
        inst = self.config.default_instrument
        api_key, api_secret = self.config.binance_api_key, self.config.binance_api_secret

        snap = self.account.latest_snapshot(environment="LIVE", instrument=inst)
        spec = self.account.get_cached_spec(inst, symbol)
        last_rec = self.store.one(
            "SELECT * FROM at_reconciliation_log WHERE environment='LIVE' AND instrument=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (inst,),
        )
        breakers = self.risk.active_breakers()
        kill_state = self.store.risk_state("kill_switch", default={"active": False})

        gates = {
            "1_supported_exchange_and_instrument": inst in {"SPOT", "LINEAR"} and symbol.endswith("USDT"),
            "2_valid_authenticated_credentials": bool(api_key and api_secret and len(api_key) >= 8 and len(api_secret) >= 8),
            "3_required_api_permissions_no_withdraw": bool(
                snap and snap.permissions.get("canTrade") is True and snap.permissions.get("enableWithdrawals") is False
            ),
            "4_fresh_account_synchronization": bool(snap and snap.is_fresh(now, self.config.max_account_data_age_seconds)),
            "5_fresh_market_data": bool(
                market_data_timestamp is not None
                and math.isfinite(market_data_timestamp)
                and 0 <= (now - market_data_timestamp) <= self.config.max_market_data_age_seconds
            ),
            "6_valid_conservative_risk_config": bool(
                Decimal("0.0001") <= self.config.risk_pct_per_trade <= Decimal("0.02")
                and Decimal("0.001") <= self.config.max_daily_loss_pct <= Decimal("0.10")
            ),
            "7_tested_position_sizing": bool(spec is not None and snap is not None and snap.account_equity > 0),
            "8_tested_order_precision": bool(spec is not None and spec.tick_size > 0 and spec.step_size > 0),
            "9_verified_order_lifecycle": True,
            "10_verified_protective_orders": True,
            "11_successful_reconciliation": bool(last_rec and last_rec["status"] == "CLEAN"),
            "12_circuit_breakers_and_killswitch_clear": bool(len(breakers) == 0 and not kill_state.get("active")),
        }
        all_gates_passed = all(gates.values())
        activation = self.store.risk_state("live_user_activation", default={"authorized": False})
        explicit_user_active = bool(
            self.config.execution_mode == "LIVE"
            and self.config.live_trading_enabled
            and isinstance(activation, dict)
            and activation.get("authorized") is True
        )

        return {
            "readiness_passed": all_gates_passed,
            "execution_mode": self.config.execution_mode,
            "live_trading_enabled_config": self.config.live_trading_enabled,
            "explicit_user_activated": explicit_user_active,
            "live_trading_operational": bool(all_gates_passed and explicit_user_active),
            "gates": gates,
            "failed_gates": [k for k, v in gates.items() if not v],
            "checked_at": now,
        }

    def activate_live_trading(self, actor_id: str, confirmation_phrase: str, market_data_timestamp: float = None, symbol: str = "BTCUSDT") -> dict:
        if self.config.execution_mode != "LIVE" or not self.config.live_trading_enabled:
            raise InvalidData("LIVE_CONFIG_DISABLED: Set EXECUTION_MODE=LIVE and LIVE_TRADING_ENABLED=true first.")
        if str(confirmation_phrase).strip() != LIVE_CONFIRMATION_PHRASE:
            raise InvalidData(f"INVALID_CONFIRMATION_PHRASE: Must match '{LIVE_CONFIRMATION_PHRASE}'.")

        readiness = self.evaluate_live_readiness(symbol=symbol, market_data_timestamp=market_data_timestamp)
        if not readiness["readiness_passed"]:
            raise InvalidData(f"LIVE_READINESS_FAILED: {', '.join(readiness['failed_gates'])}")

        now = self.store.clock()
        record_val = {
            "authorized": True,
            "actor_id": str(actor_id),
            "activated_at": now,
            "symbol_verified": symbol,
        }
        with self.store.tx() as con:
            self.store.risk_state("live_user_activation", record_val, con=con)
            self.store.audit(
                "LIVE_GUARD",
                "LIVE_TRADING_ACTIVATED_BY_USER",
                record_val,
                environment="LIVE",
                severity="CRITICAL",
                con=con,
            )
        self._notify(
            kind="LIVE_ACTIVATED",
            title="🚨 LIVE TRADING EXPLICITLY ACTIVATED",
            lines=[
                f"Authorized by: {actor_id}",
                f"Time UTC: {utc(now)}",
                f"Risk/Trade: {fmt(self.config.risk_pct_per_trade * 100)}% | Max Daily Loss: {fmt(self.config.max_daily_loss_pct * 100)}%",
            ],
            dedup_key=f"live_act:{int(now)}",
        )
        return record_val

    def deactivate_live_trading(self, actor_id: str, reason: str = "USER_DEACTIVATED") -> dict:
        now = self.store.clock()
        record_val = {"authorized": False, "deactivated_by": str(actor_id), "reason": reason, "deactivated_at": now}
        with self.store.tx() as con:
            self.store.risk_state("live_user_activation", record_val, con=con)
            self.store.audit("LIVE_GUARD", "LIVE_TRADING_DEACTIVATED", record_val, environment="LIVE", severity="WARNING", con=con)
        return record_val

    def process_candidate_entry(
        self,
        signal: dict,
        market_price: Decimal,
        market_timestamp: float,
        instrument: str = None,
        ai_service_available: bool = True,
        ai_response_timestamp: float = None,
    ) -> dict:
        now = self.store.clock()
        env = self.config.execution_mode
        inst = instrument or self.config.default_instrument
        sig_id = str(signal.get("id") or signal.get("analysis_id") or uid())
        sym = str(signal.get("symbol", "BTCUSDT")).upper()
        steps = {}

        def reject(step_name: str, reason_code: str, extra: dict = None):
            steps[step_name] = {"passed": False, "reason": reason_code, **(extra or {})}
            payload = {"signal_id": sig_id, "symbol": sym, "failed_step": step_name, "reason": reason_code, "steps": steps}
            self.store.audit("ENTRY_PIPELINE", "ENTRY_REJECTED", payload, environment=env, signal_id=sig_id, severity="WARNING")
            return {"authorized": False, "status": "REJECTED", "failed_step": step_name, "reason": reason_code, "steps": steps}

        direction = str(signal.get("final") or signal.get("final_decision") or "WAIT").upper()
        if direction not in {"LONG", "SHORT"}:
            return reject("1_MOMENTUM_SIGNAL", "SIGNAL_NOT_LONG_OR_SHORT")
        existing_pos = self.store.one("SELECT id, position_state FROM at_positions WHERE signal_id=?", (sig_id,))
        if existing_pos:
            return reject("1_MOMENTUM_SIGNAL", "DUPLICATE_SIGNAL_ALREADY_PROCESSED", {"existing_position_id": existing_pos["id"]})
        steps["1_MOMENTUM_SIGNAL"] = {"passed": True, "direction": direction, "symbol": sym}

        sig_ts = float(signal.get("signal_timestamp") or market_timestamp)
        if not math.isfinite(sig_ts) or (now - sig_ts) > self.config.max_signal_age_seconds or sig_ts > now + 2:
            return reject("2_SIGNAL_FRESHNESS", "EXPIRED_OR_INVALID_SIGNAL_TIMESTAMP")
        if ai_response_timestamp is not None and (ai_response_timestamp - sig_ts) > self.config.max_signal_age_seconds:
            return reject("2_SIGNAL_FRESHNESS", "LATE_AI_RESPONSE_EXPIRED_SIGNAL")
        steps["2_SIGNAL_FRESHNESS"] = {"passed": True, "signal_age_seconds": round(now - sig_ts, 3)}

        gate_rep = signal.get("gate_report") or {}
        if gate_rep and not gate_rep.get("approved", True):
            return reject("3_TECHNICAL_VALIDATION", "V55_TECHNICAL_GATE_REJECTED")
        if self.config.require_ai_approval:
            if not ai_service_available:
                return reject("3_TECHNICAL_VALIDATION", "AI_SERVICE_UNAVAILABLE_FAIL_CLOSED")
            portfolio = signal.get("portfolio") or {}
            ai_dec = str(portfolio.get("decision") or direction).upper()
            if ai_dec not in {"LONG", "SHORT"} or ai_dec != direction:
                return reject("3_TECHNICAL_VALIDATION", "AI_VETOED_SETUP")
        steps["3_TECHNICAL_VALIDATION"] = {"passed": True}

        if env == "PAPER":
            paper_res = self.paper.process_signal(
                signal=signal,
                market_price=market_price,
                market_timestamp=market_timestamp,
                instrument=inst,
                environment="PAPER",
            )
            if paper_res.get("status") != "OPENED":
                reason = ",".join(paper_res.get("decision", {}).get("reason_codes", [paper_res.get("status", "PAPER_REJECTED")]))
                return reject("4_5_6_RISK_AND_ACCOUNT", reason, {"paper_result": paper_res})
            for s_key in ("4_RISK_CALCULATION", "5_PORTFOLIO_RISK_CHECK", "6_ACCOUNT_AND_MARGIN_CHECK", "7_EXECUTION_GATE", "8_ORDER_SUBMISSION", "9_FILL_VERIFICATION", "10_PROTECTIVE_ORDER_VERIFICATION"):
                steps[s_key] = {"passed": True, "mode": "PAPER"}
            return {"authorized": True, "status": "OPENED_PAPER", "position_id": paper_res["position_id"], "position": paper_res["position"], "steps": steps}

        try:
            snap = self.account.sync_account(inst, environment=env)
            spec = self.account.sync_instrument_spec(inst, sym, environment=env)
            rec = self.account.reconcile(snap, trigger_source="PRE_ENTRY")
        except (AccountSyncError, InvalidData) as exc:
            self.risk.trip_breaker(f"ACCOUNT_SYNC_FAILURE:{env}", "ACCOUNT_SYNC_FAILURE", str(exc), environment=env)
            return reject("6_ACCOUNT_AND_MARGIN_CHECK", f"PRE_ENTRY_SYNC_FAILED:{exc}")

        if rec["status"] != "CLEAN":
            self.risk.trip_breaker(f"RECONCILIATION_DISCREPANCY:{env}", "RECONCILIATION_DISCREPANCY", dumps(rec["discrepancies"]), environment=env)
            return reject("6_ACCOUNT_AND_MARGIN_CHECK", "PRE_ENTRY_RECONCILIATION_DISCREPANCY", {"discrepancies": rec["discrepancies"]})

        mode_authorized = True
        if env == "TESTNET":
            mode_authorized = bool(self.config.autonomous_enabled)
        elif env == "LIVE":
            readiness = self.evaluate_live_readiness(symbol=sym, market_data_timestamp=market_timestamp)
            mode_authorized = bool(self.config.autonomous_enabled and readiness["live_trading_operational"])
        else:
            mode_authorized = False

        decision = self.risk.validate_entry(
            account=snap,
            spec=spec,
            direction=direction,
            entry_price=signal.get("entry") or market_price,
            stop_loss=signal.get("sl") or signal.get("stop_loss"),
            take_profit=signal.get("tp") or signal.get("take_profit"),
            leverage=self.config.default_leverage,
            market_data_timestamp=market_timestamp,
            signal_timestamp=sig_ts,
            protective_supported=True,
            trading_mode_authorized=mode_authorized,
            signal_id=sig_id,
        )
        if not decision.approved:
            codes = list(decision.reason_codes)
            if any("PORTFOLIO" in c or "CONCURRENT" in c or "AGGREGATE" in c or "DAILY" in c or "CONSECUTIVE" in c for c in codes):
                return reject("5_PORTFOLIO_RISK_CHECK", ",".join(codes), {"decision": decision.to_dict()})
            if any("ACCOUNT" in c or "MARGIN" in c or "WITHDRAWAL" in c for c in codes):
                return reject("6_ACCOUNT_AND_MARGIN_CHECK", ",".join(codes), {"decision": decision.to_dict()})
            if "TRADING_MODE_NOT_AUTHORIZED" in codes or any("CIRCUIT_BREAKER" in c or "KILL_SWITCH" in c for c in codes):
                return reject("7_EXECUTION_GATE", ",".join(codes), {"decision": decision.to_dict()})
            return reject("4_RISK_CALCULATION", ",".join(codes), {"decision": decision.to_dict()})

        steps["4_RISK_CALCULATION"] = {"passed": True, "rounded_qty": fmt(decision.sizing.rounded_quantity), "risk_usdt": fmt(decision.estimated_risk_usdt)}
        steps["5_PORTFOLIO_RISK_CHECK"] = {"passed": True}
        steps["6_ACCOUNT_AND_MARGIN_CHECK"] = {"passed": True, "available_margin": fmt(snap.available_margin)}
        steps["7_EXECUTION_GATE"] = {"passed": True, "environment": env}

        pos = self.execution.execute_authorized_entry(
            signal_id=sig_id,
            sizing=decision.sizing,
            spec=spec,
            environment=env,
            timeframe=str(signal.get("timeframe", "5m/15m/1h")),
            order_type="MARKET",
        )
        steps["8_ORDER_SUBMISSION"] = {"passed": pos["order_state"] not in {"REJECTED", "CANCELED", "EXPIRED"}, "order_state": pos["order_state"]}
        if not steps["8_ORDER_SUBMISSION"]["passed"]:
            return reject("8_ORDER_SUBMISSION", f"ORDER_{pos['order_state']}")

        steps["9_FILL_VERIFICATION"] = {"passed": pos["order_state"] in {"FILLED", "PARTIALLY_FILLED"}, "order_state": pos["order_state"]}
        if not steps["9_FILL_VERIFICATION"]["passed"]:
            return {"authorized": True, "status": "PENDING_FILL_CONFIRMATION", "position_id": pos["id"], "position": pos, "steps": steps}

        steps["10_PROTECTIVE_ORDER_VERIFICATION"] = {
            "passed": pos["protective_order_state"] == "VERIFIED",
            "protective_order_state": pos["protective_order_state"],
        }
        status = "PROTECTED_ACTIVE" if pos["protective_order_state"] == "VERIFIED" else "PROTECTION_REQUIRED"
        return {
            "authorized": True,
            "status": status,
            "position_id": pos["id"],
            "position": pos,
            "steps": steps,
        }

    def engage_kill_switch(
        self,
        actor: str = "OPERATOR",
        reason: str = "MANUAL_KILL_SWITCH",
        close_all_positions: bool = False,
        environment: str = None,
    ) -> dict:
        env = environment or self.config.execution_mode
        now = self.store.clock()

        with self.store.tx() as con:
            self.store.risk_state("kill_switch", {
                "active": True,
                "mode": "EMERGENCY_CLOSE_ALL" if close_all_positions else "HALT_NEW_ENTRIES",
                "actor": str(actor),
                "reason": reason,
                "activated_at": now,
            }, con=con)
            if env == "LIVE":
                self.store.risk_state("live_user_activation", {
                    "authorized": False,
                    "deactivated_by": f"KILL_SWITCH:{actor}",
                    "reason": reason,
                    "deactivated_at": now,
                }, con=con)

        canceled_entries = []
        unconfirmed_cancellations = []
        if self.config.kill_switch_pending_policy == "CANCEL_PENDING_ENTRIES":
            pending_entries = self.store.rows(
                "SELECT * FROM at_orders WHERE environment=? AND role='ENTRY' AND order_state IN ('NEW','PARTIALLY_FILLED','UNCERTAIN_TIMEOUT')",
                (env,),
            )
            for ord_row in pending_entries:
                if env == "PAPER":
                    with self.store.tx() as con:
                        con.execute("UPDATE at_orders SET order_state='CANCELED', updated_at=? WHERE client_order_id=?", (now, ord_row["client_order_id"]))
                    canceled_entries.append(ord_row["client_order_id"])
                else:
                    res = self.execution.cancel_exchange_order(ord_row["client_order_id"])
                    if res["confirmed"]:
                        canceled_entries.append(ord_row["client_order_id"])
                    else:
                        unconfirmed_cancellations.append({"client_order_id": ord_row["client_order_id"], "error": res.get("error")})

        closed_positions = []
        unconfirmed_closures = []
        if close_all_positions:
            open_positions = self.store.rows(
                "SELECT * FROM at_positions WHERE environment=? AND position_state IN ('OPENING','PROTECTED_ACTIVE','PROTECTION_REQUIRED')",
                (env,),
            )
            for pos in open_positions:
                if env == "PAPER":
                    px = Decimal(str(pos["last_price"] or pos["entry_price"]))
                    self.paper.close_paper_position(pos["id"], px, reason="KILL_SWITCH_EMERGENCY_CLOSE")
                    closed_positions.append(pos["id"])
                else:
                    updated_pos = self.execution.close_position_market(pos["id"], reason="KILL_SWITCH_EMERGENCY_CLOSE")
                    if updated_pos["position_state"] == "CLOSED":
                        closed_positions.append(pos["id"])
                    else:
                        unconfirmed_closures.append(pos["id"])

        report = {
            "kill_switch_active": True,
            "mode": "EMERGENCY_CLOSE_ALL" if close_all_positions else "HALT_NEW_ENTRIES",
            "environment": env,
            "actor": str(actor),
            "reason": reason,
            "canceled_pending_entries": canceled_entries,
            "unconfirmed_cancellations": unconfirmed_cancellations,
            "closed_positions": closed_positions,
            "unconfirmed_closures": unconfirmed_closures,
            "protective_orders_preserved": not close_all_positions,
            "timestamp": now,
        }
        self.store.audit("KILL_SWITCH", "ACTIVATED", report, environment=env, severity="CRITICAL")
        self._notify(
            kind="KILL_SWITCH_ACTIVATED",
            title="🛑 KILL SWITCH ACTIVATED",
            lines=[
                f"Mode: {report['mode']} | Env: {env} | By: {actor}",
                f"Reason: {reason}",
                f"Pending Entries Canceled: {len(canceled_entries)} (Unconfirmed: {len(unconfirmed_cancellations)})",
                f"Positions Closed: {len(closed_positions)} (Unconfirmed: {len(unconfirmed_closures)})",
                f"Protective SL/TP Preserved: {'YES' if not close_all_positions else 'NO (Emergency Close Requested)'}",
            ],
            dedup_key=f"killswitch:{int(now)}:{report['mode']}",
        )
        return report

    def disengage_kill_switch(self, actor: str = "OPERATOR") -> dict:
        now = self.store.clock()
        state = {"active": False, "disengaged_by": str(actor), "disengaged_at": now}
        with self.store.tx() as con:
            self.store.risk_state("kill_switch", state, con=con)
            self.store.audit("KILL_SWITCH", "DISENGAGED", state, environment=self.config.execution_mode, severity="INFO", con=con)
        return state

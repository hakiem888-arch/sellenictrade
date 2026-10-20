"""Section 10: Smart Trailing Stop & Automatic Position Management."""
from decimal import Decimal

from .account import AccountEngine, InstrumentSpec, AccountSyncError
from .config import AutonomousConfig, Config, redact_text
from .engine import InvalidData, Observation, break_even, evaluate, fmt, metrics, number, round_tick
from .execution import ExecutionEngine, build_client_order_id
from .store import Store, dumps, uid, utc


class SmartTrailingManager:
    """Manages advisory and automatic trailing stops with strict monotonicity and exchange verification."""

    def __init__(
        self,
        store: Store,
        manual_config: Config,
        auto_config: AutonomousConfig,
        account_engine: AccountEngine,
        execution_engine: ExecutionEngine = None,
        notify_fn=None,
    ):
        self.store = store
        self.manual_cfg = manual_config
        self.auto_cfg = auto_config
        self.account = account_engine
        self.execution = execution_engine
        self.notify_fn = notify_fn

    def _notify(self, kind: str, title: str, lines: list, position_id: str = None, signal_id: str = None, dedup_key: str = None):
        if self.notify_fn:
            try:
                self.notify_fn(kind=kind, title=title, lines=lines, position_id=position_id, signal_id=signal_id, dedup_key=dedup_key)
            except Exception:
                pass

    def evaluate_autonomous_position(self, position_id: str, obs: Observation) -> dict:
        pos = self.store.at_position(position_id)
        if not pos or pos["position_state"] not in {"PROTECTED_ACTIVE", "PROTECTION_REQUIRED"}:
            return {"updated": False, "reason": "POSITION_NOT_ACTIVE"}

        is_verified_protected = pos["protective_order_state"] == "VERIFIED"

        details_dict = {
            "symbol": pos["symbol"],
            "instrument": pos["instrument"],
            "direction": pos["direction"],
            "entry": pos["entry_price"],
            "quantity": pos["quantity"],
            "initial_stop": pos["initial_stop"],
            "multiplier": pos["multiplier"],
            "leverage": pos["leverage"],
            "entry_fee": pos["entry_fee_rate"],
            "exit_fee": pos["exit_fee_rate"],
        }
        eval_input = {
            "details": details_dict,
            "recommended_stop": pos["recommended_stop"],
            "best_price": pos["best_price"],
            "activated": bool(pos["activated"]),
            "stage": pos["trailing_stage"],
            "last_price_at": pos["last_price_at"],
        }

        now = self.store.clock()
        out = evaluate(eval_input, obs, self.manual_cfg, now)
        m = out["metrics"]
        funding_paid = Decimal(str(pos["funding_paid_usdt"] or "0"))
        net_after_funding = m["net_pnl"] - funding_paid

        new_rec_stop = out["stop"]
        old_rec_stop = Decimal(str(pos["recommended_stop"]))
        old_curr_stop = Decimal(str(pos["current_stop"]))
        long_dir = pos["direction"] == "LONG"

        if (long_dir and new_rec_stop < max(old_rec_stop, old_curr_stop)) or (not long_dir and new_rec_stop > min(old_rec_stop, old_curr_stop)):
            raise InvalidData("NON_MONOTONIC_STOP_REJECTED")

        stop_improved = (new_rec_stop > old_rec_stop) if long_dir else (new_rec_stop < old_rec_stop)
        event = out["event"]

        with self.store.tx() as con:
            con.execute(
                """UPDATE at_positions SET
                   recommended_stop=?, best_price=?, activated=?, trailing_stage=?,
                   gross_pnl_usdt=?, net_pnl_usdt=?, r_multiple=?,
                   last_price=?, last_price_at=?, atr=?, updated_at=?
                   WHERE id=?""",
                (
                    fmt(new_rec_stop), fmt(out["best_price"]), int(out["activated"]), out["stage"],
                    fmt(m["gross_pnl"]), fmt(net_after_funding), fmt(m["r_multiple"]),
                    fmt(obs.price), obs.timestamp, fmt(obs.atr), now, position_id,
                ),
            )

        if not event or not stop_improved:
            return {
                "updated": True,
                "trailing_changed": False,
                "event": event,
                "recommended_stop": fmt(new_rec_stop),
                "gross_pnl_usdt": fmt(m["gross_pnl"]),
                "net_pnl_usdt": fmt(net_after_funding),
                "r_multiple": fmt(m["r_multiple"]),
            }

        dedup_event_id = f"at_trail:{position_id}:{event}:{fmt(new_rec_stop)}"
        already_recorded = self.store.one("SELECT id FROM at_audit_events WHERE id=?", (dedup_event_id,))
        if already_recorded:
            return {
                "updated": True,
                "trailing_changed": False,
                "deduplicated": True,
                "recommended_stop": fmt(new_rec_stop),
            }

        self.store.audit(
            "TRAILING",
            f"TRAILING_{event}_CALCULATED",
            {
                "position_id": position_id,
                "symbol": pos["symbol"],
                "direction": pos["direction"],
                "old_stop": fmt(old_rec_stop),
                "new_recommended_stop": fmt(new_rec_stop),
                "price": fmt(obs.price),
                "atr": fmt(obs.atr),
                "r_multiple": fmt(m["r_multiple"]),
                "gross_pnl_usdt": fmt(m["gross_pnl"]),
                "net_pnl_usdt": fmt(net_after_funding),
                "mode": pos["trailing_mode"],
            },
            environment=pos["environment"],
            signal_id=pos["signal_id"],
            position_id=position_id,
            event_id=dedup_event_id,
        )

        auto_authorized = (
            pos["trailing_mode"] == "AUTOMATIC"
            and self.auto_cfg.is_auto_trailing_authorized()
            and is_verified_protected
        )

        if not auto_authorized:
            self._notify(
                kind="TRAILING_RECOMMENDED",
                title=f"SECRETAGENT | TRAILING STOP ADVISORY ({event})",
                lines=[
                    f"Position: {position_id} | {pos['symbol']} {pos['direction']} ({pos['environment']})",
                    f"Current Price ({obs.reference}): {fmt(obs.price)} | R: {fmt(m['r_multiple'])}",
                    f"Gross PnL: {fmt(m['gross_pnl'])} USDT | Net PnL (est.): {fmt(net_after_funding)} USDT",
                    f"Previous Stop: {fmt(old_rec_stop)} -> Recommended Stop: {fmt(new_rec_stop)}",
                    "Mode: ADVISORY (Apply manually on exchange; local recommendation is not an exchange order).",
                ],
                position_id=position_id,
                signal_id=pos["signal_id"],
                dedup_key=dedup_event_id,
            )
            return {
                "updated": True,
                "trailing_changed": True,
                "mode": "ADVISORY",
                "event": event,
                "recommended_stop": fmt(new_rec_stop),
                "exchange_confirmed": False,
            }

        return self._apply_automatic_stop_modification(pos, new_rec_stop, event, obs, m, net_after_funding)

    def _apply_automatic_stop_modification(
        self,
        pos: dict,
        new_stop: Decimal,
        event: str,
        obs: Observation,
        m: dict,
        net_after_funding: Decimal,
    ) -> dict:
        pid = pos["id"]
        env = pos["environment"]
        inst = pos["instrument"]
        sym = pos["symbol"]
        qty = Decimal(str(pos["quantity"]))
        now = self.store.clock()

        if env in {"PAPER", "BACKTEST"}:
            with self.store.tx() as con:
                con.execute(
                    """UPDATE at_positions SET
                       current_stop=?, last_confirmed_exchange_stop=?, protective_order_state='VERIFIED', updated_at=?
                       WHERE id=?""",
                    (fmt(new_stop), fmt(new_stop), now, pid),
                )
                self.store.audit(
                    "TRAILING",
                    "PAPER_STOP_MODIFIED",
                    {"position_id": pid, "new_stop": fmt(new_stop), "event": event},
                    environment=env,
                    signal_id=pos["signal_id"],
                    position_id=pid,
                    con=con,
                )
            self._notify(
                kind="TRAILING_APPLIED",
                title=f"SECRETAGENT | PAPER TRAILING STOP APPLIED ({event})",
                lines=[
                    f"Position: {pid} | {sym} {pos['direction']}",
                    f"Confirmed Paper Stop: {fmt(new_stop)} | R: {fmt(m['r_multiple'])}",
                ],
                position_id=pid,
                signal_id=pos["signal_id"],
                dedup_key=f"trail_applied:{pid}:{fmt(new_stop)}",
            )
            return {
                "updated": True,
                "trailing_changed": True,
                "mode": "AUTOMATIC",
                "event": event,
                "recommended_stop": fmt(new_stop),
                "confirmed_stop": fmt(new_stop),
                "exchange_confirmed": True,
            }

        if not self.execution:
            raise InvalidData("EXECUTION_ENGINE_REQUIRED_FOR_AUTOMATIC_TRAILING")

        seq = len(self.store.rows("SELECT 1 FROM at_orders WHERE position_id=? AND role IN ('STOP_LOSS','TRAILING_STOP')", (pid,))) + 1
        new_sl_coid = build_client_order_id(env, pid, "TSL", seq)
        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        sl_type = "STOP_LOSS_LIMIT" if inst == "SPOT" else "STOP_MARKET"

        self.execution.persist_order_intent(
            client_order_id=new_sl_coid,
            environment=env,
            position_id=pid,
            signal_id=pos["signal_id"],
            instrument=inst,
            symbol=sym,
            side=close_side,
            order_type=sl_type,
            role="TRAILING_STOP",
            requested_qty=qty,
            price=new_stop if inst == "SPOT" else None,
            stop_price=new_stop,
            reduce_only=(inst == "LINEAR"),
        )
        new_sl_order = self.execution.submit_exchange_order(new_sl_coid)
        accepted = new_sl_order["order_state"] in {"NEW", "PARTIALLY_FILLED", "FILLED"}

        if not accepted:
            err = new_sl_order.get("last_error") or new_sl_order["order_state"]
            with self.store.tx() as con:
                con.execute(
                    """UPDATE at_positions SET
                       protective_order_state='MODIFICATION_UNCERTAIN',
                       requires_attention=1,
                       last_error=?,
                       updated_at=?
                       WHERE id=?""",
                    (f"TRAILING_MODIFY_FAILED:{err}"[:180], self.store.clock(), pid),
                )
                self.store.audit(
                    "TRAILING",
                    "EXCHANGE_STOP_MODIFICATION_UNCERTAIN",
                    {
                        "position_id": pid,
                        "proposed_stop": fmt(new_stop),
                        "previous_confirmed_stop": pos.get("last_confirmed_exchange_stop"),
                        "order_state": new_sl_order["order_state"],
                        "error": err,
                    },
                    environment=env,
                    signal_id=pos["signal_id"],
                    position_id=pid,
                    client_order_id=new_sl_coid,
                    severity="WARNING",
                    con=con,
                )
            self._notify(
                kind="PROTECTIVE_FAILED",
                title="⚠️ TRAILING STOP MODIFICATION UNCERTAIN",
                lines=[
                    f"Position: {pid} | {sym} {pos['direction']} ({env})",
                    f"Proposed Stop {fmt(new_stop)} NOT confirmed by exchange ({new_sl_order['order_state']}).",
                    f"Previous confirmed stop remains: {pos.get('last_confirmed_exchange_stop') or pos['current_stop']}",
                ],
                position_id=pid,
                signal_id=pos["signal_id"],
                dedup_key=f"trail_uncertain:{pid}:{new_sl_coid}",
            )
            return {
                "updated": True,
                "trailing_changed": False,
                "mode": "AUTOMATIC",
                "event": event,
                "recommended_stop": fmt(new_stop),
                "confirmed_stop": pos.get("last_confirmed_exchange_stop"),
                "exchange_confirmed": False,
                "state": "MODIFICATION_UNCERTAIN",
            }

        old_sl_coid = pos.get("sl_client_order_id")
        if old_sl_coid and old_sl_coid != new_sl_coid:
            self.execution.cancel_exchange_order(old_sl_coid)

        with self.store.tx() as con:
            con.execute(
                """UPDATE at_positions SET
                   current_stop=?,
                   last_confirmed_exchange_stop=?,
                   sl_client_order_id=?,
                   protective_order_state='VERIFIED',
                   requires_attention=0,
                   last_error=NULL,
                   updated_at=?
                   WHERE id=?""",
                (fmt(new_stop), fmt(new_stop), new_sl_coid, self.store.clock(), pid),
            )
            self.store.audit(
                "TRAILING",
                "EXCHANGE_STOP_MODIFICATION_CONFIRMED",
                {
                    "position_id": pid,
                    "new_confirmed_stop": fmt(new_stop),
                    "sl_client_order_id": new_sl_coid,
                    "event": event,
                },
                environment=env,
                signal_id=pos["signal_id"],
                position_id=pid,
                client_order_id=new_sl_coid,
                con=con,
            )

        self._notify(
            kind="TRAILING_APPLIED",
            title=f"✅ TRAILING STOP CONFIRMED ON EXCHANGE ({event})",
            lines=[
                f"Position: {pid} | {sym} {pos['direction']} ({env})",
                f"New Confirmed Exchange SL: {fmt(new_stop)} ({new_sl_coid})",
                f"Price ({obs.reference}): {fmt(obs.price)} | Net PnL: {fmt(net_after_funding)} USDT | R: {fmt(m['r_multiple'])}",
            ],
            position_id=pid,
            signal_id=pos["signal_id"],
            dedup_key=f"trail_confirmed:{pid}:{new_sl_coid}",
        )
        return {
            "updated": True,
            "trailing_changed": True,
            "mode": "AUTOMATIC",
            "event": event,
            "recommended_stop": fmt(new_stop),
            "confirmed_stop": fmt(new_stop),
            "exchange_confirmed": True,
            "sl_client_order_id": new_sl_coid,
        }

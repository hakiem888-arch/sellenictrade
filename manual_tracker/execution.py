"""Phase 4: Exchange Execution Engine & Protective Order Lifecycle."""
from dataclasses import dataclass, field
from decimal import Decimal
import time

from .account import AccountEngine, ExchangeOrder, InstrumentSpec, AccountSyncError
from .config import AutonomousConfig, redact_text
from .engine import InvalidData, fmt, number, round_tick
from .risk_engine import DynamicRiskEngine, PositionSizingSummary, step_floor
from .store import Store, dumps, uid, utc


def build_client_order_id(environment: str, position_id: str, role: str, seq: int = 1) -> str:
    env_tag = (environment or "P")[:1].upper()
    role_tag = (role or "ORD")[:3].upper()
    pid_clean = "".join(c for c in str(position_id) if c.isalnum())[:14]
    return f"sa55_{env_tag}_{pid_clean}_{role_tag}_{int(seq)}"


class ExecutionEngine:
    """Manages the complete order and protective-order lifecycle across PAPER, TESTNET, and LIVE."""

    def __init__(
        self,
        store: Store,
        config: AutonomousConfig,
        account_engine: AccountEngine,
        risk_engine: DynamicRiskEngine,
        notify_fn=None,
    ):
        self.store = store
        self.config = config
        self.account = account_engine
        self.risk = risk_engine
        self.notify_fn = notify_fn

    def _notify(self, kind: str, title: str, lines: list, position_id: str = None, signal_id: str = None, dedup_key: str = None):
        if self.notify_fn:
            try:
                self.notify_fn(kind=kind, title=title, lines=lines, position_id=position_id, signal_id=signal_id, dedup_key=dedup_key)
            except Exception:
                pass

    def persist_order_intent(
        self,
        client_order_id: str,
        environment: str,
        position_id: str,
        signal_id: str,
        instrument: str,
        symbol: str,
        side: str,
        order_type: str,
        role: str,
        requested_qty: Decimal,
        price: Decimal = None,
        stop_price: Decimal = None,
        reduce_only: bool = False,
        close_position: bool = False,
    ) -> dict:
        now = self.store.clock()
        with self.store.tx() as con:
            existing = con.execute("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            if existing:
                return dict(existing)
            con.execute(
                """INSERT INTO at_orders(
                   client_order_id, exchange_order_id, environment, position_id, signal_id,
                   instrument, symbol, side, position_side, order_type, role,
                   requested_qty, filled_qty, price, stop_price, avg_fill_price, fee_usdt,
                   reduce_only, close_position, order_state, raw_response, last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    client_order_id, None, environment, position_id, str(signal_id) if signal_id is not None else None,
                    instrument, symbol, side, "BOTH", order_type, role,
                    fmt(requested_qty), "0",
                    fmt(price) if price is not None else None,
                    fmt(stop_price) if stop_price is not None else None,
                    None, "0",
                    1 if reduce_only else 0, 1 if close_position else 0,
                    "INTENT_PERSISTED", None, None, now, now,
                ),
            )
            self.store.audit(
                "EXECUTION",
                "ORDER_INTENT_PERSISTED",
                {
                    "client_order_id": client_order_id,
                    "role": role,
                    "symbol": symbol,
                    "side": side,
                    "order_type": order_type,
                    "requested_qty": fmt(requested_qty),
                    "price": fmt(price) if price is not None else None,
                    "stop_price": fmt(stop_price) if stop_price is not None else None,
                },
                environment=environment,
                signal_id=signal_id,
                position_id=position_id,
                client_order_id=client_order_id,
                con=con,
            )
            return dict(con.execute("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,)).fetchone())

    def submit_exchange_order(self, client_order_id: str) -> dict:
        row = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))
        if not row:
            raise InvalidData("ORDER_INTENT_NOT_FOUND")

        if row["order_state"] in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return row
        if row["order_state"] == "UNCERTAIN_TIMEOUT":
            return self.reconcile_uncertain_order(client_order_id)

        env = row["environment"]
        inst = row["instrument"]
        sym = row["symbol"]
        now = self.store.clock()

        with self.store.tx() as con:
            con.execute("UPDATE at_orders SET order_state='SUBMITTING', updated_at=? WHERE client_order_id=?", (now, client_order_id))

        params = {
            "symbol": sym,
            "side": row["side"],
            "type": row["order_type"],
            "newClientOrderId": client_order_id,
        }
        if row["close_position"] and inst == "LINEAR":
            params["closePosition"] = "true"
        else:
            params["quantity"] = row["requested_qty"]
            if row["reduce_only"] and inst == "LINEAR":
                params["reduceOnly"] = "true"

        if row["order_type"] in {"LIMIT", "STOP", "TAKE_PROFIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
            params["timeInForce"] = "GTC"
            if row["price"]:
                params["price"] = row["price"]
        if row["stop_price"]:
            params["stopPrice"] = row["stop_price"]
            if inst == "LINEAR":
                params["workingType"] = "MARK_PRICE"

        path = "/api/v3/order" if inst == "SPOT" else "/fapi/v1/order"
        try:
            resp = self.account.transport.request(env, inst, "POST", path, params=params, signed=True)
        except AccountSyncError as exc:
            if exc.code == "EXCHANGE_NETWORK_TIMEOUT" or (exc.status_code and exc.status_code >= 500):
                with self.store.tx() as con:
                    con.execute(
                        "UPDATE at_orders SET order_state='UNCERTAIN_TIMEOUT', last_error=?, updated_at=? WHERE client_order_id=?",
                        (exc.code, self.store.clock(), client_order_id),
                    )
                    self.store.audit(
                        "EXECUTION",
                        "ORDER_SUBMISSION_UNCERTAIN",
                        {"client_order_id": client_order_id, "error": str(exc)},
                        environment=env,
                        signal_id=row["signal_id"],
                        position_id=row["position_id"],
                        client_order_id=client_order_id,
                        severity="WARNING",
                        con=con,
                    )
                return self.reconcile_uncertain_order(client_order_id)

            self._record_order_failure(env, row, str(exc))
            return self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))

        return self._apply_exchange_order_response(client_order_id, resp)

    def reconcile_uncertain_order(self, client_order_id: str) -> dict:
        row = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))
        if not row:
            raise InvalidData("ORDER_NOT_FOUND")
        env, inst, sym = row["environment"], row["instrument"], row["symbol"]
        path = "/api/v3/order" if inst == "SPOT" else "/fapi/v1/order"
        try:
            resp = self.account.transport.request(
                env, inst, "GET", path,
                params={"symbol": sym, "origClientOrderId": client_order_id},
                signed=True,
            )
            return self._apply_exchange_order_response(client_order_id, resp)
        except AccountSyncError as exc:
            if exc.status_code == 400 and ("-2013" in str(exc) or "does not exist" in str(exc).lower()):
                now = self.store.clock()
                with self.store.tx() as con:
                    con.execute(
                        "UPDATE at_orders SET order_state='REJECTED', last_error='NOT_FOUND_AFTER_TIMEOUT', updated_at=? WHERE client_order_id=?",
                        (now, client_order_id),
                    )
                    self.store.audit(
                        "EXECUTION",
                        "UNCERTAIN_ORDER_CONFIRMED_ABSENT",
                        {"client_order_id": client_order_id},
                        environment=env,
                        position_id=row["position_id"],
                        client_order_id=client_order_id,
                        con=con,
                    )
                return self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))
            return row

    def _apply_exchange_order_response(self, client_order_id: str, resp: dict) -> dict:
        row = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))
        env = row["environment"]
        inst = row["instrument"]
        now = self.store.clock()

        ex_id = str(resp.get("orderId", row.get("exchange_order_id") or ""))
        status = str(resp.get("status", "NEW")).upper()
        exec_qty = number(resp.get("executedQty", "0"), "executedQty", zero=True)
        avg_px = number(resp.get("avgPrice") or resp.get("price") or "0", "avgPrice", zero=True)
        if avg_px == 0 and resp.get("cummulativeQuoteQty") and exec_qty > 0:
            quote_qty = number(resp["cummulativeQuoteQty"], "cummulativeQuoteQty", zero=True)
            if quote_qty > 0:
                avg_px = quote_qty / exec_qty

        fee_usdt = Decimal("0")
        fills = resp.get("fills") or []
        for f in fills:
            fee_usdt += number(f.get("commission", "0"), "commission", zero=True)
        if fee_usdt == 0 and exec_qty > 0 and avg_px > 0:
            fee_usdt = exec_qty * avg_px * self.config.default_taker_fee

        with self.store.tx() as con:
            con.execute(
                """UPDATE at_orders SET
                   exchange_order_id=?, filled_qty=?, avg_fill_price=?, fee_usdt=?,
                   order_state=?, raw_response=?, last_error=NULL, updated_at=?
                   WHERE client_order_id=?""",
                (
                    ex_id, fmt(exec_qty), fmt(avg_px) if avg_px > 0 else None,
                    fmt(fee_usdt), status, dumps(resp), now, client_order_id,
                ),
            )
            if exec_qty > 0:
                fill_id = f"fill_{client_order_id}_{fmt(exec_qty)}"
                con.execute(
                    """INSERT OR IGNORE INTO at_fills
                       (fill_id, client_order_id, exchange_order_id, position_id, environment, instrument, symbol, side, qty, price, fee_usdt, fee_asset, realized_pnl, executed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fill_id, client_order_id, ex_id, row["position_id"], env, inst,
                        row["symbol"], row["side"], fmt(exec_qty), fmt(avg_px if avg_px > 0 else (row["price"] or "0")),
                        fmt(fee_usdt), "USDT", "0", now,
                    ),
                )
            self.store.risk_state(f"execution_failures:{env}", 0, con=con)
            self.store.audit(
                "EXECUTION",
                f"ORDER_{status}",
                {
                    "client_order_id": client_order_id,
                    "exchange_order_id": ex_id,
                    "status": status,
                    "filled_qty": fmt(exec_qty),
                    "avg_fill_price": fmt(avg_px) if avg_px > 0 else None,
                    "role": row["role"],
                },
                environment=env,
                signal_id=row["signal_id"],
                position_id=row["position_id"],
                client_order_id=client_order_id,
                con=con,
            )

        updated = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))
        self._notify(
            kind=f"ORDER_{status}",
            title=f"SECRETAGENT | ORDER {status}",
            lines=[
                f"Symbol: {row['symbol']} | Role: {row['role']} | Env: {env}",
                f"Client Order ID: {client_order_id}",
                f"Filled: {fmt(exec_qty)} / {row['requested_qty']} @ {fmt(avg_px) if avg_px > 0 else 'PENDING'}",
            ],
            position_id=row["position_id"],
            signal_id=row["signal_id"],
            dedup_key=f"order:{client_order_id}:{status}:{fmt(exec_qty)}",
        )
        return updated

    def _record_order_failure(self, environment: str, order_row: dict, error_msg: str):
        now = self.store.clock()
        safe_err = redact_text(error_msg, self.config.secrets_tuple())[:250]
        with self.store.tx() as con:
            con.execute(
                "UPDATE at_orders SET order_state='REJECTED', last_error=?, updated_at=? WHERE client_order_id=?",
                (safe_err, now, order_row["client_order_id"]),
            )
            fails = int(self.store.risk_state(f"execution_failures:{environment}", default=0, con=con) or 0) + 1
            self.store.risk_state(f"execution_failures:{environment}", fails, con=con)
            self.store.audit(
                "EXECUTION",
                "ORDER_REJECTED",
                {"client_order_id": order_row["client_order_id"], "role": order_row["role"], "error": safe_err, "failure_count": fails},
                environment=environment,
                signal_id=order_row["signal_id"],
                position_id=order_row["position_id"],
                client_order_id=order_row["client_order_id"],
                severity="ERROR",
                con=con,
            )
            if fails >= self.config.max_execution_failures:
                self.risk.trip_breaker(
                    f"REPEATED_ORDER_FAILURES:{environment}",
                    "REPEATED_ORDER_FAILURES",
                    f"Order failures reached {fails}: {safe_err}",
                    environment=environment,
                    con=con,
                )

    def execute_authorized_entry(
        self,
        signal_id: str,
        sizing: PositionSizingSummary,
        spec: InstrumentSpec,
        environment: str,
        timeframe: str = "5m/15m/1h",
        order_type: str = "MARKET",
    ) -> dict:
        pid = "pos_" + uid()[:16]
        now = self.store.clock()
        entry_side = "BUY" if sizing.direction == "LONG" else "SELL"
        entry_coid = build_client_order_id(environment, pid, "ENT", 1)

        with self.store.tx() as con:
            con.execute(
                """INSERT INTO at_positions(
                   id, signal_id, strategy_version, environment, instrument, symbol, timeframe, direction,
                   signal_state, order_state, position_state, protective_order_state, account_state,
                   trailing_mode, trailing_stage, entry_price, quantity, multiplier, leverage, margin_mode,
                   initial_stop, current_stop, recommended_stop, take_profit, best_price,
                   entry_fee_rate, exit_fee_rate, initial_risk_usdt, last_price, last_price_at,
                   created_at, updated_at, details_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid, str(signal_id), "v5.5", environment, sizing.instrument, sizing.symbol, timeframe, sizing.direction,
                    "AUTHORIZED", "INTENT_PERSISTED", "PENDING_ENTRY", "UNPROTECTED", "SYNCHRONIZED",
                    "AUTOMATIC" if self.config.is_auto_trailing_authorized() else "ADVISORY", "WAITING_1R",
                    fmt(sizing.entry_price), fmt(sizing.rounded_quantity), fmt(sizing.contract_multiplier),
                    fmt(sizing.leverage), self.config.margin_mode,
                    fmt(sizing.stop_loss), fmt(sizing.stop_loss), fmt(sizing.stop_loss), fmt(sizing.take_profit),
                    fmt(sizing.entry_price), fmt(spec.taker_fee), fmt(spec.taker_fee),
                    fmt(sizing.estimated_total_risk_usdt), fmt(sizing.entry_price), now,
                    now, now, dumps({"entry_client_order_id": entry_coid}),
                ),
            )

        self.persist_order_intent(
            client_order_id=entry_coid,
            environment=environment,
            position_id=pid,
            signal_id=signal_id,
            instrument=sizing.instrument,
            symbol=sizing.symbol,
            side=entry_side,
            order_type=order_type,
            role="ENTRY",
            requested_qty=sizing.rounded_quantity,
            price=sizing.entry_price if order_type == "LIMIT" else None,
        )

        entry_order = self.submit_exchange_order(entry_coid)
        status = entry_order["order_state"]
        filled_qty = Decimal(str(entry_order["filled_qty"] or "0"))

        if status not in {"FILLED", "PARTIALLY_FILLED"} or filled_qty <= 0:
            with self.store.tx() as con:
                pos_state = "OPENING" if status in {"NEW", "UNCERTAIN_TIMEOUT"} else "CLOSED"
                con.execute(
                    "UPDATE at_positions SET order_state=?, position_state=?, exit_reason=?, updated_at=? WHERE id=?",
                    (status, pos_state, None if pos_state == "OPENING" else f"ENTRY_{status}", self.store.clock(), pid),
                )
            return self.store.at_position(pid)

        actual_entry = Decimal(str(entry_order["avg_fill_price"] or sizing.entry_price))
        fee_paid = Decimal(str(entry_order["fee_usdt"] or "0"))
        with self.store.tx() as con:
            con.execute(
                """UPDATE at_positions SET
                   order_state=?, position_state='OPENING', entry_price=?, quantity=?,
                   best_price=?, fees_paid_usdt=?, updated_at=?
                   WHERE id=?""",
                (status, fmt(actual_entry), fmt(filled_qty), fmt(actual_entry), fmt(fee_paid), self.store.clock(), pid),
            )

        self.install_and_verify_protection(pid, spec)
        return self.store.at_position(pid)

    def install_and_verify_protection(self, position_id: str, spec: InstrumentSpec = None) -> dict:
        pos = self.store.at_position(position_id)
        if not pos:
            raise InvalidData("POSITION_NOT_FOUND")
        env = pos["environment"]
        inst = pos["instrument"]
        sym = pos["symbol"]
        spec = spec or self.account.get_cached_spec(inst, sym)
        if spec is None:
            spec = self.account.sync_instrument_spec(inst, sym, env)

        filled_qty = Decimal(str(pos["quantity"]))
        if filled_qty <= 0:
            raise InvalidData("ZERO_POSITION_QUANTITY")

        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        sl_price = round_tick(Decimal(str(pos["current_stop"])), spec.tick_size, up=(pos["direction"] == "SHORT"))
        tp_price = round_tick(Decimal(str(pos["take_profit"])), spec.tick_size, up=(pos["direction"] == "SHORT")) if pos.get("take_profit") else None

        sl_seq = len(self.store.rows("SELECT 1 FROM at_orders WHERE position_id=? AND role='STOP_LOSS'", (position_id,))) + 1
        sl_coid = build_client_order_id(env, position_id, "SL", sl_seq)
        sl_type = "STOP_LOSS_LIMIT" if inst == "SPOT" else "STOP_MARKET"
        self.persist_order_intent(
            client_order_id=sl_coid,
            environment=env,
            position_id=position_id,
            signal_id=pos["signal_id"],
            instrument=inst,
            symbol=sym,
            side=close_side,
            order_type=sl_type,
            role="STOP_LOSS",
            requested_qty=filled_qty,
            price=sl_price if inst == "SPOT" else None,
            stop_price=sl_price,
            reduce_only=(inst == "LINEAR"),
        )
        sl_order = self.submit_exchange_order(sl_coid)

        tp_coid = None
        if tp_price and inst == "LINEAR":
            tp_seq = len(self.store.rows("SELECT 1 FROM at_orders WHERE position_id=? AND role='TAKE_PROFIT'", (position_id,))) + 1
            tp_coid = build_client_order_id(env, position_id, "TP", tp_seq)
            self.persist_order_intent(
                client_order_id=tp_coid,
                environment=env,
                position_id=position_id,
                signal_id=pos["signal_id"],
                instrument=inst,
                symbol=sym,
                side=close_side,
                order_type="TAKE_PROFIT_MARKET",
                role="TAKE_PROFIT",
                requested_qty=filled_qty,
                stop_price=tp_price,
                reduce_only=True,
            )
            self.submit_exchange_order(tp_coid)

        sl_ok = sl_order["order_state"] in {"NEW", "PARTIALLY_FILLED", "FILLED"} and Decimal(str(sl_order["requested_qty"])) >= filled_qty
        if not sl_ok:
            return self._handle_protection_failure(pos, sl_order.get("last_error") or sl_order["order_state"])

        now = self.store.clock()
        with self.store.tx() as con:
            con.execute(
                """UPDATE at_positions SET
                   position_state='PROTECTED_ACTIVE',
                   protective_order_state='VERIFIED',
                   sl_client_order_id=?,
                   tp_client_order_id=?,
                   last_confirmed_exchange_stop=?,
                   requires_attention=0,
                   last_error=NULL,
                   updated_at=?
                   WHERE id=?""",
                (sl_coid, tp_coid, fmt(sl_price), now, position_id),
            )
            self.store.audit(
                "PROTECTION",
                "PROTECTIVE_ORDERS_VERIFIED",
                {
                    "position_id": position_id,
                    "sl_client_order_id": sl_coid,
                    "tp_client_order_id": tp_coid,
                    "sl_price": fmt(sl_price),
                    "covered_qty": fmt(filled_qty),
                },
                environment=env,
                signal_id=pos["signal_id"],
                position_id=position_id,
                client_order_id=sl_coid,
                con=con,
            )

        self._notify(
            kind="PROTECTIVE_VERIFIED",
            title="SECRETAGENT | POSISI TERPROTEKSI",
            lines=[
                f"Position ID: {position_id} | {sym} {pos['direction']}",
                f"Qty: {fmt(filled_qty)} @ Entry {pos['entry_price']}",
                f"Verified SL: {fmt(sl_price)} ({sl_coid})",
                f"TP: {fmt(tp_price) if tp_price else 'N/A'}",
            ],
            position_id=position_id,
            signal_id=pos["signal_id"],
            dedup_key=f"prot_ok:{position_id}:{sl_coid}",
        )
        return self.store.at_position(position_id)

    def _handle_protection_failure(self, pos: dict, reason: str) -> dict:
        pid = pos["id"]
        env = pos["environment"]
        now = self.store.clock()
        safe_reason = redact_text(reason, self.config.secrets_tuple())[:200]

        with self.store.tx() as con:
            self.risk.trip_breaker(
                f"MISSING_PROTECTIVE_ORDERS:{env}",
                "MISSING_PROTECTIVE_ORDERS",
                f"Position {pid} ({pos['symbol']}) failed SL installation: {safe_reason}",
                environment=env,
                con=con,
            )
            con.execute(
                """UPDATE at_positions SET
                   position_state='PROTECTION_REQUIRED',
                   protective_order_state='PROTECTION_FAILED',
                   requires_attention=1,
                   last_error=?,
                   updated_at=?
                   WHERE id=?""",
                (safe_reason, now, pid),
            )
            self.store.audit(
                "PROTECTION",
                "PROTECTIVE_ORDER_FAILED",
                {
                    "position_id": pid,
                    "symbol": pos["symbol"],
                    "reason": safe_reason,
                    "emergency_policy": self.config.emergency_unprotected_policy,
                },
                environment=env,
                signal_id=pos["signal_id"],
                position_id=pid,
                severity="CRITICAL",
                con=con,
            )

        self._notify(
            kind="PROTECTIVE_FAILED",
            title="🚨 CRITICAL | PROTECTIVE ORDER FAILED",
            lines=[
                f"Position ID: {pid} | {pos['symbol']} {pos['direction']}",
                f"Reason: {safe_reason}",
                f"New Entries: BLOCKED (Circuit Breaker Active)",
                f"Policy: {self.config.emergency_unprotected_policy}",
            ],
            position_id=pid,
            signal_id=pos["signal_id"],
            dedup_key=f"prot_fail:{pid}:{safe_reason}",
        )

        if self.config.emergency_unprotected_policy == "EMERGENCY_MARKET_CLOSE":
            return self.close_position_market(pid, reason="EMERGENCY_UNPROTECTED_POLICY_CLOSE")
        return self.store.at_position(pid)

    def sync_partial_fill_and_protection(self, entry_client_order_id: str, new_filled_qty: Decimal, avg_fill_price: Decimal = None) -> dict:
        order = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (entry_client_order_id,))
        if not order:
            raise InvalidData("ORDER_NOT_FOUND")
        pid = order["position_id"]
        pos = self.store.at_position(pid)
        req_qty = Decimal(str(order["requested_qty"]))
        new_state = "FILLED" if new_filled_qty >= req_qty else "PARTIALLY_FILLED"
        now = self.store.clock()

        with self.store.tx() as con:
            con.execute(
                "UPDATE at_orders SET filled_qty=?, avg_fill_price=COALESCE(?, avg_fill_price), order_state=?, updated_at=? WHERE client_order_id=?",
                (fmt(new_filled_qty), fmt(avg_fill_price) if avg_fill_price else None, new_state, now, entry_client_order_id),
            )
            con.execute(
                "UPDATE at_positions SET quantity=?, entry_price=COALESCE(?, entry_price), order_state=?, protective_order_state='PARTIAL_COVERAGE', updated_at=? WHERE id=?",
                (fmt(new_filled_qty), fmt(avg_fill_price) if avg_fill_price else None, new_state, now, pid),
            )

        if pos.get("sl_client_order_id"):
            self.cancel_exchange_order(pos["sl_client_order_id"])
        if pos.get("tp_client_order_id"):
            self.cancel_exchange_order(pos["tp_client_order_id"])
        spec = self.account.get_cached_spec(pos["instrument"], pos["symbol"])
        return self.install_and_verify_protection(pid, spec)

    def cancel_exchange_order(self, client_order_id: str) -> dict:
        row = self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))
        if not row:
            raise InvalidData("ORDER_NOT_FOUND")
        if row["order_state"] in {"CANCELED", "FILLED", "REJECTED", "EXPIRED"}:
            return {"canceled": row["order_state"] == "CANCELED", "confirmed": True, "order": row}

        env, inst, sym = row["environment"], row["instrument"], row["symbol"]
        path = "/api/v3/order" if inst == "SPOT" else "/fapi/v1/order"
        try:
            resp = self.account.transport.request(
                env, inst, "DELETE", path,
                params={"symbol": sym, "origClientOrderId": client_order_id},
                signed=True,
            )
            status = str(resp.get("status", "CANCELED")).upper()
            confirmed = status == "CANCELED"
            with self.store.tx() as con:
                con.execute(
                    "UPDATE at_orders SET order_state=?, raw_response=?, updated_at=? WHERE client_order_id=?",
                    (status, dumps(resp), self.store.clock(), client_order_id),
                )
            return {"canceled": confirmed, "confirmed": confirmed, "order": self.store.one("SELECT * FROM at_orders WHERE client_order_id=?", (client_order_id,))}
        except AccountSyncError as exc:
            safe_err = redact_text(str(exc), self.config.secrets_tuple())[:200]
            with self.store.tx() as con:
                con.execute("UPDATE at_orders SET last_error=?, updated_at=? WHERE client_order_id=?", (safe_err, self.store.clock(), client_order_id))
            return {"canceled": False, "confirmed": False, "error": safe_err, "order": row}

    def close_position_market(self, position_id: str, reason: str = "RULE_EXIT") -> dict:
        pos = self.store.at_position(position_id)
        if not pos or pos["position_state"] == "CLOSED":
            return pos
        env, inst, sym = pos["environment"], pos["instrument"], pos["symbol"]
        qty = Decimal(str(pos["quantity"]))
        close_side = "SELL" if pos["direction"] == "LONG" else "BUY"
        seq = len(self.store.rows("SELECT 1 FROM at_orders WHERE position_id=? AND role IN ('EXIT_MARKET','EMERGENCY_CLOSE')", (position_id,))) + 1
        role = "EMERGENCY_CLOSE" if "EMERGENCY" in reason else "EXIT_MARKET"
        coid = build_client_order_id(env, position_id, role, seq)

        self.persist_order_intent(
            client_order_id=coid,
            environment=env,
            position_id=position_id,
            signal_id=pos["signal_id"],
            instrument=inst,
            symbol=sym,
            side=close_side,
            order_type="MARKET",
            role=role,
            requested_qty=qty,
            reduce_only=(inst == "LINEAR"),
        )
        exit_ord = self.submit_exchange_order(coid)
        if exit_ord["order_state"] == "FILLED":
            if pos.get("sl_client_order_id"):
                self.cancel_exchange_order(pos["sl_client_order_id"])
            if pos.get("tp_client_order_id"):
                self.cancel_exchange_order(pos["tp_client_order_id"])

            exit_px = Decimal(str(exit_ord["avg_fill_price"] or pos["last_price"] or pos["entry_price"]))
            entry_px = Decimal(str(pos["entry_price"]))
            mult = Decimal(str(pos["multiplier"] or "1"))
            sign = Decimal("1") if pos["direction"] == "LONG" else Decimal("-1")
            gross = sign * (exit_px - entry_px) * qty * mult
            fees = Decimal(str(pos["fees_paid_usdt"] or "0")) + Decimal(str(exit_ord["fee_usdt"] or "0"))
            funding = Decimal(str(pos["funding_paid_usdt"] or "0"))
            net = gross - fees - funding
            init_risk = Decimal(str(pos["initial_risk_usdt"] or "1"))
            r_mult = (gross / init_risk) if init_risk > 0 else Decimal("0")
            now = self.store.clock()

            with self.store.tx() as con:
                con.execute(
                    """UPDATE at_positions SET
                       order_state='FILLED', position_state='CLOSED', protective_order_state='CLOSED',
                       exit_price=?, exit_reason=?, fees_paid_usdt=?, gross_pnl_usdt=?, net_pnl_usdt=?,
                       r_multiple=?, requires_attention=0, closed_at=?, updated_at=?
                       WHERE id=?""",
                    (fmt(exit_px), reason, fmt(fees), fmt(gross), fmt(net), fmt(r_mult), now, now, position_id),
                )
                self.risk.record_trade_outcome(env, gross, net, fees, funding, con=con)
                self.store.audit(
                    "POSITION",
                    "POSITION_CLOSED",
                    {"position_id": position_id, "exit_price": fmt(exit_px), "net_pnl_usdt": fmt(net), "reason": reason},
                    environment=env,
                    signal_id=pos["signal_id"],
                    position_id=position_id,
                    client_order_id=coid,
                    con=con,
                )
            self._notify(
                kind="POSITION_CLOSED",
                title="SECRETAGENT | POSISI DITUTUP",
                lines=[
                    f"Position ID: {position_id} | {sym} {pos['direction']} ({env})",
                    f"Exit Price: {fmt(exit_px)} | Reason: {reason}",
                    f"Net PnL: {fmt(net)} USDT | R: {fmt(r_mult)}",
                ],
                position_id=position_id,
                signal_id=pos["signal_id"],
                dedup_key=f"pos_closed:{position_id}",
            )
        return self.store.at_position(position_id)

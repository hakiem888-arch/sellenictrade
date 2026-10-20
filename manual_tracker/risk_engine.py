"""Phase 2: Deterministic Dynamic Risk Management Engine.

Calculates position size from real account equity, stop distance, contract specifications,
fees, and slippage. Validates every prospective trade against account, portfolio, daily loss,
and circuit-breaker limits. AI can never bypass this engine.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math

from .account import AccountSnapshot, InstrumentSpec
from .config import AutonomousConfig
from .engine import InvalidData, fmt, number, round_tick
from .store import Store, dumps


@dataclass(frozen=True)
class PositionSizingSummary:
    instrument: str
    symbol: str
    direction: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    eligible_equity_usdt: Decimal
    risk_budget_usdt: Decimal
    stop_distance: Decimal
    contract_multiplier: Decimal
    estimated_costs_per_unit: Decimal
    risk_per_unit_usdt: Decimal
    raw_quantity: Decimal
    rounded_quantity: Decimal
    notional_usdt: Decimal
    leverage: Decimal
    required_margin_usdt: Decimal
    estimated_fees_usdt: Decimal
    estimated_slippage_usdt: Decimal
    estimated_total_risk_usdt: Decimal
    effective_risk_pct: Decimal
    gross_rr: Decimal
    net_rr: Decimal


@dataclass(frozen=True)
class RiskValidationDecision:
    approved: bool
    reason_codes: tuple
    calculation_inputs: dict
    sizing: PositionSizingSummary = None
    estimated_risk_usdt: Decimal = Decimal("0")
    timestamp: float = 0.0

    def to_dict(self):
        return {
            "approved": self.approved,
            "reason_codes": list(self.reason_codes),
            "calculation_inputs": self.calculation_inputs,
            "sizing": {k: fmt(v) if isinstance(v, Decimal) else v for k, v in asdict(self.sizing).items()} if self.sizing else None,
            "estimated_risk_usdt": fmt(self.estimated_risk_usdt),
            "timestamp": self.timestamp,
        }


def utc_day_key(ts: float) -> str:
    """Daily accounting boundary: 00:00:00 UTC (`YYYY-MM-DD`)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def step_floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


class DynamicRiskEngine:
    """Deterministic risk calculator, validator, daily loss tracker, and circuit breaker manager."""

    def __init__(self, store: Store, config: AutonomousConfig):
        self.store = store
        self.config = config

    def trip_breaker(self, breaker_id: str, reason: str, detail: str = "", environment: str = None, con=None):
        env = environment or self.config.execution_mode
        now = self.store.clock()
        if con is None:
            with self.store.tx() as c:
                return self.trip_breaker(breaker_id, reason, detail, env, c)
        con.execute(
            """INSERT INTO at_circuit_breakers(breaker_id, active, reason, detail, tripped_at, updated_at)
               VALUES(?, 1, ?, ?, ?, ?)
               ON CONFLICT(breaker_id) DO UPDATE SET
               active=1, reason=excluded.reason, detail=excluded.detail,
               tripped_at=COALESCE(at_circuit_breakers.tripped_at, excluded.tripped_at),
               updated_at=excluded.updated_at""",
            (breaker_id, reason, detail[:500], now, now),
        )
        self.store.audit(
            "CIRCUIT_BREAKER",
            "TRIPPED",
            {"breaker_id": breaker_id, "reason": reason, "detail": detail},
            environment=env,
            severity="CRITICAL",
            con=con,
        )

    def reset_breaker(self, breaker_id: str, actor: str = "OPERATOR", environment: str = None):
        env = environment or self.config.execution_mode
        now = self.store.clock()
        with self.store.tx() as con:
            con.execute(
                "UPDATE at_circuit_breakers SET active=0, updated_at=?, reset_by=?, reset_at=? WHERE breaker_id=?",
                (now, actor, now, breaker_id),
            )
            self.store.audit(
                "CIRCUIT_BREAKER",
                "RESET",
                {"breaker_id": breaker_id, "actor": actor},
                environment=env,
                severity="INFO",
                con=con,
            )

    def active_breakers(self) -> list:
        return self.store.rows("SELECT * FROM at_circuit_breakers WHERE active=1 ORDER BY tripped_at ASC")

    def get_or_init_daily_ledger(self, environment: str, current_equity: Decimal, now: float = None, con=None) -> dict:
        now = self.store.clock() if now is None else now
        day = utc_day_key(now)
        if con is None:
            with self.store.tx() as c:
                return self.get_or_init_daily_ledger(environment, current_equity, now, c)
        row = con.execute(
            "SELECT * FROM at_daily_risk_ledger WHERE day_utc=? AND environment=?",
            (day, environment),
        ).fetchone()
        if not row:
            con.execute(
                """INSERT INTO at_daily_risk_ledger
                   (day_utc, environment, starting_equity, updated_at)
                   VALUES(?, ?, ?, ?)""",
                (day, environment, fmt(current_equity), now),
            )
            row = con.execute(
                "SELECT * FROM at_daily_risk_ledger WHERE day_utc=? AND environment=?",
                (day, environment),
            ).fetchone()
        return dict(row)

    def record_trade_outcome(
        self,
        environment: str,
        gross_pnl: Decimal,
        net_pnl: Decimal,
        fees_paid: Decimal,
        funding_paid: Decimal = Decimal("0"),
        current_equity: Decimal = None,
        con=None,
    ) -> dict:
        now = self.store.clock()
        eq = current_equity if current_equity is not None else self.config.paper_initial_equity
        if con is None:
            with self.store.tx() as c:
                return self.record_trade_outcome(environment, gross_pnl, net_pnl, fees_paid, funding_paid, eq, c)

        ledger = self.get_or_init_daily_ledger(environment, eq, now, con)
        day = ledger["day_utc"]
        r_gross = Decimal(str(ledger["realized_pnl_gross"])) + gross_pnl
        r_net = Decimal(str(ledger["realized_pnl_net"])) + net_pnl
        fees = Decimal(str(ledger["fees_paid"])) + fees_paid
        funding = Decimal(str(ledger["funding_paid"])) + funding_paid
        trades = int(ledger["trades_count"]) + 1
        wins = int(ledger["wins_count"]) + (1 if net_pnl > 0 else 0)
        losses = int(ledger["losses_count"]) + (1 if net_pnl < 0 else 0)
        consec = (int(ledger["consecutive_losses"]) + 1) if net_pnl < 0 else (0 if net_pnl > 0 else int(ledger["consecutive_losses"]))

        global_consec = int(self.store.risk_state(f"consecutive_losses:{environment}", default=0, con=con) or 0)
        global_consec = (global_consec + 1) if net_pnl < 0 else (0 if net_pnl > 0 else global_consec)
        self.store.risk_state(f"consecutive_losses:{environment}", global_consec, con=con)

        upnl = Decimal(str(ledger["unrealized_pnl"]))
        effective_pnl = min(r_net, r_net + upnl)
        drawdown = max(Decimal(str(ledger["max_drawdown_usdt"])), max(Decimal("0"), -effective_pnl))
        start_eq = Decimal(str(ledger["starting_equity"]))
        max_loss_allowed = min(start_eq * self.config.max_daily_loss_pct, self.config.max_daily_loss_usdt)
        breached = 1 if (drawdown >= max_loss_allowed or int(ledger["daily_limit_breached"]) == 1) else 0

        con.execute(
            """UPDATE at_daily_risk_ledger SET
               realized_pnl_gross=?, realized_pnl_net=?, fees_paid=?, funding_paid=?,
               trades_count=?, wins_count=?, losses_count=?, consecutive_losses=?,
               max_drawdown_usdt=?, daily_limit_breached=?, updated_at=?
               WHERE day_utc=? AND environment=?""",
            (
                fmt(r_gross), fmt(r_net), fmt(fees), fmt(funding),
                trades, wins, losses, consec, fmt(drawdown), breached, now, day, environment,
            ),
        )
        if breached:
            self.trip_breaker(
                f"DAILY_LOSS_LIMIT:{environment}",
                "DAILY_LOSS_LIMIT_BREACH",
                f"Day={day} Drawdown={fmt(drawdown)} USDT >= Limit={fmt(max_loss_allowed)} USDT",
                environment=environment,
                con=con,
            )
        if self.config.max_consecutive_losses > 0 and global_consec >= self.config.max_consecutive_losses:
            self.trip_breaker(
                f"MAX_CONSECUTIVE_LOSSES:{environment}",
                "MAX_CONSECUTIVE_LOSSES_BREACH",
                f"Consecutive losses={global_consec} >= {self.config.max_consecutive_losses}",
                environment=environment,
                con=con,
            )
        return dict(con.execute("SELECT * FROM at_daily_risk_ledger WHERE day_utc=? AND environment=?", (day, environment)).fetchone())

    def update_daily_unrealized(self, environment: str, unrealized_pnl: Decimal, current_equity: Decimal, con=None) -> dict:
        now = self.store.clock()
        if con is None:
            with self.store.tx() as c:
                return self.update_daily_unrealized(environment, unrealized_pnl, current_equity, c)
        ledger = self.get_or_init_daily_ledger(environment, current_equity, now, con)
        day = ledger["day_utc"]
        r_net = Decimal(str(ledger["realized_pnl_net"]))
        effective_pnl = min(r_net, r_net + unrealized_pnl)
        drawdown = max(Decimal(str(ledger["max_drawdown_usdt"])), max(Decimal("0"), -effective_pnl))
        start_eq = Decimal(str(ledger["starting_equity"]))
        max_loss_allowed = min(start_eq * self.config.max_daily_loss_pct, self.config.max_daily_loss_usdt)
        breached = 1 if (drawdown >= max_loss_allowed or int(ledger["daily_limit_breached"]) == 1) else 0
        con.execute(
            """UPDATE at_daily_risk_ledger SET unrealized_pnl=?, max_drawdown_usdt=?, daily_limit_breached=?, updated_at=?
               WHERE day_utc=? AND environment=?""",
            (fmt(unrealized_pnl), fmt(drawdown), breached, now, day, environment),
        )
        if breached:
            self.trip_breaker(
                f"DAILY_LOSS_LIMIT:{environment}",
                "DAILY_LOSS_LIMIT_BREACH",
                f"Day={day} Drawdown={fmt(drawdown)} USDT >= Limit={fmt(max_loss_allowed)} USDT",
                environment=environment,
                con=con,
            )
        return dict(con.execute("SELECT * FROM at_daily_risk_ledger WHERE day_utc=? AND environment=?", (day, environment)).fetchone())

    def calculate_position_size(
        self,
        account: AccountSnapshot,
        spec: InstrumentSpec,
        direction: str,
        entry_price,
        stop_loss,
        take_profit,
        leverage=None,
    ) -> PositionSizingSummary:
        if direction not in {"LONG", "SHORT"}:
            raise InvalidData("INVALID_DIRECTION")
        if spec.instrument == "SPOT" and direction != "LONG":
            raise InvalidData("SPOT_SHORT_NOT_SUPPORTED")
        if spec.instrument not in {"SPOT", "LINEAR"}:
            raise InvalidData("UNSUPPORTED_INSTRUMENT_TYPE")

        raw_entry = number(entry_price, "entry_price")
        raw_sl = number(stop_loss, "stop_loss")
        raw_tp = number(take_profit, "take_profit")
        tick = spec.tick_size
        step = spec.step_size
        mult = spec.contract_multiplier

        long_dir = direction == "LONG"
        entry = round_tick(raw_entry, tick, up=long_dir)
        sl = round_tick(raw_sl, tick, up=not long_dir)
        tp = round_tick(raw_tp, tick, up=not long_dir)

        stop_diff = (entry - sl) if long_dir else (sl - entry)
        tp_diff = (tp - entry) if long_dir else (entry - tp)
        if stop_diff <= 0:
            raise InvalidData("INVALID_STOP_LOSS_SIDE_OR_ZERO_DISTANCE")
        if tp_diff <= 0:
            raise InvalidData("INVALID_TAKE_PROFIT_SIDE_OR_ZERO_DISTANCE")

        eff_lev = Decimal("1") if spec.instrument == "SPOT" else number(leverage or self.config.default_leverage, "leverage")
        if eff_lev <= 0:
            raise InvalidData("INVALID_LEVERAGE")

        eligible_equity = account.account_equity if spec.instrument == "LINEAR" else min(account.account_equity, account.available_balance)
        if eligible_equity <= 0:
            raise InvalidData("NON_POSITIVE_ELIGIBLE_EQUITY")

        risk_budget = eligible_equity * self.config.risk_pct_per_trade

        fee_in = spec.taker_fee
        fee_out = spec.taker_fee
        slip_rate = self.config.default_slippage_bps / Decimal("10000")
        funding_rate = abs(spec.funding_rate) if spec.instrument == "LINEAR" else Decimal("0")

        costs_per_unit = (entry * (fee_in + slip_rate + funding_rate) + sl * (fee_out + slip_rate)) * mult
        risk_per_unit = (stop_diff * mult) + costs_per_unit
        if risk_per_unit <= 0:
            raise InvalidData("NON_POSITIVE_RISK_PER_UNIT")

        raw_qty = risk_budget / risk_per_unit
        rounded_qty = step_floor(raw_qty, step)

        while rounded_qty > 0 and (rounded_qty * risk_per_unit) > risk_budget:
            rounded_qty = max(Decimal("0"), rounded_qty - step)

        notional = rounded_qty * entry * mult
        req_margin = notional if spec.instrument == "SPOT" else (notional / eff_lev)
        est_fees = rounded_qty * mult * (entry * fee_in + sl * fee_out + entry * funding_rate)
        est_slip = rounded_qty * mult * (entry + sl) * slip_rate
        total_risk = rounded_qty * risk_per_unit
        eff_risk_pct = (total_risk / account.account_equity) if account.account_equity > 0 else Decimal("0")

        gross_rr = tp_diff / stop_diff
        tp_costs_per_unit = (entry * (fee_in + slip_rate + funding_rate) + tp * (fee_out + slip_rate)) * mult
        net_reward_per_unit = (tp_diff * mult) - tp_costs_per_unit
        net_rr = net_reward_per_unit / risk_per_unit

        return PositionSizingSummary(
            instrument=spec.instrument,
            symbol=spec.symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            eligible_equity_usdt=eligible_equity,
            risk_budget_usdt=risk_budget,
            stop_distance=stop_diff,
            contract_multiplier=mult,
            estimated_costs_per_unit=costs_per_unit,
            risk_per_unit_usdt=risk_per_unit,
            raw_quantity=raw_qty,
            rounded_quantity=rounded_qty,
            notional_usdt=notional,
            leverage=eff_lev,
            required_margin_usdt=req_margin,
            estimated_fees_usdt=est_fees,
            estimated_slippage_usdt=est_slip,
            estimated_total_risk_usdt=total_risk,
            effective_risk_pct=eff_risk_pct,
            gross_rr=gross_rr,
            net_rr=net_rr,
        )

    def validate_entry(
        self,
        account: AccountSnapshot,
        spec: InstrumentSpec,
        direction: str,
        entry_price,
        stop_loss,
        take_profit,
        leverage=None,
        market_data_timestamp: float = None,
        signal_timestamp: float = None,
        protective_supported: bool = True,
        trading_mode_authorized: bool = True,
        signal_id: str = None,
    ) -> RiskValidationDecision:
        now = self.store.clock()
        reasons = []
        env = account.environment if account else self.config.execution_mode

        calc_inputs = {
            "environment": env,
            "instrument": spec.instrument if spec else None,
            "symbol": spec.symbol if spec else None,
            "direction": direction,
            "entry_price": str(entry_price),
            "stop_loss": str(stop_loss),
            "take_profit": str(take_profit),
            "leverage": str(leverage or self.config.default_leverage),
            "risk_pct_per_trade": fmt(self.config.risk_pct_per_trade),
            "now": now,
        }

        if not trading_mode_authorized:
            reasons.append("TRADING_MODE_NOT_AUTHORIZED")
        if not protective_supported:
            reasons.append("PROTECTIVE_ORDER_UNSUPPORTED")

        kill_switch = self.store.risk_state("kill_switch", default={"active": False})
        if isinstance(kill_switch, dict) and kill_switch.get("active"):
            reasons.append("KILL_SWITCH_ACTIVE")
        breakers = self.active_breakers()
        if breakers:
            for b in breakers:
                reasons.append(f"CIRCUIT_BREAKER_ACTIVE:{b['reason']}")

        if account is None:
            reasons.append("MISSING_ACCOUNT_SNAPSHOT")
        else:
            if not account.is_fresh(now, self.config.max_account_data_age_seconds):
                reasons.append("STALE_OR_DEGRADED_ACCOUNT_DATA")
            if env == "LIVE" and account.permissions.get("enableWithdrawals") is True:
                reasons.append("WITHDRAWAL_PERMISSION_ENABLED_ON_API_KEY")

        if market_data_timestamp is None or not math.isfinite(market_data_timestamp):
            reasons.append("MISSING_MARKET_DATA_TIMESTAMP")
        elif market_data_timestamp > now + 2 or (now - market_data_timestamp) > self.config.max_market_data_age_seconds:
            reasons.append("STALE_MARKET_DATA")

        if signal_timestamp is not None:
            if not math.isfinite(signal_timestamp) or (now - signal_timestamp) > self.config.max_signal_age_seconds:
                reasons.append("STALE_SIGNAL")

        if spec is None or spec.status != "TRADING" or spec.tick_size <= 0 or spec.step_size <= 0:
            reasons.append("INCOMPLETE_INSTRUMENT_METADATA")

        sizing = None
        if account is not None and spec is not None and spec.status == "TRADING":
            try:
                sizing = self.calculate_position_size(
                    account=account,
                    spec=spec,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    leverage=leverage,
                )
            except InvalidData as e:
                reasons.append(str(e))

        if sizing is not None:
            if sizing.rounded_quantity <= 0 or sizing.rounded_quantity < spec.min_qty:
                reasons.append("MIN_SIZE_EXCEEDS_RISK_BUDGET")
            if sizing.rounded_quantity > spec.max_qty:
                reasons.append("MAX_QTY_EXCEEDED")
            if sizing.notional_usdt < spec.min_notional:
                reasons.append("BELOW_EXCHANGE_MIN_NOTIONAL")
            if sizing.notional_usdt > self.config.max_position_notional_usdt:
                reasons.append("MAX_POSITION_NOTIONAL_EXCEEDED")
            if sizing.estimated_total_risk_usdt > sizing.risk_budget_usdt:
                reasons.append("POST_ROUNDING_RISK_EXCEEDS_BUDGET")

            if sizing.leverage > self.config.max_permitted_leverage or sizing.leverage > Decimal(spec.max_leverage):
                reasons.append("LEVERAGE_EXCEEDS_MAXIMUM")

            if sizing.gross_rr < self.config.min_acceptable_rr:
                reasons.append("RISK_REWARD_BELOW_MINIMUM")
            if sizing.net_rr <= Decimal("0.5"):
                reasons.append("NET_RISK_REWARD_UNFAVORABLE_AFTER_FEES")
            if sizing.risk_budget_usdt > 0 and (sizing.estimated_fees_usdt / sizing.risk_budget_usdt) > self.config.max_fee_share_of_risk:
                reasons.append("FEE_BURDEN_EXCEEDS_LIMIT")

            required_funds = sizing.required_margin_usdt + sizing.estimated_fees_usdt
            if required_funds > account.available_margin or required_funds > account.available_balance:
                reasons.append("INSUFFICIENT_AVAILABLE_MARGIN")

            open_rows = self.store.rows(
                """SELECT symbol, entry_price, quantity, multiplier, leverage, initial_risk_usdt
                   FROM at_positions
                   WHERE environment=? AND position_state IN ('PENDING_ENTRY', 'OPENING', 'PROTECTED_ACTIVE', 'PROTECTION_REQUIRED', 'HALTED_DISCREPANCY')""",
                (env,),
            )
            concurrent_count = max(len(open_rows), len(account.open_positions))
            if concurrent_count >= self.config.max_concurrent_positions:
                reasons.append("MAX_CONCURRENT_POSITIONS_REACHED")

            existing_open_risk = sum((Decimal(str(r["initial_risk_usdt"] or "0")) for r in open_rows), Decimal("0"))
            max_open_risk = account.account_equity * self.config.max_aggregate_open_risk_pct
            if (existing_open_risk + sizing.estimated_total_risk_usdt) > max_open_risk:
                reasons.append("MAX_AGGREGATE_OPEN_RISK_EXCEEDED")

            asset_notional = sum(
                (Decimal(str(r["entry_price"])) * Decimal(str(r["quantity"])) * Decimal(str(r["multiplier"] or "1"))
                 for r in open_rows if r["symbol"] == spec.symbol),
                Decimal("0"),
            ) + sizing.notional_usdt
            if (asset_notional / account.account_equity) > self.config.max_exposure_per_asset_pct:
                reasons.append("MAX_ASSET_EXPOSURE_EXCEEDED")

            total_portfolio_notional = sum(
                (Decimal(str(r["entry_price"])) * Decimal(str(r["quantity"])) * Decimal(str(r["multiplier"] or "1"))
                 for r in open_rows),
                Decimal("0"),
            ) + sizing.notional_usdt
            if (total_portfolio_notional / account.account_equity) > self.config.max_portfolio_exposure_pct:
                reasons.append("MAX_PORTFOLIO_EXPOSURE_EXCEEDED")

            ledger = self.update_daily_unrealized(env, account.unrealized_pnl, account.account_equity)
            if int(ledger.get("daily_limit_breached", 0)) == 1:
                reasons.append("DAILY_LOSS_LIMIT_BREACHED")
            consec_losses = int(self.store.risk_state(f"consecutive_losses:{env}", default=0) or 0)
            if self.config.max_consecutive_losses > 0 and consec_losses >= self.config.max_consecutive_losses:
                reasons.append("MAX_CONSECUTIVE_LOSSES_REACHED")

        dedup_reasons = tuple(dict.fromkeys(reasons))
        approved = len(dedup_reasons) == 0 and sizing is not None
        decision = RiskValidationDecision(
            approved=approved,
            reason_codes=dedup_reasons,
            calculation_inputs=calc_inputs,
            sizing=sizing,
            estimated_risk_usdt=sizing.estimated_total_risk_usdt if sizing else Decimal("0"),
            timestamp=now,
        )
        self.store.audit(
            "RISK",
            "RISK_VALIDATED" if approved else "RISK_REJECTED",
            decision.to_dict(),
            environment=env,
            signal_id=signal_id,
            severity="INFO" if approved else "WARNING",
        )
        return decision

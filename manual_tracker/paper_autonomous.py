"""Phase 3: Autonomous Paper Trading & Reproducible Walk-Forward Backtesting."""
from dataclasses import dataclass, asdict
from decimal import Decimal
import math

from .account import AccountEngine, InstrumentSpec
from .config import AutonomousConfig, Config
from .engine import InvalidData, Observation, fmt, number, round_tick
from .risk_engine import DynamicRiskEngine, PositionSizingSummary
from .store import Store, dumps, uid
from .trailing_manager import SmartTrailingManager


class AutonomousPaperEngine:
    """Reproducible paper-trading engine sharing the live risk-validation and trailing lifecycle."""

    def __init__(
        self,
        store: Store,
        manual_config: Config,
        auto_config: AutonomousConfig,
        account_engine: AccountEngine,
        risk_engine: DynamicRiskEngine,
        trailing_manager: SmartTrailingManager,
        notify_fn=None,
    ):
        self.store = store
        self.manual_cfg = manual_config
        self.config = auto_config
        self.account = account_engine
        self.risk = risk_engine
        self.trailing = trailing_manager
        self.notify_fn = notify_fn

    def _default_spec(self, instrument: str, symbol: str) -> InstrumentSpec:
        cached = self.account.get_cached_spec(instrument, symbol)
        if cached:
            return cached
        return InstrumentSpec(
            instrument=instrument,
            symbol=symbol,
            status="TRADING",
            base_asset=symbol[:-4],
            quote_asset="USDT",
            margin_asset="USDT",
            contract_type="SPOT" if instrument == "SPOT" else "PERPETUAL",
            contract_multiplier=Decimal("1"),
            price_precision=2,
            quantity_precision=3,
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("100000"),
            min_notional=Decimal("5"),
            max_leverage=1 if instrument == "SPOT" else 50,
            maker_fee=self.config.default_maker_fee,
            taker_fee=self.config.default_taker_fee,
            funding_rate=self.config.default_funding_bps / Decimal("10000"),
            checked_at=self.store.clock(),
        )

    def process_signal(
        self,
        signal: dict,
        market_price: Decimal,
        market_timestamp: float,
        instrument: str = None,
        spec: InstrumentSpec = None,
        order_type: str = "MARKET",
        partial_fill_ratio: Decimal = Decimal("1"),
        environment: str = "PAPER",
    ) -> dict:
        if environment not in {"PAPER", "BACKTEST"}:
            raise InvalidData("PAPER_ENGINE_SUPPORTS_PAPER_OR_BACKTEST_ONLY")

        direction = str(signal.get("final") or signal.get("final_decision") or "WAIT").upper()
        if direction not in {"LONG", "SHORT"}:
            return {"status": "SKIPPED_WAIT", "reason": "NON_ACTIONABLE_SIGNAL"}
        if signal.get("gate_report") and not signal["gate_report"].get("approved", True):
            return {"status": "REJECTED_TECHNICAL_GATE", "reason": "V55_GATE_VETOED"}

        inst = instrument or self.config.default_instrument
        sym = str(signal["symbol"]).upper()
        spec = spec or self._default_spec(inst, sym)
        snap = self.account.sync_account(inst, environment=environment)

        sig_ts = float(signal.get("signal_timestamp") or market_timestamp)
        proposed_entry = number(signal.get("entry") or market_price, "entry")
        proposed_sl = number(signal.get("sl") or signal.get("stop_loss"), "stop_loss")
        proposed_tp = number(signal.get("tp") or signal.get("take_profit"), "take_profit")

        decision = self.risk.validate_entry(
            account=snap,
            spec=spec,
            direction=direction,
            entry_price=proposed_entry,
            stop_loss=proposed_sl,
            take_profit=proposed_tp,
            leverage=self.config.default_leverage,
            market_data_timestamp=market_timestamp,
            signal_timestamp=sig_ts,
            protective_supported=True,
            trading_mode_authorized=True,
            signal_id=str(signal.get("id") or signal.get("analysis_id") or uid()),
        )
        if not decision.approved:
            if self.notify_fn and environment == "PAPER":
                self.notify_fn(
                    kind="RISK_REJECTED",
                    title="🛡️ RISK GUARD REJECTED ENTRY",
                    lines=[
                        f"Symbol: {sym} {direction} ({environment})",
                        f"Reasons: {', '.join(decision.reason_codes)}",
                    ],
                    signal_id=str(signal.get("id") or ""),
                    dedup_key=f"risk_rej:{signal.get('id') or uid()}",
                )
            return {"status": "REJECTED_RISK", "decision": decision.to_dict()}

        sizing = decision.sizing
        long_dir = direction == "LONG"
        half_spread = (self.config.default_spread_bps / Decimal("20000"))
        slip = (self.config.default_slippage_bps / Decimal("10000"))

        if order_type == "LIMIT":
            if (long_dir and market_price > sizing.entry_price) or (not long_dir and market_price < sizing.entry_price):
                return {"status": "LIMIT_UNFILLED", "decision": decision.to_dict()}
            fill_price = round_tick(sizing.entry_price, spec.tick_size, up=long_dir)
            fee_rate = spec.maker_fee
        else:
            impact = Decimal("1") + half_spread + slip if long_dir else Decimal("1") - half_spread - slip
            fill_price = round_tick(market_price * impact, spec.tick_size, up=long_dir)
            fee_rate = spec.taker_fee

        if (long_dir and fill_price <= sizing.stop_loss) or (not long_dir and fill_price >= sizing.stop_loss):
            return {"status": "REJECTED_SLIPPAGE_CROSSED_STOP", "fill_price": fmt(fill_price)}

        ratio = min(Decimal("1"), max(Decimal("0.1"), Decimal(str(partial_fill_ratio))))
        filled_qty = (sizing.rounded_quantity * ratio // spec.step_size) * spec.step_size
        if filled_qty < spec.min_qty:
            filled_qty = sizing.rounded_quantity

        entry_fee_usdt = filled_qty * spec.contract_multiplier * fill_price * fee_rate
        pid = f"{environment.lower()[:3]}_{uid()[:16]}"
        sig_id = str(signal.get("id") or signal.get("analysis_id") or uid())
        now = self.store.clock()

        with self.store.tx() as con:
            con.execute(
                """INSERT INTO at_positions(
                   id, signal_id, strategy_version, environment, instrument, symbol, timeframe, direction,
                   signal_state, order_state, position_state, protective_order_state, account_state,
                   trailing_mode, trailing_stage, entry_price, quantity, multiplier, leverage, margin_mode,
                   initial_stop, current_stop, recommended_stop, last_confirmed_exchange_stop, take_profit,
                   best_price, entry_fee_rate, exit_fee_rate, fees_paid_usdt, initial_risk_usdt,
                   last_price, last_price_at, created_at, updated_at, details_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid, sig_id, "v5.5", environment, inst, sym, str(signal.get("timeframe", "5m/15m/1h")), direction,
                    "VALIDATED", "FILLED" if ratio == Decimal("1") else "PARTIALLY_FILLED",
                    "PROTECTED_ACTIVE", "VERIFIED", "SYNCHRONIZED",
                    "AUTOMATIC" if self.config.is_auto_trailing_authorized() else "ADVISORY", "WAITING_1R",
                    fmt(fill_price), fmt(filled_qty), fmt(spec.contract_multiplier), fmt(sizing.leverage),
                    self.config.margin_mode, fmt(sizing.stop_loss), fmt(sizing.stop_loss), fmt(sizing.stop_loss),
                    fmt(sizing.stop_loss), fmt(sizing.take_profit), fmt(fill_price),
                    fmt(fee_rate), fmt(spec.taker_fee), fmt(entry_fee_usdt), fmt(sizing.estimated_total_risk_usdt),
                    fmt(fill_price), market_timestamp, now, now,
                    dumps({"order_type": order_type, "signal_entry": fmt(proposed_entry), "slippage_bps": fmt(self.config.default_slippage_bps)}),
                ),
            )
            self.store.audit(
                "PAPER",
                "PAPER_POSITION_OPENED",
                {
                    "position_id": pid,
                    "symbol": sym,
                    "direction": direction,
                    "fill_price": fmt(fill_price),
                    "filled_qty": fmt(filled_qty),
                    "stop_loss": fmt(sizing.stop_loss),
                    "take_profit": fmt(sizing.take_profit),
                },
                environment=environment,
                signal_id=sig_id,
                position_id=pid,
                con=con,
            )

        return {
            "status": "OPENED",
            "position_id": pid,
            "position": self.store.at_position(pid),
            "decision": decision.to_dict(),
        }

    def step_candle(
        self,
        position_id: str,
        bar: dict,
        atr_val: Decimal,
        tick_size: Decimal = Decimal("0.01"),
        tie_breaker: str = "SL_FIRST_CONSERVATIVE",
        accrue_funding: bool = False,
    ) -> dict:
        pos = self.store.at_position(position_id)
        if not pos or pos["position_state"] != "PROTECTED_ACTIVE":
            return pos

        o = number(bar["open"], "open")
        h = number(bar["high"], "high")
        l = number(bar["low"], "low")
        c = number(bar["close"], "close")
        ts = float(bar["close_time"])
        long_dir = pos["direction"] == "LONG"
        sl = Decimal(str(pos["current_stop"]))
        tp = Decimal(str(pos["take_profit"])) if pos.get("take_profit") else None
        slip = self.config.default_slippage_bps / Decimal("10000")

        if accrue_funding and pos["instrument"] == "LINEAR":
            notional = Decimal(str(pos["quantity"])) * c * Decimal(str(pos["multiplier"]))
            f_cost = notional * (self.config.default_funding_bps / Decimal("10000"))
            with self.store.tx() as con:
                old_f = Decimal(str(pos["funding_paid_usdt"] or "0"))
                con.execute("UPDATE at_positions SET funding_paid_usdt=? WHERE id=?", (fmt(old_f + f_cost), position_id))
            pos = self.store.at_position(position_id)

        sl_hit = (l <= sl) if long_dir else (h >= sl)
        tp_hit = ((h >= tp) if long_dir else (l <= tp)) if tp is not None else False

        exit_reason = None
        raw_exit_px = None
        if sl_hit and tp_hit:
            if tie_breaker == "TP_FIRST_OPTIMISTIC":
                exit_reason = "TAKE_PROFIT"
                raw_exit_px = max(o, tp) if long_dir else min(o, tp)
            else:
                exit_reason = "STOP_LOSS_INTRABAR_TIE"
                raw_exit_px = min(o, sl) if long_dir else max(o, sl)
        elif sl_hit:
            exit_reason = "STOP_LOSS"
            gap_px = min(o, sl) if long_dir else max(o, sl)
            raw_exit_px = gap_px * (Decimal("1") - slip if long_dir else Decimal("1") + slip)
        elif tp_hit:
            exit_reason = "TAKE_PROFIT"
            raw_exit_px = tp

        if exit_reason is not None:
            exit_px = round_tick(raw_exit_px, tick_size, up=not long_dir)
            return self.close_paper_position(position_id, exit_px, exit_reason, closed_at=ts)

        obs = Observation(
            price=c,
            timestamp=ts,
            atr=atr_val,
            atr_timestamp=ts,
            tick=tick_size,
            reference="MARK" if pos["instrument"] == "LINEAR" else "LAST",
            interval_seconds=int(bar.get("interval_seconds", 60)),
        )
        self.trailing.evaluate_autonomous_position(position_id, obs)
        updated = self.store.at_position(position_id)
        if updated["recommended_stop"] != updated["current_stop"]:
            with self.store.tx() as con:
                con.execute(
                    "UPDATE at_positions SET current_stop=?, last_confirmed_exchange_stop=? WHERE id=?",
                    (updated["recommended_stop"], updated["recommended_stop"], position_id),
                )
        return self.store.at_position(position_id)

    def close_paper_position(self, position_id: str, exit_price: Decimal, reason: str, closed_at: float = None) -> dict:
        pos = self.store.at_position(position_id)
        if not pos or pos["position_state"] == "CLOSED":
            return pos
        env = pos["environment"]
        ts = closed_at or self.store.clock()
        entry_px = Decimal(str(pos["entry_price"]))
        qty = Decimal(str(pos["quantity"]))
        mult = Decimal(str(pos["multiplier"] or "1"))
        sign = Decimal("1") if pos["direction"] == "LONG" else Decimal("-1")

        gross = sign * (exit_price - entry_px) * qty * mult
        exit_fee = qty * mult * exit_price * Decimal(str(pos["exit_fee_rate"]))
        total_fees = Decimal(str(pos["fees_paid_usdt"] or "0")) + exit_fee
        funding = Decimal(str(pos["funding_paid_usdt"] or "0"))
        net = gross - total_fees - funding
        init_risk = Decimal(str(pos["initial_risk_usdt"] or "1"))
        r_mult = (net / init_risk) if init_risk > 0 else Decimal("0")

        with self.store.tx() as con:
            con.execute(
                """UPDATE at_positions SET
                   position_state='CLOSED', protective_order_state='CLOSED',
                   exit_price=?, exit_reason=?, fees_paid_usdt=?, gross_pnl_usdt=?,
                   net_pnl_usdt=?, r_multiple=?, last_price=?, last_price_at=?,
                   closed_at=?, updated_at=?
                   WHERE id=?""",
                (
                    fmt(exit_price), reason, fmt(total_fees), fmt(gross),
                    fmt(net), fmt(r_mult), fmt(exit_price), ts, ts, ts, position_id,
                ),
            )
            self.risk.record_trade_outcome(env, gross, net, total_fees, funding, con=con)
            self.store.audit(
                "PAPER",
                "PAPER_POSITION_CLOSED",
                {
                    "position_id": position_id,
                    "symbol": pos["symbol"],
                    "direction": pos["direction"],
                    "exit_price": fmt(exit_price),
                    "reason": reason,
                    "gross_pnl_usdt": fmt(gross),
                    "net_pnl_usdt": fmt(net),
                    "r_multiple": fmt(r_mult),
                },
                environment=env,
                signal_id=pos["signal_id"],
                position_id=position_id,
                con=con,
            )
        return self.store.at_position(position_id)


def compute_environment_performance(store: Store, environment: str, initial_equity: Decimal = Decimal("10000")) -> dict:
    if environment not in {"BACKTEST", "PAPER", "TESTNET", "LIVE"}:
        raise InvalidData("INVALID_PERFORMANCE_ENVIRONMENT")

    closed = store.rows(
        "SELECT * FROM at_positions WHERE environment=? AND position_state='CLOSED' ORDER BY closed_at ASC, created_at ASC",
        (environment,),
    )
    open_rows = store.rows(
        "SELECT * FROM at_positions WHERE environment=? AND position_state IN ('OPENING','PROTECTED_ACTIVE','PROTECTION_REQUIRED')",
        (environment,),
    )

    sample_size = len(closed)
    wins = [r for r in closed if Decimal(str(r["net_pnl_usdt"] or "0")) > 0]
    losses = [r for r in closed if Decimal(str(r["net_pnl_usdt"] or "0")) <= 0]

    gross_profit = sum((Decimal(str(r["net_pnl_usdt"])) for r in wins), Decimal("0"))
    gross_loss = abs(sum((Decimal(str(r["net_pnl_usdt"])) for r in losses), Decimal("0")))
    total_net = gross_profit - gross_loss
    total_fees = sum((Decimal(str(r["fees_paid_usdt"] or "0")) for r in closed), Decimal("0"))

    win_rate = (Decimal(len(wins)) / Decimal(sample_size) * Decimal("100")) if sample_size > 0 else Decimal("0")
    avg_win = (gross_profit / Decimal(len(wins))) if wins else Decimal("0")
    avg_loss = (gross_loss / Decimal(len(losses))) if losses else Decimal("0")
    expectancy_usdt = (total_net / Decimal(sample_size)) if sample_size > 0 else Decimal("0")
    avg_r = (sum((Decimal(str(r["r_multiple"] or "0")) for r in closed), Decimal("0")) / Decimal(sample_size)) if sample_size > 0 else Decimal("0")
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit > 0 else Decimal("0"))

    equity = initial_equity
    peak = initial_equity
    max_dd_usdt = Decimal("0")
    max_dd_pct = Decimal("0")
    for r in closed:
        equity += Decimal(str(r["net_pnl_usdt"] or "0"))
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd_usdt:
            max_dd_usdt = dd
        if peak > 0 and (dd / peak * Decimal("100")) > max_dd_pct:
            max_dd_pct = dd / peak * Decimal("100")

    by_direction = {}
    for d in ("LONG", "SHORT"):
        sub = [r for r in closed if r["direction"] == d]
        sub_wins = [r for r in sub if Decimal(str(r["net_pnl_usdt"] or "0")) > 0]
        sub_net = sum((Decimal(str(r["net_pnl_usdt"] or "0")) for r in sub), Decimal("0"))
        by_direction[d] = {
            "count": len(sub),
            "wins": len(sub_wins),
            "win_rate_pct": float(round(Decimal(len(sub_wins)) / Decimal(len(sub)) * 100, 2)) if sub else 0.0,
            "net_pnl_usdt": fmt(sub_net),
        }

    by_timeframe = {}
    for r in closed:
        tf = r.get("timeframe") or "5m/15m/1h"
        bucket = by_timeframe.setdefault(tf, {"count": 0, "wins": 0, "net_pnl_usdt": Decimal("0")})
        bucket["count"] += 1
        pnl = Decimal(str(r["net_pnl_usdt"] or "0"))
        if pnl > 0:
            bucket["wins"] += 1
        bucket["net_pnl_usdt"] += pnl
    for tf, b in by_timeframe.items():
        b["win_rate_pct"] = float(round(Decimal(b["wins"]) / Decimal(b["count"]) * 100, 2)) if b["count"] else 0.0
        b["net_pnl_usdt"] = fmt(b["net_pnl_usdt"])

    return {
        "environment": environment,
        "sample_size": sample_size,
        "open_positions_count": len(open_rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": float(round(win_rate, 2)),
        "net_expectancy_usdt": fmt(round(expectancy_usdt, 4)),
        "net_expectancy_r": fmt(round(avg_r, 4)),
        "profit_factor": float(round(profit_factor, 4)) if profit_factor is not None else None,
        "max_drawdown_usdt": fmt(round(max_dd_usdt, 4)),
        "max_drawdown_pct": float(round(max_dd_pct, 2)),
        "avg_win_usdt": fmt(round(avg_win, 4)),
        "avg_loss_usdt": fmt(round(avg_loss, 4)),
        "total_net_pnl_usdt": fmt(round(total_net, 4)),
        "total_fees_usdt": fmt(round(total_fees, 4)),
        "current_equity_usdt": fmt(round(equity, 4)),
        "by_direction": by_direction,
        "by_timeframe": by_timeframe,
        "statistical_notice": "INSUFFICIENT_SAMPLE (<30 trades)" if sample_size < 30 else "SUFFICIENT_SAMPLE",
    }


def run_chronological_backtest(
    paper_engine: AutonomousPaperEngine,
    signals_and_bars: list,
    tie_breaker: str = "SL_FIRST_CONSERVATIVE",
) -> dict:
    store = paper_engine.store
    orig_clock = store.clock
    try:
        for item in sorted(signals_and_bars, key=lambda x: float(x["timestamp"])):
            ts = float(item["timestamp"])
            store.clock = lambda _t=ts: _t
            res = paper_engine.process_signal(
                signal=item["signal"],
                market_price=Decimal(str(item["entry_price"])),
                market_timestamp=ts,
                environment="BACKTEST",
            )
            if res.get("status") == "OPENED":
                pid = res["position_id"]
                atr_val = Decimal(str(item.get("atr", "2")))
                for bar in item.get("bars", []):
                    if float(bar["close_time"]) <= ts:
                        continue
                    b_ts = float(bar["close_time"])
                    store.clock = lambda _bt=b_ts: _bt
                    pos_now = paper_engine.step_candle(pid, bar, atr_val, tie_breaker=tie_breaker)
                    if pos_now["position_state"] == "CLOSED":
                        break
    finally:
        store.clock = orig_clock

    perf = compute_environment_performance(store, "BACKTEST", paper_engine.config.paper_initial_equity)
    paper_perf = compute_environment_performance(store, "PAPER", paper_engine.config.paper_initial_equity)
    perf["paper_vs_backtest_comparison"] = {
        "backtest_sample_size": perf["sample_size"],
        "paper_sample_size": paper_perf["sample_size"],
        "backtest_net_expectancy_usdt": perf["net_expectancy_usdt"],
        "paper_net_expectancy_usdt": paper_perf["net_expectancy_usdt"],
        "backtest_win_rate_pct": perf["win_rate_pct"],
        "paper_win_rate_pct": paper_perf["win_rate_pct"],
    }
    return perf

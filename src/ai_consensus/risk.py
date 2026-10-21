"""
Deterministic risk engine, TradePlan & position sizing, fee/spread/slippage cost model,
atomic concurrent portfolio exposure reservation, and spot-reference synthetic SHORT vs futures
instrument semantics for AI Consensus Trading (PH04-03 / F06, F16, F38, T05, T19 / Milestone M4).
Strictly zero UI, DB, or network side-effects on import.
"""

import math
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .domain import (
    DecisionAction,
    GateReport,
    TradePlan,
    is_finite_number,
    is_valid_confidence,
    preflight_market_check,
    validate_trade_plan_gate,
)

RISK_ENGINE_VERSION: str = "v6.1-deterministic-risk-1"


@dataclass(frozen=True)
class AccountRiskConfig:
    """Immutable configuration for deterministic position sizing and portfolio exposure caps (F06, T19)."""
    initial_equity: float = 10_000.0
    risk_fraction_per_trade: float = 0.005        # 0.5% of equity per trade ($50 on $10,000)
    max_total_risk_fraction: float = 0.020        # 2.0% max combined open risk across portfolio
    max_position_exposure_pct: float = 0.25       # Max 25% of equity notional in a single position
    max_total_exposure_pct: float = 0.80          # Max 80% of equity total notional across open positions
    max_open_positions: int = 4                   # Max concurrent open paper positions
    max_positions_per_symbol: int = 1             # Block duplicate open exposure on same symbol
    entry_fee_pct: float = 0.04                   # 0.04% (4 bps) entry fee
    exit_fee_pct: float = 0.04                    # 0.04% (4 bps) exit fee
    slippage_pct_per_leg: float = 0.02            # 0.02% (2 bps) slippage per leg (0.04% round-trip)
    min_net_rr: float = 1.5                       # Minimum required net R:R after all costs
    max_spread_pct: float = 0.35                  # Maximum allowed order-book spread (%)
    max_freshness_seconds: float = 300.0          # Maximum allowed closed-bar staleness when enforced
    instrument_mode: str = "spot_reference"       # "spot_reference" | "futures_perp"
    leverage: float = 1.0                         # 1.0x for spot_reference; configurable for futures_perp
    cap_position_to_exposure_limit: bool = True   # Clamp quantity to exposure cap if positive, else veto

    @property
    def round_trip_fee_pct(self) -> float:
        return max(0.0, float(self.entry_fee_pct)) + max(0.0, float(self.exit_fee_pct))

    @property
    def round_trip_slippage_pct(self) -> float:
        return 2.0 * max(0.0, float(self.slippage_pct_per_leg))


@dataclass(frozen=True)
class InstrumentSemantics:
    """Explicitly distinguishes spot-reference synthetic SHORT from futures contracts (PH04-03 / F06)."""
    direction: str
    instrument_mode: str
    instrument_type: str
    execution_semantics: str
    is_synthetic_spot_short: bool
    leverage: float
    margin_model: str
    funding_modeled: bool
    liquidation_modeled: bool
    disclaimer: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "instrument_mode": self.instrument_mode,
            "instrument_type": self.instrument_type,
            "execution_semantics": self.execution_semantics,
            "is_synthetic_spot_short": self.is_synthetic_spot_short,
            "leverage": self.leverage,
            "margin_model": self.margin_model,
            "funding_modeled": self.funding_modeled,
            "liquidation_modeled": self.liquidation_modeled,
            "disclaimer": self.disclaimer,
        }


def resolve_instrument_semantics(
    direction: str,
    instrument_mode: str = "spot_reference",
    leverage: float = 1.0,
) -> InstrumentSemantics:
    dir_norm = str(direction or "WAIT").upper().strip()
    mode_norm = str(instrument_mode or "spot_reference").lower().strip()
    lev = max(1.0, float(leverage or 1.0)) if is_finite_number(leverage) else 1.0

    if mode_norm == "futures_perp":
        return InstrumentSemantics(
            direction=dir_norm,
            instrument_mode="futures_perp",
            instrument_type="FUTURES_PERP_PAPER",
            execution_semantics=f"LINEAR_USDT_PERP_{dir_norm}_PAPER",
            is_synthetic_spot_short=False,
            leverage=lev,
            margin_model="ISOLATED_MARGIN_PAPER",
            funding_modeled=False,
            liquidation_modeled=False,
            disclaimer=(
                f"Simulasi paper kontrak perpetual futures USDT ({lev:.1f}x isolated margin); "
                "funding rate dan likuidasi paksa bursa tidak dimodelkan."
            ),
        )

    if dir_norm == "SHORT":
        return InstrumentSemantics(
            direction="SHORT",
            instrument_mode="spot_reference",
            instrument_type="SPOT_SYNTHETIC_SHORT_PAPER",
            execution_semantics="SYNTHETIC_SHORT_SPOT_REFERENCE",
            is_synthetic_spot_short=True,
            leverage=1.0,
            margin_model="UNLEVERAGED_1X_NOTIONAL_RESERVE",
            funding_modeled=False,
            liquidation_modeled=False,
            disclaimer=(
                "Paper SHORT sintetis berbasis harga referensi Spot Binance (1x notional penuh, "
                "bukan kontrak futures; tidak memodelkan bunga pinjaman margin, funding rate, atau likuidasi)."
            ),
        )
    if dir_norm == "LONG":
        return InstrumentSemantics(
            direction="LONG",
            instrument_mode="spot_reference",
            instrument_type="SPOT_LONG_PAPER",
            execution_semantics="SPOT_LONG_REFERENCE",
            is_synthetic_spot_short=False,
            leverage=1.0,
            margin_model="UNLEVERAGED_1X_CASH_RESERVE",
            funding_modeled=False,
            liquidation_modeled=False,
            disclaimer="Paper LONG berbasis harga referensi Spot Binance (1x kas penuh tanpa leverage).",
        )
    return InstrumentSemantics(
        direction="WAIT",
        instrument_mode="spot_reference",
        instrument_type="NO_POSITION",
        execution_semantics="OBSERVATION_ONLY",
        is_synthetic_spot_short=False,
        leverage=1.0,
        margin_model="NONE",
        funding_modeled=False,
        liquidation_modeled=False,
        disclaimer="Observasi WAIT — tidak ada alokasi modal atau posisi paper.",
    )


@dataclass(frozen=True)
class PositionSizingResult:
    """
    Complete deterministic TradePlan + position sizing + cost & accounting reconciliation (PH04-03 / F06 / T19).
    """
    valid: bool
    direction: str
    symbol: str
    entry: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    equity: float
    free_equity_before: float
    risk_fraction: float
    risk_budget: float
    stop_distance: float
    target_distance: float
    fee_pct_round_trip: float
    slippage_pct_round_trip: float
    spread_pct: float
    total_cost_pct_round_trip: float
    unit_cost: float
    unit_risk_gross: float
    unit_risk_net: float
    unit_reward_gross: float
    unit_reward_net: float
    raw_quantity_no_cost: float
    quantity_by_risk: float
    max_quantity_by_position_cap: float
    max_quantity_by_total_cap: float
    position_size: float
    quantity: float
    notional_value: float
    margin_required: float
    estimated_fees_total: float
    estimated_slippage_and_spread_total: float
    estimated_total_cost: float
    gross_loss_at_sl: float
    max_loss_net: float
    gross_profit_at_tp: float
    net_profit_at_tp: float
    gross_rr: float
    net_rr: float
    capped_by_exposure: bool
    accounting_reconciled: bool
    instrument: InstrumentSemantics
    veto_codes: Tuple[str, ...] = field(default_factory=tuple)
    veto_summary: str = "NONE"
    risk_engine_version: str = RISK_ENGINE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "direction": self.direction,
            "symbol": self.symbol,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "equity": self.equity,
            "free_equity_before": self.free_equity_before,
            "risk_fraction": self.risk_fraction,
            "risk_budget": self.risk_budget,
            "stop_distance": self.stop_distance,
            "target_distance": self.target_distance,
            "fee_pct_round_trip": self.fee_pct_round_trip,
            "slippage_pct_round_trip": self.slippage_pct_round_trip,
            "spread_pct": self.spread_pct,
            "total_cost_pct_round_trip": self.total_cost_pct_round_trip,
            "unit_cost": self.unit_cost,
            "unit_risk_gross": self.unit_risk_gross,
            "unit_risk_net": self.unit_risk_net,
            "unit_reward_gross": self.unit_reward_gross,
            "unit_reward_net": self.unit_reward_net,
            "raw_quantity_no_cost": self.raw_quantity_no_cost,
            "quantity_by_risk": self.quantity_by_risk,
            "max_quantity_by_position_cap": self.max_quantity_by_position_cap,
            "max_quantity_by_total_cap": self.max_quantity_by_total_cap,
            "position_size": self.position_size,
            "quantity": self.quantity,
            "notional_value": self.notional_value,
            "margin_required": self.margin_required,
            "estimated_fees_total": self.estimated_fees_total,
            "estimated_slippage_and_spread_total": self.estimated_slippage_and_spread_total,
            "estimated_total_cost": self.estimated_total_cost,
            "gross_loss_at_sl": self.gross_loss_at_sl,
            "max_loss_net": self.max_loss_net,
            "gross_profit_at_tp": self.gross_profit_at_tp,
            "net_profit_at_tp": self.net_profit_at_tp,
            "gross_rr": self.gross_rr,
            "net_rr": self.net_rr,
            "capped_by_exposure": self.capped_by_exposure,
            "accounting_reconciled": self.accounting_reconciled,
            "instrument": self.instrument.to_dict(),
            "veto_codes": list(self.veto_codes),
            "veto_summary": self.veto_summary,
            "risk_engine_version": self.risk_engine_version,
        }


def compute_position_sizing(
    direction: str,
    entry: Any,
    stop_loss: Any,
    take_profit: Any,
    *,
    symbol: str = "UNKNOWN",
    equity: float = 10_000.0,
    risk_fraction: float = 0.005,
    fee_pct_round_trip: Optional[float] = None,
    slippage_pct_round_trip: Optional[float] = None,
    spread_pct: float = 0.0,
    max_position_exposure_pct: float = 1.0,
    max_total_exposure_pct: float = 1.0,
    current_open_notional: float = 0.0,
    current_open_risk: float = 0.0,
    max_total_risk_fraction: float = 1.0,
    current_open_positions: int = 0,
    max_open_positions: int = 100,
    open_symbols: Optional[List[str]] = None,
    min_net_rr: float = 1.5,
    instrument_mode: str = "spot_reference",
    leverage: float = 1.0,
    cap_position_to_exposure_limit: bool = True,
) -> PositionSizingResult:
    dir_norm = str(direction or "WAIT").upper().strip()
    inst = resolve_instrument_semantics(dir_norm, instrument_mode=instrument_mode, leverage=leverage)
    fee_rt = 0.08 if fee_pct_round_trip is None else float(fee_pct_round_trip)
    slip_rt = 0.04 if slippage_pct_round_trip is None else float(slippage_pct_round_trip)

    def _invalid(codes: List[str]) -> PositionSizingResult:
        dedup = list(dict.fromkeys(codes))
        return PositionSizingResult(
            valid=False,
            direction=dir_norm if dir_norm in {"LONG", "SHORT"} else "WAIT",
            symbol=symbol,
            entry=float(entry) if is_finite_number(entry) else None,
            stop_loss=float(stop_loss) if is_finite_number(stop_loss) else None,
            take_profit=float(take_profit) if is_finite_number(take_profit) else None,
            equity=float(equity) if is_finite_number(equity) else 0.0,
            free_equity_before=0.0,
            risk_fraction=float(risk_fraction) if is_finite_number(risk_fraction) else 0.0,
            risk_budget=0.0,
            stop_distance=0.0,
            target_distance=0.0,
            fee_pct_round_trip=fee_rt if is_finite_number(fee_rt) else 0.0,
            slippage_pct_round_trip=slip_rt if is_finite_number(slip_rt) else 0.0,
            spread_pct=float(spread_pct) if is_finite_number(spread_pct) else 0.0,
            total_cost_pct_round_trip=0.0,
            unit_cost=0.0,
            unit_risk_gross=0.0,
            unit_risk_net=0.0,
            unit_reward_gross=0.0,
            unit_reward_net=0.0,
            raw_quantity_no_cost=0.0,
            quantity_by_risk=0.0,
            max_quantity_by_position_cap=0.0,
            max_quantity_by_total_cap=0.0,
            position_size=0.0,
            quantity=0.0,
            notional_value=0.0,
            margin_required=0.0,
            estimated_fees_total=0.0,
            estimated_slippage_and_spread_total=0.0,
            estimated_total_cost=0.0,
            gross_loss_at_sl=0.0,
            max_loss_net=0.0,
            gross_profit_at_tp=0.0,
            net_profit_at_tp=0.0,
            gross_rr=0.0,
            net_rr=0.0,
            capped_by_exposure=False,
            accounting_reconciled=True,
            instrument=inst,
            veto_codes=tuple(dedup),
            veto_summary=", ".join(dedup) if dedup else "INVALID_TRADE_PLAN",
        )

    if dir_norm not in {DecisionAction.LONG.value, DecisionAction.SHORT.value}:
        return _invalid(["WAIT_DECISION"])

    for val in (
        entry,
        stop_loss,
        take_profit,
        equity,
        risk_fraction,
        fee_rt,
        slippage_pct_round_trip if slippage_pct_round_trip is not None else slip_rt,
        spread_pct,
        max_position_exposure_pct,
        max_total_exposure_pct,
        current_open_notional,
        current_open_risk,
        max_total_risk_fraction,
    ):
        if not is_finite_number(val):
            return _invalid(["INVALID_NUMERIC"])

    entry_f = float(entry)
    sl_f = float(stop_loss)
    tp_f = float(take_profit)
    eq_f = float(equity)
    rf_f = float(risk_fraction)
    spread_f = float(spread_pct)

    vetoes: List[str] = []
    if entry_f <= 0.0 or sl_f <= 0.0 or tp_f <= 0.0:
        vetoes.append("NON_POSITIVE_PRICE")
        vetoes.append("INVALID_SL_TP")
        return _invalid(vetoes)

    if eq_f <= 0.0 or rf_f <= 0.0 or rf_f > 1.0:
        return _invalid(["INVALID_ACCOUNT_RISK_PARAMS"])

    if fee_rt < 0.0 or slip_rt < 0.0 or spread_f < 0.0:
        return _invalid(["NEGATIVE_COST_PARAMS"])

    if dir_norm == "LONG" and not (sl_f < entry_f < tp_f):
        return _invalid(["INVALID_SL_TP"])
    if dir_norm == "SHORT" and not (tp_f < entry_f < sl_f):
        return _invalid(["INVALID_SL_TP"])

    stop_dist = abs(entry_f - sl_f)
    target_dist = abs(tp_f - entry_f)
    if stop_dist <= 1e-12:
        return _invalid(["ZERO_STOP_DISTANCE", "INVALID_SL_TP"])

    risk_budget = eq_f * rf_f
    total_cost_pct = fee_rt + slip_rt + spread_f
    unit_fee = entry_f * (fee_rt / 100.0)
    unit_slip_spread = entry_f * ((slip_rt + spread_f) / 100.0)
    unit_cost = unit_fee + unit_slip_spread

    unit_risk_gross = stop_dist
    unit_risk_net = stop_dist + unit_cost
    unit_reward_gross = target_dist
    unit_reward_net = target_dist - unit_cost

    gross_rr = unit_reward_gross / unit_risk_gross
    net_rr = (unit_reward_net / unit_risk_net) if unit_risk_net > 0.0 else 0.0

    if net_rr < float(min_net_rr):
        vetoes.append("INSUFFICIENT_NET_RR")

    if int(current_open_positions) >= int(max_open_positions):
        vetoes.append("MAX_CONCURRENT_POSITIONS_EXCEEDED")

    if open_symbols and symbol in [str(s).upper().strip() for s in open_symbols]:
        vetoes.append("DUPLICATE_SYMBOL_EXPOSURE")

    max_total_risk_budget = eq_f * float(max_total_risk_fraction)
    remaining_risk_budget = max(0.0, max_total_risk_budget - float(current_open_risk))
    if remaining_risk_budget < risk_budget - 1e-9:
        vetoes.append("TOTAL_RISK_BUDGET_EXCEEDED")

    effective_risk_budget = min(risk_budget, remaining_risk_budget) if remaining_risk_budget > 0 else 0.0
    raw_qty_no_cost = risk_budget / unit_risk_gross
    qty_by_risk = effective_risk_budget / unit_risk_net if unit_risk_net > 0 else 0.0

    max_pos_notional = eq_f * float(max_position_exposure_pct)
    max_tot_notional = eq_f * float(max_total_exposure_pct)
    free_equity_before = max(0.0, eq_f - float(current_open_notional))
    remaining_tot_notional = max(0.0, min(free_equity_before * inst.leverage, max_tot_notional - float(current_open_notional)))

    max_qty_pos_cap = max_pos_notional / entry_f
    max_qty_tot_cap = remaining_tot_notional / entry_f

    if remaining_tot_notional <= 1e-9:
        vetoes.append("MAX_TOTAL_EXPOSURE_EXCEEDED")
        vetoes.append("INSUFFICIENT_FREE_EQUITY")

    capped_by_exp = False
    if qty_by_risk > max_qty_pos_cap + 1e-12:
        capped_by_exp = True
        if not cap_position_to_exposure_limit:
            vetoes.append("MAX_POSITION_EXPOSURE_EXCEEDED")

    if qty_by_risk > max_qty_tot_cap + 1e-12:
        capped_by_exp = True
        if not cap_position_to_exposure_limit or max_qty_tot_cap <= 1e-12:
            if "MAX_TOTAL_EXPOSURE_EXCEEDED" not in vetoes:
                vetoes.append("MAX_TOTAL_EXPOSURE_EXCEEDED")

    if vetoes:
        final_qty = 0.0
    else:
        final_qty = min(qty_by_risk, max_qty_pos_cap, max_qty_tot_cap)
        if final_qty <= 0.0:
            vetoes.append("ZERO_POSITION_SIZE")
            final_qty = 0.0

    notional_val = final_qty * entry_f
    margin_req = notional_val / max(1.0, inst.leverage)
    est_fees = final_qty * unit_fee
    est_slip_spread = final_qty * unit_slip_spread
    est_total_cost = final_qty * unit_cost
    gross_loss_sl = final_qty * unit_risk_gross
    max_loss_net_val = final_qty * unit_risk_net
    gross_profit_tp = final_qty * unit_reward_gross
    net_profit_tp = final_qty * unit_reward_net

    id_loss_ok = math.isclose(max_loss_net_val, gross_loss_sl + est_total_cost, rel_tol=1e-9, abs_tol=1e-9)
    id_profit_ok = math.isclose(net_profit_tp, gross_profit_tp - est_total_cost, rel_tol=1e-9, abs_tol=1e-9)
    id_budget_ok = max_loss_net_val <= risk_budget + 1e-7
    id_notional_ok = (margin_req <= free_equity_before + 1e-7) if final_qty > 0 else True
    reconciled = bool(id_loss_ok and id_profit_ok and id_budget_ok and id_notional_ok)

    is_valid = len(vetoes) == 0 and final_qty > 0.0 and reconciled
    veto_sum = ", ".join(vetoes) if vetoes else "NONE"

    return PositionSizingResult(
        valid=is_valid,
        direction=dir_norm,
        symbol=symbol,
        entry=round(entry_f, 8),
        stop_loss=round(sl_f, 8),
        take_profit=round(tp_f, 8),
        equity=round(eq_f, 4),
        free_equity_before=round(free_equity_before, 4),
        risk_fraction=round(rf_f, 6),
        risk_budget=round(risk_budget, 4),
        stop_distance=round(stop_dist, 8),
        target_distance=round(target_dist, 8),
        fee_pct_round_trip=round(fee_rt, 4),
        slippage_pct_round_trip=round(slip_rt, 4),
        spread_pct=round(spread_f, 4),
        total_cost_pct_round_trip=round(total_cost_pct, 4),
        unit_cost=round(unit_cost, 8),
        unit_risk_gross=round(unit_risk_gross, 8),
        unit_risk_net=round(unit_risk_net, 8),
        unit_reward_gross=round(unit_reward_gross, 8),
        unit_reward_net=round(unit_reward_net, 8),
        raw_quantity_no_cost=round(raw_qty_no_cost, 8),
        quantity_by_risk=round(qty_by_risk, 8),
        max_quantity_by_position_cap=round(max_qty_pos_cap, 8),
        max_quantity_by_total_cap=round(max_qty_tot_cap, 8),
        position_size=round(final_qty, 8),
        quantity=round(final_qty, 8),
        notional_value=round(notional_val, 4),
        margin_required=round(margin_req, 4),
        estimated_fees_total=round(est_fees, 4),
        estimated_slippage_and_spread_total=round(est_slip_spread, 4),
        estimated_total_cost=round(est_total_cost, 4),
        gross_loss_at_sl=round(gross_loss_sl, 4),
        max_loss_net=round(max_loss_net_val, 4),
        gross_profit_at_tp=round(gross_profit_tp, 4),
        net_profit_at_tp=round(net_profit_tp, 4),
        gross_rr=round(gross_rr, 4),
        net_rr=round(net_rr, 4),
        capped_by_exposure=capped_by_exp,
        accounting_reconciled=reconciled,
        instrument=inst,
        veto_codes=tuple(vetoes),
        veto_summary=veto_sum,
    )


class PaperPortfolioLedger:
    """
    Thread-safe atomic portfolio risk & exposure reservation ledger (PH04-03 / F06 / T19).
    """

    def __init__(self, config: Optional[AccountRiskConfig] = None) -> None:
        self._lock = threading.Lock()
        self.config = config or AccountRiskConfig()
        self.equity: float = float(self.config.initial_equity)
        self.open_positions: Dict[str, Dict[str, Any]] = {}

    def reset(self, config: Optional[AccountRiskConfig] = None) -> None:
        with self._lock:
            if config is not None:
                self.config = config
            self.equity = float(self.config.initial_equity)
            self.open_positions.clear()

    def _totals_unlocked(self) -> Tuple[float, float, float, List[str]]:
        open_notional = sum(float(p["notional_value"]) for p in self.open_positions.values())
        open_margin = sum(float(p["margin_required"]) for p in self.open_positions.values())
        open_risk = sum(float(p["max_loss_net"]) for p in self.open_positions.values())
        symbols = [str(p["symbol"]).upper().strip() for p in self.open_positions.values()]
        return open_notional, open_margin, open_risk, symbols

    def evaluate_and_reserve(
        self,
        *,
        symbol: str,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        spread_pct: float = 0.0,
        fee_pct_round_trip: Optional[float] = None,
        slippage_pct_round_trip: Optional[float] = None,
        risk_fraction: Optional[float] = None,
        position_id: Optional[str] = None,
        allow_same_symbol: bool = False,
        cap_position_to_exposure_limit: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            cfg = self.config
            open_notional, open_margin, open_risk, open_syms = self._totals_unlocked()
            sizing = compute_position_sizing(
                direction=direction,
                entry=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
                symbol=symbol,
                equity=self.equity,
                risk_fraction=cfg.risk_fraction_per_trade if risk_fraction is None else float(risk_fraction),
                fee_pct_round_trip=cfg.round_trip_fee_pct if fee_pct_round_trip is None else float(fee_pct_round_trip),
                slippage_pct_round_trip=cfg.round_trip_slippage_pct if slippage_pct_round_trip is None else float(slippage_pct_round_trip),
                spread_pct=spread_pct,
                max_position_exposure_pct=cfg.max_position_exposure_pct,
                max_total_exposure_pct=cfg.max_total_exposure_pct,
                current_open_notional=open_notional,
                current_open_risk=open_risk,
                max_total_risk_fraction=cfg.max_total_risk_fraction,
                current_open_positions=len(self.open_positions),
                max_open_positions=cfg.max_open_positions,
                open_symbols=[] if allow_same_symbol else open_syms,
                min_net_rr=cfg.min_net_rr,
                instrument_mode=cfg.instrument_mode,
                leverage=cfg.leverage,
                cap_position_to_exposure_limit=(
                    cfg.cap_position_to_exposure_limit
                    if cap_position_to_exposure_limit is None
                    else bool(cap_position_to_exposure_limit)
                ),
            )
            if not sizing.valid:
                return {
                    "reserved": False,
                    "position_id": None,
                    "sizing": sizing.to_dict(),
                    "veto_codes": list(sizing.veto_codes),
                    "account": self._snapshot_unlocked(),
                }

            pid = position_id or f"pos_{symbol}_{uuid.uuid4().hex[:8]}"
            rec = {
                "position_id": pid,
                "symbol": str(symbol).upper().strip(),
                "direction": sizing.direction,
                "quantity": sizing.quantity,
                "entry": sizing.entry,
                "stop_loss": sizing.stop_loss,
                "take_profit": sizing.take_profit,
                "notional_value": sizing.notional_value,
                "margin_required": sizing.margin_required,
                "max_loss_net": sizing.max_loss_net,
                "instrument_type": sizing.instrument.instrument_type,
            }
            self.open_positions[pid] = rec
            return {
                "reserved": True,
                "position_id": pid,
                "sizing": sizing.to_dict(),
                "veto_codes": [],
                "account": self._snapshot_unlocked(),
            }

    def release_position(self, position_id: str, realized_net_pnl: float = 0.0) -> bool:
        with self._lock:
            if position_id not in self.open_positions:
                return False
            self.open_positions.pop(position_id, None)
            if is_finite_number(realized_net_pnl):
                self.equity = round(max(0.0, self.equity + float(realized_net_pnl)), 4)
            return True

    def _snapshot_unlocked(self) -> Dict[str, Any]:
        open_notional, open_margin, open_risk, open_syms = self._totals_unlocked()
        free_equity = round(max(0.0, self.equity - open_margin), 4)
        reconciled = math.isclose(free_equity + open_margin, self.equity, rel_tol=1e-9, abs_tol=1e-6)
        return {
            "equity": round(self.equity, 4),
            "reserved_notional": round(open_notional, 4),
            "reserved_margin": round(open_margin, 4),
            "free_equity": free_equity,
            "total_open_risk": round(open_risk, 4),
            "max_total_risk_budget": round(self.equity * self.config.max_total_risk_fraction, 4),
            "max_total_notional": round(self.equity * self.config.max_total_exposure_pct, 4),
            "open_positions_count": len(self.open_positions),
            "max_open_positions": self.config.max_open_positions,
            "open_symbols": open_syms,
            "accounting_identity_ok": reconciled,
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()


GLOBAL_RISK_LEDGER = PaperPortfolioLedger()


def build_trade_plan_and_sizing(
    packet: Dict[str, Any],
    direction: str,
    *,
    equity: float = 10_000.0,
    risk_fraction: float = 0.005,
    fee_pct_round_trip: float = 0.08,
    slippage_pct_round_trip: float = 0.04,
    max_position_exposure_pct: float = 0.25,
    max_total_exposure_pct: float = 0.80,
    current_open_notional: float = 0.0,
    current_open_risk: float = 0.0,
    max_total_risk_fraction: float = 0.02,
    current_open_positions: int = 0,
    max_open_positions: int = 4,
    open_symbols: Optional[List[str]] = None,
    min_net_rr: float = 1.5,
    instrument_mode: str = "spot_reference",
) -> Dict[str, Any]:
    """
    Build candidate SL/entry/TP levels from packet + SMC zones and compute full deterministic
    PositionSizingResult and TradePlan (PH04-03 / F06 / F38 / T19).
    Ensures SL distance and TP distance account for round-trip fees, slippage, and spread so valid
    directional decisions never fail INSUFFICIENT_NET_RR due to tight ATR.
    """
    symbol = str((packet or {}).get("symbol", "UNKNOWN"))
    dir_norm = str(direction or "WAIT").upper().strip()
    if dir_norm not in {"LONG", "SHORT"} or not isinstance(packet, dict):
        sizing = compute_position_sizing("WAIT", None, None, None, symbol=symbol, equity=equity, risk_fraction=risk_fraction, instrument_mode=instrument_mode)
        return {
            "entry": None,
            "sl": None,
            "tp": None,
            "gross_rr": 0.0,
            "net_rr": 0.0,
            "position_size": 0.0,
            "notional_value": 0.0,
            "risk_budget": round(float(equity) * float(risk_fraction), 4),
            "sizing": sizing.to_dict(),
            "veto_codes": list(sizing.veto_codes),
        }

    current = packet.get("price")
    tf15 = packet.get("timeframes", {}).get("15m", {})
    atr15 = tf15.get("atr")
    if not is_finite_number(current) or not is_finite_number(atr15) or float(current) <= 0 or float(atr15) <= 0:
        sizing = compute_position_sizing(dir_norm, current, None, None, symbol=symbol, equity=equity, risk_fraction=risk_fraction, instrument_mode=instrument_mode)
        return {
            "entry": None,
            "sl": None,
            "tp": None,
            "gross_rr": 0.0,
            "net_rr": 0.0,
            "position_size": 0.0,
            "notional_value": 0.0,
            "risk_budget": 0.0,
            "sizing": sizing.to_dict(),
            "veto_codes": list(sizing.veto_codes),
        }

    current_f = float(current)
    atr15_f = float(atr15)
    smc = packet.get("smc", {})
    spread_pct = max(0.0, float(packet.get("order_book", {}).get("spread", 0.0) or 0.0))
    total_cost_pct = max(0.0, float(fee_pct_round_trip)) + max(0.0, float(slippage_pct_round_trip)) + spread_pct
    unit_cost_est = current_f * (total_cost_pct / 100.0)

    min_risk_dist = max(0.55 * atr15_f, current_f * 0.0045, unit_cost_est * 4.5)
    max_risk_dist = max(2.5 * atr15_f, current_f * 0.03, min_risk_dist * 1.5)
    target_net_rr = max(float(min_net_rr) + 0.30, 1.85)

    if dir_norm == "LONG":
        zone = smc.get("bullish_entry", {})
        z_low = zone.get("zone_low")
        raw_anchor = (
            float(z_low) - 0.10 * atr15_f
            if (is_finite_number(z_low) and 0 < float(z_low) < current_f)
            else current_f - 1.10 * atr15_f
        )
        raw_dist = max(current_f - raw_anchor, 0.35 * atr15_f)
        risk_dist = min(max(raw_dist, min_risk_dist), max_risk_dist)
        sl_val = max(current_f * 0.05, current_f - risk_dist)
        risk_dist = current_f - sl_val
        reward_dist = max(2.0 * risk_dist, target_net_rr * (risk_dist + unit_cost_est) + unit_cost_est)
        tp_val = current_f + reward_dist
    else:
        zone = smc.get("bearish_entry", {})
        z_high = zone.get("zone_high")
        raw_anchor = (
            float(z_high) + 0.10 * atr15_f
            if (is_finite_number(z_high) and float(z_high) > current_f)
            else current_f + 1.10 * atr15_f
        )
        raw_dist = max(raw_anchor - current_f, 0.35 * atr15_f)
        risk_dist = min(max(raw_dist, min_risk_dist), max_risk_dist)
        sl_val = current_f + risk_dist
        reward_dist = max(2.0 * risk_dist, target_net_rr * (risk_dist + unit_cost_est) + unit_cost_est)
        if current_f - reward_dist <= current_f * 0.05:
            reward_dist = current_f * 0.50
        tp_val = max(current_f * 0.05, current_f - reward_dist)

    sizing = compute_position_sizing(
        dir_norm,
        current_f,
        sl_val,
        tp_val,
        symbol=symbol,
        equity=equity,
        risk_fraction=risk_fraction,
        fee_pct_round_trip=fee_pct_round_trip,
        slippage_pct_round_trip=slippage_pct_round_trip,
        spread_pct=spread_pct,
        max_position_exposure_pct=max_position_exposure_pct,
        max_total_exposure_pct=max_total_exposure_pct,
        current_open_notional=current_open_notional,
        current_open_risk=current_open_risk,
        max_total_risk_fraction=max_total_risk_fraction,
        current_open_positions=current_open_positions,
        max_open_positions=max_open_positions,
        open_symbols=open_symbols,
        min_net_rr=min_net_rr,
        instrument_mode=instrument_mode,
    )
    return {
        "entry": sizing.entry if sizing.valid else current_f,
        "sl": sizing.stop_loss if sizing.valid else sl_val,
        "tp": sizing.take_profit if sizing.valid else tp_val,
        "gross_rr": sizing.gross_rr,
        "net_rr": sizing.net_rr,
        "position_size": sizing.position_size,
        "quantity": sizing.quantity,
        "notional_value": sizing.notional_value,
        "risk_budget": sizing.risk_budget,
        "instrument": sizing.instrument.to_dict(),
        "sizing": sizing.to_dict(),
        "veto_codes": list(sizing.veto_codes),
    }
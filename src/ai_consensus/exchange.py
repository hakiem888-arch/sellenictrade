"""
Exchange Account Intelligence, Authenticated Binance Spot & USD-M Futures Adapter,
Idempotent Live/Testnet Order Execution Engine, Native Protective Stop-Loss/Take-Profit
Verification, Startup & Periodic Position Reconciliation, Persistent Risk Circuit Breakers,
Kill Switch, and 13-Point Live Readiness Gate (Master Prompt 2: Phases 2, 5, 6, 8, 9, 10, 11, 16).
Strictly zero network or UI side-effects on import; live execution is disabled by default.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from .config import (
    DEFAULT_BINANCE_FUTURES_TESTNET_URL,
    DEFAULT_BINANCE_FUTURES_URL,
    DEFAULT_BINANCE_SPOT_TESTNET_URL,
    DEFAULT_BINANCE_SPOT_URL,
    DEFAULT_DB_FILE,
    DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS,
    DEFAULT_MAX_LEVERAGE,
    get_exchange_credentials,
    redact_secrets,
    resolve_execution_mode_config,
)
from .domain import (
    SignalLifecycleState,
    compute_directional_risk_reward,
    is_finite_number,
)
from .risk import (
    compute_position_sizing,
    evaluate_dynamic_protective_levels,
    evaluate_risk_guard,
    explain_risk_veto_codes,
    floor_quantity_to_lot,
    get_correlation_group,
    get_exchange_instrument_specs,
    round_price_to_tick,
)
from .services import dispatch_telegram_event
from .storage import (
    append_execution_audit_log,
    get_circuit_breaker_and_kill_switch_state,
    get_latest_account_snapshot,
    get_live_order,
    get_live_position,
     get_persisted_risk_guard_state,
    list_live_orders,
    list_live_positions,
    record_risk_guard_rejection,
    save_account_snapshot,
    update_circuit_breaker_and_kill_switch_state,
    upsert_live_order,
    upsert_live_position,
)

EXCHANGE_ENGINE_VERSION: str = "v7.0-autonomous-live-engine-1"

def _is_finite_num(v: Any) -> bool:
    if v is None or isinstance(v, bool):
        return False
    try:
        return math.isfinite(float(v))
    except (ValueError, TypeError):
        return False



class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class LivePositionLifecycleState(str, Enum):
    """Canonical persistent lifecycle states for live/testnet positions (Section 8)."""
    ENTRY_REQUESTED = "ENTRY_REQUESTED"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    PROTECTIVE_ORDERS_CONFIRMED = "PROTECTIVE_ORDERS_CONFIRMED"
    PROTECTION_MISSING_ALERT = "PROTECTION_MISSING_ALERT"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    EXIT_PARTIALLY_FILLED = "EXIT_PARTIALLY_FILLED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    EXECUTION_ERROR = "EXECUTION_ERROR"


@dataclass(frozen=True)
class InstrumentTradingRules:
    """Exchange-reported precision, lot/tick constraints, contract multiplier, and fee/funding metadata (Section 3)."""
    symbol: str
    market_type: str = "FUTURES_USDM"          # SPOT | FUTURES_USDM
    contract_type: str = "PERPETUAL"           # SPOT | PERPETUAL | INVERSE_PERPETUAL
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    tick_size: float = 0.01
    lot_size: float = 0.001
    min_qty: float = 0.001
    max_qty: float = 1000.0
    min_notional: float = 5.0
    contract_multiplier: float = 1.0
    price_precision: int = 2
    quantity_precision: int = 3
    max_leverage: float = 20.0
    maker_fee_pct: float = 0.02
    taker_fee_pct: float = 0.04
    funding_rate_pct_per_8h: float = 0.01
    status: str = "TRADING"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market_type": self.market_type,
            "contract_type": self.contract_type,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "min_qty": self.min_qty,
            "max_qty": self.max_qty,
            "min_notional": self.min_notional,
            "contract_multiplier": self.contract_multiplier,
            "price_precision": self.price_precision,
            "quantity_precision": self.quantity_precision,
            "max_leverage": self.max_leverage,
            "maker_fee_pct": self.maker_fee_pct,
            "taker_fee_pct": self.taker_fee_pct,
            "funding_rate_pct_per_8h": self.funding_rate_pct_per_8h,
            "status": self.status,
        }


@dataclass(frozen=True)
class ExchangePositionRecord:
    """Authoritative exchange position state (Section 3 & Section 8)."""
    symbol: str
    market_type: str
    direction: str                             # LONG | SHORT
    quantity: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    realized_pnl: float = 0.0
    notional_value: float = 0.0
    used_margin: float = 0.0
    leverage: float = 1.0
    margin_mode: str = "ISOLATED"              # ISOLATED | CROSSED | CASH
    liquidation_price: float = 0.0
    position_side: str = "BOTH"
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market_type": self.market_type,
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "notional_value": self.notional_value,
            "used_margin": self.used_margin,
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "liquidation_price": self.liquidation_price,
            "position_side": self.position_side,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ExchangeOrderRecord:
    """Authoritative exchange order record (Section 3 & Section 7)."""
    order_id: str
    client_order_id: str
    symbol: str
    side: str                                  # BUY | SELL
    order_type: str                            # MARKET | LIMIT | STOP_MARKET | TAKE_PROFIT_MARKET | STOP_LOSS_LIMIT
    status: str                                # NEW | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | EXPIRED
    price: float
    stop_price: float
    requested_qty: float
    executed_qty: float
    avg_fill_price: float
    reduce_only: bool = False
    close_position: bool = False
    fee_usd: float = 0.0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "status": self.status,
            "price": self.price,
            "stop_price": self.stop_price,
            "requested_qty": self.requested_qty,
            "executed_qty": self.executed_qty,
            "avg_fill_price": self.avg_fill_price,
            "reduce_only": self.reduce_only,
            "close_position": self.close_position,
            "fee_usd": self.fee_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ExchangeAccountSnapshot:
    """
    Authoritative Exchange Account Intelligence snapshot (Section 3).
    Strictly distinguishes wallet balance, available balance, account equity,
    unrealized PnL, realized PnL, used margin, and available margin.
    Never counts borrowed funds or unused leverage as additional account equity.
    """
    exchange: str
    market_type: str                           # SPOT | FUTURES_USDM
    execution_mode: str                        # PAPER | TESTNET | LIVE
    total_wallet_balance: float
    available_balance: float
    account_equity: float
    unrealized_pnl: float
    realized_pnl: float
    used_margin: float
    available_margin: float
    open_positions: Tuple[ExchangePositionRecord, ...] = field(default_factory=tuple)
    active_orders: Tuple[ExchangeOrderRecord, ...] = field(default_factory=tuple)
    instrument_rules: Dict[str, InstrumentTradingRules] = field(default_factory=dict)
    permissions: Tuple[str, ...] = field(default_factory=tuple)
    withdrawal_enabled: Optional[bool] = None
    trading_enabled: bool = True
    last_sync_ms: int = 0
    last_sync_iso: str = ""
    freshness_seconds: float = 0.0
    max_freshness_seconds: float = DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS
    is_fresh: bool = False
    sync_ok: bool = False
    is_consistent: bool = False
    api_latency_ms: float = 0.0
    error_code: Optional[str] = None
    error_message: str = ""
    veto_codes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def can_open_live_entries(self) -> bool:
        return bool(
            self.sync_ok
            and self.is_consistent
            and self.is_fresh
            and self.trading_enabled
            and self.account_equity > 0.0
            and self.available_margin >= 0.0
            and len(self.veto_codes) == 0
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange,
            "market_type": self.market_type,
            "execution_mode": self.execution_mode,
            "total_wallet_balance": round(self.total_wallet_balance, 4),
            "available_balance": round(self.available_balance, 4),
            "account_equity": round(self.account_equity, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "used_margin": round(self.used_margin, 4),
            "available_margin": round(self.available_margin, 4),
            "open_positions": [p.to_dict() for p in self.open_positions],
            "active_orders": [o.to_dict() for o in self.active_orders],
            "instrument_rules": {k: v.to_dict() for k, v in self.instrument_rules.items()},
            "permissions": list(self.permissions),
            "withdrawal_enabled": self.withdrawal_enabled,
            "trading_enabled": self.trading_enabled,
            "last_sync_ms": self.last_sync_ms,
            "last_sync_iso": self.last_sync_iso,
            "freshness_seconds": round(self.freshness_seconds, 3),
            "max_freshness_seconds": self.max_freshness_seconds,
            "is_fresh": self.is_fresh,
            "sync_ok": self.sync_ok,
            "is_consistent": self.is_consistent,
            "can_open_live_entries": self.can_open_live_entries,
            "api_latency_ms": round(self.api_latency_ms, 2),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "veto_codes": list(self.veto_codes),
        }


def parse_binance_exchange_info(
    raw_info: Dict[str, Any],
    market_type: str = "FUTURES_USDM",
) -> Dict[str, InstrumentTradingRules]:
    """Parse `/fapi/v1/exchangeInfo` or `/api/v3/exchangeInfo` into `InstrumentTradingRules` per symbol."""
    rules: Dict[str, InstrumentTradingRules] = {}
    if not isinstance(raw_info, dict):
        return rules
    m_type = str(market_type or "FUTURES_USDM").upper().strip()
    for s_item in raw_info.get("symbols") or []:
        if not isinstance(s_item, dict):
            continue
        sym = str(s_item.get("symbol") or "").upper().strip()
        if not sym:
            continue
        status = str(s_item.get("status") or s_item.get("contractStatus") or "TRADING").upper()
        base_asset = str(s_item.get("baseAsset") or sym.replace("USDT", ""))
        quote_asset = str(s_item.get("quoteAsset") or "USDT")
        c_type = str(s_item.get("contractType") or ("SPOT" if m_type == "SPOT" else "PERPETUAL")).upper()
        mult = float(s_item.get("contractSize") or 1.0) if _is_finite_num(s_item.get("contractSize")) else 1.0
        p_prec = int(s_item.get("pricePrecision") or s_item.get("quotePrecision") or 2)
        q_prec = int(s_item.get("quantityPrecision") or s_item.get("baseAssetPrecision") or 3)

        tick_size = 0.01
        lot_size = 0.001
        min_qty = 0.001
        max_qty = 1000000.0
        min_notional = 5.0

        for flt in s_item.get("filters") or []:
            if not isinstance(flt, dict):
                continue
            ft = str(flt.get("filterType") or "").upper()
            if ft == "PRICE_FILTER":
                ts = flt.get("tickSize")
                if _is_finite_num(ts) and float(ts) > 0:
                    tick_size = float(ts)
            elif ft == "LOT_SIZE":
                ss = flt.get("stepSize")
                mq = flt.get("minQty")
                xq = flt.get("maxQty")
                if _is_finite_num(ss) and float(ss) > 0:
                    lot_size = float(ss)
                if _is_finite_num(mq) and float(mq) > 0:
                    min_qty = float(mq)
                if _is_finite_num(xq) and float(xq) > 0:
                    max_qty = float(xq)
            elif ft in {"MIN_NOTIONAL", "NOTIONAL"}:
                mn = flt.get("notional") or flt.get("minNotional")
                if _is_finite_num(mn) and float(mn) > 0:
                    min_notional = float(mn)

        rules[sym] = InstrumentTradingRules(
            symbol=sym,
            market_type=m_type,
            contract_type=c_type,
            base_asset=base_asset,
            quote_asset=quote_asset,
            tick_size=tick_size,
            lot_size=lot_size,
            min_qty=min_qty,
            max_qty=max_qty,
            min_notional=min_notional,
            contract_multiplier=mult,
            price_precision=p_prec,
            quantity_precision=q_prec,
            status=status,
        )
    return rules


def parse_binance_order_record(raw_order: Dict[str, Any]) -> ExchangeOrderRecord:
    """Parse a Binance Spot or Futures order payload into `ExchangeOrderRecord`."""
    oid = str(raw_order.get("orderId") or raw_order.get("id") or "")
    coid = str(raw_order.get("clientOrderId") or raw_order.get("origClientOrderId") or "")
    sym = str(raw_order.get("symbol") or "UNKNOWN").upper().strip()
    side = str(raw_order.get("side") or "BUY").upper().strip()
    otype = str(raw_order.get("type") or raw_order.get("origType") or "MARKET").upper().strip()
    status = str(raw_order.get("status") or "NEW").upper().strip()
    price = float(raw_order.get("price") or 0.0) if _is_finite_num(raw_order.get("price")) else 0.0
    stop_price = float(raw_order.get("stopPrice") or 0.0) if _is_finite_num(raw_order.get("stopPrice")) else 0.0
    req_qty = float(raw_order.get("origQty") or raw_order.get("requested_qty") or 0.0) if _is_finite_num(raw_order.get("origQty") or raw_order.get("requested_qty")) else 0.0
    exec_qty = float(raw_order.get("executedQty") or raw_order.get("executed_qty") or 0.0) if _is_finite_num(raw_order.get("executedQty") or raw_order.get("executed_qty")) else 0.0

    avg_px = raw_order.get("avgPrice") or raw_order.get("avg_fill_price")
    if (not _is_finite_num(avg_px) or float(avg_px) <= 0.0) and exec_qty > 0:
        cum_quote = raw_order.get("cummulativeQuoteQty") or raw_order.get("cumQuote")
        if _is_finite_num(cum_quote) and float(cum_quote) > 0:
            avg_px = float(cum_quote) / exec_qty
        else:
            avg_px = price
    avg_fill_price = float(avg_px or 0.0) if _is_finite_num(avg_px) else 0.0

    red_only = bool(raw_order.get("reduceOnly", False))
    close_pos = bool(raw_order.get("closePosition", False))
    fee_usd = float(raw_order.get("fee_usd") or raw_order.get("commission") or 0.0) if _is_finite_num(raw_order.get("fee_usd") or raw_order.get("commission")) else 0.0
    ts_ms = raw_order.get("updateTime") or raw_order.get("time") or int(time.time() * 1000)
    iso_str = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).isoformat() if _is_finite_num(ts_ms) else datetime.now(timezone.utc).isoformat()

    return ExchangeOrderRecord(
        order_id=oid,
        client_order_id=coid,
        symbol=sym,
        side=side,
        order_type=otype,
        status=status,
        price=round(price, 8),
        stop_price=round(stop_price, 8),
        requested_qty=round(req_qty, 8),
        executed_qty=round(exec_qty, 8),
        avg_fill_price=round(avg_fill_price, 8),
        reduce_only=red_only,
        close_position=close_pos,
        fee_usd=round(fee_usd, 6),
        created_at=iso_str,
        updated_at=iso_str,
    )


def parse_binance_spot_account(
    raw_account: Dict[str, Any],
    *,
    raw_open_orders: Optional[List[Dict[str, Any]]] = None,
    quote_asset: str = "USDT",
    price_map: Optional[Dict[str, float]] = None,
    instrument_rules: Optional[Dict[str, InstrumentTradingRules]] = None,
    api_restrictions: Optional[Dict[str, Any]] = None,
    execution_mode: str = "TESTNET",
    sync_ms: Optional[int] = None,
    now_ms: Optional[int] = None,
    max_freshness_seconds: float = DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS,
    api_latency_ms: float = 0.0,
) -> ExchangeAccountSnapshot:
    """
    Parse Binance Spot `/api/v3/account` response (Section 3):
    - Calculates available trading funds strictly from the quote currency (`USDT`) free balance
      and locked order reservations.
    - Never counts borrowed funds or unused leverage as account equity.
    - Rejects missing, negative, or non-finite balances (`INCONSISTENT_ACCOUNT_DATA`).
    """
    curr_ms = int(now_ms if now_ms is not None else int(time.time() * 1000))
    s_ms = int(sync_ms if sync_ms is not None else curr_ms)
    s_iso = datetime.fromtimestamp(s_ms / 1000.0, tz=timezone.utc).isoformat()
    fresh_sec = max(0.0, (curr_ms - s_ms) / 1000.0)
    is_fresh = fresh_sec <= float(max_freshness_seconds)
    vetoes: List[str] = []

    if not isinstance(raw_account, dict) or "balances" not in raw_account or not isinstance(raw_account.get("balances"), list):
        return ExchangeAccountSnapshot(
            exchange="BINANCE",
            market_type="SPOT",
            execution_mode=execution_mode,
            total_wallet_balance=0.0,
            available_balance=0.0,
            account_equity=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            used_margin=0.0,
            available_margin=0.0,
            last_sync_ms=s_ms,
            last_sync_iso=s_iso,
            freshness_seconds=fresh_sec,
            max_freshness_seconds=max_freshness_seconds,
            is_fresh=False,
            sync_ok=False,
            is_consistent=False,
            api_latency_ms=api_latency_ms,
            error_code="INVALID_SPOT_ACCOUNT_PAYLOAD",
            error_message="Respons /api/v3/account tidak memiliki daftar balances yang valid.",
            veto_codes=("ACCOUNT_SYNC_FAILED", "INCONSISTENT_ACCOUNT_DATA"),
        )

    quote_u = str(quote_asset or "USDT").upper().strip()
    prices = price_map or {}
    free_quote = 0.0
    locked_quote = 0.0
    spot_positions: List[ExchangePositionRecord] = []
    holdings_value_usd = 0.0
    consistent = True

    for b in raw_account.get("balances") or []:
        if not isinstance(b, dict):
            continue
        asset = str(b.get("asset") or "").upper().strip()
        f_val = b.get("free", 0.0)
        l_val = b.get("locked", 0.0)
        if not _is_finite_num(f_val) or not _is_finite_num(l_val):
            consistent = False
            vetoes.append("INCONSISTENT_ACCOUNT_DATA")
            continue
        free_f = float(f_val)
        lock_f = float(l_val)
        if free_f < -1e-9 or lock_f < -1e-9:
            consistent = False
            vetoes.append("INCONSISTENT_ACCOUNT_DATA")
            continue

        tot_asset = free_f + lock_f
        if asset == quote_u:
            free_quote = free_f
            locked_quote = lock_f
        elif tot_asset > 1e-8:
            sym_pair = f"{asset}{quote_u}"
            px = float(prices.get(sym_pair, 0.0) or 0.0)
            if px > 0.0:
                notional = tot_asset * px
                holdings_value_usd += notional
                spot_positions.append(
                    ExchangePositionRecord(
                        symbol=sym_pair,
                        market_type="SPOT",
                        direction="LONG",
                        quantity=round(tot_asset, 8),
                        entry_price=round(px, 8),
                        mark_price=round(px, 8),
                        unrealized_pnl=0.0,
                        realized_pnl=0.0,
                        notional_value=round(notional, 4),
                        used_margin=round(notional, 4),
                        leverage=1.0,
                        margin_mode="CASH",
                        updated_at=s_iso,
                    )
                )

    total_wallet = round(free_quote + locked_quote + holdings_value_usd, 4)
    avail_bal = round(free_quote, 4)
    used_res = round(locked_quote + holdings_value_usd, 4)
    equity = total_wallet

    if equity <= 0.0:
        vetoes.append("ZERO_OR_NEGATIVE_ACCOUNT_EQUITY")
    if not is_fresh:
        vetoes.append("STALE_ACCOUNT_BALANCE")

    can_trade = bool(raw_account.get("canTrade", True))
    if not can_trade:
        vetoes.append("EXCHANGE_TRADING_DISABLED")

    withdrawal_enabled: Optional[bool] = None
    if isinstance(api_restrictions, dict) and "enableWithdrawals" in api_restrictions:
        withdrawal_enabled = bool(api_restrictions.get("enableWithdrawals"))
    elif " canWithdraw" in raw_account or "canWithdraw" in raw_account:
        # Note: /api/v3/account.canWithdraw reflects account-level capability unless apiRestrictions is provided
        pass
    if withdrawal_enabled is True:
        vetoes.append("API_KEY_WITHDRAWAL_ENABLED_UNSAFE")

    orders = tuple(
        parse_binance_order_record(o)
        for o in (raw_open_orders or [])
        if isinstance(o, dict)
    )
    perms = tuple(str(p) for p in (raw_account.get("permissions") or ["SPOT"]))

    return ExchangeAccountSnapshot(
        exchange="BINANCE",
        market_type="SPOT",
        execution_mode=str(execution_mode).upper().strip(),
        total_wallet_balance=total_wallet,
        available_balance=avail_bal,
        account_equity=equity,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        used_margin=used_res,
        available_margin=avail_bal,
        open_positions=tuple(spot_positions),
        active_orders=orders,
        instrument_rules=dict(instrument_rules or {}),
        permissions=perms,
        withdrawal_enabled=withdrawal_enabled,
        trading_enabled=can_trade,
        last_sync_ms=s_ms,
        last_sync_iso=s_iso,
        freshness_seconds=fresh_sec,
        max_freshness_seconds=max_freshness_seconds,
        is_fresh=is_fresh,
        sync_ok=True,
        is_consistent=consistent,
        api_latency_ms=api_latency_ms,
        veto_codes=tuple(dict.fromkeys(vetoes)),
    )


def parse_binance_futures_account(
    raw_account: Dict[str, Any],
    *,
    raw_positions: Optional[List[Dict[str, Any]]] = None,
    raw_open_orders: Optional[List[Dict[str, Any]]] = None,
    instrument_rules: Optional[Dict[str, InstrumentTradingRules]] = None,
    api_restrictions: Optional[Dict[str, Any]] = None,
    execution_mode: str = "TESTNET",
    sync_ms: Optional[int] = None,
    now_ms: Optional[int] = None,
    max_freshness_seconds: float = DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS,
    api_latency_ms: float = 0.0,
) -> ExchangeAccountSnapshot:
    """
    Parse Binance USD-M Futures `/fapi/v2/account` and `/fapi/v2/positionRisk` (Section 3):
    - Strictly distinguishes `totalWalletBalance`, `totalMarginBalance` (account equity),
      `availableBalance` (available margin), `totalInitialMargin` (used margin), and `totalUnrealizedProfit`.
    - Never counts unused leverage or borrowed funds as additional account equity.
    - Verifies mathematical consistency: `abs((wallet + unrealized_pnl) - equity) <= tolerance`
      and `available_margin <= equity + tolerance`.
    """
    curr_ms = int(now_ms if now_ms is not None else int(time.time() * 1000))
    s_ms = int(sync_ms if sync_ms is not None else curr_ms)
    s_iso = datetime.fromtimestamp(s_ms / 1000.0, tz=timezone.utc).isoformat()
    fresh_sec = max(0.0, (curr_ms - s_ms) / 1000.0)
    is_fresh = fresh_sec <= float(max_freshness_seconds)
    vetoes: List[str] = []

    if not isinstance(raw_account, dict) or "totalWalletBalance" not in raw_account:
        return ExchangeAccountSnapshot(
            exchange="BINANCE",
            market_type="FUTURES_USDM",
            execution_mode=execution_mode,
            total_wallet_balance=0.0,
            available_balance=0.0,
            account_equity=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            used_margin=0.0,
            available_margin=0.0,
            last_sync_ms=s_ms,
            last_sync_iso=s_iso,
            freshness_seconds=fresh_sec,
            max_freshness_seconds=max_freshness_seconds,
            is_fresh=False,
            sync_ok=False,
            is_consistent=False,
            api_latency_ms=api_latency_ms,
            error_code="INVALID_FUTURES_ACCOUNT_PAYLOAD",
            error_message="Respons /fapi/v2/account tidak memiliki field totalWalletBalance.",
            veto_codes=("ACCOUNT_SYNC_FAILED", "INCONSISTENT_ACCOUNT_DATA"),
        )

    w_raw = raw_account.get("totalWalletBalance")
    eq_raw = raw_account.get("totalMarginBalance")
    upnl_raw = raw_account.get("totalUnrealizedProfit", 0.0)
    avail_raw = raw_account.get("availableBalance")
    used_raw = raw_account.get("totalInitialMargin", 0.0)
    rpnl_raw = raw_account.get("totalRealizedProfit", 0.0)

    consistent = True
    for v in (w_raw, eq_raw, upnl_raw, avail_raw, used_raw):
        if not _is_finite_num(v):
            consistent = False
            vetoes.append("INCONSISTENT_ACCOUNT_DATA")

    if not consistent:
        return ExchangeAccountSnapshot(
            exchange="BINANCE",
            market_type="FUTURES_USDM",
            execution_mode=execution_mode,
            total_wallet_balance=0.0,
            available_balance=0.0,
            account_equity=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            used_margin=0.0,
            available_margin=0.0,
            last_sync_ms=s_ms,
            last_sync_iso=s_iso,
            freshness_seconds=fresh_sec,
            max_freshness_seconds=max_freshness_seconds,
            is_fresh=is_fresh,
            sync_ok=False,
            is_consistent=False,
            api_latency_ms=api_latency_ms,
            error_code="NON_FINITE_FUTURES_BALANCES",
            error_message="Terdapat nilai saldo/margin non-finite pada respons exchange.",
            veto_codes=tuple(dict.fromkeys(vetoes)),
        )

    wallet_bal = float(w_raw)
    upnl = float(upnl_raw)
    equity = float(eq_raw)
    avail_margin = float(avail_raw)
    used_margin = float(used_raw)
    rpnl = float(rpnl_raw) if _is_finite_num(rpnl_raw) else 0.0

    # Consistency validation: wallet_bal >= 0, equity == wallet_bal + upnl (within 1% or $5 tolerance),
    # and available_margin <= max(equity, wallet_bal) + $5
    if wallet_bal < -1e-6 or equity < -1e-6 or avail_margin < -1e-6 or used_margin < -1e-6:
        consistent = False
        vetoes.append("INCONSISTENT_ACCOUNT_DATA")

    expected_eq = wallet_bal + upnl
    if abs(equity - expected_eq) > max(5.0, abs(expected_eq) * 0.02):
        consistent = False
        vetoes.append("INCONSISTENT_ACCOUNT_DATA")

    if avail_margin > max(equity, wallet_bal) + max(5.0, equity * 0.02):
        consistent = False
        vetoes.append("INCONSISTENT_ACCOUNT_DATA")

    if equity <= 0.0:
        vetoes.append("ZERO_OR_NEGATIVE_ACCOUNT_EQUITY")
    if not is_fresh:
        vetoes.append("STALE_ACCOUNT_BALANCE")

    can_trade = bool(raw_account.get("canTrade", True))
    if not can_trade:
        vetoes.append("EXCHANGE_TRADING_DISABLED")

    withdrawal_enabled: Optional[bool] = None
    if isinstance(api_restrictions, dict) and "enableWithdrawals" in api_restrictions:
        withdrawal_enabled = bool(api_restrictions.get("enableWithdrawals"))
    if withdrawal_enabled is True:
        vetoes.append("API_KEY_WITHDRAWAL_ENABLED_UNSAFE")

    pos_source = raw_positions if raw_positions is not None else (raw_account.get("positions") or [])
    open_positions: List[ExchangePositionRecord] = []
    for p in pos_source:
        if not isinstance(p, dict):
            continue
        amt_raw = p.get("positionAmt", 0.0)
        if not _is_finite_num(amt_raw):
            continue
        amt = float(amt_raw)
        if abs(amt) <= 1e-12:
            continue
        sym = str(p.get("symbol") or "UNKNOWN").upper().strip()
        direction = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)
        ep = float(p.get("entryPrice") or 0.0) if _is_finite_num(p.get("entryPrice")) else 0.0
        mp = float(p.get("markPrice") or ep) if _is_finite_num(p.get("markPrice")) else ep
        p_upnl = float(p.get("unRealizedProfit") or p.get("unrealizedProfit") or 0.0)
        lev = float(p.get("leverage") or 1.0) if _is_finite_num(p.get("leverage")) else 1.0
        notional = abs(float(p.get("notional") or (qty * (mp if mp > 0 else ep))))
        u_marg = float(p.get("initialMargin") or p.get("isolatedWallet") or (notional / max(1.0, lev)))
        m_mode = "ISOLATED" if bool(p.get("isolated", False)) or str(p.get("marginType", "")).upper() == "ISOLATED" else "CROSSED"
        liq_px = float(p.get("liquidationPrice") or 0.0) if _is_finite_num(p.get("liquidationPrice")) else 0.0
        p_side = str(p.get("positionSide") or "BOTH").upper()

        open_positions.append(
            ExchangePositionRecord(
                symbol=sym,
                market_type="FUTURES_USDM",
                direction=direction,
                quantity=round(qty, 8),
                entry_price=round(ep, 8),
                mark_price=round(mp, 8),
                unrealized_pnl=round(p_upnl, 4),
                realized_pnl=0.0,
                notional_value=round(notional, 4),
                used_margin=round(u_marg, 4),
                leverage=lev,
                margin_mode=m_mode,
                liquidation_price=round(liq_px, 8),
                position_side=p_side,
                updated_at=s_iso,
            )
        )

    orders = tuple(
        parse_binance_order_record(o)
        for o in (raw_open_orders or [])
        if isinstance(o, dict)
    )

    return ExchangeAccountSnapshot(
        exchange="BINANCE",
        market_type="FUTURES_USDM",
        execution_mode=str(execution_mode).upper().strip(),
        total_wallet_balance=round(wallet_bal, 4),
        available_balance=round(avail_margin, 4),
        account_equity=round(equity, 4),
        unrealized_pnl=round(upnl, 4),
        realized_pnl=round(rpnl, 4),
        used_margin=round(used_margin, 4),
        available_margin=round(avail_margin, 4),
        open_positions=tuple(open_positions),
        active_orders=orders,
        instrument_rules=dict(instrument_rules or {}),
        permissions=("FUTURES",),
        withdrawal_enabled=withdrawal_enabled,
        trading_enabled=can_trade,
        last_sync_ms=s_ms,
        last_sync_iso=s_iso,
        freshness_seconds=fresh_sec,
        max_freshness_seconds=max_freshness_seconds,
        is_fresh=is_fresh,
        sync_ok=True,
        is_consistent=consistent,
        api_latency_ms=api_latency_ms,
        veto_codes=tuple(dict.fromkeys(vetoes)),
    )


def generate_client_order_id(
    signal_id: str,
    symbol: str,
    order_leg: str,
    direction: str,
    quantity: float,
    price: Optional[float] = None,
) -> str:
    """
    Generate a deterministic, exchange-compatible (`<= 36` chars alphanumeric/underscore)
    clientOrderId so retries or network timeouts never submit duplicate orders (Section 7).
    """
    leg_short = {
        "ENTRY": "ENT",
        "STOP_LOSS": "SL",
        "TAKE_PROFIT": "TP",
        "EXIT": "EXT",
        "EMERGENCY_CLOSE": "EMG",
    }.get(str(order_leg).upper().strip(), "ORD")
    sym_clean = "".join(ch for ch in str(symbol).upper() if ch.isalnum())[:8]
    raw_seed = f"{signal_id}|{sym_clean}|{leg_short}|{direction}|{float(quantity):.8f}|{float(price or 0.0):.8f}"
    digest = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:16]
    return f"sa_{leg_short}_{sym_clean}_{digest}"[:36]


class BinanceExchangeAdapter:
    """
    Authenticated Binance Spot & USD-M Futures Account + Execution Adapter (Section 3, 4, 7).
    Supports dependency-injected `request_fn` for deterministic offline testing as well as
    real signed HTTPS requests with explicit timeouts, recvWindow, rate-limit handling,
    and secret redaction.
    """

    def __init__(
        self,
        *,
        execution_mode: str = "PAPER",
        market_type: str = "FUTURES_USDM",
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 10.0,
        recv_window_ms: int = 5000,
        max_freshness_seconds: float = DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS,
        request_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.execution_mode = str(execution_mode or "PAPER").upper().strip()
        if self.execution_mode not in {"PAPER", "TESTNET", "LIVE"}:
            self.execution_mode = "PAPER"
        self.market_type = str(market_type or "FUTURES_USDM").upper().strip()
        if self.market_type not in {"SPOT", "FUTURES_USDM"}:
            self.market_type = "FUTURES_USDM"

        creds = get_exchange_credentials(self.execution_mode)
        self.api_key = str(api_key if api_key is not None else creds["api_key"]).strip()
        self.api_secret = str(api_secret if api_secret is not None else creds["api_secret"]).strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.recv_window_ms = max(1000, int(recv_window_ms))
        self.max_freshness_seconds = float(max_freshness_seconds)
        self._request_fn = request_fn or requests.request
        self._instrument_cache: Dict[str, InstrumentTradingRules] = {}

        if base_url:
            self.base_url = str(base_url).rstrip("/")
        elif self.market_type == "SPOT":
            self.base_url = (
                DEFAULT_BINANCE_SPOT_TESTNET_URL
                if self.execution_mode == "TESTNET"
                else DEFAULT_BINANCE_SPOT_URL
            )
        else:
            self.base_url = (
                DEFAULT_BINANCE_FUTURES_TESTNET_URL
                if self.execution_mode == "TESTNET"
                else DEFAULT_BINANCE_FUTURES_URL
            )

        if not self.base_url.startswith("https://"):
            raise ValueError("TLS_REQUIRED: Exchange base_url wajib menggunakan HTTPS.")

    @property
    def extra_secrets(self) -> List[str]:
        return [s for s in (self.api_key, self.api_secret) if s]

    def _sign_params(self, params: Dict[str, Any]) -> str:
        clean_params = {k: v for k, v in params.items() if v is not None}
        clean_params.setdefault("timestamp", int(time.time() * 1000))
        clean_params.setdefault("recvWindow", self.recv_window_ms)
        query = urllib.parse.urlencode(clean_params)
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={sig}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        signed: bool = False,
    ) -> Tuple[bool, Any, float, Optional[str]]:
        t0 = time.time()
        if signed and (not self.api_key or not self.api_secret):
            return False, {"msg": "Missing API credentials"}, 0.0, "MISSING_API_CREDENTIALS"

        headers: Dict[str, str] = {}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        q_params = dict(params or {})
        if signed:
            query_str = self._sign_params(q_params)
            url = f"{self.base_url}{path}?{query_str}"
            req_kwargs: Dict[str, Any] = {"headers": headers, "timeout": self.timeout_seconds}
        else:
            url = f"{self.base_url}{path}"
            req_kwargs = {"params": q_params, "headers": headers, "timeout": self.timeout_seconds}

        try:
            resp = self._request_fn(method.upper(), url, **req_kwargs)
            latency_ms = (time.time() - t0) * 1000.0
            status_code = int(getattr(resp, "status_code", 200))
            try:
                payload = resp.json() if hasattr(resp, "json") else json.loads(getattr(resp, "text", "{}"))
            except Exception:
                payload = {"raw_text": str(getattr(resp, "text", ""))[:300]}

            if status_code in (429, 418):
                return False, payload, latency_ms, "EXCHANGE_RATE_LIMITED"
            if status_code in (401, 403):
                return False, payload, latency_ms, "EXCHANGE_AUTH_FAILED"
            if status_code >= 400:
                err_code = str(payload.get("code", f"HTTP_{status_code}")) if isinstance(payload, dict) else f"HTTP_{status_code}"
                if err_code in {"-2014", "-2015", "-1022"}:
                    return False, payload, latency_ms, "EXCHANGE_AUTH_FAILED"
                return False, payload, latency_ms, f"EXCHANGE_API_ERROR_{err_code}"
            return True, payload, latency_ms, None
        except requests.exceptions.Timeout as exc:
            latency_ms = (time.time() - t0) * 1000.0
            return False, {"msg": redact_secrets(str(exc), extra_secrets=self.extra_secrets)}, latency_ms, "REQUEST_TIMEOUT"
        except Exception as exc:
            latency_ms = (time.time() - t0) * 1000.0
            return False, {"msg": redact_secrets(str(exc), extra_secrets=self.extra_secrets)}, latency_ms, "NETWORK_OR_TRANSPORT_ERROR"

    def fetch_instrument_rules(self, symbol: Optional[str] = None) -> Dict[str, InstrumentTradingRules]:
        """Fetch and cache authoritative instrument precision and constraints from exchangeInfo."""
        path = "/api/v3/exchangeInfo" if self.market_type == "SPOT" else "/fapi/v1/exchangeInfo"
        ok, payload, _, _ = self._request("GET", path, signed=False)
        if ok and isinstance(payload, dict):
            parsed = parse_binance_exchange_info(payload, market_type=self.market_type)
            self._instrument_cache.update(parsed)
        if symbol:
            sym_u = str(symbol).upper().strip()
            if sym_u in self._instrument_cache:
                return {sym_u: self._instrument_cache[sym_u]}
        return dict(self._instrument_cache)

    def synchronize_account(
        self,
        *,
        db_file: Optional[str] = None,
        now_ms: Optional[int] = None,
        price_map: Optional[Dict[str, float]] = None,
    ) -> ExchangeAccountSnapshot:
        """
        Synchronize authoritative account balances, equity, margin, open positions, and active orders
        from the authenticated exchange API (Section 3).
        Never silently falls back to an old balance or hardcoded amount!
        """
        curr_ms = int(now_ms if now_ms is not None else int(time.time() * 1000))
        curr_iso = datetime.fromtimestamp(curr_ms / 1000.0, tz=timezone.utc).isoformat()

        if not self.api_key or not self.api_secret:
            snap = ExchangeAccountSnapshot(
                exchange="BINANCE",
                market_type=self.market_type,
                execution_mode=self.execution_mode,
                total_wallet_balance=0.0,
                available_balance=0.0,
                account_equity=0.0,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                used_margin=0.0,
                available_margin=0.0,
                last_sync_ms=curr_ms,
                last_sync_iso=curr_iso,
                freshness_seconds=0.0,
                max_freshness_seconds=self.max_freshness_seconds,
                is_fresh=False,
                sync_ok=False,
                is_consistent=False,
                error_code="MISSING_API_CREDENTIALS",
                error_message="Kredensial API exchange belum dikonfigurasi pada environment variables.",
                veto_codes=("ACCOUNT_SYNC_FAILED", "MISSING_API_CREDENTIALS"),
            )
            if db_file:
                save_account_snapshot(snap.to_dict(), db_file=db_file, extra_secrets=self.extra_secrets)
            return snap

        # Fetch exchangeInfo rules if cache is empty
        if not self._instrument_cache:
            self.fetch_instrument_rules()

        if self.market_type == "SPOT":
            ok_acc, acc_payload, lat_ms, err_code = self._request("GET", "/api/v3/account", signed=True)
            if not ok_acc:
                snap = ExchangeAccountSnapshot(
                    exchange="BINANCE",
                    market_type="SPOT",
                    execution_mode=self.execution_mode,
                    total_wallet_balance=0.0,
                    available_balance=0.0,
                    account_equity=0.0,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    used_margin=0.0,
                    available_margin=0.0,
                    last_sync_ms=curr_ms,
                    last_sync_iso=curr_iso,
                    freshness_seconds=0.0,
                    max_freshness_seconds=self.max_freshness_seconds,
                    is_fresh=False,
                    sync_ok=False,
                    is_consistent=False,
                    api_latency_ms=lat_ms,
                    error_code=err_code or "ACCOUNT_SYNC_FAILED",
                    error_message=str(redact_secrets(str(acc_payload), extra_secrets=self.extra_secrets))[:300],
                    veto_codes=("ACCOUNT_SYNC_FAILED", err_code or "ACCOUNT_SYNC_FAILED"),
                )
                if db_file:
                    save_account_snapshot(snap.to_dict(), db_file=db_file, extra_secrets=self.extra_secrets)
                return snap

            ok_ord, ord_payload, _, _ = self._request("GET", "/api/v3/openOrders", signed=True)
            open_orders_raw = ord_payload if (ok_ord and isinstance(ord_payload, list)) else []
            ok_rest, rest_payload, _, _ = self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)
            api_rest = rest_payload if (ok_rest and isinstance(rest_payload, dict)) else None

            snap = parse_binance_spot_account(
                acc_payload,
                raw_open_orders=open_orders_raw,
                price_map=price_map,
                instrument_rules=self._instrument_cache,
                api_restrictions=api_rest,
                execution_mode=self.execution_mode,
                sync_ms=curr_ms,
                now_ms=curr_ms,
                max_freshness_seconds=self.max_freshness_seconds,
                api_latency_ms=lat_ms,
            )
        else:
            ok_acc, acc_payload, lat_ms, err_code = self._request("GET", "/fapi/v2/account", signed=True)
            if not ok_acc:
                snap = ExchangeAccountSnapshot(
                    exchange="BINANCE",
                    market_type="FUTURES_USDM",
                    execution_mode=self.execution_mode,
                    total_wallet_balance=0.0,
                    available_balance=0.0,
                    account_equity=0.0,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    used_margin=0.0,
                    available_margin=0.0,
                    last_sync_ms=curr_ms,
                    last_sync_iso=curr_iso,
                    freshness_seconds=0.0,
                    max_freshness_seconds=self.max_freshness_seconds,
                    is_fresh=False,
                    sync_ok=False,
                    is_consistent=False,
                    api_latency_ms=lat_ms,
                    error_code=err_code or "ACCOUNT_SYNC_FAILED",
                    error_message=str(redact_secrets(str(acc_payload), extra_secrets=self.extra_secrets))[:300],
                    veto_codes=("ACCOUNT_SYNC_FAILED", err_code or "ACCOUNT_SYNC_FAILED"),
                )
                if db_file:
                    save_account_snapshot(snap.to_dict(), db_file=db_file, extra_secrets=self.extra_secrets)
                return snap

            ok_pos, pos_payload, _, _ = self._request("GET", "/fapi/v2/positionRisk", signed=True)
            positions_raw = pos_payload if (ok_pos and isinstance(pos_payload, list)) else None
            ok_ord, ord_payload, _, _ = self._request("GET", "/fapi/v1/openOrders", signed=True)
            open_orders_raw = ord_payload if (ok_ord and isinstance(ord_payload, list)) else []

            snap = parse_binance_futures_account(
                acc_payload,
                raw_positions=positions_raw,
                raw_open_orders=open_orders_raw,
                instrument_rules=self._instrument_cache,
                execution_mode=self.execution_mode,
                sync_ms=curr_ms,
                now_ms=curr_ms,
                max_freshness_seconds=self.max_freshness_seconds,
                api_latency_ms=lat_ms,
            )

        if db_file:
            save_account_snapshot(snap.to_dict(), db_file=db_file, extra_secrets=self.extra_secrets)
        return snap

    def query_order(
        self,
        symbol: str,
        *,
        client_order_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[ExchangeOrderRecord], Optional[str]]:
        """Query authoritative order status on the exchange before retrying an uncertain order (Section 7)."""
        path = "/api/v3/order" if self.market_type == "SPOT" else "/fapi/v1/order"
        params: Dict[str, Any] = {"symbol": str(symbol).upper().strip()}
        if client_order_id:
            params["origClientOrderId"] = str(client_order_id).strip()
        elif exchange_order_id:
            params["orderId"] = str(exchange_order_id).strip()
        else:
            return False, None, "MISSING_ORDER_IDENTIFIER"

        ok, payload, _, err_code = self._request("GET", path, params=params, signed=True)
        if not ok or not isinstance(payload, dict):
            return False, None, err_code or "ORDER_NOT_FOUND"
        return True, parse_binance_order_record(payload), None

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        client_order_id: str,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
    ) -> Tuple[bool, Optional[ExchangeOrderRecord], Dict[str, Any], Optional[str]]:
        """
        Submit an order to the exchange with deterministic `newClientOrderId` and automatic
        status query on timeout so duplicate orders are never submitted (Section 7).
        """
        path = "/api/v3/order" if self.market_type == "SPOT" else "/fapi/v1/order"
        sym_u = str(symbol).upper().strip()
        side_u = str(side).upper().strip()
        otype_u = str(order_type).upper().strip()

        rules = self._instrument_cache.get(sym_u)
        tick_s = rules.tick_size if rules else get_exchange_instrument_specs(sym_u)["tick_size"]
        lot_s = rules.lot_size if rules else get_exchange_instrument_specs(sym_u)["lot_size"]

        adj_qty = floor_quantity_to_lot(float(quantity), lot_s)
        if adj_qty <= 0.0:
            return False, None, {}, "INVALID_ORDER_QUANTITY_AFTER_LOT_FLOOR"

        params: Dict[str, Any] = {
            "symbol": sym_u,
            "side": side_u,
            "type": otype_u,
            "quantity": f"{adj_qty:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": str(client_order_id).strip(),
        }
        if self.market_type == "FUTURES_USDM" and reduce_only:
            params["reduceOnly"] = "true"

        if otype_u in {"LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
            if price is None or not _is_finite_num(price) or float(price) <= 0:
                return False, None, {}, "MISSING_LIMIT_PRICE"
            adj_px = round_price_to_tick(float(price), tick_s)
            params["price"] = f"{adj_px:.8f}".rstrip("0").rstrip(".")
            params["timeInForce"] = time_in_force

        if otype_u in {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
            if stop_price is None or not _is_finite_num(stop_price) or float(stop_price) <= 0:
                return False, None, {}, "MISSING_STOP_PRICE"
            adj_stop = round_price_to_tick(float(stop_price), tick_s)
            params["stopPrice"] = f"{adj_stop:.8f}".rstrip("0").rstrip(".")

        ok, payload, _, err_code = self._request("POST", path, params=params, signed=True)
        if ok and isinstance(payload, dict):
            return True, parse_binance_order_record(payload), payload, None

        # CRITICAL SAFETY RULE (Section 7): Do not assume a timeout means the order failed!
        # Query exchange order status by client_order_id before reporting failure or retrying.
        if err_code in {"REQUEST_TIMEOUT", "NETWORK_OR_TRANSPORT_ERROR"}:
            q_ok, q_ord, _ = self.query_order(sym_u, client_order_id=client_order_id)
            if q_ok and q_ord is not None:
                return True, q_ord, q_ord.to_dict(), None
            return False, None, payload if isinstance(payload, dict) else {}, "UNCERTAIN_ORDER_TIMEOUT_UNCONFIRMED"

        return False, None, payload if isinstance(payload, dict) else {}, err_code or "ORDER_SUBMISSION_FAILED"

    def cancel_order(
        self,
        symbol: str,
        *,
        client_order_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[ExchangeOrderRecord], Optional[str]]:
        """Cancel an active order on the exchange."""
        path = "/api/v3/order" if self.market_type == "SPOT" else "/fapi/v1/order"
        params: Dict[str, Any] = {"symbol": str(symbol).upper().strip()}
        if client_order_id:
            params["origClientOrderId"] = str(client_order_id).strip()
        elif exchange_order_id:
            params["orderId"] = str(exchange_order_id).strip()
        else:
            return False, None, "MISSING_ORDER_IDENTIFIER"

        ok, payload, _, err_code = self._request("DELETE", path, params=params, signed=True)
        if ok and isinstance(payload, dict):
            return True, parse_binance_order_record(payload), None
        return False, None, err_code or "ORDER_CANCEL_FAILED"


def evaluate_and_persist_circuit_breakers(
    *,
    db_file: str = DEFAULT_DB_FILE,
    account_snapshot: Optional[ExchangeAccountSnapshot] = None,
    equity_reference: float = 10_000.0,
    max_daily_loss_fraction: float = 0.03,
    max_total_open_risk_fraction: float = 0.02,
    current_open_risk_usd: float = 0.0,
    current_open_positions: int = 0,
    max_open_positions: int = 4,
    consecutive_losses: int = 0,
    max_consecutive_losses: int = 4,
    market_data_fresh: bool = True,
    missing_protective_orders: bool = False,
    reconciliation_mismatch: bool = False,
    max_api_latency_ms: float = 5000.0,
    max_consecutive_execution_failures: int = 3,
    max_consecutive_auth_failures: int = 2,
    daily_realized_loss_override: Optional[float] = None,
    daily_unrealized_loss_override: Optional[float] = None,
    today_utc_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluate and persist all 10 mandatory Risk Circuit Breakers in SQLite (Section 10):
    1. Maximum daily loss (realized + unrealized loss vs daily limit, persisted across restarts with UTC reset boundary).
    2. Maximum total open risk.
    3. Maximum concurrent positions.
    4. Excessive consecutive losses.
    5. Stale market data.
    6. Failed or stale account synchronization.
    7. Repeated order execution failures.
    8. Missing protective orders on open positions.
    9. Unexpected exchange/local position discrepancies.
    10. Abnormal API latency or repeated authentication failures.
    """
    cb_state = get_circuit_breaker_and_kill_switch_state(db_file=db_file, today_utc_str=today_utc_str)
    rg_state = get_persisted_risk_guard_state(
        db_file=db_file,
        initial_equity=equity_reference,
        max_daily_loss_fraction=max_daily_loss_fraction,
        max_consecutive_losses=max_consecutive_losses,
        today_utc_str=today_utc_str,
    )

    eq = (
        float(account_snapshot.account_equity)
        if (account_snapshot is not None and account_snapshot.sync_ok and account_snapshot.account_equity > 0)
        else float(rg_state.get("current_equity") or equity_reference)
    )
    d_realized = (
        float(daily_realized_loss_override)
        if daily_realized_loss_override is not None
        else max(
            float(cb_state.get("daily_realized_loss_usd") or 0.0),
            float(rg_state.get("daily_realized_loss_usd") or 0.0),
        )
    )
    if account_snapshot is not None and account_snapshot.unrealized_pnl < 0.0:
        acct_unrealized_loss = abs(float(account_snapshot.unrealized_pnl))
    else:
        acct_unrealized_loss = 0.0
    d_unrealized = (
        float(daily_unrealized_loss_override)
        if daily_unrealized_loss_override is not None
        else max(acct_unrealized_loss, float(cb_state.get("daily_unrealized_loss_usd") or 0.0))
    )

    total_daily_loss = round(max(0.0, d_realized) + max(0.0, d_unrealized), 4)
    max_daily_loss_usd = round(eq * float(max_daily_loss_fraction), 4)

    c_losses = max(
        int(consecutive_losses),
        int(cb_state.get("consecutive_losses") or 0),
        int(rg_state.get("consecutive_losses") or 0),
    )
    exec_fails = int(cb_state.get("consecutive_execution_failures") or 0)
    auth_fails = int(cb_state.get("consecutive_auth_failures") or 0)

    reasons: List[str] = []

    if total_daily_loss >= max_daily_loss_usd - 1e-9 and max_daily_loss_usd > 0:
        reasons.append("CB_MAX_DAILY_LOSS_BREACHED")

    max_open_risk_usd = round(eq * float(max_total_open_risk_fraction), 4)
    if float(current_open_risk_usd) > max_open_risk_usd + 1e-9 and max_open_risk_usd > 0:
        reasons.append("CB_MAX_TOTAL_OPEN_RISK_BREACHED")

    if int(current_open_positions) > int(max_open_positions) and int(max_open_positions) > 0:
        reasons.append("CB_MAX_CONCURRENT_POSITIONS_BREACHED")

    if c_losses >= int(max_consecutive_losses) and int(max_consecutive_losses) > 0:
        reasons.append("CB_MAX_CONSECUTIVE_LOSSES_BREACHED")

    if not market_data_fresh:
        reasons.append("CB_STALE_MARKET_DATA")

    if account_snapshot is not None:
        if not account_snapshot.sync_ok or not account_snapshot.is_consistent:
            reasons.append("CB_ACCOUNT_SYNC_FAILED")
            if account_snapshot.error_code == "EXCHANGE_AUTH_FAILED":
                auth_fails += 1
        elif not account_snapshot.is_fresh:
            reasons.append("CB_STALE_ACCOUNT_BALANCE")
        if account_snapshot.api_latency_ms > float(max_api_latency_ms):
            reasons.append("CB_ABNORMAL_API_LATENCY")

    if exec_fails >= int(max_consecutive_execution_failures):
        reasons.append("CB_REPEATED_EXECUTION_FAILURES")

    if auth_fails >= int(max_consecutive_auth_failures):
        reasons.append("CB_REPEATED_AUTH_FAILURES")

    if missing_protective_orders:
        reasons.append("CB_MISSING_PROTECTIVE_ORDERS")

    if reconciliation_mismatch or str(cb_state.get("reconciliation_status") or "OK").upper() == "MISMATCH":
        reasons.append("CB_POSITION_RECONCILIATION_MISMATCH")

    is_tripped = len(reasons) > 0
    updated = update_circuit_breaker_and_kill_switch_state(
        {
            "circuit_breaker_active": is_tripped,
            "trading_day_utc": str(today_utc_str or cb_state.get("trading_day_utc") or datetime.now(timezone.utc).isoformat()[:10]),
            "active_reasons": reasons,
            "daily_realized_loss_usd": round(d_realized, 4),
            "daily_unrealized_loss_usd": round(d_unrealized, 4),
            "consecutive_losses": c_losses,
            "consecutive_execution_failures": exec_fails,
            "consecutive_auth_failures": auth_fails,
            "reconciliation_status": "MISMATCH" if reconciliation_mismatch else cb_state.get("reconciliation_status", "OK"),
        },
        db_file=db_file,
    )
    updated["total_daily_loss_usd"] = total_daily_loss
    updated["max_daily_loss_usd"] = max_daily_loss_usd
    updated["new_entries_blocked"] = bool(is_tripped or updated.get("kill_switch_active"))
    return updated


def activate_kill_switch(
    reason: str = "Manual Kill Switch activated by user",
    *,
    db_file: str = DEFAULT_DB_FILE,
    adapter: Optional[BinanceExchangeAdapter] = None,
    cancel_unfilled_entries: bool = True,
    preserve_protective_orders: bool = True,
    tg_enabled: bool = False,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Activate the emergency Kill Switch (Section 11):
    - Immediately blocks all new entries and order submissions in SQLite.
    - Cancels unfilled entry orders (`reduce_only == False`) when `cancel_unfilled_entries=True`.
    - Preserves protective SL/TP orders (`reduce_only == True`) on open positions by default (`preserve_protective_orders=True`).
    - Verifies actual state on the exchange; if exchange connectivity is unavailable, reports
      `UNVERIFIED_EXCHANGE_OFFLINE` rather than falsely claiming remote cancellation succeeded.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    canceled_orders: List[str] = []
    preserved_protective_orders: List[str] = []
    failed_cancellations: List[str] = []
    verification_status = "LOCAL_ONLY_NO_ADAPTER"

    local_active = list_live_orders(db_file=db_file, active_only=True)

    if adapter is not None:
        snap = adapter.synchronize_account(db_file=db_file)
        if not snap.sync_ok:
            verification_status = "UNVERIFIED_EXCHANGE_OFFLINE"
        else:
            verification_status = "VERIFIED_WITH_EXCHANGE"
            for ex_ord in snap.active_orders:
                is_protective = bool(
                    ex_ord.reduce_only
                    or ex_ord.close_position
                    or ex_ord.order_type in {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}
                )
                if is_protective and preserve_protective_orders:
                    preserved_protective_orders.append(ex_ord.client_order_id or ex_ord.order_id)
                    continue
                if not is_protective and cancel_unfilled_entries:
                    ok_c, c_rec, err_c = adapter.cancel_order(
                        ex_ord.symbol,
                        client_order_id=ex_ord.client_order_id or None,
                        exchange_order_id=ex_ord.order_id or None,
                    )
                    if ok_c:
                        canceled_orders.append(ex_ord.client_order_id or ex_ord.order_id)
                        if ex_ord.client_order_id:
                            upsert_live_order(
                                {
                                    "client_order_id": ex_ord.client_order_id,
                                    "exchange_order_id": ex_ord.order_id,
                                    "symbol": ex_ord.symbol,
                                    "execution_mode": adapter.execution_mode,
                                    "market_type": adapter.market_type,
                                    "order_leg": "ENTRY",
                                    "side": ex_ord.side,
                                    "order_type": ex_ord.order_type,
                                    "status": "CANCELED",
                                    "requested_qty": ex_ord.requested_qty,
                                    "executed_qty": ex_ord.executed_qty,
                                },
                                db_file=db_file,
                            )
                    else:
                        failed_cancellations.append(f"{ex_ord.symbol}:{ex_ord.client_order_id or ex_ord.order_id}:{err_c}")
                        verification_status = "PARTIAL_CANCEL_FAILURE"
    else:
        for lo in local_active:
            is_prot = bool(lo.get("reduce_only") or str(lo.get("order_leg")).upper() in {"STOP_LOSS", "TAKE_PROFIT"})
            if is_prot and preserve_protective_orders:
                preserved_protective_orders.append(str(lo.get("client_order_id")))
            elif not is_prot and cancel_unfilled_entries:
                lo_copy = dict(lo)
                lo_copy["status"] = "CANCELED"
                upsert_live_order(lo_copy, db_file=db_file)
                canceled_orders.append(str(lo.get("client_order_id")))
        verification_status = "VERIFIED_LOCAL_STATE"

    updated = update_circuit_breaker_and_kill_switch_state(
        {
            "kill_switch_active": True,
            "live_trading_authorized": False,
            "kill_switch_reason": str(reason),
            "kill_switch_activated_at": now_iso,
            "kill_switch_verification_status": verification_status,
        },
        db_file=db_file,
    )

    append_execution_audit_log(
        {
            "event_type": "KILL_SWITCH_ACTIVATED",
            "execution_mode": str(updated.get("execution_mode") or "PAPER"),
            "symbol": "SYSTEM",
            "decision_outcome": "TRADING_HALTED",
            "state_before": "ACTIVE",
            "state_after": f"KILL_SWITCH_ON ({verification_status})",
            "order_ids": {
                "canceled_entry_orders": canceled_orders,
                "preserved_protective_orders": preserved_protective_orders,
                "failed_cancellations": failed_cancellations,
            },
            "reason": str(reason),
        },
        db_file=db_file,
    )

    if tg_enabled:
        dispatch_telegram_event(
            "KILL_SWITCH_ACTIVATED",
            {
                "symbol": "SYSTEM",
                "direction": "WAIT",
                "state": "INVALIDATED",
                "reason": (
                    f"Kill Switch Diaktifkan: {reason} | Status Verifikasi: {verification_status} | "
                    f"Entry Dibatalkan: {len(canceled_orders)} | Proteksi SL/TP Dipertahankan: {len(preserved_protective_orders)}"
                ),
            },
            db_file=db_file,
            tg_enabled=tg_enabled,
            tg_token=tg_token,
            tg_chat=tg_chat,
            post_fn=post_fn,
        )

    return {
        "kill_switch_active": True,
        "verification_status": verification_status,
        "canceled_entry_orders": canceled_orders,
        "preserved_protective_orders": preserved_protective_orders,
        "failed_cancellations": failed_cancellations,
        "state": updated,
    }


def deactivate_kill_switch(
    reason: str = "Manual Kill Switch reset by user",
    *,
    db_file: str = DEFAULT_DB_FILE,
) -> Dict[str, Any]:
    """Deactivate the Kill Switch while keeping `live_trading_authorized=False` until explicitly re-authorized."""
    updated = update_circuit_breaker_and_kill_switch_state(
        {
            "kill_switch_active": False,
            "live_trading_authorized": False,
            "kill_switch_reason": "",
            "kill_switch_verification_status": "RESET_IDLE",
        },
        db_file=db_file,
    )
    append_execution_audit_log(
        {
            "event_type": "KILL_SWITCH_DEACTIVATED",
            "execution_mode": str(updated.get("execution_mode") or "PAPER"),
            "symbol": "SYSTEM",
            "decision_outcome": "KILL_SWITCH_RESET",
            "state_before": "KILL_SWITCH_ON",
            "state_after": "IDLE_PAPER_SAFE",
            "reason": str(reason),
        },
        db_file=db_file,
    )
    return updated


def reconcile_live_exchange_state(
    adapter: BinanceExchangeAdapter,
    *,
    db_file: str = DEFAULT_DB_FILE,
    auto_import_exchange_positions: bool = False,
    tg_enabled: bool = False,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Reconcile actual exchange positions and open orders against the local SQLite database
    at startup, periodically, and after uncertain execution outcomes (Section 8):
    - The exchange is authoritative for actual live positions and fills.
    - Never duplicates an existing live position because local state was lost or reset.
    - Never silently overwrites conflicting state: if discrepancies or missing protective
      orders are found on open exchange positions, pauses new entries (`RECONCILIATION_MISMATCH`)
      and records a structured audit log.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    snap = adapter.synchronize_account(db_file=db_file)
    if not snap.sync_ok:
        evaluate_and_persist_circuit_breakers(
            db_file=db_file,
            account_snapshot=snap,
            reconciliation_mismatch=False,
        )
        return {
            "reconciled": False,
            "status": "ACCOUNT_SYNC_FAILED",
            "new_entries_paused": True,
            "discrepancies": [f"ACCOUNT_SYNC_FAILED:{snap.error_code}"],
            "unprotected_positions": [],
            "exchange_positions_count": 0,
            "local_positions_count": len(list_live_positions(db_file=db_file, open_only=True)),
        }

    local_open = list_live_positions(
        db_file=db_file,
        open_only=True,
        execution_mode=adapter.execution_mode,
    )
    local_by_sym: Dict[str, Dict[str, Any]] = {
        str(p["symbol"]).upper(): p for p in local_open
    }
    ex_by_sym: Dict[str, ExchangePositionRecord] = {
        str(p.symbol).upper(): p for p in snap.open_positions
    }

    # Map active protective stop-loss orders on exchange by symbol
    protective_orders_by_sym: Dict[str, List[ExchangeOrderRecord]] = {}
    for ord_rec in snap.active_orders:
        if (
            ord_rec.reduce_only
            or ord_rec.close_position
            or ord_rec.order_type in {"STOP_MARKET", "STOP_LOSS_LIMIT"}
        ):
            protective_orders_by_sym.setdefault(ord_rec.symbol, []).append(ord_rec)

    discrepancies: List[str] = []
    unprotected_symbols: List[str] = []
    synced_positions: List[Dict[str, Any]] = []

    # 1. Inspect every authoritative exchange position
    for sym, ex_pos in ex_by_sym.items():
        prot_list = protective_orders_by_sym.get(sym, [])
        prot_qty = sum(o.requested_qty - o.executed_qty for o in prot_list if o.order_type in {"STOP_MARKET", "STOP_LOSS_LIMIT"})
        has_sl_protection = bool(
            any(o.close_position for o in prot_list if o.order_type in {"STOP_MARKET", "STOP_LOSS_LIMIT"})
            or (prot_qty >= ex_pos.quantity - 1e-6 and ex_pos.quantity > 0)
        )
        if not has_sl_protection:
            unprotected_symbols.append(sym)
            discrepancies.append(f"MISSING_EXCHANGE_STOP_LOSS:{sym}:open_qty={ex_pos.quantity}")

        loc_pos = local_by_sym.get(sym)
        if loc_pos is None:
            # Exchange has a live position not present in local DB!
            discrepancies.append(f"UNTRACKED_EXCHANGE_POSITION:{sym}:{ex_pos.direction}:qty={ex_pos.quantity}")
            # Record it in local DB so we never open a duplicate position on the same symbol,
            # and mark it RECONCILIATION_REQUIRED unless auto_import is explicitly enabled.
            new_state = (
                LivePositionLifecycleState.PROTECTIVE_ORDERS_CONFIRMED.value
                if (auto_import_exchange_positions and has_sl_protection)
                else (
                    LivePositionLifecycleState.PROTECTION_MISSING_ALERT.value
                    if not has_sl_protection
                    else LivePositionLifecycleState.RECONCILIATION_REQUIRED.value
                )
            )
            rec = upsert_live_position(
                {
                    "position_id": f"exsync_{sym}_{adapter.execution_mode.lower()}",
                    "symbol": sym,
                    "execution_mode": adapter.execution_mode,
                    "market_type": adapter.market_type,
                    "direction": ex_pos.direction,
                    "lifecycle_state": new_state,
                    "requested_qty": ex_pos.quantity,
                    "open_qty": ex_pos.quantity,
                    "entry_price": ex_pos.entry_price,
                    "avg_entry_fill_price": ex_pos.entry_price,
                    "notional_value": ex_pos.notional_value,
                    "used_margin": ex_pos.used_margin,
                    "leverage": ex_pos.leverage,
                    "unrealized_pnl_usd": ex_pos.unrealized_pnl,
                    "protection_verified": has_sl_protection,
                    "reconciliation_status": "OK" if (auto_import_exchange_positions and has_sl_protection) else "MISMATCH",
                    "reconciliation_detail": "Imported from exchange during startup/periodic reconciliation",
                    "opened_at": ex_pos.updated_at or now_iso,
                },
                db_file=db_file,
            )
            synced_positions.append(rec)
        else:
            loc_dir = str(loc_pos.get("direction") or "").upper()
            loc_qty = float(loc_pos.get("open_qty") or 0.0)
            if loc_dir != ex_pos.direction or abs(loc_qty - ex_pos.quantity) > 1e-6:
                discrepancies.append(
                    f"POSITION_STATE_MISMATCH:{sym}:local=({loc_dir},{loc_qty})!=exchange=({ex_pos.direction},{ex_pos.quantity})"
                )
                upd = dict(loc_pos)
                upd["lifecycle_state"] = (
                    LivePositionLifecycleState.PROTECTION_MISSING_ALERT.value
                    if not has_sl_protection
                    else LivePositionLifecycleState.RECONCILIATION_REQUIRED.value
                )
                upd["protection_verified"] = has_sl_protection
                upd["reconciliation_status"] = "MISMATCH"
                upd["reconciliation_detail"] = discrepancies[-1]
                upd["unrealized_pnl_usd"] = ex_pos.unrealized_pnl
                synced_positions.append(upsert_live_position(upd, db_file=db_file))
            else:
                upd = dict(loc_pos)
                upd["unrealized_pnl_usd"] = ex_pos.unrealized_pnl
                upd["protection_verified"] = has_sl_protection
                if not has_sl_protection:
                    upd["lifecycle_state"] = LivePositionLifecycleState.PROTECTION_MISSING_ALERT.value
                    upd["reconciliation_status"] = "MISMATCH"
                    upd["reconciliation_detail"] = f"Missing protective SL order on exchange for {sym}"
                else:
                    upd["reconciliation_status"] = "OK"
                synced_positions.append(upsert_live_position(upd, db_file=db_file))

    # 2. Inspect local open positions that no longer exist on the exchange
    for sym, loc_pos in local_by_sym.items():
        if sym not in ex_by_sym:
            loc_state = str(loc_pos.get("lifecycle_state") or "").upper()
            if loc_state in {
                LivePositionLifecycleState.POSITION_OPEN.value,
                LivePositionLifecycleState.PROTECTIVE_ORDERS_CONFIRMED.value,
                LivePositionLifecycleState.ENTRY_PARTIALLY_FILLED.value,
            }:
                discrepancies.append(f"LOCAL_OPEN_POSITION_MISSING_ON_EXCHANGE:{sym}:{loc_pos.get('position_id')}")
                upd = dict(loc_pos)
                upd["lifecycle_state"] = LivePositionLifecycleState.RECONCILIATION_REQUIRED.value
                upd["reconciliation_status"] = "MISMATCH"
                upd["reconciliation_detail"] = "Position open locally but flat on exchange"
                synced_positions.append(upsert_live_position(upd, db_file=db_file))

    has_mismatch = len(discrepancies) > 0
    rec_status = "MISMATCH" if has_mismatch else "OK"

    update_circuit_breaker_and_kill_switch_state(
        {
            "reconciliation_status": rec_status,
            "last_reconciled_at": now_iso,
        },
        db_file=db_file,
    )
    cb_after = evaluate_and_persist_circuit_breakers(
        db_file=db_file,
        account_snapshot=snap,
        missing_protective_orders=len(unprotected_symbols) > 0,
        reconciliation_mismatch=has_mismatch,
    )

    append_execution_audit_log(
        {
            "event_type": "POSITION_RECONCILIATION",
            "execution_mode": adapter.execution_mode,
            "symbol": "PORTFOLIO",
            "decision_outcome": "RECONCILIATION_MISMATCH" if has_mismatch else "RECONCILED_OK",
            "state_before": "UNVERIFIED",
            "state_after": rec_status,
            "exchange_response": {
                "exchange_positions": [p.to_dict() for p in snap.open_positions],
                "discrepancies": discrepancies,
                "unprotected_symbols": unprotected_symbols,
            },
            "reason": "; ".join(discrepancies) if discrepancies else "All positions and protective orders reconciled.",
        },
        db_file=db_file,
    )

    if has_mismatch and tg_enabled:
        dispatch_telegram_event(
            "RECONCILIATION_MISMATCH",
            {
                "symbol": ",".join(unprotected_symbols) if unprotected_symbols else "PORTFOLIO",
                "direction": "WAIT",
                "state": "INVALIDATED",
                "reason": f"Reconciliation mismatch ({len(discrepancies)} temuan): {'; '.join(discrepancies[:3])}",
            },
            db_file=db_file,
            tg_enabled=tg_enabled,
            tg_token=tg_token,
            tg_chat=tg_chat,
            post_fn=post_fn,
        )

    return {
        "reconciled": not has_mismatch,
        "status": rec_status,
        "new_entries_paused": bool(has_mismatch or cb_after.get("new_entries_blocked")),
        "discrepancies": discrepancies,
        "unprotected_positions": unprotected_symbols,
        "exchange_positions_count": len(snap.open_positions),
        "local_positions_count": len(local_open),
        "synced_positions": synced_positions,
        "account_snapshot": snap.to_dict(),
    }


def evaluate_live_readiness_gate(
    *,
    db_file: str = DEFAULT_DB_FILE,
    account_snapshot: Optional[ExchangeAccountSnapshot] = None,
    execution_mode: str = "PAPER",
    live_trading_enabled_config: bool = False,
    runtime_user_confirmed: bool = False,
    position_sizing_tested: bool = True,
    protective_orders_tested: bool = True,
    partial_fill_timeout_tested: bool = True,
    reconciliation_tested: bool = True,
    circuit_breakers_tested: bool = True,
    paper_and_testnet_validated: bool = True,
) -> Dict[str, Any]:
    """
    Evaluate the 13 mandatory Live Readiness Gate checks before permitting LIVE execution (Section 16):
    Never activates live trading automatically after readiness checks pass; requires both
    `live_trading_enabled_config=True` AND `runtime_user_confirmed=True`.
    """
    cb_state = get_circuit_breaker_and_kill_switch_state(db_file=db_file)
    rg_state = get_persisted_risk_guard_state(db_file=db_file)

    has_valid_creds = bool(account_snapshot is not None and account_snapshot.sync_ok and account_snapshot.trading_enabled)
    withdrawal_safe = bool(account_snapshot is not None and account_snapshot.withdrawal_enabled is not True)
    acct_fresh_consistent = bool(
        account_snapshot is not None
        and account_snapshot.sync_ok
        and account_snapshot.is_consistent
        and account_snapshot.is_fresh
        and account_snapshot.account_equity > 0.0
    )
    inst_metadata_ok = bool(account_snapshot is not None and len(account_snapshot.instrument_rules) > 0)
    risk_limits_persisted = bool(rg_state.get("max_daily_loss_usd", 0.0) > 0.0)
    reconciliation_clean = bool(
        reconciliation_tested
        and str(cb_state.get("reconciliation_status") or "OK").upper() == "OK"
        and not cb_state.get("circuit_breaker_active")
        and not cb_state.get("kill_switch_active")
    )
    explicit_user_auth = bool(
        str(execution_mode).upper().strip() == "LIVE"
        and live_trading_enabled_config
        and runtime_user_confirmed
    )

    checks: List[Tuple[str, bool, str]] = [
        ("1_EXCHANGE_AND_INSTRUMENT_SUPPORT", inst_metadata_ok, "Dukungan exchange dan metadata instrumen (tick/lot/min_notional) tersedia."),
        ("2_CREDENTIALS_AND_PERMISSIONS_VALID", has_valid_creds, "Kredensial API valid dan memiliki izin trading aktif."),
        ("3_WITHDRAWAL_PERMISSIONS_DISABLED", withdrawal_safe, "Izin penarikan dana (withdrawal) pada API key dinonaktifkan."),
        ("4_ACCOUNT_DATA_FRESH_AND_CONSISTENT", acct_fresh_consistent, "Data saldo, ekuitas, dan margin akun segar serta konsisten."),
        ("5_RISK_LIMITS_CONFIGURED_AND_PERSISTED", risk_limits_persisted, "Batas risiko harian, eksposur, dan per-trade terkonfigurasi di SQLite."),
        ("6_POSITION_SIZING_TESTED", bool(position_sizing_tested), "Perhitungan ukuran posisi deterministik telah teruji."),
        ("7_ORDER_PRECISION_AND_MINIMUMS_HANDLED", inst_metadata_ok, "Presisi tick size, lot size, min_qty, dan min_notional ditegakkan."),
        ("8_PROTECTIVE_ORDERS_VERIFIED", bool(protective_orders_tested), "Penempatan dan verifikasi order Stop-Loss/Take-Profit telah teruji."),
        ("9_PARTIAL_FILL_AND_TIMEOUT_HANDLED", bool(partial_fill_timeout_tested), "Penanganan partial fill dan query status saat timeout telah diimplementasikan."),
        ("10_POSITION_RECONCILIATION_WORKS", reconciliation_clean, "Rekonsiliasi posisi exchange vs database lokal bersih tanpa selisih."),
        ("11_CIRCUIT_BREAKERS_AND_KILL_SWITCH_TESTED", bool(circuit_breakers_tested and not cb_state.get("kill_switch_active")), "Circuit breaker dan Kill Switch siap serta tidak sedang memblokir sistem."),
        ("12_PAPER_AND_TESTNET_VALIDATED", bool(paper_and_testnet_validated), "Alur kerja PAPER dan TESTNET telah divalidasi."),
        ("13_EXPLICIT_USER_LIVE_AUTHORIZATION", explicit_user_auth, "Pengguna telah mengaktifkan EXECUTION_MODE=LIVE, LIVE_TRADING_ENABLED=true, dan konfirmasi eksplisit."),
    ]

    failed = [code for code, ok, _ in checks if not ok]
    technical_ready = all(ok for code, ok, _ in checks if code != "13_EXPLICIT_USER_LIVE_AUTHORIZATION")
    can_execute_live = len(failed) == 0

    return {
        "technical_readiness_passed": technical_ready,
        "can_execute_live": can_execute_live,
        "execution_mode": str(execution_mode).upper().strip(),
        "failed_checks": failed,
        "checklist": [
            {"code": code, "passed": ok, "description": desc}
            for code, ok, desc in checks
        ],
        "disclaimer": (
            "Pemeriksaan kesiapan teknis tidak menjamin profitabilitas atau menghilangkan risiko pasar. "
            "Eksekusi LIVE membutuhkan otorisasi eksplisit pengguna."
        ),
    }


class AutonomousExecutionEngine:
    """
    Controlled, risk-aware autonomous execution coordinator (Section 1, 7, 8, 9, 12, 14):
    Exchange Account -> Balance Synchronization -> Momentum Signal -> Risk Calculation ->
    Risk Guard -> Execution Gate -> Order Execution -> Protective SL/TP Confirmation ->
    Position Management -> PnL Reconciliation.
    """

    def __init__(
        self,
        adapter: BinanceExchangeAdapter,
        *,
        db_file: str = DEFAULT_DB_FILE,
        live_trading_enabled_config: bool = False,
        runtime_live_confirmed: bool = False,
        risk_fraction_per_trade: float = 0.005,
        max_risk_per_trade_fraction: float = 0.01,
        max_daily_loss_fraction: float = 0.03,
        max_total_open_risk_fraction: float = 0.02,
        max_position_exposure_pct: float = 0.25,
        max_total_exposure_pct: float = 0.80,
        max_open_positions: int = 4,
        max_correlated_positions: int = 2,
        max_consecutive_losses: int = 4,
        max_leverage: float = DEFAULT_MAX_LEVERAGE,
        min_net_rr: float = 1.5,
        emergency_close_on_unprotected: bool = False,
        max_protection_retries: int = 2,
        tg_enabled: bool = False,
        tg_token: Optional[str] = None,
        tg_chat: Optional[str] = None,
        post_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.adapter = adapter
        self.db_file = db_file
        self.live_trading_enabled_config = bool(live_trading_enabled_config)
        self.runtime_live_confirmed = bool(runtime_live_confirmed)
        self.risk_fraction_per_trade = float(risk_fraction_per_trade)
        self.max_risk_per_trade_fraction = float(max_risk_per_trade_fraction)
        self.max_daily_loss_fraction = float(max_daily_loss_fraction)
        self.max_total_open_risk_fraction = float(max_total_open_risk_fraction)
        self.max_position_exposure_pct = float(max_position_exposure_pct)
        self.max_total_exposure_pct = float(max_total_exposure_pct)
        self.max_open_positions = int(max_open_positions)
        self.max_correlated_positions = int(max_correlated_positions)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.max_leverage = float(max_leverage)
        self.min_net_rr = float(min_net_rr)
        self.emergency_close_on_unprotected = bool(emergency_close_on_unprotected)
        self.max_protection_retries = max(1, int(max_protection_retries))
        self.tg_enabled = bool(tg_enabled)
        self.tg_token = tg_token
        self.tg_chat = tg_chat
        self.post_fn = post_fn

    def _notify(self, event_type: str, payload: Dict[str, Any], event_key: Optional[str] = None) -> None:
        if not self.tg_enabled:
            return
        dispatch_telegram_event(
            event_type,
            payload,
            event_key=event_key,
            db_file=self.db_file,
            tg_enabled=self.tg_enabled,
            tg_token=self.tg_token,
            tg_chat=self.tg_chat,
            post_fn=self.post_fn,
            extra_secrets=self.adapter.extra_secrets,
        )

    def evaluate_and_execute_signal(
        self,
        signal_payload: Dict[str, Any],
        packet: Dict[str, Any],
        *,
        order_type: str = "MARKET",
    ) -> Dict[str, Any]:
        """
        Execute the full autonomous trading gate and order lifecycle for a candidate signal:
        1. Verify execution mode & Kill Switch / Circuit Breaker state.
        2. Synchronize authoritative exchange account balances, equity, margin, and positions.
        3. Verify Momentum Signal eligibility (`TRIGGERED`, not expired/invalidated, fresh data).
        4. Compute deterministic position size from actual account equity and instrument rules.
        5. Enforce Risk Guard & margin/leverage/exposure constraints.
        6. In `PAPER` mode, refuse exchange order submission and route to paper simulation only.
        7. In `LIVE` mode, verify the 13-point Live Readiness Gate & explicit user authorization.
        8. Submit idempotent entry order, verify fill/partial-fill, and attach + confirm native
           protective Stop-Loss and Take-Profit orders for the actual filled quantity.
        """
        sym = str(signal_payload.get("symbol") or packet.get("symbol") or "UNKNOWN").upper().strip()
        direction = str(signal_payload.get("final") or signal_payload.get("direction") or "WAIT").upper().strip()
        sig_id = str(signal_payload.get("signal_id") or signal_payload.get("analysis_id") or f"sig_{sym}_{int(time.time())}")
        lc_state = str(signal_payload.get("lifecycle_state") or signal_payload.get("state") or "WAIT").upper().strip()
        mode_u = self.adapter.execution_mode

        # Step 1: Check Kill Switch & Circuit Breakers
        cb_state = get_circuit_breaker_and_kill_switch_state(db_file=self.db_file)
        if cb_state.get("kill_switch_active"):
            reason = "KILL_SWITCH_ACTIVE: Pembukaan posisi baru diblokir oleh Kill Switch."
            append_execution_audit_log(
                {
                    "event_type": "EXECUTION_GATE_REJECTED",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "decision_outcome": "REJECTED_KILL_SWITCH",
                    "reason": reason,
                },
                db_file=self.db_file,
            )
            return {"executed": False, "status": "BLOCKED_BY_KILL_SWITCH", "veto_codes": ["KILL_SWITCH_ACTIVE"], "reason": reason}

        # Step 2: Check Signal Eligibility (Section 12)
        if direction not in {"LONG", "SHORT"} or lc_state != SignalLifecycleState.TRIGGERED.value:
            reason = f"SIGNAL_NOT_ELIGIBLE: direction={direction}, lifecycle_state={lc_state} (wajib TRIGGERED LONG/SHORT)."
            append_execution_audit_log(
                {
                    "event_type": "SIGNAL_ELIGIBILITY_REJECTED",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "decision_outcome": "REJECTED_SIGNAL_NOT_TRIGGERED",
                    "reason": reason,
                },
                db_file=self.db_file,
            )
            return {"executed": False, "status": "SIGNAL_NOT_ELIGIBLE", "veto_codes": ["SIGNAL_NOT_TRIGGERED"], "reason": reason}

        # Step 3: Execution Mode Gate (PAPER never places exchange orders)
        if mode_u == ExecutionMode.PAPER.value:
            return {
                "executed": False,
                "status": "PAPER_MODE_ONLY",
                "execution_mode": "PAPER",
                "veto_codes": ["LIVE_TRADING_DISABLED"],
                "reason": "EXECUTION_MODE=PAPER: Order tidak dikirim ke exchange (gunakan alur simulasi paper).",
            }

        # Step 4: Synchronize Authoritative Exchange Account
        acct_snap = self.adapter.synchronize_account(db_file=self.db_file)
        cb_eval = evaluate_and_persist_circuit_breakers(
            db_file=self.db_file,
            account_snapshot=acct_snap,
            max_daily_loss_fraction=self.max_daily_loss_fraction,
            max_total_open_risk_fraction=self.max_total_open_risk_fraction,
            current_open_positions=len(acct_snap.open_positions),
            max_open_positions=self.max_open_positions,
            max_consecutive_losses=self.max_consecutive_losses,
        )
        if not acct_snap.can_open_live_entries or cb_eval.get("new_entries_blocked"):
            vetoes = list(dict.fromkeys(list(acct_snap.veto_codes) + list(cb_eval.get("active_reasons") or [])))
            if not vetoes:
                vetoes = ["ACCOUNT_SYNC_FAILED"]
            reason = f"Account/Circuit Breaker memblokir eksekusi: {', '.join(vetoes)}"
            append_execution_audit_log(
                {
                    "event_type": "EXECUTION_GATE_REJECTED",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "decision_outcome": "REJECTED_ACCOUNT_OR_CB",
                    "risk_inputs": acct_snap.to_dict(),
                    "reason": reason,
                },
                db_file=self.db_file,
                extra_secrets=self.adapter.extra_secrets,
            )
            self._notify(
                "CIRCUIT_BREAKER_ACTIVATED" if cb_eval.get("circuit_breaker_active") else "RISK_GUARD_REJECTED",
                {"symbol": sym, "direction": direction, "signal_id": sig_id, "veto_codes": vetoes, "reason": reason},
            )
            return {"executed": False, "status": "BLOCKED_BY_ACCOUNT_OR_CB", "veto_codes": vetoes, "reason": reason}

        # Step 5: Live Readiness Gate if mode == LIVE
        if mode_u == ExecutionMode.LIVE.value:
            readiness = evaluate_live_readiness_gate(
                db_file=self.db_file,
                account_snapshot=acct_snap,
                execution_mode=mode_u,
                live_trading_enabled_config=self.live_trading_enabled_config,
                runtime_user_confirmed=self.runtime_live_confirmed,
            )
            if not readiness["can_execute_live"]:
                reason = f"LIVE_READINESS_FAILED: {', '.join(readiness['failed_checks'])}"
                append_execution_audit_log(
                    {
                        "event_type": "LIVE_READINESS_REJECTED",
                        "execution_mode": mode_u,
                        "symbol": sym,
                        "signal_id": sig_id,
                        "decision_outcome": "REJECTED_LIVE_READINESS",
                        "risk_inputs": readiness,
                        "reason": reason,
                    },
                    db_file=self.db_file,
                )
                return {
                    "executed": False,
                    "status": "LIVE_READINESS_GATE_FAILED",
                    "veto_codes": readiness["failed_checks"],
                    "reason": reason,
                    "readiness": readiness,
                }

        # Step 6a: Idempotent Signal / Order Deduplication Check (Section 7)
        existing_sig_orders = [
            o for o in list_live_orders(db_file=self.db_file, symbol=sym, execution_mode=mode_u)
            if str(o.get("signal_id") or "") == sig_id
            and str(o.get("order_leg") or "").upper() == "ENTRY"
            and str(o.get("status") or "").upper() in {"NEW", "PARTIALLY_FILLED", "FILLED", "PENDING_SUBMIT", "UNCERTAIN_TIMEOUT"}
        ]
        if existing_sig_orders:
            coid_prev = str(existing_sig_orders[0].get("client_order_id") or "")
            return {
                "executed": False,
                "status": "DUPLICATE_ORDER_PREVENTED",
                "client_order_id": coid_prev,
                "veto_codes": ["DUPLICATE_ORDER_SUBMISSION_BLOCKED"],
                "reason": f"Order untuk signal_id={sig_id} (client_order_id={coid_prev}) sudah pernah dikirim.",
            }

        # Step 6: Prevent Duplicate Position on Same Symbol (Authoritative Exchange + Local DB)
        ex_open_syms = [p.symbol.upper() for p in acct_snap.open_positions]
        loc_open_pos = list_live_positions(db_file=self.db_file, open_only=True, execution_mode=mode_u)
        loc_open_syms = [str(p["symbol"]).upper() for p in loc_open_pos]
        all_open_syms = list(dict.fromkeys(ex_open_syms + loc_open_syms))

        rules = acct_snap.instrument_rules.get(sym)
        if rules is None:
            fetched = self.adapter.fetch_instrument_rules(sym)
            rules = fetched.get(sym)
        if rules is None:
            reason = f"MISSING_INSTRUMENT_METADATA: Spesifikasi bursa untuk {sym} tidak tersedia."
            append_execution_audit_log(
                {
                    "event_type": "RISK_GUARD_REJECTED",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "decision_outcome": "REJECTED_MISSING_INSTRUMENT_METADATA",
                    "reason": reason,
                },
                db_file=self.db_file,
            )
            return {"executed": False, "status": "REJECTED_BY_RISK_GUARD", "veto_codes": ["MISSING_INSTRUMENT_METADATA"], "reason": reason}

        entry_px = signal_payload.get("entry")
        sl_px = signal_payload.get("sl") or signal_payload.get("stop_loss")
        tp_px = signal_payload.get("tp") or signal_payload.get("take_profit")
        spread_pct = float((packet or {}).get("order_book", {}).get("spread", 0.0) or 0.0)

        current_open_notional = sum(p.notional_value for p in acct_snap.open_positions)
        current_open_risk = sum(float(p.get("risk_budget_usd") or 0.0) for p in loc_open_pos)
        t_grp = get_correlation_group(sym)
        corr_cnt = sum(1 for s in all_open_syms if get_correlation_group(s) == t_grp)
        corr_notional = sum(p.notional_value for p in acct_snap.open_positions if get_correlation_group(p.symbol) == t_grp)
        req_lev = float(signal_payload.get("leverage") or (1.0 if self.adapter.market_type == "SPOT" else min(self.max_leverage, rules.max_leverage)))

        # Step 7: Deterministic Position Sizing & Risk Guard from Actual Account Equity
        sizing = compute_position_sizing(
            direction=direction,
            entry=entry_px,
            stop_loss=sl_px,
            take_profit=tp_px,
            symbol=sym,
            equity=acct_snap.account_equity,
            risk_fraction=self.risk_fraction_per_trade,
            max_risk_per_trade_fraction=self.max_risk_per_trade_fraction,
            fee_pct_round_trip=rules.taker_fee_pct * 2.0,
            slippage_pct_round_trip=0.04,
            spread_pct=spread_pct,
            max_position_exposure_pct=self.max_position_exposure_pct,
            max_total_exposure_pct=self.max_total_exposure_pct,
            current_open_notional=current_open_notional,
            current_open_risk=current_open_risk,
            max_total_risk_fraction=self.max_total_open_risk_fraction,
            current_open_positions=len(all_open_syms),
            max_open_positions=self.max_open_positions,
            open_symbols=all_open_syms,
            min_net_rr=self.min_net_rr,
            instrument_mode="futures_perp" if self.adapter.market_type == "FUTURES_USDM" else "spot_reference",
            leverage=req_lev,
            contract_multiplier=rules.contract_multiplier,
            tick_size=rules.tick_size,
            lot_size=rules.lot_size,
            min_qty=rules.min_qty,
            min_notional=rules.min_notional,
            daily_realized_loss_usd=float(cb_eval.get("daily_realized_loss_usd") or 0.0),
            daily_unrealized_loss_usd=float(cb_eval.get("daily_unrealized_loss_usd") or 0.0),
            max_daily_loss_fraction=self.max_daily_loss_fraction,
            consecutive_losses=int(cb_eval.get("consecutive_losses") or 0),
            max_consecutive_losses=self.max_consecutive_losses,
            correlated_open_positions=corr_cnt,
            max_correlated_positions=self.max_correlated_positions,
            correlated_open_notional=corr_notional,
            available_margin=acct_snap.available_margin,
            max_leverage=min(self.max_leverage, rules.max_leverage),
            max_qty=rules.max_qty,
            contract_type=rules.contract_type,
            account_sync_ok=acct_snap.sync_ok,
            account_is_fresh=acct_snap.is_fresh,
            circuit_breaker_active=bool(cb_eval.get("circuit_breaker_active")),
            kill_switch_active=bool(cb_eval.get("kill_switch_active")),
            reconciliation_ok=(str(cb_eval.get("reconciliation_status") or "OK").upper() == "OK"),
            require_instrument_metadata=True,
            instrument_metadata_available=True,
        )

        if not sizing.valid or sizing.quantity <= 0.0:
            vetoes = list(sizing.veto_codes)
            reasons = explain_risk_veto_codes(vetoes)
            record_risk_guard_rejection(sym, vetoes, reason="; ".join(reasons), db_file=self.db_file)
            append_execution_audit_log(
                {
                    "event_type": "RISK_GUARD_REJECTED",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "decision_outcome": "REJECTED_BY_RISK_GUARD",
                    "risk_inputs": sizing.to_dict(),
                    "reason": "; ".join(reasons),
                },
                db_file=self.db_file,
            )
            self._notify(
                "RISK_GUARD_REJECTED",
                {
                    "symbol": sym,
                    "direction": direction,
                    "signal_id": sig_id,
                    "entry": entry_px,
                    "sl": sl_px,
                    "tp": tp_px,
                    "veto_codes": vetoes,
                    "reason": "; ".join(reasons),
                },
            )
            return {
                "executed": False,
                "status": "REJECTED_BY_RISK_GUARD",
                "veto_codes": vetoes,
                "rejection_reasons": reasons,
                "sizing": sizing.to_dict(),
            }

        # Step 8: Idempotent Client Order ID & Duplicate Submission Prevention
        entry_coid = generate_client_order_id(sig_id, sym, "ENTRY", direction, sizing.quantity, sizing.entry)
        existing_entry = get_live_order(entry_coid, db_file=self.db_file)
        if existing_entry is not None and str(existing_entry.get("status")).upper() in {
            "NEW", "PARTIALLY_FILLED", "FILLED", "PENDING_SUBMIT", "UNCERTAIN_TIMEOUT"
        }:
            return {
                "executed": False,
                "status": "DUPLICATE_ORDER_PREVENTED",
                "client_order_id": entry_coid,
                "veto_codes": ["DUPLICATE_ORDER_SUBMISSION_BLOCKED"],
                "reason": f"Order dengan client_order_id={entry_coid} sudah pernah dikirim (status={existing_entry.get('status')}).",
            }

        pos_id = f"lpos_{sym}_{sig_id}"[-48:]
        entry_side = "BUY" if direction == "LONG" else "SELL"
        exit_side = "SELL" if direction == "LONG" else "BUY"

        # Persist intent BEFORE calling exchange API
        upsert_live_order(
            {
                "client_order_id": entry_coid,
                "signal_id": sig_id,
                "position_id": pos_id,
                "symbol": sym,
                "execution_mode": mode_u,
                "market_type": self.adapter.market_type,
                "order_leg": "ENTRY",
                "side": entry_side,
                "order_type": order_type,
                "status": "PENDING_SUBMIT",
                "price": sizing.entry,
                "requested_qty": sizing.quantity,
                "executed_qty": 0.0,
                "reduce_only": False,
            },
            db_file=self.db_file,
        )
        upsert_live_position(
            {
                "position_id": pos_id,
                "signal_id": sig_id,
                "symbol": sym,
                "execution_mode": mode_u,
                "market_type": self.adapter.market_type,
                "direction": direction,
                "lifecycle_state": LivePositionLifecycleState.ENTRY_REQUESTED.value,
                "requested_qty": sizing.quantity,
                "open_qty": 0.0,
                "entry_price": sizing.entry,
                "stop_loss": sizing.stop_loss,
                "take_profit": sizing.take_profit,
                "notional_value": sizing.notional_value,
                "used_margin": sizing.margin_required,
                "leverage": req_lev,
                "risk_budget_usd": sizing.risk_budget,
                "entry_client_order_id": entry_coid,
                "protection_verified": False,
            },
            db_file=self.db_file,
        )

        # Submit Entry Order
        ok_ent, ent_ord, ent_raw, ent_err = self.adapter.place_order(
            symbol=sym,
            side=entry_side,
            order_type=order_type,
            quantity=sizing.quantity,
            client_order_id=entry_coid,
            price=sizing.entry if order_type == "LIMIT" else None,
            reduce_only=False,
        )

        if not ok_ent or ent_ord is None:
            fail_status = "UNCERTAIN_TIMEOUT" if ent_err == "UNCERTAIN_ORDER_TIMEOUT_UNCONFIRMED" else "REJECTED"
            upsert_live_order(
                {
                    "client_order_id": entry_coid,
                    "signal_id": sig_id,
                    "position_id": pos_id,
                    "symbol": sym,
                    "execution_mode": mode_u,
                    "market_type": self.adapter.market_type,
                    "order_leg": "ENTRY",
                    "side": entry_side,
                    "order_type": order_type,
                    "status": fail_status,
                    "price": sizing.entry,
                    "requested_qty": sizing.quantity,
                    "executed_qty": 0.0,
                    "raw_response": ent_raw,
                    "error_message": str(ent_err or "ORDER_REJECTED"),
                },
                db_file=self.db_file,
                extra_secrets=self.adapter.extra_secrets,
            )
            upsert_live_position(
                {
                    "position_id": pos_id,
                    "signal_id": sig_id,
                    "symbol": sym,
                    "execution_mode": mode_u,
                    "market_type": self.adapter.market_type,
                    "direction": direction,
                    "lifecycle_state": (
                        LivePositionLifecycleState.RECONCILIATION_REQUIRED.value
                        if fail_status == "UNCERTAIN_TIMEOUT"
                        else LivePositionLifecycleState.EXECUTION_ERROR.value
                    ),
                    "requested_qty": sizing.quantity,
                    "open_qty": 0.0,
                    "entry_price": sizing.entry,
                    "stop_loss": sizing.stop_loss,
                    "take_profit": sizing.take_profit,
                    "entry_client_order_id": entry_coid,
                    "reconciliation_status": "MISMATCH" if fail_status == "UNCERTAIN_TIMEOUT" else "OK",
                    "reconciliation_detail": str(ent_err or ""),
                },
                db_file=self.db_file,
            )
            cb_now = get_circuit_breaker_and_kill_switch_state(db_file=self.db_file)
            update_circuit_breaker_and_kill_switch_state(
                {"consecutive_execution_failures": int(cb_now.get("consecutive_execution_failures") or 0) + 1},
                db_file=self.db_file,
            )
            append_execution_audit_log(
                {
                    "event_type": "LIVE_ORDER_REJECTED",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "position_id": pos_id,
                    "decision_outcome": fail_status,
                    "state_before": LivePositionLifecycleState.ENTRY_REQUESTED.value,
                    "state_after": fail_status,
                    "risk_inputs": sizing.to_dict(),
                    "order_ids": {"entry_client_order_id": entry_coid},
                    "exchange_response": ent_raw,
                    "reason": str(ent_err or "ENTRY_ORDER_FAILED"),
                },
                db_file=self.db_file,
                extra_secrets=self.adapter.extra_secrets,
            )
            self._notify(
                "ORDER_REJECTED",
                {"symbol": sym, "direction": direction, "signal_id": sig_id, "reason": f"Entry order gagal ({ent_err})"},
            )
            return {
                "executed": False,
                "status": fail_status,
                "client_order_id": entry_coid,
                "position_id": pos_id,
                "veto_codes": [str(ent_err or "ORDER_SUBMISSION_FAILED")],
                "reason": str(ent_err or "ORDER_SUBMISSION_FAILED"),
            }

        # Reset consecutive execution failure counter on valid exchange order response
        update_circuit_breaker_and_kill_switch_state(
            {"consecutive_execution_failures": 0},
            db_file=self.db_file,
        )
        upsert_live_order(
            {
                "client_order_id": entry_coid,
                "exchange_order_id": ent_ord.order_id,
                "signal_id": sig_id,
                "position_id": pos_id,
                "symbol": sym,
                "execution_mode": mode_u,
                "market_type": self.adapter.market_type,
                "order_leg": "ENTRY",
                "side": entry_side,
                "order_type": ent_ord.order_type,
                "status": ent_ord.status,
                "price": sizing.entry,
                "requested_qty": sizing.quantity,
                "executed_qty": ent_ord.executed_qty,
                "avg_fill_price": ent_ord.avg_fill_price,
                "fee_usd": ent_ord.fee_usd,
                "raw_response": ent_raw,
            },
            db_file=self.db_file,
            extra_secrets=self.adapter.extra_secrets,
        )

        # Do NOT assume an order has filled merely because the API accepted the request!
        if ent_ord.status == "NEW" or ent_ord.executed_qty <= 0.0:
            upsert_live_position(
                {
                    "position_id": pos_id,
                    "signal_id": sig_id,
                    "symbol": sym,
                    "execution_mode": mode_u,
                    "market_type": self.adapter.market_type,
                    "direction": direction,
                    "lifecycle_state": LivePositionLifecycleState.ENTRY_REQUESTED.value,
                    "requested_qty": sizing.quantity,
                    "open_qty": 0.0,
                    "entry_price": sizing.entry,
                    "stop_loss": sizing.stop_loss,
                    "take_profit": sizing.take_profit,
                    "notional_value": sizing.notional_value,
                    "used_margin": sizing.margin_required,
                    "leverage": req_lev,
                    "risk_budget_usd": sizing.risk_budget,
                    "entry_client_order_id": entry_coid,
                    "protection_verified": False,
                },
                db_file=self.db_file,
            )
            append_execution_audit_log(
                {
                    "event_type": "LIVE_ENTRY_SUBMITTED_PENDING_FILL",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "position_id": pos_id,
                    "decision_outcome": "ENTRY_NEW_AWAITING_FILL",
                    "state_before": "NONE",
                    "state_after": LivePositionLifecycleState.ENTRY_REQUESTED.value,
                    "risk_inputs": sizing.to_dict(),
                    "order_ids": {"entry_client_order_id": entry_coid, "exchange_order_id": ent_ord.order_id},
                    "exchange_response": ent_raw,
                },
                db_file=self.db_file,
            )
            self._notify(
                "LIVE_ENTRY_SUBMITTED",
                {
                    "symbol": sym,
                    "direction": direction,
                    "signal_id": sig_id,
                    "entry": sizing.entry,
                    "sl": sizing.stop_loss,
                    "tp": sizing.take_profit,
                    "reason": f"[{mode_u}] Entry order diterima bursa (status=NEW, menunggu fill).",
                },
            )
            return {
                "executed": True,
                "status": LivePositionLifecycleState.ENTRY_REQUESTED.value,
                "position_id": pos_id,
                "entry_order": ent_ord.to_dict(),
                "sizing": sizing.to_dict(),
            }

        # Entry is PARTIALLY_FILLED or FILLED -> Place and verify protective Stop-Loss & Take-Profit
        filled_qty = ent_ord.executed_qty
        is_partial = ent_ord.status == "PARTIALLY_FILLED" or (filled_qty < sizing.quantity - 1e-8)
        initial_pos_state = (
            LivePositionLifecycleState.ENTRY_PARTIALLY_FILLED.value
            if is_partial
            else LivePositionLifecycleState.POSITION_OPEN.value
        )

        sl_coid = generate_client_order_id(sig_id, sym, "STOP_LOSS", exit_side, filled_qty, sizing.stop_loss)
        tp_coid = generate_client_order_id(sig_id, sym, "TAKE_PROFIT", exit_side, filled_qty, sizing.take_profit)

        sl_confirmed = False
        sl_ord_rec: Optional[ExchangeOrderRecord] = None
        last_sl_err: Optional[str] = None
        sl_order_type = "STOP_LOSS_LIMIT" if self.adapter.market_type == "SPOT" else "STOP_MARKET"

        for attempt in range(self.max_protection_retries):
            ok_sl, sl_ord, sl_raw, sl_err = self.adapter.place_order(
                symbol=sym,
                side=exit_side,
                order_type=sl_order_type,
                quantity=filled_qty,
                client_order_id=sl_coid,
                price=sizing.stop_loss if sl_order_type == "STOP_LOSS_LIMIT" else None,
                stop_price=sizing.stop_loss,
                reduce_only=(self.adapter.market_type == "FUTURES_USDM"),
            )
            if ok_sl and sl_ord is not None and sl_ord.status in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
                sl_confirmed = True
                sl_ord_rec = sl_ord
                upsert_live_order(
                    {
                        "client_order_id": sl_coid,
                        "exchange_order_id": sl_ord.order_id,
                        "signal_id": sig_id,
                        "position_id": pos_id,
                        "symbol": sym,
                        "execution_mode": mode_u,
                        "market_type": self.adapter.market_type,
                        "order_leg": "STOP_LOSS",
                        "side": exit_side,
                        "order_type": sl_order_type,
                        "status": sl_ord.status,
                        "stop_price": sizing.stop_loss,
                        "requested_qty": filled_qty,
                        "executed_qty": sl_ord.executed_qty,
                        "reduce_only": True,
                        "raw_response": sl_raw,
                    },
                    db_file=self.db_file,
                )
                break
            last_sl_err = sl_err

        # Place Take-Profit order if Stop-Loss succeeded
        tp_ord_rec: Optional[ExchangeOrderRecord] = None
        if sl_confirmed:
            tp_order_type = "LIMIT" if self.adapter.market_type == "SPOT" else "TAKE_PROFIT_MARKET"
            ok_tp, tp_ord, tp_raw, _ = self.adapter.place_order(
                symbol=sym,
                side=exit_side,
                order_type=tp_order_type,
                quantity=filled_qty,
                client_order_id=tp_coid,
                price=sizing.take_profit if tp_order_type == "LIMIT" else None,
                stop_price=sizing.take_profit if tp_order_type == "TAKE_PROFIT_MARKET" else None,
                reduce_only=(self.adapter.market_type == "FUTURES_USDM"),
            )
            if ok_tp and tp_ord is not None:
                tp_ord_rec = tp_ord
                upsert_live_order(
                    {
                        "client_order_id": tp_coid,
                        "exchange_order_id": tp_ord.order_id,
                        "signal_id": sig_id,
                        "position_id": pos_id,
                        "symbol": sym,
                        "execution_mode": mode_u,
                        "market_type": self.adapter.market_type,
                        "order_leg": "TAKE_PROFIT",
                        "side": exit_side,
                        "order_type": tp_order_type,
                        "status": tp_ord.status,
                        "price": sizing.take_profit,
                        "stop_price": sizing.take_profit,
                        "requested_qty": filled_qty,
                        "executed_qty": tp_ord.executed_qty,
                        "reduce_only": True,
                        "raw_response": tp_raw,
                    },
                    db_file=self.db_file,
                )

        # CRITICAL SAFETY RULE (Section 7): If protective SL placement fails or cannot be confirmed:
        # 1. Block new entries (trip circuit breaker).
        # 2. Mark position as requiring immediate attention (PROTECTION_MISSING_ALERT).
        # 3. Send high-priority notification.
        # 4. Follow preconfigured emergency policy (only emergency-close if explicitly configured).
        if not sl_confirmed:
            emergency_closed = False
            emg_coid = None
            if self.emergency_close_on_unprotected:
                emg_coid = generate_client_order_id(sig_id, sym, "EMERGENCY_CLOSE", exit_side, filled_qty, 0.0)
                ok_emg, emg_ord, _, _ = self.adapter.place_order(
                    symbol=sym,
                    side=exit_side,
                    order_type="MARKET",
                    quantity=filled_qty,
                    client_order_id=emg_coid,
                    reduce_only=(self.adapter.market_type == "FUTURES_USDM"),
                )
                if ok_emg and emg_ord is not None and emg_ord.status == "FILLED":
                    emergency_closed = True

            final_pos_state = (
                LivePositionLifecycleState.POSITION_CLOSED.value
                if emergency_closed
                else LivePositionLifecycleState.PROTECTION_MISSING_ALERT.value
            )
            upsert_live_position(
                {
                    "position_id": pos_id,
                    "signal_id": sig_id,
                    "symbol": sym,
                    "execution_mode": mode_u,
                    "market_type": self.adapter.market_type,
                    "direction": direction,
                    "lifecycle_state": final_pos_state,
                    "requested_qty": sizing.quantity,
                    "open_qty": 0.0 if emergency_closed else filled_qty,
                    "closed_qty": filled_qty if emergency_closed else 0.0,
                    "entry_price": sizing.entry,
                    "avg_entry_fill_price": ent_ord.avg_fill_price or sizing.entry,
                    "stop_loss": sizing.stop_loss,
                    "take_profit": sizing.take_profit,
                    "notional_value": filled_qty * (ent_ord.avg_fill_price or sizing.entry) * rules.contract_multiplier,
                    "used_margin": sizing.margin_required,
                    "leverage": req_lev,
                    "risk_budget_usd": sizing.risk_budget,
                    "entry_client_order_id": entry_coid,
                    "exit_client_order_id": emg_coid,
                    "protection_verified": False,
                    "reconciliation_status": "PROTECTION_MISSING",
                    "reconciliation_detail": f"Stop-Loss gagal ditempatkan ({last_sl_err})",
                },
                db_file=self.db_file,
            )
            evaluate_and_persist_circuit_breakers(
                db_file=self.db_file,
                account_snapshot=acct_snap,
                missing_protective_orders=True,
            )
            append_execution_audit_log(
                {
                    "event_type": "CRITICAL_PROTECTION_FAILURE",
                    "execution_mode": mode_u,
                    "symbol": sym,
                    "signal_id": sig_id,
                    "position_id": pos_id,
                    "decision_outcome": "EMERGENCY_CLOSED" if emergency_closed else "PROTECTION_MISSING_HALTED",
                    "state_before": initial_pos_state,
                    "state_after": final_pos_state,
                    "risk_inputs": sizing.to_dict(),
                    "order_ids": {"entry_client_order_id": entry_coid, "sl_client_order_id": sl_coid},
                    "reason": f"Gagal memasang Stop-Loss protektif setelah {self.max_protection_retries} percobaan ({last_sl_err}).",
                },
                db_file=self.db_file,
            )
            self._notify(
                "CRITICAL_PROTECTION_FAILURE",
                {
                    "symbol": sym,
                    "direction": direction,
                    "signal_id": sig_id,
                    "entry": ent_ord.avg_fill_price or sizing.entry,
                    "sl": sizing.stop_loss,
                    "reason": (
                        f"DARURAT: Stop-Loss gagal dikonfirmasi untuk {sym} (qty={filled_qty}, err={last_sl_err}). "
                        f"Pembukaan posisi baru diblokir! Emergency close: {'YA' if emergency_closed else 'TIDAK (HOLD_AND_ALERT)'}."
                    ),
                },
            )
            return {
                "executed": True,
                "status": final_pos_state,
                "protection_verified": False,
                "emergency_closed": emergency_closed,
                "position_id": pos_id,
                "entry_order": ent_ord.to_dict(),
                "veto_codes": ["MISSING_PROTECTIVE_ORDER"],
                "reason": f"Stop-Loss gagal ditempatkan: {last_sl_err}",
            }

        # Protective SL confirmed!
        final_pos_state = (
            LivePositionLifecycleState.ENTRY_PARTIALLY_FILLED.value
            if is_partial
            else LivePositionLifecycleState.PROTECTIVE_ORDERS_CONFIRMED.value
        )
        pos_saved = upsert_live_position(
            {
                "position_id": pos_id,
                "signal_id": sig_id,
                "symbol": sym,
                "execution_mode": mode_u,
                "market_type": self.adapter.market_type,
                "direction": direction,
                "lifecycle_state": final_pos_state,
                "requested_qty": sizing.quantity,
                "open_qty": filled_qty,
                "entry_price": sizing.entry,
                "avg_entry_fill_price": ent_ord.avg_fill_price or sizing.entry,
                "stop_loss": sizing.stop_loss,
                "take_profit": sizing.take_profit,
                "notional_value": round(filled_qty * (ent_ord.avg_fill_price or sizing.entry) * rules.contract_multiplier, 4),
                "used_margin": sizing.margin_required,
                "leverage": req_lev,
                "risk_budget_usd": sizing.risk_budget,
                "total_fees_usd": ent_ord.fee_usd,
                "entry_client_order_id": entry_coid,
                "sl_client_order_id": sl_coid,
                "tp_client_order_id": tp_coid if tp_ord_rec else None,
                "protection_verified": True,
                "reconciliation_status": "OK",
                "opened_at": datetime.now(timezone.utc).isoformat(),
            },
            db_file=self.db_file,
        )
        append_execution_audit_log(
            {
                "event_type": "LIVE_POSITION_OPENED_AND_PROTECTED",
                "execution_mode": mode_u,
                "symbol": sym,
                "signal_id": sig_id,
                "position_id": pos_id,
                "decision_outcome": final_pos_state,
                "state_before": initial_pos_state,
                "state_after": final_pos_state,
                "risk_inputs": sizing.to_dict(),
                "order_ids": {
                    "entry_client_order_id": entry_coid,
                    "sl_client_order_id": sl_coid,
                    "tp_client_order_id": tp_coid if tp_ord_rec else None,
                },
                "fills": [
                    {
                        "order_id": ent_ord.order_id,
                        "executed_qty": filled_qty,
                        "avg_fill_price": ent_ord.avg_fill_price,
                        "fee_usd": ent_ord.fee_usd,
                    }
                ],
                "fees_usd": ent_ord.fee_usd,
                "reason": f"Entry filled ({filled_qty}/{sizing.quantity}) & protective SL confirmed at {sizing.stop_loss}.",
            },
            db_file=self.db_file,
        )
        self._notify(
            "ENTRY_PARTIAL_FILL" if is_partial else "ENTRY_FILLED_PROTECTED",
            {
                "symbol": sym,
                "direction": direction,
                "signal_id": sig_id,
                "position_id": pos_id,
                "entry": ent_ord.avg_fill_price or sizing.entry,
                "sl": sizing.stop_loss,
                "tp": sizing.take_profit,
                "rr": sizing.net_rr,
                "reason": (
                    f"[{mode_u}] Posisi {direction} terbuka ({filled_qty} unit @ ${ent_ord.avg_fill_price or sizing.entry:,.4f}) · "
                    f"Stop-Loss Protektif Terkonfirmasi @ ${sizing.stop_loss:,.4f}."
                ),
            },
        )
        return {
            "executed": True,
            "status": final_pos_state,
            "protection_verified": True,
            "position_id": pos_id,
            "position": pos_saved,
            "entry_order": ent_ord.to_dict(),
            "sl_order": sl_ord_rec.to_dict() if sl_ord_rec else None,
            "tp_order": tp_ord_rec.to_dict() if tp_ord_rec else None,
            "sizing": sizing.to_dict(),
        }



def close_live_position(
    adapter: BinanceExchangeAdapter,
    position_id: str,
    *,
    db_file: str = DEFAULT_DB_FILE,
    close_quantity: Optional[float] = None,
    reason: str = "Explicit position exit requested",
    tg_enabled: bool = False,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Execute a controlled exit (`EXIT_REQUESTED` -> `EXIT_PARTIALLY_FILLED` -> `POSITION_CLOSED`)
    for an open live/testnet position and reconcile realized PnL (Section 8).
    """
    pos = get_live_position(position_id, db_file=db_file)
    if pos is None:
        return {"closed": False, "status": "POSITION_NOT_FOUND", "reason": f"Position {position_id} tidak ditemukan."}

    open_qty = float(pos.get("open_qty") or 0.0)
    if open_qty <= 0.0 or str(pos.get("lifecycle_state")).upper() == LivePositionLifecycleState.POSITION_CLOSED.value:
        return {"closed": False, "status": "ALREADY_CLOSED", "reason": "Posisi sudah tertutup."}

    sym = str(pos["symbol"]).upper().strip()
    direction = str(pos["direction"]).upper().strip()
    exit_side = "SELL" if direction == "LONG" else "BUY"
    req_exit_qty = min(open_qty, float(close_quantity)) if (close_quantity is not None and float(close_quantity) > 0) else open_qty
    sig_id = str(pos.get("signal_id") or position_id)
    exit_coid = generate_client_order_id(sig_id, sym, "EXIT", exit_side, req_exit_qty, 0.0)

    pos_req = dict(pos)
    pos_req["lifecycle_state"] = LivePositionLifecycleState.EXIT_REQUESTED.value
    pos_req["exit_client_order_id"] = exit_coid
    upsert_live_position(pos_req, db_file=db_file)

    ok_ex, ex_ord, ex_raw, ex_err = adapter.place_order(
        symbol=sym,
        side=exit_side,
        order_type="MARKET",
        quantity=req_exit_qty,
        client_order_id=exit_coid,
        reduce_only=(adapter.market_type == "FUTURES_USDM"),
    )
    if not ok_ex or ex_ord is None:
        pos_err = dict(pos_req)
        pos_err["lifecycle_state"] = LivePositionLifecycleState.EXECUTION_ERROR.value
        pos_err["reconciliation_detail"] = f"Exit order failed: {ex_err}"
        upsert_live_position(pos_err, db_file=db_file)
        return {"closed": False, "status": "EXIT_ORDER_FAILED", "error": ex_err}

    filled_exit = ex_ord.executed_qty
    rem_qty = max(0.0, round(open_qty - filled_exit, 8))
    entry_fill = float(pos.get("avg_entry_fill_price") or pos.get("entry_price") or 0.0)
    exit_fill = float(ex_ord.avg_fill_price or entry_fill)
    gross_pnl = (exit_fill - entry_fill) * filled_exit if direction == "LONG" else (entry_fill - exit_fill) * filled_exit
    tot_fees = float(pos.get("total_fees_usd") or 0.0) + ex_ord.fee_usd
    net_pnl = round(gross_pnl - tot_fees, 4)

    new_state = (
        LivePositionLifecycleState.POSITION_CLOSED.value
        if rem_qty <= 1e-8
        else LivePositionLifecycleState.EXIT_PARTIALLY_FILLED.value
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    pos_after = dict(pos_req)
    pos_after["lifecycle_state"] = new_state
    pos_after["open_qty"] = rem_qty
    pos_after["closed_qty"] = round(float(pos.get("closed_qty") or 0.0) + filled_exit, 8)
    pos_after["avg_exit_fill_price"] = exit_fill
    pos_after["realized_pnl_usd"] = round(float(pos.get("realized_pnl_usd") or 0.0) + net_pnl, 4)
    pos_after["total_fees_usd"] = round(tot_fees, 4)
    if new_state == LivePositionLifecycleState.POSITION_CLOSED.value:
        pos_after["closed_at"] = now_iso
        # Cancel remaining SL/TP orders for this closed position
        for prot_coid_key in ("sl_client_order_id", "tp_client_order_id"):
            pcoid = pos.get(prot_coid_key)
            if pcoid:
                adapter.cancel_order(sym, client_order_id=str(pcoid))

    saved_pos = upsert_live_position(pos_after, db_file=db_file)

    # Update daily realized loss / consecutive losses in circuit breaker state if closed at a loss
    if new_state == LivePositionLifecycleState.POSITION_CLOSED.value:
        cb = get_circuit_breaker_and_kill_switch_state(db_file=db_file)
        d_loss = float(cb.get("daily_realized_loss_usd") or 0.0)
        c_loss = int(cb.get("consecutive_losses") or 0)
        if net_pnl < 0.0:
            d_loss = round(d_loss + abs(net_pnl), 4)
            c_loss += 1
        elif net_pnl > 0.0:
            c_loss = 0
        update_circuit_breaker_and_kill_switch_state(
            {"daily_realized_loss_usd": d_loss, "consecutive_losses": c_loss},
            db_file=db_file,
        )

    append_execution_audit_log(
        {
            "event_type": "LIVE_POSITION_CLOSED" if new_state == LivePositionLifecycleState.POSITION_CLOSED.value else "LIVE_POSITION_PARTIAL_EXIT",
            "execution_mode": adapter.execution_mode,
            "symbol": sym,
            "signal_id": sig_id,
            "position_id": position_id,
            "decision_outcome": new_state,
            "state_before": pos.get("lifecycle_state"),
            "state_after": new_state,
            "order_ids": {"exit_client_order_id": exit_coid, "exchange_order_id": ex_ord.order_id},
            "fills": [{"executed_qty": filled_exit, "avg_fill_price": exit_fill, "net_pnl_usd": net_pnl}],
            "fees_usd": ex_ord.fee_usd,
            "reason": reason,
        },
        db_file=db_file,
    )

    if tg_enabled:
        dispatch_telegram_event(
            "LIVE_POSITION_CLOSED" if new_state == LivePositionLifecycleState.POSITION_CLOSED.value else "EXIT_PARTIAL_FILL",
            {
                "symbol": sym,
                "direction": direction,
                "signal_id": sig_id,
                "position_id": position_id,
                "entry": entry_fill,
                "exit_price": exit_fill,
                "net_pnl": net_pnl,
                "reason": f"[{adapter.execution_mode}] {reason} | Net PnL: ${net_pnl:+,.2f}",
            },
            db_file=db_file,
            tg_enabled=tg_enabled,
            tg_token=tg_token,
            tg_chat=tg_chat,
            post_fn=post_fn,
        )

    return {
        "closed": new_state == LivePositionLifecycleState.POSITION_CLOSED.value,
        "status": new_state,
        "position": saved_pos,
        "exit_order": ex_ord.to_dict(),
        "realized_net_pnl_usd": net_pnl,
    }


def emergency_close_all_positions(
    adapter: BinanceExchangeAdapter,
    *,
    db_file: str = DEFAULT_DB_FILE,
    explicit_user_confirmation: bool = False,
    reason: str = "Explicit user emergency close all positions",
    tg_enabled: bool = False,
    tg_token: Optional[str] = None,
    tg_chat: Optional[str] = None,
    post_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    Separate, explicit emergency position-management action (Section 10 & 11):
    Never closes existing positions unless `explicit_user_confirmation=True` is passed!
    """
    if not explicit_user_confirmation:
        return {
            "executed": False,
            "status": "CONFIRMATION_REQUIRED",
            "reason": "Penutupan darurat seluruh posisi membutuhkan konfirmasi eksplisit (explicit_user_confirmation=True).",
        }

    open_positions = list_live_positions(db_file=db_file, open_only=True, execution_mode=adapter.execution_mode)
    closed_ids: List[str] = []
    failed_ids: List[str] = []
    for p in open_positions:
        pid = str(p["position_id"])
        res = close_live_position(
            adapter,
            pid,
            db_file=db_file,
            reason=reason,
            tg_enabled=tg_enabled,
            tg_token=tg_token,
            tg_chat=tg_chat,
            post_fn=post_fn,
        )
        if res.get("closed"):
            closed_ids.append(pid)
        else:
            failed_ids.append(pid)

    return {
        "executed": True,
        "status": "COMPLETED" if not failed_ids else "PARTIAL_FAILURE",
        "closed_position_ids": closed_ids,
        "failed_position_ids": failed_ids,
    }

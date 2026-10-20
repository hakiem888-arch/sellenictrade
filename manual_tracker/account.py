"""Phase 1: Exchange Account Intelligence & Position Reconciliation.

Implements authenticated Binance Spot (`SPOT`) and USD-M Perpetual (`LINEAR`)
account synchronization, freshness validation, API permission verification,
instrument metadata parsing, and exchange-authoritative position/order reconciliation.
"""
from collections import deque
from dataclasses import dataclass, field, asdict
from decimal import Decimal
import hashlib
import hmac
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import AutonomousConfig, redact_text
from .engine import InvalidData, fmt, number, symbol_name
from .store import Store, dumps, uid


class AccountSyncError(Exception):
    def __init__(self, code="ACCOUNT_SYNC_FAILED", message="", status_code=None):
        self.code = code
        self.status_code = status_code
        super().__init__(f"{code}: {message}" if message else code)


ExchangeHOSTS = {
    ("TESTNET", "SPOT"): "https://testnet.binance.vision",
    ("TESTNET", "LINEAR"): "https://testnet.binancefuture.com",
    ("LIVE", "SPOT"): "https://api.binance.com",
    ("LIVE", "LINEAR"): "https://fapi.binance.com",
    ("PAPER", "SPOT"): "https://data-api.binance.vision",
    ("PAPER", "LINEAR"): "https://fapi.binance.com",
}


@dataclass(frozen=True)
class InstrumentSpec:
    instrument: str
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    contract_type: str
    contract_multiplier: Decimal
    price_precision: int
    quantity_precision: int
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_leverage: int
    maker_fee: Decimal = Decimal("0.0002")
    taker_fee: Decimal = Decimal("0.0004")
    funding_rate: Decimal = Decimal("0")
    checked_at: float = 0.0


@dataclass(frozen=True)
class ExchangePosition:
    instrument: str
    symbol: str
    direction: str  # LONG | SHORT
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: Decimal
    margin_mode: str  # ISOLATED | CROSSED | SPOT
    isolated_margin: Decimal = Decimal("0")
    liquidation_price: Decimal = Decimal("0")
    updated_at: float = 0.0


@dataclass(frozen=True)
class ExchangeOrder:
    instrument: str
    symbol: str
    order_id: str
    client_order_id: str
    side: str          # BUY | SELL
    position_side: str # BOTH | LONG | SHORT
    order_type: str    # MARKET | LIMIT | STOP_MARKET | TAKE_PROFIT_MARKET | STOP | TAKE_PROFIT
    status: str        # NEW | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | EXPIRED
    orig_qty: Decimal
    executed_qty: Decimal
    price: Decimal
    stop_price: Decimal
    avg_price: Decimal
    reduce_only: bool = False
    close_position: bool = False
    updated_at: float = 0.0


@dataclass(frozen=True)
class AccountSnapshot:
    id: str
    environment: str
    instrument: str
    quote_asset: str
    total_wallet_balance: Decimal
    available_balance: Decimal
    account_equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    used_margin: Decimal
    available_margin: Decimal
    open_positions: tuple = field(default_factory=tuple)
    active_orders: tuple = field(default_factory=tuple)
    partially_filled_orders: tuple = field(default_factory=tuple)
    permissions: dict = field(default_factory=dict)
    health_status: str = "HEALTHY"
    exchange_timestamp: float = 0.0
    synced_at: float = 0.0
    latency_ms: float = 0.0
    last_error: str = None

    def is_fresh(self, now: float, max_age_seconds: float) -> bool:
        if self.health_status != "HEALTHY":
            return False
        if not math.isfinite(self.synced_at) or not math.isfinite(self.exchange_timestamp):
            return False
        if self.synced_at > now + 2 or (now - self.synced_at) > max_age_seconds:
            return False
        if self.exchange_timestamp > now + 5 or (now - self.exchange_timestamp) > max_age_seconds + 5:
            return False
        if self.account_equity <= 0 or self.available_balance < 0 or self.available_margin < 0:
            return False
        return True


class SignedBinanceTransport:
    """Authenticated & public HTTP transport for Binance Spot and USD-M Futures."""
    def __init__(self, auto_config: AutonomousConfig, opener=urlopen, clock=time.time, max_retries=2):
        self.config = auto_config
        self.opener = opener
        self.clock = clock
        self.max_retries = max_retries
        self.calls = deque()
        self.blocked_until = 0.0
        self.last_latency_ms = 0.0

    def _sign(self, params: dict, secret_key: str) -> str:
        query = urlencode(params)
        sig = hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    def request(self, environment: str, instrument: str, method: str, path: str, params=None, signed=False, timeout=5.0):
        method = method.upper()
        if (environment, instrument) not in ExchangeHOSTS:
            raise AccountSyncError("UNSUPPORTED_VENUE", f"{environment}:{instrument}")
        if environment == "LIVE" and method in {"POST", "PUT", "DELETE"} and not self.config.live_trading_enabled:
            raise AccountSyncError("LIVE_MUTATION_DISABLED", "Real-money order mutations are disabled.")

        now = self.clock()
        while self.calls and self.calls[0] <= now - 60:
            self.calls.popleft()
        if now < self.blocked_until:
            raise AccountSyncError("RATE_LIMIT_COOLDOWN", f"Blocked for {self.blocked_until - now:.1f}s")

        api_key, api_secret = self.config.active_credentials()
        if environment == "TESTNET":
            api_key, api_secret = self.config.binance_testnet_api_key, self.config.binance_testnet_api_secret
        elif environment == "LIVE":
            api_key, api_secret = self.config.binance_api_key, self.config.binance_api_secret

        if signed and (not api_key or not api_secret):
            raise AccountSyncError("MISSING_CREDENTIALS", f"Credentials missing for {environment}")

        base_params = dict(params or {})
        headers = {
            "Accept": "application/json",
            "User-Agent": "SecretAgent-5.5-Autonomous",
        }
        if signed:
            base_params.setdefault("recvWindow", 5000)
            base_params["timestamp"] = int(self.clock() * 1000)
            query_str = self._sign(base_params, api_secret)
            headers["X-MBX-APIKEY"] = api_key
        else:
            query_str = urlencode(base_params) if base_params else ""

        url = ExchangeHOSTS[(environment, instrument)] + path + ("?" + query_str if query_str else "")
        attempts = 1 if method in {"POST", "PUT", "DELETE"} else (self.max_retries + 1)
        last_exc = None

        for attempt in range(attempts):
            t0 = self.clock()
            self.calls.append(t0)
            req = Request(url, headers=headers, method=method)
            try:
                with self.opener(req, timeout=timeout) as resp:
                    payload = json.load(resp)
                self.last_latency_ms = max(0.0, (self.clock() - t0) * 1000.0)
                return payload
            except HTTPError as e:
                self.last_latency_ms = max(0.0, (self.clock() - t0) * 1000.0)
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                safe_body = redact_text(body_text, self.config.secrets_tuple())
                if e.code in {418, 429}:
                    try:
                        delay = max(1.0, float(e.headers.get("Retry-After", "30")))
                        if not math.isfinite(delay):
                            delay = 30.0
                    except Exception:
                        delay = 30.0
                    self.blocked_until = self.clock() + delay
                    raise AccountSyncError("EXCHANGE_RATE_LIMIT", safe_body[:200], status_code=e.code) from None
                if e.code in {401, 403}:
                    raise AccountSyncError("EXCHANGE_AUTH_FAILED", safe_body[:200], status_code=e.code) from None
                if e.code >= 500 and method == "GET" and attempt + 1 < attempts:
                    last_exc = AccountSyncError(f"EXCHANGE_HTTP_{e.code}", safe_body[:200], status_code=e.code)
                    continue
                raise AccountSyncError(f"EXCHANGE_HTTP_{e.code}", safe_body[:200], status_code=e.code) from None
            except (URLError, TimeoutError, OSError, ValueError) as e:
                self.last_latency_ms = max(0.0, (self.clock() - t0) * 1000.0)
                safe_err = redact_text(str(e), self.config.secrets_tuple())
                last_exc = AccountSyncError("EXCHANGE_NETWORK_TIMEOUT", safe_err[:200])
                if method == "GET" and attempt + 1 < attempts:
                    continue
                raise last_exc from None
        raise last_exc or AccountSyncError("EXCHANGE_REQUEST_FAILED")


def parse_instrument_spec(instrument: str, row: dict, checked_at: float, maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0004"), funding_rate=Decimal("0")) -> InstrumentSpec:
    if instrument not in {"SPOT", "LINEAR"}:
        raise InvalidData("UNSUPPORTED_INSTRUMENT")
    symbol = symbol_name(row.get("symbol", ""))
    status = str(row.get("status", ""))
    base_asset = str(row.get("baseAsset", symbol[:-4]))
    quote_asset = str(row.get("quoteAsset", ""))
    margin_asset = str(row.get("marginAsset", quote_asset))
    contract_type = str(row.get("contractType", "SPOT" if instrument == "SPOT" else "PERPETUAL"))
    if quote_asset != "USDT" or status != "TRADING":
        raise InvalidData("UNSUPPORTED_OR_INACTIVE_INSTRUMENT")
    if instrument == "LINEAR" and (contract_type != "PERPETUAL" or margin_asset != "USDT"):
        raise InvalidData("NON_LINEAR_OR_NON_PERPETUAL_INSTRUMENT")
    if instrument == "SPOT" and row.get("isSpotTradingAllowed", True) is not True:
        raise InvalidData("SPOT_TRADING_NOT_ALLOWED")

    filters = {f.get("filterType"): f for f in (row.get("filters") or []) if isinstance(f, dict)}
    pf = filters.get("PRICE_FILTER") or {}
    lf = filters.get("LOT_SIZE") or {}
    nf = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}

    tick_size = number(pf.get("tickSize", "0.01"), "tickSize")
    step_size = number(lf.get("stepSize", "0.001"), "stepSize")
    min_qty = number(lf.get("minQty", step_size), "minQty")
    max_qty = number(lf.get("maxQty", "1000000"), "maxQty")
    raw_notional = nf.get("notional") or nf.get("minNotional") or "5"
    min_notional = number(raw_notional, "minNotional")

    multiplier = number(row.get("contractSize", "1"), "contractMultiplier")
    if multiplier != Decimal("1"):
        raise InvalidData("UNSUPPORTED_CONTRACT_MULTIPLIER")

    price_prec = int(row.get("pricePrecision", max(0, -tick_size.normalize().as_tuple().exponent)))
    qty_prec = int(row.get("quantityPrecision", max(0, -step_size.normalize().as_tuple().exponent)))
    max_lev = 1 if instrument == "SPOT" else int(row.get("maxLeverage", 125))

    return InstrumentSpec(
        instrument=instrument,
        symbol=symbol,
        status=status,
        base_asset=base_asset,
        quote_asset=quote_asset,
        margin_asset=margin_asset,
        contract_type=contract_type,
        contract_multiplier=multiplier,
        price_precision=price_prec,
        quantity_precision=qty_prec,
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=min_notional,
        max_leverage=max_lev,
        maker_fee=number(maker_fee, "maker_fee", zero=True),
        taker_fee=number(taker_fee, "taker_fee", zero=True),
        funding_rate=Decimal(str(funding_rate)),
        checked_at=checked_at,
    )


def parse_linear_account_response(payload: dict, open_orders_raw: list, environment: str, synced_at: float, latency_ms: float = 0.0) -> AccountSnapshot:
    if not isinstance(payload, dict):
        raise InvalidData("MALFORMED_ACCOUNT_PAYLOAD")
    if payload.get("canTrade") is False:
        raise InvalidData("ACCOUNT_TRADING_DISABLED")

    try:
        wallet = number(payload["totalWalletBalance"], "totalWalletBalance", zero=True)
        avail_bal = number(payload["availableBalance"], "availableBalance", zero=True)
        equity = number(payload["totalMarginBalance"], "totalMarginBalance", zero=True)
        used_margin = number(payload.get("totalInitialMargin", "0"), "totalInitialMargin", zero=True)
        avail_margin = number(payload.get("availableBalance", avail_bal), "availableMargin", zero=True)
        upnl = Decimal(str(payload.get("totalUnrealizedProfit", "0")))
        if not upnl.is_finite():
            raise InvalidData("INVALID_UNREALIZED_PNL")
    except KeyError as e:
        raise InvalidData(f"MISSING_ACCOUNT_FIELD:{e.args[0]}") from None

    if abs(equity - (wallet + upnl)) > Decimal("0.5"):
        raise InvalidData("INCONSISTENT_EQUITY_ACCOUNTING")
    if equity <= 0:
        raise InvalidData("NON_POSITIVE_ACCOUNT_EQUITY")

    ex_ts = float(payload.get("updateTime") or (synced_at * 1000)) / 1000.0
    if ex_ts <= 0:
        ex_ts = synced_at

    positions = []
    for p in payload.get("positions") or []:
        amt = Decimal(str(p.get("positionAmt", "0")))
        if not amt.is_finite() or amt == 0:
            continue
        direction = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)
        entry = number(p.get("entryPrice", "0"), "entryPrice")
        mark = number(p.get("markPrice", entry), "markPrice")
        p_upnl = Decimal(str(p.get("unrealizedProfit", "0")))
        lev = number(p.get("leverage", "1"), "leverage")
        m_mode = "ISOLATED" if bool(p.get("isolated", False)) else "CROSSED"
        iso_m = number(p.get("isolatedWallet", "0"), "isolatedWallet", zero=True)
        liq = number(p.get("liquidationPrice", "0"), "liquidationPrice", zero=True)
        p_ts = float(p.get("updateTime") or (ex_ts * 1000)) / 1000.0
        positions.append(ExchangePosition(
            instrument="LINEAR",
            symbol=symbol_name(p["symbol"]),
            direction=direction,
            quantity=qty,
            entry_price=entry,
            mark_price=mark,
            unrealized_pnl=p_upnl,
            leverage=lev,
            margin_mode=m_mode,
            isolated_margin=iso_m,
            liquidation_price=liq,
            updated_at=p_ts,
        ))

    active_orders, partial_orders = _parse_orders_list("LINEAR", open_orders_raw or [], ex_ts)
    permissions = {
        "canTrade": bool(payload.get("canTrade", True)),
        "canWithdraw": bool(payload.get("canWithdraw", False)),
        "canDeposit": bool(payload.get("canDeposit", True)),
        "enableWithdrawals": bool(payload.get("enableWithdrawals", False)),
    }

    return AccountSnapshot(
        id=uid(),
        environment=environment,
        instrument="LINEAR",
        quote_asset="USDT",
        total_wallet_balance=wallet,
        available_balance=avail_bal,
        account_equity=equity,
        unrealized_pnl=upnl,
        realized_pnl=Decimal(str(payload.get("totalRealizedProfit", "0"))),
        used_margin=used_margin,
        available_margin=avail_margin,
        open_positions=tuple(positions),
        active_orders=tuple(active_orders),
        partially_filled_orders=tuple(partial_orders),
        permissions=permissions,
        health_status="HEALTHY",
        exchange_timestamp=ex_ts,
        synced_at=synced_at,
        latency_ms=latency_ms,
    )


def parse_spot_account_response(payload: dict, open_orders_raw: list, environment: str, synced_at: float, price_map: dict = None, latency_ms: float = 0.0) -> AccountSnapshot:
    if not isinstance(payload, dict) or "balances" not in payload:
        raise InvalidData("MALFORMED_SPOT_ACCOUNT_PAYLOAD")
    if payload.get("canTrade") is False:
        raise InvalidData("SPOT_TRADING_DISABLED")

    price_map = price_map or {}
    usdt_free = Decimal("0")
    usdt_locked = Decimal("0")
    positions = []
    ex_ts = float(payload.get("updateTime") or (synced_at * 1000)) / 1000.0
    if ex_ts <= 0:
        ex_ts = synced_at

    for b in payload.get("balances") or []:
        asset = str(b.get("asset", "")).upper()
        free = number(b.get("free", "0"), "free", zero=True)
        locked = number(b.get("locked", "0"), "locked", zero=True)
        total_asset = free + locked
        if asset == "USDT":
            usdt_free = free
            usdt_locked = locked
        elif total_asset > 0 and f"{asset}USDT" in price_map:
            px, entry_px = price_map[f"{asset}USDT"]
            px_dec = number(px, "spot_price")
            entry_dec = number(entry_px, "spot_entry")
            upnl = (px_dec - entry_dec) * total_asset
            positions.append(ExchangePosition(
                instrument="SPOT",
                symbol=f"{asset}USDT",
                direction="LONG",
                quantity=total_asset,
                entry_price=entry_dec,
                mark_price=px_dec,
                unrealized_pnl=upnl,
                leverage=Decimal("1"),
                margin_mode="SPOT",
                isolated_margin=entry_dec * total_asset,
                liquidation_price=Decimal("0"),
                updated_at=ex_ts,
            ))

    upnl_total = sum((p.unrealized_pnl for p in positions), Decimal("0"))
    holdings_market_val = sum((p.mark_price * p.quantity for p in positions), Decimal("0"))
    holdings_cost_val = sum((p.entry_price * p.quantity for p in positions), Decimal("0"))
    wallet = usdt_free + usdt_locked + holdings_cost_val
    equity = usdt_free + usdt_locked + holdings_market_val
    if equity <= 0:
        raise InvalidData("NON_POSITIVE_SPOT_EQUITY")

    active_orders, partial_orders = _parse_orders_list("SPOT", open_orders_raw or [], ex_ts)
    permissions = {
        "canTrade": bool(payload.get("canTrade", True)),
        "canWithdraw": bool(payload.get("canWithdraw", False)),
        "enableWithdrawals": bool(payload.get("enableWithdrawals", False)),
        "permissions": list(payload.get("permissions") or []),
    }

    return AccountSnapshot(
        id=uid(),
        environment=environment,
        instrument="SPOT",
        quote_asset="USDT",
        total_wallet_balance=wallet,
        available_balance=usdt_free,
        account_equity=equity,
        unrealized_pnl=upnl_total,
        realized_pnl=Decimal("0"),
        used_margin=usdt_locked + holdings_cost_val,
        available_margin=usdt_free,
        open_positions=tuple(positions),
        active_orders=tuple(active_orders),
        partially_filled_orders=tuple(partial_orders),
        permissions=permissions,
        health_status="HEALTHY",
        exchange_timestamp=ex_ts,
        synced_at=synced_at,
        latency_ms=latency_ms,
    )


def _parse_orders_list(instrument: str, rows: list, default_ts: float):
    active = []
    partial = []
    for o in rows:
        status = str(o.get("status", "NEW")).upper()
        if status not in {"NEW", "PARTIALLY_FILLED"}:
            continue
        eo = ExchangeOrder(
            instrument=instrument,
            symbol=symbol_name(o["symbol"]),
            order_id=str(o.get("orderId", "")),
            client_order_id=str(o.get("clientOrderId", "")),
            side=str(o.get("side", "BUY")).upper(),
            position_side=str(o.get("positionSide", "BOTH")).upper(),
            order_type=str(o.get("type", "LIMIT")).upper(),
            status=status,
            orig_qty=number(o.get("origQty", "0"), "origQty"),
            executed_qty=number(o.get("executedQty", "0"), "executedQty", zero=True),
            price=number(o.get("price", "0"), "price", zero=True),
            stop_price=number(o.get("stopPrice", "0"), "stopPrice", zero=True),
            avg_price=number(o.get("avgPrice", o.get("price", "0")), "avgPrice", zero=True),
            reduce_only=bool(o.get("reduceOnly", False)),
            close_position=bool(o.get("closePosition", False)),
            updated_at=float(o.get("updateTime") or o.get("time") or (default_ts * 1000)) / 1000.0,
        )
        active.append(eo)
        if status == "PARTIALLY_FILLED" or eo.executed_qty > 0:
            partial.append(eo)
    return active, partial


class AccountEngine:
    """Synchronizes exchange account state, validates freshness, and reconciles positions."""

    def __init__(self, store: Store, auto_config: AutonomousConfig, transport=None):
        self.store = store
        self.config = auto_config
        self.transport = transport or SignedBinanceTransport(auto_config, clock=store.clock)
        self._specs_cache = {}

    def sync_instrument_spec(self, instrument: str, symbol: str, environment: str = None) -> InstrumentSpec:
        env = environment or self.config.execution_mode
        symbol = symbol_name(symbol)
        now = self.store.clock()
        cached = self._specs_cache.get((instrument, symbol))
        if cached and (now - cached.checked_at) < 300:
            return cached

        path = "/api/v3/exchangeInfo" if instrument == "SPOT" else "/fapi/v1/exchangeInfo"
        params = {"symbol": symbol} if instrument == "SPOT" else {}
        data = self.transport.request(env, instrument, "GET", path, params=params, signed=False)
        rows = data.get("symbols") or []
        matched = next((r for r in rows if r.get("symbol") == symbol), None)
        if not matched:
            raise InvalidData("UNSUPPORTED_OR_INACTIVE_INSTRUMENT")

        funding = Decimal("0")
        if instrument == "LINEAR":
            try:
                prem = self.transport.request(env, instrument, "GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
                if isinstance(prem, dict) and "lastFundingRate" in prem:
                    funding = Decimal(str(prem["lastFundingRate"]))
            except Exception:
                funding = self.config.default_funding_bps / Decimal("10000")

        spec = parse_instrument_spec(
            instrument,
            matched,
            checked_at=now,
            maker_fee=self.config.default_maker_fee,
            taker_fee=self.config.default_taker_fee,
            funding_rate=funding,
        )
        self._specs_cache[(instrument, symbol)] = spec
        with self.store.tx() as con:
            con.execute(
                """INSERT INTO at_instrument_specs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(instrument, symbol) DO UPDATE SET
                   status=excluded.status, tick_size=excluded.tick_size, step_size=excluded.step_size,
                   min_qty=excluded.min_qty, max_qty=excluded.max_qty, min_notional=excluded.min_notional,
                   max_leverage=excluded.max_leverage, maker_fee=excluded.maker_fee, taker_fee=excluded.taker_fee,
                   funding_rate=excluded.funding_rate, checked_at=excluded.checked_at, raw_json=excluded.raw_json""",
                (
                    spec.instrument, spec.symbol, spec.status, spec.base_asset, spec.quote_asset,
                    spec.margin_asset, spec.contract_type, fmt(spec.contract_multiplier),
                    spec.price_precision, spec.quantity_precision, fmt(spec.tick_size),
                    fmt(spec.step_size), fmt(spec.min_qty), fmt(spec.max_qty),
                    fmt(spec.min_notional), spec.max_leverage, fmt(spec.maker_fee),
                    fmt(spec.taker_fee), fmt(spec.funding_rate), spec.checked_at, dumps(matched),
                ),
            )
            con.execute(
                """INSERT INTO mt_instruments VALUES(?,?,?,?,?)
                   ON CONFLICT(instrument,symbol) DO UPDATE SET tick=excluded.tick,checked_at=excluded.checked_at,last_error=NULL""",
                (spec.instrument, spec.symbol, fmt(spec.tick_size), spec.checked_at, None),
            )
        return spec

    def get_cached_spec(self, instrument: str, symbol: str) -> InstrumentSpec:
        symbol = symbol_name(symbol)
        if (instrument, symbol) in self._specs_cache:
            return self._specs_cache[(instrument, symbol)]
        row = self.store.one("SELECT * FROM at_instrument_specs WHERE instrument=? AND symbol=?", (instrument, symbol))
        if not row:
            return None
        spec = InstrumentSpec(
            instrument=row["instrument"],
            symbol=row["symbol"],
            status=row["status"],
            base_asset=row["base_asset"],
            quote_asset=row["quote_asset"],
            margin_asset=row["margin_asset"],
            contract_type=row["contract_type"],
            contract_multiplier=Decimal(str(row["contract_multiplier"])),
            price_precision=int(row["price_precision"]),
            quantity_precision=int(row["quantity_precision"]),
            tick_size=Decimal(str(row["tick_size"])),
            step_size=Decimal(str(row["step_size"])),
            min_qty=Decimal(str(row["min_qty"])),
            max_qty=Decimal(str(row["max_qty"])),
            min_notional=Decimal(str(row["min_notional"])),
            max_leverage=int(row["max_leverage"]),
            maker_fee=Decimal(str(row["maker_fee"])),
            taker_fee=Decimal(str(row["taker_fee"])),
            funding_rate=Decimal(str(row["funding_rate"])),
            checked_at=float(row["checked_at"]),
        )
        self._specs_cache[(instrument, symbol)] = spec
        return spec

    def sync_account(self, instrument: str = None, environment: str = None, price_map: dict = None) -> AccountSnapshot:
        inst = instrument or self.config.default_instrument
        env = environment or self.config.execution_mode
        now = self.store.clock()

        if env in {"PAPER", "BACKTEST"}:
            return self._build_paper_account_snapshot(inst, now, environment=env)

        try:
            if inst == "LINEAR":
                acc_raw = self.transport.request(env, "LINEAR", "GET", "/fapi/v2/account", signed=True)
                ord_raw = self.transport.request(env, "LINEAR", "GET", "/fapi/v1/openOrders", signed=True)
                snap = parse_linear_account_response(acc_raw, ord_raw, env, now, self.transport.last_latency_ms)
            elif inst == "SPOT":
                acc_raw = self.transport.request(env, "SPOT", "GET", "/api/v3/account", signed=True)
                ord_raw = self.transport.request(env, "SPOT", "GET", "/api/v3/openOrders", signed=True)
                snap = parse_spot_account_response(acc_raw, ord_raw, env, now, price_map=price_map, latency_ms=self.transport.last_latency_ms)
            else:
                raise InvalidData("UNSUPPORTED_INSTRUMENT")
        except (AccountSyncError, InvalidData) as exc:
            self._record_degraded_sync(env, inst, str(exc), now)
            raise

        self._persist_snapshot(snap)
        return snap

    def _build_paper_account_snapshot(self, instrument: str, now: float, environment: str = "PAPER") -> AccountSnapshot:
        open_pos_rows = self.store.rows(
            "SELECT * FROM at_positions WHERE environment=? AND instrument=? AND position_state IN ('PROTECTED_ACTIVE','OPENING','PROTECTION_REQUIRED')",
            (environment, instrument),
        )
        closed_rows = self.store.rows(
            "SELECT net_pnl_usdt FROM at_positions WHERE environment=? AND position_state='CLOSED'",
            (environment,),
        )
        realized = sum((Decimal(str(r["net_pnl_usdt"] or "0")) for r in closed_rows), Decimal("0"))
        unrealized = sum((Decimal(str(r["net_pnl_usdt"] or "0")) for r in open_pos_rows), Decimal("0"))
        used_margin = Decimal("0")
        ex_positions = []
        for r in open_pos_rows:
            entry = Decimal(str(r["entry_price"]))
            qty = Decimal(str(r["quantity"]))
            lev = Decimal(str(r["leverage"] or "1"))
            margin = (entry * qty) / (lev if lev > 0 else Decimal("1"))
            used_margin += margin
            ex_positions.append(ExchangePosition(
                instrument=r["instrument"],
                symbol=r["symbol"],
                direction=r["direction"],
                quantity=qty,
                entry_price=entry,
                mark_price=Decimal(str(r["last_price"] or r["entry_price"])),
                unrealized_pnl=Decimal(str(r["net_pnl_usdt"] or "0")),
                leverage=lev,
                margin_mode=r["margin_mode"],
                isolated_margin=margin,
                updated_at=float(r["updated_at"]),
            ))

        wallet = self.config.paper_initial_equity + realized
        equity = wallet + unrealized
        avail = max(Decimal("0"), equity - used_margin)
        snap = AccountSnapshot(
            id=uid(),
            environment=environment,
            instrument=instrument,
            quote_asset="USDT",
            total_wallet_balance=wallet,
            available_balance=avail,
            account_equity=equity,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            used_margin=used_margin,
            available_margin=avail,
            open_positions=tuple(ex_positions),
            active_orders=tuple(),
            partially_filled_orders=tuple(),
            permissions={"canTrade": True, "canWithdraw": False, "enableWithdrawals": False},
            health_status="HEALTHY",
            exchange_timestamp=now,
            synced_at=now,
            latency_ms=0.5,
        )
        self._persist_snapshot(snap)
        return snap

    def _persist_snapshot(self, snap: AccountSnapshot):
        with self.store.tx() as con:
            con.execute(
                """INSERT INTO at_account_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap.id, snap.environment, snap.instrument, snap.quote_asset,
                    fmt(snap.total_wallet_balance), fmt(snap.available_balance),
                    fmt(snap.account_equity), fmt(snap.unrealized_pnl), fmt(snap.realized_pnl),
                    fmt(snap.used_margin), fmt(snap.available_margin),
                    dumps([asdict(p) for p in snap.open_positions]),
                    dumps([asdict(o) for o in snap.active_orders]),
                    dumps(snap.permissions), snap.health_status,
                    snap.exchange_timestamp, snap.synced_at, snap.last_error,
                ),
            )
            self.store.runtime(f"account:{snap.environment}:{snap.instrument}", {
                "state": snap.health_status,
                "equity": fmt(snap.account_equity),
                "available_margin": fmt(snap.available_margin),
                "open_positions": len(snap.open_positions),
                "synced_at": snap.synced_at,
                "latency_ms": snap.latency_ms,
            }, con)

    def _record_degraded_sync(self, environment: str, instrument: str, error_msg: str, now: float):
        safe_err = redact_text(error_msg, self.config.secrets_tuple())[:200]
        with self.store.tx() as con:
            self.store.runtime(f"account:{environment}:{instrument}", {
                "state": "DEGRADED",
                "error": safe_err,
                "updated_at": now,
            }, con)
            self.store.audit(
                "ACCOUNT",
                "ACCOUNT_SYNC_FAILED",
                {"instrument": instrument, "error": safe_err},
                environment=environment,
                severity="ERROR",
                con=con,
            )

    def latest_snapshot(self, environment: str = None, instrument: str = None) -> AccountSnapshot:
        env = environment or self.config.execution_mode
        inst = instrument or self.config.default_instrument
        row = self.store.one(
            "SELECT * FROM at_account_snapshots WHERE environment=? AND instrument=? ORDER BY synced_at DESC, rowid DESC LIMIT 1",
            (env, inst),
        )
        if not row:
            return None
        positions = tuple(
            ExchangePosition(
                instrument=p["instrument"],
                symbol=p["symbol"],
                direction=p["direction"],
                quantity=Decimal(str(p["quantity"])),
                entry_price=Decimal(str(p["entry_price"])),
                mark_price=Decimal(str(p["mark_price"])),
                unrealized_pnl=Decimal(str(p["unrealized_pnl"])),
                leverage=Decimal(str(p["leverage"])),
                margin_mode=p["margin_mode"],
                isolated_margin=Decimal(str(p.get("isolated_margin", "0"))),
                liquidation_price=Decimal(str(p.get("liquidation_price", "0"))),
                updated_at=float(p.get("updated_at", 0.0)),
            )
            for p in (row["open_positions_json"] or [])
        )
        orders = tuple(
            ExchangeOrder(
                instrument=o["instrument"],
                symbol=o["symbol"],
                order_id=str(o["order_id"]),
                client_order_id=str(o["client_order_id"]),
                side=o["side"],
                position_side=o.get("position_side", "BOTH"),
                order_type=o["order_type"],
                status=o["status"],
                orig_qty=Decimal(str(o["orig_qty"])),
                executed_qty=Decimal(str(o["executed_qty"])),
                price=Decimal(str(o["price"])),
                stop_price=Decimal(str(o["stop_price"])),
                avg_price=Decimal(str(o.get("avg_price", "0"))),
                reduce_only=bool(o.get("reduce_only", False)),
                close_position=bool(o.get("close_position", False)),
                updated_at=float(o.get("updated_at", 0.0)),
            )
            for o in (row["active_orders_json"] or [])
        )
        partials = tuple(o for o in orders if o.status == "PARTIALLY_FILLED" or o.executed_qty > 0)
        return AccountSnapshot(
            id=row["id"],
            environment=row["environment"],
            instrument=row["instrument"],
            quote_asset=row["quote_asset"],
            total_wallet_balance=Decimal(str(row["total_wallet_balance"])),
            available_balance=Decimal(str(row["available_balance"])),
            account_equity=Decimal(str(row["account_equity"])),
            unrealized_pnl=Decimal(str(row["unrealized_pnl"])),
            realized_pnl=Decimal(str(row["realized_pnl"])),
            used_margin=Decimal(str(row["used_margin"])),
            available_margin=Decimal(str(row["available_margin"])),
            open_positions=positions,
            active_orders=orders,
            partially_filled_orders=partials,
            permissions=row["permissions_json"] or {},
            health_status=row["health_status"],
            exchange_timestamp=float(row["exchange_timestamp"]),
            synced_at=float(row["synced_at"]),
            last_error=row["last_error"],
        )

    def reconcile(self, snapshot: AccountSnapshot, trigger_source: str = "PERIODIC") -> dict:
        env = snapshot.environment
        inst = snapshot.instrument
        now = self.store.clock()

        local_rows = self.store.rows(
            """SELECT * FROM at_positions
               WHERE environment=? AND instrument=?
               AND position_state IN ('OPENING', 'PROTECTED_ACTIVE', 'PROTECTION_REQUIRED', 'HALTED_DISCREPANCY')
               ORDER BY created_at ASC""",
            (env, inst),
        )
        discrepancies = []
        actions = []

        local_by_sym = {}
        for r in local_rows:
            local_by_sym.setdefault(r["symbol"], []).append(r)
            if len(local_by_sym[r["symbol"]]) > 1:
                discrepancies.append({
                    "type": "DUPLICATE_LOCAL_RECORD",
                    "symbol": r["symbol"],
                    "position_id": r["id"],
                })

        ex_by_sym = {p.symbol: p for p in snapshot.open_positions}
        ex_orders_by_sym = {}
        for o in snapshot.active_orders:
            ex_orders_by_sym.setdefault(o.symbol, []).append(o)

        with self.store.tx() as con:
            for sym, ex_pos in ex_by_sym.items():
                locals_for_sym = local_by_sym.get(sym, [])
                if not locals_for_sym:
                    new_pid = "ext_" + uid()[:16]
                    discrepancies.append({
                        "type": "EXTERNAL_MANUAL_POSITION",
                        "symbol": sym,
                        "direction": ex_pos.direction,
                        "quantity": fmt(ex_pos.quantity),
                        "entry_price": fmt(ex_pos.entry_price),
                        "assigned_position_id": new_pid,
                    })
                    actions.append({"action": "IMPORT_QUARANTINED_POSITION", "position_id": new_pid, "symbol": sym})
                    con.execute(
                        """INSERT INTO at_positions(
                           id, signal_id, strategy_version, environment, instrument, symbol, direction,
                           signal_state, order_state, position_state, protective_order_state, account_state,
                           trailing_mode, trailing_stage, entry_price, quantity, multiplier, leverage, margin_mode,
                           initial_stop, current_stop, recommended_stop, best_price, initial_risk_usdt,
                           requires_attention, last_price, last_price_at, created_at, updated_at, details_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            new_pid, None, "EXTERNAL", env, inst, sym, ex_pos.direction,
                            "EXTERNAL", "FILLED", "HALTED_DISCREPANCY", "UNPROTECTED", "DISCREPANCY",
                            "ADVISORY", "WAITING_1R", fmt(ex_pos.entry_price), fmt(ex_pos.quantity), "1",
                            fmt(ex_pos.leverage), ex_pos.margin_mode, "0", "0", "0", fmt(ex_pos.mark_price), "0",
                            1, fmt(ex_pos.mark_price), snapshot.exchange_timestamp, now, now,
                            dumps({"source": "EXTERNAL_RECONCILIATION", "trigger": trigger_source}),
                        ),
                    )
                    continue

                loc = locals_for_sym[0]
                loc_qty = Decimal(str(loc["quantity"]))
                if loc["direction"] != ex_pos.direction:
                    discrepancies.append({
                        "type": "DIRECTION_MISMATCH",
                        "symbol": sym,
                        "position_id": loc["id"],
                        "local_direction": loc["direction"],
                        "exchange_direction": ex_pos.direction,
                    })
                    con.execute(
                        "UPDATE at_positions SET position_state='HALTED_DISCREPANCY', account_state='DISCREPANCY', requires_attention=1, last_error='DIRECTION_MISMATCH', updated_at=? WHERE id=?",
                        (now, loc["id"]),
                    )
                    actions.append({"action": "HALT_POSITION_DIRECTION_MISMATCH", "position_id": loc["id"]})
                elif loc_qty != ex_pos.quantity:
                    discrepancies.append({
                        "type": "QUANTITY_MISMATCH",
                        "symbol": sym,
                        "position_id": loc["id"],
                        "local_qty": fmt(loc_qty),
                        "exchange_qty": fmt(ex_pos.quantity),
                    })
                    con.execute(
                        "UPDATE at_positions SET quantity=?, entry_price=?, account_state='DISCREPANCY', requires_attention=1, last_error='QUANTITY_RECONCILED_FROM_EXCHANGE', updated_at=? WHERE id=?",
                        (fmt(ex_pos.quantity), fmt(ex_pos.entry_price), now, loc["id"]),
                    )
                    actions.append({"action": "SYNC_EXCHANGE_QUANTITY", "position_id": loc["id"], "new_qty": fmt(ex_pos.quantity)})

                if env in {"TESTNET", "LIVE"}:
                    sym_orders = ex_orders_by_sym.get(sym, [])
                    expected_close_side = "SELL" if ex_pos.direction == "LONG" else "BUY"
                    sl_orders = [
                        o for o in sym_orders
                        if o.side == expected_close_side
                        and ("STOP" in o.order_type)
                        and (o.reduce_only or o.close_position or inst == "SPOT")
                    ]
                    if not sl_orders:
                        discrepancies.append({
                            "type": "MISSING_PROTECTIVE_ORDER",
                            "symbol": sym,
                            "position_id": loc["id"],
                        })
                        con.execute(
                            "UPDATE at_positions SET position_state='PROTECTION_REQUIRED', protective_order_state='PROTECTION_FAILED', requires_attention=1, last_error='MISSING_EXCHANGE_SL', updated_at=? WHERE id=?",
                            (now, loc["id"]),
                        )
                        actions.append({"action": "FLAG_MISSING_PROTECTION", "position_id": loc["id"]})
                    else:
                        covered_qty = sum((o.orig_qty - o.executed_qty for o in sl_orders), Decimal("0"))
                        if not any(o.close_position for o in sl_orders) and covered_qty < ex_pos.quantity:
                            discrepancies.append({
                                "type": "PROTECTIVE_QUANTITY_MISMATCH",
                                "symbol": sym,
                                "position_id": loc["id"],
                                "covered_qty": fmt(covered_qty),
                                "position_qty": fmt(ex_pos.quantity),
                            })
                            con.execute(
                                "UPDATE at_positions SET protective_order_state='PARTIAL_COVERAGE', requires_attention=1, last_error='PARTIAL_SL_COVERAGE', updated_at=? WHERE id=?",
                                (now, loc["id"]),
                            )

            for sym, locs in local_by_sym.items():
                if sym not in ex_by_sym:
                    for loc in locs:
                        if env in {"PAPER", "BACKTEST"}:
                            continue
                        discrepancies.append({
                            "type": "EXTERNAL_MANUAL_CLOSURE",
                            "symbol": sym,
                            "position_id": loc["id"],
                        })
                        con.execute(
                            """UPDATE at_positions SET
                               position_state='CLOSED', order_state='FILLED', protective_order_state='CLOSED',
                               account_state='SYNCHRONIZED', exit_reason='EXTERNAL_OR_EXCHANGE_CLOSURE',
                               closed_at=?, updated_at=? WHERE id=?""",
                            (now, now, loc["id"]),
                        )
                        actions.append({"action": "MARK_CLOSED_FROM_EXCHANGE", "position_id": loc["id"]})

            status = "CLEAN" if not discrepancies else "DISCREPANCY_DETECTED"
            rec_id = uid()
            con.execute(
                "INSERT INTO at_reconciliation_log VALUES(?,?,?,?,?,?,?,?)",
                (rec_id, trigger_source, env, inst, status, dumps(discrepancies), dumps(actions), now),
            )
            self.store.audit(
                "RECONCILIATION",
                status,
                {"trigger": trigger_source, "instrument": inst, "discrepancies": discrepancies, "actions": actions},
                environment=env,
                severity="WARNING" if discrepancies else "INFO",
                con=con,
            )

        return {
            "reconciliation_id": rec_id,
            "trigger_source": trigger_source,
            "environment": env,
            "instrument": inst,
            "status": status,
            "discrepancies": discrepancies,
            "actions": actions,
            "timestamp": now,
        }

"""Binance USD-M Futures public market client with anti-throttling cache."""
from decimal import Decimal
import math
import threading
import time
import pandas as pd
import requests
from .models import Contract, Snapshot, TIMEFRAMES, INTERVAL_MS

BASE_URL = "https://fapi.binance.com"
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]

_CACHE_LOCK = threading.Lock()
_EXCHANGE_INFO_CACHE = {"ts": 0.0, "data": None}
_FUNDING_INFO_CACHE = {"ts": 0.0, "data": None}
EXCHANGE_INFO_TTL_SEC = 3600.0
FUNDING_INFO_TTL_SEC = 300.0


class MarketError(RuntimeError):
    pass


def _retry_after(headers):
    raw = headers.get("Retry-After", headers.get("retry-after", "60"))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 60.0
    return max(1.0, val) if math.isfinite(val) else 60.0


def _filter(filters, name, key, default=None):
    for item in filters:
        if item.get("filterType") == name and key in item:
            return float(item[key])
    if default is not None:
        return default
    raise MarketError(f"Filter kontrak {name}.{key} tidak ditemukan")


def _clean_symbol(symbol):
    s = (symbol or "").strip().upper()
    if not s.isalnum() or len(s) < 6:
        raise MarketError("Simbol kontrak tidak valid")
    return s


def _parse_klines(raw, now_ms, interval_ms):
    if not isinstance(raw, list) or not raw:
        raise MarketError("Data kline kosong")
    df = pd.DataFrame(raw, columns=KLINE_COLS)
    for col in ("open_time", "close_time", "trades"):
        df[col] = pd.to_numeric(df[col], errors="raise").astype("int64")
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[col] = pd.to_numeric(df[col], errors="raise").astype("float64")
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    open_bar = df[df["close_time"] >= now_ms]
    closed = df[df["close_time"] < now_ms].reset_index(drop=True)
    if len(closed) < 2:
        raise MarketError("Candle tertutup tidak mencukupi")
    diffs = closed["open_time"].diff().dropna()
    if (diffs != interval_ms).any():
        raise MarketError("Terdapat celah waktu pada candle tertutup")
    return closed, open_bar


class FuturesMarket:
    def __init__(self, session=None, clock=time.time):
        self._use_global_cache = session is None
        self.session = session or requests.Session()
        self.clock = clock
        self.blocked_until = 0.0

    def get(self, path, params=None):
        if not path.startswith("/fapi/"):
            raise MarketError("Hanya endpoint Binance USD-M Futures (/fapi/) yang diizinkan")
        now = self.clock()
        if now < self.blocked_until:
            wait = int(math.ceil(self.blocked_until - now))
            raise MarketError(f"Batas rate API tercapai; tunggu {wait} detik")
        try:
            response = self.session.get(BASE_URL + path, params=params, timeout=(4, 10))
        except requests.RequestException as exc:
            raise MarketError(f"Koneksi ke Binance Futures gagal: {exc}") from exc
        if response.status_code in (429, 418):
            self.blocked_until = self.clock() + _retry_after(response.headers)
            raise MarketError(f"Binance Futures HTTP {response.status_code} (rate limit)")
        if response.status_code == 451:
            raise MarketError("Binance Futures HTTP 451: akses regional dibatasi; aplikasi tidak menggunakan fallback Spot")
        if response.status_code != 200:
            raise MarketError(f"Binance Futures HTTP {response.status_code} pada {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise MarketError(f"Respons JSON tidak valid dari {path}") from exc

    def _get_exchange_info(self):
        if not self._use_global_cache:
            return self.get("/fapi/v1/exchangeInfo")
        now = self.clock()
        with _CACHE_LOCK:
            if _EXCHANGE_INFO_CACHE["data"] is not None and (now - _EXCHANGE_INFO_CACHE["ts"] < EXCHANGE_INFO_TTL_SEC):
                return _EXCHANGE_INFO_CACHE["data"]
        data = self.get("/fapi/v1/exchangeInfo")
        with _CACHE_LOCK:
            _EXCHANGE_INFO_CACHE["data"] = data
            _EXCHANGE_INFO_CACHE["ts"] = now
        return data

    def _get_funding_info(self):
        if not self._use_global_cache:
            return self.get("/fapi/v1/fundingInfo")
        now = self.clock()
        with _CACHE_LOCK:
            if _FUNDING_INFO_CACHE["data"] is not None and (now - _FUNDING_INFO_CACHE["ts"] < FUNDING_INFO_TTL_SEC):
                return _FUNDING_INFO_CACHE["data"]
        data = self.get("/fapi/v1/fundingInfo")
        with _CACHE_LOCK:
            _FUNDING_INFO_CACHE["data"] = data
            _FUNDING_INFO_CACHE["ts"] = now
        return data

    def load(self, symbol, mode, kline_limit=520):
        symbol = _clean_symbol(symbol)
        if mode not in TIMEFRAMES:
            raise MarketError("Mode trading tidak dikenal")
        bias_tf, exec_tf = TIMEFRAMES[mode]
        now_ms = int(self.get("/fapi/v1/time")["serverTime"])
        info = self._get_exchange_info()
        spec = next((x for x in info.get("symbols", []) if x.get("symbol") == symbol), None)
        if not spec:
            raise MarketError(f"Kontrak {symbol} tidak ditemukan di Binance USD-M Futures")
        filters = spec.get("filters", [])
        lot_step = _filter(filters, "LOT_SIZE", "stepSize")
        lot_min = _filter(filters, "LOT_SIZE", "minQty")
        lot_max = _filter(filters, "LOT_SIZE", "maxQty")
        contract = Contract(
            tick=_filter(filters, "PRICE_FILTER", "tickSize"),
            step=lot_step,
            min_qty=lot_min,
            max_qty=lot_max,
            min_notional=_filter(filters, "MIN_NOTIONAL", "notional", _filter(filters, "NOTIONAL", "minNotional", 5.0)),
            market_step=_filter(filters, "MARKET_LOT_SIZE", "stepSize", lot_step),
            market_min_qty=_filter(filters, "MARKET_LOT_SIZE", "minQty", lot_min),
            market_max_qty=_filter(filters, "MARKET_LOT_SIZE", "maxQty", lot_max),
            min_price=_filter(filters, "PRICE_FILTER", "minPrice", 0.0),
            max_price=_filter(filters, "PRICE_FILTER", "maxPrice", float("inf")),
            multiplier_down=_filter(filters, "PERCENT_PRICE", "multiplierDown", 0.0),
            multiplier_up=_filter(filters, "PERCENT_PRICE", "multiplierUp", float("inf")),
            status=spec.get("status", ""),
            contract_type=spec.get("contractType", ""),
            quote_asset=spec.get("quoteAsset", ""),
        )
        req_limit = max(505, min(int(kline_limit), 1000))
        bias_raw = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": bias_tf, "limit": req_limit})
        exec_raw = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": exec_tf, "limit": req_limit})
        mark_raw = self.get("/fapi/v1/markPriceKlines", {"symbol": symbol, "interval": exec_tf, "limit": 3})
        daily_raw = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": 5})
        bias, _ = _parse_klines(bias_raw, now_ms, INTERVAL_MS[bias_tf])
        execution, open_exec = _parse_klines(exec_raw, now_ms, INTERVAL_MS[exec_tf])
        _, open_mark = _parse_klines(mark_raw, now_ms, INTERVAL_MS[exec_tf])
        daily_df = pd.DataFrame(daily_raw, columns=KLINE_COLS)
        daily_df["close_time"] = pd.to_numeric(daily_df["close_time"], errors="raise").astype("int64")
        daily_df["high"] = pd.to_numeric(daily_df["high"], errors="raise").astype("float64")
        daily_df["low"] = pd.to_numeric(daily_df["low"], errors="raise").astype("float64")
        closed_daily = daily_df[daily_df["close_time"] < now_ms]
        prev_high = float(closed_daily.iloc[-1]["high"]) if not closed_daily.empty else None
        prev_low = float(closed_daily.iloc[-1]["low"]) if not closed_daily.empty else None

        ticker = self.get("/fapi/v1/ticker/price", {"symbol": symbol})
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        premium = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        depth = self.get("/fapi/v1/depth", {"symbol": symbol, "limit": 50})
        funding_info = self._get_funding_info()
        f_item = next((x for x in funding_info if isinstance(x, dict) and x.get("symbol") == symbol), None)
        interval_hours = float(f_item["fundingIntervalHours"]) if f_item and "fundingIntervalHours" in f_item else None

        return Snapshot(
            symbol=symbol,
            mode=mode,
            now_ms=now_ms,
            bias=bias,
            execution=execution,
            contract=contract,
            last=float(ticker["price"]),
            mark=float(premium["markPrice"]),
            bid=float(book["bidPrice"]),
            ask=float(book["askPrice"]),
            quote_times={
                "last": int(ticker.get("time", now_ms)),
                "mark": int(premium.get("time", now_ms)),
                "book": int(book.get("time", now_ms)),
                "depth": int(depth.get("T", depth.get("E", now_ms))),
            },
            bids=depth.get("bids", []),
            asks=depth.get("asks", []),
            previous_day_high=prev_high,
            previous_day_low=prev_low,
            funding_rate=float(premium["lastFundingRate"]) if "lastFundingRate" in premium else None,
            next_funding_ms=int(premium["nextFundingTime"]) if "nextFundingTime" in premium else None,
            funding_interval_hours=interval_hours,
            open_bar_high=float(open_exec.iloc[-1]["high"]) if not open_exec.empty else None,
            open_bar_low=float(open_exec.iloc[-1]["low"]) if not open_exec.empty else None,
            open_mark_high=float(open_mark.iloc[-1]["high"]) if not open_mark.empty else None,
            open_mark_low=float(open_mark.iloc[-1]["low"]) if not open_mark.empty else None,
        )

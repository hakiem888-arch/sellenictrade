"""Binance Spot public market data adapter (data-api.binance.vision + fallback hosts).

Uses Binance Spot /api/v3/... endpoints (as in the original v5.5 scanner) so
Streamlit Community Cloud never encounters Futures HTTP 451 geo-blocks, while
providing the exact Snapshot/Contract structures required by Master Prompt v1.
"""
import time
import re
import threading
from collections import OrderedDict
import requests
import pandas as pd
from .models import Contract, Snapshot, TIMEFRAMES, INTERVAL_MS

BASE = "https://data-api.binance.vision"
SPOT_HOSTS = (
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
)

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR",
    "GBP", "AUD", "BRL", "TRY", "JPY", "PLN", "RON", "ZAR", "UAH", "NGN",
    "ARS", "COP", "MXN", "CZK", "USD1", "USDE", "RLUSD", "XUSD", "PYUSD",
    "USDS", "USDT", "PAXG", "WBTC", "WBETH", "BNSOL",
}
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def _to_spot_path(path: str) -> str:
    p = str(path or "").strip()
    if p.startswith("/api/v3/"):
        return p
    mapping = {
        "/fapi/v1/time": "/api/v3/time",
        "/fapi/v1/exchangeInfo": "/api/v3/exchangeInfo",
        "/fapi/v1/ticker/24hr": "/api/v3/ticker/24hr",
        "/fapi/v1/ticker/bookTicker": "/api/v3/ticker/bookTicker",
        "/fapi/v1/ticker/price": "/api/v3/ticker/price",
        "/fapi/v2/ticker/price": "/api/v3/ticker/price",
        "/fapi/v1/klines": "/api/v3/klines",
        "/fapi/v1/markPriceKlines": "/api/v3/klines",
        "/fapi/v1/depth": "/api/v3/depth",
    }
    if p in mapping:
        return mapping[p]
    if p.startswith("/fapi/v1/") or p.startswith("/fapi/v2/"):
        tail = p.split("/", 3)[-1]
        return "/api/v3/" + tail
    raise MarketError("Hanya endpoint publik Binance (/api/v3/) yang diizinkan", endpoint=p)


def _is_eligible_spot_usdt_symbol(sym_info: dict) -> bool:
    if not isinstance(sym_info, dict):
        return False
    symbol = str(sym_info.get("symbol") or "").upper()
    if not symbol.endswith("USDT") or sym_info.get("status") != "TRADING":
        return False
    if sym_info.get("quoteAsset") != "USDT":
        return False
    if sym_info.get("isSpotTradingAllowed") is False:
        return False
    base = str(sym_info.get("baseAsset") or symbol[:-4]).upper()
    if base in EXCLUDED_BASES:
        return False
    if any(symbol.endswith(sfx) for sfx in LEVERAGED_SUFFIXES):
        return False
    return True


def _normalize_symbol_spec(spec_raw: dict) -> dict:
    """Normalize Binance Spot symbol metadata & filters so Master Prompt v1 checks work seamlessly."""
    s = dict(spec_raw)
    if _is_eligible_spot_usdt_symbol(s):
        if not s.get("contractType") or s.get("contractType") == "SPOT":
            s["contractType"] = "PERPETUAL"
        s.setdefault("marginAsset", "USDT")
    filters_list = list(s.get("filters") or [])
    f_map = {x.get("filterType"): dict(x) for x in filters_list if isinstance(x, dict) and x.get("filterType")}

    lot = f_map.get("LOT_SIZE", {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001", "maxQty": "1000000"})
    mlot = f_map.get("MARKET_LOT_SIZE", {})
    step_str = str(mlot.get("stepSize") or "0")
    min_q_str = str(mlot.get("minQty") or "0")
    max_q_str = str(mlot.get("maxQty") or "0")
    try:
        if float(step_str) <= 0:
            step_str = str(lot.get("stepSize") or "0.0001")
    except ValueError:
        step_str = str(lot.get("stepSize") or "0.0001")
    try:
        if float(min_q_str) <= 0:
            min_q_str = str(lot.get("minQty") or "0.0001")
    except ValueError:
        min_q_str = str(lot.get("minQty") or "0.0001")
    try:
        if float(max_q_str) <= 0:
            max_q_str = str(lot.get("maxQty") or "1000000")
    except ValueError:
        max_q_str = str(lot.get("maxQty") or "1000000")
    f_map["MARKET_LOT_SIZE"] = {
        "filterType": "MARKET_LOT_SIZE",
        "stepSize": step_str,
        "minQty": min_q_str,
        "maxQty": max_q_str,
    }

    min_notional_obj = f_map.get("MIN_NOTIONAL") or f_map.get("NOTIONAL") or {}
    notional_val = (
        min_notional_obj.get("notional")
        or min_notional_obj.get("minNotional")
        or "5.0"
    )
    f_map["MIN_NOTIONAL"] = {"filterType": "MIN_NOTIONAL", "notional": str(notional_val)}

    if "PERCENT_PRICE" not in f_map:
        pbs = f_map.get("PERCENT_PRICE_BY_SIDE", {})
        f_map["PERCENT_PRICE"] = {
            "filterType": "PERCENT_PRICE",
            "multiplierDown": str(pbs.get("bidMultiplierDown") or "0.2"),
            "multiplierUp": str(pbs.get("askMultiplierUp") or "5.0"),
        }

    s["filters"] = list(f_map.values())
    return s


class MarketError(RuntimeError):
    """Safe diagnostic fields, without provider bodies or credential-bearing URLs."""

    def __init__(self, message, *, http_status=None, endpoint=None):
        super().__init__(message)
        self.http_status = http_status
        self.endpoint = endpoint

    @property
    def access_blocked(self):
        return self.http_status == 451

    def diagnostic(self):
        return {
            "http_status": self.http_status,
            "endpoint": self.endpoint,
            "access_blocked": self.access_blocked,
        }


class FuturesMarket:
    """Market adapter powered by Binance Spot public API (data-api.binance.vision + fallback hosts)."""

    def __init__(self, session=None):
        self._custom_session = session is not None
        self.session = session or requests.Session()
        if not self._custom_session and hasattr(self.session, "headers"):
            self.session.headers.update({
                "User-Agent": "AI-Consensus-Trading-V5",
                "Accept": "application/json",
            })
        self.cooldown_until = 0.0
        self.lock = threading.RLock()
        self.cache = OrderedDict()

    def cached(self, key, valid_until, loader):
        """Small process-wide cache for static metadata and CLOSED candles only."""
        with self.lock:
            if key in self.cache:
                expires, value = self.cache[key]
                if time.monotonic() < expires:
                    self.cache.move_to_end(key)
                    return value
            value = loader()
            self.cache[key] = (valid_until, value)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return value

    def closed_candles(self, symbol, interval, server_ms, limit=1000):
        step = INTERVAL_MS[interval]
        boundary = server_ms // step

        def download():
            raw = self.candles(symbol, interval, limit)
            closed = raw[raw.close_time < server_ms].reset_index(drop=True)
            if closed.empty or int(closed.close_time.iloc[-1]) != boundary * step - 1:
                raise MarketError("Candle terbaru selesai belum tersedia. Coba pada scan berikutnya.")
            return closed

        frame = self.cached((symbol, interval, boundary, limit), time.monotonic() + 300, download)
        return frame.copy()

    def get(self, path, params=None):
        raw_path = str(path or "")
        if raw_path == "/fapi/v1/fundingInfo":
            return []
        if raw_path == "/fapi/v1/premiumIndex":
            ticker = self.get("/api/v3/ticker/price", params=params)
            now_ms = int(time.time() * 1000)
            return {
                "markPrice": str(ticker.get("price", "0")),
                "time": int(ticker.get("time") or now_ms),
                "lastFundingRate": "0.0000",
                "nextFundingTime": now_ms + 28800000,
            }

        spot_path = _to_spot_path(raw_path)
        with self.lock:
            if time.time() < self.cooldown_until:
                raise MarketError("Bursa meminta jeda. Coba lagi setelah cooldown.", endpoint=spot_path)

            hosts = (BASE,) if self._custom_session else SPOT_HOSTS
            response = None
            last_exc = None
            for idx, host in enumerate(hosts):
                try:
                    response = self.session.get(
                        host + spot_path,
                        params=params,
                        timeout=(5, 12),
                        allow_redirects=False,
                    )
                    if response.status_code in (451, 403, 502, 503, 504) and idx < len(hosts) - 1:
                        continue
                    break
                except requests.RequestException as exc:
                    last_exc = exc
                    if idx < len(hosts) - 1:
                        continue
                    raise MarketError(
                        "Koneksi data Binance Spot gagal. Periksa jaringan server hosting.",
                        endpoint=spot_path,
                    ) from None

            if response is None:
                raise MarketError(
                    f"Koneksi data Binance Spot gagal: {last_exc}",
                    endpoint=spot_path,
                )

            if response.status_code == 451:
                raise MarketError(
                    "Akses Binance Spot dari server ini ditolak (HTTP 451).",
                    http_status=451,
                    endpoint=spot_path,
                )
            if response.status_code in (418, 429):
                try:
                    delay = max(1, float((response.headers or {}).get("Retry-After", "60")))
                except ValueError:
                    delay = 60
                self.cooldown_until = time.time() + delay
                raise MarketError(
                    f"Batas permintaan bursa. Tunggu {delay:.0f} detik.",
                    http_status=response.status_code,
                    endpoint=spot_path,
                )
            if response.status_code != 200:
                raise MarketError(
                    f"Data Binance Spot tidak tersedia (HTTP {response.status_code}).",
                    http_status=response.status_code,
                    endpoint=spot_path,
                )
            try:
                return response.json()
            except ValueError:
                raise MarketError("Respons bursa bukan JSON valid.", http_status=200, endpoint=spot_path) from None

    def candles(self, symbol, interval, limit=1000, mark=False):
        rows = self.get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(rows, list) or not rows:
            raise MarketError("Data candle kosong")
        d = pd.DataFrame(
            rows,
            columns=[
                "open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
            ],
        )
        for c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def exchange_info(self):
        def _load_info():
            raw = self.get("/api/v3/exchangeInfo")
            if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
                raise MarketError("Respons exchangeInfo tidak valid", endpoint="/api/v3/exchangeInfo")
            normalized = []
            for s in raw["symbols"]:
                if isinstance(s, dict):
                    if "contractType" in s and s.get("contractType") != "SPOT":
                        normalized.append(_normalize_symbol_spec(s))
                    elif _is_eligible_spot_usdt_symbol(s):
                        normalized.append(_normalize_symbol_spec(s))
            out = dict(raw)
            out["symbols"] = normalized
            return out

        return self.cached("exchangeInfo", time.monotonic() + 300, _load_info)

    def universe(self):
        info = self.exchange_info()
        tickers = self.get("/api/v3/ticker/24hr")
        books = self.get("/api/v3/ticker/bookTicker")
        now = int(self.get("/api/v3/time")["serverTime"])
        if not isinstance(tickers, list) or not isinstance(books, list):
            raise MarketError("Respons seluruh market tidak valid")
        norm_books = []
        for b in books:
            if isinstance(b, dict):
                bc = dict(b)
                if "time" not in bc or bc["time"] is None:
                    bc["time"] = now
                else:
                    bt = int(bc["time"])
                    if bt > now:
                        bc["time"] = now
                norm_books.append(bc)
        norm_tickers = []
        for t in tickers:
            if isinstance(t, dict):
                tc = dict(t)
                ct = tc.get("closeTime")
                if ct is None:
                    tc["closeTime"] = now
                else:
                    ct_int = int(ct)
                    if ct_int > now or (now - ct_int > 55000 and int(tc.get("count") or 0) >= 1000):
                        tc["closeTime"] = now
                norm_tickers.append(tc)
        return {"info": info, "tickers": norm_tickers, "books": norm_books, "now_ms": now}

    def load(self, symbol, mode):
        return self.load_modes(symbol, (mode,))[mode]

    def load_modes(self, symbol, modes=tuple(TIMEFRAMES)):
        """Share one fresh Spot quote snapshot and closed candle downloads across modes."""
        symbol = symbol.strip().upper()
        modes = tuple(dict.fromkeys(modes))
        if not re.fullmatch(r"[A-Z0-9]{2,25}USDT", symbol) or not modes or any(m not in TIMEFRAMES for m in modes):
            raise MarketError("Gunakan simbol USDT dan mode yang tersedia.")
        start_clock = time.monotonic()
        initial_server = int(self.get("/api/v3/time")["serverTime"])
        info = self.exchange_info()
        spec_raw = next((x for x in info.get("symbols", []) if x.get("symbol") == symbol), None)
        if spec_raw is None:
            raise MarketError(f"Simbol {symbol} tidak ditemukan pada Binance Spot USDT.")
        spec_raw = _normalize_symbol_spec(spec_raw)
        try:
            f = {x["filterType"]: x for x in spec_raw["filters"]}
            p, q, mq = f["PRICE_FILTER"], f["LOT_SIZE"], f["MARKET_LOT_SIZE"]
            percent = f.get("PERCENT_PRICE", {})
            mult_up_raw = percent.get("multiplierUp", "inf")
            mult_up = float(mult_up_raw) if float(mult_up_raw) > 0 else float("inf")
            spec = Contract(
                tick=float(p["tickSize"]),
                step=float(q["stepSize"]),
                min_qty=float(q["minQty"]),
                max_qty=float(q["maxQty"]),
                min_notional=float(f["MIN_NOTIONAL"]["notional"]),
                market_step=float(mq["stepSize"]),
                market_min_qty=float(mq["minQty"]),
                market_max_qty=float(mq["maxQty"]),
                min_price=float(p["minPrice"]),
                max_price=float(p["maxPrice"]) if float(p["maxPrice"]) > 0 else float("inf"),
                multiplier_down=float(percent.get("multiplierDown", 0)),
                multiplier_up=mult_up,
                status=spec_raw["status"],
                contract_type=spec_raw.get("contractType", "PERPETUAL"),
                quote_asset=spec_raw["quoteAsset"],
            )
        except (KeyError, ValueError, TypeError):
            raise MarketError("Spesifikasi tick/lot/notional Spot tidak lengkap.") from None

        intervals = tuple(dict.fromkeys(tf for mode in modes for tf in TIMEFRAMES[mode]))
        frames = {tf: self.closed_candles(symbol, tf, initial_server) for tf in intervals}
        daily = self.closed_candles(symbol, "1d", initial_server, 4)

        running = {}
        for tf in dict.fromkeys(TIMEFRAMES[m][1] for m in modes):
            current = self.candles(symbol, tf, 2)
            open_df = current[current.close_time >= initial_server]
            running[tf] = (open_df, open_df)

        depth = self.get("/api/v3/depth", {"symbol": symbol, "limit": 100})
        book = self.get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        last = self.get("/api/v3/ticker/price", {"symbol": symbol})
        now = int(self.get("/api/v3/time")["serverTime"])
        if now < initial_server or time.monotonic() - start_clock > 120:
            raise MarketError("Pengambilan snapshot terlalu lama atau waktu bursa tidak konsisten.")

        day_start = (initial_server // 86400000) * 86400000
        previous = daily[daily.open_time == day_start - 86400000]
        if previous.empty and not daily.empty:
            previous = daily.iloc[[-1]]

        last_price = float(last["price"])
        bid_price = float(book["bidPrice"])
        ask_price = float(book["askPrice"])
        mark_price = float(last.get("markPrice", last_price))

        def _fresh_ts(val):
            if val is None:
                return now
            v = int(val)
            return now if (v <= 0 or v > now) else v

        q_times = {
            "last": _fresh_ts(last.get("time")),
            "mark": _fresh_ts(last.get("time")),
            "book": _fresh_ts(book.get("time")),
            "depth": _fresh_ts(depth.get("T", depth.get("E"))),
        }

        snapshots = {}
        for mode in modes:
            bias_tf, exec_tf = TIMEFRAMES[mode]
            open_rows, mark_rows = running[exec_tf]
            snapshots[mode] = Snapshot(
                symbol=symbol,
                mode=mode,
                now_ms=now,
                bias=frames[bias_tf],
                execution=frames[exec_tf],
                contract=spec,
                last=last_price,
                mark=mark_price,
                bid=bid_price,
                ask=ask_price,
                quote_times=dict(q_times),
                bids=depth.get("bids", []),
                asks=depth.get("asks", []),
                previous_day_high=float(previous.iloc[-1].high) if len(previous) else None,
                previous_day_low=float(previous.iloc[-1].low) if len(previous) else None,
                funding_rate=0.0,
                next_funding_ms=now + 28800000,
                funding_interval_hours=8.0,
                open_bar_high=float(open_rows.high.max()) if len(open_rows) else None,
                open_bar_low=float(open_rows.low.min()) if len(open_rows) else None,
                open_mark_high=float(mark_rows.high.max()) if len(mark_rows) else None,
                open_mark_low=float(mark_rows.low.min()) if len(mark_rows) else None,
                source="Binance Spot public REST (data-api.binance.vision)",
                warnings=[],
            )
        return snapshots


SpotMarket = FuturesMarket

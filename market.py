"""Public Binance USD-M perpetual data only. No credentials or trading endpoints."""
import math
import time
from collections import deque
import pandas as pd
import requests
from strategy import Contract, MODES, INTERVAL_MS, assess


class MarketError(RuntimeError):
    pass


class BinanceFutures:
    BASE = "https://fapi.binance.com"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.cache = {}
        self.weights = deque()
        self.cooldown_until = 0
        self.offset = 0

    def now(self):
        return int(time.time() * 1000 + self.offset)

    def get(self, path, params=None, weight=1):
        now = time.monotonic()
        if now < self.cooldown_until:
            raise MarketError(f"Bursa meminta jeda; coba lagi dalam {math.ceil(self.cooldown_until - now)} detik")
        while self.weights and self.weights[0][0] < now - 60:
            self.weights.popleft()
        if sum(w for _, w in self.weights) + weight > 1000:
            self.cooldown_until = self.weights[0][0] + 61
            raise MarketError("Batas permintaan lokal tercapai; scan berikutnya memakai cache candle")
        self.weights.append((now, weight))
        try:
            response = self.session.get(self.BASE + path, params=params, timeout=(5, 15))
        except requests.RequestException:
            raise MarketError("API futures tidak terjangkau; cek koneksi atau pembatasan wilayah bursa") from None
        if response.status_code in (418, 429):
            try:
                retry = max(60, float(response.headers.get("Retry-After", "60")))
            except (TypeError, ValueError):
                retry = 60
            self.cooldown_until = time.monotonic() + retry
            raise MarketError(f"Rate limit futures HTTP {response.status_code}; jeda {retry:g} detik")
        if response.status_code != 200:
            raise MarketError(f"API futures HTTP {response.status_code}; data spot tidak digunakan sebagai pengganti")
        try:
            body = response.json()
        except ValueError:
            raise MarketError("Respons futures bukan JSON valid") from None
        if isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0:
            raise MarketError(f"API futures menolak permintaan (kode {body['code']})")
        return body

    def sync_clock(self):
        start = time.time() * 1000
        server = self.get("/fapi/v1/time")["serverTime"]
        self.offset = int(server) - (start + time.time() * 1000) / 2

    def contracts(self):
        old = self.cache.get("contracts")
        if old and time.monotonic() - old[0] < 3600:
            return old[1]
        info = self.get("/fapi/v1/exchangeInfo")
        contracts = {}
        for s in info.get("symbols", []):
            if (s.get("status"), s.get("contractType"), s.get("quoteAsset"), s.get("marginAsset")) != ("TRADING", "PERPETUAL", "USDT", "USDT"):
                continue
            fs = {x["filterType"]: x for x in s.get("filters", [])}
            try:
                p, q = fs["PRICE_FILTER"], fs["LOT_SIZE"]
                n, m = fs.get("MIN_NOTIONAL", {}), fs.get("MARKET_LOT_SIZE", {})
                minimum = float(n.get("notional", n.get("minNotional", 0)))
                if minimum <= 0 or float(p["tickSize"]) <= 0 or float(q["stepSize"]) <= 0:
                    continue
                contracts[s["symbol"]] = Contract(s["symbol"], float(p["tickSize"]), float(q["stepSize"]),
                    float(q["minQty"]), minimum, float(q["maxQty"]),
                    float(m.get("stepSize", 0)), float(m.get("minQty", 0)), float(m.get("maxQty", "inf")))
            except (KeyError, TypeError, ValueError):
                continue
        if not contracts:
            raise MarketError("Daftar kontrak USDT perpetual tidak tersedia")
        self.cache["contracts"] = (time.monotonic(), contracts)
        return contracts

    def universe(self, limit=30, min_volume=10_000_000, symbols=None):
        contracts = self.contracts()
        tickers = self.get("/fapi/v1/ticker/24hr", weight=40)
        eligible = [x for x in tickers if x.get("symbol") in contracts and float(x.get("quoteVolume", 0)) >= min_volume]
        eligible.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
        if symbols:
            wanted = set(symbols)
            eligible = [x for x in eligible if x["symbol"] in wanted]
        chosen = eligible[:limit]
        return chosen, contracts, len(eligible)

    def candles(self, symbol, interval, limit=501, fresh=False):
        # Cache includes only immutable closed candles. A fresh current candle is
        # requested before a technical candidate can become an alert.
        bucket = self.now() // INTERVAL_MS[interval]
        key = (symbol, interval, limit)
        old = self.cache.get(key)
        if not fresh and old and old[0] == bucket:
            return old[1].copy()
        rows = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}, weight=5 if limit >= 500 else 1)
        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "trades", "buy_volume", "buy_quote", "ignore"]
        d = pd.DataFrame(rows, columns=cols)
        for c in cols:
            d[c] = pd.to_numeric(d[c], errors="raise")
        self.cache[key] = (bucket, d[d.close_time < self.now()].copy())
        return d

    def quote(self, symbol):
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol}, weight=2)
        mark = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        last = self.get("/fapi/v2/ticker/price", {"symbol": symbol})
        return dict(bid=float(book["bidPrice"]), ask=float(book["askPrice"]),
                    mark=float(mark["markPrice"]), last=float(last["price"]),
                    book_time=int(book["time"]), mark_time=int(mark["time"]), last_time=int(last["time"]),
                    fetched_at=self.now(), funding_rate=float(mark["lastFundingRate"]),
                    next_funding=int(mark["nextFundingTime"]))

    def inspect(self, symbol, mode, contract, fresh=False):
        bt, et = MODES[mode]
        b = self.candles(symbol, bt, fresh=fresh)
        e = self.candles(symbol, et, fresh=fresh)
        daily = b if bt == "1d" else self.candles(symbol, "1d", limit=3, fresh=fresh)
        provisional = assess(symbol, mode, b, e, daily, contract, self.now())
        if provisional.reasons != ["Bid/ask, mark, last belum tersedia"]:
            return provisional
        e = self.candles(symbol, et, fresh=True)
        quote = self.quote(symbol)
        return assess(symbol, mode, b, e, daily, contract, self.now(), quote)

    def scan(self, mode="INTRADAY", limit=30, min_volume=10_000_000, symbols=None, progress=None):
        self.sync_clock()
        universe, contracts, eligible = self.universe(limit, min_volume, symbols)
        results, errors = [], []
        for i, ticker in enumerate(universe):
            symbol = ticker["symbol"]
            try:
                result = self.inspect(symbol, mode, contracts[symbol])
                result.values["quote_volume"] = float(ticker["quoteVolume"])
                results.append(result)
            except (MarketError, ValueError, KeyError, TypeError) as exc:
                errors.append({"Pair": symbol, "Kendala": str(exc)})
                if time.monotonic() < self.cooldown_until:
                    break
            if progress:
                progress(i + 1, len(universe), symbol)
        return {"results": results, "errors": errors, "eligible": eligible, "selected": len(universe),
                "finished_at": self.now(), "mode": mode, "contracts": contracts}

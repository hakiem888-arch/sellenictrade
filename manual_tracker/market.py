"""Public REST polling fallback for the original REST-only Binance integration.

SPOT uses timestamped latest trades; LINEAR uses timestamped mark price.
No private endpoints, credentials, orders, balances, or synthetic fallback prices.
"""
from collections import deque
from decimal import Decimal
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .engine import InvalidData, Observation, number, symbol_name


class DataUnavailable(Exception):
    pass


INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
HOSTS = {"SPOT": "https://data-api.binance.vision", "LINEAR": "https://fapi.binance.com"}
PATHS = {"SPOT": {"/api/v3/exchangeInfo", "/api/v3/trades", "/api/v3/klines"},
         "LINEAR": {"/fapi/v1/exchangeInfo", "/fapi/v1/premiumIndex", "/fapi/v1/markPriceKlines"}}


class PublicHTTP:
    def __init__(self, limit=120, opener=urlopen, clock=time.time):
        self.limit, self.opener, self.clock = limit, opener, clock
        self.calls, self.blocked_until = deque(), 0.0

    def get(self, instrument, path, params):
        if instrument not in HOSTS or path not in PATHS[instrument]:
            raise InvalidData("PUBLIC_ENDPOINT_NOT_ALLOWED")
        now = self.clock()
        while self.calls and self.calls[0] <= now - 60:
            self.calls.popleft()
        if now < self.blocked_until or len(self.calls) >= self.limit:
            raise DataUnavailable("RATE_LIMIT_COOLDOWN")
        self.calls.append(now)
        req = Request(HOSTS[instrument] + path + ("?" + urlencode(params) if params else ""),
                      headers={"Accept": "application/json", "User-Agent": "SecretAgent-5.5-ManualTracker"}, method="GET")
        try:
            with self.opener(req, timeout=5) as response:
                result = json.load(response)
        except HTTPError as e:
            if e.code in {418, 429}:
                try:
                    delay = max(1, float(e.headers.get("Retry-After", "60")))
                    if not math.isfinite(delay):
                        delay = 60
                except (ValueError, TypeError):
                    delay = 60
                self.blocked_until = now + delay
            raise DataUnavailable(f"PUBLIC_HTTP_{e.code}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DataUnavailable("PUBLIC_NETWORK_OR_JSON_ERROR") from None
        return result


def completed_atr(rows, period, interval, now):
    """Rolling arithmetic mean of true range over `period` CLOSED bars."""
    try:
        parsed = []
        for row in rows:
            start, end = float(row[0]) / 1000, float(row[6]) / 1000
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise InvalidData("INVALID_CANDLE_TIME")
            if end >= now:
                continue
            high, low, close, opened = number(row[2]), number(row[3]), number(row[4]), number(row[1])
            if high < max(opened, close) or low > min(opened, close) or high < low:
                raise InvalidData("INVALID_OHLC")
            if abs((end - start) - interval) > 0.01:
                raise InvalidData("INVALID_CANDLE_DURATION")
            parsed.append((start, end, high, low, close))
        if len(parsed) < period + 1:
            raise InvalidData("INSUFFICIENT_COMPLETED_BARS")
        bars = parsed[-period - 1:]
        if any(abs(b[0] - a[0] - interval) > 0.001 for a, b in zip(bars, bars[1:])):
            raise InvalidData("CANDLE_GAP_OR_DUPLICATE")
        ranges = [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(bars, bars[1:])]
        atr = sum(ranges, Decimal(0)) / period
        return number(atr, "ATR"), bars[-1][1]
    except (TypeError, KeyError, IndexError, ValueError) as e:
        if isinstance(e, InvalidData):
            raise
        raise InvalidData("MALFORMED_CANDLES") from None


class BinancePollingFeed:
    def __init__(self, config, http=None, clock=time.time):
        self.config, self.clock = config, clock
        self.http = http or PublicHTTP(config.requests_per_minute, clock=clock)
        self._metadata, self._bars = {}, {}

    def reset(self):
        # Resynchronize both metadata and completed candles after an outage.
        self._metadata.clear()
        self._bars.clear()

    def metadata_observed_at(self, instrument, symbol):
        return self._metadata[(instrument, symbol)][0]

    def metadata(self, instrument, symbol):
        now = self.clock()
        key = (instrument, symbol)
        cached = self._metadata.get(key)
        if cached and now - cached[0] < 600:
            return cached[1]
        path = "/api/v3/exchangeInfo" if instrument == "SPOT" else "/fapi/v1/exchangeInfo"
        data = self.http.get(instrument, path, {"symbol": symbol} if instrument == "SPOT" else {})
        try:
            rows = data["symbols"]
            for row in rows:
                if row.get("quoteAsset") != "USDT" or row.get("status") != "TRADING":
                    continue
                if instrument == "LINEAR" and (row.get("contractType") != "PERPETUAL" or row.get("marginAsset") != "USDT"):
                    continue
                if instrument == "SPOT" and row.get("isSpotTradingAllowed") is not True:
                    continue
                tick = next(f["tickSize"] for f in row["filters"] if f["filterType"] == "PRICE_FILTER")
                self._metadata[(instrument, row["symbol"])] = (now, number(tick))
        except (KeyError, StopIteration, TypeError):
            raise InvalidData("INCOMPLETE_EXCHANGE_METADATA") from None
        value = self._metadata.get(key)
        if not value or value[0] != now:
            raise InvalidData("UNSUPPORTED_OR_INACTIVE_INSTRUMENT")
        return value[1]

    def observation(self, details):
        instrument = details["instrument"]
        if instrument not in HOSTS or number(details["multiplier"]) != 1:
            raise InvalidData("UNSUPPORTED_INSTRUMENT")
        symbol = symbol_name(details["symbol"])
        tick = self.metadata(instrument, symbol)
        interval = INTERVALS[self.config.atr_interval]
        key = (instrument, symbol)
        cached = self._bars.get(key)
        now = self.clock()
        # Refresh shortly after every close, never use an in-progress bar.
        if not cached or now >= cached[1] + interval + 1:
            path = "/api/v3/klines" if instrument == "SPOT" else "/fapi/v1/markPriceKlines"
            rows = self.http.get(instrument, path, {"symbol": symbol, "interval": self.config.atr_interval, "limit": self.config.atr_period + 3})
            cached = completed_atr(rows, self.config.atr_period, interval, self.clock())
            self._bars[key] = cached
        # Fetch the quote LAST so slow metadata/candle requests cannot turn an
        # old quote into a fresh recommendation by assigning a receipt time.
        try:
            if instrument == "SPOT":
                trades = self.http.get(instrument, "/api/v3/trades", {"symbol": symbol, "limit": 1})
                price, stamp, reference = trades[-1]["price"], trades[-1]["time"], "LAST"
            else:
                result = self.http.get(instrument, "/fapi/v1/premiumIndex", {"symbol": symbol})
                if result.get("symbol") != symbol:
                    raise InvalidData("WRONG_SYMBOL")
                price, stamp, reference = result["markPrice"], result["time"], "MARK"
            stamp = float(stamp) / 1000
            if not math.isfinite(stamp):
                raise InvalidData("INVALID_PRICE_TIMESTAMP")
        except (KeyError, IndexError, TypeError, ValueError) as e:
            if isinstance(e, InvalidData):
                raise
            raise InvalidData("INCOMPLETE_PRICE") from None
        return Observation(number(price), stamp, cached[0], cached[1], tick, reference, interval)

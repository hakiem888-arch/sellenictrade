"""Only public USD-M futures endpoints; no spot fallback and no order API."""
import time
import re
import threading
import requests
import pandas as pd
from .models import Contract, Snapshot, TIMEFRAMES

BASE = "https://fapi.binance.com"


class MarketError(RuntimeError):
    pass


class FuturesMarket:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.cooldown_until = 0.0
        self.lock = threading.RLock()

    def get(self, path, params=None):
        if not path.startswith("/fapi/"):
            raise MarketError("Hanya endpoint futures diizinkan")
        with self.lock:
            if time.time() < self.cooldown_until:
                raise MarketError("Bursa meminta jeda. Coba lagi setelah cooldown.")
            try:
                response = self.session.get(BASE + path, params=params, timeout=(5, 12), allow_redirects=False)
            except requests.RequestException:
                raise MarketError("Koneksi data futures gagal. Tidak menggunakan data spot pengganti.") from None
            if response.status_code in (418, 429):
                try:
                    delay = max(1, float(response.headers.get("Retry-After", "60")))
                except ValueError:
                    delay = 60
                self.cooldown_until = time.time() + delay
                raise MarketError(f"Batas permintaan bursa. Tunggu {delay:.0f} detik.")
            if response.status_code != 200:
                raise MarketError(f"Data futures tidak tersedia (HTTP {response.status_code}).")
            try:
                return response.json()
            except ValueError:
                raise MarketError("Respons bursa bukan JSON valid.") from None

    def candles(self, symbol, interval, limit=1000, mark=False):
        path = "/fapi/v1/markPriceKlines" if mark else "/fapi/v1/klines"
        rows = self.get(path, {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(rows, list) or not rows:
            raise MarketError("Data candle kosong")
        d = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                       "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
        for c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d

    def load(self, symbol, mode):
        symbol = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,25}USDT", symbol) or mode not in TIMEFRAMES:
            raise MarketError("Gunakan simbol USDT perpetual dan mode yang tersedia.")
        start_clock = time.monotonic()
        initial_server = int(self.get("/fapi/v1/time")["serverTime"])
        # Snapshot boundary is fixed before downloads to avoid including a candle
        # that only closed after a timeframe was fetched. evaluate() checks it again.
        bias_tf, exec_tf = TIMEFRAMES[mode]
        info = self.get("/fapi/v1/exchangeInfo")
        spec_raw = next((x for x in info.get("symbols", []) if x.get("symbol") == symbol), None)
        if spec_raw is None:
            raise MarketError("Simbol tidak ditemukan pada Binance USD-M Futures.")
        try:
            f = {x["filterType"]: x for x in spec_raw["filters"]}
            p, q, mq = f["PRICE_FILTER"], f["LOT_SIZE"], f["MARKET_LOT_SIZE"]
            percent = f.get("PERCENT_PRICE", {})
            spec = Contract(tick=float(p["tickSize"]), step=float(q["stepSize"]), min_qty=float(q["minQty"]),
                            max_qty=float(q["maxQty"]), min_notional=float(f["MIN_NOTIONAL"]["notional"]),
                            market_step=float(mq["stepSize"]), market_min_qty=float(mq["minQty"]), market_max_qty=float(mq["maxQty"]),
                            min_price=float(p["minPrice"]), max_price=float(p["maxPrice"]),
                            multiplier_down=float(percent.get("multiplierDown", 0)), multiplier_up=float(percent.get("multiplierUp", "inf")),
                            status=spec_raw["status"], contract_type=spec_raw["contractType"], quote_asset=spec_raw["quoteAsset"])
        except (KeyError, ValueError, TypeError):
            raise MarketError("Spesifikasi tick/lot/notional kontrak tidak lengkap.") from None
        bias_raw, execution_raw = self.candles(symbol, bias_tf), self.candles(symbol, exec_tf)
        daily = self.candles(symbol, "1d", 4)
        funding_interval = None
        warnings = []
        try:
            rates = self.get("/fapi/v1/fundingInfo")
            for item in rates:
                if item.get("symbol") == symbol:
                    funding_interval = float(item["fundingIntervalHours"])
        except (MarketError, KeyError, TypeError, ValueError):
            warnings.append("Interval funding tidak tersedia dari fundingInfo; tidak diasumsikan 8 jam.")
        if funding_interval is None:
            warnings.append("Interval funding tidak dipastikan. Cadangan total horizon harus dikonfirmasi pengguna.")
        depth = self.get("/fapi/v1/depth", {"symbol": symbol, "limit": 100})
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        mark = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        last = self.get("/fapi/v2/ticker/price", {"symbol": symbol})
        current_candles = self.candles(symbol, exec_tf, 2)
        current_marks = self.candles(symbol, exec_tf, 2, mark=True)
        now = int(self.get("/fapi/v1/time")["serverTime"])
        if now < initial_server or time.monotonic() - start_clock > 120:
            raise MarketError("Pengambilan snapshot terlalu lama atau waktu bursa tidak konsisten.")
        bias = bias_raw[bias_raw.close_time < initial_server].reset_index(drop=True)
        execution = execution_raw[execution_raw.close_time < initial_server].reset_index(drop=True)
        open_rows = current_candles[current_candles.close_time >= initial_server]
        open_high = float(open_rows.high.max()) if len(open_rows) else None
        open_low = float(open_rows.low.min()) if len(open_rows) else None
        mark_rows = current_marks[current_marks.close_time >= initial_server]
        day_start = (initial_server // 86400000) * 86400000
        previous = daily[daily.open_time == day_start - 86400000]
        # The freshest in-progress candle is checked again after expensive AI work.
        return Snapshot(symbol=symbol, mode=mode, now_ms=now, bias=bias, execution=execution, contract=spec,
                        last=float(last["price"]), mark=float(mark["markPrice"]), bid=float(book["bidPrice"]), ask=float(book["askPrice"]),
                        quote_times={"last": int(last.get("time", 0)), "mark": int(mark.get("time", 0)),
                                     "book": int(book.get("time", 0)), "depth": int(depth.get("T", depth.get("E", 0)))},
                        bids=depth.get("bids", []), asks=depth.get("asks", []),
                        previous_day_high=float(previous.iloc[-1].high) if len(previous) else None,
                        previous_day_low=float(previous.iloc[-1].low) if len(previous) else None,
                        funding_rate=float(mark["lastFundingRate"]), next_funding_ms=int(mark["nextFundingTime"]),
                        funding_interval_hours=funding_interval, open_bar_high=open_high, open_bar_low=open_low,
                        open_mark_high=float(mark_rows.high.max()) if len(mark_rows) else None,
                        open_mark_low=float(mark_rows.low.min()) if len(mark_rows) else None, warnings=warnings)

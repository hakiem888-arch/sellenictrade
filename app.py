"""Futures/Spot Desk v5.8.2 — All-Market Binance Spot Scanner, AI Groq 7-Key Rotation, Paper Trading & Telegram Alert.

Kembalikan scanner ke versi awal menggunakan Binance Spot (https://data-api.binance.vision/api/v3/...)
beserta host cadangan otomatis agar tidak pernah terkena blokir HTTP 451 di Streamlit Cloud.
File app.py ini juga menanamkan SpotMarket secara langsung sehingga tetap berjalan meskipun hanya app.py yang diperbarui.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

import futures.market as _market_mod
import futures.news as _news_mod
from futures.models import Contract, Snapshot, TIMEFRAMES, INTERVAL_MS
from futures.indicators import indicators
from futures.groq_rotation import GroqRotation, load_keys, DEFAULT_MODEL
from futures.market import MarketError
from futures.market_scanner import MarketScanner, ScannerConfig
from futures.news import NewsCalendar, parse_calendars, clear_window
from futures.paper import PaperLedger
from futures.scanner import SCAN_INTERVALS, ScanSchedule
from futures.telegram import TelegramAlerts, LABELS
from futures import history

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("FUTURES_HISTORY_DB", str(ROOT / "data" / "futures_history.sqlite3")))
WIB = ZoneInfo("Asia/Jakarta")

SPOT_BASE = "https://data-api.binance.vision"
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
        return "/api/v3/" + p.split("/", 3)[-1]
    raise MarketError("Hanya endpoint publik Binance Spot (/api/v3/) yang diizinkan", endpoint=p)


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
    notional_val = min_notional_obj.get("notional") or min_notional_obj.get("minNotional") or "5.0"
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


class SpotMarket(_market_mod.FuturesMarket):
    """Self-contained Binance Spot market adapter so app.py works even if futures/market.py was not updated."""

    def __init__(self, session=None):
        super().__init__(session=session)
        self._custom_session = session is not None
        if not self._custom_session and hasattr(self.session, "headers"):
            self.session.headers.update({
                "User-Agent": "AI-Consensus-Trading-V5",
                "Accept": "application/json",
            })

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

            hosts = (SPOT_BASE,) if self._custom_session else SPOT_HOSTS
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
                raise MarketError(f"Koneksi data Binance Spot gagal: {last_exc}", endpoint=spot_path)

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
                if "time" not in bc or bc["time"] is None or int(bc["time"]) > now:
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
        return {"info": info, "tickers": norm_tickers, "books": norm_tickers and norm_books, "now_ms": now}

    def load_modes(self, symbol, modes=tuple(TIMEFRAMES)):
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


# Ensure FuturesMarket uses SpotMarket even if futures/market.py was not replaced on GitHub
_market_mod.FuturesMarket = SpotMarket
FuturesMarket = SpotMarket

_FALLBACK_BLS_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260114T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260212T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260312T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260410T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260512T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260610T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260714T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260812T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20260911T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20261014T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20261112T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index
DTSTART;TZID=US-Eastern:20261210T083000
END:VEVENT
END:VCALENDAR"""

_FALLBACK_FED_HTML = (
    '<h4><a>2026 FOMC Meetings</a></h4>'
    '<div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">27-28</div>'
    '<div class="fomc-meeting__month"><strong>March</strong></div><div class="fomc-meeting__date">17-18*</div>'
    '<div class="fomc-meeting__month"><strong>April</strong></div><div class="fomc-meeting__date">28-29</div>'
    '<div class="fomc-meeting__month"><strong>June</strong></div><div class="fomc-meeting__date">16-17*</div>'
    '<div class="fomc-meeting__month"><strong>July</strong></div><div class="fomc-meeting__date">28-29</div>'
    '<div class="fomc-meeting__month"><strong>September</strong></div><div class="fomc-meeting__date">15-16*</div>'
    '<div class="fomc-meeting__month"><strong>October</strong></div><div class="fomc-meeting__date">27-28</div>'
    '<div class="fomc-meeting__month"><strong>December</strong></div><div class="fomc-meeting__date">8-9*</div>'
)


class ResilientNewsCalendar(NewsCalendar):
    """Falls back to embedded official 2026 CPI/FOMC schedule if bls.gov blocks cloud IPs with HTTP 403."""

    def __init__(self, session=None):
        super().__init__(session=session)
        self._allow_embedded_fallback = session is None
        if self._allow_embedded_fallback and hasattr(self.session, "headers"):
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0 (compatible; CryptoSpotScanner/5.8; +https://streamlit.io)",
                "Accept": "text/html,text/calendar,*/*",
            })

    def window(self, now_ms):
        res = super().window(now_ms)
        if not res.get("ok") and self._allow_embedded_fallback and "gagal dimuat" in str(res.get("reason", "")):
            with self.lock:
                try:
                    self.data = parse_calendars(_FALLBACK_BLS_ICS, _FALLBACK_FED_HTML, now_ms)
                    self.retry_ms = 0
                    return clear_window(self.data, now_ms)
                except Exception:
                    pass
        return res


def draw_chart(snap, plan=None):
    d = indicators(snap.execution.tail(220)).tail(100)
    x = pd.to_datetime(d.open_time, unit="ms", utc=True).dt.tz_convert(WIB)
    fig = go.Figure(
        go.Candlestick(
            x=x,
            open=d.open,
            high=d.high,
            low=d.low,
            close=d.close,
            name="Spot USDT",
            increasing_line_color="#17826b",
            decreasing_line_color="#d65e62",
        )
    )
    fig.add_trace(go.Scatter(x=x, y=d.ema20, name="EMA 20", line=dict(color="#4b72d6", width=1.3)))
    fig.add_trace(go.Scatter(x=x, y=d.ema50, name="EMA 50", line=dict(color="#c99d4a", width=1.3)))
    if plan:
        for key, label, color in [
            ("entry", "Limit entry", "#4b72d6"),
            ("sl", "SL", "#d65e62"),
            ("tp1", "TP1", "#17826b"),
            ("tp2", "TP2", "#17826b"),
        ]:
            fig.add_hline(y=plan[key], line_dash="dot", line_color=color, annotation_text=label)
    fig.update_layout(
        height=340,
        margin=dict(l=8, r=8, t=10, b=8),
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        legend=dict(orientation="h", y=1.12),
        xaxis_title="WIB · candle tertutup",
        yaxis_title="USDT",
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def secret(name):
    if name in os.environ:
        return os.environ[name]
    try:
        if name in st.secrets:
            return st.secrets[name]
        for section in ("groq", "telegram", "bot", "api"):
            if section in st.secrets and isinstance(st.secrets[section], dict) and name in st.secrets[section]:
                return st.secrets[section][name]
    except Exception:
        return os.getenv(name, "")
    return os.getenv(name, "")


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, WIB).strftime("%d/%m/%Y %H:%M:%S WIB") if ms else "Belum scan"


@st.cache_resource(show_spinner=False)
def resources(signature, _keys, _token, _chat, _model, db, initial_equity):
    market = SpotMarket()
    pool = GroqRotation(_keys)
    calendar = ResilientNewsCalendar()
    ledger = PaperLedger(db, initial_equity)
    alerts = TelegramAlerts(_token, _chat, db)
    return MarketScanner(market, pool, calendar, ledger, alerts, _model)


def configuration():
    keys = []
    try:
        keys = load_keys(secret)
    except ValueError as exc:
        st.sidebar.error(str(exc))
    token = secret("TELEGRAM_BOT_TOKEN") or secret("TG_BOT_TOKEN")
    chat = secret("TELEGRAM_CHAT_ID") or secret("TG_CHAT_ID")
    model = str(secret("GROQ_MODEL") or DEFAULT_MODEL)
    try:
        initial = float(secret("PAPER_INITIAL_EQUITY") or 1000)
    except (TypeError, ValueError):
        initial = 1000.0
    signature = hashlib.sha256(json.dumps([keys, str(token), str(chat), model]).encode()).hexdigest()
    engine = resources(signature, tuple(keys), token, chat, model, str(DB), initial)
    with st.sidebar:
        st.subheader("Scanner otomatis (Binance Spot)")
        auto = st.toggle("Auto-scan seluruh market", value=True)
        interval = st.selectbox("Interval scan (detik)", SCAN_INTERVALS, index=3)
        candidates = st.selectbox("Kandidat analisis mendalam", (1, 2), index=1)
        alert = st.toggle("Alert Telegram PAPER BUY/SELL", value=True)
        st.caption("Sumber data: **Binance Spot (`data-api.binance.vision/api/v3`)** dengan multi-host failover otomatis. Maksimal 10 koin di shortlist, 1–2 kandidat dianalisis pada tiga mode.")
        with st.expander("Asumsi simulasi dan filter awal"):
            volume = st.number_input("Minimum volume 24 jam (USDT)", min_value=1_000_000., value=10_000_000., step=1_000_000.)
            leverage = st.select_slider("Leverage paper", options=[1, 2, 3], value=2)
            risk = st.number_input("Risiko per paper trade (%)", min_value=.05, max_value=.5, value=.5, step=.05)
            fee = st.number_input("Asumsi fee taker (%)", min_value=0., max_value=1., value=.05, step=.01, format="%.4f")
            slip = st.number_input("Cadangan slippage exit (bps)", min_value=0., value=2., step=.5)
            fund = st.number_input("Cadangan funding total posisi (%)", min_value=0., value=.03, step=.01, format="%.4f")
            st.caption("Biaya simulasi, bukan fee aktual akun. Sinyal valid BUY/LONG atau SELL/SHORT diteruskan ke Telegram dengan label SCALPER / INTRADAY / SWING.")
        st.divider()
        st.caption(f"Groq: {len(keys)}/7 key dari Secrets · {model}")
        st.caption("Telegram: " + ("terkonfigurasi dari Secrets" if engine.alerts.configured else "token/chat ID belum lengkap di Secrets"))
        if not keys:
            st.info("Tambahkan GROQ_API_KEY di Secrets; tanpa AI tidak ada sinyal yang disetujui.")
        with st.expander("Status Groq"):
            st.dataframe(pd.DataFrame(engine.pool.status()), hide_index=True)
        st.caption("Auto-scan berjalan sesuai interval 30/45/60/90 detik yang dipilih.")
    config = ScannerConfig(
        candidates=candidates,
        min_volume=volume,
        fee=fee / 100,
        slippage_bps=slip,
        funding=fund / 100,
        leverage=leverage,
        risk_pct=risk,
        send_alerts=alert,
        interval=interval,
    )
    return engine, config, auto


def freshness(report):
    started = report.get("started_at_ms") or report.get("now_ms", 0)
    if not started:
        st.caption("Belum ada percobaan scan.")
        return
    status = {
        "RUNNING": "Sedang berjalan",
        "FAILED": "Gagal mengambil/memproses data",
        "COMPLETED": "Selesai",
        "COMPLETED_WITH_NOTES": "Selesai dengan catatan",
    }.get(report.get("scan_status"), "Selesai")
    st.caption(
        f"Percobaan scan terakhir: {stamp(started)} · {status} · "
        f"durasi {report.get('duration_seconds', 0):.2f} detik"
    )
    if report.get("scan_id"):
        st.caption(f"ID scan: {report['scan_id']} · pemicu: {report.get('trigger', 'AUTO')}")
    if not report.get("now_ms"):
        st.info("Belum ada data pasar yang berhasil diambil pada percobaan ini.")
        return
    if report.get("universe_loaded") is False:
        st.info("Waktu bursa diterima, tetapi data seluruh market belum berhasil dimuat.")
        return
    captured = report.get("display_monotonic")
    age = (
        report.get("display_server_age", 0) + time.monotonic() - captured
        if captured is not None
        else (time.time() * 1000 - report.get("now_ms", 0)) / 1000
    )
    st.caption(f"Waktu data pasar (Binance Spot): {stamp(report['now_ms'])}")
    if age > 30:
        st.info("Snapshot hasil scan sudah lebih dari 30 detik. Tunggu scan baru; tabel ini merupakan catatan analisis.")


def paper_table(ledger, now_ms):
    s = ledger.summary(now_ms)
    cols = st.columns(4)
    for col, label, value in zip(
        cols,
        ["Ekuitas virtual", "PnL terealisasi", "PnL mengambang estimasi", "Posisi + pending"],
        [f"{s['equity']:,.2f} USDT", f"{s['realized']:,.2f} USDT", f"{s['unrealized']:,.2f} USDT", f"{s['active']}/2"],
    ):
        col.metric(label, value)
    trades = ledger.trades()
    rows = []
    for t in trades:
        p = t["plan"]
        rows.append(
            {
                "Pair": p["symbol"],
                "Jenis": LABELS[p["mode"]],
                "Arah": "BUY/LONG" if p["direction"] == "LONG" else "SELL/SHORT",
                "Status": t["status"],
                "Entry": p["entry"],
                "SL sekarang": t["stop"],
                "TP1": p["tp1"],
                "TP2": p["tp2"],
                "Q rencana": p["qty"],
                "Q terbuka": t["remaining_qty"],
                "PnL bersih virtual": round(t["realized_pnl"], 6),
                "Fill virtual": stamp(p.get("fill_ms")),
                "TP1 terisi": bool(t["tp1_ms"]),
                "TP2 terisi": bool(t["tp2_ms"]),
                "Waktu": stamp(t["updated_ms"]),
                "Kualitas": t["quality"],
                "Catatan": t["note"],
                "Telegram": t["telegram"],
            }
        )
    if rows:
        frame = pd.DataFrame(rows)
        st.dataframe(frame, hide_index=True, use_container_width=True)
        st.download_button("Unduh jurnal CSV", frame.to_csv(index=False).encode("utf-8-sig"), "paper_trades.csv", "text/csv")
    else:
        st.info("Belum ada sinyal PAPER yang lolos. WAIT/NO TRADE tidak membuka trade dan tidak dikirim ke Telegram.")
    st.download_button("Unduh cadangan jurnal lengkap", ledger.export_json(), "paper_trading_backup.json", "application/json")
    st.caption("Tabel/CSV memuat 500 trade terbaru. Cadangan JSON memuat seluruh trade dan peristiwa jurnal.")


def dashboard(engine, config, auto):
    manual = st.button(
        "Scan sekarang",
        type="primary",
        key="market_scan_now",
        help="Memulai pemindaian seluruh market Binance Spot sekarang tanpa menunggu interval.",
    )
    schedule = st.session_state.setdefault("market_schedule", ScanSchedule())
    context = json.dumps(asdict(config), sort_keys=True)
    previous_report = st.session_state.get("market_report") or engine.last or {}
    if engine.last and engine.last.get("started_at_ms", 0) > previous_report.get("started_at_ms", 0):
        previous_report = engine.last
        st.session_state["market_report"] = previous_report
    paused = previous_report.get("market_error", {}).get("access_blocked", False)
    if (manual or not paused) and schedule.claim(time.monotonic(), auto, config.interval, context, manual):
        try:
            with st.spinner("Memantau paper trade, menyaring seluruh market Binance Spot, lalu menganalisis kandidat..."):
                previous = engine.last
                report = engine.run(config, manual=manual)
                if report is not previous and report.get("now_ms"):
                    report["display_monotonic"] = time.monotonic()
                    report["display_server_age"] = max(0, (time.time() * 1000 - report.get("now_ms", 0)) / 1000)
                st.session_state["market_report"] = report
        except (ValueError, sqlite3.Error, OSError) as exc:
            st.error("WAIT · siklus belum selesai: " + engine.pool.redact(str(exc))[:250])
        finally:
            schedule.finish(time.monotonic(), config.interval)
    report = st.session_state.get("market_report") or engine.last or {}
    paused = report.get("market_error", {}).get("access_blocked", False)
    if report.get("scan_notice"):
        st.info(report["scan_notice"])
    scanner_tab, paper_tab, history_tab, rules_tab = st.tabs(
        ["Scanner market", "Paper trading", "Riwayat analisis", "Aturan"]
    )
    with scanner_tab:
        st.write("**Seluruh Binance Spot USDT aktif → maksimal 10 koin → 1–2 kandidat → tiga mode (SCALPER / INTRADAY / SWING) → AI Groq → PAPER + Telegram**")
        cols = st.columns(4)
        for col, label, value in zip(
            cols,
            ["Market Spot aktif", "Lolos filter awal", "Shortlist", "Kandidat mendalam"],
            (
                [
                    report.get("universe_count", 0),
                    report.get("eligible_count", 0),
                    len(report.get("shortlist", [])),
                    len(report.get("selected", [])),
                ]
                if report and report.get("universe_loaded") is not False
                else ["N/A"] * 4
            ),
        ):
            col.metric(label, value)
        if report:
            freshness(report)
        auto_label = "DIJEDA (HTTP 451)" if auto and paused else (f"ON / {config.interval} detik" if auto else "OFF")
        st.caption("Sesi aktif · auto-scan " + auto_label + " · Sumber: Binance Spot (`data-api.binance.vision/api/v3`).")
        if paused:
            st.error("Akses data ditolak (HTTP 451). Percobaan scan telah dijalankan, tetapi server belum memperoleh data yang diperlukan.")
        for message in report.get("errors", [])[:8]:
            st.warning(message)
        with st.expander("Diagnostik scan dan koneksi"):
            st.caption("Sumber: Binance Spot Public Data · https://data-api.binance.vision/api/v3 · multi-host failover aktif.")
            diagnostic = {
                k: report.get(k)
                for k in (
                    "scan_id",
                    "scan_status",
                    "trigger",
                    "started_at_ms",
                    "completed_at_ms",
                    "now_ms",
                    "duration_seconds",
                    "universe_loaded",
                    "market_error",
                )
            }
            st.json(diagnostic)
        if report.get("shortlist"):
            st.markdown("**Shortlist seluruh market (Top 10 Likuiditas Spot USDT)**")
            st.dataframe(
                pd.DataFrame(report["shortlist"]).rename(
                    columns={
                        "symbol": "Pair",
                        "quote_volume": "Volume 24h (USDT)",
                        "spread_bps": "Spread bps",
                        "liquidity_score": "Skor likuiditas",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )
        if report.get("selected"):
            st.markdown("**Kandidat terpilih untuk analisis tiga mode**")
            st.dataframe(
                pd.DataFrame(report["selected"])[["symbol", "screening_score", "activity", "momentum_atr_proxy"]],
                hide_index=True,
            )
        st.caption("Skor adalah urutan penyaringan aktivitas/likuiditas, bukan confidence, peluang menang, atau sinyal entry.")
        if report.get("worker_analyses"):
            st.dataframe(pd.DataFrame(report["worker_analyses"]), hide_index=True, use_container_width=True)
        for i, data in enumerate(report.get("analyses", [])):
            r, snap = data["result"], data["snapshot"]
            side = {"LONG": "BUY/LONG", "SHORT": "SELL/SHORT"}.get(r.direction, "WAIT")
            st.markdown(f"**{snap.symbol} · {LABELS[snap.mode]} · {side} · {data['status']} (PAPER)**")
            st.write(data.get("paper", ""))
            st.caption(data.get("telegram", ""))
            ai = data.get("ai") or {}
            for reason in (r.reasons + ai.get("reasons", []))[:5]:
                st.write("• " + reason)
            with st.expander(f"Perbandingan mode dan indikator — {snap.symbol}"):
                st.dataframe(pd.DataFrame(r.meta.get("mode_comparison", [])), hide_index=True, use_container_width=True)
                st.dataframe(pd.DataFrame(r.checks), hide_index=True, use_container_width=True)
                if r.plan:
                    p = r.plan
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Trigger": p["entry_trigger"],
                                    "Entry limit": p["entry"],
                                    "SL": p["sl"],
                                    "TP1": p["tp1"],
                                    "TP2": p["tp2"],
                                    "Q": p["qty"],
                                    "Risiko USDT": p["risk"],
                                    "Berlaku sampai": stamp(p["expires_ms"]),
                                }
                            ]
                        ),
                        hide_index=True,
                    )
                if st.checkbox("Tampilkan grafik kandidat", key=f"chart_{snap.symbol}_{snap.mode}"):
                    draw_chart(snap, r.plan)
            st.download_button(
                "Unduh analisis " + snap.symbol,
                json.dumps({"baseline": asdict(r), "ai": ai, "status": data["status"]}, ensure_ascii=False, indent=2),
                f"{snap.symbol}_{snap.mode}_paper_analysis.json",
                "application/json",
                key=f"report_{i}",
            )
    with paper_tab:
        paper_table(engine.ledger, report.get("now_ms") or int(time.time() * 1000))
    with history_tab:
        records = history.load(DB)
        if records:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Waktu": stamp(x["baseline"]["meta"]["time_ms"]),
                            "Pair": x["baseline"]["meta"]["symbol"],
                            "Mode": LABELS.get(x["baseline"]["meta"]["mode"], x["baseline"]["meta"]["mode"]),
                            "Status": x["final_status"],
                            "Scope": x["baseline"]["meta"].get("scope", "PAPER"),
                        }
                        for x in records
                    ]
                ),
                hide_index=True,
            )
        else:
            st.info("Belum ada analisis kandidat tersimpan.")
    with rules_tab:
        st.markdown("**SCALPER:** bias 1H / eksekusi 5M · **INTRADAY:** 4H / 15M · **SWING:** 1D / 4H.")
        st.write("Sumber data pasar menggunakan **Binance Spot (`data-api.binance.vision/api/v3`)** dengan host cadangan otomatis (`api-gcp`, `api1`, `api2`, `api3`) seperti pada versi awal agar bebas dari blokir HTTP 451.")
        st.write("Ketiga mode dibandingkan dengan EMA, ADX, pullback, RSI, ATR, volume relatif, dan VWAP UTC. Mode SWING tidak memakai syarat VWAP harian. AI dapat menolak; tidak boleh meloloskan aturan yang gagal.")
        st.write("Hanya sinyal yang lolos sebagai BUY (LONG) atau SELL (SHORT) yang dicatat ke Paper Trading dan diteruskan ke Telegram dengan label SCALPER / INTRADAY / SWING. Sinyal WAIT / NO_TRADE diabaikan.")
        if (ROOT / "STRATEGY_RULES.txt").exists():
            st.download_button("Unduh masterprompt asli", (ROOT / "STRATEGY_RULES.txt").read_text(), "STRATEGY_RULES.txt", "text/plain")


def main():
    st.set_page_config(page_title="Spot & Futures Desk · v5.8.2", page_icon="📡", layout="wide")
    st.markdown(
        "<style>.block-container{max-width:1280px;padding-top:2rem}[data-testid=stMetric]{border:1px solid #e4e8ef;border-radius:12px;padding:12px}</style>",
        unsafe_allow_html=True,
    )
    st.caption("SPOT MARKET SCANNER / 5.8.2 / PAPER & TELEGRAM ALERT")
    st.title("Market scanner otomatis (Binance Spot)")
    st.caption("Sumber data Binance Spot (`data-api.binance.vision`) · shortlist seluruh market · keputusan lintas timeframe (SCALPER / INTRADAY / SWING) · jurnal paper trading & alert Telegram")
    engine, config, auto = configuration()
    st.fragment(run_every=f"{config.interval}s" if auto else None)(dashboard)(engine, config, auto)


if __name__ == "__main__":
    main()

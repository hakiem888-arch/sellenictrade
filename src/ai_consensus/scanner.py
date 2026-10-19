"""
Bidirectional market scanner, symmetric activity vs long/short strength scoring,
shared snapshot/order-book cache reuse, request-weight/Retry-After budget enforcement,
and per-symbol failure diagnostics for AI Consensus Trading (PH03-04 / F27, F28, T13, T14).
Strictly zero side-effects on import.
"""

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from .domain import is_finite_number
from .market import (
    GLOBAL_MARKET_CACHE,
    MarketSnapshotCache,
    TimeframeSnapshot,
    build_timeframe_snapshot,
    extract_closed_df,
    normalize_as_of_ms,
)

SCANNER_VERSION: str = "v6.0-bidirectional-scanner-1"

STABLE_BASE_ASSETS = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "EUR", "AEUR", "PAXG",
})

ENDPOINT_WEIGHTS: Dict[str, int] = {
    "/api/v3/ping": 1,
    "/api/v3/ticker/price": 2,
    "/api/v3/klines": 2,
    "/api/v3/depth": 5,
    "/api/v3/ticker/24hr": 40,
}


class BinanceRateLimitError(RuntimeError):
    """Explicit rate-limit (HTTP 429 / 418) error carrying retry_after seconds."""

    def __init__(self, message: str, status_code: int = 429, retry_after: float = 30.0) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.retry_after = float(retry_after)


def extract_retry_after_seconds(err_or_msg: Any, default_seconds: float = 30.0) -> float:
    """Extract Retry-After seconds from an exception attribute, header string, or error text."""
    if hasattr(err_or_msg, "retry_after") and is_finite_number(getattr(err_or_msg, "retry_after")):
        return max(1.0, float(getattr(err_or_msg, "retry_after")))
    text = str(err_or_msg or "")
    m = re.search(r"retry[-_\s]?after\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.I)
    if m:
        return max(1.0, float(m.group(1)))
    return max(1.0, float(default_seconds))


def is_http_429_error(err_or_msg: Any) -> bool:
    """Return True iff the error represents an HTTP 429 / 418 rate-limit condition."""
    if isinstance(err_or_msg, BinanceRateLimitError):
        return True
    if getattr(err_or_msg, "status_code", None) in (429, 418):
        return True
    msg = str(err_or_msg or "").lower()
    return "429" in msg or "418" in msg or "too many requests" in msg or "rate limit" in msg or "way too much request weight" in msg


class BinanceRequestBudget:
    """
    Thread-safe request count, request weight, and HTTP 429 Retry-After cooldown tracker (F28, T14).
    Prevents hammering Binance endpoints when a cycle budget is reached or a 429 Retry-After is active.
    """

    def __init__(
        self,
        max_requests_per_cycle: int = 1000,
        max_weight_per_cycle: int = 5000,
    ) -> None:
        self._lock = threading.Lock()
        self.max_requests_per_cycle = int(max_requests_per_cycle)
        self.max_weight_per_cycle = int(max_weight_per_cycle)
        self.requests_made = 0
        self.weight_used = 0
        self.cooldown_until_ms: int = 0
        self.last_retry_after_seconds: float = 0.0
        self.rate_limited_count = 0
        self.budget_blocked_count = 0

    def reset_cycle(
        self,
        max_requests_per_cycle: Optional[int] = None,
        max_weight_per_cycle: Optional[int] = None,
        preserve_cooldown: bool = True,
    ) -> None:
        with self._lock:
            if max_requests_per_cycle is not None:
                self.max_requests_per_cycle = int(max_requests_per_cycle)
            if max_weight_per_cycle is not None:
                self.max_weight_per_cycle = int(max_weight_per_cycle)
            self.requests_made = 0
            self.weight_used = 0
            self.budget_blocked_count = 0
            if not preserve_cooldown:
                self.cooldown_until_ms = 0
                self.last_retry_after_seconds = 0.0
                self.rate_limited_count = 0

    def check_and_reserve(self, path: str, now_ms: Optional[int] = None) -> Tuple[bool, str]:
        curr_ms = normalize_as_of_ms(now_ms)
        weight = int(ENDPOINT_WEIGHTS.get(path, 2))
        with self._lock:
            if self.cooldown_until_ms > curr_ms:
                rem_s = round((self.cooldown_until_ms - curr_ms) / 1000.0, 2)
                self.budget_blocked_count += 1
                return False, f"RATE_LIMIT_COOLDOWN:Retry-After {rem_s}s masih aktif"
            if self.requests_made >= self.max_requests_per_cycle:
                self.budget_blocked_count += 1
                return False, f"REQUEST_BUDGET_EXCEEDED:max_requests={self.max_requests_per_cycle}"
            if self.weight_used + weight > self.max_weight_per_cycle:
                self.budget_blocked_count += 1
                return False, f"WEIGHT_BUDGET_EXCEEDED:max_weight={self.max_weight_per_cycle}"
            self.requests_made += 1
            self.weight_used += weight
            return True, "OK"

    def record_rate_limit(self, retry_after_seconds: float, now_ms: Optional[int] = None) -> None:
        curr_ms = normalize_as_of_ms(now_ms)
        wait_s = max(1.0, float(retry_after_seconds))
        with self._lock:
            self.last_retry_after_seconds = wait_s
            self.cooldown_until_ms = max(self.cooldown_until_ms, curr_ms + int(round(wait_s * 1000.0)))
            self.rate_limited_count += 1

    def snapshot(self, now_ms: Optional[int] = None) -> Dict[str, Any]:
        curr_ms = normalize_as_of_ms(now_ms)
        with self._lock:
            in_cooldown = self.cooldown_until_ms > curr_ms
            rem_s = max(0.0, round((self.cooldown_until_ms - curr_ms) / 1000.0, 2)) if in_cooldown else 0.0
            return {
                "requests_made": self.requests_made,
                "weight_used": self.weight_used,
                "max_requests_per_cycle": self.max_requests_per_cycle,
                "max_weight_per_cycle": self.max_weight_per_cycle,
                "in_cooldown": in_cooldown,
                "cooldown_remaining_seconds": rem_s,
                "last_retry_after_seconds": self.last_retry_after_seconds,
                "rate_limited_count": self.rate_limited_count,
                "budget_blocked_count": self.budget_blocked_count,
            }


GLOBAL_REQUEST_BUDGET = BinanceRequestBudget()


@dataclass(frozen=True)
class ScanDiagnosticsReport:
    """Explicit quality & error diagnostics report for scan_markets and momentum_candidates (F28, T14)."""
    total_tickers: int
    liquid_universe_count: int
    target_scan_count: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    failed_symbols: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    cache_stats: Dict[str, int] = field(default_factory=dict)
    budget_stats: Dict[str, Any] = field(default_factory=dict)
    rate_limited: bool = False
    retry_after_seconds: float = 0.0
    scanner_version: str = SCANNER_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_tickers": self.total_tickers,
            "liquid_universe_count": self.liquid_universe_count,
            "target_scan_count": self.target_scan_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "failed_symbols": [dict(x) for x in self.failed_symbols],
            "cache_stats": dict(self.cache_stats),
            "budget_stats": dict(self.budget_stats),
            "rate_limited": self.rate_limited,
            "retry_after_seconds": self.retry_after_seconds,
            "scanner_version": self.scanner_version,
        }


def scanner_score_bidirectional(
    symbol: str,
    change: float,
    qvol: float,
    d: pd.DataFrame,
    ob: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compute symmetric bidirectional scanner scores separating activity_score from long_score and short_score (F27, T13).
    """
    d_closed = extract_closed_df(d)
    if d_closed is None or len(d_closed) == 0:
        return {
            "symbol": symbol,
            "score": 0,
            "activity_score": 0,
            "long_score": 0,
            "short_score": 0,
            "legacy_bull_score": 0,
            "direction_hint": "WAIT",
            "why": ["data tidak cukup"],
            "why_long": ["data tidak cukup"],
            "why_short": ["data tidak cukup"],
        }

    a = d_closed.iloc[-1]
    chg = float(change or 0.0)
    abs_chg = abs(chg)
    qv = max(0.0, float(qvol or 0.0))
    vr = float(a.VOL_RATIO) if ("VOL_RATIO" in d_closed.columns and pd.notna(a.VOL_RATIO)) else 0.0
    buy = float(a.BUY_PRESSURE) if ("BUY_PRESSURE" in d_closed.columns and pd.notna(a.BUY_PRESSURE)) else 0.5
    sell = 1.0 - buy
    r_val = float(a.RSI) if ("RSI" in d_closed.columns and pd.notna(a.RSI)) else 50.0
    imb = float((ob or {}).get("imbalance", 0.0) or 0.0)
    spread = float((ob or {}).get("spread", 0.0) or 0.0)

    act = 40.0
    act += min(qv / 10_000_000.0, 2.5) * 10.0
    if vr >= 3.0:
        act += 20.0
    elif vr >= 2.0:
        act += 14.0
    elif vr >= 1.5:
        act += 8.0

    if abs_chg >= 3.0:
        act += 15.0
    elif abs_chg >= 1.0:
        act += 8.0

    if spread > 0.35:
        act -= 15.0

    activity_score = int(max(0, min(100, round(act))))

    l_score = 50.0
    why_long: List[str] = []
    if chg >= 3.0:
        l_score += 12.0
        why_long.append("harga naik kuat")
    elif chg >= 1.0:
        l_score += 6.0
        why_long.append("momentum positif")
    elif chg <= -3.0:
        l_score -= 12.0
    elif chg <= -1.0:
        l_score -= 6.0

    if vr >= 3.0:
        l_score += 18.0
        why_long.append("volume sangat tinggi")
    elif vr >= 2.0:
        l_score += 12.0
        why_long.append("volume meningkat")
    elif vr >= 1.5:
        l_score += 6.0

    if buy >= 0.60:
        l_score += 12.0
        why_long.append("taker-buy dominan")
    elif buy >= 0.55:
        l_score += 6.0
    elif buy <= 0.40:
        l_score -= 12.0

    if imb >= 0.20:
        l_score += 8.0
        why_long.append("bid depth lebih tebal")
    elif imb <= -0.20:
        l_score -= 8.0

    if 50.0 <= r_val <= 68.0:
        l_score += 6.0
        why_long.append("RSI zona bullish sehat")
    elif r_val > 78.0:
        l_score -= 8.0
        why_long.append("RSI overbought")

    s_score = 50.0
    why_short: List[str] = []
    if chg <= -3.0:
        s_score += 12.0
        why_short.append("harga turun kuat")
    elif chg <= -1.0:
        s_score += 6.0
        why_short.append("momentum negatif")
    elif chg >= 3.0:
        s_score -= 12.0
    elif chg >= 1.0:
        s_score -= 6.0

    if vr >= 3.0:
        s_score += 18.0
        why_short.append("volume sangat tinggi")
    elif vr >= 2.0:
        s_score += 12.0
        why_short.append("volume meningkat")
    elif vr >= 1.5:
        s_score += 6.0

    if sell >= 0.60:
        s_score += 12.0
        why_short.append("taker-sell dominan")
    elif sell >= 0.55:
        s_score += 6.0
    elif sell <= 0.40:
        s_score -= 12.0

    if imb <= -0.20:
        s_score += 8.0
        why_short.append("ask depth lebih tebal")
    elif imb >= 0.20:
        s_score -= 8.0

    if 32.0 <= r_val <= 50.0:
        s_score += 6.0
        why_short.append("RSI zona bearish sehat")
    elif r_val < 22.0:
        s_score -= 8.0
        why_short.append("RSI oversold")

    long_score = int(max(0, min(100, round(l_score))))
    short_score = int(max(0, min(100, round(s_score))))

    if long_score > short_score:
        direction_hint = "LONG"
        best_score = long_score
        best_why = why_long
    elif short_score > long_score:
        direction_hint = "SHORT"
        best_score = short_score
        best_why = why_short
    else:
        direction_hint = "NEUTRAL"
        best_score = long_score
        best_why = why_long or why_short

    return {
        "symbol": symbol,
        "score": best_score,
        "activity_score": activity_score,
        "long_score": long_score,
        "short_score": short_score,
        "legacy_bull_score": long_score,
        "direction_hint": direction_hint,
        "why": best_why,
        "why_long": why_long,
        "why_short": why_short,
    }


def filter_liquid_universe(
    tickers: List[Dict[str, Any]],
    min_quote_volume: float = 2_000_000.0,
) -> Tuple[List[Tuple[str, float, float, float]], int]:
    candidates: List[Tuple[str, float, float, float]] = []
    total = len(tickers or [])
    for t in tickers or []:
        if not isinstance(t, dict):
            continue
        symbol = str(t.get("symbol", "") or "").upper().strip()
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_BASE_ASSETS:
            continue
        try:
            qvol = float(t.get("quoteVolume", 0.0) or 0.0)
            change = float(t.get("priceChangePercent", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if not is_finite_number(qvol) or not is_finite_number(change) or qvol < float(min_quote_volume):
            continue

        activity_rank = min(qvol / 20_000_000.0, 5.0) * 10.0 + min(abs(change), 10.0) * 2.0
        candidates.append((symbol, change, qvol, round(activity_rank, 4)))

    candidates.sort(key=lambda x: (x[3], x[2]), reverse=True)
    return candidates, total


def scan_markets_service(
    n: int = 60,
    *,
    tickers_fetcher: Callable[[], List[Dict[str, Any]]],
    kline_fetcher: Callable[[str, str, int], pd.DataFrame],
    order_book_fetcher: Callable[[str], Dict[str, Any]],
    cache: Optional[MarketSnapshotCache] = None,
    budget: Optional[BinanceRequestBudget] = None,
    as_of: Any = None,
    use_cache: bool = True,
    kline_limit: int = 260,
    min_quote_volume: float = 2_000_000.0,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    active_cache = cache if cache is not None else GLOBAL_MARKET_CACHE
    if budget is None:
        GLOBAL_REQUEST_BUDGET.reset_cycle(preserve_cooldown=True)
        active_budget = GLOBAL_REQUEST_BUDGET
    else:
        active_budget = budget
    now_ms = normalize_as_of_ms(as_of)

    failed_symbols: List[Dict[str, Any]] = []
    rate_limited = False
    retry_after_sec = 0.0

    tickers: Optional[List[Dict[str, Any]]] = None
    if use_cache and active_cache is not None:
        tickers = active_cache.get_tickers_24h(now_ms=now_ms)
    if tickers is None:
        ok_b, reason_b = active_budget.check_and_reserve("/api/v3/ticker/24hr", now_ms=now_ms)
        if not ok_b:
            rep = ScanDiagnosticsReport(
                total_tickers=0,
                liquid_universe_count=0,
                target_scan_count=0,
                succeeded_count=0,
                failed_count=1,
                skipped_count=0,
                failed_symbols=({"symbol": "ALL_TICKERS", "stage": "ticker_24hr", "error_code": reason_b.split(":")[0], "message": reason_b},),
                cache_stats=active_cache.stats() if active_cache else {},
                budget_stats=active_budget.snapshot(now_ms=now_ms),
                rate_limited="RATE_LIMIT" in reason_b,
                retry_after_seconds=active_budget.last_retry_after_seconds,
            )
            return {"candidates": [], "long_shortlist": [], "short_shortlist": [], "diagnostics": rep.to_dict()}
        try:
            tickers = tickers_fetcher()
            if use_cache and active_cache is not None and isinstance(tickers, list):
                active_cache.put_tickers_24h(tickers, fetched_at_ms=now_ms)
        except Exception as exc:
            if is_http_429_error(exc):
                rate_limited = True
                retry_after_sec = extract_retry_after_seconds(exc)
                active_budget.record_rate_limit(retry_after_sec, now_ms=now_ms)
            raise

    liquid_pool, total_tickers = filter_liquid_universe(tickers, min_quote_volume=min_quote_volume)
    selected_pool = liquid_pool[: max(int(n), 0)]
    target_count = len(selected_pool)

    out: List[Dict[str, Any]] = []
    skipped_count = 0

    for i, (symbol, change, qvol, activity_rank) in enumerate(selected_pool):
        if progress_callback is not None:
            progress_callback(i + 1, target_count, symbol)

        try:
            snap_15m: Optional[TimeframeSnapshot] = None
            if use_cache and active_cache is not None:
                snap_15m = active_cache.get_kline_snapshot(symbol, "15m", kline_limit, now_ms=now_ms)

            if snap_15m is None:
                ok_k, reason_k = active_budget.check_and_reserve("/api/v3/klines", now_ms=now_ms)
                if not ok_k:
                    skipped_count += 1
                    failed_symbols.append({
                        "symbol": symbol,
                        "stage": "klines_15m",
                        "error_code": reason_k.split(":")[0],
                        "message": reason_k,
                    })
                    continue
                raw_df = kline_fetcher(symbol, "15m", kline_limit)
                snap_15m = build_timeframe_snapshot(raw_df, symbol=symbol, interval="15m", as_of=now_ms, limit=kline_limit)
                if use_cache and active_cache is not None and snap_15m.validation.valid:
                    active_cache.put_kline_snapshot(snap_15m, limit=kline_limit, fetched_at_ms=now_ms)

            d_closed = snap_15m.copy_closed_df()
            if len(d_closed) == 0:
                failed_symbols.append({
                    "symbol": symbol,
                    "stage": "klines_15m",
                    "error_code": "INSUFFICIENT_CLOSED_BARS",
                    "message": "Tidak ada closed bar yang valid untuk simbol ini.",
                })
                continue

            ob: Optional[Dict[str, Any]] = None
            if use_cache and active_cache is not None:
                ob = active_cache.get_order_book(symbol, limit=20, now_ms=now_ms)
            if ob is None:
                ok_ob, reason_ob = active_budget.check_and_reserve("/api/v3/depth", now_ms=now_ms)
                if not ok_ob:
                    skipped_count += 1
                    failed_symbols.append({
                        "symbol": symbol,
                        "stage": "order_book",
                        "error_code": reason_ob.split(":")[0],
                        "message": reason_ob,
                    })
                    continue
                ob = order_book_fetcher(symbol)
                if use_cache and active_cache is not None and isinstance(ob, dict):
                    active_cache.put_order_book(symbol, ob, limit=20, fetched_at_ms=now_ms)

            bi = scanner_score_bidirectional(symbol, change, qvol, d_closed, ob)
            a = d_closed.iloc[-1]

            out.append({
                "symbol": symbol,
                "change": change,
                "qvol": qvol,
                "vr": float(a.VOL_RATIO),
                "buy": float(a.BUY_PRESSURE),
                "imb": float(ob["imbalance"]),
                "spread": float(ob["spread"]),
                "rsi": float(a.RSI),
                "score": bi["score"],
                "activity_score": bi["activity_score"],
                "long_score": bi["long_score"],
                "short_score": bi["short_score"],
                "direction_hint": bi["direction_hint"],
                "why": bi["why"],
                "why_long": bi["why_long"],
                "why_short": bi["why_short"],
                "bar_time": snap_15m.watermark.bar_time_iso,
                "from_cache": bool(snap_15m.from_cache),
            })

        except Exception as exc:
            err_code = "HTTP_429_RATE_LIMIT" if is_http_429_error(exc) else "SYMBOL_FETCH_FAILED"
            if is_http_429_error(exc):
                rate_limited = True
                retry_after_sec = extract_retry_after_seconds(exc)
                active_budget.record_rate_limit(retry_after_sec, now_ms=now_ms)
            failed_symbols.append({
                "symbol": symbol,
                "stage": "scan_symbol",
                "error_code": err_code,
                "message": str(exc)[:240],
            })

    long_shortlist = sorted(
        [x for x in out if x["long_score"] >= x["short_score"]],
        key=lambda x: (x["long_score"], x["activity_score"], x["qvol"]),
        reverse=True,
    )
    short_shortlist = sorted(
        [x for x in out if x["short_score"] >= x["long_score"]],
        key=lambda x: (x["short_score"], x["activity_score"], x["qvol"]),
        reverse=True,
    )
    combined = sorted(
        out,
        key=lambda x: (x["score"], x["activity_score"], x["qvol"]),
        reverse=True,
    )

    rep = ScanDiagnosticsReport(
        total_tickers=total_tickers,
        liquid_universe_count=len(liquid_pool),
        target_scan_count=target_count,
        succeeded_count=len(out),
        failed_count=len(failed_symbols),
        skipped_count=skipped_count,
        failed_symbols=tuple(failed_symbols),
        cache_stats=active_cache.stats() if active_cache else {},
        budget_stats=active_budget.snapshot(now_ms=now_ms),
        rate_limited=rate_limited or active_budget.snapshot(now_ms=now_ms)["in_cooldown"],
        retry_after_seconds=retry_after_sec or active_budget.last_retry_after_seconds,
    )

    return {
        "candidates": combined,
        "long_shortlist": long_shortlist,
        "short_shortlist": short_shortlist,
        "diagnostics": rep.to_dict(),
    }
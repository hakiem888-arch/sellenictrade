"""
Freqtrade REST API Client for Streamlit Momentum Radar v5.5 Integration.

Communicates with the official Freqtrade REST API (`/api/v1/*` as defined in
`freqtrade/rpc/api_server/*` and `ft_client/freqtrade_client/ft_rest_client.py`).
Supports HTTP Basic Auth and JWT Bearer tokens (`/api/v1/token/login` & `/api/v1/token/refresh`),
automatic timeout handling, health checks, and secret redaction.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, Timeout

from .config_service import sanitize_log_text

logger = logging.getLogger("freqtrade_client")


@dataclass
class ApiResponse:
    """Standardized response envelope for Freqtrade REST API calls."""

    ok: bool
    data: Any = None
    error: str | None = None
    status_code: int = 0
    latency_ms: float = 0.0
    endpoint: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "status_code": self.status_code,
            "latency_ms": round(self.latency_ms, 2),
            "endpoint": self.endpoint,
            "timestamp": self.timestamp,
        }


class FreqtradeClient:
    """
    Official-compatible Freqtrade REST API Client with JWT & HTTP Basic Auth,
    connection health diagnostics, and safe error handling.
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8080",
        username: str | None = "freqtrader",
        password: str | None = None,
        *,
        timeout: float = 10.0,
        pool_connections: int = 10,
        pool_maxsize: int = 10,
    ):
        self.server_url = str(server_url or "http://127.0.0.1:8080").rstrip("/")
        self.username = username
        self._password = password
        self.timeout = float(timeout)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self.last_error: str | None = None
        self.last_check_at: str | None = None

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=pool_connections, pool_maxsize=pool_maxsize)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def _build_url(self, apipath: str, params: dict[str, Any] | None = None) -> str:
        clean_path = str(apipath).lstrip("/")
        basepath = f"{self.server_url}/api/v1/{clean_path}"
        schema, netloc, path, par, query, fragment = urlparse(basepath)
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            query = urlencode(filtered, doseq=True)
        return urlunparse((schema, netloc, path, par, query, fragment))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def _request_auth(self) -> tuple[str, str] | None:
        if self._access_token:
            return None
        if self.username and self._password:
            return (self.username, self._password)
        return None

    def login_jwt(self) -> ApiResponse:
        """Obtain JWT access_token and refresh_token via POST /api/v1/token/login."""
        if not self.username or not self._password:
            return ApiResponse(
                ok=False,
                error="Username atau password REST API Freqtrade belum dikonfigurasi.",
                status_code=401,
                endpoint="token/login",
            )
        t0 = time.monotonic()
        url = self._build_url("token/login")
        try:
            resp = self._session.post(
                url,
                auth=(self.username, self._password),
                timeout=self.timeout,
            )
            latency = (time.monotonic() - t0) * 1000.0
            if resp.status_code == 200:
                payload = resp.json()
                self._access_token = payload.get("access_token")
                self._refresh_token = payload.get("refresh_token")
                return ApiResponse(
                    ok=True,
                    data={"authenticated": True, "user": self.username},
                    status_code=200,
                    latency_ms=latency,
                    endpoint="token/login",
                )
            err_msg = sanitize_log_text(resp.text, [self._password or ""])
            self.last_error = f"HTTP {resp.status_code}: {err_msg}"
            return ApiResponse(
                ok=False,
                error=self.last_error,
                status_code=resp.status_code,
                latency_ms=latency,
                endpoint="token/login",
            )
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000.0
            self.last_error = sanitize_log_text(str(exc), [self._password or ""])
            return ApiResponse(
                ok=False,
                error=self.last_error,
                status_code=0,
                latency_ms=latency,
                endpoint="token/login",
            )

    def refresh_jwt(self) -> ApiResponse:
        """Refresh JWT access_token via POST /api/v1/token/refresh."""
        if not self._refresh_token:
            return self.login_jwt()
        t0 = time.monotonic()
        url = self._build_url("token/refresh")
        try:
            resp = self._session.post(
                url,
                headers={"Authorization": f"Bearer {self._refresh_token}"},
                auth=None,
                timeout=self.timeout,
            )
            latency = (time.monotonic() - t0) * 1000.0
            if resp.status_code == 200:
                payload = resp.json()
                self._access_token = payload.get("access_token")
                return ApiResponse(
                    ok=True,
                    data={"refreshed": True},
                    status_code=200,
                    latency_ms=latency,
                    endpoint="token/refresh",
                )
            return self.login_jwt()
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000.0
            self.last_error = sanitize_log_text(str(exc), [self._password or ""])
            return ApiResponse(
                ok=False,
                error=self.last_error,
                status_code=0,
                latency_ms=latency,
                endpoint="token/refresh",
            )

    def _call(
        self,
        method: str,
        apipath: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
    ) -> ApiResponse:
        method_up = str(method).upper()
        if method_up not in ("GET", "POST", "PUT", "DELETE"):
            raise ValueError(f"Metode HTTP tidak valid: <{method}>")

        url = self._build_url(apipath, params=params)
        t0 = time.monotonic()
        self.last_check_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        try:
            body = json.dumps(data) if data is not None else None
            resp = self._session.request(
                method_up,
                url,
                headers=self._headers(),
                auth=self._request_auth(),
                timeout=self.timeout,
                data=body,
            )
            latency = (time.monotonic() - t0) * 1000.0

            if resp.status_code == 401 and self._access_token:
                self._access_token = None
                resp = self._session.request(
                    method_up,
                    url,
                    headers=self._headers(),
                    auth=self._request_auth(),
                    timeout=self.timeout,
                    data=body,
                )

            try:
                parsed = resp.json()
            except Exception:
                parsed = resp.text

            if 200 <= resp.status_code < 300:
                self.last_error = None
                return ApiResponse(
                    ok=True,
                    data=parsed,
                    status_code=resp.status_code,
                    latency_ms=latency,
                    endpoint=apipath,
                )

            detail = parsed.get("detail") if isinstance(parsed, dict) else str(parsed)
            err_msg = sanitize_log_text(
                f"HTTP {resp.status_code}: {detail}", [self._password or ""]
            )
            self.last_error = err_msg
            return ApiResponse(
                ok=False,
                data=parsed if isinstance(parsed, dict) else None,
                error=err_msg,
                status_code=resp.status_code,
                latency_ms=latency,
                endpoint=apipath,
            )

        except Timeout as exc:
            latency = (time.monotonic() - t0) * 1000.0
            err = f"Timeout koneksi ke Freqtrade REST API ({self.server_url}) setelah {self.timeout:.1f}s."
            self.last_error = sanitize_log_text(f"{err} ({exc})", [self._password or ""])
            return ApiResponse(
                ok=False,
                error=self.last_error,
                status_code=0,
                latency_ms=latency,
                endpoint=apipath,
            )
        except RequestsConnectionError as exc:
            latency = (time.monotonic() - t0) * 1000.0
            err = f"Tidak dapat terhubung ke Freqtrade REST API di {self.server_url}. Pastikan proses Freqtrade / webserver sedang berjalan."
            self.last_error = sanitize_log_text(f"{err} ({exc})", [self._password or ""])
            return ApiResponse(
                ok=False,
                error=self.last_error,
                status_code=0,
                latency_ms=latency,
                endpoint=apipath,
            )
        except RequestException as exc:
            latency = (time.monotonic() - t0) * 1000.0
            self.last_error = sanitize_log_text(f"Kesalahan HTTP: {exc}", [self._password or ""])
            return ApiResponse(
                ok=False,
                error=self.last_error,
                status_code=0,
                latency_ms=latency,
                endpoint=apipath,
            )

    def _get(self, apipath: str, params: dict[str, Any] | None = None) -> ApiResponse:
        return self._call("GET", apipath, params=params)

    def _post(
        self, apipath: str, params: dict[str, Any] | None = None, data: Any = None
    ) -> ApiResponse:
        return self._call("POST", apipath, params=params, data=data)

    def _delete(self, apipath: str, params: dict[str, Any] | None = None) -> ApiResponse:
        return self._call("DELETE", apipath, params=params)

    # Info & Health Endpoints
    def ping(self) -> ApiResponse:
        res = self._get("ping")
        if res.ok and isinstance(res.data, dict) and res.data.get("status") == "pong":
            return res
        cfg = self.show_config()
        if cfg.ok and isinstance(cfg.data, dict):
            return ApiResponse(
                ok=True,
                data={"status": "pong", "state": cfg.data.get("state", "unknown")},
                status_code=cfg.status_code,
                latency_ms=cfg.latency_ms,
                endpoint="ping",
            )
        return res

    def version(self) -> ApiResponse:
        return self._get("version")

    def show_config(self) -> ApiResponse:
        return self._get("show_config")

    def health(self) -> ApiResponse:
        return self._get("health")

    def sysinfo(self) -> ApiResponse:
        return self._get("sysinfo")

    def logs(self, limit: int | None = 100) -> ApiResponse:
        return self._get("logs", params={"limit": limit} if limit else None)

    def get_connection_summary(self) -> dict[str, Any]:
        ping_res = self.ping()
        if not ping_res.ok:
            return {
                "connected": False,
                "bot_state": "OFFLINE",
                "dry_run": True,
                "strategy": "-",
                "version": "-",
                "latency_ms": round(ping_res.latency_ms, 1),
                "last_check_at": self.last_check_at,
                "last_error": self.last_error or ping_res.error,
                "server_url": self.server_url,
            }

        cfg_res = self.show_config()
        cfg_data = cfg_res.data if (cfg_res.ok and isinstance(cfg_res.data, dict)) else {}
        health_res = self.health()
        health_data = health_res.data if (health_res.ok and isinstance(health_res.data, dict)) else {}

        return {
            "connected": True,
            "bot_state": str(cfg_data.get("state", "running")).upper(),
            "runmode": str(cfg_data.get("runmode", "dry_run")),
            "dry_run": bool(cfg_data.get("dry_run", True)),
            "strategy": cfg_data.get("strategy") or "-",
            "timeframe": cfg_data.get("timeframe") or "-",
            "exchange": cfg_data.get("exchange") or "-",
            "version": cfg_data.get("version") or cfg_data.get("strategy_version") or "-",
            "last_process": health_data.get("last_process"),
            "latency_ms": round(ping_res.latency_ms, 1),
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "server_url": self.server_url,
        }

    # Bot Control Endpoints
    def start(self) -> ApiResponse:
        return self._post("start")

    def stop(self) -> ApiResponse:
        return self._post("stop")

    def pause(self) -> ApiResponse:
        return self._post("pause")

    def stopbuy(self) -> ApiResponse:
        return self._post("stopbuy")

    def reload_config(self) -> ApiResponse:
        return self._post("reload_config")

    # Trading, Balance & Performance Endpoints
    def balance(self) -> ApiResponse:
        return self._get("balance")

    def count(self) -> ApiResponse:
        return self._get("count")

    def profit(self) -> ApiResponse:
        return self._get("profit")

    def profit_all(self) -> ApiResponse:
        return self._get("profit_all")

    def performance(self) -> ApiResponse:
        return self._get("performance")

    def stats(self) -> ApiResponse:
        return self._get("stats")

    def daily(self, days: int = 7) -> ApiResponse:
        return self._get("daily", params={"timescale": days})

    def weekly(self, weeks: int = 4) -> ApiResponse:
        return self._get("weekly", params={"timescale": weeks})

    def monthly(self, months: int = 3) -> ApiResponse:
        return self._get("monthly", params={"timescale": months})

    def status(self) -> ApiResponse:
        return self._get("status")

    def trades(
        self, limit: int = 500, offset: int = 0, order_by_id: bool = True
    ) -> ApiResponse:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if not order_by_id:
            params["order_by_id"] = False
        return self._get("trades", params=params)

    def trade(self, trade_id: int) -> ApiResponse:
        return self._get(f"trade/{int(trade_id)}")

    def delete_trade(self, trade_id: int) -> ApiResponse:
        return self._delete(f"trades/{int(trade_id)}")

    def cancel_open_order(self, trade_id: int) -> ApiResponse:
        return self._delete(f"trades/{int(trade_id)}/open-order")

    def forceenter(
        self,
        pair: str,
        side: str = "long",
        price: float | None = None,
        *,
        order_type: str | None = None,
        stake_amount: float | None = None,
        leverage: float | None = None,
        enter_tag: str | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {"pair": pair, "side": side}
        if price is not None:
            payload["price"] = price
        if order_type:
            payload["ordertype"] = order_type
        if stake_amount is not None:
            payload["stakeamount"] = stake_amount
        if leverage is not None:
            payload["leverage"] = leverage
        if enter_tag:
            payload["entry_tag"] = enter_tag
        return self._post("forceenter", data=payload)

    def forceexit(
        self, trade_id: int | str, ordertype: str | None = None, amount: float | None = None
    ) -> ApiResponse:
        payload: dict[str, Any] = {"tradeid": str(trade_id)}
        if ordertype:
            payload["ordertype"] = ordertype
        if amount is not None:
            payload["amount"] = amount
        return self._post("forceexit", data=payload)

    def whitelist(self) -> ApiResponse:
        return self._get("whitelist")

    def blacklist(self, add_pairs: list[str] | None = None) -> ApiResponse:
        if not add_pairs:
            return self._get("blacklist")
        return self._post("blacklist", data={"blacklist": add_pairs})

    def locks(self) -> ApiResponse:
        return self._get("locks")

    def delete_lock(self, lock_id: int) -> ApiResponse:
        return self._delete(f"locks/{int(lock_id)}")

    def entries(self, pair: str | None = None) -> ApiResponse:
        return self._get("entries", params={"pair": pair} if pair else None)

    def exits(self, pair: str | None = None) -> ApiResponse:
        return self._get("exits", params={"pair": pair} if pair else None)

    def strategies(self) -> ApiResponse:
        return self._get("strategies")

    def strategy(self, strategy_name: str) -> ApiResponse:
        return self._get(f"strategy/{strategy_name}")

    def available_pairs(
        self, timeframe: str | None = None, stake_currency: str | None = None
    ) -> ApiResponse:
        return self._get(
            "available_pairs",
            params={
                "timeframe": timeframe or "",
                "stake_currency": stake_currency or "",
            },
        )

    def pair_candles(
        self, pair: str, timeframe: str, limit: int | None = None
    ) -> ApiResponse:
        params: dict[str, Any] = {"pair": pair, "timeframe": timeframe}
        if limit:
            params["limit"] = limit
        return self._get("pair_candles", params=params)

    def pair_history(
        self,
        pair: str,
        timeframe: str,
        strategy: str,
        timerange: str | None = None,
    ) -> ApiResponse:
        return self._get(
            "pair_history",
            params={
                "pair": pair,
                "timeframe": timeframe,
                "strategy": strategy,
                "timerange": timerange or "",
            },
        )

    def start_backtest(self, bt_settings: dict[str, Any]) -> ApiResponse:
        return self._post("backtest", data=bt_settings)

    def get_backtest(self) -> ApiResponse:
        return self._get("backtest")

    def abort_backtest(self) -> ApiResponse:
        return self._get("backtest/abort")

    def delete_backtest(self) -> ApiResponse:
        return self._delete("backtest")

    def backtest_history(self) -> ApiResponse:
        return self._get("backtest/history")

    def download_data(self, payload: dict[str, Any]) -> ApiResponse:
        return self._post("download_data", data=payload)

    def background_jobs(self) -> ApiResponse:
        return self._get("background")

    def background_job(self, job_id: str) -> ApiResponse:
        return self._get(f"background/{job_id}")

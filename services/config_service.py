"""
Configuration, validation, database separation, and security guard service.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False


class LiveTradingSecurityError(PermissionError):
    """Raised when attempting to enable live trading without passing all safety gates."""


SECRET_KEY_PATTERNS = re.compile(
    r"(api[_-]?key|secret|password|token|jwt|authorization|groq|gemini|telegram|private[_-]?key)",
    re.IGNORECASE,
)

RAW_TOKEN_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{25,}\b"),  # Telegram bot token
]

PLACEHOLDER_SECRETS = {
    "",
    "your_exchange_api_key",
    "your_exchange_secret",
    "your_api_key",
    "your_secret",
    "changeme",
    "password",
    "supersecurepassword",
    "somethingrandomsomethingrandom123",
    "secret_ws_t0ken.",
}

ALLOWED_LOCAL_HOSTS = {
    "127.0.0.1",
    "localhost",
    "0.0.0.0",  # Inside container when bound to 127.0.0.1 on host port mapping
    "freqtrade",  # Docker Compose internal service name
}


def mask_secret_value(value: Any) -> str:
    """Mask a secret string so it never leaks in logs or UI."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "****"
    return f"{s[:3]}...{s[-3:]}"


def sanitize_log_text(text: str, extra_secrets: list[str] | None = None) -> str:
    """Remove sensitive tokens, keys, passwords, and JWTs from log or error strings."""
    if not text:
        return ""
    cleaned = str(text)
    for pat in RAW_TOKEN_PATTERNS:
        cleaned = pat.sub("[REDACTED_SECRET]", cleaned)

    # Redact JSON/key-value style secrets in logs
    cleaned = re.sub(
        r'("?(?:api_key|secret|password|jwt_secret_key|ws_token|token|Authorization)"?\s*[:=]\s*["\'])([^"\']+)(["\'])',
        r"\1[REDACTED_SECRET]\3",
        cleaned,
        flags=re.IGNORECASE,
    )

    if extra_secrets:
        for sec in extra_secrets:
            if sec and len(str(sec).strip()) >= 6:
                cleaned = cleaned.replace(str(sec).strip(), "[REDACTED_SECRET]")
    return cleaned


def redact_config_dict(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a configuration dict with all sensitive values masked."""
    if not isinstance(cfg, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out[k] = redact_config_dict(v)
        elif isinstance(v, list):
            out[k] = [redact_config_dict(item) if isinstance(item, dict) else item for item in v]
        elif SECRET_KEY_PATTERNS.search(str(k)):
            out[k] = mask_secret_value(v) if v else ""
        else:
            out[k] = v
    return out


class ConfigService:
    """Manages configuration, validation, DB separation, and safety controls."""

    LIVE_CONFIRMATION_PHRASE = "SAYA PAHAM RISIKO LIVE TRADING"

    def __init__(self, project_root: str | Path | None = None, env_file: str | Path | None = None):
        self.project_root = Path(project_root or os.getcwd()).resolve()
        self.env_file = Path(env_file) if env_file else self.project_root / ".env"
        if self.env_file.is_file():
            load_dotenv(self.env_file, override=False)

        self.user_data_dir = Path(
            os.getenv("FREQTRADE_USER_DATA_DIR", str(self.project_root / "user_data"))
        ).resolve()
        self.config_path = Path(
            os.getenv("FREQTRADE_CONFIG_PATH", str(self.user_data_dir / "config.json"))
        ).resolve()
        self.legacy_db_path = Path(
            os.getenv("AI_CONSENSUS_DB_PATH", str(self.project_root / "signals.db"))
        ).resolve()
        self.dryrun_db_path = Path(
            os.getenv(
                "FREQTRADE_DRYRUN_DB_PATH",
                str(self.user_data_dir / "tradesv3.dryrun.sqlite"),
            )
        ).resolve()
        self.live_db_path = Path(
            os.getenv(
                "FREQTRADE_LIVE_DB_PATH",
                str(self.user_data_dir / "tradesv3.sqlite"),
            )
        ).resolve()

        self.ensure_directories()

    def ensure_directories(self) -> None:
        """Ensure required user_data directories exist."""
        for sub in ("strategies", "data", "data/binance", "backtest_results", "hyperopt_results", "logs", "backups"):
            (self.user_data_dir / sub).mkdir(parents=True, exist_ok=True)

    def get_api_connection_Params(self) -> dict[str, Any]:
        """Return REST API connection settings from environment or Freqtrade config.json."""
        cfg = self.load_freqtrade_config(create_if_missing=True)
        api_cfg = cfg.get("api_server", {}) if isinstance(cfg, dict) else {}

        host = os.getenv("FREQTRADE_API_HOST", api_cfg.get("listen_ip_address", "127.0.0.1"))
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = int(os.getenv("FREQTRADE_API_PORT", api_cfg.get("listen_port", 8080)))
        base_url = os.getenv("FREQTRADE_API_URL", f"http://{host}:{port}").rstrip("/")
        username = os.getenv("FREQTRADE_API_USERNAME", api_cfg.get("username", "freqtrader"))
        password = os.getenv("FREQTRADE_API_PASSWORD", api_cfg.get("password", ""))
        jwt_secret = os.getenv("FREQTRADE_JWT_SECRET", api_cfg.get("jwt_secret_key", ""))
        timeout = float(os.getenv("FREQTRADE_API_TIMEOUT", "10.0"))

        return {
            "server_url": base_url,
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "jwt_secret_key": jwt_secret,
            "timeout": timeout,
        }

    def is_live_trading_env_enabled(self) -> bool:
        """Check whether ENABLE_LIVE_TRADING is explicitly set to true in environment."""
        val = os.getenv("ENABLE_LIVE_TRADING", "false").strip().lower()
        return val == "true"

    def validate_database_separation(self) -> tuple[bool, str]:
        """Ensure legacy paper trading SQLite DB and Freqtrade SQLite DBs are strictly separate."""
        legacy = self.legacy_db_path.resolve()
        dryrun = self.dryrun_db_path.resolve()
        live = self.live_db_path.resolve()

        if legacy == dryrun or legacy == live:
            return (
                False,
                f"Konflik database terdeteksi: Database aplikasi lama ({legacy.name}) tidak boleh "
                f"sama dengan database Freqtrade ({dryrun.name}).",
            )
        if dryrun == live:
            return (
                False,
                f"Konflik database terdeteksi: Database dry-run ({dryrun.name}) dan live ({live.name}) "
                "harus dipisahkan.",
            )
        return True, "Database aplikasi lama (signals.db) dan Freqtrade (tradesv3) terpisah dengan aman."

    def build_default_freqtrade_config(self) -> dict[str, Any]:
        """Build a safe, valid default Freqtrade configuration with dry_run=True."""
        api_user = os.getenv("FREQTRADE_API_USERNAME", "freqtrader")
        api_pass = os.getenv("FREQTRADE_API_PASSWORD", "MomentumRadar_FT_Local_2026!")
        jwt_secret = os.getenv("FREQTRADE_JWT_SECRET", "MomentumRadar_JWT_Secret_Key_2026_SafeLocal")
        ws_token = os.getenv("FREQTRADE_WS_TOKEN", "MomentumRadar_WS_Token_2026")

        return {
            "$schema": "https://schema.freqtrade.io/schema.json",
            "bot_name": "momentum_radar_freqtrade",
            "max_open_trades": 3,
            "stake_currency": "USDT",
            "stake_amount": 100,
            "tradable_balance_ratio": 0.99,
            "fiat_display_currency": "USD",
            "dry_run": True,
            "dry_run_wallet": 1000,
            "cancel_open_orders_on_exit": False,
            "trading_mode": "spot",
            "margin_mode": "",
            "timeframe": "5m",
            "minimal_roi": {
                "60": 0.01,
                "30": 0.02,
                "0": 0.04,
            },
            "stoploss": -0.10,
            "trailing_stop": False,
            "use_exit_signal": True,
            "exit_profit_only": False,
            "ignore_roi_if_entry_signal": False,
            "unfilledtimeout": {
                "entry": 10,
                "exit": 10,
                "exit_timeout_count": 0,
                "unit": "minutes",
            },
            "entry_pricing": {
                "price_side": "same",
                "use_order_book": True,
                "order_book_top": 1,
                "price_last_balance": 0.0,
                "check_depth_of_market": {
                    "enabled": False,
                    "bids_to_ask_delta": 1,
                },
            },
            "exit_pricing": {
                "price_side": "same",
                "use_order_book": True,
                "order_book_top": 1,
            },
            "exchange": {
                "name": "binance",
                "key": os.getenv("EXCHANGE_API_KEY", ""),
                "secret": os.getenv("EXCHANGE_API_SECRET", ""),
                "ccxt_config": {},
                "ccxt_async_config": {},
                "pair_whitelist": [
                    "BTC/USDT",
                    "ETH/USDT",
                    "SOL/USDT",
                    "BNB/USDT",
                    "XRP/USDT",
                    "ADA/USDT",
                ],
                "pair_blacklist": [
                    "BNB/.*",
                ],
            },
            "pairlists": [
                {"method": "StaticPairList"},
            ],
            "telegram": {
                "enabled": False,
                "token": "",
                "chat_id": "",
            },
            "api_server": {
                "enabled": True,
                "listen_ip_address": "127.0.0.1",
                "listen_port": 8080,
                "verbosity": "error",
                "enable_openapi": False,
                "jwt_secret_key": jwt_secret,
                "CORS_origins": ["http://localhost:8501", "http://127.0.0.1:8501"],
                "username": api_user,
                "password": api_pass,
                "ws_token": ws_token,
            },
            "db_url": f"sqlite:///{self.dryrun_db_path.as_posix()}",
            "initial_state": "running",
            "force_entry_enable": False,
            "internals": {
                "process_throttle_secs": 5,
            },
            "strategy": "SampleStrategy",
            "strategy_path": str(self.user_data_dir / "strategies"),
            "user_data_dir": str(self.user_data_dir),
            "datadir": str(self.user_data_dir / "data" / "binance"),
            "dataformat_ohlcv": "feather",
            "dataformat_trades": "feather",
        }

    def load_freqtrade_config(self, create_if_missing: bool = True) -> dict[str, Any]:
        """Load Freqtrade configuration JSON, creating a safe default if missing."""
        if not self.config_path.is_file():
            default_cfg = self.build_default_freqtrade_config()
            if create_if_missing:
                self.save_freqtrade_config(default_cfg, allow_live=False)
            return default_cfg

        try:
            raw = self.config_path.read_text(encoding="utf-8")
            # Strip single-line // comments if user copied a config_examples file
            cleaned_lines = [
                line for line in raw.splitlines() if not line.strip().startswith("//")
            ]
            cfg = json.loads("\n".join(cleaned_lines))
            if not isinstance(cfg, dict):
                raise ValueError("Root JSON configuration must be an object.")
            return cfg
        except Exception as exc:
            raise ValueError(f"Gagal membaca konfigurasi Freqtrade ({self.config_path}): {exc}") from exc

    def validate_freqtrade_config(
        self,
        config: dict[str, Any],
        *,
        allow_live: bool = False,
        confirmation_phrase: str = "",
    ) -> tuple[bool, list[str], list[str]]:
        """
        Validate a Freqtrade config dict.
        Returns (is_valid, errors, warnings).
        """
        errors: list[str] = []
        warnings: list[str] = []

        if not isinstance(config, dict):
            return False, ["Konfigurasi harus berupa objek JSON (dict)."], []

        # 1. Required fields check
        required_keys = ["max_open_trades", "stake_currency", "stake_amount", "dry_run", "exchange"]
        for req in required_keys:
            if req not in config:
                errors.append(f"Field wajib '{req}' tidak ditemukan dalam konfigurasi.")

        # 2. Dry-run vs Live trading safety gate
        dry_run = bool(config.get("dry_run", True))
        if not dry_run:
            if not self.is_live_trading_env_enabled():
                errors.append(
                    "Live trading diblokir: Variabel lingkungan ENABLE_LIVE_TRADING belum diset ke 'true' "
                    "(default: ENABLE_LIVE_TRADING=false)."
                )
            if not allow_live:
                errors.append(
                    "Live trading diblokir: Mode live tidak diizinkan tanpa aktivasi eksplisit."
                )
            if confirmation_phrase.strip() != self.LIVE_CONFIRMATION_PHRASE:
                errors.append(
                    f"Live trading memerlukan frasa konfirmasi eksplisit: '{self.LIVE_CONFIRMATION_PHRASE}'."
                )

            exch = config.get("exchange", {}) if isinstance(config.get("exchange"), dict) else {}
            api_key = str(exch.get("key") or exch.get("api_key") or "").strip()
            api_secret = str(exch.get("secret") or "").strip()
            if api_key.lower() in PLACEHOLDER_SECRETS or len(api_key) < 10:
                errors.append("Live trading memerlukan Exchange API Key yang valid (bukan placeholder).")
            if api_secret.lower() in PLACEHOLDER_SECRETS or len(api_secret) < 10:
                errors.append("Live trading memerlukan Exchange API Secret yang valid (bukan placeholder).")

        # 3. Numeric sanity checks
        stake_amount = config.get("stake_amount")
        dry_wallet = config.get("dry_run_wallet", 1000)
        tradable_ratio = float(config.get("tradable_balance_ratio", 0.99) or 0.99)
        if isinstance(stake_amount, (int, float)):
            if stake_amount <= 0:
                errors.append("stake_amount harus lebih besar dari 0.")
            elif isinstance(dry_wallet, (int, float)) and stake_amount > (dry_wallet * tradable_ratio):
                errors.append(
                    f"stake_amount ({stake_amount}) melebihi kapasitas dompet yang dapat diperdagangkan "
                    f"({dry_wallet * tradable_ratio:.2f})."
                )
        elif stake_amount != "unlimited":
            errors.append("stake_amount harus berupa angka positif atau 'unlimited'.")

        max_open = config.get("max_open_trades")
        if not isinstance(max_open, (int, float)) or (max_open < -1):
            errors.append("max_open_trades harus berupa bilangan bulat >= 0 atau -1.")

        # 4. Exchange & Pairlist validation
        exch = config.get("exchange", {})
        if not isinstance(exch, dict) or not exch.get("name"):
            errors.append("Konfigurasi 'exchange.name' wajib diisi (contoh: 'binance').")
        else:
            whitelist = exch.get("pair_whitelist", [])
            if not isinstance(whitelist, list) or len(whitelist) == 0:
                warnings.append("pair_whitelist kosong; pastikan pairlist dinamis atau pasangan dipilih saat backtest.")
            else:
                for p in whitelist:
                    if not isinstance(p, str) or "/" not in p:
                        errors.append(
                            f"Format pasangan '{p}' tidak valid untuk Freqtrade. Gunakan format 'BASE/QUOTE' (contoh: 'BTC/USDT')."
                        )

        # 5. API Server security validation
        api_srv = config.get("api_server", {})
        if isinstance(api_srv, dict) and api_srv.get("enabled"):
            listen_ip = str(api_srv.get("listen_ip_address", "127.0.0.1")).strip()
            if listen_ip not in ALLOWED_LOCAL_HOSTS:
                errors.append(
                    f"Keamanan API: listen_ip_address '{listen_ip}' mengekspos REST API ke jaringan luar. "
                    "Gunakan '127.0.0.1' (localhost) atau jaringan internal Docker."
                )
            api_pass = str(api_srv.get("password", "")).strip()
            jwt_sec = str(api_srv.get("jwt_secret_key", "")).strip()
            if api_pass.lower() in PLACEHOLDER_SECRETS or len(api_pass) < 8:
                warnings.append("Password REST API masih menggunakan nilai default/lemah. Disarankan menggantinya di .env.")
            if jwt_sec.lower() in PLACEHOLDER_SECRETS or len(jwt_sec) < 12:
                warnings.append("jwt_secret_key REST API masih menggunakan nilai default/lemah.")

        # 6. Database separation check
        db_ok, db_msg = self.validate_database_separation()
        if not db_ok:
            errors.append(db_msg)

        return len(errors) == 0, errors, warnings

    def save_freqtrade_config(
        self,
        config: dict[str, Any],
        *,
        allow_live: bool = False,
        confirmation_phrase: str = "",
    ) -> Path:
        """Validate and save Freqtrade configuration, creating a backup of any previous config."""
        cfg_copy = copy.deepcopy(config)

        # Enforce dry_run=True unless all live trading safety gates pass
        if not cfg_copy.get("dry_run", True):
            is_valid, errors, _ = self.validate_freqtrade_config(
                cfg_copy, allow_live=allow_live, confirmation_phrase=confirmation_phrase
            )
            if not is_valid:
                raise LiveTradingSecurityError(" | ".join(errors))
        else:
            cfg_copy["dry_run"] = True
            is_valid, errors, _ = self.validate_freqtrade_config(cfg_copy, allow_live=False)
            if not is_valid:
                raise ValueError(" | ".join(errors))

        if self.config_path.is_file():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = self.user_data_dir / "backups" / f"config.backup.{ts}.json"
            shutil.copy2(self.config_path, backup_path)

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg_copy, indent=2), encoding="utf-8")
        return self.config_path

    def backup_legacy_database(self) -> Path | None:
        """Create a timestamped backup of the legacy signals.db database if it exists."""
        if not self.legacy_db_path.is_file():
            return None
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = self.user_data_dir / "backups" / f"signals.backup.{ts}.db"
        shutil.copy2(self.legacy_db_path, backup_file)
        return backup_file

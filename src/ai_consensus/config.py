"""
Validated application configuration, model tiers, risk/gate parameters,
deployment security policy, and secret masking/redaction (PH02-01..PH02-03 / F34, F35).
Strictly zero side-effects on import.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_BINANCE_URL: str = "https://data-api.binance.vision"
DEFAULT_DB_FILE: str = "trading_v5.db"
LEGACY_ACCOUNTING_MODE: str = "LEGACY_R_SNAPSHOT_ONLY"

STRATEGY_VERSION: str = "v5.6-ph02"
PROMPT_VERSION: str = "v5.6-id-strict-1"
SCHEMA_VERSION_AI: str = "v5.6-json-1"
DEPLOYMENT_MODE: str = os.getenv("DEPLOYMENT_MODE", "single_user_private")

DEFAULT_ANALYST_MODEL: str = "openai/gpt-oss-20b"
DEFAULT_DEBATE_MODEL: str = "openai/gpt-oss-20b"
DEFAULT_RESEARCH_MODEL: str = "openai/gpt-oss-120b"
DEFAULT_JUDGE_MODEL: str = "openai/gpt-oss-120b"

DEFAULT_GEMINI_ANALYST_MODEL: str = "gemini-2.5-flash"
DEFAULT_GEMINI_DEBATE_MODEL: str = "gemini-2.5-flash"
DEFAULT_GEMINI_RESEARCH_MODEL: str = "gemini-2.5-pro"
DEFAULT_GEMINI_JUDGE_MODEL: str = "gemini-2.5-pro"

GEMINI_MODEL_TIERS: Dict[str, str] = {
    "analyst": DEFAULT_GEMINI_ANALYST_MODEL,
    "debate": DEFAULT_GEMINI_DEBATE_MODEL,
    "research": DEFAULT_GEMINI_RESEARCH_MODEL,
    "judge": DEFAULT_GEMINI_JUDGE_MODEL,
}

DEFAULT_ANALYST_MAX_TOKENS: int = 700
DEFAULT_DEBATE_MAX_TOKENS: int = 700
DEFAULT_RESEARCH_MAX_TOKENS: int = 900
DEFAULT_JUDGE_MAX_TOKENS: int = 1100
DEFAULT_TRADER_MAX_TOKENS: int = 600
DEFAULT_RISK_MAX_TOKENS: int = 550
DEFAULT_MIN_COMPLETION_TOKENS: int = 128

DEFAULT_MAX_LOGICAL_CALLS: int = 12
DEFAULT_MAX_PHYSICAL_ATTEMPTS: int = 12
DEFAULT_MAX_ATTEMPTS_PER_CALL: int = 2
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 20.0
DEFAULT_CANDIDATE_DEADLINE_SECONDS: float = 90.0
DEFAULT_GROQ_MIN_DELAY: float = 1.0
DEFAULT_GROQ_RETRY_SECONDS: float = 30.0

DEFAULT_MAX_SPREAD_PCT: float = 0.35
DEFAULT_MIN_NET_RR: float = 1.5
DEFAULT_ROUND_TRIP_COST_PCT: float = 0.12
DEFAULT_MIN_JUDGE_CONFIDENCE: float = 55.0

TG_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT: str = os.getenv("TELEGRAM_CHAT_ID", "")

MODEL_TIERS: Dict[str, Dict[str, Any]] = {
    "analyst": {
        "model": DEFAULT_ANALYST_MODEL,
        "max_tokens": DEFAULT_ANALYST_MAX_TOKENS,
        "effective_max_tokens": max(DEFAULT_MIN_COMPLETION_TOKENS, DEFAULT_ANALYST_MAX_TOKENS),
        "min_completion_tokens": DEFAULT_MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "debate": {
        "model": DEFAULT_DEBATE_MODEL,
        "max_tokens": DEFAULT_DEBATE_MAX_TOKENS,
        "effective_max_tokens": max(DEFAULT_MIN_COMPLETION_TOKENS, DEFAULT_DEBATE_MAX_TOKENS),
        "min_completion_tokens": DEFAULT_MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "research": {
        "model": DEFAULT_RESEARCH_MODEL,
        "max_tokens": DEFAULT_RESEARCH_MAX_TOKENS,
        "effective_max_tokens": max(DEFAULT_MIN_COMPLETION_TOKENS, DEFAULT_RESEARCH_MAX_TOKENS),
        "min_completion_tokens": DEFAULT_MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
    "judge": {
        "model": DEFAULT_JUDGE_MODEL,
        "max_tokens": DEFAULT_JUDGE_MAX_TOKENS,
        "effective_max_tokens": max(DEFAULT_MIN_COMPLETION_TOKENS, DEFAULT_JUDGE_MAX_TOKENS),
        "min_completion_tokens": DEFAULT_MIN_COMPLETION_TOKENS,
        "reasoning_effort": "low",
    },
}


@dataclass(frozen=True)
class ModelTierConfig:
    """Immutable configuration for an AI model tier."""
    tier: str
    model: str
    max_tokens: int
    min_completion_tokens: int = DEFAULT_MIN_COMPLETION_TOKENS
    reasoning_effort: str = "low"

    @property
    def effective_max_tokens(self) -> int:
        return max(1, int(self.min_completion_tokens), int(self.max_tokens))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "effective_max_tokens": self.effective_max_tokens,
            "min_completion_tokens": self.min_completion_tokens,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class RiskConfig:
    """Immutable risk & deterministic gate parameters."""
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT
    min_net_rr: float = DEFAULT_MIN_NET_RR
    round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT
    min_judge_confidence: float = DEFAULT_MIN_JUDGE_CONFIDENCE
    accounting_mode: str = LEGACY_ACCOUNTING_MODE


@dataclass(frozen=True)
class StorageConfig:
    """Immutable SQLite database & migration configuration."""
    db_file: str = DEFAULT_DB_FILE
    busy_timeout_ms: int = 5000
    enable_wal: bool = False
    foreign_keys: bool = True


@dataclass(frozen=True)
class AppConfig:
    """Validated top-level application configuration."""
    binance_url: str = DEFAULT_BINANCE_URL
    deployment_mode: str = "single_user_private"
    max_logical_calls: int = DEFAULT_MAX_LOGICAL_CALLS
    max_physical_attempts: int = DEFAULT_MAX_PHYSICAL_ATTEMPTS
    max_attempts_per_call: int = DEFAULT_MAX_ATTEMPTS_PER_CALL
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    candidate_deadline_seconds: float = DEFAULT_CANDIDATE_DEADLINE_SECONDS
    groq_min_delay: float = DEFAULT_GROQ_MIN_DELAY
    groq_retry_seconds: float = DEFAULT_GROQ_RETRY_SECONDS
    min_completion_tokens: int = DEFAULT_MIN_COMPLETION_TOKENS
    trader_max_tokens: int = DEFAULT_TRADER_MAX_TOKENS
    risk_max_tokens: int = DEFAULT_RISK_MAX_TOKENS
    risk: RiskConfig = field(default_factory=RiskConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tiers: Dict[str, ModelTierConfig] = field(default_factory=dict)

    @classmethod
    def from_env(cls, getter: Optional[Callable[[str], Any]] = None) -> "AppConfig":
        get = getter or (lambda k: os.getenv(k, ""))
        min_comp = int(get("MIN_COMPLETION_TOKENS") or DEFAULT_MIN_COMPLETION_TOKENS)
        tiers = {
            "analyst": ModelTierConfig(
                tier="analyst",
                model=str(get("ANALYST_MODEL") or DEFAULT_ANALYST_MODEL),
                max_tokens=int(get("ANALYST_MAX_TOKENS") or DEFAULT_ANALYST_MAX_TOKENS),
                min_completion_tokens=min_comp,
                reasoning_effort="low",
            ),
            "debate": ModelTierConfig(
                tier="debate",
                model=str(get("DEBATE_MODEL") or DEFAULT_DEBATE_MODEL),
                max_tokens=int(get("DEBATE_MAX_TOKENS") or DEFAULT_DEBATE_MAX_TOKENS),
                min_completion_tokens=min_comp,
                reasoning_effort="low",
            ),
            "research": ModelTierConfig(
                tier="research",
                model=str(get("RESEARCH_MODEL") or DEFAULT_RESEARCH_MODEL),
                max_tokens=int(get("RESEARCH_MAX_TOKENS") or DEFAULT_RESEARCH_MAX_TOKENS),
                min_completion_tokens=min_comp,
                reasoning_effort="low",
            ),
            "judge": ModelTierConfig(
                tier="judge",
                model=str(get("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL),
                max_tokens=int(get("JUDGE_MAX_TOKENS") or DEFAULT_JUDGE_MAX_TOKENS),
                min_completion_tokens=min_comp,
                reasoning_effort="low",
            ),
        }
        max_log = int(get("MAX_AI_CALLS_PER_CANDIDATE") or DEFAULT_MAX_LOGICAL_CALLS)
        max_phys = int(get("MAX_PHYSICAL_AI_ATTEMPTS_PER_CANDIDATE") or max_log)
        return cls(
            binance_url=str(get("BINANCE_URL") or DEFAULT_BINANCE_URL),
            deployment_mode=str(get("DEPLOYMENT_MODE") or "single_user_private"),
            max_logical_calls=max_log,
            max_physical_attempts=max_phys,
            max_attempts_per_call=int(get("MAX_PHYSICAL_ATTEMPTS_PER_CALL") or DEFAULT_MAX_ATTEMPTS_PER_CALL),
            http_timeout_seconds=float(get("AI_HTTP_TIMEOUT_SECONDS") or DEFAULT_HTTP_TIMEOUT_SECONDS),
            candidate_deadline_seconds=float(get("AI_CANDIDATE_DEADLINE_SECONDS") or DEFAULT_CANDIDATE_DEADLINE_SECONDS),
            groq_min_delay=float(get("GROQ_MIN_DELAY") or DEFAULT_GROQ_MIN_DELAY),
            groq_retry_seconds=float(get("GROQ_RETRY_SECONDS") or DEFAULT_GROQ_RETRY_SECONDS),
            min_completion_tokens=min_comp,
            trader_max_tokens=int(get("TRADER_MAX_TOKENS") or DEFAULT_TRADER_MAX_TOKENS),
            risk_max_tokens=int(get("RISK_MAX_TOKENS") or DEFAULT_RISK_MAX_TOKENS),
            risk=RiskConfig(
                max_spread_pct=float(get("MAX_SPREAD_PCT") or DEFAULT_MAX_SPREAD_PCT),
                min_net_rr=float(get("MIN_NET_RR") or DEFAULT_MIN_NET_RR),
                round_trip_cost_pct=float(get("ESTIMATED_ROUND_TRIP_COST_PCT") or DEFAULT_ROUND_TRIP_COST_PCT),
                min_judge_confidence=float(get("MIN_JUDGE_CONFIDENCE") or DEFAULT_MIN_JUDGE_CONFIDENCE),
            ),
            storage=StorageConfig(
                db_file=str(get("DB_FILE") or DEFAULT_DB_FILE),
            ),
            tiers=tiers,
        )


def mask_secret(key: Any) -> str:
    """Mask an API key or token so raw secrets never appear in logs or UI (F35)."""
    s = str(key or "").strip()
    if not s:
        return "-"
    if len(s) <= 12:
        return "***"
    return f"{s[:5]}...{s[-4:]}"


def parse_secret_list(value: Any) -> List[str]:
    """Parse a single string, comma/whitespace separated keys, or list/tuple of secrets."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = re.split(r"[\s,]+", str(value))
    return [str(x).strip() for x in raw_items if str(x).strip()]


def get_groq_keys(getter: Optional[Callable[[str], Any]] = None) -> List[str]:
    """Collect deduplicated Groq API keys from environment or secret getter."""
    get = getter or (lambda k: os.getenv(k, ""))
    keys: List[str] = []
    keys.extend(parse_secret_list(get("GROQ_API_KEY")))
    keys.extend(parse_secret_list(get("GROQ_API_KEYS")))
    for i in range(1, 21):
        keys.extend(parse_secret_list(get(f"GROQ_API_KEY_{i}")))
    return list(dict.fromkeys(k for k in keys if k))


def get_gemini_keys(getter: Optional[Callable[[str], Any]] = None) -> List[str]:
    """Collect deduplicated optional Gemini API keys from environment or secret getter (PH05-04)."""
    get = getter or (lambda k: os.getenv(k, ""))
    keys: List[str] = []
    keys.extend(parse_secret_list(get("GEMINI_API_KEY")))
    keys.extend(parse_secret_list(get("GEMINI_API_KEYS")))
    keys.extend(parse_secret_list(get("GOOGLE_API_KEY")))
    for i in range(1, 21):
        keys.extend(parse_secret_list(get(f"GEMINI_API_KEY_{i}")))
    return list(dict.fromkeys(k for k in keys if k))


def is_gemini_enabled(
    getter: Optional[Callable[[str], Any]] = None,
    gemini_keys: Optional[List[str]] = None,
) -> bool:
    """Return True iff optional Gemini fallback is explicitly enabled and has at least one key (PH05-04)."""
    get = getter or (lambda k: os.getenv(k, ""))
    raw_flag = str(get("GEMINI_ENABLED") or get("ENABLE_GEMINI_FALLBACK") or "").strip().lower()
    flag_on = raw_flag in {"1", "true", "yes", "on"}
    keys = gemini_keys if gemini_keys is not None else get_gemini_keys(get)
    return bool(flag_on and len(keys) > 0)


_SECRET_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:bot)?\d{6,12}:[A-Za-z0-9_\-]{10,}\b"),
)


def redact_secrets(value: Any, extra_secrets: Optional[Iterable[str]] = None) -> Any:
    """Recursively redact API keys, Telegram tokens, and explicit extra_secrets from strings, dicts, and lists (F35, T18)."""
    extras = [str(s).strip() for s in (extra_secrets or ()) if s and len(str(s).strip()) >= 6]
    if isinstance(value, str):
        out = value
        for s in extras:
            if s in out:
                out = out.replace(s, "[REDACTED_SECRET]")
        for pat in _SECRET_PATTERNS:
            out = pat.sub("[REDACTED_SECRET]", out)
        return out
    if isinstance(value, dict):
        return {k: redact_secrets(v, extra_secrets=extras) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v, extra_secrets=extras) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(v, extra_secrets=extras) for v in value)
    return value


def compute_model_config_hash(tiers: Optional[Dict[str, Any]] = None) -> str:
    """Compute a deterministic SHA-256 fingerprint of the active model tiers configuration (F26, T18)."""
    payload = tiers if tiers is not None else MODEL_TIERS
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_deployment_security(
    mode: Optional[str] = None,
    auth_enabled: bool = False,
    user_isolated_db: bool = False,
) -> Dict[str, Any]:
    """Validate deployment mode policy: single_user_private is allowed by default; shared requires auth + DB isolation (F35)."""
    target_mode = str(mode or DEPLOYMENT_MODE or "single_user_private").strip().lower()
    if target_mode == "single_user_private":
        return {
            "allowed": True,
            "mode": "single_user_private",
            "veto_code": None,
            "reason": "Mode single_user_private diizinkan.",
        }
    if target_mode == "shared":
        if auth_enabled and user_isolated_db:
            return {
                "allowed": True,
                "mode": "shared",
                "veto_code": None,
                "reason": "Mode shared diizinkan dengan autentikasi dan isolasi DB.",
            }
        return {
            "allowed": False,
            "mode": "shared",
            "veto_code": "SHARED_MODE_REQUIRES_AUTH_AND_ISOLATION",
            "reason": "Mode shared ditolak tanpa autentikasi dan isolasi database per pengguna.",
        }
    return {
        "allowed": False,
        "mode": target_mode,
        "veto_code": "UNKNOWN_DEPLOYMENT_MODE",
        "reason": f"Mode deployment tidak dikenal: {target_mode}",
    }


# ---------------------------------------------------------
# Master Prompt 2: Autonomous Live Trading & Exchange Config
# ---------------------------------------------------------
DEFAULT_EXECUTION_MODE: str = "PAPER"  # PAPER | TESTNET | LIVE
DEFAULT_LIVE_TRADING_ENABLED: bool = False
DEFAULT_EXCHANGE_MARKET_TYPE: str = "FUTURES_USDM"  # SPOT | FUTURES_USDM
DEFAULT_BINANCE_SPOT_URL: str = "https://api.binance.com"
DEFAULT_BINANCE_SPOT_TESTNET_URL: str = "https://testnet.binance.vision"
DEFAULT_BINANCE_FUTURES_URL: str = "https://fapi.binance.com"
DEFAULT_BINANCE_FUTURES_TESTNET_URL: str = "https://testnet.binancefuture.com"
DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS: float = 60.0
DEFAULT_MAX_LEVERAGE: float = 5.0
DEFAULT_EMERGENCY_CLOSE_ON_UNPROTECTED: bool = False
DEFAULT_KILL_SWITCH_CANCEL_UNFILLED_ENTRIES: bool = True
DEFAULT_KILL_SWITCH_PRESERVE_PROTECTIVE_ORDERS: bool = True


def _parse_bool_env(val: Any, default: bool = False) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def get_exchange_credentials(
    execution_mode: str = "PAPER",
    getter: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve exchange API credentials from environment variables without ever mixing
    TESTNET and LIVE credentials or exposing raw secrets in logs/UI (Section 4).
    """
    get = getter or (lambda k: os.getenv(k, ""))
    mode_u = str(execution_mode or get("EXECUTION_MODE") or DEFAULT_EXECUTION_MODE).upper().strip()
    if mode_u not in {"PAPER", "TESTNET", "LIVE"}:
        mode_u = "PAPER"

    if mode_u == "TESTNET":
        api_key = str(get("BINANCE_TESTNET_API_KEY") or "").strip()
        api_secret = str(get("BINANCE_TESTNET_API_SECRET") or "").strip()
        cred_source = "BINANCE_TESTNET_API_KEY"
    elif mode_u == "LIVE":
        api_key = str(get("BINANCE_API_KEY") or "").strip()
        api_secret = str(get("BINANCE_API_SECRET") or "").strip()
        cred_source = "BINANCE_API_KEY"
    else:
        # PAPER mode can still optionally read testnet/live read-only keys if explicitly requested,
        # but defaults to unauthenticated paper simulation.
        api_key = str(get("BINANCE_TESTNET_API_KEY") or get("BINANCE_API_KEY") or "").strip()
        api_secret = str(get("BINANCE_TESTNET_API_SECRET") or get("BINANCE_API_SECRET") or "").strip()
        cred_source = "OPTIONAL_PAPER_READONLY"

    has_credentials = bool(api_key and api_secret)
    return {
        "execution_mode": mode_u,
        "credential_source": cred_source,
        "has_credentials": has_credentials,
        "api_key": api_key,
        "api_secret": api_secret,
        "api_key_masked": mask_secret(api_key),
        "extra_secrets": [s for s in (api_key, api_secret) if s],
    }


def resolve_execution_mode_config(
    getter: Optional[Callable[[str], Any]] = None,
    *,
    runtime_live_confirmed: bool = False,
    kill_switch_active: bool = False,
) -> Dict[str, Any]:
    """
    Resolve autonomous execution mode policy (Section 9):
    - Defaults strictly to EXECUTION_MODE=PAPER and LIVE_TRADING_ENABLED=false.
    - Never switches automatically from PAPER or TESTNET to LIVE.
    - LIVE execution requires BOTH config-level (`EXECUTION_MODE=LIVE` and `LIVE_TRADING_ENABLED=true`)
      AND explicit user runtime activation (`runtime_live_confirmed=True`) with Kill Switch inactive.
    """
    get = getter or (lambda k: os.getenv(k, ""))
    raw_mode = str(get("EXECUTION_MODE") or DEFAULT_EXECUTION_MODE).upper().strip()
    if raw_mode not in {"PAPER", "TESTNET", "LIVE"}:
        raw_mode = "PAPER"

    live_cfg_flag = _parse_bool_env(get("LIVE_TRADING_ENABLED"), default=DEFAULT_LIVE_TRADING_ENABLED)
    testnet_cfg_flag = _parse_bool_env(get("TESTNET_TRADING_ENABLED"), default=(raw_mode == "TESTNET"))
    market_type = str(get("EXCHANGE_MARKET_TYPE") or DEFAULT_EXCHANGE_MARKET_TYPE).upper().strip()
    if market_type not in {"SPOT", "FUTURES_USDM"}:
        market_type = DEFAULT_EXCHANGE_MARKET_TYPE

    emergency_close = _parse_bool_env(
        get("EMERGENCY_CLOSE_ON_UNPROTECTED"),
        default=DEFAULT_EMERGENCY_CLOSE_ON_UNPROTECTED,
    )
    cancel_unfilled = _parse_bool_env(
        get("KILL_SWITCH_CANCEL_UNFILLED_ENTRIES"),
        default=DEFAULT_KILL_SWITCH_CANCEL_UNFILLED_ENTRIES,
    )
    preserve_protective = _parse_bool_env(
        get("KILL_SWITCH_PRESERVE_PROTECTIVE_ORDERS"),
        default=DEFAULT_KILL_SWITCH_PRESERVE_PROTECTIVE_ORDERS,
    )
    max_acct_age = float(get("MAX_ACCOUNT_FRESHNESS_SECONDS") or DEFAULT_MAX_ACCOUNT_FRESHNESS_SECONDS)
    max_lev = float(get("MAX_PERMITTED_LEVERAGE") or DEFAULT_MAX_LEVERAGE)

    live_authorized = bool(
        raw_mode == "LIVE"
        and live_cfg_flag
        and runtime_live_confirmed
        and not kill_switch_active
    )
    testnet_authorized = bool(
        raw_mode == "TESTNET"
        and testnet_cfg_flag
        and not kill_switch_active
    )

    if raw_mode == "LIVE" and not live_authorized:
        auth_status = "BLOCKED_AWAITING_EXPLICIT_AUTHORIZATION"
    elif raw_mode == "LIVE" and live_authorized:
        auth_status = "LIVE_AUTHORIZED"
    elif raw_mode == "TESTNET" and testnet_authorized:
        auth_status = "TESTNET_AUTHORIZED"
    elif raw_mode == "TESTNET":
        auth_status = "TESTNET_DISABLED"
    else:
        auth_status = "PAPER_SIMULATION_ONLY"

    return {
        "execution_mode": raw_mode,
        "market_type": market_type,
        "live_trading_enabled_config": live_cfg_flag,
        "runtime_live_confirmed": bool(runtime_live_confirmed),
        "live_execution_authorized": live_authorized,
        "testnet_execution_authorized": testnet_authorized,
        "kill_switch_active": bool(kill_switch_active),
        "authorization_status": auth_status,
        "max_account_freshness_seconds": max_acct_age,
        "max_permitted_leverage": max_lev,
        "emergency_close_on_unprotected": emergency_close,
        "kill_switch_cancel_unfilled_entries": cancel_unfilled,
        "kill_switch_preserve_protective_orders": preserve_protective,
    }
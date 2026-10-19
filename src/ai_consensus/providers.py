"""
Provider adapter contract, model capability registry (strict json_schema vs json_object),
role-specific schema & prompt version binding, Retry-After / x-ratelimit-reset header parsing,
typed provider error hierarchy, and optional Gemini adapter isolation for AI Consensus Trading
(PH05-01 / F02, F18, F20, F21 / T21).
Strictly zero UI, DB, or network side-effects on import.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .agents import (
    AI_BASE_SCHEMA,
    AI_JUDGE_SCHEMA,
    build_agent_prompt,
    get_schema_for_role,
    is_judge_role,
)
from .config import (
    PROMPT_VERSION,
    SCHEMA_VERSION_AI,
    STRATEGY_VERSION,
    mask_secret,
    redact_secrets,
)
from .domain import (
    is_finite_number,
    is_valid_confidence,
)

PROVIDERS_ENGINE_VERSION: str = "v6.0-provider-adapter-1"


# ---------------------------------------------------------
# Rate-Limit & Reset Header Parsing (F21, T21)
# ---------------------------------------------------------
def parse_duration_header_seconds(raw_val: Any) -> Optional[float]:
    """
    Parse duration headers such as '30', '45.5', '2m30s', '1h5m10s', '600ms' into seconds.
    Returns None if unparseable.
    """
    if raw_val is None:
        return None
    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
        return max(0.0, float(raw_val)) if is_finite_number(raw_val) else None

    s = str(raw_val).strip()
    if not s:
        return None

    try:
        val = float(s)
        if is_finite_number(val) and val >= 0.0:
            return val
    except ValueError:
        pass

    # Match composite duration tokens like 1h, 2m, 30s, 500ms
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", flags=re.I)
    matches = list(pattern.finditer(s))
    if not matches:
        return None

    total_sec = 0.0
    for m in matches:
        num = float(m.group(1))
        unit = m.group(2).lower()
        if unit == "h":
            total_sec += num * 3600.0
        elif unit == "m":
            total_sec += num * 60.0
        elif unit == "s":
            total_sec += num
        elif unit == "ms":
            total_sec += num / 1000.0
    return max(0.0, total_sec)


def parse_rate_limit_headers(
    headers: Optional[Dict[str, Any]],
    default_retry_seconds: float = 30.0,
) -> Dict[str, Any]:
    """
    Extract Retry-After and x-ratelimit-* reset/remaining metadata from HTTP headers (F21, T21).
    """
    if not isinstance(headers, dict) or not headers:
        return {
            "retry_after_seconds": max(1.0, float(default_retry_seconds)),
            "has_explicit_retry_after": False,
            "reset_requests_seconds": None,
            "reset_tokens_seconds": None,
            "remaining_requests": None,
            "remaining_tokens": None,
        }

    lower_map = {str(k).lower().strip(): v for k, v in headers.items() if k is not None}

    ra_sec = parse_duration_header_seconds(lower_map.get("retry-after"))
    reset_req_sec = parse_duration_header_seconds(lower_map.get("x-ratelimit-reset-requests"))
    reset_tok_sec = parse_duration_header_seconds(lower_map.get("x-ratelimit-reset-tokens"))

    rem_req_raw = lower_map.get("x-ratelimit-remaining-requests")
    rem_tok_raw = lower_map.get("x-ratelimit-remaining-tokens")
    try:
        rem_req = int(rem_req_raw) if rem_req_raw is not None else None
    except (TypeError, ValueError):
        rem_req = None
    try:
        rem_tok = int(rem_tok_raw) if rem_tok_raw is not None else None
    except (TypeError, ValueError):
        rem_tok = None

    candidates = [x for x in (ra_sec, reset_req_sec, reset_tok_sec) if x is not None and x > 0.0]
    effective_retry = max(1.0, max(candidates)) if candidates else max(1.0, float(default_retry_seconds))

    return {
        "retry_after_seconds": round(effective_retry, 3),
        "has_explicit_retry_after": len(candidates) > 0,
        "reset_requests_seconds": reset_req_sec,
        "reset_tokens_seconds": reset_tok_sec,
        "remaining_requests": rem_req,
        "remaining_tokens": rem_tok,
    }


# ---------------------------------------------------------
# Typed Provider Error Hierarchy (PH05-01 / F21 / T21)
# ---------------------------------------------------------
class ProviderError(RuntimeError):
    """Base typed exception for all AI provider errors with explicit backoff & routing policy."""

    category: str = "PROVIDER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        provider: str = "groq",
        model_id: str = "",
        category: Optional[str] = None,
        status_code: Optional[int] = None,
        retry_after_seconds: float = 0.0,
        cooldown_seconds: float = 0.0,
        disable_key: bool = False,
        exhaust_quota_scope: bool = False,
        retry_another_key: bool = False,
        allow_fallback_provider: bool = False,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.provider = str(provider or "unknown").lower().strip()
        self.model_id = str(model_id or "")
        if category is not None:
            self.category = str(category)
        self.status_code = int(status_code) if status_code is not None else None
        self.retry_after_seconds = float(retry_after_seconds or 0.0)
        self.cooldown_seconds = float(cooldown_seconds or 0.0)
        self.disable_key = bool(disable_key)
        self.exhaust_quota_scope = bool(exhaust_quota_scope)
        self.retry_another_key = bool(retry_another_key)
        self.allow_fallback_provider = bool(allow_fallback_provider)
        self.headers = dict(headers or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "provider": self.provider,
            "model_id": self.model_id,
            "status_code": self.status_code,
            "retry_after_seconds": self.retry_after_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "disable_key": self.disable_key,
            "exhaust_quota_scope": self.exhaust_quota_scope,
            "retry_another_key": self.retry_another_key,
            "allow_fallback_provider": self.allow_fallback_provider,
            "message": str(self),
        }


class ProviderAuthError(ProviderError):
    """401/403 invalid or unauthorized API key."""
    category = "AUTH_ERROR"

    def __init__(self, message: str, *, provider: str = "groq", model_id: str = "", status_code: int = 401, headers: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message,
            provider=provider,
            model_id=model_id,
            category="AUTH_ERROR",
            status_code=status_code,
            retry_after_seconds=0.0,
            cooldown_seconds=24.0 * 3600.0,
            disable_key=True,
            exhaust_quota_scope=False,
            retry_another_key=True,
            allow_fallback_provider=True,
            headers=headers,
        )


class ProviderModelUnsupportedError(ProviderError):
    """Model ID is not in the verified capability registry or not supported by the provider (T21)."""
    category = "MODEL_UNSUPPORTED"

    def __init__(self, message: str, *, provider: str = "groq", model_id: str = "", status_code: int = 404, headers: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message,
            provider=provider,
            model_id=model_id,
            category="MODEL_UNSUPPORTED",
            status_code=status_code,
            retry_after_seconds=0.0,
            cooldown_seconds=0.0,
            disable_key=False,
            exhaust_quota_scope=False,
            retry_another_key=False,
            allow_fallback_provider=False,
            headers=headers,
        )


class ProviderRateLimitError(ProviderError):
    """Temporary HTTP 429 rate-limit with Retry-After / reset header backoff."""
    category = "RATE_LIMIT_TEMPORARY"

    def __init__(
        self,
        message: str,
        *,
        provider: str = "groq",
        model_id: str = "",
        retry_after_seconds: float = 30.0,
        status_code: int = 429,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        wait_s = max(1.0, float(retry_after_seconds))
        super().__init__(
            message,
            provider=provider,
            model_id=model_id,
            category="RATE_LIMIT_TEMPORARY",
            status_code=status_code,
            retry_after_seconds=wait_s,
            cooldown_seconds=wait_s,
            disable_key=True,
            exhaust_quota_scope=False,
            retry_another_key=True,
            allow_fallback_provider=True,
            headers=headers,
        )


class ProviderDailyQuotaError(ProviderError):
    """Daily token/request quota or billing exhaustion — halts key rotation on the same quota scope (F21, T21)."""
    category = "DAILY_QUOTA_EXHAUSTED"

    def __init__(
        self,
        message: str,
        *,
        provider: str = "groq",
        model_id: str = "",
        cooldown_seconds: float = 24.0 * 3600.0,
        status_code: int = 429,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        cd_s = max(60.0, float(cooldown_seconds))
        super().__init__(
            message,
            provider=provider,
            model_id=model_id,
            category="DAILY_QUOTA_EXHAUSTED",
            status_code=status_code,
            retry_after_seconds=cd_s,
            cooldown_seconds=cd_s,
            disable_key=True,
            exhaust_quota_scope=True,
            retry_another_key=False,
            allow_fallback_provider=True,
            headers=headers,
        )


class ProviderNetworkTimeoutError(ProviderError):
    """HTTP timeout or connection failure."""
    category = "TIMEOUT_OR_NETWORK"

    def __init__(self, message: str, *, provider: str = "groq", model_id: str = "", cooldown_seconds: float = 60.0, headers: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message,
            provider=provider,
            model_id=model_id,
            category="TIMEOUT_OR_NETWORK",
            status_code=504,
            retry_after_seconds=float(cooldown_seconds),
            cooldown_seconds=float(cooldown_seconds),
            disable_key=True,
            exhaust_quota_scope=False,
            retry_another_key=True,
            allow_fallback_provider=True,
            headers=headers,
        )


class ProviderMalformedOutputError(ProviderError):
    """Model returned non-JSON, truncated JSON, or schema-invalid output — never disables the API key (T21)."""
    category = "MALFORMED_OUTPUT"

    def __init__(self, message: str, *, provider: str = "groq", model_id: str = "", finish_reason: Optional[str] = None) -> None:
        super().__init__(
            message,
            provider=provider,
            model_id=model_id,
            category="MALFORMED_OUTPUT",
            status_code=None,
            retry_after_seconds=0.0,
            cooldown_seconds=0.0,
            disable_key=False,
            exhaust_quota_scope=False,
            retry_another_key=False,
            allow_fallback_provider=False,
        )
        self.finish_reason = finish_reason


def coerce_to_provider_error(
    exc: Any,
    *,
    provider: str = "groq",
    model_id: str = "",
    default_retry_seconds: float = 30.0,
    headers: Optional[Dict[str, Any]] = None,
) -> ProviderError:
    """
    Convert any raw SDK/HTTP/validation exception into a typed ProviderError subclass
    respecting header Retry-After/reset values and checking DAILY_QUOTA_EXHAUSTED before generic 429 (F21, T21).
    """
    if isinstance(exc, ProviderError):
        return exc

    raw_headers = headers
    if raw_headers is None and hasattr(exc, "headers") and isinstance(getattr(exc, "headers"), dict):
        raw_headers = getattr(exc, "headers")
    elif raw_headers is None and hasattr(exc, "response") and hasattr(getattr(exc, "response"), "headers"):
        try:
            raw_headers = dict(getattr(exc, "response").headers)
        except Exception:
            raw_headers = None

    hdr_info = parse_rate_limit_headers(raw_headers, default_retry_seconds=default_retry_seconds)
    msg = str(exc or "")
    msg_lower = msg.lower()
    status_code = getattr(exc, "status_code", None)

    # 1. Daily quota / TPD / RPD / billing check FIRST (even if HTTP 429)
    if any(
        x in msg_lower
        for x in (
            "tokens per day",
            "requests per day",
            "daily limit",
            "daily quota",
            "quota exceeded",
            "quota has been exceeded",
            "insufficient_quota",
            "billing",
            "resource_exhausted",
            "per day (tpd)",
            "per day (rpd)",
        )
    ):
        cd_sec = hdr_info["retry_after_seconds"] if hdr_info["has_explicit_retry_after"] and hdr_info["retry_after_seconds"] >= 300.0 else 24.0 * 3600.0
        return ProviderDailyQuotaError(
            msg,
            provider=provider,
            model_id=model_id,
            cooldown_seconds=cd_sec,
            status_code=int(status_code or 429),
            headers=raw_headers,
        )

    # 2. Temporary 429 rate-limit
    if status_code == 429 or "429" in msg_lower or "rate_limit" in msg_lower or "rate limit" in msg_lower or "too many requests" in msg_lower:
        retry_sec = hdr_info["retry_after_seconds"]
        if not hdr_info["has_explicit_retry_after"]:
            m = re.search(r"retry[-_\s]?after\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", msg, flags=re.I)
            if m:
                retry_sec = max(1.0, float(m.group(1)))
        return ProviderRateLimitError(
            msg,
            provider=provider,
            model_id=model_id,
            retry_after_seconds=retry_sec,
            status_code=429,
            headers=raw_headers,
        )

    # 3. Authentication / Permission error (401/403)
    if status_code in (401, 403) or any(x in msg_lower for x in ("401", "403", "invalid api key", "authentication", "unauthorized", "permission_denied", "api_key_invalid")):
        return ProviderAuthError(
            msg,
            provider=provider,
            model_id=model_id,
            status_code=int(status_code or 401),
            headers=raw_headers,
        )

    # 4. Model not found / unsupported
    if status_code == 404 or any(x in msg_lower for x in ("model_not_found", "does not exist", "do not have access", "unsupported model", "model not found", "decommissioned")):
        return ProviderModelUnsupportedError(
            msg,
            provider=provider,
            model_id=model_id,
            status_code=int(status_code or 404),
            headers=raw_headers,
        )

    # 5. Network / Timeout
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(x in msg_lower for x in ("timeout", "timed out", "connection", "dns", "unreachable")):
        return ProviderNetworkTimeoutError(
            msg,
            provider=provider,
            model_id=model_id,
            cooldown_seconds=60.0,
            headers=raw_headers,
        )

    # 6. Malformed JSON / Schema output
    return ProviderMalformedOutputError(
        msg,
        provider=provider,
        model_id=model_id,
    )


# ---------------------------------------------------------
# Model Capability Registry (PH05-01 / F18, F20 / T21)
# ---------------------------------------------------------
@dataclass(frozen=True)
class ModelCapability:
    """Verified capability specification for an LLM model ID on a specific provider."""
    provider: str
    model_id: str
    response_mode: str  # "json_schema" | "json_object"
    supports_strict_json_schema: bool
    supports_json_object: bool
    supports_reasoning_effort: bool
    allowed_reasoning_efforts: Tuple[str, ...] = field(default_factory=tuple)
    default_reasoning_effort: Optional[str] = None
    max_completion_tokens_limit: int = 8192
    min_completion_tokens_floor: int = 64

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "response_mode": self.response_mode,
            "supports_strict_json_schema": self.supports_strict_json_schema,
            "supports_json_object": self.supports_json_object,
            "supports_reasoning_effort": self.supports_reasoning_effort,
            "allowed_reasoning_efforts": list(self.allowed_reasoning_efforts),
            "default_reasoning_effort": self.default_reasoning_effort,
            "max_completion_tokens_limit": self.max_completion_tokens_limit,
            "min_completion_tokens_floor": self.min_completion_tokens_floor,
        }


DEFAULT_CAPABILITY_REGISTRY: Dict[Tuple[str, str], ModelCapability] = {
    ("groq", "openai/gpt-oss-20b"): ModelCapability(
        provider="groq",
        model_id="openai/gpt-oss-20b",
        response_mode="json_schema",
        supports_strict_json_schema=True,
        supports_json_object=True,
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=("low", "medium", "high"),
        default_reasoning_effort="low",
        max_completion_tokens_limit=8192,
    ),
    ("groq", "openai/gpt-oss-120b"): ModelCapability(
        provider="groq",
        model_id="openai/gpt-oss-120b",
        response_mode="json_schema",
        supports_strict_json_schema=True,
        supports_json_object=True,
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=("low", "medium", "high"),
        default_reasoning_effort="low",
        max_completion_tokens_limit=8192,
    ),
    ("groq", "qwen/qwen3.6-27b"): ModelCapability(
        provider="groq",
        model_id="qwen/qwen3.6-27b",
        response_mode="json_object",
        supports_strict_json_schema=False,
        supports_json_object=True,
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=("none", "default"),
        default_reasoning_effort="none",
        max_completion_tokens_limit=8192,
    ),
    ("groq", "qwen/qwen3-32b"): ModelCapability(
        provider="groq",
        model_id="qwen/qwen3-32b",
        response_mode="json_object",
        supports_strict_json_schema=False,
        supports_json_object=True,
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=("none", "default"),
        default_reasoning_effort="none",
        max_completion_tokens_limit=8192,
    ),
    ("groq", "llama-3.3-70b-versatile"): ModelCapability(
        provider="groq",
        model_id="llama-3.3-70b-versatile",
        response_mode="json_object",
        supports_strict_json_schema=False,
        supports_json_object=True,
        supports_reasoning_effort=False,
        allowed_reasoning_efforts=(),
        default_reasoning_effort=None,
        max_completion_tokens_limit=8192,
    ),
    ("gemini", "gemini-2.5-flash"): ModelCapability(
        provider="gemini",
        model_id="gemini-2.5-flash",
        response_mode="json_schema",
        supports_strict_json_schema=True,
        supports_json_object=True,
        supports_reasoning_effort=False,
        allowed_reasoning_efforts=(),
        default_reasoning_effort=None,
        max_completion_tokens_limit=8192,
    ),
    ("gemini", "gemini-2.5-pro"): ModelCapability(
        provider="gemini",
        model_id="gemini-2.5-pro",
        response_mode="json_schema",
        supports_strict_json_schema=True,
        supports_json_object=True,
        supports_reasoning_effort=False,
        allowed_reasoning_efforts=(),
        default_reasoning_effort=None,
        max_completion_tokens_limit=8192,
    ),
}

_CUSTOM_CAPABILITY_REGISTRY: Dict[Tuple[str, str], ModelCapability] = {}


def register_model_capability(capability: ModelCapability) -> None:
    """Explicitly register a verified model capability in the runtime registry."""
    key = (capability.provider.lower().strip(), capability.model_id.strip())
    _CUSTOM_CAPABILITY_REGISTRY[key] = capability


def get_model_capability(
    model_id: str,
    provider: str = "groq",
    strict_registry: bool = True,
) -> ModelCapability:
    """
    Look up verified capability for (provider, model_id).
    When strict_registry=True, raises ProviderModelUnsupportedError if the model is not registered
    for that provider — preventing silent fallback or hallucinated model strings (PH05-01, T21).
    """
    prov = str(provider or "groq").lower().strip()
    mid = str(model_id or "").strip()
    if not mid:
        raise ProviderModelUnsupportedError(
            f"MODEL_UNSUPPORTED: Model ID kosong untuk provider '{prov}'.",
            provider=prov,
            model_id=mid,
        )

    key = (prov, mid)
    if key in _CUSTOM_CAPABILITY_REGISTRY:
        return _CUSTOM_CAPABILITY_REGISTRY[key]
    if key in DEFAULT_CAPABILITY_REGISTRY:
        return DEFAULT_CAPABILITY_REGISTRY[key]

    if strict_registry:
        raise ProviderModelUnsupportedError(
            f"MODEL_UNSUPPORTED: Model '{mid}' tidak terdaftar pada capability registry provider '{prov}'.",
            provider=prov,
            model_id=mid,
        )

    # Non-strict fallback only when explicitly requested by caller
    return ModelCapability(
        provider=prov,
        model_id=mid,
        response_mode="json_object",
        supports_strict_json_schema=False,
        supports_json_object=True,
        supports_reasoning_effort=False,
    )


# ---------------------------------------------------------
# Role-Specific Request & Schema Contract Builder (PH05-01 / F18 / T21)
# ---------------------------------------------------------
def build_provider_request_payload(
    *,
    role: str,
    tier: str,
    model_id: str,
    prompt: str,
    max_completion_tokens: int,
    provider: str = "groq",
    reasoning_effort: Optional[str] = None,
    temperature: float = 0.1,
    strict_registry: bool = True,
) -> Dict[str, Any]:
    """
    Build a capability-verified request payload with role-specific JSON schema and version metadata (PH05-01, F18, T21):
    - Final Judge receives AI_JUDGE_SCHEMA (6 fields including 'modes').
    - Specialist, Bull, Bear, Research Manager, Trader, and Risk roles receive AI_BASE_SCHEMA (5 fields without 'modes').
    - Models supporting strict json_schema use response_format={'type': 'json_schema', ...}.
    - Models supporting json_object (e.g., Qwen) use response_format={'type': 'json_object'} + local schema validator.
    """
    cap = get_model_capability(model_id, provider=provider, strict_registry=strict_registry)
    judge_flag = is_judge_role(role, tier)
    target_schema = get_schema_for_role(role, tier)
    schema_name = "trading_judge_decision" if judge_flag else "trading_agent_decision"
    role_schema_version = f"{SCHEMA_VERSION_AI}:{'judge-6f' if judge_flag else 'agent-5f'}"

    eff_tokens = max(
        cap.min_completion_tokens_floor,
        min(int(max_completion_tokens), cap.max_completion_tokens_limit),
    )

    kwargs: Dict[str, Any] = {
        "model": cap.model_id,
        "messages": [{"role": "user", "content": str(prompt)}],
        "temperature": float(temperature),
        "max_completion_tokens": eff_tokens,
    }

    if cap.supports_reasoning_effort:
        req_effort = str(reasoning_effort or cap.default_reasoning_effort or "").strip().lower()
        if cap.allowed_reasoning_efforts and req_effort not in cap.allowed_reasoning_efforts:
            req_effort = str(cap.default_reasoning_effort or cap.allowed_reasoning_efforts[0])
        if req_effort:
            kwargs["reasoning_effort"] = req_effort

    if cap.supports_strict_json_schema:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": target_schema,
            },
        }
    else:
        kwargs["response_format"] = {"type": "json_object"}

    return {
        "provider": cap.provider,
        "model_id": cap.model_id,
        "role": role,
        "tier": tier,
        "is_judge_role": judge_flag,
        "capability": cap.to_dict(),
        "sdk_kwargs": kwargs,
        "schema_name": schema_name,
        "schema": target_schema,
        "prompt_version": PROMPT_VERSION,
        "schema_version": role_schema_version,
        "strategy_version": STRATEGY_VERSION,
    }


def verify_prompt_schema_alignment(prompt: str, role: str, tier: str = "analyst") -> Dict[str, Any]:
    """
    Verify that the prompt text and the role-specific JSON schema do not contradict each other (PH05-01 done_when).
    """
    judge_flag = is_judge_role(role, tier)
    schema = get_schema_for_role(role, tier)
    schema_has_modes = "modes" in schema.get("properties", {}) and "modes" in schema.get("required", [])
    prompt_requires_modes = '"scalping":{"decision"' in prompt or "Field modes WAJIB diisi lengkap" in prompt
    prompt_forbids_modes = "JANGAN menyertakan field modes" in prompt

    aligned = (
        (judge_flag and schema_has_modes and prompt_requires_modes and not prompt_forbids_modes)
        or (not judge_flag and not schema_has_modes and prompt_forbids_modes and not prompt_requires_modes)
    )
    return {
        "aligned": bool(aligned),
        "role": role,
        "tier": tier,
        "is_judge_role": judge_flag,
        "schema_has_modes": schema_has_modes,
        "prompt_requires_modes": prompt_requires_modes,
        "prompt_forbids_modes": prompt_forbids_modes,
        "prompt_version": PROMPT_VERSION,
        "schema_version": f"{SCHEMA_VERSION_AI}:{'judge-6f' if judge_flag else 'agent-5f'}",
    }


# ---------------------------------------------------------
# Provider Adapters: Groq & Optional Gemini (PH05-01 / T21)
# ---------------------------------------------------------
class BaseProviderAdapter:
    """Abstract base contract for LLM provider adapters (Groq primary, Gemini optional)."""
    provider_name: str = "base"

    def is_available(self) -> bool:
        return False

    def validate_model(self, model_id: str, strict_registry: bool = True) -> ModelCapability:
        return get_model_capability(model_id, provider=self.provider_name, strict_registry=strict_registry)


class GroqProviderAdapter(BaseProviderAdapter):
    """
    Primary Groq provider adapter with capability registry verification, role-specific schema contract,
    Retry-After / reset header parsing, and typed error classification (PH05-01, T21).
    Never silently falls back to an unrequested model when a model is unsupported.
    """
    provider_name = "groq"

    def __init__(
        self,
        *,
        api_keys: Optional[List[str]] = None,
        transport_fn: Optional[Callable[..., Any]] = None,
        json_parser_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
        normalizer_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        usage_extractor_fn: Optional[Callable[[Any], Dict[str, Any]]] = None,
        default_retry_seconds: float = 30.0,
        strict_registry: bool = True,
    ) -> None:
        self.api_keys = list(api_keys or [])
        self.transport_fn = transport_fn
        self.json_parser_fn = json_parser_fn
        self.normalizer_fn = normalizer_fn
        self.usage_extractor_fn = usage_extractor_fn
        self.default_retry_seconds = float(default_retry_seconds)
        self.strict_registry = bool(strict_registry)

    def is_available(self) -> bool:
        return len(self.api_keys) > 0 or self.transport_fn is not None

    def execute_Single_attempt(
        self,
        *,
        role: str,
        tier: str,
        model_id: str,
        prompt: str,
        api_key: str,
        requested_max_tokens: int,
        effective_max_tokens: int,
        reasoning_effort: Optional[str] = None,
        strict_role_schema: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a single physical request against Groq (or injected transport_fn) and validate output.
        Raises a typed ProviderError subclass on transport/auth/model/rate-limit/daily-quota failure.
        """
        req_bundle = build_provider_request_payload(
            role=role,
            tier=tier,
            model_id=model_id,
            prompt=prompt,
            max_completion_tokens=effective_max_tokens,
            provider=self.provider_name,
            reasoning_effort=reasoning_effort,
            strict_registry=self.strict_registry,
        )

        if self.transport_fn is None:
            raise ProviderNetworkTimeoutError(
                "Transport Groq belum dikonfigurasi.",
                provider=self.provider_name,
                model_id=model_id,
            )

        try:
            try:
                raw_resp = self.transport_fn(
                    model_id,
                    prompt,
                    api_key,
                    effective_max_tokens,
                    tier,
                    role=role,
                )
            except TypeError:
                raw_resp = self.transport_fn(
                    model_id,
                    prompt,
                    api_key,
                    effective_max_tokens,
                    tier,
                )
        except Exception as exc:
            raise coerce_to_provider_error(
                exc,
                provider=self.provider_name,
                model_id=model_id,
                default_retry_seconds=self.default_retry_seconds,
            ) from exc

        choice = raw_resp.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        usage_info = self.usage_extractor_fn(raw_resp) if self.usage_extractor_fn else {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_known": False,
            "cost_status": "UNKNOWN_USAGE",
        }
        message = choice.message
        content = getattr(message, "content", None) or ""

        if str(finish_reason or "").strip().lower() == "length":
            if self.normalizer_fn is not None:
                norm_len = self.normalizer_fn(
                    {},
                    tier,
                    provider=self.provider_name,
                    model=model_id,
                    finish_reason=finish_reason,
                    usage=usage_info,
                    requested_max_tokens=requested_max_tokens,
                    effective_max_tokens=effective_max_tokens,
                    role=role,
                    strict_role_schema=strict_role_schema,
                )
                norm_len["prompt_version"] = req_bundle["prompt_version"]
                norm_len["schema_version"] = req_bundle["schema_version"]
                return norm_len
            raise ProviderMalformedOutputError(
                "Respons terpotong karena batas token (finish_reason=length).",
                provider=self.provider_name,
                model_id=model_id,
                finish_reason=finish_reason,
            )

        if not str(content).strip():
            raise ProviderMalformedOutputError(
                "Respons AI kosong.",
                provider=self.provider_name,
                model_id=model_id,
                finish_reason=finish_reason,
            )

        try:
            obj = self.json_parser_fn(content) if self.json_parser_fn else json.loads(content)
        except Exception as parse_exc:
            raise ProviderMalformedOutputError(
                f"Respons AI bukan JSON valid: {parse_exc}",
                provider=self.provider_name,
                model_id=model_id,
                finish_reason=finish_reason,
            ) from parse_exc

        if self.normalizer_fn is not None:
            normalized = self.normalizer_fn(
                obj,
                tier,
                provider=self.provider_name,
                model=model_id,
                finish_reason=finish_reason,
                usage=usage_info,
                requested_max_tokens=requested_max_tokens,
                effective_max_tokens=effective_max_tokens,
                role=role,
                strict_role_schema=strict_role_schema,
            )
            normalized["prompt_version"] = req_bundle["prompt_version"]
            normalized["schema_version"] = req_bundle["schema_version"]
            normalized["capability_mode"] = req_bundle["capability"]["response_mode"]
            return normalized

        return {
            "status": "OK",
            **obj,
            "tier": tier,
            "provider": self.provider_name,
            "model": model_id,
            "finish_reason": finish_reason,
            "usage": usage_info,
            "requested_max_tokens": requested_max_tokens,
            "effective_max_tokens": effective_max_tokens,
            "prompt_version": req_bundle["prompt_version"],
            "schema_version": req_bundle["schema_version"],
            "capability_mode": req_bundle["capability"]["response_mode"],
        }


class OptionalGeminiProviderAdapter(BaseProviderAdapter):
    """
    Optional Gemini provider adapter implementing the exact same contract as GroqProviderAdapter (PH05-01, PH05-04, T21).
    - If unconfigured/disabled, never crashes the application and never interferes with Groq.
    - When invoked with an unsupported model or error, raises/returns the same typed error classifications
      and never silently substitutes a different model.
    """
    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_keys: Optional[List[str]] = None,
        enabled: bool = False,
        transport_fn: Optional[Callable[..., Any]] = None,
        json_parser_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
        normalizer_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        usage_extractor_fn: Optional[Callable[[Any], Dict[str, Any]]] = None,
        default_retry_seconds: float = 30.0,
        strict_registry: bool = True,
    ) -> None:
        self.api_keys = [k for k in (api_keys or []) if str(k).strip()]
        self.enabled = bool(enabled)
        self.transport_fn = transport_fn
        self.json_parser_fn = json_parser_fn
        self.normalizer_fn = normalizer_fn
        self.usage_extractor_fn = usage_extractor_fn
        self.default_retry_seconds = float(default_retry_seconds)
        self.strict_registry = bool(strict_registry)

    def is_available(self) -> bool:
        return bool(self.enabled and (len(self.api_keys) > 0 or self.transport_fn is not None))

    def execute_safe(
        self,
        *,
        role: str,
        tier: str,
        model_id: str = "gemini-2.5-flash",
        prompt: str = "",
        api_key: str = "",
        requested_max_tokens: int = 450,
        effective_max_tokens: int = 450,
        strict_role_schema: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute Gemini call safely: if optional provider is disabled or missing keys/transport,
        returns a clean PROVIDER_UNAVAILABLE / WAIT dict without raising or crashing (T21).
        """
        if not self.is_available():
            return {
                "status": "SKIPPED",
                "error_category": "PROVIDER_UNAVAILABLE",
                "decision": "WAIT",
                "confidence": 0.0,
                "reasoning": "Provider opsional Gemini tidak diaktifkan atau belum dikonfigurasi.",
                "key_risk": "Provider opsional tidak aktif.",
                "evidence": [],
                "modes": {},
                "tier": tier,
                "provider": self.provider_name,
                "model": model_id,
                "finish_reason": None,
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "total_tokens": None,
                    "usage_known": False,
                    "cost_status": "UNKNOWN_USAGE",
                },
                "requested_max_tokens": requested_max_tokens,
                "effective_max_tokens": effective_max_tokens,
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION_AI,
            }

        try:
            req_bundle = build_provider_request_payload(
                role=role,
                tier=tier,
                model_id=model_id,
                prompt=prompt,
                max_completion_tokens=effective_max_tokens,
                provider=self.provider_name,
                strict_registry=self.strict_registry,
            )
            key_to_use = api_key or (self.api_keys[0] if self.api_keys else "")
            raw_resp = self.transport_fn(
                model_id,
                prompt,
                key_to_use,
                effective_max_tokens,
                tier,
                role=role,
            )
            choice = raw_resp.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            usage_info = self.usage_extractor_fn(raw_resp) if self.usage_extractor_fn else {
                "input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": None,
                "usage_known": False,
                "cost_status": "UNKNOWN_USAGE",
            }
            content = getattr(choice.message, "content", None) or ""
            if not str(content).strip():
                raise ProviderMalformedOutputError(
                    "Respons Gemini kosong.",
                    provider=self.provider_name,
                    model_id=model_id,
                    finish_reason=finish_reason,
                )
            try:
                obj = self.json_parser_fn(content) if self.json_parser_fn else json.loads(content)
            except Exception as parse_exc:
                raise ProviderMalformedOutputError(
                    f"Respons Gemini bukan JSON valid: {parse_exc}",
                    provider=self.provider_name,
                    model_id=model_id,
                    finish_reason=finish_reason,
                ) from parse_exc

            if self.normalizer_fn is not None:
                norm = self.normalizer_fn(
                    obj,
                    tier,
                    provider=self.provider_name,
                    model=model_id,
                    finish_reason=finish_reason,
                    usage=usage_info,
                    requested_max_tokens=requested_max_tokens,
                    effective_max_tokens=effective_max_tokens,
                    role=role,
                    strict_role_schema=strict_role_schema,
                )
                norm["prompt_version"] = req_bundle["prompt_version"]
                norm["schema_version"] = req_bundle["schema_version"]
                return norm

            return {
                "status": "OK",
                **obj,
                "tier": tier,
                "provider": self.provider_name,
                "model": model_id,
                "finish_reason": finish_reason,
                "usage": usage_info,
                "requested_max_tokens": requested_max_tokens,
                "effective_max_tokens": effective_max_tokens,
                "prompt_version": req_bundle["prompt_version"],
                "schema_version": req_bundle["schema_version"],
            }
        except Exception as exc:
            p_err = coerce_to_provider_error(
                exc,
                provider=self.provider_name,
                model_id=model_id,
                default_retry_seconds=self.default_retry_seconds,
            )
            status_str = (
                "INVALID_OUTPUT"
                if p_err.category == "MALFORMED_OUTPUT"
                else ("BUDGET_EXHAUSTED" if p_err.category == "DAILY_QUOTA_EXHAUSTED" else "FAILED")
            )
            return {
                "status": status_str,
                "error_category": p_err.category,
                "error_details": p_err.to_dict(),
                "decision": "WAIT",
                "confidence": 0.0,
                "reasoning": str(redact_secrets(str(p_err), extra_secrets=self.api_keys))[:500],
                "key_risk": f"Provider {self.provider_name} error ({p_err.category})",
                "evidence": [],
                "modes": {},
                "tier": tier,
                "provider": self.provider_name,
                "model": model_id,
                "finish_reason": getattr(p_err, "finish_reason", None),
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "total_tokens": None,
                    "usage_known": False,
                    "cost_status": "UNKNOWN_USAGE",
                },
                "requested_max_tokens": requested_max_tokens,
                "effective_max_tokens": effective_max_tokens,
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION_AI,
            }
# ---------------------------------------------------------
# Durable Budget Context & Preflight Helpers (PH05-02 / F02, F20, F21 / T20)
# ---------------------------------------------------------
@dataclass
class BudgetContext:
    """
    Context for tracking and reserving LLM budget and physical attempts per candidate analysis run
    (PH05-02 / F02, F20, F21 / T20).
    """
    analysis_id: str
    scope_id: str = "groq_default"
    max_physical_attempts: int = 14
    deadline_epoch: Optional[float] = None
    judge_budget: int = 750
    judge_reserved: bool = False
    reservations: Dict[str, str] = field(default_factory=dict)
    physical_attempts_used: int = 0
    logical_calls_used: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "scope_id": self.scope_id,
            "max_physical_attempts": self.max_physical_attempts,
            "deadline_epoch": self.deadline_epoch,
            "judge_budget": self.judge_budget,
            "judge_reserved": self.judge_reserved,
            "reservations": dict(self.reservations),
            "physical_attempts_used": self.physical_attempts_used,
            "logical_calls_used": self.logical_calls_used,
        }


def create_budget_context(
    analysis_id: str,
    scope_id: str = "groq_default",
    max_physical_attempts: int = 14,
    deadline_epoch: Optional[float] = None,
    judge_budget: int = 750,
) -> BudgetContext:
    """Create a new budget context for a candidate analysis run."""
    return BudgetContext(
        analysis_id=str(analysis_id),
        scope_id=str(scope_id),
        max_physical_attempts=int(max_physical_attempts),
        deadline_epoch=float(deadline_epoch) if deadline_epoch is not None else None,
        judge_budget=int(judge_budget),
    )


def preflight_reserve_judge_budget(
    budget_context: BudgetContext,
    budget_manager: Optional[Any] = None,
    db_file: Optional[str] = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    Reserve budget and physical attempt slot for the Final Judge before running optional agents (PH05-02).
    """
    if budget_manager is None:
        try:
            from .storage import GLOBAL_DURABLE_BUDGET_MANAGER
            budget_manager = GLOBAL_DURABLE_BUDGET_MANAGER
        except ImportError:
            budget_manager = None

    if budget_manager is None:
        budget_context.judge_reserved = True
        return True, "IN_MEMORY_PREFLIGHT_OK", None

    ok, reason, res_id = budget_manager.preflight_reserve_judge(
        analysis_id=budget_context.analysis_id,
        scope_id=budget_context.scope_id,
        judge_budget=budget_context.judge_budget,
        max_physical_attempts=budget_context.max_physical_attempts,
        deadline_epoch=budget_context.deadline_epoch,
        db_file=db_file,
    )
    if ok:
        budget_context.judge_reserved = True
        if res_id:
            budget_context.reservations["preflight_judge"] = res_id
    return ok, reason, res_id


# ---------------------------------------------------------
# Optional Gemini Fallback Routing & Payload Helpers (PH05-04 / M5)
# ---------------------------------------------------------
GEMINI_DEFAULT_TIER_MODELS: Dict[str, str] = {
    "analyst": "gemini-2.5-flash",
    "debate": "gemini-2.5-flash",
    "research": "gemini-2.5-pro",
    "judge": "gemini-2.5-pro",
}


def should_fallback_to_secondary_provider(
    error_or_category: Any,
    *,
    gemini_enabled: bool = False,
    gemini_available: bool = False,
) -> bool:
    """
    Determine whether call_ai may fall back from primary Groq to optional Gemini (PH05-04 / F02, F21 / T21):
    - Returns False immediately if Gemini is disabled or unavailable.
    - Returns False for MODEL_UNSUPPORTED and MALFORMED_OUTPUT so configuration errors and schema violations
      are never silently masked by switching providers.
    - Returns True for transport/quota/auth failures (RATE_LIMIT_TEMPORARY, DAILY_QUOTA_EXHAUSTED,
      AUTH_ERROR, TIMEOUT_OR_NETWORK).
    """
    if not (gemini_enabled and gemini_available):
        return False
    if isinstance(error_or_category, ProviderError):
        return bool(
            error_or_category.allow_fallback_provider
            and error_or_category.category not in {"MODEL_UNSUPPORTED", "MALFORMED_OUTPUT"}
        )
    cat = str(error_or_category or "").strip().upper()
    return cat in {
        "RATE_LIMIT_TEMPORARY",
        "DAILY_QUOTA_EXHAUSTED",
        "AUTH_ERROR",
        "TIMEOUT_OR_NETWORK",
    }


def build_gemini_http_payload(
    *,
    role: str,
    tier: str,
    model_id: str,
    prompt: str,
    max_completion_tokens: int,
    temperature: float = 0.1,
    strict_registry: bool = True,
) -> Dict[str, Any]:
    """
    Build a capability-verified Gemini generateContent REST/SDK payload with role-specific JSON schema
    and version metadata (PH05-04, F18, T21).
    """
    req_bundle = build_provider_request_payload(
        role=role,
        tier=tier,
        model_id=model_id,
        prompt=prompt,
        max_completion_tokens=max_completion_tokens,
        provider="gemini",
        temperature=temperature,
        strict_registry=strict_registry,
    )
    eff_tokens = req_bundle["sdk_kwargs"]["max_completion_tokens"]
    target_schema = req_bundle["schema"]
    return {
        "provider": "gemini",
        "model_id": req_bundle["model_id"],
        "role": role,
        "tier": tier,
        "is_judge_role": req_bundle["is_judge_role"],
        "capability": req_bundle["capability"],
        "prompt_version": req_bundle["prompt_version"],
        "schema_version": req_bundle["schema_version"],
        "strategy_version": req_bundle["strategy_version"],
        "http_body": {
            "contents": [{" role": "user", "parts": [{"text": str(prompt)}]}],
            "generationConfig": {
                "temperature": float(temperature),
                "maxOutputTokens": int(eff_tokens),
                "responseMimeType": "application/json",
                "responseSchema": target_schema,
            },
        },
    }

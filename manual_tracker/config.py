from dataclasses import dataclass, field
from decimal import Decimal
import math
import os
import re


def ids(value):
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[\s,]+", str(value or ""))
    return frozenset(int(x) for x in values if str(x).strip())


def redact_text(text, secrets=()):
    out = str(text or "")
    for s in secrets:
        s_str = str(s or "").strip()
        if len(s_str) >= 4:
            out = out.replace(s_str, "[REDACTED]")
    out = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot[REDACTED]", out)
    out = re.sub(r"(signature=)[0-9a-fA-F]{16,}", r"\1[REDACTED]", out)
    return out


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    token: str = field(default="", repr=False)
    chat_id: str = ""
    users: frozenset = frozenset()
    chats: frozenset = frozenset()
    poll_seconds: float = 5.0
    stale_seconds: float = 20.0
    signal_ttl: float = 86400.0
    session_ttl: float = 900.0
    activation_r: Decimal = Decimal("1")
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("2")
    atr_interval: str = "1m"
    min_ticks: int = 2
    min_improvement_r: Decimal = Decimal("0.05")
    cost_buffer_bps: Decimal = Decimal("5")
    requests_per_minute: int = 120

    def __post_init__(self):
        for name in ("poll_seconds", "stale_seconds", "signal_ttl", "session_ttl"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"Konfigurasi {name} harus positif dan finite.")
        if self.poll_seconds < 2 or self.stale_seconds <= self.poll_seconds:
            raise ValueError("Polling minimal 2 detik; stale_seconds harus lebih besar dari polling.")
        for name in ("activation_r", "atr_multiplier", "min_improvement_r", "cost_buffer_bps"):
            v = getattr(self, name)
            if not v.is_finite() or v < 0:
                raise ValueError(f"Konfigurasi {name} tidak valid.")
        if self.activation_r <= 0 or self.atr_multiplier <= 0 or self.cost_buffer_bps > 1000:
            raise ValueError("Aktivasi, ATR, atau buffer biaya tidak valid.")
        if not 2 <= self.atr_period <= 100 or self.min_ticks < 1:
            raise ValueError("Periode ATR 2..100 dan min_ticks >= 1.")
        if self.atr_interval not in {"1m", "5m", "15m", "1h"}:
            raise ValueError("Interval ATR didukung: 1m, 5m, 15m, 1h.")
        if not 10 <= self.requests_per_minute <= 600:
            raise ValueError("Batas permintaan per menit 10..600.")

    @classmethod
    def load(cls, getter=None):
        get = getter or os.getenv
        def v(key, default):
            val = get(key)
            return default if val is None or val == "" else val
        chat = str(v("TELEGRAM_CHAT_ID", ""))
        return cls(
            enabled=str(v("MANUAL_TRACKING_ENABLED", "false")).lower() in {"true", "1", "yes", "on"},
            token=str(v("TELEGRAM_BOT_TOKEN", "")), chat_id=chat,
            users=ids(v("MANUAL_TELEGRAM_USER_IDS", "")),
            chats=ids(v("MANUAL_TELEGRAM_CHAT_IDS", chat)),
            poll_seconds=float(v("MANUAL_POLL_SECONDS", 5)),
            stale_seconds=float(v("MANUAL_STALE_SECONDS", 20)),
            signal_ttl=float(v("MANUAL_SIGNAL_TTL_SECONDS", 86400)),
            session_ttl=float(v("MANUAL_SESSION_TTL_SECONDS", 900)),
            activation_r=Decimal(str(v("MANUAL_ACTIVATION_R", 1))),
            atr_period=int(v("MANUAL_ATR_PERIOD", 14)),
            atr_multiplier=Decimal(str(v("MANUAL_ATR_MULTIPLIER", 2))),
            atr_interval=str(v("MANUAL_ATR_INTERVAL", "1m")),
            min_ticks=int(v("MANUAL_MIN_IMPROVEMENT_TICKS", 2)),
            min_improvement_r=Decimal(str(v("MANUAL_MIN_IMPROVEMENT_R", "0.05"))),
            cost_buffer_bps=Decimal(str(v("MANUAL_COST_BUFFER_BPS", 5))),
            requests_per_minute=int(v("MANUAL_REQUESTS_PER_MINUTE", 120)),
        )

    def require_worker(self):
        if not self.enabled or not self.token or not self.users or not self.chats:
            raise ValueError("Aktifkan tracker, token Telegram, serta allowlist user dan chat numerik.")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token):
            raise ValueError("Format token Telegram tidak valid.")
        if not re.fullmatch(r"-?[0-9]+", self.chat_id) or int(self.chat_id) not in self.chats:
            raise ValueError("TELEGRAM_CHAT_ID harus numerik dan tercantum dalam allowlist chat.")

    def authorized(self, user, chat):
        return self.enabled and int(user) in self.users and int(chat) in self.chats


@dataclass(frozen=True)
class AutonomousConfig:
    """Configuration for Phases 1-6 Autonomous Trading System.

    Real-money trading (`EXECUTION_MODE=LIVE` and `LIVE_TRADING_ENABLED=true`)
    is disabled by default and never auto-activates.
    """
    execution_mode: str = "PAPER"  # PAPER | TESTNET | LIVE
    live_trading_enabled: bool = False
    autonomous_enabled: bool = False
    default_instrument: str = "LINEAR"  # LINEAR | SPOT
    default_leverage: Decimal = Decimal("3")
    margin_mode: str = "ISOLATED"

    # Trailing stop mode: ADVISORY (default) | AUTOMATIC
    trailing_mode: str = "ADVISORY"
    auto_trailing_enabled: bool = False

    # Emergency and Kill-Switch policies
    emergency_unprotected_policy: str = "ALERT_AND_HOLD"  # ALERT_AND_HOLD | EMERGENCY_MARKET_CLOSE
    kill_switch_pending_policy: str = "CANCEL_PENDING_ENTRIES"  # CANCEL_PENDING_ENTRIES | KEEP_PENDING

    # Risk Management parameters (Phase 2)
    risk_pct_per_trade: Decimal = Decimal("0.005")          # 0.5% per trade
    max_daily_loss_pct: Decimal = Decimal("0.02")           # 2.0% daily drawdown limit
    max_daily_loss_usdt: Decimal = Decimal("500")           # Hard USDT daily cap
    max_aggregate_open_risk_pct: Decimal = Decimal("0.015") # 1.5% max total open risk
    max_concurrent_positions: int = 3
    max_exposure_per_asset_pct: Decimal = Decimal("0.20")   # 20% of equity per asset
    max_portfolio_exposure_pct: Decimal = Decimal("0.60")   # 60% of equity total notional/leverage-adjusted
    max_permitted_leverage: Decimal = Decimal("5")
    min_acceptable_rr: Decimal = Decimal("1.5")
    max_position_notional_usdt: Decimal = Decimal("25000")
    max_consecutive_losses: int = 3
    max_execution_failures: int = 3
    max_fee_share_of_risk: Decimal = Decimal("0.40")

    # Freshness and latency thresholds
    max_market_data_age_seconds: float = 20.0
    max_account_data_age_seconds: float = 30.0
    max_signal_age_seconds: float = 120.0
    reconcile_interval_seconds: float = 30.0
    abnormal_latency_ms: float = 5000.0

    # Cost & Simulation assumptions (Phase 3)
    paper_initial_equity: Decimal = Decimal("10000")
    default_maker_fee: Decimal = Decimal("0.0002")
    default_taker_fee: Decimal = Decimal("0.0004")
    default_slippage_bps: Decimal = Decimal("5")
    default_spread_bps: Decimal = Decimal("2")
    default_funding_bps: Decimal = Decimal("1")
    require_ai_approval: bool = True

    # Credentials (never printed in repr or logs)
    binance_api_key: str = field(default="", repr=False)
    binance_api_secret: str = field(default="", repr=False)
    binance_testnet_api_key: str = field(default="", repr=False)
    binance_testnet_api_secret: str = field(default="", repr=False)

    def __post_init__(self):
        if self.execution_mode not in {"PAPER", "TESTNET", "LIVE"}:
            raise ValueError("EXECUTION_MODE harus PAPER, TESTNET, atau LIVE.")
        if self.default_instrument not in {"SPOT", "LINEAR"}:
            raise ValueError("DEFAULT_INSTRUMENT harus SPOT atau LINEAR.")
        if self.trailing_mode not in {"ADVISORY", "AUTOMATIC"}:
            raise ValueError("TRAILING_MODE harus ADVISORY atau AUTOMATIC.")
        if self.emergency_unprotected_policy not in {"ALERT_AND_HOLD", "EMERGENCY_MARKET_CLOSE"}:
            raise ValueError("EMERGENCY_UNPROTECTED_POLICY harus ALERT_AND_HOLD atau EMERGENCY_MARKET_CLOSE.")
        if self.kill_switch_pending_policy not in {"CANCEL_PENDING_ENTRIES", "KEEP_PENDING"}:
            raise ValueError("KILL_SWITCH_PENDING_POLICY harus CANCEL_PENDING_ENTRIES atau KEEP_PENDING.")
        if not (Decimal("0.0001") <= self.risk_pct_per_trade <= Decimal("0.05")):
            raise ValueError("risk_pct_per_trade harus antara 0.0001 (0.01%) dan 0.05 (5%).")
        if not (Decimal("0.001") <= self.max_daily_loss_pct <= Decimal("0.20")):
            raise ValueError("max_daily_loss_pct harus antara 0.001 dan 0.20.")
        if self.max_daily_loss_usdt <= 0:
            raise ValueError("max_daily_loss_usdt harus positif.")
        if not (Decimal("0.0001") <= self.max_aggregate_open_risk_pct <= Decimal("0.20")):
            raise ValueError("max_aggregate_open_risk_pct harus antara 0.0001 dan 0.20.")
        if self.max_concurrent_positions < 1 or self.max_concurrent_positions > 50:
            raise ValueError("max_concurrent_positions harus 1..50.")
        if not (Decimal("1") <= self.max_permitted_leverage <= Decimal("125")):
            raise ValueError("max_permitted_leverage harus 1..125.")
        if self.default_leverage < Decimal("1") or self.default_leverage > self.max_permitted_leverage:
            raise ValueError("default_leverage tidak boleh melebihi max_permitted_leverage.")
        if self.min_acceptable_rr <= Decimal("0"):
            raise ValueError("min_acceptable_rr harus positif.")
        if self.max_position_notional_usdt <= Decimal("0"):
            raise ValueError("max_position_notional_usdt harus positif.")
        for fname in ("max_market_data_age_seconds", "max_account_data_age_seconds", "max_signal_age_seconds", "reconcile_interval_seconds"):
            val = getattr(self, fname)
            if not math.isfinite(val) or val <= 0:
                raise ValueError(f"{fname} harus positif dan finite.")

    @classmethod
    def load(cls, getter=None):
        get = getter or os.getenv
        def v(key, default):
            val = get(key)
            return default if val is None or val == "" else val
        def b(key, default="false"):
            return str(v(key, default)).strip().lower() in {"true", "1", "yes", "on"}
        return cls(
            execution_mode=str(v("EXECUTION_MODE", "PAPER")).strip().upper(),
            live_trading_enabled=b("LIVE_TRADING_ENABLED", "false"),
            autonomous_enabled=b("AUTONOMOUS_TRADING_ENABLED", "false"),
            default_instrument=str(v("DEFAULT_INSTRUMENT", "LINEAR")).strip().upper(),
            default_leverage=Decimal(str(v("DEFAULT_LEVERAGE", "3"))),
            margin_mode=str(v("DEFAULT_MARGIN_MODE", "ISOLATED")).strip().upper(),
            trailing_mode=str(v("TRAILING_MODE", "ADVISORY")).strip().upper(),
            auto_trailing_enabled=b("AUTO_TRAILING_ENABLED", "false"),
            emergency_unprotected_policy=str(v("EMERGENCY_UNPROTECTED_POLICY", "ALERT_AND_HOLD")).strip().upper(),
            kill_switch_pending_policy=str(v("KILL_SWITCH_PENDING_POLICY", "CANCEL_PENDING_ENTRIES")).strip().upper(),
            risk_pct_per_trade=Decimal(str(v("RISK_PCT_PER_TRADE", "0.005"))),
            max_daily_loss_pct=Decimal(str(v("MAX_DAILY_LOSS_PCT", "0.02"))),
            max_daily_loss_usdt=Decimal(str(v("MAX_DAILY_LOSS_USDT", "500"))),
            max_aggregate_open_risk_pct=Decimal(str(v("MAX_AGGREGATE_OPEN_RISK_PCT", "0.015"))),
            max_concurrent_positions=int(v("MAX_CONCURRENT_POSITIONS", 3)),
            max_exposure_per_asset_pct=Decimal(str(v("MAX_EXPOSURE_PER_ASSET_PCT", "0.20"))),
            max_portfolio_exposure_pct=Decimal(str(v("MAX_PORTFOLIO_EXPOSURE_PCT", "0.60"))),
            max_permitted_leverage=Decimal(str(v("MAX_PERMITTED_LEVERAGE", "5"))),
            min_acceptable_rr=Decimal(str(v("MIN_ACCEPTABLE_RR", "1.5"))),
            max_position_notional_usdt=Decimal(str(v("MAX_POSITION_NOTIONAL_USDT", "25000"))),
            max_consecutive_losses=int(v("MAX_CONSECUTIVE_LOSSES", 3)),
            max_execution_failures=int(v("MAX_EXECUTION_FAILURES", 3)),
            max_fee_share_of_risk=Decimal(str(v("MAX_FEE_SHARE_OF_RISK", "0.40"))),
            max_market_data_age_seconds=float(v("MAX_MARKET_DATA_AGE_SECONDS", 20.0)),
            max_account_data_age_seconds=float(v("MAX_ACCOUNT_DATA_AGE_SECONDS", 30.0)),
            max_signal_age_seconds=float(v("MAX_SIGNAL_AGE_SECONDS", 120.0)),
            reconcile_interval_seconds=float(v("RECONCILE_INTERVAL_SECONDS", 30.0)),
            abnormal_latency_ms=float(v("ABNORMAL_LATENCY_MS", 5000.0)),
            paper_initial_equity=Decimal(str(v("PAPER_INITIAL_EQUITY", "10000"))),
            default_maker_fee=Decimal(str(v("DEFAULT_MAKER_FEE", "0.0002"))),
            default_taker_fee=Decimal(str(v("DEFAULT_TAKER_FEE", "0.0004"))),
            default_slippage_bps=Decimal(str(v("DEFAULT_SLIPPAGE_BPS", "5"))),
            default_spread_bps=Decimal(str(v("DEFAULT_SPREAD_BPS", "2"))),
            default_funding_bps=Decimal(str(v("DEFAULT_FUNDING_BPS", "1"))),
            require_ai_approval=b("REQUIRE_AI_APPROVAL", "true"),
            binance_api_key=str(v("BINANCE_API_KEY", "")),
            binance_api_secret=str(v("BINANCE_API_SECRET", "")),
            binance_testnet_api_key=str(v("BINANCE_TESTNET_API_KEY", "")),
            binance_testnet_api_secret=str(v("BINANCE_TESTNET_API_SECRET", "")),
        )

    def active_credentials(self):
        if self.execution_mode == "TESTNET":
            return self.binance_testnet_api_key, self.binance_testnet_api_secret
        if self.execution_mode == "LIVE":
            return self.binance_api_key, self.binance_api_secret
        return "", ""

    def secrets_tuple(self):
        return tuple(
            s for s in (
                self.binance_api_key,
                self.binance_api_secret,
                self.binance_testnet_api_key,
                self.binance_testnet_api_secret,
            ) if s
        )

    def is_auto_trailing_authorized(self):
        return self.trailing_mode == "AUTOMATIC" and self.auto_trailing_enabled

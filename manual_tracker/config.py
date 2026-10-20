from dataclasses import dataclass, field
from decimal import Decimal
import os
import re


def ids(value):
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[\s,]+", str(value or ""))
    return frozenset(int(x) for x in values if str(x).strip())


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
        import math
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

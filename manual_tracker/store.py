"""Additive SQLite schema. Never changes v5.5 signals or paper tables."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import time
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS mt_schema(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mt_signals(
 id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, source_signal_id INTEGER NOT NULL,
 snapshot TEXT NOT NULL, chat_id INTEGER NOT NULL, owner_id INTEGER,
 state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 message_id INTEGER
);
CREATE TABLE IF NOT EXISTS mt_positions(
 id TEXT PRIMARY KEY, signal_id TEXT UNIQUE NOT NULL REFERENCES mt_signals(id),
 owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, details TEXT NOT NULL,
 state TEXT NOT NULL, recommended_stop TEXT NOT NULL, best_price TEXT NOT NULL,
 activated INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'WAITING_1R',
 last_price TEXT, last_price_at REAL, atr TEXT, metrics TEXT,
 data_quality TEXT NOT NULL DEFAULT 'AWAITING_DATA', last_error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 last_event_id TEXT, last_notified_stop TEXT, exit_price TEXT, closed_at REAL,
 closure_source TEXT, previous_state TEXT
);
CREATE INDEX IF NOT EXISTS mt_positions_state ON mt_positions(state);
CREATE TABLE IF NOT EXISTS mt_instruments(
 instrument TEXT NOT NULL, symbol TEXT NOT NULL, tick TEXT,
 checked_at REAL NOT NULL, last_error TEXT, PRIMARY KEY(instrument,symbol)
);
CREATE TABLE IF NOT EXISTS mt_sessions(
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, nonce TEXT UNIQUE NOT NULL,
 signal_id TEXT, position_id TEXT, step TEXT NOT NULL, data TEXT NOT NULL,
 prompt_event TEXT, expires_at REAL NOT NULL, PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS mt_updates(
 bot_id TEXT NOT NULL, update_id INTEGER NOT NULL, callback_id TEXT,
 outcome TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(bot_id,update_id),
 UNIQUE(bot_id,callback_id)
);
CREATE TABLE IF NOT EXISTS mt_runtime(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mt_events(
 id TEXT PRIMARY KEY, signal_id TEXT, position_id TEXT, kind TEXT NOT NULL,
 detail TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mt_outbox(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'sendMessage',
 payload TEXT NOT NULL, signal_id TEXT, position_id TEXT, stop TEXT,
 state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, expires_at REAL, next_attempt REAL NOT NULL DEFAULT 0,
 message_id INTEGER, last_error TEXT, sent_at REAL
);
CREATE INDEX IF NOT EXISTS mt_outbox_pending ON mt_outbox(state,next_attempt);

-- Phase 1-6 Autonomous Trading System Additive Schema (at_*)
CREATE TABLE IF NOT EXISTS at_account_snapshots(
 id TEXT PRIMARY KEY,
 environment TEXT NOT NULL,
 instrument TEXT NOT NULL,
 quote_asset TEXT NOT NULL DEFAULT 'USDT',
 total_wallet_balance TEXT NOT NULL,
 available_balance TEXT NOT NULL,
 account_equity TEXT NOT NULL,
 unrealized_pnl TEXT NOT NULL,
 realized_pnl TEXT NOT NULL,
 used_margin TEXT NOT NULL,
 available_margin TEXT NOT NULL,
 open_positions_json TEXT NOT NULL,
 active_orders_json TEXT NOT NULL,
 permissions_json TEXT NOT NULL,
 health_status TEXT NOT NULL,
 exchange_timestamp REAL NOT NULL,
 synced_at REAL NOT NULL,
 last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_at_account_env_inst ON at_account_snapshots(environment, instrument, synced_at DESC);

CREATE TABLE IF NOT EXISTS at_instrument_specs(
 instrument TEXT NOT NULL,
 symbol TEXT NOT NULL,
 status TEXT NOT NULL,
 base_asset TEXT NOT NULL,
 quote_asset TEXT NOT NULL,
 margin_asset TEXT NOT NULL,
 contract_type TEXT NOT NULL,
 contract_multiplier TEXT NOT NULL,
 price_precision INTEGER NOT NULL,
 quantity_precision INTEGER NOT NULL,
 tick_size TEXT NOT NULL,
 step_size TEXT NOT NULL,
 min_qty TEXT NOT NULL,
 max_qty TEXT NOT NULL,
 min_notional TEXT NOT NULL,
 max_leverage INTEGER NOT NULL,
 maker_fee TEXT NOT NULL,
 taker_fee TEXT NOT NULL,
 funding_rate TEXT NOT NULL DEFAULT '0',
 checked_at REAL NOT NULL,
 raw_json TEXT NOT NULL,
 PRIMARY KEY(instrument, symbol)
);

CREATE TABLE IF NOT EXISTS at_risk_state(
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL,
 updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS at_daily_risk_ledger(
 day_utc TEXT NOT NULL,
 environment TEXT NOT NULL,
 starting_equity TEXT NOT NULL,
 realized_pnl_gross TEXT NOT NULL DEFAULT '0',
 realized_pnl_net TEXT NOT NULL DEFAULT '0',
 unrealized_pnl TEXT NOT NULL DEFAULT '0',
 fees_paid TEXT NOT NULL DEFAULT '0',
 funding_paid TEXT NOT NULL DEFAULT '0',
 trades_count INTEGER NOT NULL DEFAULT 0,
 wins_count INTEGER NOT NULL DEFAULT 0,
 losses_count INTEGER NOT NULL DEFAULT 0,
 consecutive_losses INTEGER NOT NULL DEFAULT 0,
 max_drawdown_usdt TEXT NOT NULL DEFAULT '0',
 daily_limit_breached INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL,
 PRIMARY KEY(day_utc, environment)
);

CREATE TABLE IF NOT EXISTS at_circuit_breakers(
 breaker_id TEXT PRIMARY KEY,
 active INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL,
 detail TEXT NOT NULL,
 tripped_at REAL,
 updated_at REAL NOT NULL,
 reset_by TEXT,
 reset_at REAL
);

CREATE TABLE IF NOT EXISTS at_orders(
 client_order_id TEXT PRIMARY KEY,
 exchange_order_id TEXT,
 environment TEXT NOT NULL,
 position_id TEXT,
 signal_id TEXT,
 instrument TEXT NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 position_side TEXT NOT NULL DEFAULT 'BOTH',
 order_type TEXT NOT NULL,
 role TEXT NOT NULL,
 requested_qty TEXT NOT NULL,
 filled_qty TEXT NOT NULL DEFAULT '0',
 price TEXT,
 stop_price TEXT,
 avg_fill_price TEXT,
 fee_usdt TEXT NOT NULL DEFAULT '0',
 reduce_only INTEGER NOT NULL DEFAULT 0,
 close_position INTEGER NOT NULL DEFAULT 0,
 order_state TEXT NOT NULL,
 raw_response TEXT,
 last_error TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_orders_pos ON at_orders(position_id, role);
CREATE INDEX IF NOT EXISTS idx_at_orders_state ON at_orders(environment, order_state);

CREATE TABLE IF NOT EXISTS at_fills(
 fill_id TEXT PRIMARY KEY,
 client_order_id TEXT NOT NULL,
 exchange_order_id TEXT,
 position_id TEXT,
 environment TEXT NOT NULL,
 instrument TEXT NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 qty TEXT NOT NULL,
 price TEXT NOT NULL,
 fee_usdt TEXT NOT NULL DEFAULT '0',
 fee_asset TEXT NOT NULL DEFAULT 'USDT',
 realized_pnl TEXT NOT NULL DEFAULT '0',
 executed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS at_positions(
 id TEXT PRIMARY KEY,
 signal_id TEXT UNIQUE,
 strategy_version TEXT NOT NULL DEFAULT 'v5.5',
 environment TEXT NOT NULL,
 instrument TEXT NOT NULL,
 symbol TEXT NOT NULL,
 timeframe TEXT NOT NULL DEFAULT '5m/15m/1h',
 direction TEXT NOT NULL,
 signal_state TEXT NOT NULL,
 order_state TEXT NOT NULL,
 position_state TEXT NOT NULL,
 protective_order_state TEXT NOT NULL,
 account_state TEXT NOT NULL,
 trailing_mode TEXT NOT NULL DEFAULT 'ADVISORY',
 trailing_stage TEXT NOT NULL DEFAULT 'WAITING_1R',
 entry_price TEXT NOT NULL,
 quantity TEXT NOT NULL,
 multiplier TEXT NOT NULL DEFAULT '1',
 leverage TEXT,
 margin_mode TEXT NOT NULL DEFAULT 'ISOLATED',
 initial_stop TEXT NOT NULL,
 current_stop TEXT NOT NULL,
 recommended_stop TEXT NOT NULL,
 last_confirmed_exchange_stop TEXT,
 take_profit TEXT,
 best_price TEXT NOT NULL,
 activated INTEGER NOT NULL DEFAULT 0,
 sl_client_order_id TEXT,
 tp_client_order_id TEXT,
 entry_fee_rate TEXT NOT NULL DEFAULT '0.0004',
 exit_fee_rate TEXT NOT NULL DEFAULT '0.0004',
 fees_paid_usdt TEXT NOT NULL DEFAULT '0',
 funding_paid_usdt TEXT NOT NULL DEFAULT '0',
 gross_pnl_usdt TEXT NOT NULL DEFAULT '0',
 net_pnl_usdt TEXT NOT NULL DEFAULT '0',
 r_multiple TEXT NOT NULL DEFAULT '0',
 initial_risk_usdt TEXT NOT NULL,
 exit_price TEXT,
 exit_reason TEXT,
 requires_attention INTEGER NOT NULL DEFAULT 0,
 last_price TEXT,
 last_price_at REAL,
 atr TEXT,
 last_error TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 closed_at REAL,
 details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_positions_env_state ON at_positions(environment, position_state);

CREATE TABLE IF NOT EXISTS at_reconciliation_log(
 id TEXT PRIMARY KEY,
 trigger_source TEXT NOT NULL,
 environment TEXT NOT NULL,
 instrument TEXT NOT NULL,
 status TEXT NOT NULL,
 discrepancies_json TEXT NOT NULL,
 actions_json TEXT NOT NULL,
 created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS at_audit_events(
 id TEXT PRIMARY KEY,
 environment TEXT NOT NULL,
 category TEXT NOT NULL,
 event_type TEXT NOT NULL,
 signal_id TEXT,
 position_id TEXT,
 client_order_id TEXT,
 severity TEXT NOT NULL DEFAULT 'INFO',
 payload_json TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_audit_env_cat ON at_audit_events(environment, category, created_at DESC);
"""

TRANSITIONS = {
    "SIGNAL_SENT": {"IGNORED", "AWAITING_ENTRY_DETAILS"},
    "AWAITING_ENTRY_DETAILS": {"SIGNAL_SENT", "IGNORED", "ACTIVE"},
    "ACTIVE": {"PAUSED", "EXIT_REPORTED", "TRACKING_ERROR", "INVALID_DATA"},
    "PAUSED": {"ACTIVE", "EXIT_REPORTED"},
    "TRACKING_ERROR": {"ACTIVE", "PAUSED", "INVALID_DATA", "EXIT_REPORTED"},
    "INVALID_DATA": {"ACTIVE", "PAUSED", "TRACKING_ERROR", "EXIT_REPORTED"},
    "EXIT_REPORTED": {"CLOSED", "PAUSED"},
    "IGNORED": set(), "CLOSED": set(),
}

JSON_COLUMNS = (
    "details", "snapshot", "metrics", "data", "payload",
    "open_positions_json", "active_orders_json", "permissions_json",
    "raw_json", "raw_response", "details_json", "discrepancies_json",
    "actions_json", "payload_json",
)


def transition(old, new):
    if old != new and new not in TRANSITIONS.get(old, set()):
        raise ValueError(f"Transisi {old} ke {new} tidak diizinkan.")


def uid():
    return uuid.uuid4().hex[:20]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def record(row):
    if row is None:
        return None
    d = dict(row)
    for key in JSON_COLUMNS:
        if key in d and d[key] is not None and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    return d


class Store:
    def __init__(self, path="trading_v5.db", clock=time.time):
        self.path, self.clock = str(path), clock
        self.migrate()

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @contextmanager
    def tx(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def migrate(self):
        # CREATE-only migration, independent of the legacy schema version.
        con = self.connect()
        try:
            con.executescript(
                "BEGIN IMMEDIATE;\n"
                + SCHEMA
                + "\nINSERT OR IGNORE INTO mt_schema VALUES(1, strftime('%s','now'));\n"
                + "INSERT OR IGNORE INTO mt_schema VALUES(2, strftime('%s','now'));\n"
                + "COMMIT;"
            )
        finally:
            con.close()

    def rows(self, sql, params=()):
        con = self.connect()
        try:
            return [record(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def position(self, pid):
        return self.one("SELECT * FROM mt_positions WHERE id=?", (pid,))

    def at_position(self, pid):
        return self.one("SELECT * FROM at_positions WHERE id=?", (pid,))

    def runtime(self, key, value, con=None):
        if con is None:
            with self.tx() as c:
                self.runtime(key, value, c)
            return
        con.execute("INSERT INTO mt_runtime VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, dumps(value), self.clock()))

    def risk_state(self, key, value=None, default=None, con=None):
        if value is None:
            if con is not None:
                row = con.execute("SELECT value FROM at_risk_state WHERE key=?", (key,)).fetchone()
            else:
                row = self.one("SELECT value FROM at_risk_state WHERE key=?", (key,))
            if not row:
                return default
            try:
                return json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
            except Exception:
                return row["value"]
        if con is None:
            with self.tx() as c:
                self.risk_state(key, value, default, c)
            return value
        con.execute(
            "INSERT INTO at_risk_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, dumps(value), self.clock()),
        )
        return value

    def audit(self, category, event_type, payload, environment="PAPER", signal_id=None, position_id=None, client_order_id=None, severity="INFO", event_id=None, con=None):
        if con is None:
            with self.tx() as c:
                return self.audit(category, event_type, payload, environment, signal_id, position_id, client_order_id, severity, event_id, c)
        eid = event_id or uid()
        con.execute(
            """INSERT OR IGNORE INTO at_audit_events
               (id, environment, category, event_type, signal_id, position_id, client_order_id, severity, payload_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (eid, environment, category, event_type, str(signal_id) if signal_id is not None else None,
             position_id, client_order_id, severity, dumps(payload), self.clock()),
        )
        return eid

    def event(self, con, key, kind, detail, sid=None, pid=None):
        con.execute("INSERT OR IGNORE INTO mt_events VALUES(?,?,?,?,?,?)",
                    (key, sid, pid, kind, dumps(detail), self.clock()))

    def queue(self, con, key, chat, text, markup=None, kind="INFO", sid=None, pid=None, stop=None, ttl=None, method="sendMessage", payload=None):
        body = payload if payload is not None else {"chat_id": chat, "text": text[:4000]}
        if markup is not None:
            body["reply_markup"] = markup
        con.execute("""INSERT OR IGNORE INTO mt_outbox
            (id,kind,method,payload,signal_id,position_id,stop,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (key, kind, method, dumps(body), sid, pid, str(stop) if stop is not None else None,
             self.clock(), self.clock() + ttl if ttl else None))

    def set_state(self, con, p, state, reason="USER_REPORTED"):
        transition(p["state"], state)
        now = self.clock()
        con.execute("UPDATE mt_positions SET state=?,updated_at=?,revision=revision+1 WHERE id=?", (state, now, p["id"]))
        con.execute("UPDATE mt_signals SET state=? WHERE id=?", (state, p["signal_id"]))
        self.event(con, uid(), state, reason, p["signal_id"], p["id"])
        if state not in {"ACTIVE", "TRACKING_ERROR", "INVALID_DATA"}:
            con.execute("UPDATE mt_outbox SET state='SUPERSEDED' WHERE position_id=? AND kind='RECOMMENDATION' AND state='PENDING'", (p["id"],))

    def expire_sessions(self, con):
        for row in con.execute("SELECT * FROM mt_sessions WHERE expires_at < ?", (self.clock(),)).fetchall():
            s = record(row)
            if s["position_id"]:
                p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (s["position_id"],)).fetchone())
                if p and p["state"] == "EXIT_REPORTED":
                    self.set_state(con, p, "PAUSED", "CLOSE_CONFIRMATION_EXPIRED")
            else:
                con.execute("UPDATE mt_signals SET state='SIGNAL_SENT',owner_id=NULL WHERE id=? AND state='AWAITING_ENTRY_DETAILS'", (s["signal_id"],))
            con.execute("DELETE FROM mt_sessions WHERE nonce=?", (s["nonce"],))

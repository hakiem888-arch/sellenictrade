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
    for key in ("details", "snapshot", "metrics", "data", "payload"):
        if key in d and d[key] is not None:
            d[key] = json.loads(d[key])
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
            con.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nINSERT OR IGNORE INTO mt_schema VALUES(1, strftime('%s','now'));\nCOMMIT;")
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

    def runtime(self, key, value, con=None):
        if con is None:
            with self.tx() as c:
                self.runtime(key, value, c)
            return
        con.execute("INSERT INTO mt_runtime VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, dumps(value), self.clock()))

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

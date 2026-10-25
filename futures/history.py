import json
import sqlite3
from pathlib import Path
from dataclasses import asdict


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, at_ms INTEGER, symbol TEXT, mode TEXT, status TEXT, payload TEXT)")
    return conn


def save(path, result, final_status, ai):
    data = {"baseline": asdict(result), "final_status": final_status, "ai": ai}
    meta = result.meta
    ident = f"{meta['symbol']}:{meta['mode']}:{meta['time_ms']}"
    with connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?)", (ident, meta["time_ms"], meta["symbol"], meta["mode"], final_status,
                                                                             json.dumps(data, allow_nan=False)))
    return data


def load(path, limit=100):
    with connect(path) as conn:
        return [json.loads(x[0]) for x in conn.execute("SELECT payload FROM analyses ORDER BY at_ms DESC LIMIT ?", (limit,))]



def is_alert_sent(path: Path, alert_id: str) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_alerts ("
            "alert_id TEXT PRIMARY KEY, at_ms INTEGER, symbol TEXT, mode TEXT, direction TEXT, message TEXT)"
        )
        row = conn.execute("SELECT 1 FROM sent_alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        return row is not None


def record_alert_sent(path: Path, alert_id: str, at_ms: int, symbol: str, mode: str, direction: str, message: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_alerts ("
            "alert_id TEXT PRIMARY KEY, at_ms INTEGER, symbol TEXT, mode TEXT, direction TEXT, message TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO sent_alerts (alert_id, at_ms, symbol, mode, direction, message) VALUES (?, ?, ?, ?, ?, ?)",
            (alert_id, int(at_ms), symbol, mode, direction, message),
        )


def load_sent_alerts(path: Path, limit: int = 30):
    path = Path(path)
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_alerts ("
            "alert_id TEXT PRIMARY KEY, at_ms INTEGER, symbol TEXT, mode TEXT, direction TEXT, message TEXT)"
        )
        rows = conn.execute(
            "SELECT alert_id, at_ms, symbol, mode, direction, message FROM sent_alerts ORDER BY at_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [
        {"alert_id": r[0], "at_ms": r[1], "symbol": r[2], "mode": r[3], "direction": r[4], "message": r[5]}
        for r in rows
    ]

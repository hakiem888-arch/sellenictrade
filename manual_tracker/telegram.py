"""Telegram API and durable outbox. Ambiguous sends are never auto-retried."""
from decimal import Decimal
import hashlib
import json
import socket
import time
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .engine import fmt
from .store import dumps, record, uid, utc


class TelegramError(Exception):
    def __init__(self, code="NETWORK", retry_after=None, ambiguous=False):
        self.code, self.retry_after, self.ambiguous = code, retry_after, ambiguous
        super().__init__(f"TELEGRAM_{code}")


class TelegramAPI:
    METHODS = {"sendMessage", "getUpdates", "getWebhookInfo", "answerCallbackQuery"}

    def __init__(self, token, opener=urlopen):
        self._token, self._open = token, opener

    def call(self, method, payload):
        if method not in self.METHODS or not self._token:
            raise TelegramError("CONFIG")
        req = Request(f"https://api.telegram.org/bot{self._token}/{method}",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._open(req, timeout=35 if method == "getUpdates" else 10) as r:
                data = json.load(r)
        except HTTPError as e:
            retry = None
            try:
                data = json.load(e)
                retry = data.get("parameters", {}).get("retry_after")
            except (ValueError, TypeError):
                pass
            raise TelegramError(e.code, retry, ambiguous=e.code >= 500) from None
        except (URLError, TimeoutError, socket.timeout, ValueError, OSError):
            # Request might have reached Telegram. Retrying sendMessage can duplicate it.
            raise TelegramError("NETWORK_OR_INVALID_RESPONSE", ambiguous=True) from None
        if not isinstance(data, dict):
            raise TelegramError("INVALID_RESPONSE", ambiguous=True)
        if not data.get("ok"):
            code = data.get("error_code", "API")
            raise TelegramError(code, data.get("parameters", {}).get("retry_after"), ambiguous=isinstance(code, int) and code >= 500)
        return data.get("result")


def keyboard(rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def position_buttons(p):
    rows = []
    if p["state"] == "CLOSED":
        return keyboard(rows)
    if p["state"] == "PAUSED":
        rows.append([("Lanjutkan Tracking", f"mt:r:{p['id']}")])
    elif p["state"] != "EXIT_REPORTED":
        rows.append([("Pause Tracking", f"mt:p:{p['id']}")])
    rows.append([("Posisi Sudah Ditutup", f"mt:x:{p['id']}")])
    return keyboard(rows)


def position_text(p, now=None, stale_seconds=20):
    d = p["details"]
    m = p.get("metrics") or {}
    now = time.time() if now is None else now
    fresh = p["state"] == "ACTIVE" and p["data_quality"] == "FRESH" and 0 <= now - (p.get("last_price_at") or 0) <= stale_seconds
    pnl = fmt(m.get("net_pnl")) if fresh else "N/A (data belum valid/basi atau tracking tidak aktif)"
    r = fmt(m.get("r_multiple")) if fresh else "N/A"
    return (f"SECRETAGENT | POSISI MANUAL\nID: {p['id']}\n"
            f"{d['symbol']} | {d['instrument']} | {d['direction']}\nStatus: {p['state']}\n"
            f"Entry aktual (laporan pengguna): {d['entry']}\nKuantitas: {d['quantity']}\n"
            f"SL awal: {d['initial_stop']}\nSL rekomendasi terakhir: {p['recommended_stop']}\n"
            f"Harga terakhir: {p.get('last_price') or 'N/A'}\n"
            f"PnL estimasi setelah fee: {pnl} USDT\nR: {r}\n"
            f"Data tersimpan: {p['data_quality']} | {utc(p['last_price_at']) if p.get('last_price_at') else 'belum tersedia'}\n"
            "Periksa umur data. Tidak ada bukti order/fill exchange. /positions untuk memperbarui tampilan.")


def publish_signal(store, config, row, legacy_text=""):
    """One durable delivery per *saved* LONG/SHORT signal, even after UI restart."""
    if row["final_decision"] not in {"LONG", "SHORT"}:
        raise ValueError("WAIT tidak boleh membuka tracker.")
    source = hashlib.sha256(f"{row['id']}|{row['created_at']}|{row.get('analysis_id', '')}".encode()).hexdigest()
    now = store.clock()
    with store.tx() as con:
        found = con.execute("SELECT id FROM mt_signals WHERE source_key=?", (source,)).fetchone()
        if found:
            return "signal:" + found["id"]
        sid = uid()
        con.execute("INSERT INTO mt_signals(id,source_key,source_signal_id,snapshot,chat_id,state,created_at,expires_at) VALUES(?,?,?,?,?,'SIGNAL_SENT',?,?)",
                    (sid, source, row["id"], dumps(row), int(config.chat_id), now, now + config.signal_ttl))
        header = (f"SECRETAGENT V5.5 | SETUP REFERENSI\nSignal ID: {row['id']} / {sid}\n"
                  f"Symbol: {row['symbol']}\nDirection: {row['final_decision']}\n"
                  f"Timeframe: {row['timeframe']}\nWaktu sinyal: {row['created_at']}\n"
                  f"Entry referensi: {fmt(row['entry'])}\nUsulan SL awal: {fmt(row['stop_loss'])}\n"
                  f"TP: {fmt(row.get('take_profit'))}\nR/R: {fmt(row.get('risk_reward'))}\n"
                  "Ini setup analisis, belum merupakan transaksi aktual. Klik Saya Sudah Entry hanya setelah Anda entry manual.\n\n")
        text = header + legacy_text[:2500]
        buttons = keyboard([[('Saya Sudah Entry', f'mt:e:{sid}'), ('Abaikan Sinyal', f'mt:i:{sid}')]])
        store.queue(con, "signal:" + sid, int(config.chat_id), text, buttons, "SIGNAL", sid=sid, ttl=config.signal_ttl)
        store.event(con, "signal:" + sid, "SIGNAL_QUEUED", {"source_signal_id": row["id"]}, sid=sid)
    return "signal:" + sid


class Dispatcher:
    def __init__(self, store, config, api):
        self.store, self.config, self.api = store, config, api

    def recover_inflight(self):
        # Called once under the exclusive worker lock, not on each UI rerun.
        with self.store.tx() as con:
            con.execute("UPDATE mt_outbox SET state='UNKNOWN',last_error='RESTART_DURING_SEND' WHERE state='SENDING'")

    def dispatch_one(self, event_id=None):
        now = self.store.clock()
        with self.store.tx() as con:
            cooldown = con.execute("SELECT value FROM mt_runtime WHERE key='send_cooldown'").fetchone()
            if cooldown and json.loads(cooldown["value"]) > now:
                return False
            row = con.execute("SELECT * FROM mt_outbox WHERE state='PENDING' AND next_attempt<=?" +
                              (" AND id=?" if event_id else "") + " ORDER BY CASE WHEN method='answerCallbackQuery' THEN 0 ELSE 1 END,created_at LIMIT 1",
                              (now, event_id) if event_id else (now,)).fetchone()
            if not row:
                return False
            out = record(row)
            reason = None
            if out["expires_at"] is not None and out["expires_at"] < now:
                reason = "EXPIRED"
            if out["kind"] == "RECOMMENDATION":
                p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (out["position_id"],)).fetchone())
                if not p or p["state"] != "ACTIVE" or p["data_quality"] != "FRESH" or now - (p["last_price_at"] or 0) > self.config.stale_seconds:
                    reason = "STALE_OR_INACTIVE"
                elif p["recommended_stop"] != out["stop"]:
                    reason = "NEWER_STOP"
                elif (p["details"]["direction"] == "LONG" and Decimal(out["stop"]) >= Decimal(p["last_price"])) or (p["details"]["direction"] == "SHORT" and Decimal(out["stop"]) <= Decimal(p["last_price"])):
                    reason = "STOP_ALREADY_TOUCHED"
            if reason:
                con.execute("UPDATE mt_outbox SET state='SUPERSEDED',last_error=? WHERE id=?", (reason, out["id"]))
                return True
            con.execute("UPDATE mt_outbox SET state='SENDING',attempts=attempts+1 WHERE id=?", (out["id"],))
        result = None
        try:
            result = self.api.call(out["method"], out["payload"])
            if out["method"] == "sendMessage" and (not isinstance(result, dict) or not result.get("message_id")):
                raise TelegramError("MISSING_MESSAGE_ID", ambiguous=True)
            status, err, delay = "SENT", None, 0
        except TelegramError as e:
            err = str(e)
            status = "UNKNOWN" if e.ambiguous else ("PENDING" if e.code == 429 else "FAILED")
            try:
                delay = max(1, float(e.retry_after or 30))
                if not math.isfinite(delay):
                    delay = 30
            except (ValueError, TypeError):
                delay = 30
        except Exception:
            status, err, delay = "UNKNOWN", "UNEXPECTED_SEND_FAILURE", 0
        mid = result.get("message_id") if isinstance(result, dict) else None
        with self.store.tx() as con:
            if status == "PENDING":
                self.store.runtime("send_cooldown", self.store.clock() + delay, con)
            con.execute("UPDATE mt_outbox SET state=?,last_error=?,next_attempt=?,message_id=?,sent_at=? WHERE id=?",
                        (status, err, self.store.clock() + delay, mid, self.store.clock() if status == "SENT" else None, out["id"]))
            if status == "SENT" and out["kind"] == "SIGNAL":
                con.execute("UPDATE mt_signals SET message_id=? WHERE id=?", (mid, out["signal_id"]))
            if status == "SENT" and out["kind"] == "RECOMMENDATION":
                con.execute("UPDATE mt_positions SET last_notified_stop=?,last_event_id=? WHERE id=?", (out["stop"], out["id"], out["position_id"]))
        return True

    def drain(self, limit=30):
        for _ in range(limit):
            if not self.dispatch_one():
                break

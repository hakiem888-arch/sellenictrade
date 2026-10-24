"""Telegram notifications with persistent, destination-scoped duplicate suppression."""
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests


def wib(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, ZoneInfo("Asia/Jakarta")).strftime("%d %b %Y %H:%M:%S WIB")


def number(value):
    return f"{value:.10f}".rstrip("0").rstrip(".")


def message(result, plan=None):
    bt, et = {"SCALPING": ("1H", "5M"), "INTRADAY": ("4H", "15M"), "SWING": ("1D", "4H")}[result.mode]
    ready = bool(plan and plan.get("ready"))
    title = f"SETUP {result.direction} VALID — MENUNGGU TRIGGER" if ready else f"KANDIDAT TEKNIS {result.direction} — WAIT"
    v, p = result.values, result.levels
    lines = [title, f"{result.symbol} · Binance USDT perpetual", f"{result.mode} · Bias {bt} / Sinyal {et}",
             f"Candle: {wib(result.signal_close + 1)}", f"Snapshot: {wib(result.checked_at)}",
             f"ADX {v['adx']:.1f} · RSI {v['rsi']:.1f} · Volume {v['rvol']:.2f}x"]
    if ready:
        d = plan["data"]
        lines += [f"STOP-LIMIT: trigger {number(p['trigger'])} | limit {number(p['entry'])} (Last)",
                  f"SL stop-market: {number(p['stop'])} (rencana pemicu Mark)",
                  f"TP1 2R: {number(p['tp1'])} · Q {number(d['tp1_qty'])}",
                  f"TP2 3R: {number(p['tp2'])} · Q {number(d['tp2_qty'])}",
                  f"Runner Q {number(d['runner_qty'])} · trailing 2 ATR setelah TP2",
                  f"Q total {number(d['quantity'])} · risiko estimasi {d['actual_risk']:.2f} USDT",
                  f"Margin estimasi {d['margin']:.2f} USDT · {d['leverage']}x isolated",
                  f"BE estimasi setelah TP1: {number(d['breakeven'])}",
                  f"Likuidasi input pengguna: {number(d['liquidation'])}",
                  "Akun, berita, likuiditas dan likuidasi berdasarkan verifikasi manual pengguna."]
    else:
        lines += ["Indikator dan filter harga publik lolos.",
                  "WAIT: verifikasi akun, fee, funding, likuidasi, likuiditas ukuran order dan berita CPI/FOMC.",
                  "Buka detail aplikasi untuk menghitung rencana setelah data tersebut tersedia."]
    lines += [f"Batas rencana: {wib(p['expires_at'])}",
              "Evaluasi ulang jika trigger/SL sudah disentuh, bias berubah, atau biaya/likuiditas gagal.",
              "Snapshot, bukan order. Wajib perbarui harga sebelum entry; tidak ada pemantauan fill/exit."]
    return "\n".join(lines)


class Telegram:
    def __init__(self, token, chat_id, db_path=None, session=None):
        self.token, self.chat_id = str(token).strip(), str(chat_id).strip()
        self.db_path = Path(db_path or os.getenv("ALERT_DB_PATH", ".runtime/alerts.sqlite3"))
        self.session = session or requests.Session()
        self.destination = hashlib.sha256(f"{self.token}|{self.chat_id}".encode()).hexdigest()

    @property
    def configured(self):
        return bool(self.token and self.chat_id)

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS alerts (destination TEXT, key TEXT, state TEXT, updated REAL, retry_at REAL, note TEXT, PRIMARY KEY(destination,key))")
        return con

    def send(self, text, key):
        if not self.configured:
            return False, "Token / Chat ID Telegram belum diisi"
        now = time.time()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT state,retry_at FROM alerts WHERE destination=? AND key=?", (self.destination, key)).fetchone()
            if row and row[0] == "sent":
                return False, "Sudah terkirim untuk candle ini"
            if row and row[1] > now:
                return False, "Menunggu jeda pengiriman ulang"
            # Telegram flood wait is shared by all keys for this destination.
            flood = con.execute("SELECT MAX(retry_at) FROM alerts WHERE destination=? AND state='rate_limit'", (self.destination,)).fetchone()[0]
            if flood and flood > now:
                return False, "Telegram meminta jeda"
            con.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?)", (self.destination, key, "sending", now, now + 120, "Sedang dikirim"))
        ok, note, delay, state = False, "", 60, "failed"
        try:
            response = self.session.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000], "link_preview_options": {"is_disabled": True}}, timeout=(5, 15))
            body = response.json()
            ok = response.status_code == 200 and body.get("ok") is True
            if ok:
                note, state = "Alert Telegram terkirim", "sent"
            else:
                code = int(body.get("error_code", response.status_code))
                note = f"Telegram menolak pesan (kode {code}); cek token, Chat ID, dan akses bot"
                if code == 429:
                    delay = max(1, int(body.get("parameters", {}).get("retry_after", 60)))
                    state = "rate_limit"
        except (requests.RequestException, ValueError, TypeError):
            note = "Telegram belum terkonfirmasi; koneksi/respons bermasalah"
        with self.connect() as con:
            con.execute("UPDATE alerts SET state=?,updated=?,retry_at=?,note=? WHERE destination=? AND key=?",
                        (state, time.time(), time.time() + delay if not ok else 0, note, self.destination, key))
        return ok, note

    def history(self):
        with self.connect() as con:
            return con.execute("SELECT key,state,updated,note FROM alerts WHERE destination=? ORDER BY updated DESC LIMIT 20", (self.destination,)).fetchall()

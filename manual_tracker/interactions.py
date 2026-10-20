"""Transactional Telegram conversations with ownership, replay protection, and autonomous controls."""
import json
from decimal import Decimal

from .config import AutonomousConfig
from .engine import InvalidData, fmt, number, validate_details
from .store import dumps, record, uid, transition
from .telegram import keyboard, position_buttons, position_text


PROMPTS = {
    "instrument": "Pilih instrumen aktual. Balas SPOT (LONG saja) atau LINEAR (Binance USDT perpetual). Inverse/COIN-M/margin spot tidak didukung.",
    "entry": "Masukkan harga entry aktual dari fill Anda. Harga referensi sinyal tidak otomatis menjadi harga entry.",
    "quantity": "Masukkan kuantitas aktual dalam unit aset dasar (contoh BTC: 0.001), bukan nominal USDT. Untuk spot gunakan jumlah aset yang benar-benar dimiliki setelah fee aset dasar.",
    "initial_stop": "Masukkan SL awal aktual yang Anda konfirmasikan. Pastikan sesuai tick exchange. LONG: SL < entry; SHORT: SL > entry.",
    "multiplier": "Konfirmasi contract multiplier. Adapter Binance USD-M memakai kuantitas aset dasar; balas 1. Kontrak dengan multiplier lain tidak didukung adapter ini.",
    "leverage": "Masukkan leverage aktual (1..125), atau '-' jika tidak diketahui. Tanpa leverage, ROI futures tidak ditampilkan.",
    "entry_fee": "Masukkan estimasi fee entry sebagai fraksi, misalnya 0.0004 = 0.04%. Balas 0 hanya jika memang tanpa fee. Tanpa estimasi biaya, konfirmasi belum dapat dilanjutkan.",
    "exit_fee": "Masukkan estimasi fee exit sebagai fraksi, misalnya 0.0004 = 0.04%. Funding dan slippage belum tercakup.",
    "exit_price": "Anda melaporkan posisi telah ditutup. Masukkan harga exit aktual, atau '-' jika tidak tersedia. Langkah berikutnya meminta konfirmasi penutupan.",
}


class Interactions:
    def __init__(self, store, config, auto_config=None):
        self.store, self.config = store, config
        self.auto_config = auto_config or AutonomousConfig.load()
        self.bot_id = config.token.split(":", 1)[0]

    def process(self, update):
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return "INVALID_UPDATE"
        query = update.get("callback_query")
        message = query.get("message", {}) if query else update.get("message", {})
        sender = query.get("from", {}) if query else message.get("from", {})
        user, chat = sender.get("id"), message.get("chat", {}).get("id")
        callback_id = query.get("id") if query else None
        with self.store.tx() as con:
            seen = con.execute("SELECT 1 FROM mt_updates WHERE bot_id=? AND (update_id=? OR callback_id=?)", (self.bot_id, update_id, callback_id)).fetchone()
            if seen:
                previous = con.execute("SELECT value FROM mt_runtime WHERE key=?", ("offset:" + self.bot_id,)).fetchone()
                self.store.runtime("offset:" + self.bot_id, max(update_id + 1, json.loads(previous["value"]) if previous else 0), con)
                return "DUPLICATE"
            self.store.expire_sessions(con)
            authorized = isinstance(user, int) and isinstance(chat, int) and not sender.get("is_bot", False) and self.config.authorized(user, chat)
            outcome = "UNAUTHORIZED"
            if authorized:
                con.execute("SAVEPOINT interaction")
                try:
                    if query:
                        outcome = self._callback(con, query, user, chat, update_id)
                    elif "text" in message:
                        outcome = self._message(con, message, user, chat, update_id)
                    else:
                        outcome = "UNSUPPORTED_UPDATE"
                    con.execute("RELEASE interaction")
                except (InvalidData, ValueError, KeyError) as e:
                    con.execute("ROLLBACK TO interaction")
                    con.execute("RELEASE interaction")
                    outcome = "REJECTED"
                    text = str(e) if isinstance(e, (InvalidData, ValueError)) else "Data tidak lengkap. Gunakan /positions atau /cancel."
                    self.store.queue(con, f"reply:{self.bot_id}:{update_id}", chat, text)
            if callback_id:
                ack = "Tercatat." if outcome not in {"UNAUTHORIZED", "REJECTED"} else ("Tidak diizinkan." if outcome == "UNAUTHORIZED" else "Interaksi tidak valid. Periksa pesan bot.")
                self.store.queue(con, f"ack:{self.bot_id}:{callback_id}", chat, "", method="answerCallbackQuery",
                                 payload={"callback_query_id": callback_id, "text": ack}, ttl=30)
            con.execute("INSERT INTO mt_updates VALUES(?,?,?,?,?)", (self.bot_id, update_id, callback_id, outcome, self.store.clock()))
            previous = con.execute("SELECT value FROM mt_runtime WHERE key=?", ("offset:" + self.bot_id,)).fetchone()
            offset = max(update_id + 1, json.loads(previous["value"]) if previous else 0)
            self.store.runtime("offset:" + self.bot_id, offset, con)
            return outcome

    def _session(self, con, user, chat):
        return record(con.execute("SELECT * FROM mt_sessions WHERE user_id=? AND chat_id=?", (user, chat)).fetchone())

    def _owned_position(self, con, pid, user, chat):
        p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (pid,)).fetchone())
        if not p or p["owner_id"] != user or p["chat_id"] != chat:
            raise ValueError("Posisi tidak ditemukan atau bukan milik Anda.")
        return p

    def _verified_message(self, con, query, chat, sid=None, pid=None, event=None):
        mid = query.get("message", {}).get("message_id")
        candidates = con.execute("SELECT * FROM mt_outbox WHERE message_id=? AND state='SENT'", (mid,)).fetchall()
        for row in candidates:
            out = record(row)
            if out["payload"].get("chat_id") != chat:
                continue
            if out["expires_at"] is not None and out["expires_at"] < self.store.clock():
                continue
            if (sid and out["kind"] == "SIGNAL" and out["signal_id"] == sid) or (pid and out["position_id"] == pid) or (event and out["id"] == event):
                return
        raise ValueError("Tombol kedaluwarsa atau bukan dari pesan bot yang terdaftar. Gunakan /positions.")

    def _callback(self, con, q, user, chat, update_id):
        parts = str(q.get("data", "")).split(":")
        if len(parts) != 3 or parts[0] != "mt" or len(parts[2]) != 20:
            raise ValueError("Tombol tidak valid.")
        _, action, target = parts
        if action in {"e", "i"}:
            s = record(con.execute("SELECT * FROM mt_signals WHERE id=?", (target,)).fetchone())
            if not s or s["chat_id"] != chat or s["expires_at"] < self.store.clock():
                raise ValueError("Sinyal tidak valid atau kedaluwarsa.")
            self._verified_message(con, q, chat, sid=target)
            if s["snapshot"]["final_decision"] not in {"LONG", "SHORT"}:
                raise ValueError("Sinyal WAIT tidak dapat ditracking.")
            if s["owner_id"] is not None and s["owner_id"] != user:
                raise ValueError("Sinyal telah diklaim pengguna lain.")
            if s["state"] == "IGNORED":
                return "IGNORED"
            if s["state"] not in {"SIGNAL_SENT", "AWAITING_ENTRY_DETAILS"}:
                raise ValueError("Sinyal sudah memiliki tracker. Gunakan /positions.")
            session = self._session(con, user, chat)
            if action == "i":
                transition(s["state"], "IGNORED")
                con.execute("UPDATE mt_signals SET state='IGNORED',owner_id=? WHERE id=?", (user, target))
                con.execute("DELETE FROM mt_sessions WHERE signal_id=? AND position_id IS NULL", (target,))
                self.store.event(con, "ignored:" + target, "IGNORED", {"user": user}, sid=target)
                self.store.queue(con, "ignored:" + target, chat, "Sinyal diabaikan permanen untuk tracker ini. Tidak ada posisi aktif. Lihat /history.")
                return "IGNORED"
            if session:
                if session["signal_id"] == target and not session["position_id"]:
                    return "AWAITING_ENTRY_DETAILS"
                raise ValueError("Selesaikan percakapan yang berjalan atau gunakan /cancel.")
            nonce = uid()
            data = {"symbol": s["snapshot"]["symbol"], "direction": s["snapshot"]["final_decision"], "multiplier": "1", "leverage": None}
            con.execute("UPDATE mt_signals SET state='AWAITING_ENTRY_DETAILS',owner_id=? WHERE id=?", (user, target))
            con.execute("INSERT INTO mt_sessions(chat_id,user_id,nonce,signal_id,step,data,expires_at) VALUES(?,?,?,?,'instrument',?,?)",
                        (chat, user, nonce, target, dumps(data), self.store.clock() + self.config.session_ttl))
            self._prompt(con, self._session(con, user, chat), update_id)
            return "AWAITING_ENTRY_DETAILS"
        if action in {"c", "n"}:
            s = self._session(con, user, chat)
            if not s or s["nonce"] != target or s["step"] != "confirm":
                raise ValueError("Konfirmasi kedaluwarsa atau sudah diproses.")
            self._verified_message(con, q, chat, event=s["prompt_event"])
            if action == "n":
                self._cancel(con, s)
                self.store.queue(con, f"cancel:{target}", chat, "Konfirmasi dibatalkan. Tracker penutupan tetap PAUSED.")
                return "CANCELLED"
            if s["position_id"]:
                p = self._owned_position(con, s["position_id"], user, chat)
                self.store.set_state(con, p, "CLOSED")
                con.execute("UPDATE mt_positions SET exit_price=?,closed_at=?,closure_source='USER_REPORTED' WHERE id=?",
                            (s["data"].get("exit_price"), self.store.clock(), p["id"]))
                text = f"Tracker {p['id']} CLOSED berdasarkan konfirmasi pengguna. Harga exit: {s['data'].get('exit_price') or 'tidak diberikan'}. Tidak memverifikasi fill exchange."
                pid, result = p["id"], "CLOSED"
            else:
                signal = record(con.execute("SELECT * FROM mt_signals WHERE id=?", (s["signal_id"],)).fetchone())
                if not signal or signal["state"] != "AWAITING_ENTRY_DETAILS" or signal["owner_id"] != user:
                    raise ValueError("Sinyal tidak dapat diaktifkan.")
                d = validate_details(s["data"], signal["snapshot"]["final_decision"])
                meta = con.execute("SELECT * FROM mt_instruments WHERE instrument=? AND symbol=?", (d["instrument"], d["symbol"])).fetchone()
                if not meta or meta["last_error"] or self.store.clock() - meta["checked_at"] > 600:
                    raise InvalidData("Metadata exchange belum valid. Pastikan worker berjalan, tunggu pembaruan data, lalu tekan Konfirmasi lagi. Posisi belum diaktifkan.")
                if Decimal(d["initial_stop"]) % number(meta["tick"]) != 0:
                    raise InvalidData("SL awal tidak sesuai tick exchange. Gunakan /cancel lalu daftarkan ulang dengan SL yang valid.")
                d["confirmed_tick"] = meta["tick"]
                d["confirmed_at"] = self.store.clock()
                pid = uid()
                con.execute("""INSERT INTO mt_positions(id,signal_id,owner_id,chat_id,details,state,recommended_stop,best_price,created_at,updated_at)
                            VALUES(?,?,?,?,?,'ACTIVE',?,?,?,?)""",
                            (pid, s["signal_id"], user, chat, dumps(d), d["initial_stop"], d["entry"], self.store.clock(), self.store.clock()))
                transition(signal["state"], "ACTIVE")
                con.execute("UPDATE mt_signals SET state='ACTIVE' WHERE id=?", (s["signal_id"],))
                self.store.event(con, "registered:" + pid, "ACTIVE", d, sid=s["signal_id"], pid=pid)
                p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (pid,)).fetchone())
                text, result = position_text(p, self.store.clock(), self.config.stale_seconds), "ACTIVE"
            con.execute("DELETE FROM mt_sessions WHERE nonce=?", (target,))
            p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (pid,)).fetchone())
            self.store.queue(con, "confirmed:" + target, chat, text, position_buttons(p), pid=pid)
            return result
        if action in {"p", "r", "x"}:
            p = self._owned_position(con, target, user, chat)
            self._verified_message(con, q, chat, pid=target)
            if action in {"p", "r"}:
                state = "PAUSED" if action == "p" else "ACTIVE"
                if action == "r" and p["state"] != "PAUSED":
                    raise ValueError("Hanya PAUSED dapat dilanjutkan secara eksplisit.")
                self.store.set_state(con, p, state)
                con.execute("UPDATE mt_positions SET data_quality=? WHERE id=?", ("PAUSED" if state == "PAUSED" else "AWAITING_RESYNC", target))
                p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (target,)).fetchone())
                self.store.queue(con, f"action:{self.bot_id}:{update_id}", chat, position_text(p, self.store.clock(), self.config.stale_seconds), position_buttons(p), pid=target)
                return state
            if p["state"] == "CLOSED":
                return "CLOSED"
            if self._session(con, user, chat):
                raise ValueError("Selesaikan percakapan aktif atau /cancel terlebih dahulu.")
            self.store.set_state(con, p, "EXIT_REPORTED", "AWAITING_USER_CLOSE_CONFIRMATION")
            nonce = uid()
            con.execute("INSERT INTO mt_sessions(chat_id,user_id,nonce,signal_id,position_id,step,data,expires_at) VALUES(?,?,?,?,?,'exit_price','{}',?)",
                        (chat, user, nonce, p["signal_id"], target, self.store.clock() + self.config.session_ttl))
            self._prompt(con, self._session(con, user, chat), update_id)
            return "EXIT_REPORTED"
        raise ValueError("Aksi tidak dikenal.")

    def _prompt(self, con, s, update_id):
        step, d = s["step"], s["data"]
        markup = {"force_reply": True, "selective": True}
        if step == "confirm":
            if s["position_id"]:
                text = f"Konfirmasi laporan penutupan {s['position_id']}. Harga exit: {d.get('exit_price') or 'tidak tersedia'}. Tracking akan berhenti."
            else:
                d = validate_details(d, d["direction"])
                text = (f"PERIKSA POSISI AKTUAL\n{d['symbol']} | {d['instrument']} | {d['direction']}\n"
                        f"Entry: {d['entry']}\nQuantity: {d['quantity']}\nSL awal: {d['initial_stop']}\n"
                        f"Multiplier: {d['multiplier']}\nLeverage: {d.get('leverage') or 'tidak diketahui; ROI dinonaktifkan'}\n"
                        f"Fee entry/exit (fraksi): {d['entry_fee']} / {d['exit_fee']}\n"
                        f"Buffer BE: {self.config.cost_buffer_bps} bps. Funding/slippage tidak diketahui.\n"
                        "Informasi ini adalah laporan Anda, bukan verifikasi exchange. Tracking mulai setelah konfirmasi; metadata exchange harus valid sebelum rekomendasi.")
            markup = keyboard([[('Konfirmasi', f'mt:c:{s["nonce"]}'), ('Batal', f'mt:n:{s["nonce"]}')]])
        else:
            text = f"Signal/tracker: {s['signal_id']}\n{PROMPTS[step]}\nBalas langsung pesan ini. /cancel untuk batal."
        key = f"prompt:{s['nonce']}:{update_id}"
        self.store.queue(con, key, s["chat_id"], text, markup, sid=s["signal_id"], pid=s["position_id"], ttl=self.config.session_ttl)
        con.execute("UPDATE mt_sessions SET prompt_event=?,expires_at=? WHERE nonce=?", (key, self.store.clock() + self.config.session_ttl, s["nonce"]))

    def _cancel(self, con, s):
        if s["position_id"]:
            p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (s["position_id"],)).fetchone())
            if p["state"] == "EXIT_REPORTED":
                self.store.set_state(con, p, "PAUSED", "CLOSE_CANCELLED")
        else:
            con.execute("UPDATE mt_signals SET state='SIGNAL_SENT',owner_id=NULL WHERE id=? AND state='AWAITING_ENTRY_DETAILS'", (s["signal_id"],))
        con.execute("DELETE FROM mt_sessions WHERE nonce=?", (s["nonce"],))

    def _message(self, con, msg, user, chat, update_id):
        text = msg["text"].strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        s = self._session(con, user, chat)
        key = f"reply:{self.bot_id}:{update_id}"
        if command == "/cancel":
            if s:
                self._cancel(con, s)
            self.store.queue(con, key, chat, "Percakapan dibatalkan. Jika proses close dibatalkan, posisi tetap PAUSED.")
            return "CANCELLED"
        if command in {"/killswitch", "/halt"}:
            now = self.store.clock()
            ks_val = {"active": True, "mode": "HALT_NEW_ENTRIES", "actor": f"telegram:{user}", "reason": "TELEGRAM_KILL_SWITCH", "activated_at": now}
            self.store.risk_state("kill_switch", ks_val, con=con)
            self.store.audit("KILL_SWITCH", "ACTIVATED_TELEGRAM", ks_val, environment=self.auto_config.execution_mode, severity="CRITICAL", con=con)
            self.store.queue(
                con, key, chat,
                "🛑 KILL SWITCH AKTIF (HALT_NEW_ENTRIES)\nSeluruh entry baru diblokir. Order proteksi SL/TP pada posisi terbuka tetap dipertahankan.",
            )
            return "KILL_SWITCH_ENGAGED"
        if command == "/resume_entries":
            now = self.store.clock()
            ks_val = {"active": False, "disengaged_by": f"telegram:{user}", "disengaged_at": now}
            self.store.risk_state("kill_switch", ks_val, con=con)
            self.store.audit("KILL_SWITCH", "DISENGAGED_TELEGRAM", ks_val, environment=self.auto_config.execution_mode, con=con)
            self.store.queue(con, key, chat, "🟢 Kill switch dinonaktifkan. Entry baru tetap harus melewati seluruh pemeriksaan kesiapan dan Risk Engine.")
            return "KILL_SWITCH_DISENGAGED"
        if command in {"/status", "/account", "/risk"}:
            env = self.auto_config.execution_mode
            inst = self.auto_config.default_instrument
            snap = record(con.execute(
                "SELECT * FROM at_account_snapshots WHERE environment=? AND instrument=? ORDER BY synced_at DESC LIMIT 1",
                (env, inst),
            ).fetchone())
            ks = self.store.risk_state("kill_switch", default={"active": False}, con=con)
            breakers = [r["reason"] for r in con.execute("SELECT reason FROM at_circuit_breakers WHERE active=1").fetchall()]
            open_at = con.execute(
                "SELECT COUNT(*) AS c FROM at_positions WHERE environment=? AND position_state IN ('OPENING','PROTECTED_ACTIVE','PROTECTION_REQUIRED')",
                (env,),
            ).fetchone()["c"]
            eq_str = snap["account_equity"] if snap else "N/A"
            avail_str = snap["available_margin"] if snap else "N/A"
            summary_text = (
                f"SECRETAGENT V5.5 | STATUS OPERASIONAL\n"
                f"Mode: {env} ({inst}) | Trailing: {self.auto_config.trailing_mode}\n"
                f"Equity: {eq_str} USDT | Available Margin: {avail_str} USDT\n"
                f"Posisi Autonomous Terbuka: {open_at}\n"
                f"Kill Switch: {'AKTIF' if ks.get('active') else 'NONAKTIF'}\n"
                f"Circuit Breakers: {', '.join(breakers) if breakers else 'CLEAR'}"
            )
            self.store.queue(con, key, chat, summary_text)
            return "STATUS"
        if command == "/positions":
            rows = con.execute("SELECT * FROM mt_positions WHERE owner_id=? AND chat_id=? ORDER BY created_at DESC LIMIT 20", (user, chat)).fetchall()
            for row in rows:
                p = record(row)
                self.store.queue(con, key + ":" + p["id"], chat, position_text(p, self.store.clock(), self.config.stale_seconds), position_buttons(p), pid=p["id"], ttl=self.config.signal_ttl)
            if not rows:
                self.store.queue(con, key, chat, "Belum ada posisi manual terdaftar.")
            return "POSITIONS"
        if command == "/history":
            rows = con.execute("SELECT id,source_signal_id,state FROM mt_signals WHERE chat_id=? AND owner_id=? ORDER BY created_at DESC LIMIT 30", (chat, user)).fetchall()
            self.store.queue(con, key, chat, "RIWAYAT TRACKING\n" + ("\n".join(f"Signal {r['source_signal_id']} / {r['id']}: {r['state']}" for r in rows) or "Belum ada."))
            return "HISTORY"
        if command in {"/start", "/help"}:
            self.store.queue(
                con, key, chat,
                "Klik Saya Sudah Entry pada sinyal LONG/SHORT, lalu balas tiap pertanyaan dan konfirmasi. "
                "/positions untuk pause/resume/close; /history untuk riwayat; /status untuk ringkasan akun & risiko; "
                "/killswitch untuk menghentikan entry baru; /cancel untuk membatalkan formulir. Semua tindakan manual tetap terlindungi.",
            )
            return "HELP"
        if not s or s["step"] == "confirm":
            raise ValueError("Tidak ada pertanyaan aktif. Gunakan tombol konfirmasi, /positions, atau /help.")
        out = con.execute("SELECT message_id,state FROM mt_outbox WHERE id=?", (s["prompt_event"],)).fetchone()
        if not out or out["state"] != "SENT" or msg.get("reply_to_message", {}).get("message_id") != out["message_id"]:
            raise ValueError("Balas langsung pertanyaan terakhir bot agar data terkait posisi yang benar.")
        if msg.get("date", 0) + self.config.session_ttl < self.store.clock():
            raise ValueError("Jawaban kedaluwarsa. /cancel lalu mulai kembali.")
        d, step = s["data"], s["step"]
        if step == "instrument":
            val = text.upper()
            if val not in {"SPOT", "LINEAR"} or (val == "SPOT" and d["direction"] != "LONG"):
                raise InvalidData("Gunakan SPOT untuk LONG atau LINEAR untuk LONG/SHORT. Inverse tidak didukung.")
            d[step], next_step = val, "entry"
        elif step == "leverage" and text == "-":
            d[step], next_step = None, "entry_fee"
        elif step == "exit_price":
            d[step], next_step = (None if text == "-" else fmt(number(text, "Harga exit"))), "confirm"
        else:
            d[step] = fmt(number(text, step, zero=step in {"entry_fee", "exit_fee"}))
            if step in {"entry_fee", "exit_fee"} and Decimal(d[step]) >= Decimal("0.1"):
                raise InvalidData("Fee harus fraksi < 0.1, contoh 0.0004.")
            if step == "multiplier" and Decimal(d[step]) != 1:
                raise InvalidData("Multiplier Binance harus 1 dalam unit aset dasar.")
            if step == "leverage" and not 1 <= Decimal(d[step]) <= 125:
                raise InvalidData("Leverage harus 1..125 atau '-'.")
            if step == "initial_stop":
                long = d["direction"] == "LONG"
                if (long and Decimal(d[step]) >= Decimal(d["entry"])) or (not long and Decimal(d[step]) <= Decimal(d["entry"])):
                    raise InvalidData("SL berada di sisi entry yang salah.")
            next_step = {"entry": "quantity", "quantity": "initial_stop", "initial_stop": "multiplier" if d["instrument"] == "LINEAR" else "entry_fee", "multiplier": "leverage", "leverage": "entry_fee", "entry_fee": "exit_fee", "exit_fee": "confirm"}[step]
        con.execute("UPDATE mt_sessions SET data=?,step=? WHERE nonce=?", (dumps(d), next_step, s["nonce"]))
        self._prompt(con, self._session(con, user, chat), update_id)
        return "AWAITING_CONFIRMATION" if next_step == "confirm" else "AWAITING_DETAILS"

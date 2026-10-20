"""Market monitoring uses no AI provider and cannot submit exchange orders."""
from .engine import InvalidData, evaluate, fmt, symbol_name
from .market import DataUnavailable
from .store import dumps, record, utc
from .telegram import position_buttons


ELIGIBLE = {"ACTIVE", "TRACKING_ERROR", "INVALID_DATA"}


class Monitor:
    def __init__(self, store, config, feed):
        self.store, self.config, self.feed = store, config, feed
        self._needs_resync = False

    def recover(self):
        self.feed.reset()
        with self.store.tx() as con:
            con.execute("UPDATE mt_positions SET data_quality='AWAITING_RESYNC' WHERE state IN ('ACTIVE','TRACKING_ERROR','INVALID_DATA')")
            self.store.expire_sessions(con)

    def cycle(self):
        if self._needs_resync:
            self.feed.reset()
            self._needs_resync = False
        # Resolve instrument metadata in the market thread while Telegram asks
        # for entry details. The confirmation transaction never performs I/O.
        sessions = self.store.rows("SELECT data FROM mt_sessions WHERE position_id IS NULL AND expires_at>=?", (self.store.clock(),))
        requested = {(s["data"].get("instrument"), s["data"].get("symbol")) for s in sessions if s["data"].get("instrument")}
        for instrument, symbol in requested:
            try:
                symbol = symbol_name(symbol)
                tick = self.feed.metadata(instrument, symbol)
                checked_at = self.feed.metadata_observed_at(instrument, symbol) if hasattr(self.feed, "metadata_observed_at") else self.store.clock()
                error = None
            except Exception:
                tick, error = None, "INSTRUMENT_METADATA_UNAVAILABLE"
                checked_at = self.store.clock()
            with self.store.tx() as con:
                con.execute("INSERT INTO mt_instruments VALUES(?,?,?,?,?) ON CONFLICT(instrument,symbol) DO UPDATE SET tick=excluded.tick,checked_at=excluded.checked_at,last_error=excluded.last_error",
                            (instrument, symbol, fmt(tick) if tick is not None else None, checked_at, error))
        positions = self.store.rows("SELECT * FROM mt_positions WHERE state IN ('ACTIVE','TRACKING_ERROR','INVALID_DATA') ORDER BY created_at")
        grouped = {}
        for p in positions:
            d = p["details"]
            grouped.setdefault((d["instrument"], d["symbol"]), []).append(p)
        failed, updated = 0, 0
        for group in grouped.values():
            try:
                obs = self.feed.observation(group[0]["details"])
            except (DataUnavailable, InvalidData) as e:
                for p in group:
                    self.degrade(p["id"], str(e), invalid=isinstance(e, InvalidData))
                    failed += 1
                self._needs_resync = True
                continue
            except Exception:
                for p in group:
                    self.degrade(p["id"], "UNEXPECTED_MARKET_FAILURE")
                    failed += 1
                self._needs_resync = True
                continue
            for p in group:
                try:
                    if self.apply(p["id"], obs):
                        updated += 1
                except InvalidData as e:
                    self.degrade(p["id"], str(e), invalid=str(e) in {"INITIAL_STOP_OFF_TICK", "PRICE_REFERENCE_MISMATCH"})
                    failed += 1
                    self._needs_resync = True
            # Heartbeat between symbols too; a stalled process will visibly age.
            self.store.runtime("market", {"state": "RUNNING", "updated": updated, "degraded": failed})
        with self.store.tx() as con:
            self.store.expire_sessions(con)
            self.store.runtime("market", {"state": "RUNNING", "positions": len(positions), "updated": updated, "degraded": failed}, con)
        return {"positions": len(positions), "updated": updated, "degraded": failed}

    def degrade(self, pid, reason, invalid=False):
        with self.store.tx() as con:
            p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (pid,)).fetchone())
            if not p or p["state"] not in ELIGIBLE:
                return
            state = "INVALID_DATA" if invalid else "TRACKING_ERROR"
            first = p["state"] == "ACTIVE"
            if p["state"] != state:
                self.store.set_state(con, p, state, reason)
            con.execute("UPDATE mt_positions SET data_quality='UNAVAILABLE',last_error=?,updated_at=? WHERE id=?", (reason[:100], self.store.clock(), pid))
            con.execute("UPDATE mt_outbox SET state='SUPERSEDED',last_error='DATA_UNAVAILABLE' WHERE position_id=? AND kind='RECOMMENDATION' AND state='PENDING'", (pid,))
            if first:
                key = f"degraded:{pid}:{p['revision']}"
                self.store.queue(con, key, p["chat_id"],
                                 f"SECRETAGENT | TRACKING TERGANGGU\n{p['details']['symbol']} | {pid}\nStatus: {state}\nData: {reason}\nWaktu UTC: {utc(self.store.clock())}\nRekomendasi baru berhenti sampai data valid kembali. Posisi tidak dianggap ditutup.",
                                 position_buttons({**p, "state": state}), "DATA_ERROR", pid=pid)

    def apply(self, pid, obs):
        with self.store.tx() as con:
            p = record(con.execute("SELECT * FROM mt_positions WHERE id=?", (pid,)).fetchone())
            # Re-read after network I/O so a concurrent pause/close wins.
            if not p or p["state"] not in ELIGIBLE:
                return False
            now = self.store.clock()
            out = evaluate(p, obs, self.config, now)
            if p["state"] != "ACTIVE":
                self.store.set_state(con, p, "ACTIVE", "FRESH_DATA_RECOVERED")
                self.store.queue(con, f"recovered:{pid}:{p['revision']}", p["chat_id"],
                                 f"SECRETAGENT | DATA PULIH\n{p['details']['symbol']} | {pid}\nMetadata, candle tertutup, dan harga valid kembali. Tracking ACTIVE. Ekstrem harga selama celah data tidak diketahui.\nUTC: {utc(now)}", pid=pid)
            con.execute("""UPDATE mt_positions SET state='ACTIVE',recommended_stop=?,best_price=?,activated=?,stage=?,
                        last_price=?,last_price_at=?,atr=?,metrics=?,data_quality='FRESH',last_error=NULL,updated_at=?,revision=revision+1 WHERE id=?""",
                        (fmt(out["stop"]), fmt(out["best_price"]), int(out["activated"]), out["stage"], fmt(obs.price), obs.timestamp,
                         fmt(obs.atr), dumps(out["metrics"]), now, pid))
            if out["event"]:
                event, stop = out["event"], fmt(out["stop"])
                key = f"{pid}:{event}:{stop}"
                self.store.event(con, key, event, {"price": fmt(obs.price), "stop": stop, "observed_at": obs.timestamp}, p["signal_id"], pid)
                if event == "STOP_TOUCHED":
                    text = (f"SECRETAGENT | SENTUH LEVEL STOP\n{p['details']['symbol']} | {pid}\n"
                            f"Harga {fmt(obs.price)} menyentuh/melewati level SL rekomendasi {stop}.\n"
                            f"Referensi: {obs.reference} | FRESH | UTC: {utc(obs.timestamp)}\n"
                            "Ini pengamatan harga, bukan bukti fill atau penutupan exchange. Periksa posisi dan konfirmasikan jika telah ditutup.")
                    kind, ttl = "STOP_TOUCH", self.config.stale_seconds
                else:
                    reason = "Aktivasi break-even setelah ambang R; termasuk estimasi fee dan buffer." if event == "BREAK_EVEN" else f"Perbaikan ATR({self.config.atr_period}) × {self.config.atr_multiplier}, dari ekstrem harga valid sejak tracking."
                    m, d = out["metrics"], p["details"]
                    text = (f"SECRETAGENT | TRAILING STOP UPDATE\nID: {pid}\nSymbol: {d['symbol']}\nDirection: {d['direction']}\nStatus: ACTIVE\n"
                            f"Entry aktual (laporan pengguna): {d['entry']}\nHarga kini ({obs.reference}): {fmt(obs.price)}\n"
                            f"Floating PnL estimasi setelah fee: {fmt(m['net_pnl'])} USDT\nR-multiple (gross): {fmt(m['r_multiple'])}\n"
                            f"ROI estimasi: {fmt(m['estimated_roi_pct'])}{'%' if m['estimated_roi_pct'] is not None else ' (leverage belum tersedia)'}\n\n"
                            f"SL rekomendasi sebelumnya: {p['recommended_stop']}\nSL rekomendasi baru: {stop}\n\nAlasan: {reason}\n"
                            "Tindakan: Tinjau lalu ubah stop-loss secara MANUAL di exchange. Ini usulan, bukan order yang telah dipasang.\n"
                            "Fee adalah estimasi. Funding/slippage tidak diketahui; buffer tidak menjamin impas.\n"
                            f"Data: FRESH | ATR candle tertutup {utc(obs.atr_timestamp)}\nUTC harga: {utc(obs.timestamp)}")
                    kind, ttl = "RECOMMENDATION", self.config.stale_seconds
                self.store.queue(con, key, p["chat_id"], text, position_buttons({**p, "state": "ACTIVE"}), kind,
                                 sid=p["signal_id"], pid=pid, stop=stop, ttl=ttl)
            return True

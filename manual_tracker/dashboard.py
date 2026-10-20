"""Secondary panel. Changes still require the authenticated Telegram owner."""
import hmac
import json

from .config import Config
from .store import Store, uid, utc
from .telegram import keyboard


def render_panel(st, db_path, getter):
    with st.expander("📌 Posisi manual & trailing stop", expanded=False):
        try:
            cfg = Config.load(getter)
            if not cfg.enabled:
                st.caption("Tracker manual belum aktif. Paper trading tetap terpisah.")
                return
            expected = str(getter("MANUAL_DASHBOARD_TOKEN") or "")
            if len(expected) < 16:
                st.info("Atur MANUAL_DASHBOARD_TOKEN minimal 16 karakter untuk membuka panel privat ini.")
                return
            entered = st.text_input("Kunci panel posisi manual", type="password", key="manual_panel_secret")
            if not hmac.compare_digest(entered, expected):
                st.caption("Masukkan kunci panel untuk melihat posisi dan meminta tindakan lewat Telegram.")
                return
            store = Store(db_path)
            now = store.clock()
            worker = store.one("SELECT * FROM mt_runtime WHERE key='worker'")
            alive = bool(worker and now - worker["updated_at"] < 15 and json.loads(worker["value"]).get("state") == "RUNNING")
            st.caption("Worker manual: aktif" if alive else "Worker manual: belum tersedia atau heartbeat basi. Tidak ada jaminan pemantauan berlangsung.")
            st.caption("Eksekusi dilaporkan pengguna. PnL dan ROI merupakan estimasi setelah fee, belum termasuk funding/slippage. Tidak ada order exchange otomatis.")
            if st.button("Perbarui posisi manual", key="manual_refresh"):
                st.rerun()
            positions = store.rows("SELECT * FROM mt_positions ORDER BY created_at DESC LIMIT 100")
            rows = []
            for p in positions:
                d, m = p["details"], p["metrics"] or {}
                fresh = alive and p["state"] == "ACTIVE" and p["data_quality"] == "FRESH" and now - (p["last_price_at"] or 0) <= cfg.stale_seconds
                rows.append({"Tracking ID": p["id"], "Symbol": d["symbol"], "Direction": d["direction"], "Instrument": d["instrument"],
                             "Entry aktual (laporan)": d["entry"], "Harga terakhir": p["last_price"],
                             "PnL estimasi USDT": m.get("net_pnl") if fresh else None, "R gross": m.get("r_multiple") if fresh else None,
                             "SL awal": d["initial_stop"], "SL rekomendasi": p["recommended_stop"], "State": p["state"],
                             "Kualitas data": "FRESH" if fresh else "STALE/INACTIVE", "UTC harga": utc(p["last_price_at"]) if p["last_price_at"] else "-",
                             "Exit laporan": p["exit_price"], "Keterangan": p["last_error"]})
            if rows:
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.info("Belum ada posisi manual. Daftarkan melalui tombol Saya Sudah Entry di Telegram.")
            actionable = [p for p in positions if p["state"] != "CLOSED" and cfg.authorized(p["owner_id"], p["chat_id"])]
            if actionable:
                selection = st.selectbox("Posisi untuk tindakan manual", [p["id"] for p in actionable], key="manual_selected")
                p = next(p for p in actionable if p["id"] == selection)
                c1, c2 = st.columns(2)
                requested = None
                if c1.button("Pause via Telegram", key="manual_pause_request", disabled=p["state"] in {"PAUSED", "EXIT_REPORTED"}):
                    requested = ("Pause Tracking", "p")
                if c2.button("Tutup tracking via Telegram", key="manual_close_request", disabled=p["state"] == "EXIT_REPORTED"):
                    requested = ("Posisi Sudah Ditutup", "x")
                if requested:
                    label, action = requested
                    with store.tx() as con:
                        store.queue(con, "dashboard:" + uid(), p["chat_id"], f"Permintaan panel untuk posisi {p['id']} ({p['details']['symbol']}). Pemilik posisi harus mengonfirmasi lewat tombol berikut.",
                                    keyboard([[(label, f"mt:{action}:{p['id']}")]]), pid=p["id"], ttl=cfg.session_ttl)
                    st.info("Permintaan masuk antrean Telegram. Status berubah hanya setelah pemilik menekan tombol di Telegram.")
            st.caption("Riwayat sinyal manual, termasuk IGNORED")
            history = store.rows("SELECT source_signal_id AS signal_id,state,created_at FROM mt_signals ORDER BY created_at DESC LIMIT 100")
            if history:
                st.dataframe(history, use_container_width=True, hide_index=True)
            delivery = store.rows("SELECT state,COUNT(*) AS total FROM mt_outbox GROUP BY state")
            st.caption("Status pengiriman: " + ", ".join(f"{r['state']}={r['total']}" for r in delivery))
            if any(r["state"] in {"UNKNOWN", "FAILED"} for r in delivery):
                st.warning("Ada pengiriman gagal/tidak pasti. UNKNOWN tidak dikirim ulang otomatis untuk mencegah duplikasi. Gunakan /positions untuk meminta ringkasan baru.")
        except Exception:
            st.warning("Panel posisi manual tidak tersedia. Periksa konfigurasi dan database. Mesin sinyal tetap terpisah.")

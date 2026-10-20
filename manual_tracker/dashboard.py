"""Operational & secondary panels for SecretAgent v5.5 Momentum Radar UI.

Keeps the simplified Momentum Radar UI clean without reintroducing Market Scanner.
Provides:
1. `render_operational_header`: Essential operational telemetry & Kill Switch.
2. `render_panel`: Existing manual position & trailing stop panel (100% preserved) plus secondary autonomous views.
"""
import hmac
import json

from .config import AutonomousConfig, Config
from .live_guard import LIVE_CONFIRMATION_PHRASE
from .orchestrator import AutonomousSystemOrchestrator
from .paper_autonomous import compute_environment_performance
from .store import Store, uid, utc
from .telegram import keyboard


def render_operational_header(st, db_path, getter, ai_keys_count=0):
    """Render essential operational status and Kill Switch on the Momentum Radar interface."""
    try:
        store = Store(db_path)
        m_cfg = Config.load(getter)
        a_cfg = AutonomousConfig.load(getter)
        orch = AutonomousSystemOrchestrator(store, m_cfg, a_cfg)
        summary = orch.operational_summary(ai_keys_count=ai_keys_count)

        c1, c2, c3, c4 = st.columns(4)
        if hasattr(c1, "metric"):
            c1.metric("Account Equity", f"${summary['account_equity_usdt']} USDT")
            c2.metric("Available Funds", f"${summary['available_funds_usdt']} USDT")
            c3.metric("Execution Mode", f"{summary['execution_mode']} ({summary['live_authorization_state']})")
            c4.metric("Open Positions / Orders", f"{summary['open_positions_count']} / {summary['active_orders_count']}")

        c5, c6, c7, c8 = st.columns(4)
        if hasattr(c5, "metric"):
            c5.metric("Aggregate Open Risk", f"${summary['aggregate_open_risk_usdt']} / ${summary['max_open_risk_usdt']}")
            c6.metric("Daily PnL / Loss Limit", f"${summary['daily_pnl_usdt']} / -${summary['daily_loss_limit_usdt']}")
            c7.metric("Protective Orders", summary["protective_order_status"])
            c8.metric("Trailing / Health", f"{summary['trailing_mode']} ({summary['latest_trailing_state']}) · {summary['system_health']}")

        ks_col1, ks_col2, ks_col3 = st.columns(3)
        if ks_col1.button("🛑 Kill Switch: Hentikan Entry Baru", key="auto_kill_switch_halt"):
            orch.live_guard.engage_kill_switch(actor="UI_OPERATOR", reason="UI_HALT_NEW_ENTRIES", close_all_positions=False)
            if hasattr(st, "warning"):
                st.warning("Kill Switch diaktifkan: seluruh entry baru dihentikan, order proteksi tetap dijaga.")
        if ks_col2.button("🚨 Emergency Close All Positions", key="auto_kill_switch_close_all"):
            orch.live_guard.engage_kill_switch(actor="UI_OPERATOR", reason="UI_EMERGENCY_CLOSE_ALL", close_all_positions=True)
            if hasattr(st, "warning"):
                st.warning("Emergency Close dijalankan pada posisi terbuka.")
        if summary["kill_switch_active"] and ks_col3.button("🟢 Reset Kill Switch", key="auto_kill_switch_reset"):
            orch.live_guard.disengage_kill_switch(actor="UI_OPERATOR")
            if hasattr(st, "info"):
                st.info("Kill Switch dinonaktifkan.")

        with st.expander("🛡️ Konfigurasi Risiko, Kesiapan Live & Log Eksekusi (Secondary View)", expanded=False):
            st.caption(
                f"Risk/Trade: {float(a_cfg.risk_pct_per_trade)*100:.2f}% · "
                f"Max Daily Loss: {float(a_cfg.max_daily_loss_pct)*100:.2f}% (${a_cfg.max_daily_loss_usdt}) · "
                f"Max Open Risk: {float(a_cfg.max_aggregate_open_risk_pct)*100:.2f}% · "
                f"Max Leverage: {a_cfg.max_permitted_leverage}x · "
                f"Min R:R: {a_cfg.min_acceptable_rr}"
            )
            if summary["active_circuit_breakers"]:
                st.warning("Active Circuit Breakers: " + ", ".join(summary["active_circuit_breakers"]))
            if summary["open_positions"]:
                st.dataframe(summary["open_positions"], use_container_width=True, hide_index=True)

            # Separated Performance by Environment
            for env_name in ("PAPER", "TESTNET", "LIVE", "BACKTEST"):
                perf_env = compute_environment_performance(store, env_name, a_cfg.paper_initial_equity)
                if perf_env["sample_size"] > 0 or env_name == summary["execution_mode"]:
                    st.caption(
                        f"[{env_name}] Trades: {perf_env['sample_size']} · Win Rate: {perf_env['win_rate_pct']}% · "
                        f"Net PnL: ${perf_env['total_net_pnl_usdt']} · Expectancy: ${perf_env['net_expectancy_usdt']} · "
                        f"Max DD: {perf_env['max_drawdown_pct']}%"
                    )

            audits = store.rows("SELECT created_at, environment, category, event_type, severity FROM at_audit_events ORDER BY created_at DESC LIMIT 25")
            if audits:
                st.dataframe(audits, use_container_width=True, hide_index=True)
    except Exception:
        pass


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

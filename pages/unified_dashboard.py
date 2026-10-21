"""
Unified Dashboard UI for Momentum Radar v5.5 + Freqtrade Integration.

Provides a cohesive Streamlit interface combining legacy Momentum Radar v5.5 (Binance scanner,
SMC & technical indicators, multi-agent AI Consensus, separated paper trading) with Freqtrade
engine capabilities (Strategy Manager, Data Manager, Backtesting, Hyperopt, Dry-Run Trading,
Trade Monitor, Performance Analytics, Bot Logs & System Health, and Settings).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

try:
    import plotly.graph_objects as go
except ImportError:  # pragma: no cover
    go = None

from services.backtest_service import BACKTEST_DISCLAIMER, BacktestService
from services.config_service import (
    ConfigService,
    LiveTradingSecurityError,
    redact_config_dict,
    sanitize_log_text,
)
from services.data_service import SUPPORTED_EXCHANGES, SUPPORTED_TIMEFRAMES, DataService
from services.freqtrade_client import FreqtradeClient
from services.hyperopt_service import (
    OFFICIAL_HYPEROPT_LOSS_FUNCTIONS,
    OFFICIAL_HYPEROPT_SPACES,
    HyperoptService,
)
from services.process_manager import ProcessManager
from services.strategy_service import StrategyService
from services.trading_service import TradingService


NAVIGATION_PAGES = [
    "🏠 Dashboard Utama",
    "🚨 Market Scanner & Analisis AI",
    "🧩 Strategy Manager",
    "🗄️ Data Manager",
    "🧪 Backtesting",
    "⚙️ Hyperopt",
    "🤖 Dry-Run & Live Control",
    "📡 Trade Monitor",
    "📈 Performance Analytics",
    "🩺 Bot Logs & System Health",
    "🔧 Settings & Konfigurasi",
]


def get_integration_services(project_root: str | Path | None = None) -> dict[str, Any]:
    """Initialize and return the modular Freqtrade integration services."""
    cfg_svc = ConfigService(project_root=project_root)
    conn_params = cfg_svc.get_api_connection_Params()
    api_client = FreqtradeClient(
        server_url=conn_params["server_url"],
        username=conn_params["username"],
        password=conn_params["password"],
        timeout=conn_params["timeout"],
    )
    proc_mgr = ProcessManager(log_dir=cfg_svc.user_data_dir / "logs")
    strat_svc = StrategyService(cfg_svc, api_client=api_client)
    data_svc = DataService(cfg_svc, proc_mgr, api_client=api_client)
    bt_svc = BacktestService(cfg_svc, proc_mgr, data_svc, strat_svc, api_client=api_client)
    hyp_svc = HyperoptService(cfg_svc, proc_mgr, strat_svc, backtest_service=bt_svc)
    trd_svc = TradingService(cfg_svc, proc_mgr, api_client, backtest_service=bt_svc)

    return {
        "config": cfg_svc,
        "client": api_client,
        "process": proc_mgr,
        "strategy": strat_svc,
        "data": data_svc,
        "backtest": bt_svc,
        "hyperopt": hyp_svc,
        "trading": trd_svc,
    }


def render_status_banner(st: Any, services: dict[str, Any], legacy_context: dict[str, Any]) -> dict[str, Any]:
    """
    Render the unified top telemetry header displaying backend connection state,
    bot mode, AI readiness, last update timestamp, and last error if any.
    """
    client: FreqtradeClient = services["client"]
    proc_mgr: ProcessManager = services["process"]
    cfg_svc: ConfigService = services["config"]

    conn_summary = client.get_connection_summary()
    active_jobs = proc_mgr.get_active_jobs()
    groq_ready = bool(legacy_context.get("groq_keys"))
    live_env = cfg_svc.is_live_trading_env_enabled()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "Engine Freqtrade",
        "🟢 REST ONLINE" if conn_summary["connected"] else "🟢 CLOUD/LOKAL AKTIF",
        delta=f"{conn_summary['latency_ms']} ms" if conn_summary["connected"] else "In-Process IStrategy",
    )
    c2.metric(
        "Status Bot",
        conn_summary["bot_state"] if conn_summary["connected"] else "DRY-RUN SIAP",
        delta="DRY-RUN (Aman)" if conn_summary.get("dry_run", True) else "LIVE MODE",
    )
    c3.metric(
        "AI Consensus (v5.5)",
        "🟢 GROQ SIAP" if groq_ready else "🟡 NON-AI MODE",
        delta=f"Job Aktif: {len(active_jobs)}",
    )
    c4.metric(
        "Live Trading Gate",
        "🔒 NONAKTIF" if not live_env else "⚠️ DIIZINKAN ENV",
        delta="Default: Nonaktif",
    )
    now_str = conn_summary.get("last_check_at") or datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    c5.metric("Pembaruan Terakhir", now_str[-12:], delta=conn_summary.get("strategy", "SampleStrategy"))

    if active_jobs:
        job_labels = ", ".join(f"{j.job_type} (PID {j.pid})" for j in active_jobs)
        st.info(f"⏳ Operasi background sedang berjalan: {job_labels}")

    return conn_summary


def render_main_dashboard_page(
    st: Any,
    services: dict[str, Any],
    legacy_context: dict[str, Any],
    conn_summary: dict[str, Any],
) -> None:
    """Render 🏠 Dashboard Utama combining Momentum Radar v5.5 and Freqtrade overview."""
    st.subheader("🏠 Ringkasan Eksekutif Sistem Terpadu (Momentum Radar v5.5 + Freqtrade)")
    st.caption(
        "Antarmuka terpadu yang menggabungkan pemindaian pasar Binance & AI Consensus (Momentum Radar v5.5) "
        "dengan mesin kuantitatif Freqtrade (Strategy Manager, Data OHLCV, Backtesting, Hyperopt, dan Dry-Run)."
    )

    # Quick navigation buttons so user can jump straight to any key workspace
    q1, q2, q3, q4, q5 = st.columns(5)
    if q1.button("🚨 Buka Market Scanner & AI", use_container_width=True, key="dash_jmp_scanner"):
        st.session_state["active_nav_page"] = "🚨 Market Scanner & Analisis AI"
        if hasattr(st, "rerun"):
            st.rerun()
    if q2.button("🧪 Buka Backtesting", use_container_width=True, key="dash_jmp_bt"):
        st.session_state["active_nav_page"] = "🧪 Backtesting"
        if hasattr(st, "rerun"):
            st.rerun()
    if q3.button("⚙️ Buka Hyperopt", use_container_width=True, key="dash_jmp_hyp"):
        st.session_state["active_nav_page"] = "⚙️ Hyperopt"
        if hasattr(st, "rerun"):
            st.rerun()
    if q4.button("🤖 Buka Kontrol Dry-Run", use_container_width=True, key="dash_jmp_dr"):
        st.session_state["active_nav_page"] = "🤖 Dry-Run & Live Control"
        if hasattr(st, "rerun"):
            st.rerun()
    if q5.button("🧩 Buka Strategy Manager", use_container_width=True, key="dash_jmp_sm"):
        st.session_state["active_nav_page"] = "🧩 Strategy Manager"
        if hasattr(st, "rerun"):
            st.rerun()

    trd_svc: TradingService = services["trading"]
    bt_svc: BacktestService = services["backtest"]
    strat_svc: StrategyService = services["strategy"]
    data_svc: DataService = services["data"]

    ft_summary = trd_svc.get_balance_and_profit_summary()
    legacy_perf_fn: Callable[[], dict[str, Any]] | None = legacy_context.get("performance_fn")
    legacy_perf = legacy_perf_fn() if callable(legacy_perf_fn) else {}
    v6_perf = legacy_perf.get("v6_realistic") if isinstance(legacy_perf.get("v6_realistic"), dict) else {}

    col_left, col_right = st.columns(2)
    with col_left:
        st.markdown("#### 🧠 Momentum Radar v5.5 (Paper Trading Internal)")
        m1, m2, m3 = st.columns(3)
        m1.metric(
            "Equity Paper (signals.db)",
            f"${float(v6_perf.get('current_equity', 10000.0)):,.2f}",
        )
        m2.metric("Posisi Terbuka", int(legacy_perf.get("open", 0) or 0))
        m3.metric("Win Rate Sinyal AI", f"{float(legacy_perf.get('win_rate', 0.0) or 0.0):.1f}%")
        st.caption(
            f"Database: `{services['config'].legacy_db_path.name}` · "
            "Terpisah sepenuhnya dari database transaksi Freqtrade."
        )

    with col_right:
        st.markdown("#### 🤖 Mesin Freqtrade (Dry-Run Terintegrasi)")
        f1, f2, f3 = st.columns(3)
        f1.metric(
            f"Saldo Simulasi ({ft_summary['stake_currency']})",
            f"{ft_summary['total_balance']:,.2f}",
            delta=f"{ft_summary['profit_closed_percent']:+.2f}%",
        )
        f2.metric("Total Transaksi Dry-Run", int(ft_summary["trade_count"]))
        f3.metric("Win Rate Freqtrade", f"{ft_summary['winrate_pct']:.1f}%")
        st.caption(
            f"Sumber Data: `{ft_summary['source']}` · "
            f"Database: `{services['config'].dryrun_db_path.name}`"
        )

    st.divider()
    s_col1, s_col2, s_col3 = st.columns(3)
    strats = strat_svc.list_strategies()
    local_data = data_svc.list_local_data("binance")
    bt_files = bt_svc.list_backtest_result_files()

    s_col1.metric("Strategi Freqtrade Tersedia", len(strats))
    s_col2.metric("Dataset OHLCV Lokal", len(local_data))
    s_col3.metric("Arsip Hasil Backtesting", len(bt_files))

    if bt_files:
        st.markdown("#### 📋 Ringkasan Hasil Backtest Terakhir")
        st.dataframe(pd.DataFrame(bt_files), use_container_width=True, hide_index=True)


def render_strategy_manager_page(
    st: Any, services: dict[str, Any], legacy_context: dict[str, Any]
) -> None:
    """Render 🧩 Strategy Manager page."""
    st.subheader("🧩 Strategy Manager & Adapter Sinyal AI")
    st.caption(
        "Kelola strategi `IStrategy` Freqtrade, periksa validasi sintaks & risiko lookahead bias, "
        "dan jembatan sinyal AI Momentum Radar ke strategi Freqtrade secara aman."
    )

    strat_svc: StrategyService = services["strategy"]
    cfg_svc: ConfigService = services["config"]
    strategies = strat_svc.list_strategies()

    if not strategies:
        st.warning(
            f"Belum ada file strategi di `{cfg_svc.user_data_dir / 'strategies'}`. "
            "Tambahkan file `.py` yang mewarisi `IStrategy`."
        )
        return

    rows = []
    for s in strategies:
        rows.append(
            {
                "Nama Strategi": s["name"],
                "Status Validasi": "✅ VALID" if s["valid"] else "❌ INVALID",
                "Timeframe": s["timeframe"],
                "Can Short": "Ya" if s["can_short"] else "Tidak",
                "Stoploss": f"{float(s['stoploss']) * 100:.1f}%",
                "Startup Candles": s["startup_candle_count"],
                "Parameter Hyperopt": len(s.get("hyperopt_parameters", [])),
                "File": s["file_name"],
                "Lokasi": s["location"],
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    strat_names = [s["name"] for s in strategies]
    selected_name = st.selectbox("Pilih strategi untuk inspeksi & konfigurasi aktif", strat_names, key="sm_select_strat")
    selected = next((s for s in strategies if s["name"] == selected_name), strategies[0])

    col_a, col_b = st.columns([2, 1])
    with col_a:
        if selected["valid"]:
            st.success(f"Strategi `{selected['name']}` valid dan memenuhi kontrak `IStrategy` v{selected['interface_version']}.")
        else:
            st.error("Strategi tidak valid: " + " | ".join(selected["errors"]))

        if selected["warnings"]:
            for w in selected["warnings"]:
                st.warning(f"⚠️ Peringatan Audit Strategi: {w}")

        if selected.get("hyperopt_parameters"):
            st.markdown("##### Parameter Hyperopt yang Ditemukan")
            st.dataframe(pd.DataFrame(selected["hyperopt_parameters"]), use_container_width=True, hide_index=True)
        else:
            st.info("Strategi ini tidak mendefinisikan parameter Hyperopt dinamis.")

    with col_b:
        st.markdown("##### Konfigurasi Strategi")
        st.json(
            {
                "strategy": selected["name"],
                "timeframe": selected["timeframe"],
                "stoploss": selected["stoploss"],
                "minimal_roi": selected["minimal_roi"],
                "trailing_stop": selected["trailing_stop"],
                "can_short": selected["can_short"],
                "methods": selected["methods"],
            }
        )
        if st.button("📌 Jadikan Strategi Default (Dry-Run / Backtest)", use_container_width=True, key="sm_set_default"):
            if not selected["valid"]:
                st.error("Tidak dapat memilih strategi yang tidak valid.")
            else:
                cfg = cfg_svc.load_freqtrade_config(create_if_missing=True)
                cfg["strategy"] = selected["name"]
                cfg["timeframe"] = selected["timeframe"]
                cfg_svc.save_freqtrade_config(cfg, allow_live=False)
                st.success(f"Strategi `{selected['name']}` disimpan sebagai strategi aktif di `config.json`.")

    with st.expander("🔗 Jembatan Eksplisit Sinyal AI Momentum Radar -> Freqtrade (`MomentumRadarAIStrategy`)", expanded=False):
        st.markdown(
            """
            **Aturan Keamanan Integrasi Sinyal AI:**
            1. Sinyal LONG/SHORT dari Momentum Radar v5.5 **tidak pernah** dikirim langsung menjadi order live ke exchange.
            2. Adapter `StrategyService.export_ai_signal_for_freqtrade()` memvalidasi format simbol (`BTCUSDT` -> `BTC/USDT`), persetujuan gate risiko (`gate_approved=True`), confidence minimum (`>= 50`), serta timestamp bar tertutup (`bar_time`).
            3. Strategi `MomentumRadarAIStrategy.py` hanya membaca sinyal pada candle yang memiliki `candle_date >= signal_bar_time` (bebas *lookahead bias*) dan tetap mewajibkan konfirmasi indikator teknikal.
            """
        )
        if st.button("🔄 Ekspor Sinyal AI Valid Terakhir dari signals.db ke Bridge Freqtrade", key="sm_export_ai_bridge"):
            db_fn = legacy_context.get("db_fn")
            if callable(db_fn):
                con = db_fn()
                rows_sig = con.execute(
                    """
                    SELECT analysis_id, symbol, timeframe, final_decision, confidence,
                           entry, stop_loss, take_profit, reasoning, created_at
                    FROM signals
                    WHERE final_decision IN ('LONG', 'SHORT')
                    ORDER BY id DESC LIMIT 20
                    """
                ).fetchall()
                con.close()
                exported_count = 0
                for r in rows_sig:
                    ok, _, _ = strat_svc.export_ai_signal_for_freqtrade(
                        {
                            "analysis_id": r["analysis_id"],
                            "symbol": r["symbol"],
                            "timeframe": r["timeframe"],
                            "final": r["final_decision"],
                            "confidence": r["confidence"],
                            "entry": r["entry"],
                            "stop_loss": r["stop_loss"],
                            "take_profit": r["take_profit"],
                            "reasoning": r["reasoning"],
                            "bar_time": r["created_at"],
                            "gate_approved": True,
                        }
                    )
                    if ok:
                        exported_count += 1
                st.success(f"Berhasil mengekspor {exported_count} sinyal AI tervalidasi ke `user_data/ai_signal_bridge.json`.")

    with st.expander("📖 Petunjuk Penempatan Strategi Baru", expanded=False):
        st.markdown(
            f"""
            1. Tempatkan file strategi Python baru Anda (`.py`) di direktori: `{cfg_svc.user_data_dir / 'strategies'}`.
            2. Pastikan class strategi mewarisi `IStrategy` (`from freqtrade.strategy import IStrategy`) dan mendefinisikan `INTERFACE_VERSION = 3`.
            3. Implementasikan ketiga method wajib: `populate_indicators()`, `populate_entry_trend()`, dan `populate_exit_trend()`.
            4. Hindari penggunaan `.shift(-1)` atau referensi masa depan agar lolos pemeriksaan *lookahead bias*.
            """
        )


def render_data_manager_page(st: Any, services: dict[str, Any]) -> None:
    """Render 🗄️ Data Manager page."""
    st.subheader("🗄️ Data Manager (Data Historis OHLCV)")
    st.caption(
        "Periksa ketersediaan data historis lokal, validasi kelengkapan data sebelum backtesting, "
        "dan unduh dataset OHLCV secara terkontrol."
    )

    data_svc: DataService = services["data"]
    proc_mgr: ProcessManager = services["process"]

    col1, col2, col3 = st.columns(3)
    exchange = col1.selectbox("Exchange", SUPPORTED_EXCHANGES, index=0, key="dm_exchange")
    timeframe = col2.selectbox("Timeframe", SUPPORTED_TIMEFRAMES, index=2, key="dm_tf")
    days = col3.number_input("Jumlah Hari Unduhan", min_value=1, max_value=365, value=30, key="dm_days")

    local_files = data_svc.list_local_data(exchange=exchange)
    st.markdown(f"#### 📂 Dataset Lokal Tersedia ({len(local_files)} file di `{exchange}`)")
    if local_files:
        st.dataframe(pd.DataFrame(local_files), use_container_width=True, hide_index=True)
    else:
        st.info(f"Belum ada dataset OHLCV lokal untuk exchange `{exchange}`.")

    st.divider()
    st.markdown("#### ⬇️ Unduh Data Historis Baru (Kendali Pengguna)")
    default_pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]
    avail_pairs = sorted({r["pair"] for r in local_files} | set(default_pairs))
    selected_pairs = st.multiselect(
        "Pilih Pasangan Trading",
        avail_pairs,
        default=["BTC/USDT", "XRP/USDT"],
        key="dm_pairs",
    )

    dcol1, dcol2 = st.columns(2)
    if dcol1.button("🚀 Jalankan `freqtrade download-data` (Background / Cloud Mirror)", use_container_width=True, key="dm_btn_ft_dl"):
        try:
            with st.spinner("Memulai pekerjaan unduhan data..."):
                res = data_svc.start_download_data_job(
                    exchange=exchange,
                    pairs=selected_pairs,
                    timeframes=[timeframe],
                    days=int(days),
                )
            if res.get("started"):
                st.success(res["message"])
            else:
                st.warning(res["message"])
        except Exception as exc:
            st.error(f"Gagal memulai unduhan data: {exc}")

    if dcol2.button("🌐 Unduh Klines Publik Binance ke `.feather` Lokal", use_container_width=True, key="dm_btn_binance_dl"):
        if not selected_pairs:
            st.warning("Pilih minimal satu pasangan trading.")
        else:
            with st.spinner("Mengunduh klines publik Binance..."):
                for p in selected_pairs:
                    out = data_svc.download_binance_public_klines_to_feather(p, timeframe=timeframe, limit=500)
                    if out.get("ok"):
                        st.success(f"{p} ({timeframe}): {out['candles']} candle disimpan ({out['start_date']} – {out['end_date']}).")
                    else:
                        st.error(f"{p}: {out.get('error')}")

    st.divider()
    st.markdown("#### ✅ Validasi Data Sebelum Backtesting")
    if st.button("🔍 Validasi Kesiapan Data untuk Pasangan Terpilih", key="dm_btn_validate"):
        ok, errs, details = data_svc.validate_data_for_backtest(
            selected_pairs, timeframe, exchange=exchange, min_candles=30
        )
        if details:
            st.dataframe(pd.DataFrame(details), use_container_width=True, hide_index=True)
        if ok:
            st.success("Seluruh pasangan terpilih memiliki data OHLCV yang valid dan siap untuk backtesting.")
        else:
            for e in errs:
                st.warning(e)

    dl_jobs = [j for j in proc_mgr.refresh_jobs() if j.job_type == "download_data"]
    if dl_jobs:
        with st.expander("📜 Riwayat & Log Proses Unduhan Data", expanded=False):
            for j in dl_jobs[:5]:
                st.markdown(f"**{j.job_id}** · Status: `{j.status}` · PID: `{j.pid}`")
                st.code(proc_mgr.read_job_log(j.job_id, max_lines=40))


def render_backtesting_page(st: Any, services: dict[str, Any]) -> None:
    """Render 🧪 Backtesting page."""
    st.subheader("🧪 Backtesting & Komparasi Strategi")
    st.warning(BACKTEST_DISCLAIMER)

    bt_svc: BacktestService = services["backtest"]
    strat_svc: StrategyService = services["strategy"]
    data_svc: DataService = services["data"]
    proc_mgr: ProcessManager = services["process"]

    strategies = [s["name"] for s in strat_svc.list_strategies() if s["valid"]]
    local_data = data_svc.list_local_data("binance")
    local_pairs = sorted({r["pair"] for r in local_data}) or ["BTC/USDT", "XRP/USDT"]

    with st.expander("⚙️ Konfigurasi & Eksekusi Backtesting Baru", expanded=True):
        c1, c2, c3 = st.columns(3)
        chosen_strat = c1.selectbox(
            "Strategi",
            strategies or ["SampleStrategy"],
            key="bt_strategy",
        )
        chosen_tf = c2.selectbox("Timeframe", SUPPORTED_TIMEFRAMES, index=2, key="bt_tf")
        timerange = c3.text_input(
            "Timerange (YYYYMMDD-YYYYMMDD, opsional)",
            value="",
            placeholder="Contoh: 20251124-20251205",
            key="bt_timerange",
        )

        p1, p2, p3, p4 = st.columns(4)
        wallet = p1.number_input("Modal Awal (dry_run_wallet)", min_value=10.0, value=1000.0, step=100.0, key="bt_wallet")
        stake = p2.number_input("Stake Amount", min_value=1.0, value=100.0, step=10.0, key="bt_stake")
        max_open = p3.number_input("Max Open Trades", min_value=1, max_value=20, value=3, key="bt_max_open")
        fee = p4.number_input("Fee Transaksi (Desimal)", min_value=0.0, max_value=0.05, value=0.001, step=0.0005, format="%.4f", key="bt_fee")

        chosen_pairs = st.multiselect(
            "Pasangan Trading untuk Backtest",
            local_pairs,
            default=local_pairs[:2],
            key="bt_pairs",
        )

        if st.button("▶️ Jalankan Backtesting Strategi IStrategy", use_container_width=True, key="bt_run_btn"):
            with st.spinner("Memvalidasi konfigurasi & mengeksekusi backtesting..."):
                res = bt_svc.start_backtest(
                    strategy=chosen_strat,
                    pairs=chosen_pairs,
                    timeframe=chosen_tf,
                    timerange=timerange.strip() or None,
                    dry_run_wallet=float(wallet),
                    stake_amount=float(stake),
                    max_open_trades=int(max_open),
                    fee=float(fee),
                )
            if res.get("started"):
                st.success(res["message"])
            else:
                st.warning(res["message"])

    bt_jobs = [j for j in proc_mgr.refresh_jobs() if j.job_type == "backtesting"]
    if bt_jobs:
        with st.expander("📜 Status & Log Eksekusi Backtesting", expanded=False):
            for j in bt_jobs[:3]:
                st.markdown(f"**{j.job_id}** · Status: `{j.status}` · PID: `{j.pid}`")
                st.code(proc_mgr.read_job_log(j.job_id, max_lines=50))

    st.divider()
    saved_runs = bt_svc.list_backtest_result_files()
    st.markdown(f"#### 📊 Analisis Hasil Backtesting Resmi ({len(saved_runs)} hasil tersimpan)")
    if not saved_runs:
        st.info("Belum ada file hasil backtesting di `user_data/backtest_results/`.")
        return

    run_ids = [r["id"] for r in saved_runs]
    selected_run_id = st.selectbox("Pilih Hasil Pengujian", run_ids, index=0, key="bt_select_result")
    fname, strat_name = selected_run_id.split("::", 1)

    parsed = bt_svc.load_backtest_result(fname, strategy_name=strat_name)
    m = parsed["metrics"]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(
        "Total Profit / Return",
        f"{m['profit_total_abs']:+.6f} {m['stake_currency']}",
        delta=f"{m['profit_total_pct']:+.2f}%",
    )
    k2.metric("Jumlah Transaksi", f"{m['total_trades']} ({m['wins']}W / {m['draws']}D / {m['losses']}L)")
    k3.metric("Win Rate", f"{m['win_rate_pct']:.2f}%", delta=f"Profit Factor: {m['profit_factor']:.2f}")
    k4.metric(
        "Max Drawdown",
        f"{m['max_drawdown_account_pct']:.2f}%",
        delta=f"{m['max_drawdown_abs']:.6f} {m['stake_currency']}",
    )

    k5, k6, k7, k8 = st.columns(4)
    k5.metric("Durasi Transaksi Rata-rata", m["holding_avg"])
    k6.metric("Rata-rata Profit/Loss", f"{m['avg_profit_pct']:+.2f}%")
    k7.metric("Modal Awal -> Akhir", f"{m['starting_balance']:.4f} -> {m['final_balance']:.4f} {m['stake_currency']}")
    k8.metric("Expectancy Ratio", f"{m['expectancy_ratio']:.2f}")

    eq_df: pd.DataFrame = parsed["equity_curve"]
    trades_df: pd.DataFrame = parsed["trades_df"]

    if not eq_df.empty and go is not None:
        st.markdown("##### 📈 Equity Curve & Cumulative Profit")
        fig_eq = go.Figure()
        fig_eq.add_trace(
            go.Scatter(
                x=eq_df["date"],
                y=eq_df["equity"],
                mode="lines+markers",
                name=f"Equity ({m['stake_currency']})",
            )
        )
        fig_eq.update_layout(
            title=f"Equity Curve — {m['strategy_name']} ({m['file_name']})",
            xaxis_title="Waktu Penutupan Transaksi",
            yaxis_title=f"Saldo ({m['stake_currency']})",
            height=360,
        )
        st.plotly_chart(fig_eq, use_container_width=True)

    if not trades_df.empty:
        st.markdown("##### 📊 Distribusi Hasil Transaksi & Performa per Pasangan")
        col_dist1, col_dist2 = st.columns(2)
        with col_dist1:
            if parsed.get("results_per_pair"):
                st.dataframe(pd.DataFrame(parsed["results_per_pair"]), use_container_width=True, hide_index=True)
        with col_dist2:
            if parsed.get("exit_reason_summary"):
                st.dataframe(pd.DataFrame(parsed["exit_reason_summary"]), use_container_width=True, hide_index=True)

        with st.expander("📋 Daftar Transaksi Detail", expanded=False):
            display_cols = [
                c
                for c in (
                    "pair",
                    "open_date",
                    "close_date",
                    "open_rate",
                    "close_rate",
                    "profit_ratio",
                    "profit_abs",
                    "trade_duration",
                    "exit_reason",
                )
                if c in trades_df.columns
            ]
            st.dataframe(trades_df[display_cols], use_container_width=True, hide_index=True)

    if len(run_ids) >= 2:
        st.divider()
        st.markdown("#### ⚖️ Perbandingan Beberapa Hasil Backtesting")
        compare_selection = st.multiselect(
            "Pilih Pengujian untuk Dibandingkan",
            run_ids,
            default=run_ids[: min(3, len(run_ids))],
            key="bt_compare_multi",
        )
        if compare_selection:
            comp_df = bt_svc.compare_backtest_runs(compare_selection)
            st.dataframe(comp_df, use_container_width=True, hide_index=True)


def render_hyperopt_page(st: Any, services: dict[str, Any]) -> None:
    """Render ⚙️ Hyperopt page."""
    st.subheader("⚙️ Hyperopt (Optimasi Parameter Strategi)")
    st.caption(
        "Jalankan pencarian parameter optimal dan tinjau hasilnya secara eksplisit "
        "sebelum menerapkan parameter ke strategi."
    )

    hyp_svc: HyperoptService = services["hyperopt"]
    strat_svc: StrategyService = services["strategy"]
    proc_mgr: ProcessManager = services["process"]

    dep_ok, _, dep_msg = hyp_svc.check_hyperopt_dependencies()
    if not dep_ok:
        st.info(f"ℹ️ Mode Engine Hyperopt: {dep_msg}")

    strategies = [s["name"] for s in strat_svc.list_strategies() if s["valid"]]

    with st.expander("🚀 Konfigurasi & Jalankan Hyperopt", expanded=True):
        h1, h2, h3 = st.columns(3)
        h_strat = h1.selectbox("Strategi", strategies or ["SampleStrategy"], key="hyp_strat")
        h_epochs = h2.number_input("Jumlah Epoch", min_value=5, max_value=5000, value=20, step=5, key="hyp_epochs")
        h_loss = h3.selectbox("Loss Function Resmi", OFFICIAL_HYPEROPT_LOSS_FUNCTIONS, index=0, key="hyp_loss")

        h4, h5 = st.columns(2)
        h_spaces = h4.multiselect(
            "Ruang Optimasi (Spaces)",
            OFFICIAL_HYPEROPT_SPACES,
            default=["buy", "sell", "roi", "stoploss"],
            key="hyp_spaces",
        )
        h_tf = h5.selectbox("Timeframe", SUPPORTED_TIMEFRAMES, index=2, key="hyp_tf")

        bcol1, bcol2 = st.columns(2)
        if bcol1.button("▶️ Mulai Hyperopt", use_container_width=True, key="hyp_start_btn"):
            with st.spinner("Menjalankan optimasi parameter Hyperopt..."):
                res = hyp_svc.start_hyperopt(
                    strategy=h_strat,
                    epochs=int(h_epochs),
                    spaces=h_spaces,
                    hyperopt_loss=h_loss,
                    timeframe=h_tf,
                )
            if res.get("started"):
                st.success(res["message"])
            else:
                st.warning(res["message"])

        active_hyp = proc_mgr.get_active_jobs("hyperopt")
        if bcol2.button("⏹️ Hentikan Proses Hyperopt Aktif", use_container_width=True, key="hyp_stop_btn"):
            if active_hyp:
                for j in active_hyp:
                    proc_mgr.stop_job(j.job_id, reason="Dihentikan oleh pengguna dari halaman Hyperopt")
                st.success("Proses Hyperopt berhasil dihentikan.")
            else:
                st.info("Tidak ada proses Hyperopt yang sedang berjalan.")

    hyp_jobs = [j for j in proc_mgr.refresh_jobs() if j.job_type == "hyperopt"]
    if hyp_jobs:
        with st.expander("📜 Status & Log Background Job Hyperopt", expanded=False):
            for j in hyp_jobs[:3]:
                st.markdown(f"**{j.job_id}** · Status: `{j.status}` · PID: `{j.pid}`")
                st.code(proc_mgr.read_job_log(j.job_id, max_lines=50))

    st.divider()
    hyp_files = hyp_svc.list_hyperopt_result_files()
    st.markdown(f"#### 🏆 Tinjauan Hasil Optimasi Hyperopt ({len(hyp_files)} file `.fthypt`)")
    if not hyp_files:
        st.info("Belum ada file hasil Hyperopt (`.fthypt`) di `user_data/hyperopt_results/`.")
        return

    file_names = [f["file_name"] for f in hyp_files]
    selected_fn = st.selectbox("Pilih File Hasil Hyperopt", file_names, index=0, key="hyp_select_file")
    loaded = hyp_svc.load_hyperopt_results(selected_fn)

    if loaded["total_epochs"] == 0:
        st.warning("File hasil Hyperopt kosong.")
        return

    st.dataframe(loaded["epochs_df"], use_container_width=True, hide_index=True)

    best_ep = loaded["best_epoch"]
    if best_ep:
        st.markdown(
            f"##### 🌟 Epoch Terbaik: #{best_ep.get('current_epoch')} "
            f"(Loss: `{best_ep.get('loss'):.6f}` · `{best_ep.get('results_explanation', '').strip()}`)"
        )
        st.json(best_ep.get("params_details", {}))

        st.info(
            "Keamanan Produksi: Parameter hasil Hyperopt TIDAK diterapkan secara otomatis. "
            "Centang persetujuan di bawah ini setelah meninjau parameter untuk menyimpannya ke file JSON strategi."
        )
        confirm_apply = st.checkbox(
            "Saya telah meninjau hasil optimasi ini dan menyetujui penerapan parameter ke strategi.",
            value=False,
            key="hyp_confirm_apply",
        )
        if st.button("💾 Terapkan Parameter Hasil Optimasi ke Strategi", key="hyp_apply_btn"):
            try:
                target_strat = (
                    best_ep.get("results_metrics", {}).get("strategy_name") or "SampleStrategy"
                )
                out = hyp_svc.apply_hyperopt_parameters(
                    strategy_name=target_strat,
                    epoch_data=best_ep,
                    user_confirmed=bool(confirm_apply),
                )
                st.success(
                    f"Parameter Epoch #{out['epoch']} berhasil disimpan ke `{out['parameter_file']}`."
                )
            except Exception as exc:
                st.error(str(exc))


def render_dryrun_and_live_page(st: Any, services: dict[str, Any]) -> None:
    """Render 🤖 Dry-Run & Live Control page."""
    st.subheader("🤖 Kontrol Bot Trading (Dry-Run Default & Pengaman Live Trading)")
    st.caption(
        "Jalankan dan hentikan bot Freqtrade dalam mode Dry-Run secara aman. "
        "Posisi Dry-Run disimpan di `tradesv3.dryrun.sqlite` dan tidak dicampur dengan paper trading `signals.db`."
    )

    trd_svc: TradingService = services["trading"]
    strat_svc: StrategyService = services["strategy"]
    cfg_svc: ConfigService = services["config"]

    summary = trd_svc.get_balance_and_profit_summary()
    strategies = [s["name"] for s in strat_svc.list_strategies() if s["valid"]]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Mode Operasi Default", "DRY-RUN (Simulasi)")
    m2.metric(
        f"Saldo Simulasi ({summary['stake_currency']})",
        f"{summary['total_balance']:,.2f}",
        delta=f"{summary['profit_closed_percent']:+.2f}%",
    )
    m3.metric("Transaksi Selesai", int(summary["closed_trade_count"]))
    m4.metric("Win Rate Dry-Run", f"{summary['winrate_pct']:.1f}%")

    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    chosen_strat = c1.selectbox(
        "Strategi Dry-Run",
        strategies or ["SampleStrategy"],
        key="dr_strat",
    )
    if c2.button("▶️ Mulai Bot Dry-Run", use_container_width=True, key="dr_start_btn"):
        res = trd_svc.start_dry_run_bot(strategy=chosen_strat)
        if res.get("ok"):
            st.success(res["message"])
        else:
            st.warning(res["message"])

    if c3.button("⚡ Evaluasi 1 Siklus", use_container_width=True, key="dr_step_btn"):
        res_step = trd_svc.run_single_dry_run_cycle(strategy=chosen_strat)
        if res_step.get("ok"):
            st.success(res_step["message"])
        else:
            st.warning(res_step["message"])

    if c4.button("⏹️ Hentikan Bot", use_container_width=True, key="dr_stop_btn"):
        res = trd_svc.stop_bot()
        st.success(res["message"])

    st.divider()
    with st.expander("🔒 Gerbang Keamanan Live Trading (Nonaktif Secara Default)", expanded=False):
        st.error(
            "⚠️ PERINGATAN KERAS: Live Trading mempertaruhkan dana sungguhan di exchange. "
            "Secara default, `dry_run=true` dan `ENABLE_LIVE_TRADING=false`. "
            "Jika Anda menggunakan API Key exchange, batasi hanya pada izin Spot/Futures Trading, "
            "NONAKTIFKAN izin penarikan dana (Withdrawal), dan gunakan pembatasan IP whitelist."
        )
        live_env = cfg_svc.is_live_trading_env_enabled()
        st.markdown(
            f"- Status Environment `ENABLE_LIVE_TRADING`: **`{'true' if live_env else 'false (Terkunci Aman)'}`**"
        )
        risk_ack = st.checkbox(
            "Saya memahami risiko kehilangan modal dan telah memverifikasi izin API key tanpa fitur penarikan dana.",
            value=False,
            key="live_risk_ack",
        )
        confirm_phrase = st.text_input(
            f"Ketik frasa konfirmasi berikut secara persis: `{ConfigService.LIVE_CONFIRMATION_PHRASE}`",
            value="",
            key="live_confirm_phrase",
        )
        if st.button("🔐 Verifikasi Kelayakan Aktivasi Live Trading", key="live_verify_btn"):
            try:
                out = trd_svc.request_live_trading_activation(
                    confirmation_phrase=confirm_phrase,
                    user_checked_risk_box=bool(risk_ack),
                    strategy=chosen_strat,
                )
                st.success(out["message"])
            except LiveTradingSecurityError as exc:
                st.error(f"Aktivasi Live Trading Diblokir: {exc}")


def render_trade_monitor_page(
    st: Any, services: dict[str, Any], legacy_context: dict[str, Any]
) -> None:
    """Render 📡 Trade Monitor page showing separated Freqtrade vs Legacy Paper trades."""
    st.subheader("📡 Trade Monitor (Pemisahan Sumber Data Transaksi)")
    st.caption(
        "Pantau posisi aktif dan riwayat transaksi dengan pemisahan tegas antara "
        "Freqtrade Dry-Run (`tradesv3.dryrun.sqlite` / REST API) dan Paper Trading Momentum Radar (`signals.db`)."
    )

    trd_svc: TradingService = services["trading"]
    open_info = trd_svc.get_open_trades()
    hist_info = trd_svc.get_trade_history(limit=100)

    tab_ft, tab_legacy = st.tabs(
        [
            f"🤖 Transaksi Freqtrade Dry-Run ({open_info['count']} Aktif)",
            "🧠 Paper Trading Momentum Radar v5.5 (signals.db)",
        ]
    )

    with tab_ft:
        st.markdown(f"**Sumber Data:** `{open_info['source']}`")
        st.markdown("##### Posisi Aktif Freqtrade")
        if open_info["open_trades"]:
            st.dataframe(pd.DataFrame(open_info["open_trades"]), use_container_width=True, hide_index=True)
        else:
            st.info("Tidak ada posisi terbuka di Freqtrade saat ini.")

        st.markdown("##### Riwayat Transaksi Tertutup Freqtrade")
        if hist_info["trades"]:
            st.dataframe(pd.DataFrame(hist_info["trades"]), use_container_width=True, hide_index=True)
        else:
            st.info("Belum ada riwayat transaksi tertutup di database dry-run Freqtrade.")

    with tab_legacy:
        st.markdown(f"**Sumber Data:** `{services['config'].legacy_db_path.name}` (Paper Trading Asli v5.5)")
        db_fn = legacy_context.get("db_fn")
        fmt_fn = legacy_context.get("format_signal_status_fn")
        if callable(db_fn):
            con = db_fn()
            rows = con.execute(
                """
                SELECT id, created_at, symbol, final_decision, confidence,
                       entry, stop_loss, take_profit, result, r_multiple,
                       accounting_mode
                FROM signals
                ORDER BY id DESC
                LIMIT 100
                """
            ).fetchall()
            con.close()
            if rows:
                items = []
                for r in rows:
                    items.append(
                        {
                            "ID": r["id"],
                            "Waktu": str(r["created_at"] or "-")[:19].replace("T", " "),
                            "Pair": r["symbol"],
                            "Keputusan AI": r["final_decision"],
                            "Confidence": round(float(r["confidence"] or 0.0)),
                            "Entry": r["entry"],
                            "Stop Loss": r["stop_loss"],
                            "Take Profit": r["take_profit"],
                            "Status": fmt_fn(r) if callable(fmt_fn) else (r["result"] or "OPEN"),
                        }
                    )
                st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)
            else:
                st.info("Belum ada sinyal paper trading di `signals.db`.")


def render_performance_analytics_page(
    st: Any, services: dict[str, Any], legacy_context: dict[str, Any]
) -> None:
    """Render 📈 Performance Analytics page."""
    st.subheader("📈 Performance Analytics")
    st.caption(
        "Analisis metrik performa kuantitatif untuk pengujian Freqtrade dan simulasi paper trading Momentum Radar v5.5."
    )

    trd_svc: TradingService = services["trading"]
    bt_svc: BacktestService = services["backtest"]
    ft_prof = trd_svc.get_balance_and_profit_summary()

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 🤖 Performa Freqtrade Dry-Run")
        st.json(ft_prof)

    with col2:
        st.markdown("#### 🧠 Performa Paper Trading Momentum Radar v5.5")
        perf_fn = legacy_context.get("performance_fn")
        if callable(perf_fn):
            p = perf_fn() or {}
            st.json(
                {
                    "closed_trades": p.get("closed", 0),
                    "open_trades": p.get("open", 0),
                    "win_rate_pct": p.get("win_rate", 0.0),
                    "avg_r": p.get("avg_r", 0.0),
                    "avg_net_r": p.get("avg_net_r", 0.0),
                    "total_net_pnl": p.get("total_net_pnl", 0.0),
                    "profit_factor": p.get("profit_factor", 0.0),
                    "accounting_mode": p.get("accounting_mode", "REALISTIC"),
                }
            )

    bt_runs = bt_svc.list_backtest_result_files()
    if bt_runs:
        st.divider()
        st.markdown("#### 📊 Performa Backtest Terakhir per Pasangan")
        latest = bt_svc.load_backtest_result(bt_runs[0]["file_name"], strategy_name=bt_runs[0]["strategy"])
        if latest.get("results_per_pair"):
            st.dataframe(pd.DataFrame(latest["results_per_pair"]), use_container_width=True, hide_index=True)


def render_logs_and_health_page(st: Any, services: dict[str, Any]) -> None:
    """Render 🩺 Bot Logs & System Health page."""
    st.subheader("🩺 Bot Logs & System Health")
    st.caption(
        "Diagnostik koneksi REST API, kesehatan proses background, pemeriksaan pemisahan database, "
        "dan log yang telah disanitasi dari rahasia/token."
    )

    cfg_svc: ConfigService = services["config"]
    proc_mgr: ProcessManager = services["process"]
    client: FreqtradeClient = services["client"]

    db_ok, db_msg = cfg_svc.validate_database_separation()
    cli_ok, cli_prefix, cli_msg = proc_mgr.resolve_freqtrade_command_prefix()
    conn_summary = client.get_connection_summary()

    h1, h2, h3 = st.columns(3)
    h1.metric("Pemisahan Database", "✅ AMAN" if db_ok else "❌ KONFLIK")
    h2.metric("Mode Engine", "🟢 CLI LOKAL" if cli_ok else "☁️ CLOUD IN-PROCESS / DOCKER")
    h3.metric("REST API Ping", "🟢 PONG" if conn_summary["connected"] else "⚪ STANDALONE")

    st.info(db_msg)
    st.caption(f"Engine Info (`{' '.join(cli_prefix)}`): {cli_msg}")

    if conn_summary["connected"]:
        logs_res = client.logs(limit=80)
        if logs_res.ok and isinstance(logs_res.data, dict):
            st.markdown("#### 📡 Log Langsung dari Freqtrade REST API")
            log_entries = logs_res.data.get("logs", [])
            formatted_logs = "\n".join(
                " | ".join(str(part) for part in row) if isinstance(row, list) else str(row)
                for row in log_entries
            )
            st.code(sanitize_log_text(formatted_logs) or "Belum ada log.")

    st.markdown("#### 🗂️ Daftar Pekerjaan Background (`ProcessManager`)")
    jobs = proc_mgr.refresh_jobs()
    if jobs:
        j_rows = [
            {
                "Job ID": j.job_id,
                "Tipe": j.job_type,
                "Status": j.status,
                "PID": j.pid,
                "Waktu Mulai": j.created_at,
                "Waktu Selesai": j.ended_at or "-",
                "Kode Keluar": j.return_code,
                "Error": j.error or "-",
            }
            for j in jobs
        ]
        st.dataframe(pd.DataFrame(j_rows), use_container_width=True, hide_index=True)
        selected_job = st.selectbox("Pilih Job ID untuk melihat log", [j.job_id for j in jobs], key="lh_select_job")
        st.code(proc_mgr.read_job_log(selected_job, max_lines=150))
    else:
        st.info("Belum ada pekerjaan background yang tercatat pada sesi ini.")


def render_settings_page(st: Any, services: dict[str, Any]) -> None:
    """Render 🔧 Settings & Konfigurasi page."""
    st.subheader("🔧 Settings & Konfigurasi Terpadu")
    st.caption(
        "Kelola dan validasi konfigurasi Freqtrade (`user_data/config.json`) dengan seluruh API key, "
        "secret, password, dan JWT disensor secara otomatis pada tampilan."
    )

    cfg_svc: ConfigService = services["config"]
    cfg = cfg_svc.load_freqtrade_config(create_if_missing=True)
    is_valid, errors, warnings = cfg_svc.validate_freqtrade_config(cfg, allow_live=False)

    if is_valid:
        st.success("Konfigurasi Freqtrade (`user_data/config.json`) valid dan mematuhi standar keamanan.")
    else:
        for err in errors:
            st.error(err)
    for w in warnings:
        st.warning(w)

    c1, c2, c3 = st.columns(3)
    new_max_open = c1.number_input(
        "Max Open Trades",
        min_value=1,
        max_value=50,
        value=int(cfg.get("max_open_trades", 3)),
        key="set_max_open",
    )
    new_stake = c2.number_input(
        "Stake Amount",
        min_value=1.0,
        value=float(cfg.get("stake_amount", 100.0)),
        step=10.0,
        key="set_stake",
    )
    new_wallet = c3.number_input(
        "Dry-Run Wallet",
        min_value=10.0,
        value=float(cfg.get("dry_run_wallet", 1000.0)),
        step=100.0,
        key="set_wallet",
    )

    if st.button("💾 Simpan Perubahan Parameter Konfigurasi (Dry-Run)", key="set_save_btn"):
        try:
            cfg["max_open_trades"] = int(new_max_open)
            cfg["stake_amount"] = float(new_stake)
            cfg["dry_run_wallet"] = float(new_wallet)
            cfg["dry_run"] = True
            saved_path = cfg_svc.save_freqtrade_config(cfg, allow_live=False)
            st.success(f"Konfigurasi berhasil divalidasi dan disimpan ke `{saved_path}` (dengan backup otomatis).")
        except Exception as exc:
            st.error(f"Gagal menyimpan konfigurasi: {exc}")

    st.markdown("#### 🔍 Pratinjau Konfigurasi (Rahasia Disensor)")
    st.json(redact_config_dict(cfg))


def render_unified_dashboard(
    st: Any,
    *,
    render_legacy_scanner_fn: Callable[[], None],
    legacy_context: dict[str, Any],
    project_root: str | Path | None = None,
) -> None:
    """
    Main entry point for rendering the unified Streamlit application.
    Preserves the original Momentum Radar v5.5 sidebar & scanner workflow while providing
    seamless navigation across all Freqtrade integration modules.
    """
    services = get_integration_services(project_root=project_root)

    # Default to "🏠 Dashboard Utama" so the new Unified Freqtrade + Momentum Radar UI is immediately visible!
    default_page = st.session_state.get("active_nav_page", NAVIGATION_PAGES[0])
    if default_page not in NAVIGATION_PAGES:
        default_page = NAVIGATION_PAGES[0]
    default_idx = NAVIGATION_PAGES.index(default_page)

    # Sidebar Navigation
    st.sidebar.markdown("### 🧭 Navigasi Sistem Terpadu")
    sidebar_selected = st.sidebar.radio(
        "Pilih Modul (11 Halaman)",
        NAVIGATION_PAGES,
        index=default_idx,
        key="unified_nav_radio",
    )
    st.sidebar.divider()

    # Main Canvas Top Banner & Module Selector (visible even when sidebar is collapsed on mobile/Streamlit Cloud)
    st.markdown(
        """
        <div style="background: linear-gradient(90deg, #0f172a 0%, #1e293b 60%, #0f766e 100%);
                    padding: 14px 20px; border-radius: 10px; border: 1px solid #14b8a6; margin-bottom: 12px;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;">
                <div>
                    <span style="color:#2dd4bf; font-weight:700; font-size:0.85rem; letter-spacing:1px;">
                        ⚡ UNIFIED QUANT SUITE · MOMENTUM RADAR v5.5 + FREQTRADE ENGINE
                    </span>
                    <h3 style="color:#f8fafc; margin:2px 0 0 0; font-size:1.35rem;">
                        AI Consensus Market Scanner &amp; Freqtrade Algorithmic Trading Hub
                    </h3>
                </div>
                <div style="color:#cbd5e1; font-size:0.82rem; text-align:right;">
                    Mode Default: <strong style="color:#4ade80;">DRY-RUN AMAN</strong><br/>
                    Live Gate: <strong style="color:#f87171;">LOCKED (NONAKTIF)</strong>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Sync sidebar and top navigation selector
    if sidebar_selected != default_page:
        selected_page = sidebar_selected
        st.session_state["active_nav_page"] = selected_page
    else:
        top_selected = st.selectbox(
            "🧭 Pindah Cepat Modul (Momentum Radar v5.5 & Freqtrade Quant Suite):",
            NAVIGATION_PAGES,
            index=default_idx,
            key="unified_top_nav_select",
        )
        selected_page = top_selected
        st.session_state["active_nav_page"] = selected_page

    # Always render unified status banner at the top
    conn_summary = render_status_banner(st, services, legacy_context)
    st.divider()

    if selected_page == "🏠 Dashboard Utama":
        render_main_dashboard_page(st, services, legacy_context, conn_summary)
    elif selected_page == "🚨 Market Scanner & Analisis AI":
        render_legacy_scanner_fn()
    elif selected_page == "🧩 Strategy Manager":
        render_strategy_manager_page(st, services, legacy_context)
    elif selected_page == "🗄️ Data Manager":
        render_data_manager_page(st, services)
    elif selected_page == "🧪 Backtesting":
        render_backtesting_page(st, services)
    elif selected_page == "⚙️ Hyperopt":
        render_hyperopt_page(st, services)
    elif selected_page == "🤖 Dry-Run & Live Control":
        render_dryrun_and_live_page(st, services)
    elif selected_page == "📡 Trade Monitor":
        render_trade_monitor_page(st, services, legacy_context)
    elif selected_page == "📈 Performance Analytics":
        render_performance_analytics_page(st, services, legacy_context)
    elif selected_page == "🩺 Bot Logs & System Health":
        render_logs_and_health_page(st, services)
    elif selected_page == "🔧 Settings & Konfigurasi":
        render_settings_page(st, services)

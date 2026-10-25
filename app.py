"""Futures Desk v5.6 — Auto Scan (30/45/60/90s) & Telegram Alert. Run: streamlit run app.py"""
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from futures.models import Account, TIMEFRAMES
from futures.market import FuturesMarket, MarketError
from futures.strategy import evaluate, evaluate_auto, estimate_isolated_liquidation, pnl_scenarios
from futures.indicators import indicators
from futures.groq_rotation import GroqRotation, DEFAULT_MODEL, load_keys, combine
from futures.lifecycle import after_fill, manage_position
from futures.telegram_alert import (
    MODE_LABELS,
    action_side_label,
    dispatch_valid_trade_alert,
    format_telegram_alert,
    load_telegram_credentials,
    send_telegram_message,
    should_send_telegram_alert,
    trading_type_label,
)
from futures import history

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("FUTURES_HISTORY_DB", str(ROOT / "data" / "futures_history.sqlite3")))
WIB = ZoneInfo("Asia/Jakarta")
AUTO_SCAN_INTERVALS = [30, 45, 60, 90]
WATCHLIST_DEFAULTS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
]


def secret(name):
    if name in os.environ:
        return os.environ[name]
    try:
        if name in st.secrets:
            return st.secrets[name]
        for section in ("groq", "telegram", "bot", "api"):
            if section in st.secrets and isinstance(st.secrets[section], dict) and name in st.secrets[section]:
                return st.secrets[section][name]
    except Exception:
        return os.getenv(name, "")
    return os.getenv(name, "")


@st.cache_resource(show_spinner=False)
def key_pool(signature, _keys):
    return GroqRotation(_keys)


@st.cache_resource(show_spinner=False)
def market():
    return FuturesMarket()


def ms(iso):
    dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def stamp(value):
    return datetime.fromtimestamp(value / 1000, WIB).strftime("%d %b %Y, %H:%M:%S WIB")


def number(x):
    return f"{x:,.8f}".rstrip("0").rstrip(".") if x is not None else "Belum tersedia"


def configuration():
    now = datetime.now(timezone.utc)
    with st.sidebar:
        st.markdown("### 🔐 Status Secrets (Otomatis)")
        try:
            keys = load_keys(secret)
        except ValueError as exc:
            st.error(str(exc))
            keys = []
        model = str(secret("GROQ_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        pool = key_pool(hashlib.sha256("|".join(keys).encode()).hexdigest(), tuple(keys))
        tg_token, tg_chat_id = load_telegram_credentials(secret)

        if keys:
            st.success(f"Groq AI: **{len(keys)}/7 key aktif** dari `st.secrets` · `{model}`")
        else:
            st.warning("Groq API key belum terbaca di `st.secrets`.")

        if tg_token and tg_chat_id:
            st.success("Telegram Bot: **Terhubung** dari `st.secrets` (Siap Alert BUY/SELL)")
            if st.button("🔔 Tes Kirim Alert Telegram", use_container_width=True):
                ok, detail = send_telegram_message(
                    tg_token,
                    tg_chat_id,
                    "✅ [TES KONEKSI] Futures Desk v5.6 terhubung dari Streamlit Secrets.\n"
                    "Hanya sinyal lolos BUY (LONG) & SELL (SHORT) yang akan dikirim otomatis dengan label [SCALPER], [INTRADAY], atau [SWING].",
                )
                if ok:
                    st.toast("Pesan tes berhasil dikirim ke Telegram!", icon="✅")
                else:
                    st.error(detail)
        else:
            st.info("Telegram Bot: Isi `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` di `st.secrets` untuk alert otomatis.")

        with st.expander("Status 7 slot rotasi Groq"):
            if keys:
                st.dataframe(pd.DataFrame(pool.status()), hide_index=True, use_container_width=True)
            else:
                st.info("Tambahkan GROQ_API_KEY_1..7 pada Streamlit Secrets.")

        st.divider()
        st.markdown("### ⏱️ Auto Scan & Filter Alert")
        auto_scan = st.toggle("Aktifkan Auto Scan", value=False)
        scan_interval = st.selectbox(
            "Interval Auto Scan (detik)",
            options=AUTO_SCAN_INTERVALS,
            index=2,
            format_func=lambda s: f"{s} detik",
        )
        scan_all_modes = st.checkbox(
            "Scan ketiga jenis trading (SCALPER, INTRADAY, SWING)",
            value=False,
            help="Jika dicentang, setiap siklus Auto Scan akan memeriksa mode SCALPER (1h/5m), INTRADAY (4h/15m), dan SWING (1d/4h).",
        )
        watchlist = st.multiselect(
            "Daftar Pair Auto Scan (opsional)",
            options=WATCHLIST_DEFAULTS,
            default=[],
            help="Kosongkan untuk hanya memindai Pair utama yang dipilih di layar tengah, atau pilih beberapa pair sekaligus.",
        )
        st.caption(
            "Saat Auto Scan berjalan, setiap setup diuji dengan aturan **Master Prompt v1** dan dianalisis **AI Groq**. "
            "Hanya **BUY (LONG)** atau **SELL (SHORT)** valid yang dikirim ke Telegram dengan label **[SCALPER]**, **[INTRADAY]**, atau **[SWING]**. Status **WAIT** / **NO_TRADE** diabaikan."
        )

        st.divider()
        st.markdown("### ⚙️ Pengaturan Akun & Risiko")
        equity = st.number_input("Ekuitas (USDT)", min_value=1.0, value=1000.0, step=100.0)
        leverage = st.select_slider("Leverage", options=[1, 2, 3], value=2, format_func=lambda x: f"{x}x")
        st.caption("Risiko maksimum 0,5% per rencana. Margin isolated.")
        auto_verify_gate = st.checkbox(
            "Verifikasi teknis & likuidasi otomatis (Mode Auto-Scan)",
            value=True,
            help="Menghitung harga likuidasi isolated & buffer ≥2 ATR secara otomatis sesuai Master Prompt v1 tanpa harus mengisi form likuidasi manual.",
        )
        with st.expander("Akun dan batas risiko"):
            free = st.number_input("Margin bebas (USDT)", min_value=0.0, value=float(equity))
            start = st.number_input("Ekuitas awal hari UTC", min_value=1.0, value=float(equity))
            flow = st.number_input("Deposit dikurangi withdrawal hari ini", value=0.0)
            day = st.date_input("Tanggal akun (UTC)", value=now.date())
            count = st.number_input("Jumlah posisi aktif + pending", min_value=0, max_value=100, value=0)
            total = st.number_input("Risiko posisi + pending (USDT)", min_value=0.0, value=0.0)
            correlated = st.number_input("Risiko altcoin searah (USDT)", min_value=0.0, value=0.0)
            losses = st.number_input("Rugi beruntun", min_value=0, max_value=100, value=0)
            risk = st.number_input("Risiko per rencana (%)", min_value=0.05, max_value=0.5, value=0.5, step=0.05)
            isolated = st.checkbox("Margin isolated, auto-add margin mati", value=True)
            verified = st.checkbox("Data akun sudah saya cocokkan dengan bursa", value=True)
        with st.expander("Biaya dan verifikasi pasar"):
            fee = st.number_input("Fee taker akun (%)", min_value=0.0, max_value=2.0, value=0.05, step=0.01, format="%.4f")
            slip = st.number_input("Cadangan slippage exit (bps)", min_value=0.0, value=2.0, step=0.5)
            fund = st.number_input("Cadangan funding total horizon (%)", min_value=0.0, value=0.03, step=0.01, format="%.4f")
            costs = st.checkbox("Fee terverifikasi dan cadangan sudah saya evaluasi", value=True)
            orders = st.checkbox("Fitur order dan trigger sudah saya verifikasi", value=True)
            st.markdown("**Kalender berita (UTC)**")
            source = st.text_input("Sumber kalender CPI/FOMC", value="Kalender Ekonomi Normal (Non-CPI/FOMC)")
            clear_from = st.text_input("Awal jendela tanpa event (UTC)", value=(now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"))
            clear_until = st.text_input("Akhir jendela tanpa event (UTC)", value=(now + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M"))
            checked_at = st.text_input("Waktu pemeriksaan kalender (UTC)", value=now.strftime("%Y-%m-%d %H:%M"))
            news = st.checkbox("Saya sudah memeriksa CPI, FOMC, dan konferensi pers", value=True)

    a = Account(
        equity=equity,
        free_margin=free,
        day_start_equity=start,
        net_cash_flow=flow,
        account_day_utc=day.isoformat(),
        risk_pct=risk,
        leverage=leverage,
        active_pending_count=count,
        total_existing_risk=total,
        correlated_alt_risk=correlated,
        consecutive_losses=losses,
        isolated=isolated,
        account_verified=verified,
        taker_fee=fee / 100,
        exit_slippage_bps=slip,
        funding_reserve_rate=fund / 100,
        costs_verified=costs,
        order_semantics_verified=orders,
    )
    if news:
        try:
            a.news_source, a.news_clear_from_ms, a.news_clear_until_ms = source, ms(clear_from), ms(clear_until)
            a.news_verified_at_ms = ms(checked_at)
        except ValueError:
            st.sidebar.error("Format tanggal kalender tidak valid. Gunakan YYYY-MM-DD HH:MM UTC.")

    scan_cfg = {
        "auto_scan": auto_scan,
        "scan_interval": scan_interval,
        "scan_all_modes": scan_all_modes,
        "watchlist": watchlist,
        "auto_verify": auto_verify_gate,
        "tg_token": tg_token,
        "tg_chat_id": tg_chat_id,
    }
    return a, pool, model, scan_cfg


def account_signature(a, symbol, mode):
    fields = asdict(a)
    return hashlib.sha256(json.dumps([fields, symbol, mode], sort_keys=True).encode()).hexdigest()


def evaluate_single_pair(symbol, mode, account, pool, model, scan_cfg):
    """
    Load Binance Futures data, evaluate Master Prompt v1 rules, run Groq AI 7-key rotation
    when baseline is VALID, and dispatch Telegram alert ONLY when final decision is BUY (LONG) or SELL (SHORT).
    """
    eval_fn = evaluate_auto if scan_cfg.get("auto_verify", True) else evaluate
    snap = market().load(symbol, mode)
    acquired = time.monotonic()
    result = eval_fn(snap, account)
    ai = None
    if result.status == "VALID":
        ai = pool.review(result, model)
        if ai.get("ok"):
            reviewed_id = result.plan["id"]
            snap = market().load(symbol, mode)
            acquired = time.monotonic()
            result = eval_fn(snap, account)
            if not result.plan or result.plan["id"] != reviewed_id:
                ai = {
                    "decision": "WAIT",
                    "reasons": ["Setup atau data berubah setelah respons AI. Analisis ulang diperlukan."],
                    "cautions": [],
                    "ok": False,
                }
    status = combine(result, ai)
    t_label = trading_type_label(mode)
    side_label = action_side_label(result.direction) if result.direction in ("LONG", "SHORT") else "-"

    tg_outcome = dispatch_valid_trade_alert(
        db_path=DB,
        result=result,
        final_status=status,
        ai=ai,
        token=scan_cfg.get("tg_token", ""),
        chat_id=scan_cfg.get("tg_chat_id", ""),
    )

    try:
        history.save(DB, result, status, ai)
    except (OSError, ValueError):
        pass

    return {
        "snapshot": snap,
        "result": result,
        "ai": ai,
        "status": status,
        "trading_type": t_label,
        "action_side": side_label,
        "telegram": tg_outcome,
        "monotonic": acquired,
        "signature": account_signature(account, symbol, mode),
    }


def run_analysis(symbol, mode, account, pool, model, scan_cfg=None):
    scan_cfg = scan_cfg or {"auto_verify": True, "tg_token": "", "tg_chat_id": ""}
    st.session_state.pop("analysis", None)
    with st.spinner(f"Menganalisis {symbol} ({trading_type_label(mode)}) dengan Master Prompt v1 & AI Groq..."):
        try:
            data = evaluate_single_pair(symbol, mode, account, pool, model, scan_cfg)
            st.session_state["analysis"] = data
            if data["status"] == "VALID":
                st.session_state["approved_plan"] = data["result"].plan
        except (MarketError, ValueError, KeyError, TypeError) as exc:
            st.error("WAIT · Data futures belum lengkap. " + pool.redact(str(exc))[:400])


def draw_chart(snap, plan=None):
    # Compute indicators on the tail 220 bars only to prevent Streamlit Cloud CPU throttling
    d = indicators(snap.execution.tail(220)).tail(100)
    x = pd.to_datetime(d.open_time, unit="ms", utc=True).dt.tz_convert(WIB)
    fig = go.Figure(
        go.Candlestick(
            x=x,
            open=d.open,
            high=d.high,
            low=d.low,
            close=d.close,
            name="Futures",
            increasing_line_color="#17826b",
            decreasing_line_color="#d65e62",
        )
    )
    fig.add_trace(go.Scatter(x=x, y=d.ema20, name="EMA 20", line=dict(color="#4b72d6", width=1.3)))
    fig.add_trace(go.Scatter(x=x, y=d.ema50, name="EMA 50", line=dict(color="#c99d4a", width=1.3)))
    if plan:
        for key, label, color in [
            ("entry", "Limit entry", "#4b72d6"),
            ("sl", "SL", "#d65e62"),
            ("tp1", "TP1", "#17826b"),
            ("tp2", "TP2", "#17826b"),
        ]:
            fig.add_hline(y=plan[key], line_dash="dot", line_color=color, annotation_text=label)
    fig.update_layout(
        height=340,
        margin=dict(l=8, r=8, t=10, b=8),
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        legend=dict(orientation="h", y=1.12),
        xaxis_title="WIB · candle tertutup",
        yaxis_title="USDT",
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def show_analysis(data):
    """Render analysis results cleanly without a 5-second polling fragment (prevents CPU throttling)."""
    result, snap, ai = data["result"], data["snapshot"], data["ai"]
    status = data["status"]
    t_label = data.get("trading_type") or trading_type_label(snap.mode)
    side_label = data.get("action_side") or action_side_label(result.direction)
    tg_info = data.get("telegram") or {}

    if status == "VALID":
        st.success(f"SETUP {result.direction} VALID · [{t_label}] {side_label} · MENUNGGU TRIGGER")
        if tg_info.get("sent"):
            st.info(f"📲 Alert Telegram terkirim otomatis dengan label **[{t_label}] {side_label}**.")
        elif tg_info.get("reason"):
            st.caption(f"ℹ️ Status Telegram: {tg_info['reason']}")
    elif status == "NO_TRADE":
        st.error(f"NO TRADE [{t_label}] · Syarat strategi belum terpenuhi (Tidak dikirim ke Telegram)")
    else:
        st.warning(f"WAIT [{t_label}] · Belum ada setup matang (Tidak dikirim ke Telegram)")

    st.caption(
        f"{snap.symbol} · Jenis Trading: **{t_label}** ({snap.mode}) · "
        f"Bias {TIMEFRAMES[snap.mode][0]} / eksekusi {TIMEFRAMES[snap.mode][1]} · {stamp(snap.now_ms)}"
    )
    reasons = result.reasons + ((ai or {}).get("reasons", []))
    if result.status == "VALID" and ai is None:
        reasons.append("Penilaian Groq belum tersedia.")
    if result.status == "VALID" and ai and ai.get("decision") in ("LONG", "SHORT") and ai["decision"] != result.direction:
        reasons.insert(0, "Arah AI tidak sesuai baseline. Tidak ada persetujuan entry.")
    for reason in list(dict.fromkeys(reasons))[:5]:
        st.write("• " + reason)

    cols = st.columns(4)
    for col, label, val in zip(
        cols,
        ["Last futures", "Mark price", "RSI 14", "ADX bias"],
        [snap.last, snap.mark, result.metrics.get("rsi"), result.metrics.get("adx")],
    ):
        col.metric(label, number(val))

    draw_chart(snap, result.plan if status == "VALID" else None)

    if status == "VALID":
        p = result.plan
        st.markdown(f"**Rencana bersyarat · [{t_label}] {side_label}**")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Bagian": "Entry stop-limit", "Harga": number(p["entry"]), "Jumlah": number(p["qty"]), "Ketentuan": f"Trigger Last {number(p['entry_trigger'])}"},
                    {"Bagian": "Stop-loss", "Harga": number(p["sl"]), "Jumlah": number(p["qty"]), "Ketentuan": "Stop-market · Mark · reduce-only sesuai bursa"},
                    {"Bagian": "TP1", "Harga": number(p["tp1"]), "Jumlah": number(p["tp1_qty"]), "Ketentuan": f"{p['tp1_qty']/p['qty']:.1%} dari awal"},
                    {"Bagian": "TP2", "Harga": number(p["tp2"]), "Jumlah": number(p["tp2_qty"]), "Ketentuan": f"{p['tp2_qty']/p['qty']:.1%} dari awal"},
                    {"Bagian": "Runner", "Harga": "Trailing", "Jumlah": number(p["runner_qty"]), "Ketentuan": "2 ATR setelah TP2; hanya diperketat"},
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(f"Berlaku sampai {stamp(p['expires_ms'])}. Partial fill harus langsung mendapat SL sesuai jumlah terisi. Stop-limit tidak menjamin fill.")
        with st.expander("📲 Format Pesan Alert Telegram"):
            st.code(format_telegram_alert(result, ai), language="text")
    elif result.candidate:
        with st.expander("Draf untuk pemeriksaan likuidasi · belum siap entry"):
            p = result.candidate
            st.write(f"[{t_label}] {p['direction']} · Q {number(p['qty'])} · E {number(p['entry'])} · SL {number(p['sl'])} · leverage {p['leverage']}x")
            st.code(p["id"], language=None)
    with st.expander("Kriteria dan sumber data"):
        st.dataframe(pd.DataFrame(result.checks), hide_index=True, use_container_width=True)
        st.caption(snap.source + " · seluruh candle indikator sudah ditutup · VWAP reset 00.00 UTC")
    if result.candidate:
        with st.expander("Risiko dan skenario PnL"):
            p = result.candidate
            labels = {
                "equity": "Ekuitas",
                "risk_budget": "B (batas risiko)",
                "entry": "E (fill terburuk)",
                "atr": "ATR",
                "distance": "D (1R)",
                "cost_sl_per_unit": "Biaya SL per unit",
                "qty": "Q",
                "notional": "Notional",
                "leverage": "Leverage",
                "margin": "Margin perkiraan",
                "risk": "Risiko termasuk biaya",
                "liquidation": "Harga likuidasi isolated",
                "liquidation_buffer": "Buffer likuidasi",
                "net_rr_tp1": "RR bersih TP1 (seluruh posisi)",
            }
            st.dataframe(
                pd.DataFrame([{"Komponen": label, "Nilai": number(p.get(key))} for key, label in labels.items()]),
                hide_index=True,
                use_container_width=True,
            )
            st.dataframe(pd.DataFrame(pnl_scenarios(p)).round(4), hide_index=True, use_container_width=True)
    with st.expander("Ulasan Groq"):
        if ai:
            st.write("Keputusan: " + ai["decision"])
            for line in ai.get("reasons", []) + ai.get("cautions", []):
                st.write("• " + line)
            if ai.get("slot"):
                st.caption(f"Slot {ai['slot']} · {ai['model']}")
        else:
            st.caption("Groq dipanggil otomatis saat seluruh aturan Master Prompt v1 lolos (status VALID).")
    report = {"baseline": asdict(result), "ai": ai, "status": status, "trading_type": t_label}
    st.download_button(
        "Unduh analisis JSON",
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        file_name=f"{snap.symbol}_{snap.mode}_analysis.json",
        mime="application/json",
    )


def position_calculator():
    p = st.session_state.get("approved_plan")
    st.caption("Kalkulator posisi berdasarkan fill yang Anda konfirmasi. Tidak menjalankan order atau pemantauan otomatis.")
    if p is None:
        st.info("Rencana yang lolos aturan dan Groq akan tersedia di sini untuk evaluasi fill.")
        return
    st.write(f"Rencana {p['symbol']} [{trading_type_label(p['mode'])}] {p['direction']} · ID {p['id']}")
    with st.form("position_check"):
        a, b = st.columns(2)
        fill = a.number_input("Harga rata-rata fill aktual", min_value=0.00000001, value=float(p["entry"]), format="%.8f")
        qty = b.number_input("Total kuantitas terisi", min_value=0.00000001, max_value=float(p["qty"]), value=float(p["qty"]), format="%.8f")
        fill_at = st.text_input("Waktu fill UTC", value=datetime.fromtimestamp((p["signal_close_ms"] + 1000) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        tp1 = st.text_input("Waktu TP1 selesai terisi UTC (kosong jika belum)")
        tp2 = st.text_input("Waktu TP2 selesai terisi UTC (kosong jika belum)")
        tp1_qty = st.number_input("Kuantitas TP1 sudah terisi, termasuk partial", min_value=0.0, value=0.0, format="%.8f")
        tp2_qty = st.number_input("Kuantitas TP2 sudah terisi, termasuk partial", min_value=0.0, value=0.0, format="%.8f")
        entry_fee = st.number_input("Alokasi fee entry aktual per unit", min_value=0.0, value=float(p["entry"] * p["fee"]), format="%.8f")
        stop = st.number_input("SL terakhir", min_value=0.00000001, value=float(p["sl"]), format="%.8f")
        funded = st.number_input("Funding dibayar yang dialokasikan per unit sisa", min_value=0.0, value=0.0, format="%.8f")
        reached = st.checkbox("Saya mengonfirmasi harga pernah mencapai +1R setelah fill")
        confirmed = st.checkbox("Saya mengonfirmasi data fill dan TP benar")
        submit = st.form_submit_button("Hitung ulang proteksi", use_container_width=True)
    if submit and confirmed:
        try:
            filled = after_fill(p, fill, qty, ms(fill_at))
            fresh = market().load(p["symbol"], p["mode"])
            if not 0 <= fresh.now_ms - fresh.quote_times["mark"] <= 30000:
                raise MarketError("Mark price kedaluwarsa. Perbarui data sebelum evaluasi posisi.")
            advice = manage_position(
                filled,
                fresh.execution,
                fresh.now_ms,
                fresh.mark,
                stop,
                ms(tp1) if tp1.strip() else None,
                ms(tp2) if tp2.strip() else None,
                reached,
                funded,
                entry_fee,
                tp1_qty,
                tp2_qty,
            )
            st.write(f"**{advice['action']}** · {advice['reason']}")
            st.write(f"SL: {number(advice['stop'])} · Q sisa: {number(advice['remaining_qty'])}")
            st.write(f"TP1 baru: {number(filled['tp1'])} · TP2 baru: {number(filled['tp2'])} · Risiko: {number(filled['risk'])} USDT")
            st.caption(f"Sisa entry belum fill: {number(filled['remaining_entry_qty'])}; proteksi SL untuk {number(filled['protection_qty'])} unit terisi.")
            if advice["cancel_unfilled"]:
                st.warning("Batalkan sisa entry yang belum terisi.")
            if not filled["partial_tp_ready"]:
                st.warning("Partial fill terlalu kecil untuk pembagian TP. Lindungi jumlah terisi dan periksa batas order bursa.")
            st.caption(advice["notice"])
        except (ValueError, MarketError) as exc:
            st.error(str(exc))
    elif submit:
        st.warning("Konfirmasi data fill terlebih dahulu.")


def main():
    st.set_page_config(page_title="Futures Desk · v5.6 Auto-Scan", page_icon="📈", layout="wide")
    st.markdown(
        """<style>
    .block-container {max-width:1140px;padding-top:2.1rem;padding-bottom:2rem}
    [data-testid="stMetric"] {border:1px solid #e3e8ef;border-radius:12px;padding:14px}
    [data-testid="stMetricValue"] {font-size:1.45rem}
    h1 {font-size:2rem!important;letter-spacing:-.7px}
    .eyebrow {color:#4b72d6;font-size:.75rem;letter-spacing:.12em;font-weight:650}
    </style>""",
        unsafe_allow_html=True,
    )
    st.session_state.setdefault("scan_table", [])
    st.session_state.setdefault("scan_cycles", 0)
    st.session_state.setdefault("last_scan_wib", "-")

    account, pool, model, scan_cfg = configuration()
    st.markdown('<div class="eyebrow">FUTURES DESK / 5.6 · AUTO-SCAN & TELEGRAM ALERT</div>', unsafe_allow_html=True)
    st.title("Rencana trading otomatis, disiplin Master Prompt.")
    st.caption(
        "Aturan terukur Master Prompt v1 · Data eksklusif Binance USD-M Futures · "
        "Rotasi 7 API Groq & Bot Telegram otomatis dari Streamlit Secrets · "
        "Auto Scan 30 / 45 / 60 / 90 detik · Alert Telegram khusus BUY/SELL dengan label SCALPER / INTRADAY / SWING."
    )

    analyse_tab, manage_tab, history_tab = st.tabs(["Analisis", "Evaluasi fill", "Riwayat"])
    with analyse_tab:
        c1, c2, c3 = st.columns([2, 2, 1])
        symbol = c1.text_input("Pair USD-M perpetual", value="BTCUSDT", placeholder="BTCUSDT").strip().upper()
        mode = c2.selectbox(
            "Mode (Jenis Trading)",
            list(TIMEFRAMES),
            index=1,
            format_func=lambda m: f"{MODE_LABELS[m]} ({m} · {TIMEFRAMES[m][0]}/{TIMEFRAMES[m][1]})",
        )
        c3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        run = c3.button("Analisis", type="primary", use_container_width=True)

        previous = st.session_state.get("analysis")
        if (
            not scan_cfg["auto_verify"]
            and previous
            and previous["result"].candidate
            and previous["snapshot"].symbol == symbol
            and previous["snapshot"].mode == mode
        ):
            candidate = previous["result"].candidate
            with st.expander("Verifikasi likuidasi manual untuk draf saat ini"):
                st.caption(f"ID {candidate['id']} · {candidate['direction']} · Q {number(candidate['qty'])} · E {number(candidate['entry'])} · {candidate['leverage']}x")
                liq = st.number_input("Harga likuidasi dari bursa/kalkulator", min_value=0.0, value=0.0, format="%.8f", key="liq_" + candidate["id"])
                origin = st.text_input("Sumber dan risk tier", key="liq_source_" + candidate["id"])
                liq_checked = st.text_input("Waktu verifikasi UTC", value=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), key="liq_time_" + candidate["id"])
                match = st.checkbox("Nilai sesuai Q, E, leverage, isolated, dan risk tier draf ini", key="liq_match_" + candidate["id"])
                if match:
                    try:
                        account.liquidation_price, account.liquidation_plan_id = liq, candidate["id"]
                        account.liquidation_source, account.liquidation_checked_ms = origin, ms(liq_checked)
                    except ValueError:
                        st.error("Format waktu verifikasi tidak valid")

        if run:
            run_analysis(symbol, mode, account, pool, model, scan_cfg)

        interval_sec = int(scan_cfg["scan_interval"])
        auto_enabled = bool(scan_cfg["auto_scan"])

        @st.fragment(run_every=f"{interval_sec}s" if auto_enabled else None)
        def auto_scanner_panel():
            if auto_enabled:
                pairs = list(dict.fromkeys((scan_cfg["watchlist"] or []) + [symbol]))
                modes = list(TIMEFRAMES) if scan_cfg["scan_all_modes"] else [mode]
                new_rows = []
                for sym in pairs:
                    for m in modes:
                        try:
                            out = evaluate_single_pair(sym, m, account, pool, model, scan_cfg)
                            st.session_state["analysis"] = out
                            if out["status"] == "VALID":
                                st.session_state["approved_plan"] = out["result"].plan
                            p = out["result"].plan or out["result"].candidate or {}
                            tg_res = out["telegram"]
                            if tg_res.get("sent"):
                                tg_col = f"✅ Terkirim [{out['trading_type']}] {out['action_side']}"
                            elif out["status"] in ("WAIT", "NO_TRADE"):
                                tg_col = f"⏭️ Diabaikan ({out['status']})"
                            else:
                                tg_col = tg_res.get("reason") or tg_res.get("detail") or "-"
                            new_rows.append(
                                {
                                    "Waktu WIB": stamp(out["snapshot"].now_ms),
                                    "Pair": sym,
                                    "Jenis Trading": out["trading_type"],
                                    "Keputusan": out["status"],
                                    "Aksi": out["action_side"] if out["status"] == "VALID" else "-",
                                    "Last (USDT)": number(out["snapshot"].last),
                                    "Entry (E)": number(p.get("entry")),
                                    "SL": number(p.get("sl")),
                                    "TP1 (2R)": number(p.get("tp1")),
                                    "Alert Telegram": tg_col,
                                }
                            )
                        except Exception as exc:
                            new_rows.append(
                                {
                                    "Waktu WIB": datetime.now(WIB).strftime("%d %b %Y, %H:%M:%S WIB"),
                                    "Pair": sym,
                                    "Jenis Trading": trading_type_label(m),
                                    "Keputusan": "WAIT",
                                    "Aksi": "-",
                                    "Last (USDT)": "-",
                                    "Entry (E)": "-",
                                    "SL": "-",
                                    "TP1 (2R)": "-",
                                    "Alert Telegram": f"⏭️ Diabaikan (WAIT: {str(exc)[:60]})",
                                }
                            )
                st.session_state["scan_cycles"] += 1
                st.session_state["last_scan_wib"] = datetime.now(WIB).strftime("%d %b %Y, %H:%M:%S WIB")
                st.session_state["scan_table"] = new_rows + st.session_state["scan_table"][:35]

            if auto_enabled or st.session_state["scan_table"]:
                st.markdown(
                    f"**Status Auto Scan:** {'🟢 Aktif setiap ' + str(interval_sec) + ' detik' if auto_enabled else '⚪ Nonaktif'} · "
                    f"Siklus: `{st.session_state['scan_cycles']}` · Terakhir: `{st.session_state['last_scan_wib']}`"
                )
                if st.session_state["scan_table"]:
                    st.dataframe(pd.DataFrame(st.session_state["scan_table"][:20]), hide_index=True, use_container_width=True)

            data = st.session_state.get("analysis")
            if data and data["signature"] == account_signature(account, data["snapshot"].symbol, data["snapshot"].mode):
                show_analysis(data)
            elif data:
                st.info("Pengaturan berubah. Tekan Analisis untuk mendapatkan hasil yang sesuai.")
            else:
                st.info("Pilih pair dan mode, lalu tekan Analisis atau aktifkan Auto Scan (30, 45, 60, 90 detik) di sidebar.")
                st.markdown("**Satu status yang jelas:** LONG (BUY), SHORT (SELL), WAIT, atau NO TRADE.")

        auto_scanner_panel()

    with manage_tab:
        position_calculator()

    with history_tab:
        col_h1, col_h2 = st.columns(2)
        with col_h1:
            st.markdown("**Riwayat Analisis & Scan**")
            try:
                records = history.load(DB)
                if records:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Waktu WIB": stamp(x["baseline"]["meta"]["time_ms"]),
                                    "Pair": x["baseline"]["meta"]["symbol"],
                                    "Jenis Trading": trading_type_label(x["baseline"]["meta"]["mode"]),
                                    "Status": x["final_status"],
                                    "Aksi": action_side_label(x["baseline"]["direction"]) if x["final_status"] == "VALID" else (x["baseline"]["direction"] or "-"),
                                }
                                for x in records
                            ]
                        ),
                        hide_index=True,
                        use_container_width=True,
                    )
                else:
                    st.info("Belum ada analisis tersimpan.")
            except Exception:
                st.warning("Riwayat belum dapat dibuka.")
        with col_h2:
            st.markdown("**Riwayat Alert BUY/SELL Terkirim ke Telegram**")
            try:
                sent_rows = history.load_sent_alerts(DB)
                if sent_rows:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Waktu WIB": stamp(r["at_ms"]),
                                    "Pair": r["symbol"],
                                    "Jenis Trading": trading_type_label(r["mode"]),
                                    "Aksi": action_side_label(r["direction"]),
                                    "ID Sinyal": r["alert_id"],
                                }
                                for r in sent_rows
                            ]
                        ),
                        hide_index=True,
                        use_container_width=True,
                    )
                else:
                    st.info("Belum ada alert BUY/SELL yang dikirim ke Telegram.")
            except Exception:
                st.info("Belum ada riwayat alert Telegram.")


if __name__ == "__main__":
    main()

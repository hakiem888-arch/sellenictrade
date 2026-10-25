"""Futures Desk v5.6. Run: streamlit run app.py"""
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
from futures.strategy import evaluate, pnl_scenarios
from futures.indicators import indicators
from futures.groq_rotation import GroqRotation, DEFAULT_MODEL, load_keys, combine
from futures.lifecycle import after_fill, manage_position
from futures import history

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("FUTURES_HISTORY_DB", str(ROOT / "data" / "futures_history.sqlite3")))
WIB = ZoneInfo("Asia/Jakarta")


def secret(name):
    try:
        return st.secrets.get(name, os.getenv(name, ""))
    except Exception:
        return os.getenv(name, "")


@st.cache_resource(show_spinner=False)
def key_pool(signature, _keys):
    return GroqRotation(_keys)


@st.cache_resource(show_spinner=False)
def market():
    return FuturesMarket()


def release_session_keys():
    st.session_state.pop("session_keys", None)
    for i in range(1, 8):
        st.session_state.pop(f"groq_slot_{i}", None)
    key_pool.clear()


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
        st.markdown("### Pengaturan")
        equity = st.number_input("Ekuitas (USDT)", min_value=1.0, value=1000.0, step=100.0)
        leverage = st.select_slider("Leverage", options=[1, 2, 3], value=2, format_func=lambda x: f"{x}x")
        st.caption("Risiko maksimum 0,5% per rencana. Margin isolated.")
        with st.expander("Akun dan batas risiko"):
            free = st.number_input("Margin bebas (USDT)", min_value=0.0, value=1000.0)
            start = st.number_input("Ekuitas awal hari UTC", min_value=1.0, value=1000.0)
            flow = st.number_input("Deposit dikurangi withdrawal hari ini", value=0.0)
            day = st.date_input("Tanggal akun (UTC)", value=now.date())
            count = st.number_input("Jumlah posisi aktif + pending", min_value=0, max_value=100, value=0)
            total = st.number_input("Risiko posisi + pending (USDT)", min_value=0.0, value=0.0)
            correlated = st.number_input("Risiko altcoin searah (USDT)", min_value=0.0, value=0.0)
            losses = st.number_input("Rugi beruntun", min_value=0, max_value=100, value=0)
            risk = st.number_input("Risiko per rencana (%)", min_value=0.05, max_value=0.5, value=0.5, step=0.05)
            isolated = st.checkbox("Margin isolated, auto-add margin mati", value=False)
            verified = st.checkbox("Data akun sudah saya cocokkan dengan bursa", value=False,
                                   help="Termasuk unrealized PnL, fee, funding, seluruh posisi/pending, dan kelompok altcoin searah. Data ini manual.")
        with st.expander("Biaya dan verifikasi pasar"):
            fee = st.number_input("Fee taker akun (%)", min_value=0.0, max_value=2.0, value=0.05, step=0.01, format="%.4f")
            slip = st.number_input("Cadangan slippage exit (bps)", min_value=0.0, value=2.0, step=0.5)
            fund = st.number_input("Cadangan funding total horizon (%)", min_value=0.0, value=0.03, step=0.01, format="%.4f")
            costs = st.checkbox("Fee terverifikasi dan cadangan sudah saya evaluasi", value=False)
            st.caption("Nilai awal hanya contoh input. Cadangan funding mencakup durasi posisi dan interval kontrak, termasuk SWING.")
            orders = st.checkbox("Fitur order dan trigger sudah saya verifikasi", value=False,
                                 help="Stop-limit entry dengan Last; stop-market SL dengan Mark; reduce-only/close-position; proteksi partial fill. Tidak ada order yang dikirim aplikasi.")
            st.markdown("**Kalender berita (UTC)**")
            source = st.text_input("Sumber kalender CPI/FOMC", placeholder="URL kalender resmi yang Anda periksa")
            clear_from = st.text_input("Awal jendela tanpa event (UTC)", value=(now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"))
            clear_until = st.text_input("Akhir jendela tanpa event (UTC)", value=(now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
            checked_at = st.text_input("Waktu pemeriksaan kalender (UTC)", value=now.strftime("%Y-%m-%d %H:%M"))
            news = st.checkbox("Saya sudah memeriksa CPI, FOMC, dan konferensi pers", value=False)
            st.caption("Filter berita membutuhkan jarak 15 menit sebelum dan sesudah entry. Kalender tidak diambil otomatis.")
        with st.expander("Groq · sampai 7 API key"):
            st.caption("Isi melalui Secrets atau kolom tersamarkan ini. Key dari UI hanya berada di memori server, tidak ditulis ke file.")
            with st.form("groq_keys"):
                entered = [st.text_input(f"Key {i}", type="password", key=f"groq_slot_{i}") for i in range(1, 8)]
                applied = st.form_submit_button("Gunakan key ini", use_container_width=True)
            if applied:
                st.session_state["session_keys"] = tuple(dict.fromkeys(x.strip() for x in entered if x.strip()))
            model = st.text_input("Model Groq", value=str(secret("GROQ_MODEL") or DEFAULT_MODEL))
            st.caption("Rotasi 1→2→…→7. HTTP 429 menjeda seluruh pool karena kuota bisa dibagi satu organisasi.")
            st.button("Lepas key sesi", use_container_width=True, on_click=release_session_keys)
        try:
            keys = list(st.session_state.get("session_keys", ())) or load_keys(secret)
        except ValueError as exc:
            st.error(str(exc))
            keys = []
        pool = key_pool(hashlib.sha256("|".join(keys).encode()).hexdigest(), tuple(keys))
        st.caption(f"Groq: {len(keys)}/7 key dikonfigurasi · {model}")
        with st.expander("Status koneksi AI"):
            if keys:
                st.dataframe(pd.DataFrame(pool.status()), hide_index=True, use_container_width=True)
            else:
                st.info("Isi key untuk penilaian AI.")
        st.divider()
        st.caption("Riset dan perencanaan. Tidak mengirim order. Belum ada klaim hasil backtest.")
    a = Account(equity=equity, free_margin=free, day_start_equity=start, net_cash_flow=flow,
                account_day_utc=day.isoformat(), risk_pct=risk, leverage=leverage, active_pending_count=count,
                total_existing_risk=total, correlated_alt_risk=correlated, consecutive_losses=losses,
                isolated=isolated, account_verified=verified, taker_fee=fee / 100,
                exit_slippage_bps=slip, funding_reserve_rate=fund / 100, costs_verified=costs,
                order_semantics_verified=orders)
    if news:
        try:
            a.news_source, a.news_clear_from_ms, a.news_clear_until_ms = source, ms(clear_from), ms(clear_until)
            a.news_verified_at_ms = ms(checked_at)
        except ValueError:
            st.sidebar.error("Format tanggal kalender tidak valid. Gunakan YYYY-MM-DD HH:MM UTC.")
    return a, pool, model


def account_signature(a, symbol, mode):
    fields = asdict(a)
    return hashlib.sha256(json.dumps([fields, symbol, mode], sort_keys=True).encode()).hexdigest()


def run_analysis(symbol, mode, account, pool, model):
    st.session_state.pop("analysis", None)
    with st.spinner("Mengambil candle futures dan memeriksa aturan..."):
        try:
            snap = market().load(symbol, mode)
            acquired = time.monotonic()
            result = evaluate(snap, account)
            ai = None
            if result.status == "VALID":
                ai = pool.review(result, model)
                # AI latency cannot turn an expired/triggered old snapshot into an entry.
                if ai.get("ok"):
                    reviewed_id = result.plan["id"]
                    snap = market().load(symbol, mode)
                    acquired = time.monotonic()
                    result = evaluate(snap, account)
                    if not result.plan or result.plan["id"] != reviewed_id:
                        ai = {"decision": "WAIT", "reasons": ["Setup atau data berubah setelah respons AI. Analisis ulang diperlukan."], "cautions": [], "ok": False}
            status = combine(result, ai)
            data = {"snapshot": snap, "result": result, "ai": ai, "status": status,
                    "monotonic": acquired, "signature": account_signature(account, symbol, mode)}
            st.session_state["analysis"] = data
            if status == "VALID":
                st.session_state["approved_plan"] = result.plan
            try:
                history.save(DB, result, status, ai)
            except (OSError, ValueError) as exc:
                st.warning("Analisis selesai, tetapi riwayat belum tersimpan.")
        except (MarketError, ValueError, KeyError, TypeError) as exc:
            st.error("WAIT · Data futures belum lengkap. " + pool.redact(str(exc))[:400])


def draw_chart(snap, plan=None):
    d = indicators(snap.execution).tail(100)
    x = pd.to_datetime(d.open_time, unit="ms", utc=True).dt.tz_convert(WIB)
    fig = go.Figure(go.Candlestick(x=x, open=d.open, high=d.high, low=d.low, close=d.close,
                                 name="Futures", increasing_line_color="#17826b", decreasing_line_color="#d65e62"))
    fig.add_trace(go.Scatter(x=x, y=d.ema20, name="EMA 20", line=dict(color="#4b72d6", width=1.3)))
    fig.add_trace(go.Scatter(x=x, y=d.ema50, name="EMA 50", line=dict(color="#c99d4a", width=1.3)))
    if plan:
        for key, label, color in [("entry", "Limit entry", "#4b72d6"), ("sl", "SL", "#d65e62"), ("tp1", "TP1", "#17826b"), ("tp2", "TP2", "#17826b")]:
            fig.add_hline(y=plan[key], line_dash="dot", line_color=color, annotation_text=label)
    fig.update_layout(height=340, margin=dict(l=8, r=8, t=10, b=8), xaxis_rangeslider_visible=False,
                      template="plotly_white", legend=dict(orientation="h", y=1.12), xaxis_title="WIB · candle tertutup", yaxis_title="USDT")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


@st.fragment(run_every="5s")
def show_analysis(data):
    result, snap, ai = data["result"], data["snapshot"], data["ai"]
    elapsed_ms = (time.monotonic() - data["monotonic"]) * 1000
    now = snap.now_ms + elapsed_ms
    stale = any(now - value > 30000 for value in snap.quote_times.values())
    status = data["status"]
    if stale and status == "VALID":
        status = "WAIT"
    if result.plan and now >= result.plan["expires_ms"]:
        status = "WAIT"
    if status == "VALID":
        st.success(f"SETUP {result.direction} VALID · MENUNGGU TRIGGER")
    elif status == "NO_TRADE":
        st.error("NO TRADE · Syarat strategi belum terpenuhi")
    else:
        st.warning("WAIT · Perbarui snapshot" if stale else "WAIT · Lengkapi verifikasi yang diperlukan")
    st.caption(f"{snap.symbol} · {snap.mode} · Bias {TIMEFRAMES[snap.mode][0]} / eksekusi {TIMEFRAMES[snap.mode][1]} · {stamp(snap.now_ms)}")
    reasons = result.reasons + ((ai or {}).get("reasons", []))
    if result.status == "VALID" and ai is None:
        reasons.append("Penilaian Groq belum tersedia.")
    if stale:
        reasons.insert(0, "Snapshot historis. Tekan Analisis untuk memeriksa harga dan trigger terbaru.")
    if result.status == "VALID" and ai and ai.get("decision") in ("LONG", "SHORT") and ai["decision"] != result.direction:
        reasons.insert(0, "Arah AI tidak sesuai baseline. Tidak ada persetujuan entry.")
    for reason in list(dict.fromkeys(reasons))[:5]:
        st.write("• " + reason)
    cols = st.columns(4)
    for col, label, val in zip(cols, ["Last futures", "Mark price", "RSI 14", "ADX bias"],
                               [snap.last, snap.mark, result.metrics.get("rsi"), result.metrics.get("adx")]):
        col.metric(label, number(val))
    draw_chart(snap, result.plan if status == "VALID" else None)
    if status == "VALID":
        p = result.plan
        st.markdown("**Rencana bersyarat**")
        st.dataframe(pd.DataFrame([
            {"Bagian": "Entry stop-limit", "Harga": number(p["entry"]), "Jumlah": number(p["qty"]), "Ketentuan": f"Trigger Last {number(p['entry_trigger'])}"},
            {"Bagian": "Stop-loss", "Harga": number(p["sl"]), "Jumlah": number(p["qty"]), "Ketentuan": "Stop-market · Mark · reduce-only sesuai bursa"},
            {"Bagian": "TP1", "Harga": number(p["tp1"]), "Jumlah": number(p["tp1_qty"]), "Ketentuan": f"{p['tp1_qty']/p['qty']:.1%} dari awal"},
            {"Bagian": "TP2", "Harga": number(p["tp2"]), "Jumlah": number(p["tp2_qty"]), "Ketentuan": f"{p['tp2_qty']/p['qty']:.1%} dari awal"},
            {"Bagian": "Runner", "Harga": "Trailing", "Jumlah": number(p["runner_qty"]), "Ketentuan": "2 ATR setelah TP2; hanya diperketat"}
        ]), hide_index=True, use_container_width=True)
        st.caption(f"Berlaku sampai {stamp(p['expires_ms'])}. Partial fill harus langsung mendapat SL sesuai jumlah terisi. Stop-limit tidak menjamin fill.")
        st.write("Batalkan jika: " + "; ".join(p["cancellation"]) + ".")
    elif result.candidate:
        with st.expander("Draf untuk pemeriksaan likuidasi · belum siap entry"):
            p = result.candidate
            st.write(f"{p['direction']} · Q {number(p['qty'])} · E {number(p['entry'])} · SL {number(p['sl'])} · leverage {p['leverage']}x")
            st.code(p["id"], language=None)
            st.caption("Gunakan Q, E, margin isolated, leverage, dan risk tier yang sama pada kalkulator bursa. Ini hanya draf verifikasi.")
    with st.expander("Kriteria dan sumber data"):
        st.dataframe(pd.DataFrame(result.checks), hide_index=True, use_container_width=True)
        st.caption(snap.source + " · seluruh candle indikator sudah ditutup · VWAP reset 00.00 UTC")
        for warning in result.meta.get("limitations", []):
            st.caption(warning)
    if result.candidate:
        with st.expander("Risiko dan skenario PnL"):
            p = result.candidate
            st.caption("Estimasi bersyarat; cadangan biaya dapat berbeda dari fill aktual. Bukan hasil backtest.")
            labels = {"equity": "Ekuitas", "risk_budget": "B (batas risiko)", "entry": "E (fill terburuk)", "atr": "ATR", "distance": "D (1R)",
                      "cost_sl_per_unit": "Biaya SL per unit", "qty": "Q", "notional": "Notional", "leverage": "Leverage",
                      "margin": "Margin perkiraan", "risk": "Risiko termasuk biaya", "liquidation": "Likuidasi dari verifikasi manual",
                      "liquidation_buffer": "Buffer likuidasi", "net_rr_tp1": "RR bersih TP1 (seluruh posisi)"}
            st.dataframe(pd.DataFrame([{"Komponen": label, "Nilai": number(p.get(key))} for key, label in labels.items()]), hide_index=True, use_container_width=True)
            st.dataframe(pd.DataFrame(pnl_scenarios(p)).round(4), hide_index=True, use_container_width=True)
            st.caption(f"Jarak harga SL: {p['distance']/p['entry']:.2%}; TP1: {abs(p['tp1']-p['entry'])/p['entry']:.2%}. PnL tidak dikalikan leverage lagi. Skenario runner menggunakan harga TP1, bukan target tetap.")
            st.caption("BE biaya: LONG = (E + alokasi fee entry + funding/unit)/(1 − fee exit) + slippage; SHORT memakai pengurangan biaya dan pembagi (1 + fee exit). Setelah fill, hitung ulang dengan biaya aktual.")
    with st.expander("Ulasan Groq"):
        if ai:
            st.write("Keputusan: " + ai["decision"])
            for line in ai.get("reasons", []) + ai.get("cautions", []):
                st.write("• " + line)
            if ai.get("slot"):
                st.caption(f"Slot {ai['slot']} · {ai['model']}")
        else:
            st.caption("Groq dipanggil setelah seluruh aturan dan data wajib lolos.")
    report = {"baseline": asdict(result), "ai": ai, "status": status, "historical_snapshot": stale}
    st.download_button("Unduh analisis JSON", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                       file_name=f"{snap.symbol}_{snap.mode}_analysis.json", mime="application/json")


def position_calculator():
    p = st.session_state.get("approved_plan")
    st.caption("Kalkulator posisi berdasarkan fill yang Anda konfirmasi. Tidak menjalankan order atau pemantauan otomatis.")
    if p is None:
        st.info("Rencana yang lolos aturan dan Groq akan tersedia di sini untuk evaluasi fill.")
        return
    st.write(f"Rencana {p['symbol']} {p['direction']} · ID {p['id']}")
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
            advice = manage_position(filled, fresh.execution, fresh.now_ms, fresh.mark, stop,
                                     ms(tp1) if tp1.strip() else None, ms(tp2) if tp2.strip() else None,
                                     reached, funded, entry_fee, tp1_qty, tp2_qty)
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
    st.set_page_config(page_title="Futures Desk · v5.6", page_icon="📈", layout="wide")
    st.markdown("""<style>
    .block-container {max-width:1140px;padding-top:2.1rem;padding-bottom:2rem}
    [data-testid="stMetric"] {border:1px solid #e3e8ef;border-radius:12px;padding:14px}
    [data-testid="stMetricValue"] {font-size:1.45rem}
    h1 {font-size:2rem!important;letter-spacing:-.7px}
    .eyebrow {color:#4b72d6;font-size:.75rem;letter-spacing:.12em;font-weight:650}
    </style>""", unsafe_allow_html=True)
    account, pool, model = configuration()
    st.markdown('<div class="eyebrow">FUTURES DESK / 5.6</div>', unsafe_allow_html=True)
    st.title("Rencana trading, lebih jelas.")
    st.caption("Aturan terukur · data futures · ulasan Groq")
    analyse_tab, manage_tab, history_tab = st.tabs(["Analisis", "Evaluasi fill", "Riwayat"])
    with analyse_tab:
        c1, c2, c3 = st.columns([2, 2, 1])
        symbol = c1.text_input("Pair USD-M perpetual", value="BTCUSDT", placeholder="BTCUSDT").strip().upper()
        mode = c2.selectbox("Mode", list(TIMEFRAMES), index=1)
        c3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        run = c3.button("Analisis", type="primary", use_container_width=True)
        previous = st.session_state.get("analysis")
        if previous and previous["result"].candidate and previous["snapshot"].symbol == symbol and previous["snapshot"].mode == mode:
            candidate = previous["result"].candidate
            with st.expander("Verifikasi likuidasi untuk draf saat ini"):
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
                st.caption("Tekan Analisis kembali setelah diisi. Perubahan rencana akan membatalkan verifikasi lama.")
        if run:
            run_analysis(symbol, mode, account, pool, model)
            if st.session_state.get("analysis"):
                st.rerun()
        data = st.session_state.get("analysis")
        if data and data["signature"] == account_signature(account, symbol, mode):
            show_analysis(data)
        elif data:
            st.info("Pengaturan berubah. Tekan Analisis untuk mendapatkan hasil yang sesuai.")
        else:
            st.info("Pilih pair dan mode, lalu tekan Analisis.")
            st.markdown("**Satu status yang jelas:** LONG, SHORT, WAIT, atau NO TRADE.")
            st.write("Buka pengaturan akun untuk melengkapi biaya, risiko, dan sumber verifikasi. Data publik tetap bisa dianalisis sebelum seluruh input lengkap.")
        st.caption("Strategi baseline belum terbukti sampai diuji pada data kronologis dan forward test. Grafik dan skenario bukan backtest.")
    with manage_tab:
        position_calculator()
    with history_tab:
        try:
            records = history.load(DB)
            if records:
                st.dataframe(pd.DataFrame([{"Waktu WIB": stamp(x["baseline"]["meta"]["time_ms"]),
                                            "Pair": x["baseline"]["meta"]["symbol"], "Mode": x["baseline"]["meta"]["mode"],
                                            "Status saat analisis": x["final_status"], "Bias": x["baseline"]["direction"] or "-"}
                                           for x in records]), hide_index=True, use_container_width=True)
                st.caption("Riwayat snapshot, bukan sinyal yang masih berlaku atau laporan win rate.")
            else:
                st.info("Belum ada analisis tersimpan.")
        except Exception:
            st.warning("Riwayat belum dapat dibuka.")


if __name__ == "__main__":
    main()

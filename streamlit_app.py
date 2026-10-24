"""Compact single-page scanner. Run: python -m streamlit run streamlit_app.py"""
import os
import time
import pandas as pd
import streamlit as st
from alerts import Telegram, message, number, wib
from market import BinanceFutures, MarketError
from strategy import MODES
from risk import Account, verify

st.set_page_config(page_title="Momentum Scanner", page_icon="📡", layout="wide")
st.markdown("""<style>
.block-container{max-width:1220px;padding-top:2.2rem;padding-bottom:2rem}
h1{letter-spacing:-1.2px;font-size:2.35rem!important;font-weight:750!important}
[data-testid="stMetric"]{background:white;border:1px solid #e5ebef;border-radius:12px;padding:16px 20px}
[data-testid="stMetricValue"]{font-size:1.65rem}
[data-testid="stSidebar"]{border-right:1px solid #e5ebef}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:2px;color:#087f8c;margin-bottom:8px}
.empty-state{padding:42px 30px;background:white;border:1px dashed #c8d5dd;border-radius:16px;text-align:center;margin-top:20px}
.empty-state h3{font-size:21px;margin:8px 0}.empty-state p{color:#667985;margin:0}
</style>""", unsafe_allow_html=True)


def secret(*keys):
    for key in keys:
        if os.getenv(key):
            return os.environ[key]
        try:
            value = st.secrets.get(key)
            if value:
                return str(value)
        except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
            pass
    return ""


if "market_client" not in st.session_state:
    st.session_state.market_client = BinanceFutures()
client = st.session_state.market_client

with st.sidebar:
    st.markdown("### Kontrol scanner")
    mode = st.selectbox("Gaya trading", list(MODES), index=1)
    bt, et = MODES[mode]
    st.caption(f"Bias {bt.upper()} → sinyal {et.upper()}")
    limit = st.select_slider("Maksimum pair", options=[10, 20, 30, 50, 100, 150, 200], value=30)
    auto = st.toggle("Scan otomatis", value=False)
    tg_enabled = st.toggle("Alert Telegram", value=False)
    with st.expander("Pengaturan scan"):
        interval = st.selectbox("Jeda scan (detik)", [30, 60, 120, 300], index=1)
        min_volume = st.number_input("Minimum volume 24 jam (juta USDT)", min_value=0.0, value=10.0, step=5.0)
        chosen = st.text_input("Pair pilihan (opsional)", placeholder="BTCUSDT, ETHUSDT, SOLUSDT")
        st.caption("Kosong = urutan volume futures terbesar. Pair pilihan tetap melewati batas volume.")
    with st.expander("Koneksi Telegram"):
        configured_token = secret("TELEGRAM_BOT_TOKEN", "TG_TOKEN")
        configured_chat = secret("TELEGRAM_CHAT_ID", "TG_CHAT")
        if configured_token and configured_chat:
            st.success("Kredensial server tersedia")
        token_override = st.text_input("Token bot (sesi ini)", type="password")
        chat_override = st.text_input("Chat ID (sesi ini)", type="password")
        st.caption("Bisa juga diisi melalui Settings → Secrets. Kirim /start kepada bot terlebih dahulu.")
        tg = Telegram(token_override or configured_token, chat_override or configured_chat)
        if st.button("Kirim pesan tes", use_container_width=True):
            ok, note = tg.send("Tes Momentum Scanner — Telegram terhubung.", f"test|{time.time_ns()}")
            st.success(note) if ok else st.warning(note)
    st.divider()
    st.caption("Sumber: Binance USDⓈ-M perpetual. Tidak memerlukan API key bursa.")
    st.caption("Scan otomatis mengikuti sesi aplikasi. Untuk pemantauan tanpa browser, jalankan worker.py pada komputer/server yang tetap aktif.")

symbols = tuple(dict.fromkeys(s.strip().upper() for s in chosen.split(",") if s.strip()))
signature = (mode, limit, min_volume, symbols)
if st.session_state.get("scan_signature") != signature:
    st.session_state.scan_signature = signature
    st.session_state.pop("report", None)
    st.session_state.pop("last_scan_error", None)
    st.session_state.last_scan_monotonic = 0

st.markdown('<div class="eyebrow">BINANCE FUTURES · USDT PERPETUAL</div>', unsafe_allow_html=True)
st.title("Momentum Scanner")
st.caption("Temukan pullback searah tren. Periksa setup. Terima alert Telegram.")


def run_scan():
    progress = st.progress(0, text="Mengambil daftar kontrak futures…")
    try:
        report = client.scan(mode, limit, min_volume * 1_000_000, symbols,
                             lambda done, total, pair: progress.progress(done / total, text=f"Memindai {pair} · {done}/{total}"))
        notifications = []
        if tg_enabled:
            if not tg.configured:
                notifications.append("Alert belum aktif: isi token dan Chat ID Telegram.")
            else:
                for i, result in enumerate(report["results"]):
                    if not result.candidate:
                        continue
                    try:
                        if client.now() - result.checked_at > 15_000:
                            result = client.inspect(result.symbol, result.mode, report["contracts"][result.symbol], fresh=True)
                            report["results"][i] = result
                        if result.candidate:
                            _, note = tg.send(message(result), result.key)
                            notifications.append(f"{result.symbol}: {note}")
                    except (MarketError, ValueError, KeyError, TypeError) as exc:
                        notifications.append(f"{result.symbol}: alert ditunda — {exc}")
        report["notifications"] = notifications
        st.session_state.report = report
        st.session_state.pop("last_scan_error", None)
    except (MarketError, ValueError, KeyError, TypeError) as exc:
        st.session_state.last_scan_error = str(exc)
        st.session_state.pop("report", None)
    finally:
        st.session_state.last_scan_monotonic = time.monotonic()
        progress.empty()


def planning(result, contract):
    """All operational fields stay collapsed until a user selects one candidate."""
    with st.expander("Hitung rencana entry, SL, dan TP"):
        st.caption("Isi data akun aktual. Perhitungan awal akan diberi WAIT sampai seluruh verifikasi lengkap. Data akun tidak dikirim ke Binance.")
        with st.form(f"risk_{result.key}"):
            c1, c2, c3 = st.columns(3)
            equity = c1.number_input("Ekuitas aktual (USDT)", 0.0, value=0.0, step=100.0)
            free = c2.number_input("Margin bebas (USDT)", 0.0, value=0.0, step=100.0)
            leverage = c3.selectbox("Leverage isolated", [1, 2, 3], index=1)
            risk_pct = c1.number_input("Risiko per transaksi (%)", .01, .5, .5, .01)
            fee = c2.number_input("Fee taker akun (%)", 0.0, 2.0, 0.0, .01, format="%.4f")
            slip = c3.number_input("Cadangan slippage exit (%)", 0.0, 10.0, 0.0, .01, format="%.4f")
            funding = c1.number_input("Cadangan funding selama posisi (%)", 0.0, 10.0, 0.0, .01, format="%.4f")
            count = c2.number_input("Jumlah posisi + pending aktif", 0, 100, 0)
            open_risk = c3.number_input("Risiko semua posisi/pending (USDT)", 0.0, value=0.0)
            alt_risk = c1.number_input("Risiko altcoin searah (USDT)", 0.0, value=0.0)
            drawdown = c2.number_input("Drawdown sejak 00.00 UTC (%)", 0.0, 100.0, 0.0)
            streak = c3.number_input("Trade rugi beruntun hari UTC ini", 0, 100, 0)
            liquidation = c1.number_input("Likuidasi dari bursa (harga)", 0.0, value=0.0, format="%.8f")
            st.caption("Drawdown harus memasukkan biaya dan unrealized PnL serta penyesuaian deposit/withdrawal. Funding adalah cadangan total untuk horizon posisi, bukan otomatis satu interval.")
            verified = st.checkbox("Data akun dan biaya sudah benar; isolated dan auto-add margin nonaktif telah dicek")
            news = st.checkbox("Kalender terverifikasi: tidak ada CPI/FOMC/konferensi pers terkait ±15 menit dari rencana entry")
            liquid = st.checkbox("Order book cukup likuid untuk kuantitas hasil perhitungan")
            liq_ok = st.checkbox("Likuidasi dari bursa cocok dengan pair, arah, kuantitas hasil perhitungan, margin, dan risk tier")
            submitted = st.form_submit_button("Perbarui harga & verifikasi rencana", type="primary")
        if submitted:
            try:
                client.sync_clock()
                fresh = client.inspect(result.symbol, result.mode, contract, fresh=True)
                if fresh.key != result.key:
                    st.warning("Candle berubah. Jalankan scan baru dan pilih kandidat terbaru.")
                    return
                account = Account(equity=equity, free_margin=free, risk_pct=risk_pct, leverage=leverage,
                    taker_pct=fee, exit_slippage_pct=slip, funding_reserve_pct=funding,
                    active_count=count, open_risk=open_risk, same_direction_alt_risk=alt_risk,
                    drawdown_pct=drawdown, loss_streak=streak, liquidation=liquidation,
                    verified=verified, news_clear=news, liquidity_ok=liquid, liquidation_verified=liq_ok)
                plan = verify(fresh, contract, account, client.now())
                st.session_state[f"plan_{result.key}"] = (fresh, plan)
            except (MarketError, ValueError, KeyError, TypeError) as exc:
                st.session_state.pop(f"plan_{result.key}", None)
                st.error(f"WAIT — {exc}")
        saved = st.session_state.get(f"plan_{result.key}")
        if not saved:
            return
        fresh, plan = saved
        expired = client.now() - fresh.checked_at > 30_000 or bool(fresh.levels and client.now() >= fresh.levels["expires_at"])
        if plan["ready"] and not expired:
            st.success(f"SETUP {fresh.direction} VALID — MENUNGGU TRIGGER. Verifikasi akun berdasarkan input Anda.")
        else:
            st.warning("WAIT — " + ("snapshot sudah lewat 30 detik; verifikasi ulang" if expired else "; ".join(plan["reasons"])))
        d = plan["data"]
        if d:
            st.caption("Perhitungan berdasarkan input; belum siap entry jika masih ada WAIT.")
            st.dataframe(pd.DataFrame([{"Q total": d["quantity"], "Risiko estimasi USDT": d["actual_risk"],
                "Margin USDT": d["margin"], "Cadangan biaya USDT": d["cost_reserve"], "RR bersih TP1": d["net_rr"],
                "Q TP1": d["tp1_qty"], "Q TP2": d["tp2_qty"], "Q runner": d["runner_qty"]}]), hide_index=True, use_container_width=True)
        if plan["ready"] and not expired:
            p = fresh.levels
            rows = [("Entry STOP-LIMIT · trigger Last", p["trigger"]), ("Limit / asumsi fill terburuk", p["entry"]),
                    ("SL stop-market · rencana pemicu Mark", p["stop"]), ("TP1 · 2R", p["tp1"]),
                    ("TP2 · 3R", p["tp2"]), ("BE estimasi setelah TP1 terisi", d["breakeven"])]
            st.dataframe(pd.DataFrame(rows, columns=["Rencana", "Harga"]), hide_index=True, use_container_width=True)
            st.caption(f"Berlaku sampai {wib(p['expires_at'])}. Runner {d['runner_qty'] / d['quantity']:.1%}; trailing 2 ATR setelah TP2. Perubahan harga ke TP1 {abs(p['tp1'] - p['entry']) / p['entry']:.2%}.")
            st.dataframe(pd.DataFrame(plan["scenarios"]), hide_index=True, use_container_width=True)
            with st.expander("Rincian risiko"):
                st.json(d)
            if st.button("Kirim rencana terverifikasi ke Telegram", key=f"send_{result.key}"):
                if client.now() - fresh.checked_at > 30_000:
                    st.warning("Snapshot sudah kedaluwarsa. Verifikasi ulang.")
                else:
                    ok, note = tg.send(message(fresh, plan), fresh.key + "|verified")
                    st.success(note) if ok else st.warning(note)


@st.fragment(run_every=interval if auto else None)
def dashboard():
    left, right = st.columns([1, 3])
    clicked = left.button("Scan sekarang", type="primary", use_container_width=True)
    right.caption((f"Otomatis setiap {interval} detik setelah scan selesai" if auto else "Mode manual") +
                  " · 500 candle tertutup minimum per timeframe")
    due = time.monotonic() - st.session_state.get("last_scan_monotonic", 0) >= interval
    if clicked or (auto and due):
        run_scan()
    if st.session_state.get("last_scan_error"):
        st.error("WAIT — " + st.session_state.last_scan_error)
    if tg_enabled and not tg.configured:
        st.warning("Alert Telegram belum terhubung. Isi koneksi Telegram di sidebar.")
    report = st.session_state.get("report")
    if not report:
        st.markdown('<div class="empty-state"><div style="font-size:32px">⌁</div><h3>Mulai dari satu scan</h3><p>Pilih gaya trading, lalu tekan Scan sekarang.<br>Hasil hanya muncul jika data dan aturan indikator terpenuhi.</p></div>', unsafe_allow_html=True)
        return
    results = report["results"]
    candidates = sorted([r for r in results if r.candidate], key=lambda r: r.values.get("rvol", 0), reverse=True)
    a, b, c, d = st.columns(4)
    a.metric("Pair diperiksa", len(results))
    b.metric("Kandidat momentum", len(candidates))
    c.metric("Kendala data", len(report["errors"]))
    d.metric("Telegram", "Aktif" if tg_enabled and tg.configured else "Nonaktif")
    st.caption(f"Scan terakhir {wib(report['finished_at'])} · {report['eligible']} kontrak memenuhi pilihan dan volume. Kandidat adalah snapshot teknis, belum merupakan izin entry.")
    if report["selected"] == 0:
        st.warning("Tidak ada pair yang cocok. Periksa penulisan simbol atau minimum volume.")
    if len(results) + len(report["errors"]) < report["selected"]:
        st.warning("Scan sebagian: batas permintaan tercapai. Pair yang belum diperiksa akan diproses pada scan berikutnya dengan bantuan cache.")
    st.markdown("### Momentum terdeteksi")
    if candidates:
        table = [{"Pair": r.symbol, "Arah": r.direction, "Status": "WAIT · verifikasi risiko",
                  "RSI": round(r.values["rsi"], 1), "ADX": round(r.values["adx"], 1),
                  "Volume relatif": round(r.values["rvol"], 2), "Candle WIB": wib(r.signal_close + 1)} for r in candidates]
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
        selected = st.selectbox("Lihat detail kandidat", [r.symbol for r in candidates])
        r = next(x for x in candidates if x.symbol == selected)
        with st.expander(f"Kriteria {r.symbol} · {r.direction}"):
            st.dataframe(pd.DataFrame(r.checks), hide_index=True, use_container_width=True)
            st.caption(f"Snapshot {wib(r.checked_at)}. Funding terakhir {r.values['funding_rate']:.4%}; jadwal berikutnya {wib(r.values['next_funding'])}. Bukan proyeksi total biaya funding.")
            st.caption("Hambatan terdekat: " + (number(r.levels["obstacle"]) if r.levels["obstacle"] is not None else "tidak teridentifikasi dalam cakupan data"))
        planning(r, report["contracts"][r.symbol])
    else:
        st.info("Belum ada kandidat yang memenuhi semua aturan. Lihat alasan penolakan jika ingin memeriksa pair tertentu.")
    with st.expander("Alasan pair belum lolos"):
        rejected = [r for r in results if not r.candidate]
        st.dataframe(pd.DataFrame([{"Pair": r.symbol, "Status": r.status, "Alasan": "; ".join(r.reasons)} for r in rejected]), hide_index=True, use_container_width=True)
        if report["errors"]:
            st.dataframe(pd.DataFrame(report["errors"]), hide_index=True, use_container_width=True)
    with st.expander("Status pengiriman Telegram"):
        for note in report.get("notifications", []):
            st.text(note)
        if tg.configured:
            st.dataframe(pd.DataFrame([{"Sinyal": k, "Status": s, "Waktu WIB": wib(int(t * 1000)), "Keterangan": n}
                for k, s, t, n in tg.history()]), hide_index=True, use_container_width=True)
        else:
            st.caption("Belum ada koneksi Telegram.")


dashboard()
with st.expander("Aturan ringkas & batas penggunaan"):
    st.markdown("""
    - **Tren:** EMA 50/200, slope EMA 50 selama lima candle, ADX 14 ≥ 20.
    - **Entry teknis:** pullback EMA 20 ± 0,25 ATR pada lima candle sebelum sinyal, struktur EMA 50 tetap utuh, breakout candle konfirmasi.
    - **Momentum:** RSI 50–65 LONG / 35–50 SHORT; volume ≥ 1,2× rata-rata 20 candle sebelumnya. VWAP reset 00.00 UTC untuk scalping/intraday.
    - **Risiko:** SL struktural 1–3 ATR; ruang target ≥ 2R + 0,1 ATR. TP1 2R, TP2 3R, runner tanpa target tetap.
    - **Setelah fill:** hitung ulang memakai fill aktual, lindungi partial fill, TP1 → BE termasuk biaya, TP2 → trailing 2 ATR pada candle lengkap setelah TP2. SL hanya diperketat.
    - **Pembatalan:** tiga candle setelah sinyal; scalping/intraday paling lambat 00.00 UTC. Evaluasi ulang bila trigger/SL tersentuh, bias atau biaya berubah.
    - **Time exit:** setelah 12 candle sejak fill jika belum pernah +1R. Scalping/intraday tutup sisa sebelum 00.00 UTC.

    Aplikasi ini memindai dan mengirim alert. Tidak memasang order, memantau fill/partial TP, menggeser SL, atau menjalankan exit otomatis. Proses setelah fill dilakukan di bursa/bot eksekusi terpisah.
    Strategi belum dibuktikan profitabilitasnya; pengujian kode bukan backtest. Funding, slippage, dan fill aktual dapat mengubah hasil. Tidak ada skor peluang menang atau janji profit.
    """)

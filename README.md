# SecretAgent v5.5: Autonomous Trading System & Manual Position Tracker (Phases 1–6)

Implementasi ini mentransformasikan repositori **SecretAgent v5.5 Manual Position Tracker** menjadi sistem trading otonom terkendali risiko (**Phases 1–6**), sambil mempertahankan **100% mesin momentum v5.5 asli, ambang sinyal, kalkulasi indikator, multi-timeframe SMC, prompt AI, dan modul paper trading asli** (236 node AST sumber awal tetap identik).

Real-money trading (`EXECUTION_MODE=LIVE` dan `LIVE_TRADING_ENABLED=true`) **dinonaktifkan secara default** (`EXECUTION_MODE=PAPER`, `LIVE_TRADING_ENABLED=false`, `AUTONOMOUS_TRADING_ENABLED=false`) dan tidak pernah aktif otomatis setelah build, pengujian, migrasi, atau restart.

---

## 1. Arsitektur Fase 1–6

| Fase | Modul | Kapabilitas Utama |
| :--- | :--- | :--- |
| **Baseline & Manual Tracker** | `app.py`, `manual_tracker/engine.py`, `market.py`, `monitor.py`, `interactions.py`, `telegram.py` | Strategi momentum v5.5 asli (24 skenario fixture terverifikasi), pelacakan posisi manual via Telegram, outbox idempoten. |
| **Phase 1: Account Intelligence** | `manual_tracker/account.py` | Sinkronisasi akun terotentikasi HMAC-SHA256 untuk **Binance Spot (`SPOT`)** dan **Binance USD-M Perpetual (`LINEAR`)**, validasi kesegaran data (`is_fresh`), pemeriksaan izin API (wajib `enableWithdrawals=false` untuk Live), dan rekonsiliasi posisi/order otoritatif (`STARTUP`, `PRE_ENTRY`, `UNCERTAIN_ORDER`, `RECONNECT`, `POST_FILL_EXIT`, `PERIODIC`). |
| **Phase 2: Dynamic Risk Engine** | `manual_tracker/risk_engine.py` | Perhitungan ukuran posisi deterministik (`Decimal`) dari ekuitas akun riil, jarak SL, multiplier kontrak, estimasi fee, dan slippage. Pembulatan ke bawah (`step_size`), verifikasi ulang risiko pasca-pembulatan (tidak pernah menaikkan risiko demi memenuhi ukuran minimum exchange), batas kerugian harian persisten lintas restart (`00:00:00 UTC`), dan circuit breaker persisten. |
| **Phase 3: Autonomous Paper & Backtest** | `manual_tracker/paper_autonomous.py` | Simulasi siklus penuh menggunakan pipeline validasi risiko yang sama dengan eksekusi live. Mencakup spread, slippage, fee maker/taker, partial fill, akrual funding, eksekusi gap-open, dan tie-breaker intrabar konservatif (`SL_FIRST_CONSERVATIVE`). Memisahkan secara ketat metrik `BACKTEST`, `PAPER`, `TESTNET`, dan `LIVE`. |
| **Phase 4: Testnet Execution Engine** | `manual_tracker/execution.py` | Siklus hidup order dengan persistensi niat pra-kirim (`INTENT_PERSISTED`) dan `clientOrderId` idempoten (`sa55_<env>_<pid>_<role>_<seq>`). Rekonsiliasi status order saat timeout (`UNCERTAIN_TIMEOUT`) tanpa retry buta. Pemasangan dan verifikasi order proteksi SL/TP otomatis setelah fill entry, sinkronisasi kuantitas partial fill, dan kebijakan darurat terkonfigurasi (`ALERT_AND_HOLD` default atau `EMERGENCY_MARKET_CLOSE`). |
| **Phase 5: Guarded Live Trading** | `manual_tracker/live_guard.py` | Evaluasi kesiapan 12 gerbang (`evaluate_live_readiness`), aktivasi eksplisit pengguna dengan frasa konfirmasi (`AUTHORIZE_SECRETAGENT_V55_LIVE`), rantai otorisasi entry 10 langkah wajib, circuit breaker persisten, serta Kill Switch UI & Telegram (`/killswitch`, `/halt`, `/resume_entries`) yang membedakan penghentian entry baru (`HALT_NEW_ENTRIES`) dari penutupan darurat (`EMERGENCY_CLOSE_ALL`). |
| **Phase 6 & Section 10: Smart Trailing & Reliability** | `manual_tracker/trailing_manager.py`, `reliability.py`, `orchestrator.py` | Smart Trailing Stop dalam mode **Advisory (default)** dan **Authorized Automatic** (+1R break-even termasuk fee/buffer, ATR trailing dari candle tertutup, sifat monotonik ketat, verifikasi modifikasi order di exchange), prosesor stream WebSocket real-time dengan fallback/resync REST, pembatas laju Groq 7-key terkoordinasi, dan pemulihan restart 8 langkah. |

---

## 2. Setup, Migrasi, dan Menjalankan Sistem

Gunakan Python 3.11+ / 3.12:

```bash
cd secretagent-main
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp config.example.env .env
```

Edit `.env` dan arahkan `SECRETAGENT_DB_FILE` serta `MANUAL_DB_FILE` ke path absolut file SQLite yang sama (`trading_v5.db`).

Validasi konfigurasi, jalankan migrasi aditif (membuat backup SQLite otomatis terlebih dahulu), dan periksa status:

```bash
set -a
source ./.env
set +a
python3 -m manual_tracker --check
python3 -m manual_tracker --migrate --db "$SECRETAGENT_DB_FILE"
python3 -m manual_tracker --status --db "$SECRETAGENT_DB_FILE"
```

Menjalankan worker pemantauan dan pengiriman Telegram:

```bash
python3 -m manual_tracker --worker --db "$SECRETAGENT_DB_FILE"
```

Menjalankan antarmuka **Momentum Radar** (tetap sederhana tanpa Market Scanner, dilengkapi header operasional dan Kill Switch):

```bash
streamlit run app.py
```

---

## 3. Pengujian dan Verifikasi Kualitas

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py manual_tracker tests
sha256sum -c FILE_HASHES.sha256
```

Semua **109 automated tests** (89 baseline/regression tests + 20 Phase 1–6 unit, integration, safety, dan backtest tests) lulus tanpa memanggil layanan eksternal nyata atau mengirim order uang riil. Lihat `docs/TEST_RESULTS.md` dan `docs/AUDIT.md` untuk rincian cakupan dan syarat validasi eksternal sebelum aktivasi live.

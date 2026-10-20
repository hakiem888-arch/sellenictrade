# Audit Baseline dan Cakupan Implementasi Fase 1–6

## 1. Sumber Baseline & Penyebab Kegagalan Fingerprint Awal
- **Sumber baseline**: Arsip `secretagent-v5.5-manual-tracker.zip` (`secretagent-main/`).
- **Investigasi kegagalan fingerprint awal**:
  - Pada runtime Python 3.11, `test_original_strategy_globals_prompts_and_embedded_modules_unchanged` awalnya gagal karena `tests/baseline_v55.json` dibuat pada Python 3.12 (`PEP 695`), yang menambahkan atribut `type_params=[]` pada node AST `ClassDef`, `FunctionDef`, dan `AsyncFunctionDef`.
  - Seluruh 236 node AST tingkat atas pada `app.py` (termasuk `_EMBEDDED_AI_CONSENSUS_B64` dengan hash `4d70b01335f86e65`, kalkulasi indikator, skor momentum 24 skenario, prompt AI, dan gerbang risiko) **100% tidak berubah**.
  - Normalisasi kompatibilitas AST Python 3.11/3.12 (`_ast_dump_py312_compat`) diterapkan pada `tests/test_app_regression.py` tanpa mengubah satu pun hash di `tests/baseline_v55.json` dan tanpa melemahkan pengujian regresi.

## 2. Modul Baru & Integrasi (Fase 1–6)
- `manual_tracker/account.py`: Adapter Binance Spot (`SPOT`) dan USD-M Futures (`LINEAR`) dengan tanda tangan HMAC-SHA256, pemeriksaan izin API (menolak kunci dengan izin penarikan dana aktif pada mode Live), validasi kesegaran data, dan rekonsiliasi posisi/order terhadap database lokal.
- `manual_tracker/risk_engine.py`: Mesin risiko deterministik, perhitungan ukuran posisi berbasis ekuitas riil dan jarak stop-loss, pembulatan `step_size` ke bawah, verifikasi ulang risiko pasca-pembulatan, ledger batas kerugian harian persisten lintas restart (`00:00:00 UTC`), dan circuit breaker persisten.
- `manual_tracker/paper_autonomous.py`: Siklus paper trading otonom dengan simulasi biaya realistis (fee maker/taker, spread, slippage, partial fill, akrual funding, eksekusi gap-open, tie-breaker konservatif `SL_FIRST_CONSERVATIVE`), serta backtest kronologis bebas look-ahead bias yang terpisah untuk `BACKTEST`, `PAPER`, `TESTNET`, dan `LIVE`.
- `manual_tracker/execution.py`: Manajemen siklus hidup order dengan persistensi `INTENT_PERSISTED` sebelum pengiriman jaringan, idempotensi `clientOrderId`, rekonsiliasi order saat timeout (`UNCERTAIN_TIMEOUT`), serta penempatan dan verifikasi order proteksi SL/TP otomatis setelah fill entry.
- `manual_tracker/live_guard.py`: Evaluasi kesiapan 12 gerbang, aktivasi eksplisit pengguna (`AUTHORIZE_SECRETAGENT_V55_LIVE`), pipeline otorisasi entry 10 langkah, serta kontrol Kill Switch (`HALT_NEW_ENTRIES` dan `EMERGENCY_CLOSE_ALL`).
- `manual_tracker/trailing_manager.py`: Integrasi Smart Trailing Stop dalam mode `ADVISORY` (default) dan `AUTOMATIC` (ketika diizinkan eksplisit), mencakup break-even +1R yang disesuaikan biaya/buffer, ATR trailing dari candle tertutup, sifat monotonik ketat, dan verifikasi konfirmasi modifikasi order di exchange.
- `manual_tracker/reliability.py` & `manual_tracker/orchestrator.py`: Prosesor event WebSocket kline/order real-time dengan deteksi batas candle tertutup dan resinkronisasi REST, pembatas laju Groq 7-key terkoordinasi, manajer pemulihan restart 8 langkah, dan pemantau kesehatan 10 subsistem.

## 3. Batasan Lingkungan & Validasi Eksternal yang Diperlukan
- Lingkungan sandbox ini tidak memiliki koneksi internet keluar dan tidak menyertakan kredensial API exchange atau bot Telegram nyata.
- Seluruh pengujian HTTP/WebSocket menggunakan mock transport deterministik. **Pengujian dengan mock tidak diklaim sebagai verifikasi konektivitas live exchange.**
- Sebelum mengaktifkan `EXECUTION_MODE=TESTNET` atau `EXECUTION_MODE=LIVE` pada host produksi Anda, jalankan verifikasi dengan kredensial Testnet Binance (`https://testnet.binancefuture.com` / `https://testnet.binance.vision`) dan bot Telegram khusus Anda.

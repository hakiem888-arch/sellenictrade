# State Machine, Migrasi Database, dan Pemulihan Restart

## 1. Pemisahan State Internal (Fase 1–6)
Tabel `at_positions` dan `at_orders` memisahkan secara eksplisit lima dimensi status agar tidak pernah menggunakan satu kolom status yang ambigu:

1. **`signal_state`**: `AUTHORIZED`, `VALIDATED`, `EXTERNAL`
2. **`order_state`**: `INTENT_PERSISTED`, `SUBMITTING`, `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`, `UNCERTAIN_TIMEOUT`
3. **`position_state`**: `PENDING_ENTRY`, `OPENING`, `PROTECTED_ACTIVE`, `PROTECTION_REQUIRED`, `CLOSING`, `CLOSED`, `HALTED_DISCREPANCY`
4. **`protective_order_state`**: `UNPROTECTED`, `SUBMITTING_PROTECTION`, `VERIFIED`, `PARTIAL_COVERAGE`, `PROTECTION_FAILED`, `MODIFICATION_UNCERTAIN`, `CLOSED`
5. **`account_state`**: `SYNCHRONIZED`, `STALE`, `DISCREPANCY`

State posisi manual di `mt_positions` (`SIGNAL_SENT`, `AWAITING_ENTRY_DETAILS`, `ACTIVE`, `PAUSED`, `TRACKING_ERROR`, `INVALID_DATA`, `EXIT_REPORTED`, `CLOSED`, `IGNORED`) tetap dipertahankan 100%.

## 2. Migrasi Aditif (`mt_*` & `at_*`)
Perintah `python3 -m manual_tracker --migrate --db /path/trading_v5.db` membuat salinan backup SQLite konsisten terlebih dahulu, lalu menerapkan skema `mt_schema` versi 1 & 2 secara idempoten (`CREATE TABLE IF NOT EXISTS` tanpa `DROP` atau `DELETE` terhadap tabel legacy `signals`, `paper_positions`, `paper_account_ledger`, maupun tabel `mt_*`).

Tabel otonom baru (`at_*`):
- `at_account_snapshots`: Riwayat sinkronisasi saldo dompet, saldo tersedia, ekuitas, PnL belum terealisasi/terealisasi, margin terpakai/tersedia, posisi terbuka, order aktif, izin API, dan status kesehatan.
- `at_instrument_specs`: Metadata instrumen (`tick_size`, `step_size`, `min_qty`, `max_qty`, `min_notional`, `contract_multiplier`, `max_leverage`, `maker_fee`, `taker_fee`, `funding_rate`).
- `at_risk_state` & `at_daily_risk_ledger`: Status risiko persisten, penghitung kerugian beruntun, kegagalan eksekusi, aktivasi live pengguna, status Kill Switch, serta buku besar PnL harian dengan batas hari `00:00:00 UTC`.
- `at_circuit_breakers`: Status circuit breaker persisten lintas restart.
- `at_orders` & `at_fills`: Jejak siklus hidup order (`client_order_id` unik, `exchange_order_id`, kuantitas diminta vs terisi, harga rata-rata fill, fee).
- `at_positions`: Status posisi otonom dengan pemisahan lingkungan (`BACKTEST`, `PAPER`, `TESTNET`, `LIVE`) dan pelacakan stop (`initial_stop`, `current_stop`, `recommended_stop`, `last_confirmed_exchange_stop`).
- `at_reconciliation_log` & `at_audit_events`: Jejak audit lengkap untuk setiap keputusan risiko, niat order, fill, perubahan trailing stop, rekonsiliasi, circuit breaker, dan tindakan operator tanpa pernah mencatat kredensial rahasia.

## 3. Urutan Pemulihan Restart 8 Langkah (`RestartRecoveryManager`)
Saat aplikasi/worker dimulai ulang:
1. Memuat state risiko persisten, status Kill Switch, dan penghitung kerugian beruntun dari `at_risk_state`.
2. Menghubungkan kembali ke adapter exchange dan menyinkronkan akun (`sync_account`).
3. Mengambil posisi terbuka dan order aktif aktual dari exchange, serta merekonsiliasi setiap order berstatus `SUBMITTING` atau `UNCERTAIN_TIMEOUT`.
4. Merekonsiliasi state lokal terhadap posisi dan order exchange (`trigger_source='STARTUP'`).
5. Memverifikasi cakupan order proteksi SL/TP untuk seluruh posisi terbuka; jika ada posisi tanpa proteksi terverifikasi, langsung mengaktifkan circuit breaker `MISSING_PROTECTIVE_ORDERS`.
6. Memulihkan tugas pemantauan tanpa duplikasi worker (dijaga oleh OS file lock `worker_lock`).
7. Memeriksa seluruh circuit breaker dan batas kerugian harian (`at_daily_risk_ledger`).
8. Mengizinkan entry baru **hanya** jika seluruh pemeriksaan kesiapan dan rekonsiliasi bersih; sinyal lama yang sudah tersimpan di database tidak pernah membuka posisi duplikat.

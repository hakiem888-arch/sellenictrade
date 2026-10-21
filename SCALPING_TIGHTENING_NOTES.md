# Revisi alert dan parameter scalping (versi eksperimen)

## Perubahan
- Alert Telegram analisis AI sekarang menampilkan label `SCALPING`, timeframe entry 5m, konfirmasi 15m/1H, dan pengingat bahwa alert bukan order.
- Alert radar pola menampilkan mode utama SCALPING dan keterangan timeframe, serta penekanan untuk memprioritaskan setup TRIGGERED dan tidak mengejar status WAIT/DEVELOPING.
- Parameter strategi diperketat: volume ratio default 1.3, batas RSI entry long default 65, confidence AI default 80 jika filter AI diaktifkan.
- Target ROI Freqtrade diturunkan bertahap (1.2% awal, lalu 0.8%, 0.5%, 0.3%) dan stop-loss maksimum ditetapkan 2.5%, dengan trailing stop mulai aktif setelah profit 0.6% dan mengunci 0.3%.
- Config dan contoh config tetap `dry_run: true`; tidak mengaktifkan live trading.

## Catatan penting
Ini baseline untuk diuji, bukan klaim strategi pasti profit. Angka ROI adalah target gross sebelum biaya; fee, spread, slippage, pair, likuiditas, dan kondisi pasar dapat membuat hasil bersih negatif. Freqtrade konfigurasi dapat menimpa nilai strategi. Jalankan backtest dengan fee realistis lalu dry-run sebelum dana sungguhan. Jangan mengubah `dry_run` menjadi `false` sebelum hasil dan mekanisme exit diverifikasi.

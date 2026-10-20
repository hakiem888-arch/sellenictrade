# Hasil pengujian

Tanggal: 20 Oktober 2026. Runtime: Python 3.12.14, Linux. **89 test lulus**, 0 failure, 0 error, 0 skipped. Waktu eksekusi suite final: 5.133 detik. Log utuh: `TEST_LOG.txt`.

```text
python -m unittest discover -v
Ran 89 tests in 5.133s
OK

python -m compileall -q app.py manual_tracker tests
exit code 0
```

Tidak ada test suite dalam arsip awal. Suite baru menguji perilaku yang diminta, integrasi SQLite dengan app asli, dan regresi terhadap sumber v5.5 yang diunggah. Tidak ada order exchange dalam pengujian.

## Bukti dan cakupan

| Area | Verifikasi | Jenis |
| --- | --- | --- |
| Registrasi | LONG/SHORT linear, LONG spot, harga aktual terpisah dari referensi, konfirmasi wajib | Percakapan Telegram sintetis, SQLite nyata |
| Instrumen | WAIT, inverse, spot SHORT, multiplier adapter yang salah ditolak | Unit test deterministik |
| Input | Quantity/entry/SL/fee invalid, NaN, Infinity, nilai nol/negatif, SL salah sisi/tick, metadata hilang | Unit dan integration test |
| Otorisasi | Allowlist user+chat, kepemilikan sinyal/posisi, message ID, nonce, expiry | Update Telegram mock |
| Idempotensi | Callback sama, tombol ditekan dua kali, konfirmasi ulang, dua klaim bersamaan | SQLite nyata, termasuk konkurensi thread |
| Akuntansi | PnL LONG/SHORT, multiplier, biaya, risiko uang awal, R, leverage/ROI, spot accounting | Harga sintetis, Decimal |
| Trailing | +1R, ambang alternatif, fee-adjusted BE kedua arah, ATR LONG/SHORT, monotonic stop, tick dan minimum improvement | Unit test deterministik |
| Data pasar | Candle tertutup, gap candle, mark/last reference, metadata, cache, rate-limit cooldown, reconnect/resync | HTTP mock, tanpa API exchange |
| Stale | Quote basi/future/NaN, ATR basi, timestamp out of order, rekomendasi pending basi | Unit dan integration test |
| Alert | Dedup level/stage, last notified stop, touch advisory, supersede level lama, pause saat fetch | SQLite + Telegram mock |
| Failure | 429, 403, timeout UNKNOWN, restart ketika SENDING, rollback database | Fault injection |
| Restart | State, ekstrem, stop, dedup, offset, PAUSED tetap PAUSED | SQLite dibuka ulang |
| Penutupan | EXIT_REPORTED lalu konfirmasi CLOSED, harga exit opsional, sumber USER_REPORTED, batal/expiry menjadi PAUSED | Percakapan sintetis |
| Worker | Lock bot per-host, token rotation, bot binding, webhook conflict, 409, offset persisten | Mock Telegram dan OS lock nyata |
| Worker terpadu | Mulai tiga loop, resync, hitung BE, dispatcher kirim mock, berhenti dan lepas lock | Thread worker nyata dengan adapter mock |
| Dashboard | Panel privat, kolom posisi, permintaan pause via Telegram tanpa mutasi langsung | Mock antarmuka Streamlit |
| Legacy | Schema app startup, CLI status, sender/format asli saat opt-out, sinyal enabled, saved WAIT ditolak | App asli + SQLite, HTTP dimock |
| Preservasi | 236 node AST sumber awal identik; seluruh embedded payload tetap identik | Fingerprint sumber unggahan |
| Momentum | **24 skenario** menghasilkan output penuh yang sama dengan fungsi dari ZIP asli | Fixture baseline; subkasus dalam suite |
| Paper/database | Data tabel legacy dan paper tidak berubah akibat migrasi/registrasi manual | SQLite nyata |

## Pemeriksaan startup

`STARTUP_CHECKS.json` mencatat hasil CLI berikut, semuanya exit code 0:

- `python -m manual_tracker --check`, memakai token dummy dan ID dummy khusus validasi format.
- `python -m manual_tracker --migrate --db <temporary-test-db>`, dengan data marker sebelumnya.
- `python -m manual_tracker --status --db <temporary-test-db>`.
- Backup tercipta, marker asli tetap identik, `PRAGMA integrity_check` menghasilkan `ok`.

Import app dan CLI status legacy juga lulus. Dependensi `requests`, `streamlit`, `plotly`, dan `groq` aplikasi asli tidak terpasang lengkap pada lingkungan pengerjaan. Upaya pemasangan paket tidak tersedia melalui index lingkungan ini. Harness QA memakai `pip._vendor.requests` untuk memenuhi impor requests dan fallback UI bawaan app untuk startup headless. Ini tidak mengubah requirements atau kode impor produksi.

## Belum diverifikasi terhadap layanan nyata

Tidak ada token bot operasional, interaksi pengguna Telegram nyata, akses exchange nyata, inferensi Groq nyata, atau browser Streamlit nyata dalam pengujian ini. Mock menguji kontrak yang diimplementasikan; mock tidak membuktikan layanan eksternal dapat diakses dari deployment Anda. Referensi dokumentasi API ada di `AUDIT.md`.

Setelah setup pada host Anda, lakukan pemeriksaan eksternal berikut sebelum mengandalkan notifikasinya:

1. Jalankan worker dan UI dengan database yang sama. Periksa `--status`, heartbeat, dan akses panel privat.
2. Gunakan tombol Test Telegram bawaan untuk menguji kredensial pengirim. Pastikan tidak ada webhook atau consumer getUpdates lain.
3. Pada sinyal LONG/SHORT berikutnya yang benar-benar dihasilkan aplikasi, periksa Signal ID, format referensi, dan kedua tombol. Jangan mengubah threshold untuk memaksa sinyal.
4. Periksa bahwa user di luar allowlist tidak dapat mengklaim atau mengubah tracker. Periksa `/history` setelah mengabaikan sinyal.
5. Daftarkan hanya posisi yang memang Anda eksekusi. Cocokkan entry, quantity, fee, instrument, dan SL dengan catatan Anda. Jangan membuat data posisi fiktif di database operasional.
6. Cocokkan last trade spot atau mark price futures serta tick dengan sumber Binance. Periksa timestamp dan sifat estimasi PnL.
7. Uji pause/resume, restart worker, dan laporan penutupan sesuai kondisi posisi sebenarnya. Pastikan tidak ada perubahan order exchange oleh aplikasi.
8. Verifikasi rendering panel pada versi Streamlit yang terpasang. Uji kehilangan jaringan secara terkontrol dan periksa penghentian rekomendasi serta pemulihan data.

Strategi dan parameter trailing tidak dibacktest untuk profitabilitas dalam pekerjaan ini. Seluruh hasil di atas adalah bukti pengujian perangkat lunak, bukan bukti hasil trading.

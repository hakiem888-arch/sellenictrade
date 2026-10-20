# Audit baseline dan cakupan perubahan

Sumber: arsip pengguna `v 5.5(1).zip`, berisi `secretagent-main/app.py` dan `requirements.txt`. Tidak ada database, test suite, file secrets, atau direktori `src/` terpisah di arsip.

`app.py` menyimpan 13 modul dalam `_EMBEDDED_AI_CONSENSUS_B64`: config, domain, market, features, smc, scanner, agents, providers, risk, paper, storage, services, dan monitor. Payload, hash embedded `4d70b01335f86e65`, prompt, threshold, serta algoritme modul ini tidak berubah. Beberapa label versi v5.6/v6 sudah ada pada sumber awal; audit ini tidak menyebut label tersebut sebagai fitur baru.

## Jalur integrasi

1. Alur utama menyimpan sinyal dengan `save_signal`, lalu `save_rendered_analysis` mengirim Telegram.
2. Retry monitor berada di `process_monitor_candidate_workflow`. Saat snapshot session tidak lagi ada setelah restart, integrasi baru mengambil sinyal tersimpan berdasarkan analysis ID, bukan membuat tracker dari pattern alert.
3. Pengiriman lama hanya mendukung `sendMessage`, tanpa getUpdates, callback, conversation, atau allowlist posisi.
4. Database lama mempunyai signals, agent_votes, memories, pattern_events, paper_positions, paper_account_ledger, analisis, kuota, DAG, dan heartbeat monitor. Catatan manual tidak dimasukkan ke ledger itu.
5. Akses pasar lama memakai requests REST ke endpoint publik Binance. Tracker memakai klien REST publik sendiri dengan timestamp dan validasi instrumen. Tidak ada akses private API atau API key exchange.

## Perubahan pada app.py

Hanya tiga fungsi asli diubah: `save_rendered_analysis`, `process_monitor_candidate_workflow`, dan `render_app`. Dua helper baru memeriksa flag dan menghubungkan pengiriman sinyal ke outbox. Default path database tetap `trading_v5.db`, dengan opsi environment `SECRETAGENT_DB_FILE` agar worker dan UI dapat menggunakan path absolut yang sama.

Saat flag false, pengiriman dan format Telegram asli dipakai sebagaimana sebelumnya. Saat flag true, sinyal memakai outbox persisten dan tombol interaksi. Hanya posisi yang lolos konfirmasi manual dan validasi metadata yang dipantau. Panel tambahan tidak menggantikan UI momentum maupun panel paper trading.

`tests/baseline_v55.json` mencatat SHA-256 arsip/aplikasi asli dan fingerprint 236 node AST tingkat atas yang wajib tetap identik dan dalam urutan yang sama. Pengecualian hanya tiga fungsi integrasi di atas, dua helper baru, dan assignment path database. `tests/momentum_fixtures_v55.json` berisi 24 input/output yang dihitung langsung dari fungsi momentum di arsip asli. Test memeriksa hasil penuh, bukan sekadar jumlah sinyal.

Lihat `CHANGES.patch` untuk diff app.py. `requirements.txt` tetap identik dengan file asli; modul manual hanya menambah kebutuhan standard library Python. Dependensi UI dan AI tetap mengikuti aplikasi awal.

## Batas implementasi

- Tidak ada WebSocket atau swing-structure trailing. Polling REST dan ATR deterministik tersedia.
- Mendukung Binance spot USDT LONG dan Binance USDT perpetual linear LONG/SHORT dengan multiplier 1. Inverse, spot short, dated futures, dan exchange lain ditolak.
- Tidak ada sinkronisasi saldo, margin, funding, posisi, order, fee aktual, atau fill exchange.
- Satu tracker per sinyal; scale-in, partial close, dan koreksi fill setelah registrasi belum tersedia.
- Ekstrem harga hanya berdasarkan sampel valid sejak tracking. Data sebelum registrasi dan selama outage tidak direkonstruksi.
- Penguncian consumer bersifat per-host. Deployment multi-host membutuhkan satu worker terpusat, bukan beberapa consumer bot.
- UI memerlukan worker manual yang terus hidup. Menutup browser tidak menghentikan worker; menghentikan worker menghilangkan pemantauan.
- UNKNOWN delivery membutuhkan peninjauan. Sistem tidak mengklaim Telegram exactly-once.
- Tampilan browser aktual, Telegram asli, dan koneksi exchange asli belum diverifikasi di lingkungan pengerjaan.

## Referensi API

Dokumentasi primer diperiksa pada 20 Oktober 2026 untuk memeriksa kontrak API. Pembacaan dokumentasi bukan pengujian koneksi layanan.

- [Telegram Bot API](https://core.telegram.org/bots/api), untuk getUpdates, callback query, force reply, message ID, dan respons retry_after.
- [Binance Spot Market Data Only](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only), untuk endpoint publik data-api.binance.vision.
- [Binance Spot Market API](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market), untuk latest trades dan klines.
- [Binance USD-M Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data), untuk exchangeInfo, premiumIndex, dan markPriceKlines.

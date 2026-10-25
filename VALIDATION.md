# Catatan validasi v5.8

Tanggal laporan: 26 Oktober 2026 WIB (25 Oktober 2026 UTC).

## Hasil akhir

- `python -m pytest -q --disable-warnings --maxfail=3`: **185 passed** dalam 10,59 detik.
- Kompilasi `app.py`, `manual_app.py`, `worker.py`, dan seluruh modul `futures/` berhasil.
- Startup Streamlit lokal: `/_stcore/health` mengembalikan HTTP 200 `ok`; halaman `/` HTTP 200.
- Dashboard otomatis dan alur manual dibuka dengan Streamlit AppTest tanpa exception pada skenario yang diuji.
- Masterprompt sama persis dengan `STRATEGY_RULES.txt` dalam ZIP v5.6 asli. SHA-256:
  `ceee3193cec00c2ec1d6193e568c18fb26d1647eab7655ad6a3d8e514a562fa5`.
- Tidak ada file Secrets asli dalam paket. Semua HTTP eksternal diblokir otomatis selama test suite; tidak ada pesan Telegram atau order bursa yang dikirim oleh pengujian.

## Scanner otomatis dan GUI

- Universe sintetis 300 kontrak menguji penyaringan seluruh simbol, jenis kontrak, quote/margin USDT, status TRADING, volume, jumlah transaksi, spread, rentang harga, serta timestamp.
- Alur terpadu dengan 120 market sintetis menghasilkan maksimal 10 anggota shortlist, dua kandidat, evaluasi tiga mode pada setiap kandidat, dan maksimal dua ulasan AI. BUY/LONG serta SELL/SHORT tercakup.
- Pilihan satu kandidat, interval 30/45/60/90, auto-scan aktif secara default, stop/manual scan, dan tidak adanya input pair, mode, API key, atau token pada dashboard diperiksa.
- Mode ditentukan oleh hasil aturan; AI dapat menolak. WAIT/NO TRADE, kalender tidak tersedia, data gagal, dan perubahan trigger setelah AI tidak membuka paper trade atau mengirim alert.
- Jurnal PENDING dibuat sebelum transport Telegram dipanggil. Risiko portofolio diperiksa ulang untuk kandidat berikutnya.
- Lock thread/proses dan jadwal bersama mencegah siklus tumpang tindih pada database yang sama. Lock dilepas saat error pasar maupun kegagalan penulisan ringkasan.
- Kandidat yang tertunda karena siklus lambat diprioritaskan pada siklus berikutnya apabila masih terpilih.
- Ringkasan worker, pesan error, jurnal dan ekspor diuji. `worker.py --once` diuji dengan transport simulasi; log tidak berisi kredensial.

## Data, indikator, strategi, dan AI

- OHLCV sintetis menguji EMA, RMA/Wilder, RSI 0/50/100, ATR/ADX, volume relatif tanpa candle sinyal dalam baseline, VWAP UTC, dan pivot yang membutuhkan dua candle kanan. Indikator historis tidak berubah akibat data masa depan.
- Setup LONG/SHORT lengkap menguji semua mode timeframe, minimum candle, candle tertutup, gap data, NaN, timestamp masa depan, serta batas snapshot 30 detik.
- Gate spread, Mark–Last, depth, funding, biaya, leverage, isolated, margin, tick/lot, min notional, jarak SL, RR neto, target, batas risiko, dan trigger yang sudah tersentuh tercakup.
- Masterprompt asli dikirim utuh ke Groq. Rotasi maksimal tujuh key, duplikat, autentikasi, timeout/5xx, 429 organisasi, Retry-After, schema JSON, output terpotong/malformed, NaN, harga yang tidak diizinkan, dan arah AI yang salah diuji.
- AI tidak dapat meloloskan gate numerik yang gagal. Perubahan ID rencana setelah respons AI menghasilkan WAIT.
- Tiga mode berbagi download timeframe dan satu batch quote segar. Cache metadata/candle dibatasi, berganti saat boundary candle, dan tidak berubah saat DataFrame pemanggil dimutasi. Harga/candle berjalan tetap diminta ulang.
- Adapter memakai endpoint futures dan tidak beralih ke spot saat akses Binance gagal. Pengukuran request pada fixture bukan benchmark CPU hosting.

## Kalender berita

- Parser CPI menguji ICS, line folding, zona Eastern dan DST; parser FOMC menguji rentang tanggal termasuk lintas bulan, kelengkapan tahun, serta blok seluruh hari rapat.
- Batas jendela CPI/FOMC, cache, sumber gagal, format rusak, kalender kedaluwarsa dan cakupan tanggal hilang diuji. Data kalender tidak lengkap menghasilkan WAIT.
- Di luar test suite, dokumen BLS dan Federal Reserve berhasil diunduh dengan HTTP 200 dan diproses menjadi 80 jendela peristiwa dari cakupan kalender sumber. Ini memverifikasi format sumber saat diperiksa, bukan menjamin jadwal tidak akan berubah atau semua berita mendadak tercakup.

## Paper trading dan Telegram

- Scope PAPER terlihat pada hasil, rencana dan alert. Likuidasi kosong ditandai SIMULASI; aturan manual tetap menuntut verifikasi likuidasi. Gate berita/teknis lain tidak dikecualikan.
- PENDING, trigger, fill virtual LONG/SHORT, quote di luar limit, kedalaman kurang, berita, expiry, perubahan likuiditas, SL sebelum fill, data lama, dan restart jurnal diuji.
- SL/gap, biaya entry/funding/exit, PnL neto, TP1/TP2, kuantitas sisa, BE, runner, dan tidak adanya exit/TP ganda tercakup. Sentuhan candle historis tidak dipakai untuk mengarang fill.
- Tidak ada trade duplikat per candle; satu posisi/pending per simbol, dua posisi/pending total, margin, total risiko, dan risiko grup altcoin diperiksa atomik.
- Dashboard dengan timestamp lama tidak dapat memundurkan hari risiko yang sudah dicatat worker atau menghapus patokan drawdown/rugi beruntun hari berjalan.
- Jeda data panjang membatalkan pending atau menutup posisi pada quote baru dengan label DATA_GAP_EXIT; tidak dibuat urutan harga selama offline.
- Telegram menguji label SCALPER/INTRADAY/SWING, WAIT/NO TRADE diam, AI salah/gagal, expiry/data lama, deduplikasi lintas instance, restart, 429/cooldown, penolakan, dan timeout tanpa retry duplikat.
- Retry pending memerlukan pemeriksaan ulang rencana dan tidak membuat trade kedua. Kegagalan Telegram tidak menghapus jurnal.
- Seluruh uji pengiriman menggunakan transport simulasi; tidak ada pesan test ke pengguna.

## Batas validasi

- Permintaan langsung ke endpoint waktu Binance dari lingkungan pengerjaan mengalami **ReadTimeout**. Pengambilan universe dan data pasar nyata belum berhasil diverifikasi dari lingkungan ini; perlu diverifikasi pada deployment pengguna.
- Key Groq serta token/chat ID Telegram berada di Secrets hosting pengguna dan tidak tersedia di workspace. Integrasi nyata Groq/Telegram belum diuji. Tes hanya membuktikan perilaku kode dengan respons simulasi.
- Streamlit AppTest dan startup HTTP tidak membuktikan cadence timer pada browser nyata. Pengujian browser headless belum dilakukan; unduhan Chromium pada revisi sebelumnya gagal.
- Worker belum dipasang atau dijalankan terus-menerus pada server pengguna. File lock diuji pada Linux; cabang Windows belum diuji langsung.
- Belum ada benchmark CPU pada Community Cloud, forward test, backtest historis, pengukuran expectancy/profit factor/drawdown strategi, atau pembuktian profitabilitas. Seluruh fixture algoritme bersifat sintetis dan tidak digunakan sebagai fallback pasar produksi.
- Paper fill menggunakan polling dan asumsi biaya. Likuidasi, funding aktual sepanjang horizon, antrean order, partial entry, serta urutan tick di antara polling tidak dimodelkan. Ledger tidak disinkronkan dengan akun bursa.
- SQLite memerlukan disk persisten agar bertahan dari reset/redeploy. Cadangan JSON adalah arsip audit; impor lewat GUI belum tersedia.
- Paket ini belum diterapkan ke deployment. Pembaruan tidak menghapus throttle CPU yang sudah berlaku. Timer Streamlit memerlukan sesi aktif; operasional tanpa browser memerlukan worker pada host yang terus hidup.

Status: **185 tes perangkat lunak dan startup HTTP lokal lolos**. Integrasi API nyata, browser timer, performa produksi, serta validasi hasil trading masih belum diverifikasi.

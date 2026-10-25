# Futures Desk v5.7 — Auto-scan + AI + Telegram

Penerapan `Master_Prompt_Crypto_Futures_Entry_SL_TP.txt` pada aplikasi Streamlit yang dilampirkan. UI disederhanakan menjadi Analisis, Evaluasi fill, dan Riwayat. Aplikasi menghasilkan rencana bersyarat dan tidak mengirim order ke exchange.

## Memperbarui aplikasi yang sudah online

Ekstrak ZIP ini dan ganti isi proyek dengan isi folder `secretagent-main`, terutama
`app.py`, seluruh folder `futures/`, `STRATEGY_RULES.txt`, dan `requirements.txt`.
Pada repo Streamlit, commit/push file hasil revisi, lalu pastikan main file tetap
`app.py`. Secrets yang sudah ada di pengaturan Streamlit tetap dipakai; jangan
menimpa atau mengunggah `.streamlit/secrets.toml` asli. Setelah versi **5.7**
tampil, pilih interval dan aktifkan **Auto-scan**. File ini belum otomatis
diterapkan ke aplikasi online Anda.

## Mulai

Gunakan Python 3.12 seperti lingkungan pengujian. Dependensi yang dipatok membutuhkan sekurangnya Python 3.11.

```bash
python -m venv .venv
```

Aktifkan `.venv` sesuai sistem: Windows `.venv\Scripts\activate`, macOS/Linux `source .venv/bin/activate`. Jalankan dari folder yang memuat `app.py`:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Buka URL lokal yang ditampilkan Streamlit. Untuk Streamlit Community Cloud, gunakan `app.py` sebagai main file dan isi Secrets melalui pengaturan aplikasi.

## Secrets Streamlit (tanpa input API di GUI)

Kredensial yang sudah Anda simpan di **App settings → Secrets** dibaca otomatis.
Tidak ada kolom API key, bot token, atau chat ID di GUI. Jangan mengunggah secrets
bersama kode. Nama yang didukung sama dengan versi sebelumnya:

```toml
GROQ_API_KEY = "isi_key_Anda"
GROQ_MODEL = "openai/gpt-oss-20b"
TELEGRAM_BOT_TOKEN = "isi_token_bot_Anda"
TELEGRAM_CHAT_ID = "isi_chat_id_tujuan"
```

Untuk beberapa key, gunakan `GROQ_API_KEYS = ["key1", "key2"]` atau
`GROQ_API_KEY_1` sampai `GROQ_API_KEY_7`. Maksimum tujuh key unik; pilih satu
format untuk menghindari konfigurasi ganda. Environment variable bernama sama
didukung untuk penggunaan lokal; `.env` tidak otomatis dibaca. Contoh lengkap
ada di `.streamlit/secrets.toml.example`. Bot harus sudah dapat mengirim ke chat
yang dituju (misalnya pengguna sudah memulai chat bot atau bot diberi izin grup).

Model diubah melalui `GROQ_MODEL` di Secrets dan harus mendukung structured
outputs strict. UI hanya menampilkan status konfigurasi, bukan bukti koneksi
berhasil. Key yang tidak tersedia/invalid atau jawaban AI yang gagal menghasilkan
WAIT untuk persetujuan entry. Rotasi tetap 1 → 2 → … → 7, maksimum tiga percobaan
per ulasan. HTTP 429 menjeda seluruh pool mengikuti `Retry-After`, tanpa memakai
key lain untuk melewati limit organisasi. Timeout/5xx memakai cooldown; 401/403
menonaktifkan slot sampai cache/server dimuat ulang.

## Auto-scan dan alert

1. Pilih **satu pair** dan **satu mode**: SCALPING, INTRADAY, atau SWING.
2. Pilih **30, 45, 60, atau 90 detik** (default 60 detik).
3. Aktifkan **Auto-scan**. Scan pertama segera berjalan; matikan toggle untuk berhenti.
4. **Alert Telegram BUY/SELL** aktif secara default; dapat dimatikan tanpa menghentikan analisis.
5. **Tampilkan grafik** mati secara default untuk menghemat render. Analisis dan alert tetap berjalan.

Setiap scan mengambil data futures, menjalankan mesin aturan, lalu meminta
ulasan Groq memakai **seluruh masterprompt asli `STRATEGY_RULES.txt`**.
AI menilai juga hasil WAIT/NO TRADE, tetapi tidak boleh meloloskan filter wajib
yang gagal. Data yang gagal diambil menghasilkan WAIT tanpa panggilan AI.
Persetujuan LONG/SHORT diperiksa ulang dengan harga/order book/candle berjalan
terbaru setelah respons AI sebelum dapat dikirim.

| Hasil akhir | Telegram |
| --- | --- |
| VALID + AI LONG sesuai baseline + data segar | BUY / LONG |
| VALID + AI SHORT sesuai baseline + data segar | SELL / SHORT |
| WAIT, NO TRADE, AI gagal/berlawanan, atau data kedaluwarsa | Tidak dikirim |

Label jenis trading mengikuti mode, bukan tebakan AI: **SCALPER** untuk
SCALPING (1H/5M), **INTRADAY** (4H/15M), **SWING** (1D/4H). Alert memuat trigger,
limit entry, SL, TP1/TP2, runner, kuantitas, leverage, risiko, waktu WIB, batas
berlaku, serta alasan AI. BUY/SELL adalah **rencana bersyarat menunggu trigger**;
aplikasi tidak memasang order.

Satu candle sinyal per pair/mode/arah hanya boleh mengirim satu alert ke tujuan
yang sama, walaupun tombol Analisis diklik ulang, kuantitas berubah, atau ada
beberapa tab. SQLite menyimpan reservasi pengiriman secara atomik. Telegram
429 dijadwalkan ulang sesuai `retry_after` pada scan berikutnya yang masih
valid; tidak ada loop retry/sleep. Timeout/hasil tidak pasti tidak otomatis
diulang untuk candle yang sama karena pesan mungkin sudah diterima Telegram.
Jika catatan deduplikasi tidak tersedia, pengiriman dihentikan. Catatan berlaku
selama database lokal masih ada; reboot/redeploy yang menghapus disk ephemeral
atau beberapa replica tanpa DB bersama dapat menghilangkan perlindungan ini.

**Batas otomatisasi:** timer berjalan saat sesi Streamlit aktif, bukan layanan
24/7 ketika tab ditutup, koneksi terputus, atau aplikasi tidur. API lambat,
cooldown, dan throttling dapat menunda jadwal; scan yang terlewat tidak ditumpuk.
Verifikasi akun/biaya/berita/likuidasi tetap wajib sesuai masterprompt. Harga
likuidasi terikat ID rencana dan berumur maksimum lima menit: auto-scan tidak
menciptakan atau memperpanjang konfirmasi manual itu. Tanpa verifikasi yang
valid, hasil tetap WAIT dan Telegram diam.

## Mengurangi CPU dan menangani throttling

Grafik, indikator, dan tabel tidak lagi dirender setiap lima detik. Timer kecil
lima detik hanya memperbarui status kedaluwarsa; scan berat mengikuti pilihan
30/45/60/90 detik. Metadata bursa dicache 5 menit, fundingInfo 60 detik, dan
candle tertutup sampai batas candle baru (maksimum TTL 5 menit). Last, Mark,
book, depth, dan candle berjalan selalu diminta ulang. Cache dibatasi 24 item;
riwayat analisis dibatasi 2.000 snapshot dan koneksi SQLite ditutup setelah
pemakaian. Pivot memakai array numerik, tanpa membuat ratusan sub-DataFrame.

Gunakan satu tab scanner dan interval 60/90 detik saat sumber daya terbatas.
Peringatan “Your app has been throttled” berasal dari pembatas CPU platform;
revisi kode tidak bisa membatalkan throttle yang sudah berlaku. Ikuti waktu
berakhir pada notifikasi. Batas platform tetap berlaku dan tidak ada jaminan
throttle tidak akan muncul lagi.

Aplikasi ditujukan untuk penggunaan pribadi/satu pengguna. Cache dan pembatas
scan berlaku per proses server; riwayat dan deduplikasi lokal tidak dirancang
sebagai dashboard publik multi-tenant.

## Alur penggunaan

1. Pilih simbol Binance USD-M USDT perpetual, misalnya `BTCUSDT`, dan mode.
2. Isi ekuitas, margin bebas, data awal hari UTC, posisi/pending, serta risiko yang sudah terbuka. Konfirmasikan data akun termasuk unrealized PnL.
3. Cocokkan fee taker dengan akun. Nilai awal form adalah contoh input yang belum terverifikasi. Tentukan cadangan slippage exit dan total funding selama horizon posisi. Cadangan funding tidak mengasumsikan semua kontrak berinterval delapan jam dan tidak mengurangi risiko berdasarkan funding yang mungkin diterima.
4. Periksa kalender CPI/FOMC/konferensi pers, masukkan sumber, waktu pemeriksaan, dan rentang tanpa event dalam UTC. Rentang harus mencakup 15 menit sebelum dan sesudah waktu analisis. Kalender perlu dicek ulang ketika jadwal berubah.
5. Klik **Analisis**. Mesin memeriksa indikator, struktur, biaya, risiko, dan likuiditas. Status yang mungkin: WAIT, NO TRADE, atau draf untuk verifikasi likuidasi.
6. Jika draf tersedia, gunakan Q, E, leverage, margin isolated, dan risk tier yang sama pada kalkulator/informasi bursa. Isi harga likuidasi, sumber/risk tier, serta waktu pemeriksaannya di panel verifikasi. Nilai ini merupakan konfirmasi manual pengguna, bukan hasil akses akun bursa oleh aplikasi.
7. Klik **Analisis** lagi atau tunggu auto-scan. Groq menilai hasil dengan masterprompt asli. Hanya setelah baseline dan AI sama-sama menyetujui, mesin memeriksa ulang data pasar. Perubahan rencana, harga yang terlanjur memicu entry, atau data lama membatalkan persetujuan dan alert.
8. Rencana berstatus VALID masih menunggu trigger. Snapshot lebih dari 30 detik berubah menjadi WAIT pada tampilan. Pemeriksaan status tampilan setiap lima detik tidak mengambil harga baru dan bukan pemantauan order. Auto-scan mengambil snapshot baru pada interval pilihan; tombol Analisis tetap dapat dipakai.

Groq dapat memilih arah yang sesuai baseline atau memveto menjadi WAIT/NO TRADE. Groq tidak dapat mengubah entry, SL, TP, Q, leverage, atau menghapus gate risiko. Tidak ada confidence/win probability yang dibuat tanpa pengujian.

## Aturan yang diterapkan

| Bagian prompt | Implementasi |
| --- | --- |
| Data | Futures bursa yang sama, minimum 500 candle tertutup pada masing-masing timeframe; kontinuitas, OHLCV, timestamp, dan candle terakhir diperiksa. Adapter meminta 1.000 candle. Tidak ada fallback spot. |
| Timeframe | SCALPING 1H/5M; INTRADAY 4H/15M; SWING 1D/4H. |
| Indikator | EMA close; RSI, ATR, ADX Wilder/RMA dengan seed eksplisit; VWAP hlc3 berbobot volume reset 00.00 UTC; volume relatif mengecualikan candle sinyal dari rata-rata 20 candle. |
| Bias dan pullback | EMA50/200, arah EMA50 lima bar, ADX ≥20; EMA20/50 eksekusi; lima bar sebelum sinyal; candle konfirmasi; RSI; volume; VWAP untuk scalping/intraday. |
| Entry | Stop-limit Last, satu tick di luar high/low sinyal, toleransi maksimum 0,10 ATR; E menggunakan limit terburuk. Trigger yang sudah tersentuh lalu memantul tetap WAIT. |
| SL | Lima candle termasuk sinyal ±0,25 ATR, pembulatan keluar, D 1–3 ATR. Riwayat ekstrem Last dan Mark candle berjalan memeriksa SL sebelum fill. |
| Hambatan | Pivot terkonfirmasi 2 kiri/2 kanan pada 100 candle bias dan eksekusi, serta high/low hari UTC sebelumnya. Ruang minimum 2D +0,10 ATR. Tidak ada level teridentifikasi tidak berarti tidak ada hambatan di luar data. |
| TP | TP1 2R; TP2 3R; pembagian 50/25/sisa dengan pembulatan lot valid; runner tidak memiliki TP tetap. Proporsi aktual ditampilkan. |
| Risiko | Maksimum 0,5% per rencana, 1% total, 0,5% kelompok altcoin searah; maksimum dua posisi/pending; isolated; leverage 1–3x; margin maksimum 20% dan cadangan biaya terpisah. |
| Ukuran posisi | Q = B/(D+cSL), kemudian dibatasi sisa risiko, margin, dan ukuran order. Step gabungan LOT_SIZE/MARKET_LOT_SIZE; pemeriksaan minQty/minNotional setiap bagian secara konservatif. |
| Biaya/pasar | Spread ≤0,05 ATR; biaya SL ≤20% D; Mark–Last ≤0,25 ATR; TR sinyal ≤2,5 ATR sebelumnya; RR bersih TP1 ≥1,5; kedalaman order book dalam toleransi mencukupi. |
| Likuidasi | Konfirmasi manual terkait ID rencana, ≤5 menit; SL harus mendahului likuidasi dengan buffer maksimum(ATR, deviasi Mark–Last + slippage exit). Perubahan Q/E/leverage membatalkan verifikasi. |
| Batas waktu | Tiga candle sesudah sinyal; scalping/intraday paling lambat 00.00 UTC. Jendela berita yang diperiksa dapat memperpendek masa berlaku. |
| Kerugian harian | Hentikan rencana baru pada drawdown ≥2% dari awal hari, disesuaikan arus dana dan equity termasuk unrealized PnL, atau tiga kerugian beruntun. Hari dimulai 00.00 UTC. |
| Setelah fill | Kalkulator memakai harga rata-rata dan jumlah terisi yang dikonfirmasi pengguna, mempertahankan SL struktural, menghitung ulang target/biaya/risiko, dan tidak menambah Q. Partial fill segera memerlukan SL untuk jumlah terisi. |
| TP/trailing | BE biaya setelah TP1 selesai; partial TP mengurangi kuantitas proteksi; trailing 2 ATR setelah TP2 hanya memakai candle lengkap setelah waktu fill TP2; SL hanya diperketat; level yang sudah dilewati Mark menghasilkan EXIT. |
| Time exit | Evaluasi manual 12 candle lengkap setelah fill tanpa +1R; scalping/intraday ditutup paling lambat akhir hari UTC. Pengguna dapat mengonfirmasi +1R pada bagian candle setelah fill yang tidak dapat disimpulkan dari OHLCV penuh. |
| Output | Status, sumber/waktu WIB, tabel kriteria, rencana bersyarat, risiko, tiga skenario PnL, ROI margin dan hasil terhadap ekuitas secara terpisah, maksimal lima alasan utama, serta JSON yang bisa diunduh. |

## Evaluasi fill dan batas otomatisasi

Tab **Evaluasi fill** terbuka setelah ada rencana yang lolos baseline dan Groq dalam sesi ini. Isikan harga rata-rata fill, jumlah terisi, waktu fill, partial TP yang sudah terisi, waktu TP selesai, SL terakhir, dan biaya aktual. Penghitungan tidak menganggap sentuhan OHLC sebagai fill.

Jumlah TP yang selesai harus sama dengan target yang dihitung ulang dari jumlah entry terisi. Untuk partial TP yang belum selesai, isi jumlahnya dan biarkan waktu TP selesai kosong. Isi SL terakhir agar trailing tidak melonggarkan proteksi dari evaluasi sebelumnya. Gunakan kembali kuantitas terbaru setelah setiap partial fill. Tidak ada sinkronisasi order/account langsung, bot trailing aktif, market order fallback, atau eksekusi Telegram. Verifikasi likuidasi setelah fill tetap perlu diperbarui di bursa.

Adapter hanya membaca endpoint publik. Mode margin, fee akun, posisi/pending, risk tier, likuidasi, kalender berita, dan kecukupan cadangan funding memerlukan konfirmasi manual. Order book adalah snapshot, bukan jaminan likuiditas saat trigger di masa depan. Semua filter perlu dicek ulang sebelum penempatan order dan selama order pending. SL stop-market tetap dapat mengalami slippage yang melebihi cadangan.

Scanner momentum, konsensus multi-agent, dan simulasi fill legacy tidak dipanggil. Auto-scan dan Telegram v5.7 memakai mesin aturan futures yang sama dengan analisis manual. Sumber awal tetap tersedia di `legacy/app_v5_5.py` untuk rujukan; entry point yang benar adalah `app.py`.

## Pengujian

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Lihat `VALIDATION.md` untuk cakupan dan keterbatasan pengujian. Data fixture sintetis hanya untuk pengujian perangkat lunak. Tidak ada backtest, forward test, atau bukti keuntungan yang diklaim. Indikator ML tambahan tidak diaktifkan. Pengujian strategi harus memisahkan pengembangan/validasi/test secara kronologis dan memasukkan fee, funding, slippage, partial fill, order expiry, liquidation, serta Mark historis sesuai dokumen aturan. Bila TP/SL tersentuh pada candle sama, gunakan data lebih kecil atau asumsi konservatif SL terlebih dahulu.

## Struktur

`app.py` adalah UI. `futures/strategy.py` memuat aturan dan risiko; `indicators.py` indikator; `market.py` data futures; `groq_rotation.py` AI dan key pool; `lifecycle.py` evaluasi fill/exit; `history.py` menyimpan snapshot SQLite; `scanner.py` menjadwalkan dan mengunci scan; `telegram.py` memfilter, memformat, serta mendeduplikasi alert. `STRATEGY_RULES.txt` memuat prompt asli tanpa perubahan.

## Dokumentasi resmi

- [Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment): timer ketika sesi aktif.
- [Streamlit resource limits](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app): throttling dan hibernation.
- [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage): sendMessage dan retry_after.
- [Binance USD-M market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data): kline, Mark kline, exchangeInfo, depth, fundingInfo, bookTicker, premiumIndex, ticker/price, server time.
- [Groq rate limits](https://console.groq.com/docs/rate-limits): batas organisasi, HTTP 429, dan Retry-After.
- [Groq structured outputs](https://console.groq.com/docs/structured-outputs): schema strict dan model yang didukung.
- [Groq API reference](https://console.groq.com/docs/api-reference): chat completions.

Pemeriksaan dokumentasi: 25 Oktober 2026. Ketersediaan model, skema API, serta akses bursa dapat berubah; error menghasilkan WAIT, bukan sinyal sintetis pengganti.

# Momentum Scanner — versi ringkas

Aplikasi satu halaman untuk memindai momentum **Binance USDT perpetual futures** dan mengirim alert Telegram. Aturan indikator mengikuti `ATURAN_TRADING.txt`, yaitu master prompt yang Anda lampirkan. Tidak memakai Groq, Gemini, LLM, atau API key bursa.

## Yang berubah

| Sebelumnya | Versi ini |
| --- | --- |
| Dashboard besar dengan banyak panel | Satu halaman: kontrol scan, kandidat momentum, status Telegram |
| Freqtrade, backtest, hyperopt, paper trade, AI consensus | Dihapus dari paket ini |
| Scanner lama memakai endpoint spot | Seluruh data harga dan candle memakai Binance USDⓈ-M futures |
| Sedikit pair pilihan | Daftar kontrak aktif dinamis, top 10–200 berdasarkan volume, atau isi simbol sendiri |
| Sinyal dengan aturan lama / keputusan AI | Perhitungan deterministik EMA, ADX, RSI, ATR, VWAP, volume |
| Notifikasi berpotensi berulang | Catatan pengiriman SQLite per tujuan + pair + mode + arah + candle |

Charts, laporan performa, database paper trade, integrasi eksekusi, unduhan data historis lama, dan konfigurasi Freqtrade tidak disertakan. Data pribadi/kredensial dari paket lama tidak dipindahkan. File lisensi asli dipertahankan.

## Cara memperbarui GitHub / Streamlit

1. Ekstrak ZIP ini. Buka folder **momentum-scanner**.
2. Ganti **isi repository aplikasi lama** dengan isi folder tersebut. Jangan hanya menimpa `app.py`: hapus folder lama `pages`, `src`, `services`, `ft_client`, `strategies`, `user_data`, dan file integrasi lama agar menu lama tidak muncul lagi. Simpan salinan repository lama jika masih memerlukan fiturnya.
3. Upload file dan folder dari dalam `momentum-scanner`, bukan ZIP utuh dan bukan folder induk tambahan.
4. Pada pengaturan deploy Streamlit, isi **Main file path: `streamlit_app.py`**. Alias `app.py` juga tersedia untuk deploy lama.
5. Pilih Python **3.11 atau 3.12**, kemudian reboot/redeploy agar dependensi diperbarui.
6. Buka aplikasi, pilih **Intraday**, tekan **Scan sekarang**.

Paket ini jauh di bawah 100 file. Tidak ada ZIP lain di dalamnya. Tidak perlu Docker atau Freqtrade untuk aplikasi ini.

### Telegram

Buat bot melalui BotFather, lalu kirim `/start` ke bot tersebut. Untuk grup, tambahkan bot dan pastikan bot boleh mengirim pesan. Masukkan token dan Chat ID yang benar di sidebar **Koneksi Telegram**, atau di Streamlit Cloud **App settings → Secrets**:

```toml
TELEGRAM_BOT_TOKEN = "TOKEN_BOT_ANDA"
TELEGRAM_CHAT_ID = "CHAT_ID_ANDA"
```

`TG_TOKEN` dan `TG_CHAT` dari konfigurasi lama juga dikenali. Gunakan **Kirim pesan tes**, kemudian aktifkan **Alert Telegram**. Untuk scan berulang, aktifkan **Scan otomatis**. Token asli jangan dimasukkan ke repository atau `secrets.example.toml`.

Alert otomatis berisi **kandidat teknis LONG/SHORT dengan status WAIT**, waktu WIB, RSI, ADX, dan volume relatif. Aplikasi tidak mengarang fee akun, saldo, ukuran posisi, harga likuidasi, atau kalender ekonomi. Karena master prompt mewajibkan data tersebut, kandidat teknis tidak langsung disebut rencana siap entry.

Untuk memperoleh **entry, SL, TP dan ukuran posisi**, pilih kandidat → buka **Hitung rencana entry, SL, dan TP** → isi data aktual → tekan **Perbarui harga & verifikasi rencana**. Perhitungan kuantitas awal dapat diperiksa saat masih WAIT. Cocokkan kuantitas tersebut dengan likuiditas dan kalkulator likuidasi bursa, lalu lengkapi verifikasi. Jika semua pemeriksaan lolos, tabel harga muncul dan rencana dapat dikirim ke Telegram melalui tombol khusus. Input risiko hanya disimpan di sesi aplikasi.

Jika tombol verifikasi melaporkan candle berubah atau trigger sudah dilewati, scan ulang. Jangan mengejar sinyal lama. Rencana yang lebih tua dari 30 detik perlu diverifikasi lagi sebelum dipakai/dikirim.

### Menjalankan di Windows

Buka PowerShell di folder hasil ekstrak, lalu:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Buka alamat lokal yang ditampilkan terminal. Untuk konfigurasi tetap, salin `secrets.example.toml` menjadi `.streamlit/secrets.toml`, lalu isi di komputer Anda.

### Pemantauan tanpa browser

Scan otomatis di halaman Streamlit berjalan selama sesi terhubung dan proses aplikasi aktif. Ini bukan layanan yang dijamin hidup 24 jam. Untuk Telegram tanpa membuka browser, jalankan worker pada komputer/VPS yang tetap aktif:

```powershell
.\.venv\Scripts\python.exe worker.py --mode INTRADAY --pairs 30 --interval 60 --telegram
```

Worker membaca `.streamlit/secrets.toml` atau environment `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`. Jalankan dari folder proyek. Tekan Ctrl+C untuk berhenti. Contoh pair tertentu:

```powershell
.\.venv\Scripts\python.exe worker.py --mode SCALPING --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT --telegram
```

Untuk memeriksa satu putaran tanpa mengirim pesan:

```powershell
.\.venv\Scripts\python.exe worker.py --once --pairs 10
```

UI dan worker berbagi pencegahan duplikat jika menggunakan `ALERT_DB_PATH` yang sama. File `.runtime/alerts.sqlite3` harus berada pada penyimpanan persisten jika ingin riwayat deduplikasi bertahan setelah redeploy. SQLite bukan koordinasi antar-server yang memakai disk berbeda. Gangguan jaringan setelah Telegram menerima pesan dapat membuat pengiriman tidak terkonfirmasi; retry pada kasus ambigu masih mungkin menghasilkan duplikat. HTTP 429 dihormati melalui `retry_after`.

## Aturan yang diterapkan

| Komponen | Implementasi |
| --- | --- |
| Timeframe | Scalping 1H/5M; intraday 4H/15M; swing 1D/4H |
| Data | Minimal 500 candle tertutup, urut, tanpa lubang; candle terakhir selesai wajib tersedia; candle berjalan tidak dihitung sebagai sinyal |
| Tren | Close > EMA50 > EMA200 + EMA50 naik vs 5 candle sebelumnya + ADX14 ≥20 untuk LONG; simetris untuk SHORT |
| Pullback | Rentang low–high minimal satu dari 5 candle sebelum sinyal beririsan dengan EMA20 ±0,25 ATR milik candle itu; close tidak merusak EMA50 |
| Konfirmasi | Candle bullish/bearish, close melewati high/low sebelumnya dan EMA20; EMA20/50 searah |
| RSI | LONG 50–65, SHORT 35–50, batas inklusif |
| Volume | Candle sinyal / rata-rata 20 candle sebelum sinyal ≥1,2 |
| Smoothing | EMA sumber close dengan seed SMA; RSI/ATR/ADX memakai Wilder RMA14; ADX DI14/smoothing14 |
| VWAP | HLC3 berbobot volume; reset 00.00 UTC (07.00 WIB); wajib untuk scalping/intraday, bukan swing |
| Lonjakan harga | TR sinyal ≤2,5 ATR candle sebelumnya |
| Entry | Trigger high +1 tick LONG / low −1 tick SHORT; limit maksimal 0,10 ATR dari trigger, dibulatkan ke dalam toleransi |
| SL | Ekstrem 5 candle termasuk sinyal ±0,25 ATR; dibulatkan keluar struktur; jarak 1–3 ATR dari limit entry |
| Hambatan | Pivot 2 kiri/2 kanan yang sudah terkonfirmasi, pencarian 100 candle bias dan eksekusi, high/low hari UTC sebelumnya |
| Ruang target | Minimal 2D +0,10 ATR ke hambatan terdekat; jika tak ditemukan, dicatat sebagai keterbatasan cakupan |
| Target | TP1 2R sekitar 50%; TP2 3R sekitar 25%; sisa runner; jumlah aktual mengikuti lot step dan minimum order |
| Harga publik | Bid/ask, Mark, Last maksimal berumur 30 detik; spread ≤0,05 ATR; Mark–Last ≤0,25 ATR |
| Anti-chase | WAIT jika Last atau ekstrem candle berjalan sudah menyentuh trigger atau SL |
| Kedaluwarsa | Tiga candle setelah sinyal; scalping/intraday dipotong di batas sesi UTC asal sinyal |
| Akun | Risiko ≤0,5%; leverage maksimal 3x; margin + cadangan biaya ≤20% ekuitas; maksimal 2 posisi/pending; risiko gabungan ≤ 1% |
| Altcoin | Semua kontrak selain BTC dianggap kelompok altcoin searah secara konservatif; risiko gabungan ≤ 0,5% |
| Biaya | Fee taker entry+exit, slippage exit, cadangan funding; cSL ≤ 20% D; RR bersih pembanding TP1 ≥ 1,5; tidak menggandakan slippage entry |
| Likuidasi | Wajib input terverifikasi dari bursa; buffer SL–likuidasi ≥ maksimum(ATR, deviasi Mark–Last+slippage exit) |
| Harian | Tolak rencana jika drawdown ≥ 2% atau tiga rugi beruntun pada hari UTC yang sama |
| Berita | Verifikasi manual CPI/FOMC/konferensi pers ±15 menit; tidak mengklaim kalender terhubung |

Ketika hitungan Q diperkecil karena margin, risiko riil dapat lebih kecil dari anggaran. Funding reserve adalah estimasi total biaya yang mungkin dibayar sepanjang posisi, bukan otomatis memakai interval 8 jam atau menambahkan funding yang mungkin diterima. Harga funding publik hanya ditampilkan sebagai informasi.

Semua harga merupakan rencana berbasis snapshot. STOP-LIMIT entry memakai Last; SL direncanakan stop-market dengan trigger Mark, sesuai kemampuan bursa saat order dibuat. Aplikasi tidak memasang order, mengaktifkan trailing di bursa, atau memverifikasi likuidasi melalui API akun. Verifikasi manual harus sesuai kuantitas akhir, isolated, leverage, maintenance margin/risk tier, dan kondisi akun aktual.

## Setelah entry benar-benar terisi

Ini adalah **aturan pengelolaan** dari master prompt, bukan fitur eksekusi otomatis aplikasi:

- Lindungi setiap partial fill dengan SL dan sesuaikan kuantitas exit; gunakan reduce-only/close-position yang didukung bursa.
- Hitung ulang D, target, biaya dan risiko memakai rata-rata fill aktual; jangan menambah kuantitas untuk menghabiskan anggaran.
- Sesudah TP1 terisi, geser stop sisa ke estimasi BE termasuk biaya. Perhitungan aplikasi memakai biaya per unit sisa; laba TP1 tidak dipakai untuk melonggarkan stop.
- Sesudah TP2 terisi, trailing runner 2 ATR dari ekstrem candle yang selesai seluruhnya setelah TP2. Sebelum ada candle lengkap, pertahankan SL. Stop hanya diperketat; jika level baru sudah dilewati harga pemicu, evaluasi EXIT.
- Jika setelah 12 candle sejak fill belum pernah mencapai +1R, lakukan time-exit sesuai rencana. Scalping/intraday tutup sisa posisi sebelum 00.00 UTC berikutnya.
- Batalkan sisa pending order saat kedaluwarsa, SL tercapai sebelum fill, bias berubah, atau biaya/likuiditas tidak lolos. Stop-limit yang tidak terisi tidak otomatis diubah menjadi market order.

Scanner mengevaluasi candle sinyal **terakhir** pada setiap scan. Ia tidak menyimpan siklus hidup order tiga candle sebelumnya, tidak mengetahui fill sebenarnya, dan tidak mengirim notifikasi TP/SL tersentuh. Masa berlaku dalam alert adalah batas rencana bagi pengguna/bot eksekusi. Kalender dan data risiko perlu diperiksa lagi jika trigger baru tercapai kemudian.

## Jika hasil kosong atau ada error

- **NO TRADE:** lihat bagian Alasan pair belum lolos. Satu aturan wajib gagal sudah cukup untuk menolak kandidat.
- **WAIT 500 candle:** kontrak baru atau data tidak cukup; tidak dibuat indikator dari history pendek.
- **HTTP 451/403:** endpoint futures dibatasi oleh bursa untuk lingkungan hosting tersebut. Pakai lingkungan yang memang diizinkan bursa. Aplikasi tidak beralih diam-diam ke harga spot.
- **Rate limit:** tunggu sesuai pesan. Candle tertutup di-cache; pemindaian besar dapat dilanjutkan pada putaran berikutnya. Beberapa sesi aplikasi sekaligus memakai IP hosting yang sama juga memengaruhi batas bursa.
- **Telegram gagal:** cek token, Chat ID, `/start`, izin grup, serta status pengiriman. Kegagalan tidak dicatat sebagai sukses; percobaan berikutnya hanya dilakukan jika setup masih menjadi kandidat.
- **Tidak ada alert setelah browser ditutup:** gunakan worker pada proses/server yang terus hidup.

## Pengujian

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

Pengujian memakai data sintetis dan HTTP mock. Cakupannya meliputi indikator, bias/pullback LONG dan SHORT, candle tertutup, pivot, VWAP UTC, pembulatan, filter harga, risiko, Telegram, dan UI Streamlit. Lihat `HASIL_PENGUJIAN.md` untuk hasil pengerjaan.

Pengujian kode ini **bukan backtest profitabilitas**. Belum ada klaim win rate, profit factor, atau profit maksimal. Sebelum live, lakukan pengujian kronologis out-of-sample serta forward/paper test dengan fee, funding, slippage, partial fill, kedaluwarsa, dan data Mark Price yang sesuai. Modul backtest sengaja tidak menjadi bagian aplikasi ringkas ini.

## Referensi implementasi

- Aturan indikator dan risiko: master prompt pengguna, disalin utuh ke `ATURAN_TRADING.txt`.
- [Binance USDⓈ-M Futures market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data): klines, exchangeInfo, premiumIndex, bookTicker, ticker price.
- [Streamlit st.fragment](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment): pembaruan halaman selama sesi aktif.
- [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage): sendMessage dan retry_after.

Kode ini merupakan penyederhanaan dari arsip aplikasi yang Anda berikan. Lisensi sumber asli ada di `LICENSE`.

# Futures Desk v5.6

Penerapan `Master_Prompt_Crypto_Futures_Entry_SL_TP.txt` pada aplikasi Streamlit yang dilampirkan. UI disederhanakan menjadi Analisis, Evaluasi fill, dan Riwayat. Aplikasi menghasilkan rencana bersyarat dan tidak mengirim order ke exchange.

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

## Tujuh API key Groq

Pilih salah satu:

1. Buka Pengaturan → Groq di sidebar. Isi sampai tujuh kolom password, lalu klik **Gunakan key ini**. Key berada di memori server dan tidak masuk JSON analisis atau database. **Lepas key sesi** menghapus input sesi dan cache pool; key yang berasal dari environment/Secrets tetap mengikuti konfigurasi server.
2. Salin `.streamlit/secrets.toml.example` menjadi `.streamlit/secrets.toml`, lalu isi `GROQ_API_KEY_1` sampai `GROQ_API_KEY_7`.
3. Gunakan environment variable dengan nama yang sama. `GROQ_API_KEYS` juga menerima daftar dipisahkan koma atau baris baru. File `.env` tidak otomatis dibaca.

Model awal: `openai/gpt-oss-20b`, dengan structured JSON schema. Model dapat diubah pada sidebar. Pilih model Groq yang mendukung schema strict. Model tidak tersedia atau output tidak valid menghasilkan WAIT.

Rotasi berlaku per permintaan: slot 1 → 2 → ... → 7 → 1. Satu analisis menggunakan satu respons yang valid, bukan tujuh voting. Key duplikat dihapus. Key dengan HTTP 401/403 dinonaktifkan sampai pool dimuat ulang; timeout/5xx memakai cooldown dan failover, maksimum tiga percobaan per permintaan. HTTP 429 menjeda seluruh pool berdasarkan `Retry-After`. Pergantian key tidak menambah kuota organisasi. HTTP 400 tidak dicoba ke semua key.

Jalankan sebagai aplikasi pribadi/satu pengguna. Pool dan batas permintaan berlaku pada satu proses server; beberapa replica memerlukan pengelola kuota terpusat. Riwayat SQLite lokal tidak memisahkan akun pengguna dan tidak dirancang sebagai dashboard publik multi-tenant.

## Alur penggunaan

1. Pilih simbol Binance USD-M USDT perpetual, misalnya `BTCUSDT`, dan mode.
2. Isi ekuitas, margin bebas, data awal hari UTC, posisi/pending, serta risiko yang sudah terbuka. Konfirmasikan data akun termasuk unrealized PnL.
3. Cocokkan fee taker dengan akun. Nilai awal form adalah contoh input yang belum terverifikasi. Tentukan cadangan slippage exit dan total funding selama horizon posisi. Cadangan funding tidak mengasumsikan semua kontrak berinterval delapan jam dan tidak mengurangi risiko berdasarkan funding yang mungkin diterima.
4. Periksa kalender CPI/FOMC/konferensi pers, masukkan sumber, waktu pemeriksaan, dan rentang tanpa event dalam UTC. Rentang harus mencakup 15 menit sebelum dan sesudah waktu analisis. Kalender perlu dicek ulang ketika jadwal berubah.
5. Klik **Analisis**. Mesin memeriksa indikator, struktur, biaya, risiko, dan likuiditas. Status yang mungkin: WAIT, NO TRADE, atau draf untuk verifikasi likuidasi.
6. Jika draf tersedia, gunakan Q, E, leverage, margin isolated, dan risk tier yang sama pada kalkulator/informasi bursa. Isi harga likuidasi, sumber/risk tier, serta waktu pemeriksaannya di panel verifikasi. Nilai ini merupakan konfirmasi manual pengguna, bukan hasil akses akun bursa oleh aplikasi.
7. Klik **Analisis** lagi. Hanya setelah seluruh gate lolos, Groq menilai rencana. Mesin memeriksa ulang data pasar setelah respons AI. Perubahan rencana, harga yang terlanjur memicu entry, atau data lama membatalkan persetujuan.
8. Rencana berstatus VALID masih menunggu trigger. Snapshot lebih dari 30 detik berubah menjadi WAIT pada tampilan. Pemeriksaan status tampilan setiap lima detik tidak mengambil harga baru dan bukan pemantauan order. Klik Analisis untuk snapshot baru.

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

Scanner momentum, konsensus multi-agent, pemantauan otomatis, Telegram, dan simulasi fill lama tidak dipanggil oleh aplikasi baru. Sumber awal tetap tersedia di `legacy/app_v5_5.py` untuk rujukan, dengan requirements awalnya. UI baru berfokus pada satu pair dan satu mode agar hasil tidak tercampur dengan strategi lama berbasis spot.

## Pengujian

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Lihat `VALIDATION.md` untuk cakupan dan keterbatasan pengujian. Data fixture sintetis hanya untuk pengujian perangkat lunak. Tidak ada backtest, forward test, atau bukti keuntungan yang diklaim. Indikator ML tambahan tidak diaktifkan. Pengujian strategi harus memisahkan pengembangan/validasi/test secara kronologis dan memasukkan fee, funding, slippage, partial fill, order expiry, liquidation, serta Mark historis sesuai dokumen aturan. Bila TP/SL tersentuh pada candle sama, gunakan data lebih kecil atau asumsi konservatif SL terlebih dahulu.

## Struktur

`app.py` adalah UI. `futures/strategy.py` memuat aturan dan risiko; `indicators.py` indikator; `market.py` data futures; `groq_rotation.py` AI dan key pool; `lifecycle.py` evaluasi fill/exit; `history.py` menyimpan snapshot SQLite. `STRATEGY_RULES.txt` memuat prompt asli tanpa perubahan.

## Dokumentasi resmi yang diperiksa

- [Binance USD-M market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data): kline, Mark kline, exchangeInfo, depth, fundingInfo, bookTicker, premiumIndex, ticker/price, server time.
- [Groq rate limits](https://console.groq.com/docs/rate-limits): batas organisasi, HTTP 429, dan Retry-After.
- [Groq structured outputs](https://console.groq.com/docs/structured-outputs): schema strict dan model yang didukung.
- [Groq API reference](https://console.groq.com/docs/api-reference): chat completions.

Pemeriksaan dokumentasi: 25 Oktober 2026. Ketersediaan model, skema API, serta akses bursa dapat berubah; error menghasilkan WAIT, bukan sinyal sintetis pengganti.

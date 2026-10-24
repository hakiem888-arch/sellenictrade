# Hasil verifikasi versi ringkas

**55 pengujian lulus** menggunakan `python -m pytest -q`.

Lingkungan: Python 3.12, Streamlit 1.66.1, pandas 2.2.3, NumPy 2.3.5.

| Area | Cakupan |
| --- | --- |
| Indikator | Seed/perulangan Wilder RMA, volume relatif tanpa candle sinyal pada pembagi, reset VWAP UTC |
| Sinyal | Fixture OHLCV sintetis LONG dan SHORT melalui seluruh perhitungan indikator dan filter |
| Integritas data | Candle berjalan, kurang 500 candle, gap, duplikat, keterlambatan candle, NaN, OHLC tidak valid |
| Struktur | Pivot menunggu dua candle kanan selesai, hambatan dekat menolak setup |
| Harga | Pembulatan tick, limit tidak melewati toleransi ATR, SL struktural, target 2R/3R |
| Filter live | Snapshot tua, spread lebar, Mark–Last menyimpang, trigger/SL tersentuh, trigger sudah tersentuh intrabar |
| Sesi | Kedaluwarsa tengah malam UTC; swing tidak mewajibkan VWAP |
| Risiko | Batas risiko dan margin, biaya, kuantitas partial, minimum order, RR, likuidasi, drawdown, rugi beruntun, posisi aktif, risiko altcoin, data belum diverifikasi |
| Pengelolaan | Perhitungan skenario PnL dan fungsi trailing yang hanya memperketat stop |
| Telegram | HTTP mock: sukses, duplikat setelah inisialisasi ulang, pemisahan tujuan, gagal lalu retry, rate limit, token tidak muncul dalam pesan error |
| Bursa | HTTP mock rate limit dan pemilihan daftar futures dinamis; tidak ada fallback spot |
| Tampilan | Streamlit AppTest: layar awal, hasil kosong, kandidat, error API, perubahan mode, form rencana valid, snapshot kedaluwarsa, alias app.py |

## Batas verifikasi

- Percobaan mengakses API Binance Futures langsung mendapat **HTTP 451** dari lingkungan pengujian. Pengambilan data pasar live end-to-end belum berhasil diverifikasi. Kode mempertahankan status WAIT/error dan tidak mengganti data futures dengan spot.
- Pengiriman ke Telegram diuji dengan respons simulasi; **tidak ada pesan nyata yang dikirim** dan tidak ada kredensial pengguna yang digunakan. Uji dengan tombol Kirim pesan tes setelah token dan Chat ID Anda diisi.
- Pengujian tampilan dilakukan melalui Streamlit AppTest, bukan screenshot browser penuh.
- Tidak dilakukan backtest profitabilitas, paper/forward trading, atau eksekusi order bursa. Hasil pengujian kode tidak membuktikan strategi menguntungkan.

Paket distribusi tidak menyertakan runtime database, cache Python, token, data pasar lama, atau dependensi terpasang. Master prompt dan lisensi sumber disertakan.

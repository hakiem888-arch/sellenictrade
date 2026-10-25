# Catatan validasi v5.6

Tanggal laporan: 25 Oktober 2026.

## Hasil

- `python -m pytest tests -q`: **95 passed**.
- Aplikasi membuka halaman awal melalui Streamlit AppTest tanpa exception.
- Alur UI lengkap diuji: pengaturan akun → WAIT/draf → verifikasi likuidasi yang terkait ID rencana → persetujuan Groq simulasi → pemeriksaan ulang data → VALID.
- Skenario harga menyentuh trigger selama penilaian AI menghasilkan WAIT setelah pemeriksaan ulang.
- Perubahan ekuitas/pengaturan menyembunyikan hasil lama sampai analisis ulang.
- Uji startup HTTP lokal: `/_stcore/health` menghasilkan 200 `ok`; halaman `/` menghasilkan HTTP 200.
- Pengujian UI bersifat fungsional. Screenshot visual dengan browser headless tidak selesai karena unduhan browser gagal di lingkungan ini.

## Cakupan

1. Seed RMA dan perhitungan Wilder, batas RSI 0/50/100, volume relatif yang mengecualikan sinyal, VWAP UTC, pivot yang baru tersedia setelah dua candle kanan, dan tidak berubahnya indikator historis oleh data masa depan.
2. Setup LONG dan SHORT lengkap dari OHLCV sintetis tanpa mengganti mesin indikator; semua mode timeframe; pembulatan tick/lot; TP 2R/3R; konservasi jumlah partial TP; batas margin dan risiko.
3. Candle tidak cukup, candle belum tutup, candle terakhir hilang, celah waktu, NaN, snapshot lewat 30 detik, timestamp masa depan, serta batas tepat 30 detik.
4. Gate biaya, kalender berita, hari akun, isolated, leverage, maksimum posisi/pending, rugi harian, penyesuaian arus dana, risiko grup altcoin, kedalaman order book, min notional partial TP, dan kontrak yang tidak didukung.
5. Trigger yang sudah tersentuh kemudian memantul, SL Last/Mark yang sudah tersentuh sebelum fill, resistance terlalu dekat, masa berlaku tiga candle, pemendekan jendela berita, dan pembatalan akhir hari UTC.
6. Likuidasi terikat pada ID rencana; perubahan leverage membatalkan verifikasi; buffer yang tidak cukup memveto rencana.
7. Round-robin tujuh key dan wrap-around, duplikat, failover autentikasi, cooldown timeout/5xx, maksimum tiga percobaan, HTTP 429 organisasi tanpa menghabiskan key lain, dan Retry-After dalam angka/tanggal.
8. Respons JSON malformed, field harga yang tidak diizinkan, NaN, keputusan asing, respons terpotong, redaksi key, arah AI yang berlawanan, dan ketidakmampuan AI meloloskan gate yang gagal.
9. Perhitungan fill aktual dan partial entry, larangan fill di luar limit/masa berlaku, proteksi sesuai jumlah terisi, partial TP tanpa BE prematur, BE bersih biaya, trailing LONG/SHORT yang tidak melonggar, pengecualian candle yang belum lengkap setelah TP2, EXIT ketika Mark melewati SL, dan time-exit 12 candle.
10. Adapter Binance menggunakan endpoint futures; tidak beralih ke spot pada HTTP 451; HTTP 429 menghentikan panggilan berikutnya; interval funding yang tidak diketahui tidak diasumsikan delapan jam.

## Batas validasi

- Pengujian HTTP langsung ke Binance dari lingkungan pengerjaan mengembalikan **451**. Integrasi adapter diuji dengan respons HTTP simulasi yang mengikuti struktur dokumentasi resmi. Konektivitas dan kompatibilitas respons bursa langsung perlu diverifikasi di lingkungan pengguna.
- Tidak ada key Groq milik pengguna yang tersedia dalam lampiran. Rotasi, schema, failover, dan logika keputusan diuji menggunakan transport simulasi. Panggilan Groq nyata belum diuji.
- Data untuk pengujian algoritme berlabel sintetis. Tidak ada backtest historis, forward test, paper fill otomatis, pengukuran expectancy, profit factor, drawdown strategi, atau klaim win rate.
- Aplikasi baru tidak mengimpor atau menjalankan modul legacy. Aplikasi menyimpan riwayat analisis, bukan buku posisi yang tersinkronisasi dengan exchange.
- Konfirmasi akun, fee, kalender, fitur order, funding horizon, dan likuidasi berasal dari pengguna. Pengujian memastikan gate tidak lolos ketika konfirmasi/data wajib hilang, bukan membuktikan kebenaran input manual.
- Kalender perlu diperiksa ulang ketika jadwal berubah. Depth/mark/last adalah snapshot, bukan jaminan kondisi pada saat stop-limit terpicu nanti.

Status keseluruhan: implementasi dan pengujian perangkat lunak selesai; validasi strategi dan integrasi akun nyata belum dilakukan.

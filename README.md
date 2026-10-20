# SecretAgent v5.5: Manual Position Tracker

Implementasi ini melanjutkan **file v5.5 yang diunggah**. Mesin momentum, ambang sinyal, prompt AI, pengambilan keputusan, risk gate, dan modul paper trading asli tetap utuh. Fitur baru hanya mencatat posisi yang pengguna laporkan dan memberikan usulan stop. Tidak ada fungsi untuk membuat atau mengubah order exchange.

Arsip sumber awal hanya berisi `app.py` dan `requirements.txt`. `app.py` memuat 13 modul terkompresi. Sumber tersebut sudah memiliki beberapa label internal v5.6/v6 dan schema legacy versi 6. Label dan modul bawaan itu dipertahankan; proyek ini tidak dimigrasikan ke basis kode lain. Lihat `docs/AUDIT.md`.

## 1. Setup

Gunakan Python 3.12, versi yang digunakan untuk pengujian ini. Ekstrak proyek ke direktori aplikasi Anda. Pertahankan file database dan konfigurasi asli. Arsip ini tidak menyertakan database pengguna, kredensial, atau transaksi contoh sebagai data aplikasi.

```bash
cd secretagent-main
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.env .env
```

Edit `.env`. Isi token bot, **ID numerik** user dan chat yang diizinkan, serta kunci panel privat. Gunakan bot khusus untuk aplikasi ini. Untuk beberapa ID, gunakan koma. Pada grup, isi ID grup di allowlist chat dan ID masing-masing pengguna di allowlist user. Keanggotaan grup saja tidak memberikan izin.

Atur `SECRETAGENT_DB_FILE` ke path absolut **database v5.5 yang sudah ada**. `MANUAL_DB_FILE` dan argumen `--db` harus menunjuk file yang sama. Jika aplikasi lama berjalan dari direktori lain, cari file `trading_v5.db` di direktori kerja aplikasi lama; jangan membuat database kosong tanpa sengaja.

Muat konfigurasi pada **setiap terminal/proses**:

```bash
set -a
source ./.env
set +a
python -m manual_tracker --check
python -m manual_tracker --migrate --db "$SECRETAGENT_DB_FILE"
```

`--check` hanya memvalidasi konfigurasi lokal. Perintah ini tidak membuktikan bot atau exchange dapat diakses. `--migrate` membuat backup SQLite menggunakan API backup sebelum menambahkan tabel `mt_*`, jika database sudah ada. Jalankan migrasi dengan worker dihentikan dan akses aplikasi dijeda. Jangan gunakan tombol reset database untuk migrasi.

Terminal pertama, worker manual yang harus terus berjalan:

```bash
source .venv/bin/activate
set -a
source ./.env
set +a
python -m manual_tracker --worker --db "$SECRETAGENT_DB_FILE"
```

Terminal kedua, UI asli:

```bash
source .venv/bin/activate
set -a
source ./.env
set +a
streamlit run app.py
```

Jika sebelumnya memakai worker scan asli, perintah aslinya tetap tersedia, misalnya:

```bash
python app.py --worker --db-file "$SECRETAGENT_DB_FILE" --telegram
```

Worker scan menghasilkan sinyal dengan kebijakan v5.5 asli. Worker manual menerima Telegram, memantau posisi manual, dan mengirim antrean pesan. Worker manual tidak memerlukan Groq. Jangan menyalakan worker scan dan auto-scan UI sekaligus jika instalasi lama Anda memang hanya menggunakan salah satunya.

Konfigurasi lewat `st.secrets` masih berlaku untuk UI. Worker manual membaca **environment variables**, bukan `st.secrets`. Pastikan nilainya sama pada kedua proses. Pada Windows gunakan aktivasi virtualenv dan pengisian environment yang setara; pengujian penguncian worker dalam paket ini dilakukan di Linux.

## 2. Alur pengguna

1. Aktifkan pengiriman Telegram pada UI/worker scan. Sinyal LONG/SHORT baru berisi Signal ID, waktu, timeframe, level referensi, serta tombol **Saya Sudah Entry** dan **Abaikan Sinyal**.
2. **Abaikan Sinyal** menyimpan `IGNORED`. Sinyal tersebut tidak dapat menjadi tracker melalui tombol lamanya. Lihat `/history` atau panel riwayat.
3. **Saya Sudah Entry** memulai formulir. Balas langsung setiap pertanyaan bot: instrumen, harga entry aktual, kuantitas, SL awal, multiplier/leverage jika relevan, dan estimasi fee entry/exit.
4. Gunakan titik desimal. Fee memakai fraksi: `0.0004` berarti `0.04%`. `0` adalah konfirmasi tanpa fee, bukan nilai default. Leverage `-` menonaktifkan estimasi ROI futures.
5. Periksa ringkasan, lalu tekan **Konfirmasi**. Worker harus memperoleh metadata instrumen yang valid. Jika metadata belum tersedia, konfirmasi ditolak tanpa membuat posisi; tunggu dan tekan konfirmasi lagi. SL harus sesuai tick exchange.
6. Setelah ACTIVE, periksa rekomendasi dan ubah SL **secara manual** di exchange. Tracker tidak mengetahui apakah usulan SL sudah Anda pasang. PnL, ROI, dan break-even adalah estimasi.
7. Gunakan `/positions` untuk membuka tombol terbaru. **Pause Tracking** berhenti memantau. **Lanjutkan Tracking** memerlukan tindakan eksplisit. **Posisi Sudah Ditutup** meminta harga exit opsional dan konfirmasi akhir.
8. `/cancel` membatalkan formulir. Jika Anda membatalkan atau membiarkan konfirmasi penutupan kedaluwarsa, posisi menjadi **PAUSED**, bukan otomatis ACTIVE.

Hanya pengguna pertama yang sah dan mengklaim sinyal dapat melanjutkan formulir serta mengelola posisi itu. Satu sinyal mempunyai maksimal satu posisi, termasuk setelah CLOSED. Untuk mengoreksi formulir sebelum konfirmasi, gunakan `/cancel` lalu mulai lagi. Tidak ada fitur averaging, partial close, perubahan kuantitas, atau edit fill setelah posisi terdaftar pada versi ini.

Panel **Posisi manual & trailing stop** berada di bawah alur momentum asli. Masukkan `MANUAL_DASHBOARD_TOKEN` untuk melihat posisi. Tombol pause/close di panel mengantrekan permintaan ke Telegram; hanya pemilik posisi yang sah dapat mengonfirmasinya. Panel tidak mengubah status posisi secara langsung. Lindungi akses aplikasi dan file SQLite sesuai pengaturan deployment Anda.

## 3. Instrumen, harga, dan biaya

| Instrumen | Dukungan | Sumber harga | Kuantitas |
| --- | --- | --- | --- |
| Binance spot, pasangan USDT | LONG tanpa pinjaman | Last trade bertimestamp | Unit aset dasar yang aktual dimiliki |
| Binance USDT perpetual linear | LONG dan SHORT | Mark price bertimestamp | Unit aset dasar, multiplier 1 |
| Inverse/COIN-M, opsi, dated futures, spot margin, venue lain | Ditolak | Tidak tersedia | Tidak dihitung |

Fungsi akuntansi linear memperhitungkan multiplier dan mempunyai pengujian multiplier selain 1. Adapter Binance yang disediakan **membatasi multiplier 1** karena quantity Binance sudah dalam unit aset dasar. Jangan memasukkan jumlah kontrak dari venue lain ke adapter ini.

PnL gross = `(harga - entry) × arah × quantity × multiplier`, dengan arah LONG = +1 dan SHORT = -1. Estimasi biaya = `(entry × fee_entry + harga × fee_exit) × quantity × multiplier`. PnL net estimasi = gross dikurangi estimasi biaya. Untuk futures, ROI memakai margin teoretis `entry × quantity × multiplier / leverage`, bukan saldo akun, margin cross aktual, atau perhitungan likuidasi. Risiko uang awal dan R memakai jarak entry-SL sebelum biaya.

Fee harus berupa estimasi biaya dalam satuan quote. Diskon BNB, fee aset dasar, fee rebate, funding, slippage, saldo akun, serta margin aktual tidak disinkronkan. Quantity spot harus mencerminkan jumlah aset aktual; perhitungan fee tetap estimasi, bukan rekonsiliasi buku kas exchange. Tanpa leverage, ROI futures tampil N/A. Tanpa quantity/fee yang valid, formulir tidak dapat dikonfirmasi.

## 4. Pemantauan dan trailing

Integrasi pasar sumber awal hanya memakai REST. Versi ini menyediakan **polling fallback**; belum menyediakan WebSocket. Interval default 5 detik, timeout GET 5 detik per request, dan batas basi 20 detik. Ini bukan jaminan latensi 5 detik: latensi jaringan, jumlah simbol, refresh candle/metadata, serta rate limit dapat memperpanjang satu siklus. Sampel dapat melewatkan lonjakan harga dan sentuhan stop di antara polling.

Harga menggunakan waktu observasi exchange, bukan waktu respons lokal. Gunakan jam sistem UTC yang tersinkronisasi. Harga basi, timestamp tidak valid, candle tidak lengkap, metadata hilang, atau koneksi gagal menghentikan rekomendasi baru. HTTP 418/429 menghormati `Retry-After`; batas lokal membatasi jumlah GET per menit, bukan bobot request Binance. Simbol aktif yang sama berbagi satu pengambilan harga per siklus. Metadata selama registrasi juga membutuhkan request. Setelah gangguan, worker mengambil metadata dan candle lagi sebelum melanjutkan.

ATR memakai mean true range pada N candle **tertutup yang berurutan**, bukan smoothing Wilder. Futures memakai mark-price candles; spot memakai trade-price candles. Default N=14, interval 1 menit, multiplier 2. Ekstrem harga mulai dari entry sebagai acuan dan sampel valid sejak konfirmasi tracking. Tidak ada rekonstruksi harga sejak waktu fill sebelum registrasi atau selama downtime.

Aktivasi default +1R. Usulan pertama adalah break-even dengan fee dan buffer, dibulatkan ke tick yang tetap mencakup estimasi biaya. Berikutnya, LONG memakai harga tertinggi teramati dikurangi ATR × multiplier; SHORT memakai harga terendah ditambah ATR × multiplier. Stop baru harus berada di sisi valid terhadap harga kini dan memperbaiki stop sebelumnya. Stop tidak pernah dilonggarkan. Minimum perbaikan ATR = maksimum dari `min_ticks × tick` dan `min_improvement_r × risiko per unit`. Aktivasi break-even memerlukan perbaikan minimal satu tick.

Break-even LONG = `entry × (1 + fee_entry + buffer) / (1 - fee_exit)`.
Break-even SHORT = `entry × (1 - fee_entry - buffer) / (1 + fee_exit)`.
Buffer = bps / 10.000. Perhitungan ini tidak menjamin posisi benar-benar impas.

Sentuhan level stop hanya menghasilkan advisory. Status tidak berubah menjadi CLOSED tanpa konfirmasi pengguna. Swing-structure trailing belum disediakan. Parameter default merupakan titik awal pengujian, bukan hasil optimasi atau bukti profitabilitas.

## 5. Konfigurasi

| Variabel | Default | Fungsi |
| --- | --- | --- |
| `MANUAL_TRACKING_ENABLED` | `false` | Mengaktifkan integrasi baru |
| `TELEGRAM_BOT_TOKEN` | kosong | Token bot khusus, tidak disimpan di database |
| `TELEGRAM_CHAT_ID` | kosong | Tujuan sinyal, harus numerik dan ada di allowlist |
| `MANUAL_TELEGRAM_USER_IDS` | kosong | User numerik yang diizinkan; wajib |
| `MANUAL_TELEGRAM_CHAT_IDS` | chat tujuan | Chat numerik yang diizinkan; wajib |
| `MANUAL_DASHBOARD_TOKEN` | kosong | Kunci panel minimal 16 karakter |
| `SECRETAGENT_DB_FILE` | `trading_v5.db` | Path database untuk UI asli |
| `MANUAL_DB_FILE` | `trading_v5.db` | Default path worker; `--db` mengesampingkan |
| `MANUAL_POLL_SECONDS` | `5` | Interval minimum antarsiklus; minimal 2 |
| `MANUAL_STALE_SECONDS` | `20` | Batas umur quote; harus lebih besar dari polling |
| `MANUAL_SIGNAL_TTL_SECONDS` | `86400` | Umur tombol entry/ignore; tidak otomatis menutup posisi |
| `MANUAL_SESSION_TTL_SECONDS` | `900` | Batas tiap tahap formulir |
| `MANUAL_ACTIVATION_R` | `1` | Aktivasi break-even |
| `MANUAL_ATR_PERIOD` | `14` | 2..100 candle tertutup |
| `MANUAL_ATR_MULTIPLIER` | `2` | Pengali ATR positif |
| `MANUAL_ATR_INTERVAL` | `1m` | `1m`, `5m`, `15m`, `1h` |
| `MANUAL_MIN_IMPROVEMENT_TICKS` | `2` | Perbaikan minimum dalam tick |
| `MANUAL_MIN_IMPROVEMENT_R` | `0.05` | Perbaikan minimum relatif risiko awal |
| `MANUAL_COST_BUFFER_BPS` | `5` | Buffer break-even; 0..1000 bps |
| `MANUAL_REQUESTS_PER_MINUTE` | `120` | Batas lokal GET; 10..600 |
| `MANUAL_LOCK_DIR` | direktori temporary OS | Direktori lock bersama untuk semua proses bot di satu host |

Untuk menonaktifkan integrasi, hentikan worker manual dan atur `MANUAL_TRACKING_ENABLED=false` pada UI/worker scan. Data manual tetap tersimpan. Tidak ada monitoring selama worker berhenti. Memulai worker lagi memulihkan posisi ACTIVE/error setelah resinkronisasi; PAUSED tetap PAUSED.

## 6. Pengiriman, restart, dan pemeriksaan

```bash
python -m manual_tracker --status --db "$SECRETAGENT_DB_FILE"
python -m unittest discover -v
python -m compileall -q app.py manual_tracker tests
```

Outbox menyimpan PENDING, SENDING, SENT, FAILED, UNKNOWN, atau SUPERSEDED. SENT hanya tercatat setelah respons Telegram sukses dengan message ID. Penolakan pasti 429 dijadwalkan ulang setelah cooldown. Timeout, respons ambigu, HTTP 5xx, atau restart saat SENDING menjadi UNKNOWN dan **tidak diulang otomatis**. Telegram sendMessage tidak menyediakan jaminan exactly-once; pilihan ini menghindari duplikasi dengan konsekuensi sebagian pesan mungkin tidak sampai. Catatan UNKNOWN/FAILED terlihat pada panel dan `--status`.

Jika prompt tidak sampai, periksa bot lalu gunakan `/cancel` dan mulai kembali. Jika ringkasan posisi tidak sampai, gunakan `/positions`. Pesan sinyal UNKNOWN tidak diterbitkan ulang otomatis. Verifikasi manual sebelum membuat mekanisme pengiriman ulang tambahan. Rekomendasi yang sudah basi atau tidak relevan akan menjadi SUPERSEDED. Pesan yang sudah berada dalam perjalanan jaringan masih mungkin tiba sesudah pause; baca timestamp dan status terbaru.

Satu worker memegang OS lock berdasarkan ID bot, termasuk ketika token berotasi. Bot juga terikat pada database. Lock berlaku pada satu host dengan direktori lock yang sama. Gunakan satu instance worker dan bot khusus pada seluruh deployment; lock lokal tidak dapat mengoordinasikan host lain. Worker memeriksa webhook tanpa menghapusnya. Konflik getUpdates 409 menghentikan penerima callback dan mencatat BLOCKED_CONFLICT. Pemantauan dan pengiriman memiliki loop terpisah dari penerima callback.

Tidak ada tes real Telegram, real exchange, atau browser Streamlit dalam lingkungan pengerjaan ini. Pengujian HTTP memakai mock dan harga sintetis. Dependensi UI tidak tersedia di lingkungan offline ini; impor aplikasi menggunakan fallback UI bawaan dan requests vendored pip untuk QA saja. Petunjuk pengujian eksternal dan hasil lengkap ada di `docs/TEST_RESULTS.md`.

## 7. Isi proyek

`manual_tracker/` berisi konfigurasi, akuntansi, persistence, interaksi Telegram, market feed, monitor, outbox, worker, dan panel UI. `tests/` berisi pengujian deterministik serta fingerprint dan fixture dari sumber asli. `docs/STATE_AND_MIGRATION.md` menjelaskan state machine, tabel, dan pemulihan. `docs/CHANGES.patch` memperlihatkan perubahan kecil pada `app.py` asli.

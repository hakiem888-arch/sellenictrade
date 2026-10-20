# State, persistence, dan pemulihan

## State machine

| State sekarang | Pemicu | State berikutnya |
| --- | --- | --- |
| SIGNAL_SENT | Entry dari user/chat allowlist dan message ID valid | AWAITING_ENTRY_DETAILS |
| SIGNAL_SENT / AWAITING_ENTRY_DETAILS | Abaikan oleh pemilik yang sah | IGNORED |
| AWAITING_ENTRY_DETAILS | Seluruh field valid, metadata segar, SL sesuai tick, konfirmasi akhir | ACTIVE |
| AWAITING_ENTRY_DETAILS | `/cancel` atau formulir kedaluwarsa | SIGNAL_SENT, tanpa tracker |
| ACTIVE | Gangguan koneksi, harga basi, candle tidak lengkap | TRACKING_ERROR |
| ACTIVE / TRACKING_ERROR | Metadata/instrumen atau data posisi tidak valid | INVALID_DATA |
| TRACKING_ERROR / INVALID_DATA | Harga, candle, dan metadata kembali valid | ACTIVE |
| ACTIVE / TRACKING_ERROR / INVALID_DATA | Pause eksplisit pemilik | PAUSED |
| PAUSED | Resume eksplisit pemilik | ACTIVE, menunggu resinkronisasi |
| ACTIVE / PAUSED / TRACKING_ERROR / INVALID_DATA | Pemilik mulai melaporkan penutupan | EXIT_REPORTED |
| EXIT_REPORTED | Konfirmasi akhir pemilik | CLOSED |
| EXIT_REPORTED | Batal atau konfirmasi kedaluwarsa | PAUSED |
| IGNORED / CLOSED | Tombol entry lama, restart, harga baru | Tidak berubah |

State `SIGNAL_SENT` adalah lifecycle sinyal yang diterbitkan ke outbox, **bukan klaim pengiriman sukses**. Keberhasilan kirim harus diperiksa di `mt_outbox.state=SENT` dengan `message_id`. Jika belum terkirim, belum ada interaksi valid yang dapat mengaktifkan posisi.

Tidak ada transisi dari WAIT ke tracker. Sentuhan level SL hanya event `STOP_TOUCHED`; tidak mengubah ACTIVE menjadi CLOSED. Gangguan harga tidak dapat menutup posisi. State CLOSED selalu menyimpan `closure_source=USER_REPORTED`; harga exit boleh NULL. Tidak ada deteksi fill dari exchange.

Sinyal IGNORED terminal. Formulir yang dibatalkan tidak menciptakan tracker. Satu sinyal maksimal satu posisi sepanjang lifecycle, melalui UNIQUE constraint. Permintaan entry dari dua pengguna bersamaan diurutkan oleh transaksi SQLite; hanya pemilik klaim pertama yang dapat melanjutkan.

## Migrasi

Jalankan `python -m manual_tracker --migrate --db /path/trading_v5.db`. Perintah ini membuat backup konsisten terlebih dahulu jika file ada. Simpan path backup yang dicetak. Migrasi sendiri menggunakan transaksi CREATE-only yang idempoten. Tidak ada DROP, DELETE, penggantian database, pengurangan risk gate, atau perubahan schema version legacy oleh modul baru.

| Tabel baru | Isi |
| --- | --- |
| `mt_schema` | Versi migrasi fitur manual, terpisah dari schema legacy |
| `mt_signals` | ID manual, ID sinyal asli, source key, snapshot referensi, chat/pemilik, state, expiry, message ID |
| `mt_positions` | Detail fill aktual dalam JSON, posisi/status, stop rekomendasi, ekstrem, aktivasi, ATR, harga dan waktu, metrik, event terakhir, penutupan pengguna |
| `mt_instruments` | Tick terverifikasi, instrumen/simbol, waktu metadata, error metadata |
| `mt_sessions` | Formulir per user/chat, nonce, tahap, data, expiry, prompt terakhir |
| `mt_updates` | ID update/callback, bot, hasil, waktu; pencegahan replay |
| `mt_events` | Jejak transisi, aktivasi, trailing, sentuhan stop, dan registrasi |
| `mt_outbox` | Isi pesan, message ID, delivery state, retry, expiry, last error |
| `mt_runtime` | Heartbeat, offset Telegram, identitas bot, cooldown pengiriman |

`mt_positions.details` menyimpan symbol, instrument, direction, entry, quantity, initial_stop, multiplier, leverage, estimasi entry_fee/exit_fee, confirmed_tick, confirmed_at, quote_asset, dan execution_source. Angka uang diserialisasikan sebagai string decimal; perhitungan memakai Decimal. Timestamp runtime berupa epoch detik UTC; tampilan menyertakan UTC. Snapshot sinyal mempertahankan timestamp ISO asli dari v5.5.

Tidak ada foreign key dari tabel manual ke tabel `signals` lama. Ini disengaja: data manual dan snapshot tidak ikut terhapus oleh reset sandbox paper-trading yang sudah ada. Source key menggabungkan ID, waktu penciptaan, dan analysis ID agar sinyal baru setelah reset tidak tertukar dengan posisi lama. Jangan memakai reset sebagai prosedur migrasi.

## Atomicity dan delivery

Perubahan state, pembuatan tracker, event, reply outbox, ID callback, dan offset update dikomit dalam transaksi yang sama. Error database membatalkan transaksi; offset tidak maju. Jaringan tidak dipanggil selama transaksi interaksi. Pengiriman dilakukan loop berbeda dengan claim PENDING → SENDING sebelum HTTP.

Respons sukses mencatat SENT dan ID pesan. Pencatatan `last_notified_stop` hanya terjadi setelah sukses. Level terakhir yang direkomendasikan tetap tersimpan meskipun pengiriman gagal, agar algoritme tidak pernah mengusulkan pelonggaran stop. Status pengiriman dan level rekomendasi memiliki arti berbeda; keduanya bukan bukti bahwa pengguna telah memasang order SL.

Event recommendation mempunyai kunci unik `(position ID, stage, level stop)`. SQLite membatasi event dan pesan ganda. Event touch memakai level yang disentuh. Saat pause/close/error, rekomendasi pending disupersede. Sebelum pengiriman, dispatcher memeriksa state aktif, umur data, level terbaru, dan posisi harga terhadap stop. Pesan yang sudah dikirim ke jaringan tidak dapat ditarik kembali oleh lock lokal.

Timeout dan respons tidak pasti memakai UNKNOWN, bukan retry otomatis. Crash setelah Telegram menerima pesan tetapi sebelum commit lokal tidak bisa dibedakan dari crash sebelum pengiriman. Restart mengubah SENDING menjadi UNKNOWN. Ini bukan jaminan exactly-once dari Telegram. UNKNOWN mengutamakan penghindaran duplikasi; pesan bisa tidak tersampaikan. Gunakan `/positions` untuk ringkasan baru, atau `/cancel` dan ulang formulir jika prompt hilang.

## Restart dan gangguan

- ACTIVE, TRACKING_ERROR, dan INVALID_DATA menjalani resinkronisasi metadata, candle tertutup, dan quote. Rekomendasi lama pending tidak boleh dianggap valid selama resinkronisasi.
- PAUSED tetap PAUSED. Tidak ada silent resume setelah restart.
- EXIT_REPORTED tetap menunggu konfirmasi jika session belum kedaluwarsa. Setelah expiry, status menjadi PAUSED.
- Ekstrem harga, stop monotonic, aktivasi, ID event, dan offset Telegram tetap tersimpan.
- Monitor tidak mengarang candle/quote untuk menutup celah data. Nilai ekstrem selama downtime tidak diketahui.
- Groq dan worker scan tidak dibutuhkan untuk melanjutkan monitoring posisi yang sudah sah.
- Heartbeat yang basi membuat panel mengosongkan metrik terkini. Harga terakhir tetap tampak dengan timestamp.

## Rollback deployment

Hentikan worker manual, nonaktifkan flag, lalu jalankan kembali aplikasi v5.5 asli bila diperlukan. Tabel `mt_*` boleh tetap ada tanpa digunakan. Jangan menimpa database aktif dengan backup lama karena itu dapat membuang perubahan transaksi sesudah backup. Restore penuh hanya melalui prosedur backup operasional yang Anda setujui. Arsip hasil tidak melakukan restore atau perubahan pada database pengguna.

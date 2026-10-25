# Futures Desk v5.6 — Auto Scan (30/45/60/90s) & Telegram Alert

Aplikasi Streamlit analisis dan pemindaian otomatis **Crypto Futures (Binance USD-M Perpetual)** berbasis aturan deterministik **Master Prompt v1** dan rotasi **7 API Key Groq** serta notifikasi **Bot Telegram** langsung dari **Streamlit Secrets (`st.secrets`)**.

## Pembaruan Utama (v5.6 Auto-Scan)
1. **Perbaikan CPU Throttling Streamlit Cloud**:
   - Menghapus loop re-render grafik `@st.fragment(run_every="5s")` yang sebelumnya membebani CPU secara terus-menerus.
   - Menambahkan *in-memory TTL cache* pada `/fapi/v1/exchangeInfo` (1 jam) dan `/fapi/v1/fundingInfo` (5 menit), serta membatasi komputasi indikator grafik pada candle tertutup yang relevan.
2. **Tanpa Input API di GUI (100% dari `st.secrets`)**:
   - Form input 7 API Key Groq dan Token/Chat ID Telegram dihapus dari antarmuka pengguna.
   - Seluruh kredensial dibaca otomatis dari `.streamlit/secrets.toml` atau pengaturan **Secrets** di Streamlit Community Cloud.
3. **Auto Scan Tiap `30`, `45`, `60`, atau `90` Detik**:
   - Pilihan interval pemindaian otomatis `30`, `45`, `60`, atau `90` detik (single pair maupun multi-pair watchlist, satu mode maupun seluruh mode sekaligus).
   - Setiap hasil scan langsung dievaluasi menggunakan mesin aturan **Master Prompt v1** (`evaluate_auto`) dan divalidasi oleh **AI Groq** (rotasi 7 key).
4. **Filter Alert Telegram Khusus `BUY (LONG)` & `SELL (SHORT)` + Label Jenis Trading**:
   - Hanya sinyal yang **LOLOS (`VALID`)** sebagai **BUY (`LONG`)** atau **SELL (`SHORT`)** yang diteruskan ke Telegram.
   - Sinyal **`WAIT`** atau **`NO_TRADE`** otomatis diabaikan dan **tidak** dikirim ke Telegram.
   - Setiap pesan alert Telegram mencantumkan label jenis trading secara eksplisit: **`[SCALPER]`** (Bias 1H / Eksekusi 5M), **`[INTRADAY]`** (Bias 4H / Eksekusi 15M), atau **`[SWING]`** (Bias 1D / Eksekusi 4H).

## Konfigurasi Streamlit Secrets (`.streamlit/secrets.toml`)
```toml
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_API_KEY_1 = "gsk_..."
GROQ_API_KEY_2 = "gsk_..."
GROQ_API_KEY_3 = "gsk_..."
GROQ_API_KEY_4 = "gsk_..."
GROQ_API_KEY_5 = "gsk_..."
GROQ_API_KEY_6 = "gsk_..."
GROQ_API_KEY_7 = "gsk_..."

TELEGRAM_BOT_TOKEN = "123456789:ABCDEF..."
TELEGRAM_CHAT_ID = "-1001234567890"
```

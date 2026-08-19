import os
import re
import json
import ast
import math
import sqlite3
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from groq import Groq

# ============================================================
# AI CONSENSUS TRADING V6.0 (SMC EDITION - FULL UI & AUTO ROTATE)
# - Menggunakan Order Block & Fair Value Gap
# - Bypass Groq JSON Sensors (Qwen Model)
# - Dynamic TP/SL Execution
# - Robust AST JSON Parser + Raw Error Tracker
# - Auto-Rotate 7+ API Keys
# - Full Bahasa Indonesia Prompt
# ============================================================

st.set_page_config(page_title="AI Consensus Trading V6 SMC", page_icon="🧠", layout="wide")

BINANCE = "https://data-api.binance.vision"
DB_FILE = "trading_v6.db"

# -------------------------
# Secrets & API Key Parsing
# -------------------------
def secret(key):
    try:
        if key in st.secrets: return st.secrets[key]
    except Exception: pass
    return os.getenv(key, "")

def _parse_secret_list(value):
    if value is None: return []
    if isinstance(value, (list, tuple)): raw_items = value
    else: raw_items = re.split(r"[\s,]+", str(value))
    return [str(x).strip() for x in raw_items if str(x).strip()]

_GROQ_KEYS = []
_GROQ_KEYS.extend(_parse_secret_list(secret("GROQ_API_KEY")))
_GROQ_KEYS.extend(_parse_secret_list(secret("GROQ_API_KEYS")))
for _i in range(1, 21):
    _GROQ_KEYS.extend(_parse_secret_list(secret(f"GROQ_API_KEY_{_i}")))

# Menghapus duplikat API Key
GROQ_KEYS = list(dict.fromkeys(k for k in _GROQ_KEYS if k))

TG_TOKEN = secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = secret("TELEGRAM_CHAT_ID")

# -------------------------
# Database
# -------------------------
def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL, scanner_score REAL, final_decision TEXT NOT NULL,
        confidence REAL, entry REAL, stop_loss REAL, take_profit REAL, risk_reward REAL,
        market_regime TEXT, score_consensus REAL, reasoning TEXT, closed_at TEXT,
        result TEXT, r_multiple REAL, exit_price REAL
    );
    CREATE TABLE IF NOT EXISTS agent_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, agent TEXT NOT NULL,
        decision TEXT NOT NULL, confidence REAL, reasoning TEXT,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    );
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol TEXT,
        memory_type TEXT NOT NULL, content TEXT NOT NULL, signal_id INTEGER
    );
    """)
    con.commit(); con.close()

init_db()

# -------------------------
# Binance & SMC Indicators
# -------------------------
def bget(path, params=None):
    r = requests.get(BINANCE + path, params=params, timeout=15, headers={"User-Agent": "AI-V6-SMC"})
    if r.status_code != 200: raise RuntimeError(f"Binance HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def binance_health():
    try:
        bget("/api/v3/ping")
        return True, "ONLINE"
    except Exception as e:
        return False, str(e)

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.where(d > 0, 0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.where(d < 0, 0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)

def atr(df, n=14):
    tr = pd.concat([df.High - df.Low, (df.High - df.Close.shift()).abs(), (df.Low - df.Close.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def detect_smc(df):
    fvgs = {"bullish": [], "bearish": []}
    obs = {"bullish": [], "bearish": []}
    
    for i in range(2, len(df) - 1):
        atr_val = df['ATR'].iloc[i]
        if df['Low'].iloc[i] > df['High'].iloc[i-2]:
            gap = df['Low'].iloc[i] - df['High'].iloc[i-2]
            if gap > atr_val * 0.1: 
                fvgs["bullish"].append({"bottom": df['High'].iloc[i-2], "top": df['Low'].iloc[i]})
        elif df['High'].iloc[i] < df['Low'].iloc[i-2]:
            gap = df['Low'].iloc[i-2] - df['High'].iloc[i]
            if gap > atr_val * 0.1:
                fvgs["bearish"].append({"bottom": df['High'].iloc[i], "top": df['Low'].iloc[i-2]})
                
    for i in range(1, len(df) - 1):
        body = abs(df['Close'].iloc[i] - df['Open'].iloc[i])
        atr_val = df['ATR'].iloc[i]
        
        if df['Close'].iloc[i] > df['Open'].iloc[i] and body > atr_val * 1.5:
            for j in range(i-1, max(-1, i-6), -1):
                if df['Close'].iloc[j] < df['Open'].iloc[j]:
                    obs["bullish"].append({"bottom": df['Low'].iloc[j], "top": df['High'].iloc[j]})
                    break
        elif df['Close'].iloc[i] < df['Open'].iloc[i] and body > atr_val * 1.5:
            for j in range(i-1, max(-1, i-6), -1):
                if df['Close'].iloc[j] > df['Open'].iloc[j]:
                    obs["bearish"].append({"bottom": df['Low'].iloc[j], "top": df['High'].iloc[j]})
                    break

    return {
        "fvg_bull": fvgs["bullish"][-2:], 
        "fvg_bear": fvgs["bearish"][-2:],
        "ob_bull": obs["bullish"][-2:],
        "ob_bear": obs["bearish"][-2:]
    }

def klines(symbol, interval="15m", limit=180):
    x = bget("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["time", "Open", "High", "Low", "Close", "Volume", "close_time", "QuoteVolume", "Trades", "TakerBuyBase", "TakerBuyQuote", "ignore"]
    d = pd.DataFrame(x, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume", "QuoteVolume", "Trades", "TakerBuyBase", "TakerBuyQuote"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d.index = pd.to_datetime(d.time, unit="ms", utc=True)
    d["EMA20"] = ema(d.Close, 20); d["EMA50"] = ema(d.Close, 50)
    d["RSI"] = rsi(d.Close); d["ATR"] = atr(d)
    d["VOL_RATIO"] = d.Volume / d.Volume.rolling(20).mean()
    d["BUY_PRESSURE"] = d.TakerBuyQuote / d.QuoteVolume.replace(0, pd.NA)
    return d.dropna()

def order_book(symbol, limit=20):
    x = bget("/api/v3/depth", {"symbol": symbol, "limit": limit})
    bid = sum(float(q) for _, q in x.get("bids", []))
    ask = sum(float(q) for _, q in x.get("asks", []))
    total = bid + ask
    imbalance = (bid - ask) / total if total else 0
    best_bid = float(x["bids"][0][0]) if x.get("bids") else 0
    best_ask = float(x["asks"][0][0]) if x.get("asks") else 0
    spread = ((best_ask - best_bid) / best_bid * 100 if best_bid else 0)
    return {"imbalance": imbalance, "spread": spread, "bid_depth": bid, "ask_depth": ask}

def candle_info(d):
    a = d.iloc[-1]; p = d.iloc[-2]
    body = abs(a.Close - a.Open); rng = max(a.High - a.Low, 1e-12)
    upper = a.High - max(a.Open, a.Close); lower = min(a.Open, a.Close) - a.Low
    body_ratio = body / rng
    if body_ratio < 0.10: name = "DOJI"
    elif lower > body * 2 and upper < max(body, 1e-12): name = "HAMMER BULLISH"
    elif upper > body * 2 and lower < max(body, 1e-12): name = "SHOOTING STAR BEARISH"
    elif a.Close > a.Open and p.Close < p.Open and a.Open <= p.Close and a.Close >= p.Open: name = "BULLISH ENGULFING"
    elif a.Close < a.Open and p.Close > p.Open and a.Open >= p.Close and a.Close <= p.Open: name = "BEARISH ENGULFING"
    elif a.Close > a.Open: name = "BULLISH"
    elif a.Close < a.Open: name = "BEARISH"
    else: name = "NETRAL"
    return {"name": name, "body": body_ratio, "upper_wick": upper / rng, "lower_wick": lower / rng}

def market_regime(d):
    a = d.iloc[-1]
    vol = float(a.ATR / a.Close)
    if vol >= 0.03: return "VOLATILE"
    return "TRENDING"

# -------------------------
# Scanner
# -------------------------
def scanner_score(symbol, change, qvol, d, ob):
    a = d.iloc[-1]
    score = 50; why = []
    if change >= 3: score += 12; why.append("naik kuat")
    elif change >= 1: score += 6; why.append("momentum positif")
    elif change <= -3: score -= 12; why.append("turun kuat")
    elif change <= -1: score -= 6; why.append("momentum negatif")
    vr = float(a.VOL_RATIO)
    if vr >= 3: score += 18; why.append("vol sangat tinggi")
    elif vr >= 2: score += 12; why.append("vol meningkat")
    elif vr >= 1.5: score += 6
    buy = float(a.BUY_PRESSURE)
    if buy >= 0.60: score += 12; why.append("taker-buy dominan")
    elif buy >= 0.55: score += 6
    elif buy <= 0.40: score -= 12; why.append("taker-sell dominan")
    if ob["imbalance"] >= 0.20: score += 8; why.append("bid tebal")
    elif ob["imbalance"] <= -0.20: score -= 8; why.append("ask tebal")
    r = float(a.RSI)
    if 50 <= r <= 68: score += 6
    elif r > 78: score -= 8
    return max(0, min(100, round(score))), why

def scan_markets(n=60):
    tickers = bget("/api/v3/ticker/24hr")
    candidates = []
    stable_like = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE"}
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT") or symbol[:-4] in stable_like: continue
        qvol = float(t.get("quoteVolume", 0) or 0)
        if qvol < 2_000_000: continue
        candidates.append((symbol, float(t.get("priceChangePercent", 0) or 0), qvol))
    candidates.sort(key=lambda x: x[2], reverse=True)
    out = []; progress = st.progress(0); status = st.empty()
    for i, (symbol, change, qvol) in enumerate(candidates[:n]):
        status.write(f"🔎 Memindai {symbol} ({i+1}/{min(len(candidates), n)})")
        try:
            d = klines(symbol, "15m", 100); ob = order_book(symbol)
            score, why = scanner_score(symbol, change, qvol, d, ob)
            a = d.iloc[-1]
            out.append({"symbol": symbol, "change": change, "qvol": qvol, "vr": float(a.VOL_RATIO), "buy": float(a.BUY_PRESSURE), "imb": float(ob["imbalance"]), "spread": float(ob["spread"]), "rsi": float(a.RSI), "score": score, "why": why})
        except Exception: pass
        progress.progress((i + 1) / min(len(candidates), n))
    progress.empty(); status.empty()
    return sorted(out, key=lambda x: x["score"], reverse=True)

# -------------------------
# AI engine — KUNCI QWEN + MULTI-KEY ROTATION
# -------------------------
ANALYST_MODEL = "qwen/qwen3.6-27b"
DEBATE_MODEL = "qwen/qwen3.6-27b"
RESEARCH_MODEL = "qwen/qwen3.6-27b"
JUDGE_MODEL = "qwen/qwen3.6-27b"

MODEL_TIERS = {
    "analyst": {"model": ANALYST_MODEL, "max_tokens": 800},
    "debate": {"model": DEBATE_MODEL, "max_tokens": 800},
    "research": {"model": RESEARCH_MODEL, "max_tokens": 1000},
    "judge": {"model": JUDGE_MODEL, "max_tokens": 1000},
}

GROQ_MIN_DELAY = 1.0
GROQ_RETRY_SECONDS = 15.0

def compact_json(obj, max_chars=1400):
    if isinstance(obj, dict):
        evidence = obj.get("evidence", [])
        if not isinstance(evidence, list): evidence = [str(evidence)] if evidence else []
        compacted = {
            "decision": obj.get("decision", "WAIT"),
            "confidence": obj.get("confidence", 0),
            "reasoning": obj.get("reasoning", ""),
            "key_risk": obj.get("key_risk", ""),
            "evidence": evidence[:3],
        }
        if "tp_price" in obj: compacted["tp_price"] = obj["tp_price"]
        if "sl_price" in obj: compacted["sl_price"] = obj["sl_price"]
        obj = compacted
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))[:max_chars]

def _safe_json_from_text(raw):
    raw = (raw or "").strip()
    if not raw: raise ValueError("Respons AI kosong melompong.")
    cleaned = re.sub(r"```[a-zA-Z]*\n?", "", raw).replace("```", "").strip()
    try: return json.loads(cleaned)
    except Exception: pass
    
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start:end+1]
        try: return json.loads(candidate)
        except Exception: pass
        try:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, dict): return parsed
        except Exception: pass
    raise ValueError(f"RAW OUTPUT QWEN: {raw[:300]}...")

def _normalize_ai(obj, tier):
    if not isinstance(obj, dict): raise ValueError("Output AI bukan object JSON.")
    decision = str(obj.get("decision", "WAIT")).upper().strip()
    if decision not in {"LONG", "SHORT", "WAIT"}: decision = "WAIT"
    try: confidence = float(obj.get("confidence", 0) or 0)
    except Exception: confidence = 0.0
    evidence = obj.get("evidence", [])
    if not isinstance(evidence, list): evidence = [str(evidence)] if evidence else []
    return {
        "decision": decision, "confidence": max(0.0, min(100.0, confidence)),
        "reasoning": str(obj.get("reasoning", ""))[:700], "key_risk": str(obj.get("key_risk", ""))[:400],
        "evidence": [str(x)[:180] for x in evidence[:3]], "tp_price": float(obj.get("tp_price", 0.0) or 0.0),
        "sl_price": float(obj.get("sl_price", 0.0) or 0.0), "tier": tier, "model": MODEL_TIERS[tier]["model"]
    }

def _build_prompt(role, data, instructions):
    return f"""Kamu adalah {role} dalam sistem riset trading SMC (Smart Money Concepts).
TUGAS: {instructions}
ATURAN WAJIB:
1. Gunakan data Order Block (OB) dan Fair Value Gap (FVG). Jangan pakai MA/Lagging indicator.
2. Jika harga belum mencapai OB/FVG, pilih WAIT. Jika harga memitigasi OB, pilih LONG/SHORT.
3. JAWABAN HARUS BERUPA JSON MURNI TANPA TEKS LAIN.
4. SELURUH TEKS (reasoning, key_risk, evidence) WAJIB DITULIS DALAM BAHASA INDONESIA.
5. Tentukan tp_price dan sl_price berdasarkan batas OB/FVG terdekat (JIKA WAIT, ISI 0.0).
CONTOH JSON:
{{"decision":"LONG","confidence":85,"reasoning":"Harga memitigasi bullish OB dan memantul kuat.","key_risk":"Tembus level support OB di bawah.","evidence":["Mitigasi Bull OB di 65000"],"tp_price":66000.0,"sl_price":64500.0}}
DATA:
{data}
"""

def _request_groq(model, prompt, key, max_tokens):
    client = Groq(api_key=key)
    kwargs = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a rigid data API. You output ONLY valid JSON format. All string values inside the JSON MUST be in Indonesian language (Bahasa Indonesia). NO conversational text. NO markdown formatting."
            },
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": int(max_tokens)
    }
    return client.chat.completions.create(**kwargs)

def call_ai(role, data, instructions, tier="analyst", budget=None):
    if tier not in MODEL_TIERS: tier = "analyst"
    if not GROQ_KEYS: return {"decision": "WAIT", "confidence": 0, "reasoning": "API_KEY kosong.", "key_risk": "", "evidence": [], "tp_price": 0.0, "sl_price": 0.0, "tier": tier, "model": ""}
    
    cfg = MODEL_TIERS[tier]
    max_tokens = budget or cfg["max_tokens"]
    prompt = _build_prompt(role, data, instructions)
    
    # Inisialisasi index key aktif jika belum ada
    if "_active_groq_key" not in st.session_state:
        st.session_state["_active_groq_key"] = 0
        
    total_keys = len(GROQ_KEYS)
    start_index = st.session_state["_active_groq_key"] % total_keys
    
    # ROTASI OTOMATIS: Coba semua kunci satu per satu jika kena limit
    for offset in range(total_keys):
        current_index = (start_index + offset) % total_keys
        current_key = GROQ_KEYS[current_index]
        
        try:
            last = float(st.session_state.get("_last_ai_call", 0))
            wait = GROQ_MIN_DELAY - (time.time() - last)
            if wait > 0: time.sleep(wait)
            
            raw = _request_groq(cfg["model"], prompt, current_key, max_tokens)
            
            st.session_state["_last_ai_call"] = time.time()
            st.session_state["_active_groq_key"] = current_index # Simpan kunci yang berhasil
            
            content = raw.choices[0].message.content or ""
            obj = _safe_json_from_text(content)
            return _normalize_ai(obj, tier)
            
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower() or "quota" in msg.lower() or "billing" in msg.lower():
                # API Limit tersentuh, lanjutkan iterasi loop untuk pindah ke API Key berikutnya
                continue 
            else:
                # Error format JSON atau lainnya, langsung keluar (bukan karena kuota)
                return {"decision": "WAIT", "confidence": 0, "reasoning": f"AI error: {msg}", "key_risk": "Gagal.", "evidence": [], "tp_price": 0.0, "sl_price": 0.0, "tier": tier, "model": cfg["model"]}
                
    # Jika loop selesai dan tidak ada return, berarti 7 kunci semuanya mati/kena limit
    return {"decision": "WAIT", "confidence": 0, "reasoning": "Seluruh API Key (7 buah) kehabisan kuota atau terkena limit 429.", "key_risk": "Gagal.", "evidence": [], "tp_price": 0.0, "sl_price": 0.0, "tier": tier, "model": cfg["model"]}

# -------------------------
# Market packet & Data Injector
# -------------------------
def market_packet(symbol):
    frames = {"15m": klines(symbol, "15m", 180)}
    ob = order_book(symbol)
    d = frames["15m"]
    smc_data = detect_smc(d) 
    c = candle_info(d)
    
    packet = {
        "symbol": symbol, "price": float(d.iloc[-1].Close), "market_regime": market_regime(d),
        "order_book": ob, "support": float(d.Low.tail(30).min()), "resistance": float(d.High.tail(30).max()),
        "candlestick": c, "smc": smc_data, "timeframes": {}
    }
    x = d.iloc[-1]
    packet["timeframes"]["15m"] = {"price": float(x.Close), "rsi": float(x.RSI), "atr": float(x.ATR), "vol_ratio": float(x.VOL_RATIO), "buy_pressure": float(x.BUY_PRESSURE)}
    return frames, packet

def specialist_data(packet, focus):
    smc = packet["smc"]
    lines = [
        f"Symbol: {packet['symbol']} | Harga: {packet['price']:.8f} | Regime: {packet['market_regime']}",
        "[SMART MONEY CONCEPTS (SMC) - 15m ZONES]"
    ]
    if smc["fvg_bull"]: lines.append(f"Bullish FVG: {smc['fvg_bull'][-1]['bottom']:.8f} - {smc['fvg_bull'][-1]['top']:.8f}")
    if smc["fvg_bear"]: lines.append(f"Bearish FVG: {smc['fvg_bear'][-1]['top']:.8f} - {smc['fvg_bear'][-1]['bottom']:.8f}")
    if smc["ob_bull"]: lines.append(f"Bullish OB: {smc['ob_bull'][-1]['bottom']:.8f} - {smc['ob_bull'][-1]['top']:.8f}")
    if smc["ob_bear"]: lines.append(f"Bearish OB: {smc['ob_bear'][-1]['top']:.8f} - {smc['ob_bear'][-1]['bottom']:.8f}")
    
    ob = packet["order_book"]
    lines.append(f"Imbalance OB: {ob['imbalance']:+.2%} | RSI 15m: {packet['timeframes']['15m']['rsi']:.2f}")
    return "\n".join(lines)

# -------------------------
# Orchestration
# -------------------------
ANALYSTS = [
    ("🧭 SMC Analyst", "Fokus area Order Block dan FVG. Harga harus masuk zona (mitigasi) untuk validasi LONG/SHORT."),
    ("📚 Liquidity Analyst", "Fokus order-book imbalance, spread, liquidity sweeps di sekitar zona SMC."),
    ("🕯️ Price Action Analyst", "Fokus reaksi candle (rejection/engulfing) saat harga menyentuh Order Block.")
]

def run_multi_agent(symbol, scanner_score_value):
    frames, packet = market_packet(symbol)
    data = specialist_data(packet, "all")

    analyst_results = []
    for name, rule in ANALYSTS:
        analyst_results.append({"agent": name, **call_ai(name, data, rule, tier="analyst")})

    analyst_summary = "\n".join([f"{x['agent']} | {x['decision']} | TP: {x['tp_price']} | SL: {x['sl_price']}" for x in analyst_results])

    debate_data = data + "\n\nANALYSTS:\n" + analyst_summary
    bull = call_ai("🐂 SMC Bull", debate_data, "Bangun kasus LONG berdasarkan Bullish OB/FVG.", tier="debate")
    bear = call_ai("🐻 SMC Bear", debate_data, "Serang thesis Bull, cari Bearish OB/FVG untuk kasus SHORT.", tier="debate")
    debate = f"BULL: {bull['decision']}\nBEAR: {bear['decision']}"

    trader_input = f"Market: {symbol} price={packet['price']:.8f}\nData SMC:\n{data}\nDebate: {debate}"
    trader = call_ai("💹 Smart Trader", trader_input, "Buat rencana trading dari Order Block. Tentukan tp_price dan sl_price dinamis dari data SMC.", tier="analyst")

    judge_input = f"MARKET: {symbol} price={packet['price']:.8f}\nTRADER:\n{compact_json(trader)}\nDATA SMC:\n{data}"
    portfolio = call_ai("👨‍⚖️ Final SMC Judge", judge_input, "Ambil keputusan final. WAJIB keluarkan tp_price (di batas resisten/OB lawan) dan sl_price (di bawah batas OB kita) jika LONG/SHORT.", tier="judge")

    final = portfolio["decision"]
    if portfolio.get("confidence", 0) < 55: final = "WAIT"

    current = packet["price"]
    atr15 = packet["timeframes"]["15m"]["atr"]
    
    if final in {"LONG", "SHORT"}:
        ai_tp = portfolio.get("tp_price", 0.0)
        ai_sl = portfolio.get("sl_price", 0.0)
        
        if ai_tp > 0 and ai_sl > 0 and ai_tp != current and ai_sl != current:
            tp = ai_tp
            sl = ai_sl
        else:
            sl = current - 1.0 * atr15 if final == "LONG" else current + 1.0 * atr15
            tp = current + 2.0 * atr15 if final == "LONG" else current - 2.0 * atr15
            
        risk = abs(current - sl)
        reward = abs(tp - current)
        rr = reward / risk if risk > 0 else 0.0
    else:
        sl = None; tp = None; rr = 0.0

    return {
        "symbol": symbol, "frames": frames, "packet": packet, "analysts": analyst_results,
        "bull": bull, "bear": bear, "trader": trader, "portfolio": portfolio, "final": final,
        "scanner_score": scanner_score_value, "entry": current if final != "WAIT" else None, "sl": sl, "tp": tp, "rr": rr
    }

# -------------------------
# Memory / paper trading
# -------------------------
def save_signal(result):
    con = db()
    now = datetime.now(timezone.utc).isoformat()
    cur = con.execute("INSERT INTO signals (created_at,symbol,timeframe,scanner_score,final_decision,confidence,entry,stop_loss,take_profit,risk_reward,market_regime,score_consensus,reasoning) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (now, result["symbol"], "15m SMC", result["scanner_score"], result["final"], result["portfolio"]["confidence"], result["entry"], result["sl"], result["tp"], result["rr"], result["packet"]["market_regime"], result["scanner_score"], result["portfolio"]["reasoning"]))
    con.commit(); con.close()
    return cur.lastrowid

def current_price(symbol):
    return float(bget("/api/v3/ticker/price", {"symbol": symbol})["price"])

def update_paper_trades():
    con = db(); rows = con.execute("SELECT * FROM signals WHERE result IS NULL AND final_decision IN ('LONG','SHORT')").fetchall(); updated = []
    for row in rows:
        try: price = current_price(row["symbol"])
        except Exception: continue
        res = r_mult = None
        if row["final_decision"] == "LONG":
            if price >= row["take_profit"]: res, r_mult = "TP", 2.0
            elif price <= row["stop_loss"]: res, r_mult = "SL", -1.0
        elif row["final_decision"] == "SHORT":
            if price <= row["take_profit"]: res, r_mult = "TP", 2.0
            elif price >= row["stop_loss"]: res, r_mult = "SL", -1.0
        if res:
            con.execute("UPDATE signals SET closed_at=?, result=?, r_multiple=?, exit_price=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), res, r_mult, price, row["id"]))
            updated.append((row["symbol"], res, r_mult))
    con.commit(); con.close()
    return updated

# -------------------------
# UI
# -------------------------
if "scan" not in st.session_state: st.session_state.scan = []
st.title("🧠 AI Consensus V6 SMC (Smart Money Edition)")

# ---- SIDEBAR PANEL KIRI ----
st.sidebar.header("🔎 Scanner SMC")
scan_n = st.sidebar.slider("Koin discan", 20, 100, 60)
top_n = st.sidebar.slider("Dianalisis AI", 1, 3, 1)

if st.sidebar.button("🔍 SCAN MARKET", use_container_width=True):
    ok, status = binance_health()
    if not ok: st.error(f"❌ Offline: {status}")
    else:
        with st.spinner("🛰️ Mencari market & mendeteksi FVG/OB..."): st.session_state.scan = scan_markets(scan_n)

if st.sidebar.button("🔄 UPDATE PAPER TRADES", use_container_width=True):
    updates = update_paper_trades()
    if updates:
        for symbol, res, r in updates: st.sidebar.success(f"{symbol}: {res} ({r:+.1f}R)")
    else: st.sidebar.info("Belum ada trade TP/SL.")

st.sidebar.divider()
st.sidebar.header("🤖 AI Status")
st.sidebar.write("Groq:", "🟢 SIAP" if GROQ_KEYS else "🔴 BELUM ADA")
if GROQ_KEYS: 
    aktif = st.session_state.get('_active_groq_key', 0) + 1
    st.sidebar.caption(f"🔑 API keys terpasang: {len(GROQ_KEYS)} (Memakai Key #{aktif})")
st.sidebar.caption("Arsitektur V6 SMC:")
st.sidebar.caption(f"⚡ Analyst: {ANALYST_MODEL}")
st.sidebar.caption(f"🐂/🐻 Debate: {DEBATE_MODEL}")
st.sidebar.caption(f"👨‍⚖️ Judge: {JUDGE_MODEL}")
st.sidebar.caption(f"⏱️ Jeda AI: {GROQ_MIN_DELAY:.1f}s")
# -----------------------------

st.divider()

if not st.session_state.scan:
    st.info("Klik SCAN MARKET untuk mendeteksi zona likuiditas.")
else:
    st.subheader("🔥 Top Kandidat SMC")
    rows = [{"Rank": i, "Symbol": x["symbol"], "Score": x["score"], "24h": f"{x['change']:+.2f}%", "Vol Ratio": f"{x['vr']:.2f}x"} for i, x in enumerate(st.session_state.scan[:10], 1)]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.divider()

    for rank, candidate in enumerate(st.session_state.scan[:top_n], 1):
        symbol = candidate["symbol"]
        st.markdown(f"## #{rank} {symbol} — SMC Analysis")

        with st.spinner(f"🧠 Membaca Order Block & FVG untuk {symbol}..."):
            try: result = run_multi_agent(symbol, candidate["score"])
            except Exception as e: st.error(f"Gagal: {e}"); continue

        c1, c2, c3 = st.columns(3)
        c1.metric("Harga", f"${result['packet']['price']:,.6f}")
        c2.metric("Regime", result['packet']['market_regime'])
        
        smc = result['packet']['smc']
        smc_text = ""
        if smc['ob_bull']: smc_text += f"🟢 Bull OB: {smc['ob_bull'][-1]['bottom']:.4f} - {smc['ob_bull'][-1]['top']:.4f}\n"
        if smc['ob_bear']: smc_text += f"🔴 Bear OB: {smc['ob_bear'][-1]['top']:.4f} - {smc['ob_bear'][-1]['bottom']:.4f}"
        c3.info(smc_text if smc_text else "Menunggu zona OB terbentuk...")

        # 1. TAMPILKAN GRAFIK CANDLESTICK
        ch = result["frames"]["15m"].tail(100)
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=ch.index, open=ch.Open, high=ch.High, low=ch.Low, close=ch.Close, name="Candlestick"))
        fig.update_layout(height=400, template="plotly_dark", xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)

        # 2. TAMPILKAN ANALYST TEAM
        st.markdown("### 🧩 SMC Analyst Team")
        cols = st.columns(len(result["analysts"]))
        for col, agent in zip(cols, result["analysts"]):
            with col:
                if agent["decision"] == "LONG":
                    col.success(f"**{agent['agent']}**\n\nLONG (Conf: {agent['confidence']:.0f})\n\n{agent['reasoning']}")
                elif agent["decision"] == "SHORT":
                    col.error(f"**{agent['agent']}**\n\nSHORT (Conf: {agent['confidence']:.0f})\n\n{agent['reasoning']}")
                else:
                    col.warning(f"**{agent['agent']}**\n\nWAIT (Conf: {agent['confidence']:.0f})\n\n{agent['reasoning']}")

        # 3. TAMPILKAN BULL VS BEAR
        st.markdown("### 🐂 Bull vs 🐻 Bear Debate")
        b1, b2 = st.columns(2)
        b1.success(f"**🐂 SMC Bull:** {result['bull']['decision']} (Conf: {result['bull']['confidence']:.0f})\n\n{result['bull']['reasoning']}")
        b2.error(f"**🐻 SMC Bear:** {result['bear']['decision']} (Conf: {result['bear']['confidence']:.0f})\n\n{result['bear']['reasoning']}")

        # 4. TAMPILKAN TRADER PLAN
        st.markdown("### 💹 Smart Trader Plan")
        st.info(f"**Decision:** {result['trader']['decision']}\n\n**Reasoning:** {result['trader']['reasoning']}\n\n**Risk:** {result['trader']['key_risk']}")

        # 5. KEPUTUSAN FINAL & TP/SL KARTU
        st.markdown("### 👨‍⚖️ SMC Final Judge Decision")
        final = result["final"]
        if final == "LONG": st.success(f"🚀 **LONG {symbol}** — Confidence: {result['portfolio']['confidence']:.0f}")
        elif final == "SHORT": st.error(f"🩸 **SHORT {symbol}** — Confidence: {result['portfolio']['confidence']:.0f}")
        else: st.warning(f"⚖️ **WAIT {symbol}** — Confidence: {result['portfolio']['confidence']:.0f}")
        
        st.write(result["portfolio"]["reasoning"])
        
        if final in {"LONG", "SHORT"}:
            e1, e2, e3, e4 = st.columns(4)
            e1.metric("Dynamic Entry", f"${result['entry']:,.8f}")
            e2.metric("Dynamic SL", f"${result['sl']:,.8f}")
            e3.metric("Dynamic TP", f"${result['tp']:,.8f}")
            e4.metric("Risk / Reward", f"1:{result['rr']:.1f}")

            if st.button(f"💾 Simpan Signal {symbol}"): 
                save_signal(result)
                st.success("Tersimpan ke V6 SMC Journal.")
        st.divider()

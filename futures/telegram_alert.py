"""Telegram Bot alert dispatcher for valid BUY (LONG) and SELL (SHORT) Futures setups."""
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from .models import TIMEFRAMES
from . import history

WIB = ZoneInfo("Asia/Jakarta")

MODE_LABELS = {
    "SCALPING": "SCALPER",
    "INTRADAY": "INTRADAY",
    "SWING": "SWING",
}
MODE_TRADING_LABEL = MODE_LABELS


def trading_type_label(mode: str) -> str:
    """Return user-facing trading type label: SCALPER, INTRADAY, or SWING."""
    m = str(mode or "").strip().upper()
    return MODE_LABELS.get(m, m or "INTRADAY")


def action_side_label(direction: str) -> str:
    """Return clear BUY / SELL label from LONG / SHORT direction."""
    d = str(direction or "").strip().upper()
    if d == "LONG":
        return "BUY / LONG"
    if d == "SHORT":
        return "SELL / SHORT"
    return "WAIT"


def load_telegram_credentials(getter) -> tuple[str, str]:
    """Load Telegram Bot Token and Chat ID from Streamlit secrets or environment variables."""
    token = ""
    chat_id = ""
    for k in ("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TG_TOKEN", "TELEGRAM_TOKEN", "BOT_TOKEN"):
        val = getter(k)
        if val and str(val).strip() and not str(val).strip().startswith("123456789:ABCDEF_isi"):
            token = str(val).strip()
            break
    for k in ("TELEGRAM_CHAT_ID", "TG_CHAT_ID", "TG_CHAT", "CHAT_ID"):
        val = getter(k)
        if val and str(val).strip() and not str(val).strip().startswith("-1001234567890_isi"):
            chat_id = str(val).strip()
            break
    return token, chat_id


def load_telegram_config(getter) -> dict:
    token, chat_id = load_telegram_credentials(getter)
    return {"token": token, "chat_id": chat_id, "configured": bool(token and chat_id)}


def should_send_telegram_alert(final_status: str, result, ai: dict | None = None) -> bool:
    """
    Strictly return True ONLY if the setup passed both Master Prompt rules and AI review as BUY (LONG) or SELL (SHORT).
    WAIT and NO_TRADE are never forwarded to Telegram.
    """
    if str(final_status or "").upper() != "VALID":
        return False
    if result is None or getattr(result, "status", "") != "VALID":
        return False
    direction = str(getattr(result, "direction", "") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return False
    plan = getattr(result, "plan", None)
    if not isinstance(plan, dict) or not plan.get("entry") or not plan.get("sl"):
        return False
    if ai is not None:
        if not ai.get("ok") or str(ai.get("decision", "")).upper() != direction:
            return False
    return True


def _fmt_num(x) -> str:
    if x is None:
        return "-"
    try:
        return f"{float(x):,.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(x)


def _fmt_wib(ms: int) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, WIB).strftime("%d %b %Y, %H:%M:%S WIB")
    except Exception:
        return "-"


def format_telegram_alert(result, ai: dict | None = None) -> str:
    """Format a structured Indonesian Telegram alert with SCALPER / INTRADAY / SWING label and BUY / SELL action."""
    p = result.plan or {}
    mode = str(p.get("mode") or result.meta.get("mode") or "INTRADAY").upper()
    t_label = trading_type_label(mode)
    direction = str(p.get("direction") or result.direction or "").upper()
    side_label = action_side_label(direction)
    symbol = p.get("symbol") or result.meta.get("symbol") or "UNKNOWN"
    bias_tf, exec_tf = TIMEFRAMES.get(mode, ("-", "-"))
    emoji = "🟢" if direction == "LONG" else "🔴"

    ai_slot = f"Slot #{ai.get('slot')}" if isinstance(ai, dict) and ai.get("slot") else "Groq AI"
    ai_model = ai.get("model") if isinstance(ai, dict) and ai.get("model") else "Default"
    ai_reasons = [str(r) for r in (ai.get("reasons") or [])] if isinstance(ai, dict) else []

    lines = [
        f"🚨 [{t_label}] SINYAL {side_label} — {symbol}",
        f"{emoji} Status: VALID (Lolos Master Prompt v1 + AI Groq)",
        f"🏷️ Jenis Trading: {t_label} (Mode {mode} · Bias {bias_tf} / Eksekusi {exec_tf})",
        f"📈 Posisi / Aksi: {side_label}",
        f"🕒 Waktu Sinyal: {_fmt_wib(result.meta.get('time_ms', 0))}",
        "",
        "📌 RENCANA ORDER BERSYARAT (STOP-LIMIT):",
        f"• Trigger Entry (Last): {_fmt_num(p.get('entry_trigger'))} USDT",
        f"• Limit Entry Maks/Min (E): {_fmt_num(p.get('entry'))} USDT",
        f"• Stop-Loss (Mark Price): {_fmt_num(p.get('sl'))} USDT",
        f"• TP1 (2R · Tutup 50%): {_fmt_num(p.get('tp1'))} USDT (Qty: {_fmt_num(p.get('tp1_qty'))})",
        f"• TP2 (3R · Tutup 25%): {_fmt_num(p.get('tp2'))} USDT (Qty: {_fmt_num(p.get('tp2_qty'))})",
        f"• Runner (Sisa 25%): Trailing 2 ATR setelah TP2 (Qty: {_fmt_num(p.get('runner_qty'))})",
        "",
        "⚖️ RISIKO, KUANTITAS & MARGIN:",
        f"• Kuantitas Total (Q): {_fmt_num(p.get('qty'))} ({p.get('leverage', 2)}x Isolated)",
        f"• Risiko Terukur: {_fmt_num(p.get('risk'))} USDT (Batas B: {_fmt_num(p.get('risk_budget'))} USDT)",
        f"• Margin Perkiraan: {_fmt_num(p.get('margin'))} USDT",
        f"• Likuidasi / Buffer: {_fmt_num(p.get('liquidation'))} USDT (Buffer: {_fmt_num(p.get('liquidation_buffer'))})",
        f"• RR Bersih ke TP1: {_fmt_num(p.get('net_rr_tp1'))}R",
        f"• Berlaku s/d: {_fmt_wib(p.get('expires_ms', 0))}",
        "",
        f"🤖 ANALISIS AI GROQ ({ai_slot} · {ai_model}):",
    ]
    for reason in ai_reasons[:4]:
        lines.append(f"• {reason}")
    lines.append("")
    lines.append("⚠️ Disiplin Master Prompt: Gunakan Stop-Limit, jangan kejar harga jika melewati limit E.")
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str, session=None) -> tuple[bool, str]:
    """Send a plain-text message to Telegram Bot API."""
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum ada di Streamlit Secrets."
    http = session or requests
    try:
        resp = http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            return False, f"Telegram HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        body = resp.json()
        if body.get("ok"):
            return True, "Alert Telegram terkirim."
        return False, str(body.get("description") or "Gagal mengirim Telegram.")
    except Exception as exc:
        return False, f"Gagal menghubungi Telegram: {str(exc)[:180]}"


def dispatch_valid_trade_alert(
    db_path,
    result,
    final_status: str,
    ai: dict | None,
    token: str,
    chat_id: str,
    session=None,
) -> dict:
    """
    Check if the result is a valid BUY (LONG) or SELL (SHORT) setup.
    If WAIT or NO_TRADE, skip immediately without sending anything to Telegram.
    If VALID BUY/SELL and not yet sent for this signal candle, send to Telegram and record in SQLite.
    """
    if not should_send_telegram_alert(final_status, result, ai):
        return {
            "sent": False,
            "skipped": True,
            "reason": "Status WAIT/NO_TRADE — tidak dikirim ke Telegram sesuai aturan.",
        }

    p = result.plan
    alert_id = f"{p['symbol']}:{p['mode']}:{p['direction']}:{p['signal_close_ms']}"
    try:
        if db_path and history.is_alert_sent(db_path, alert_id):
            return {
                "sent": False,
                "skipped": True,
                "alert_id": alert_id,
                "reason": "Alert untuk sinyal candle ini sudah pernah dikirim ke Telegram.",
            }
    except Exception:
        pass

    msg = format_telegram_alert(result, ai)
    ok, detail = send_telegram_message(token, chat_id, msg, session=session)
    if ok and db_path:
        try:
            history.record_alert_sent(
                db_path,
                alert_id=alert_id,
                at_ms=int(result.meta.get("time_ms", 0)),
                symbol=p["symbol"],
                mode=p["mode"],
                direction=p["direction"],
                message=msg,
            )
        except Exception:
            pass
    return {
        "sent": ok,
        "skipped": False,
        "alert_id": alert_id,
        "trading_type": trading_type_label(p["mode"]),
        "action": action_side_label(p["direction"]),
        "detail": detail,
        "message": msg,
    }

"""Narrow app integration: no changes to momentum, prompts or trade gates.

Supports both manual-tracking Telegram queuing and optional autonomous execution
when `AUTONOMOUS_TRADING_ENABLED=true`.
"""
from decimal import Decimal
import time

from .config import AutonomousConfig, Config
from .orchestrator import AutonomousSystemOrchestrator
from .store import Store
from .telegram import publish_signal
from .worker import bind_bot


def queue_saved_signal(db_path, getter, signal_id, legacy_text=""):
    cfg = Config.load(getter)
    cfg.require_worker()
    store = Store(db_path)
    bind_bot(store, cfg.token)
    row = store.one("SELECT * FROM signals WHERE id=?", (signal_id,))
    if row is None:
        return False, "Sinyal tersimpan tidak ditemukan."
    key = publish_signal(store, cfg, row, legacy_text)
    delivery = store.one("SELECT state FROM mt_outbox WHERE id=?", (key,))
    status = delivery["state"]

    # If autonomous execution is enabled, evaluate candidate entry through the 10-step pipeline
    auto_cfg = AutonomousConfig.load(getter)
    if auto_cfg.autonomous_enabled and row.get("final_decision") in {"LONG", "SHORT"}:
        try:
            orch = AutonomousSystemOrchestrator(store, cfg, auto_cfg)
            now = store.clock()
            sig_payload = {
                "id": row["id"],
                "analysis_id": row.get("analysis_id"),
                "symbol": row["symbol"],
                "timeframe": row.get("timeframe", "5m/15m/1h"),
                "final": row["final_decision"],
                "entry": row["entry"],
                "sl": row["stop_loss"],
                "tp": row["take_profit"],
                "rr": row.get("risk_reward"),
                "signal_timestamp": now,
                "gate_report": {"approved": True},
                "portfolio": {"decision": row["final_decision"], "confidence": row.get("confidence", 80)},
            }
            orch.live_guard.process_candidate_entry(
                signal=sig_payload,
                market_price=Decimal(str(row["entry"])),
                market_timestamp=now,
                instrument=auto_cfg.default_instrument,
            )
        except Exception as exc:
            store.audit("BRIDGE", "AUTONOMOUS_BRIDGE_ERROR", {"signal_id": signal_id, "error": str(exc)}, environment=auto_cfg.execution_mode, severity="ERROR")

    return status == "SENT", f"Manual tracking Telegram: {status}. Worker terpisah menangani pengiriman."


def retry_saved_analysis(db_path, getter, analysis_id):
    store = Store(db_path)
    row = store.one("SELECT id FROM signals WHERE analysis_id=?", (analysis_id,))
    if not row:
        return False, "Signal ID tidak ditemukan; tidak membuat tracker dari pattern fallback."
    return queue_saved_signal(db_path, getter, row["id"])

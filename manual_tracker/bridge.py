"""Narrow app integration: no changes to momentum, prompts or trade gates."""
from .config import Config
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
    return status == "SENT", f"Manual tracking Telegram: {status}. Worker terpisah menangani pengiriman."


def retry_saved_analysis(db_path, getter, analysis_id):
    store = Store(db_path)
    row = store.one("SELECT id FROM signals WHERE analysis_id=?", (analysis_id,))
    if not row:
        return False, "Signal ID tidak ditemukan; tidak membuat tracker dari pattern fallback."
    return queue_saved_signal(db_path, getter, row["id"])

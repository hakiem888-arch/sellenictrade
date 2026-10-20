"""Run with python -m manual_tracker --worker --db /absolute/trading_v5.db."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading
import time

from .config import AutonomousConfig, Config
from .interactions import Interactions
from .market import BinancePollingFeed
from .monitor import Monitor
from .orchestrator import AutonomousSystemOrchestrator
from .store import Store
from .telegram import Dispatcher, TelegramAPI, TelegramError


def bind_bot(store, token):
    bot = token.split(":", 1)[0]
    with store.tx() as con:
        old = con.execute("SELECT value FROM mt_runtime WHERE key='bot_binding'", ()).fetchone()
        if old and json.loads(old["value"]) != bot:
            raise ValueError("Database tracker terikat ke bot berbeda. Gunakan bot semula atau database terpisah.")
        store.runtime("bot_binding", bot, con)


@contextmanager
def worker_lock(token, directory=None):
    # Bot numeric ID stays stable on token rotation. Shared OS lock even when
    # two launchers point at different databases. All processes must use this dir.
    root = Path(directory or os.getenv("MANUAL_LOCK_DIR") or (Path(tempfile.gettempdir()) / "secretagent-manual-locks"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = hashlib.sha256(token.split(":", 1)[0].encode()).hexdigest()[:24]
    fd = os.open(str(root / (identity + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(" "); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Worker bot ini sudah berjalan. Jangan jalankan getUpdates consumer lain.") from None
        yield
    finally:
        handle.close()


class Poller:
    def __init__(self, store, config, api, auto_config=None):
        self.store, self.config, self.api = store, config, api
        self.handler = Interactions(store, config, auto_config=auto_config)

    def preflight(self):
        info = self.api.call("getWebhookInfo", {})
        if not isinstance(info, dict) or info.get("url"):
            raise RuntimeError("WEBHOOK_CONFLICT: gunakan bot khusus tanpa webhook; webhook tidak dihapus otomatis.")

    def once(self):
        row = self.store.one("SELECT value FROM mt_runtime WHERE key=?", ("offset:" + self.handler.bot_id,))
        offset = json.loads(row["value"]) if row else 0
        updates = self.api.call("getUpdates", {"offset": offset, "timeout": 25, "limit": 100, "allowed_updates": ["message", "callback_query"]})
        if not isinstance(updates, list):
            raise TelegramError("INVALID_UPDATES")
        for update in sorted(updates, key=lambda u: u["update_id"]):
            # Transaction commits mutations, reply outbox, dedupe and offset
            # together. A database exception prevents acknowledgement/replay loss.
            self.handler.process(update)
        self.store.runtime("telegram", {"state": "POLLING"})

    def run(self, stop):
        ready, backoff = False, 1
        while not stop.is_set():
            try:
                if not ready:
                    self.preflight()
                    ready = True
                self.once()
                backoff = 1
            except RuntimeError:
                self.store.runtime("telegram", {"state": "BLOCKED_WEBHOOK", "action": "Perbaiki konfigurasi lalu restart worker."})
                return
            except TelegramError as e:
                if e.code == 409:
                    self.store.runtime("telegram", {"state": "BLOCKED_CONFLICT", "action": "Hentikan consumer bot lain, lalu restart worker."})
                    return
                if e.code in {401, 403}:
                    self.store.runtime("telegram", {"state": "BLOCKED_AUTH"})
                    return
                self.store.runtime("telegram", {"state": "RETRYING", "error": str(e)})
                try:
                    delay = max(backoff, float(e.retry_after or 0))
                except (ValueError, TypeError):
                    delay = backoff
                stop.wait(min(delay, 300))
                backoff = min(backoff * 2, 30)
            except Exception:
                # Do not print exception bodies: request URLs may contain tokens.
                try:
                    self.store.runtime("telegram", {"state": "RETRYING", "error": "DATABASE_OR_UPDATE_ERROR"})
                except Exception:
                    pass
                stop.wait(backoff)
                backoff = min(backoff * 2, 30)


class Worker:
    def __init__(self, store, config, api=None, feed=None, auto_config=None):
        self.store, self.config = store, config
        self.auto_config = auto_config or AutonomousConfig.load()
        self.api = api or TelegramAPI(config.token)
        self.feed = feed or BinancePollingFeed(config, clock=store.clock)
        self.monitor = Monitor(store, config, self.feed)
        self.dispatcher = Dispatcher(store, config, self.api)
        self.poller = Poller(store, config, self.api, auto_config=self.auto_config)
        self.orchestrator = AutonomousSystemOrchestrator(store, config, self.auto_config, rest_feed=self.feed)
        self.stop = threading.Event()
        self._last_reconcile = 0.0

    def _autonomous_cycle(self):
        if not self.auto_config.autonomous_enabled:
            return
        now = self.store.clock()
        env = self.auto_config.execution_mode
        inst = self.auto_config.default_instrument
        # Periodic account sync & position reconciliation
        if (now - self._last_reconcile) >= self.auto_config.reconcile_interval_seconds:
            snap = self.orchestrator.account.sync_account(inst, environment=env)
            self.orchestrator.account.reconcile(snap, trigger_source="PERIODIC")
            self._last_reconcile = now

        # Evaluate trailing stops on active autonomous positions
        active_pos = self.store.rows(
            "SELECT * FROM at_positions WHERE environment=? AND position_state IN ('PROTECTED_ACTIVE','PROTECTION_REQUIRED')",
            (env,),
        )
        for pos in active_pos:
            try:
                obs = self.feed.observation({"instrument": pos["instrument"], "symbol": pos["symbol"], "multiplier": pos["multiplier"]})
                self.orchestrator.trailing.evaluate_autonomous_position(pos["id"], obs)
            except Exception:
                pass

    def _market_loop(self):
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                self.monitor.cycle()
                self._autonomous_cycle()
            except Exception:
                try:
                    self.store.runtime("market", {"state": "ERROR", "error": "DATABASE_OR_MONITOR_FAILURE"})
                except Exception:
                    pass
            self.stop.wait(max(0.2, self.config.poll_seconds - (time.monotonic() - start)))

    def _delivery_loop(self):
        while not self.stop.is_set():
            try:
                self.dispatcher.drain()
                self.store.runtime("delivery", {"state": "RUNNING"})
            except Exception:
                try:
                    self.store.runtime("delivery", {"state": "ERROR", "error": "DATABASE_OR_DISPATCH_FAILURE"})
                except Exception:
                    pass
            self.stop.wait(0.25)

    def run(self):
        self.config.require_worker()
        with worker_lock(self.config.token):
            bind_bot(self.store, self.config.token)
            self.dispatcher.recover_inflight()
            self.monitor.recover()
            if self.auto_config.autonomous_enabled:
                self.orchestrator.recovery.recover_on_startup()
            threads = [threading.Thread(target=fn, daemon=True, name=name) for fn, name in
                       [(self._market_loop, "manual-market"), (self._delivery_loop, "manual-delivery"), (lambda: self.poller.run(self.stop), "manual-telegram")]]
            for thread in threads:
                thread.start()
            try:
                while not self.stop.wait(1):
                    self.store.runtime("worker", {"state": "RUNNING", "pid": os.getpid()})
            finally:
                self.stop.set()
                for thread in threads:
                    thread.join(timeout=40)
                self.store.runtime("worker", {"state": "STOPPED"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="SecretAgent v5.5 Manual Tracker & Autonomous Trading Worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--worker", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--migrate", action="store_true")
    mode.add_argument("--check", action="store_true", help="Validate configuration, no external services")
    parser.add_argument("--db", default=os.getenv("MANUAL_DB_FILE", "trading_v5.db"))
    args = parser.parse_args(argv)
    try:
        cfg = Config.load()
        auto_cfg = AutonomousConfig.load()
        if args.check:
            cfg.require_worker()
            print(json.dumps({
                "configuration": "VALID",
                "execution_mode": auto_cfg.execution_mode,
                "live_trading_enabled": auto_cfg.live_trading_enabled,
                "autonomous_enabled": auto_cfg.autonomous_enabled,
                "trailing_mode": auto_cfg.trailing_mode,
                "external_services": "NOT_TESTED",
                "live_orders": False,
            }))
            return 0
        if args.migrate and Path(args.db).is_file():
            backup = str(args.db) + ".manual-backup-" + str(time.time_ns())
            with sqlite3.connect(args.db) as src, sqlite3.connect(backup) as dest:
                src.backup(dest)
            print("Database backup: " + backup)
        store = Store(args.db)
        if args.migrate:
            print("mt_* and at_* migration complete; legacy tables unchanged.")
            return 0
        if args.status:
            orch = AutonomousSystemOrchestrator(store, cfg, auto_cfg)
            op = orch.operational_summary()
            print(json.dumps({
                "runtime": store.rows("SELECT * FROM mt_runtime"),
                "positions": store.rows("SELECT state,COUNT(*) AS total FROM mt_positions GROUP BY state"),
                "delivery": store.rows("SELECT state,COUNT(*) AS total FROM mt_outbox GROUP BY state"),
                "autonomous": {
                    "execution_mode": op["execution_mode"],
                    "live_authorization_state": op["live_authorization_state"],
                    "account_equity_usdt": op["account_equity_usdt"],
                    "open_positions_count": op["open_positions_count"],
                    "active_orders_count": op["active_orders_count"],
                    "protective_order_status": op["protective_order_status"],
                    "system_health": op["system_health"],
                    "kill_switch_active": op["kill_switch_active"],
                },
            }, indent=2))
            return 0
        worker = Worker(store, cfg, auto_config=auto_cfg)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: worker.stop.set())
        worker.run()
        return 0
    except (ValueError, RuntimeError) as e:
        print(str(e))
        return 2
    except Exception:
        print("Worker gagal memulai. Periksa path database, dependensi, dan konfigurasi; detail sensitif tidak dicetak.")
        return 2

"""
SQLite connection policy, versioned atomic schema migrations, backup/restore,
and provenance repositories for AI Consensus Trading (PH02-02 / F25, F26, T17, T18).
Strictly zero side-effects on import.
"""

import hashlib
import math
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .config import (
    DEFAULT_DB_FILE,
    LEGACY_ACCOUNTING_MODE,
    MODEL_TIERS,
    PROMPT_VERSION,
    SCHEMA_VERSION_AI,
    STRATEGY_VERSION,
    compute_model_config_hash,
    redact_secrets,
)
from .domain import (
    compute_signal_idempotency_key,
    is_finite_number,
    validate_trade_plan_gate,
)
from .paper import (
    ACTIVE_POSITION_STATES,
    CLOSED_TRADE_STATES,
    IntrabarTieBreaker,
    PaperPositionState,
    REALISTIC_ACCOUNTING_MODE,
    REALISTIC_ACCOUNTING_QUALITY,
    TERMINAL_POSITION_STATES,
    compute_confidence_calibration_report,
    compute_expectancy_and_drawdown_analytics,
    compute_realized_trade_accounting,
    evaluate_position_on_bars,
    validate_position_transition,
)

CURRENT_SCHEMA_VERSION: int = 8


def connect_db(
    db_file: str = DEFAULT_DB_FILE,
    busy_timeout_ms: int = 5000,
    foreign_keys: bool = True,
    enable_wal: bool = False,
) -> sqlite3.Connection:
    timeout_s = max(1.0, float(busy_timeout_ms) / 1000.0)
    con = sqlite3.connect(db_file, timeout=timeout_s)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    if foreign_keys:
        con.execute("PRAGMA foreign_keys = ON")
    if enable_wal and db_file != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
    else:
        con.execute("PRAGMA synchronous = OFF")
        con.execute("PRAGMA journal_mode = MEMORY")
    return con


def backup_database(source_db_file: str, backup_db_file: Optional[str] = None) -> Dict[str, Any]:
    if not source_db_file or source_db_file == ":memory:":
        raise ValueError("Backup memerlukan file database berbasis disk.")
    src_path = Path(source_db_file)
    if not src_path.exists():
        raise FileNotFoundError(f"Database sumber tidak ditemukan: {source_db_file}")

    dest_path = Path(backup_db_file) if backup_db_file else Path(f"{source_db_file}.backup")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()

    src_con = sqlite3.connect(str(src_path))
    dest_con = sqlite3.connect(str(dest_path))
    try:
        src_con.backup(dest_con)
    finally:
        dest_con.close()
        src_con.close()

    raw_bytes = dest_path.read_bytes()
    return {
        "backup_path": str(dest_path),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "bytes": len(raw_bytes),
    }


def verify_database_integrity(db_file: str = DEFAULT_DB_FILE) -> Dict[str, Any]:
    con = connect_db(db_file)
    try:
        integrity_rows = con.execute("PRAGMA integrity_check").fetchall()
        integrity_messages = [str(r[0]) for r in integrity_rows]
        integrity_ok = integrity_messages == ["ok"]

        fk_rows = con.execute("PRAGMA foreign_key_check").fetchall()
        fk_violations = [dict(r) for r in fk_rows]
        fk_ok = len(fk_violations) == 0

        existing_tables = {
            str(r[0])
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        counts: Dict[str, int] = {}
        for tbl in (
            "signals",
            "agent_votes",
            "memories",
            "pattern_events",
            "schema_migrations",
            "analysis_runs",
            "agent_runs",
            "usage_events",
            "quota_scopes",
            "budget_reservations",
            "dag_checkpoints",
            "paper_positions",
            "paper_account_ledger",
            "monitor_runtime_state",
            "telegram_outbox",
            "risk_guard_state",
        ):
            if tbl in existing_tables:
                counts[tbl] = int(con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])

        schema_ver = 0
        if "schema_migrations" in existing_tables:
            row = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_ver = int(row[0] or 0) if row else 0

        return {
            "ok": bool(integrity_ok and fk_ok),
            "integrity_ok": integrity_ok,
            "integrity_messages": integrity_messages,
            "foreign_keys_ok": fk_ok,
            "foreign_key_violations": fk_violations,
            "schema_version": schema_ver,
            "counts": counts,
        }
    finally:
        con.close()


def restore_database(backup_db_file: str, target_db_file: str) -> Dict[str, Any]:
    bpath = Path(backup_db_file)
    if not bpath.exists():
        raise FileNotFoundError(f"File backup tidak ditemukan: {backup_db_file}")

    pre_check = verify_database_integrity(str(bpath))
    if not pre_check["ok"]:
        raise RuntimeError(f"File backup gagal pemeriksaan integritas: {pre_check}")

    tpath = Path(target_db_file)
    tpath.parent.mkdir(parents=True, exist_ok=True)

    bcon = sqlite3.connect(str(bpath))
    tcon = sqlite3.connect(str(tpath))
    try:
        bcon.backup(tcon)
    finally:
        tcon.close()
        bcon.close()

    post_check = verify_database_integrity(str(tpath))
    return {
        "restored": post_check["ok"],
        "target_db_file": str(tpath),
        "integrity": post_check,
    }


def _migration_001_baseline_and_legacy_provenance(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            scanner_score REAL,
            final_decision TEXT NOT NULL,
            confidence REAL,
            entry REAL,
            stop_loss REAL,
            take_profit REAL,
            risk_reward REAL,
            market_regime TEXT,
            score_consensus REAL,
            reasoning TEXT,
            closed_at TEXT,
            result TEXT,
            r_multiple REAL,
            exit_price REAL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL,
            agent TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence REAL,
            reasoning TEXT,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT,
            memory_type TEXT NOT NULL,
            content TEXT NOT NULL,
            signal_id INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pattern_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            mode TEXT NOT NULL,
            state TEXT NOT NULL,
            direction TEXT NOT NULL,
            score REAL NOT NULL,
            fingerprint TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
        """
    )

    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
    had_origin_col = "origin" in existing_cols

    additive_columns = [
        ("smc_score", "ALTER TABLE signals ADD COLUMN smc_score REAL"),
        ("entry_status", "ALTER TABLE signals ADD COLUMN entry_status TEXT"),
        ("entry_zone_low", "ALTER TABLE signals ADD COLUMN entry_zone_low REAL"),
        ("entry_zone_high", "ALTER TABLE signals ADD COLUMN entry_zone_high REAL"),
        ("smc_bias", "ALTER TABLE signals ADD COLUMN smc_bias TEXT"),
        ("analysis_id", "ALTER TABLE signals ADD COLUMN analysis_id TEXT"),
        ("idempotency_key", "ALTER TABLE signals ADD COLUMN idempotency_key TEXT"),
        ("raw_model_decision", "ALTER TABLE signals ADD COLUMN raw_model_decision TEXT"),
        ("veto_reasons", "ALTER TABLE signals ADD COLUMN veto_reasons TEXT"),
        ("accounting_mode", "ALTER TABLE signals ADD COLUMN accounting_mode TEXT"),
        ("origin", "ALTER TABLE signals ADD COLUMN origin TEXT"),
        ("accounting_quality", "ALTER TABLE signals ADD COLUMN accounting_quality TEXT"),
        ("strategy_version", "ALTER TABLE signals ADD COLUMN strategy_version TEXT"),
        ("prompt_version", "ALTER TABLE signals ADD COLUMN prompt_version TEXT"),
        ("schema_version", "ALTER TABLE signals ADD COLUMN schema_version TEXT"),
        ("position_size", "ALTER TABLE signals ADD COLUMN position_size REAL"),
        ("notional_value", "ALTER TABLE signals ADD COLUMN notional_value REAL"),
        ("risk_budget", "ALTER TABLE signals ADD COLUMN risk_budget REAL"),
        ("net_rr", "ALTER TABLE signals ADD COLUMN net_rr REAL"),
        ("instrument_type", "ALTER TABLE signals ADD COLUMN instrument_type TEXT"),
        ("trade_plan_json", "ALTER TABLE signals ADD COLUMN trade_plan_json TEXT"),
    ]
    for col, ddl in additive_columns:
        if col not in existing_cols:
            con.execute(ddl)

    if not had_origin_col:
        con.execute(
            """
            UPDATE signals
            SET origin = COALESCE(origin, 'legacy'),
                accounting_quality = COALESCE(accounting_quality, 'legacy_snapshot_only'),
                accounting_mode = COALESCE(accounting_mode, ?)
            WHERE origin IS NULL
            """,
            (LEGACY_ACCOUNTING_MODE,),
        )
    else:
        con.execute(
            """
            UPDATE signals
            SET origin = 'legacy',
                accounting_quality = COALESCE(accounting_quality, 'legacy_snapshot_only')
            WHERE origin IS NULL AND analysis_id IS NULL
            """
        )

    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_analysis_id "
        "ON signals(analysis_id) WHERE analysis_id IS NOT NULL"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_idempotency_key "
        "ON signals(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_pattern_events_fingerprint "
        "ON pattern_events(fingerprint)"
    )

    con.execute(
        """
        DELETE FROM pattern_events
        WHERE id NOT IN (
            SELECT MAX(id) FROM pattern_events GROUP BY fingerprint
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pattern_events_fingerprint_unique "
        "ON pattern_events(fingerprint)"
    )


def _migration_002_analysis_and_agent_runs_provenance(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_runs (
            analysis_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            bar_time TEXT,
            scanner_score REAL,
            score_consensus REAL,
            status TEXT NOT NULL,
            raw_model_decision TEXT NOT NULL,
            gated_decision TEXT NOT NULL,
            final_confidence REAL,
            veto_reasons TEXT,
            gate_report_json TEXT,
            modes_json TEXT,
            packet_snapshot_json TEXT,
            budget_summary_json TEXT,
            usage_total_json TEXT,
            model_config_hash TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'v6_runtime',
            accounting_quality TEXT NOT NULL DEFAULT 'legacy_r_snapshot',
            idempotency_key TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT NOT NULL,
            role TEXT NOT NULL,
            tier TEXT,
            status TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence REAL,
            reasoning TEXT,
            key_risk TEXT,
            evidence_json TEXT,
            provider TEXT,
            model_id TEXT,
            finish_reason TEXT,
            requested_max_tokens INTEGER,
            effective_max_tokens INTEGER,
            usage_json TEXT,
            prompt_version TEXT,
            schema_version TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(analysis_id) REFERENCES analysis_runs(analysis_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT,
            role TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_id TEXT,
            status TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            usage_known INTEGER NOT NULL DEFAULT 0,
            cost_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(analysis_id) REFERENCES analysis_runs(analysis_id)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_analysis_id ON agent_runs(analysis_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_analysis_id ON usage_events(analysis_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_analysis_runs_symbol ON analysis_runs(symbol)")


def _migration_003_quota_scopes_and_budget_reservations(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_scopes (
            scope_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            max_requests_per_day INTEGER,
            max_tokens_per_day INTEGER,
            max_physical_attempts_per_run INTEGER,
            requests_today INTEGER DEFAULT 0,
            tokens_today INTEGER DEFAULT 0,
            physical_attempts_total INTEGER DEFAULT 0,
            exhausted_until_epoch REAL DEFAULT 0.0,
            exhausted_reason TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS budget_reservations (
            reservation_id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            role TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            estimated_input_tokens INTEGER DEFAULT 0,
            reserved_completion_tokens INTEGER DEFAULT 0,
            reserved_physical_attempts INTEGER DEFAULT 1,
            actual_input_tokens INTEGER,
            actual_output_tokens INTEGER,
            actual_reasoning_tokens INTEGER,
            actual_total_tokens INTEGER,
            usage_known INTEGER DEFAULT 0,
            cost_status TEXT NOT NULL DEFAULT 'UNKNOWN_USAGE',
            deadline_epoch REAL,
            created_at TEXT NOT NULL,
            reconciled_at TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_budget_reservations_analysis_id ON budget_reservations(analysis_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_budget_reservations_scope_id ON budget_reservations(scope_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_budget_reservations_status ON budget_reservations(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_quota_scopes_provider ON quota_scopes(provider)")


def _migration_004_dag_checkpoints(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS dag_checkpoints (
            analysis_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            dependencies_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (analysis_id, node_id)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_dag_checkpoints_analysis_id ON dag_checkpoints(analysis_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dag_checkpoints_stage ON dag_checkpoints(analysis_id, stage)")


def _migration_005_paper_positions_and_ledger(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            position_id TEXT PRIMARY KEY,
            signal_id INTEGER UNIQUE,
            analysis_id TEXT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL DEFAULT '15m',
            mode TEXT NOT NULL DEFAULT 'scalping',
            direction TEXT NOT NULL,
            state TEXT NOT NULL,
            instrument_type TEXT,
            entry REAL,
            stop_loss REAL,
            take_profit REAL,
            entry_zone_low REAL,
            entry_zone_high REAL,
            quantity REAL NOT NULL DEFAULT 0.0,
            notional_value REAL NOT NULL DEFAULT 0.0,
            margin_required REAL NOT NULL DEFAULT 0.0,
            risk_budget REAL NOT NULL DEFAULT 0.0,
            fee_pct_round_trip REAL NOT NULL DEFAULT 0.08,
            slippage_pct_round_trip REAL NOT NULL DEFAULT 0.04,
            spread_pct REAL NOT NULL DEFAULT 0.0,
            entry_fill_price REAL,
            exit_price REAL,
            exit_fill_price REAL,
            exit_reason TEXT,
            entry_fee REAL DEFAULT 0.0,
            exit_fee REAL DEFAULT 0.0,
            total_fees REAL DEFAULT 0.0,
            slippage_cost REAL DEFAULT 0.0,
            spread_cost REAL DEFAULT 0.0,
            total_cost REAL DEFAULT 0.0,
            gross_pnl REAL,
            net_pnl REAL,
            gross_r_multiple REAL,
            net_r_multiple REAL,
            bars_waited INTEGER NOT NULL DEFAULT 0,
            bars_held INTEGER NOT NULL DEFAULT 0,
            max_wait_bars INTEGER,
            max_hold_bars INTEGER,
            expires_at TEXT,
            time_stop_at TEXT,
            intrabar_conflict INTEGER NOT NULL DEFAULT 0,
            tie_breaker_policy TEXT NOT NULL DEFAULT 'SL_FIRST_CONSERVATIVE',
            created_at TEXT NOT NULL,
            opened_at TEXT,
            closed_at TEXT,
            last_evaluated_bar_time TEXT,
            origin TEXT NOT NULL DEFAULT 'v6_runtime',
            accounting_quality TEXT NOT NULL DEFAULT 'v6_realistic_ledger',
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_account_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            position_id TEXT,
            signal_id INTEGER,
            symbol TEXT,
            equity_before REAL NOT NULL,
            equity_after REAL NOT NULL,
            cash_delta REAL NOT NULL DEFAULT 0.0,
            reserved_margin REAL NOT NULL DEFAULT 0.0,
            free_equity REAL NOT NULL,
            gross_pnl REAL DEFAULT 0.0,
            total_cost REAL DEFAULT 0.0,
            net_pnl REAL DEFAULT 0.0,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_paper_positions_state ON paper_positions(state)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_paper_positions_symbol ON paper_positions(symbol)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_paper_account_ledger_pos ON paper_account_ledger(position_id)")

    existing_sig_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
    for col, ddl in [
        ("position_state", "ALTER TABLE signals ADD COLUMN position_state TEXT"),
        ("gross_pnl", "ALTER TABLE signals ADD COLUMN gross_pnl REAL"),
        ("net_pnl", "ALTER TABLE signals ADD COLUMN net_pnl REAL"),
        ("total_fees", "ALTER TABLE signals ADD COLUMN total_fees REAL"),
        ("slippage_cost", "ALTER TABLE signals ADD COLUMN slippage_cost REAL"),
        ("total_cost", "ALTER TABLE signals ADD COLUMN total_cost REAL"),
        ("gross_r_multiple", "ALTER TABLE signals ADD COLUMN gross_r_multiple REAL"),
        ("net_r_multiple", "ALTER TABLE signals ADD COLUMN net_r_multiple REAL"),
        ("exit_reason", "ALTER TABLE signals ADD COLUMN exit_reason TEXT"),
        ("intrabar_conflict", "ALTER TABLE signals ADD COLUMN intrabar_conflict INTEGER DEFAULT 0"),
        ("tie_breaker_policy", "ALTER TABLE signals ADD COLUMN tie_breaker_policy TEXT"),
        ("opened_at", "ALTER TABLE signals ADD COLUMN opened_at TEXT"),
        ("last_evaluated_bar_time", "ALTER TABLE signals ADD COLUMN last_evaluated_bar_time TEXT"),
    ]:
        if col not in existing_sig_cols:
            con.execute(ddl)


def _migration_006_monitor_runtime_state(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_runtime_state (
            worker_id TEXT PRIMARY KEY,
            execution_mode TEXT NOT NULL DEFAULT 'worker',
            status TEXT NOT NULL DEFAULT 'IDLE',
            pid INTEGER,
            interval_seconds INTEGER NOT NULL DEFAULT 45,
            monitor_n INTEGER NOT NULL DEFAULT 8,
            max_validations INTEGER NOT NULL DEFAULT 1,
            cycles_completed INTEGER NOT NULL DEFAULT 0,
            candidates_scanned INTEGER NOT NULL DEFAULT 0,
            ai_validations_run INTEGER NOT NULL DEFAULT 0,
            signals_saved INTEGER NOT NULL DEFAULT 0,
            paper_trades_reconciled INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_ms INTEGER NOT NULL DEFAULT 0,
            last_cycle_at TEXT,
            last_error TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_monitor_runtime_status ON monitor_runtime_state(status)")


def _migration_007_lifecycle_risk_and_telegram_outbox(con: sqlite3.Connection) -> None:
    existing_sig_cols = {row[1] for row in con.execute("PRAGMA table_info(signals)").fetchall()}
    for col, ddl in [
        ("signal_lifecycle_state", "ALTER TABLE signals ADD COLUMN signal_lifecycle_state TEXT"),
        ("invalidation_level", "ALTER TABLE signals ADD COLUMN invalidation_level REAL"),
        ("expires_at", "ALTER TABLE signals ADD COLUMN expires_at TEXT"),
        ("funding_cost", "ALTER TABLE signals ADD COLUMN funding_cost REAL DEFAULT 0.0"),
    ]:
        if col not in existing_sig_cols:
            con.execute(ddl)

    existing_pos_cols = {row[1] for row in con.execute("PRAGMA table_info(paper_positions)").fetchall()}
    for col, ddl in [
        ("market_regime", "ALTER TABLE paper_positions ADD COLUMN market_regime TEXT"),
        ("strategy_version", "ALTER TABLE paper_positions ADD COLUMN strategy_version TEXT"),
        ("funding_rate_pct_per_8h", "ALTER TABLE paper_positions ADD COLUMN funding_rate_pct_per_8h REAL DEFAULT 0.0"),
        ("funding_cost", "ALTER TABLE paper_positions ADD COLUMN funding_cost REAL DEFAULT 0.0"),
        ("invalidation_level", "ALTER TABLE paper_positions ADD COLUMN invalidation_level REAL"),
    ]:
        if col not in existing_pos_cols:
            con.execute(ddl)

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_outbox (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            signal_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            signal_state TEXT,
            timeframe TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            message_hash TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_telegram_outbox_status ON telegram_outbox(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_telegram_outbox_symbol ON telegram_outbox(symbol)")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_guard_state (
            account_id TEXT PRIMARY KEY,
            trading_day_utc TEXT NOT NULL,
            daily_realized_loss_usd REAL NOT NULL DEFAULT 0.0,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            last_rejection_reason TEXT DEFAULT '',
            last_rejected_symbol TEXT DEFAULT '',
            last_rejected_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _migration_008_autonomous_live_execution(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL DEFAULT 'BINANCE',
            market_type TEXT NOT NULL DEFAULT 'FUTURES_USDM',
            execution_mode TEXT NOT NULL DEFAULT 'PAPER',
            total_wallet_balance REAL NOT NULL DEFAULT 0.0,
            available_balance REAL NOT NULL DEFAULT 0.0,
            account_equity REAL NOT NULL DEFAULT 0.0,
            unrealized_pnl REAL NOT NULL DEFAULT 0.0,
            realized_pnl REAL NOT NULL DEFAULT 0.0,
            used_margin REAL NOT NULL DEFAULT 0.0,
            available_margin REAL NOT NULL DEFAULT 0.0,
            open_positions_count INTEGER NOT NULL DEFAULT 0,
            active_orders_count INTEGER NOT NULL DEFAULT 0,
            withdrawal_enabled INTEGER DEFAULT 0,
            sync_ok INTEGER NOT NULL DEFAULT 1,
            is_consistent INTEGER NOT NULL DEFAULT 1,
            api_latency_ms REAL DEFAULT 0.0,
            error_code TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            snapshot_json TEXT,
            synced_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_account_snapshots_mode ON account_snapshots(execution_mode, synced_at)")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_orders (
            client_order_id TEXT PRIMARY KEY,
            exchange_order_id TEXT,
            signal_id TEXT,
            position_id TEXT,
            symbol TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            market_type TEXT NOT NULL DEFAULT 'FUTURES_USDM',
            order_leg TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            status TEXT NOT NULL,
            price REAL,
            stop_price REAL,
            requested_qty REAL NOT NULL,
            executed_qty REAL NOT NULL DEFAULT 0.0,
            avg_fill_price REAL,
            fee_usd REAL NOT NULL DEFAULT 0.0,
            reduce_only INTEGER NOT NULL DEFAULT 0,
            raw_response_json TEXT,
            error_message TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_orders_symbol_status ON live_orders(symbol, status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_orders_position ON live_orders(position_id)")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_positions (
            position_id TEXT PRIMARY KEY,
            signal_id TEXT,
            symbol TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            market_type TEXT NOT NULL DEFAULT 'FUTURES_USDM',
            direction TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL,
            requested_qty REAL NOT NULL DEFAULT 0.0,
            open_qty REAL NOT NULL DEFAULT 0.0,
            closed_qty REAL NOT NULL DEFAULT 0.0,
            entry_price REAL,
            avg_entry_fill_price REAL,
            stop_loss REAL,
            take_profit REAL,
            avg_exit_fill_price REAL,
            notional_value REAL NOT NULL DEFAULT 0.0,
            used_margin REAL NOT NULL DEFAULT 0.0,
            leverage REAL NOT NULL DEFAULT 1.0,
            risk_budget_usd REAL NOT NULL DEFAULT 0.0,
            realized_pnl_usd REAL NOT NULL DEFAULT 0.0,
            unrealized_pnl_usd REAL NOT NULL DEFAULT 0.0,
            total_fees_usd REAL NOT NULL DEFAULT 0.0,
            entry_client_order_id TEXT,
            sl_client_order_id TEXT,
            tp_client_order_id TEXT,
            exit_client_order_id TEXT,
            protection_verified INTEGER NOT NULL DEFAULT 0,
            reconciliation_status TEXT NOT NULL DEFAULT 'OK',
            reconciliation_detail TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            opened_at TEXT,
            closed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_positions_state ON live_positions(lifecycle_state)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_positions_symbol ON live_positions(symbol, execution_mode)")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            account_id TEXT PRIMARY KEY,
            execution_mode TEXT NOT NULL DEFAULT 'PAPER',
            live_trading_authorized INTEGER NOT NULL DEFAULT 0,
            kill_switch_active INTEGER NOT NULL DEFAULT 0,
            kill_switch_reason TEXT DEFAULT '',
            kill_switch_activated_at TEXT,
            kill_switch_verification_status TEXT DEFAULT 'IDLE',
            circuit_breaker_active INTEGER NOT NULL DEFAULT 0,
            active_reasons_json TEXT DEFAULT '[]',
            trading_day_utc TEXT NOT NULL,
            daily_realized_loss_usd REAL NOT NULL DEFAULT 0.0,
            daily_unrealized_loss_usd REAL NOT NULL DEFAULT 0.0,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            consecutive_execution_failures INTEGER NOT NULL DEFAULT 0,
            consecutive_auth_failures INTEGER NOT NULL DEFAULT 0,
            reconciliation_status TEXT NOT NULL DEFAULT 'OK',
            last_reconciled_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            symbol TEXT NOT NULL,
            signal_id TEXT,
            position_id TEXT,
            decision_outcome TEXT NOT NULL,
            state_before TEXT,
            state_after TEXT,
            risk_inputs_json TEXT,
            order_ids_json TEXT,
            exchange_response_json TEXT,
            fills_json TEXT,
            fees_usd REAL DEFAULT 0.0,
            reason TEXT DEFAULT ''
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_execution_audit_log_sym ON execution_audit_log(symbol, created_at)")


MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "001_baseline_and_legacy_provenance", _migration_001_baseline_and_legacy_provenance),
    (2, "002_analysis_and_agent_runs_provenance", _migration_002_analysis_and_agent_runs_provenance),
    (3, "003_quota_scopes_and_budget_reservations", _migration_003_quota_scopes_and_budget_reservations),
    (4, "004_dag_checkpoints", _migration_004_dag_checkpoints),
    (5, "005_paper_positions_and_ledger", _migration_005_paper_positions_and_ledger),
    (6, "006_monitor_runtime_state", _migration_006_monitor_runtime_state),
    (7, "007_lifecycle_risk_and_telegram_outbox", _migration_007_lifecycle_risk_and_telegram_outbox),
    (8, "008_autonomous_live_execution", _migration_008_autonomous_live_execution),
]


def run_migrations(
    db_file: str = DEFAULT_DB_FILE,
    fail_on_version: Optional[int] = None,
) -> Dict[str, Any]:
    con = connect_db(db_file)
    con.isolation_level = None
    applied_now: List[int] = []

    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        con.execute("COMMIT")

        existing = {
            int(row["version"]): str(row["name"])
            for row in con.execute("SELECT version, name FROM schema_migrations").fetchall()
        }

        for version, name, step_fn in MIGRATIONS:
            if version in existing:
                continue
            checksum = hashlib.sha256(f"{version}:{name}".encode("utf-8")).hexdigest()[:16]
            con.execute("BEGIN IMMEDIATE")
            try:
                step_fn(con)
                if fail_on_version is not None and int(fail_on_version) == int(version):
                    raise RuntimeError(f"Injected migration failure at version {version} ({name})")
                now = datetime.now(timezone.utc).isoformat()
                con.execute(
                    "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES(?,?,?,?)",
                    (version, name, checksum, now),
                )
                con.execute("COMMIT")
                applied_now.append(version)
            except Exception:
                con.execute("ROLLBACK")
                raise

        final_ver_row = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        final_version = int(final_ver_row[0] or 0) if final_ver_row else 0
        return {
            "schema_version": final_version,
            "applied_migrations": applied_now,
        }
    finally:
        con.close()


def compute_consensus_score(result: Dict[str, Any]) -> Optional[float]:
    if not isinstance(result, dict):
        return None
    final_dec = str(result.get("final", "WAIT")).upper().strip()
    raw_dec = str(result.get("raw_model_decision") or result.get("portfolio", {}).get("decision") or final_dec).upper().strip()
    target_dir = final_dec if final_dec in {"LONG", "SHORT"} else (raw_dec if raw_dec in {"LONG", "SHORT"} else "WAIT")

    agents: List[Dict[str, Any]] = []
    for a in result.get("analysts") or []:
        if isinstance(a, dict):
            agents.append(a)
    for key in ("bull", "bear", "research_manager", "trader"):
        item = result.get(key)
        if isinstance(item, dict):
            agents.append(item)
    for r in result.get("risk_agents") or []:
        if isinstance(r, dict):
            agents.append(r)
    if isinstance(result.get("portfolio"), dict):
        agents.append(result["portfolio"])

    ok_agents = [a for a in agents if str(a.get("status", "OK")).upper() == "OK"]
    if not ok_agents:
        return None

    if target_dir in {"LONG", "SHORT"}:
        aligned = [
            float(a.get("confidence", 0.0) or 0.0)
            for a in ok_agents
            if str(a.get("decision", "WAIT")).upper().strip() == target_dir
            and is_finite_number(a.get("confidence", 0.0))
        ]
        ratio = len(aligned) / len(ok_agents)
        avg_conf = (sum(aligned) / len(aligned)) if aligned else 0.0
        return round(min(100.0, max(0.0, (ratio * 60.0) + (avg_conf * 0.40))), 2)

    wait_count = sum(1 for a in ok_agents if str(a.get("decision", "WAIT")).upper().strip() == "WAIT")
    return round((wait_count / len(ok_agents)) * 100.0, 2)


def _aggregate_usage_total(agent_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    in_tok = 0
    out_tok = 0
    reas_tok = 0
    tot_tok = 0
    any_known = False
    unknown_count = 0

    for x in agent_entries:
        u = x.get("usage") if isinstance(x.get("usage"), dict) else {}
        if u.get("usage_known"):
            any_known = True
            if isinstance(u.get("input_tokens"), int):
                in_tok += u["input_tokens"]
            if isinstance(u.get("output_tokens"), int):
                out_tok += u["output_tokens"]
            if isinstance(u.get("reasoning_tokens"), int):
                reas_tok += u["reasoning_tokens"]
            if isinstance(u.get("total_tokens"), int):
                tot_tok += u["total_tokens"]
        else:
            unknown_count += 1

    return {
        "input_tokens": in_tok if any_known else None,
        "output_tokens": out_tok if any_known else None,
        "reasoning_tokens": reas_tok if any_known else None,
        "total_tokens": tot_tok if any_known else None,
        "usage_known": any_known,
        "unknown_agent_count": unknown_count,
        "cost_status": "MEASURED" if (any_known and unknown_count == 0) else ("PARTIAL_UNKNOWN" if any_known else "UNKNOWN_USAGE"),
    }


def _find_existing_signal_id(con: sqlite3.Connection, aid: Optional[str], ikey: Optional[str]) -> Optional[int]:
    if aid:
        row = con.execute("SELECT id FROM signals WHERE analysis_id=? LIMIT 1", (aid,)).fetchone()
        if row:
            return int(row["id"])
    if ikey:
        row = con.execute("SELECT id FROM signals WHERE idempotency_key=? LIMIT 1", (ikey,)).fetchone()
        if row:
            return int(row["id"])
    return None


def _resolve_veto_reasons_str(result: Dict[str, Any]) -> str:
    if isinstance(result.get("veto_reasons"), str) and result["veto_reasons"].strip():
        return result["veto_reasons"].strip()
    gr = result.get("gate_report")
    if isinstance(gr, dict):
        codes = gr.get("veto_codes")
        if isinstance(codes, list) and codes:
            return ", ".join(str(c) for c in codes)
        if isinstance(gr.get("veto_summary"), str) and gr["veto_summary"].strip():
            return gr["veto_summary"].strip()
    return "NONE"


def save_signal_record(
    result: Dict[str, Any],
    db_file: str = DEFAULT_DB_FILE,
    analysis_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    extra_secrets: Optional[Iterable[str]] = None,
) -> int:
    run_migrations(db_file)
    sanitized: Dict[str, Any] = redact_secrets(result, extra_secrets=extra_secrets)

    con = connect_db(db_file)
    now = datetime.now(timezone.utc).isoformat()
    aid = analysis_id or sanitized.get("analysis_id")

    raw_dec = str(
        sanitized.get("raw_model_decision")
        or sanitized.get("portfolio", {}).get("decision")
        or sanitized.get("final", "WAIT")
    ).upper().strip()
    veto_reasons_str = _resolve_veto_reasons_str(sanitized)
    acc_mode = str(sanitized.get("accounting_mode") or LEGACY_ACCOUNTING_MODE)
    origin_str = str(sanitized.get("origin") or "v6_runtime")
    acc_quality = str(sanitized.get("accounting_quality") or "legacy_r_snapshot")
    strat_ver = str(sanitized.get("strategy_version") or STRATEGY_VERSION)
    prompt_ver = str(sanitized.get("prompt_version") or PROMPT_VERSION)
    schema_ver = str(sanitized.get("schema_version") or SCHEMA_VERSION_AI)
    model_cfg_hash = str(
        sanitized.get("model_config_hash")
        or compute_model_config_hash(sanitized.get("model_tiers") or MODEL_TIERS)
    )

    final_dec = str(sanitized.get("final", "WAIT")).upper().strip()
    entry_v = sanitized.get("entry")
    sl_v = sanitized.get("sl")
    tp_v = sanitized.get("tp")
    rr_v = sanitized.get("rr", 0.0)
    if final_dec in {"LONG", "SHORT"}:
        spread_pct = sanitized.get("packet", {}).get("order_book", {}).get("spread", 0.0)
        plan_check = validate_trade_plan_gate(final_dec, entry_v, sl_v, tp_v, gross_rr=rr_v or 2.0, spread_pct=spread_pct)
        if not plan_check["valid"]:
            final_dec = "WAIT"
            entry_v, sl_v, tp_v, rr_v = None, None, None, 0.0
            merged_vetoes = [c for c in (veto_reasons_str.split(", ") if veto_reasons_str != "NONE" else []) if c]
            for c in plan_check["veto_codes"]:
                if c not in merged_vetoes:
                    merged_vetoes.append(c)
            veto_reasons_str = ", ".join(merged_vetoes) if merged_vetoes else "INVALID_SL_TP"

    effective_result = {**sanitized, "final": final_dec}
    ikey = idempotency_key or sanitized.get("idempotency_key") or compute_signal_idempotency_key(effective_result)

    consensus_val = sanitized.get("score_consensus")
    if consensus_val is None or consensus_val == sanitized.get("scanner_score"):
        consensus_val = compute_consensus_score(effective_result)

    existing_id = _find_existing_signal_id(con, aid, ikey)
    if existing_id is not None:
        con.close()
        return existing_id

    try:
        cur = con.execute(
            """
            INSERT INTO signals
            (created_at,symbol,timeframe,scanner_score,final_decision,
             confidence,entry,stop_loss,take_profit,risk_reward,
             market_regime,score_consensus,reasoning,smc_score,entry_status,
             entry_zone_low,entry_zone_high,smc_bias,analysis_id,idempotency_key,
             raw_model_decision,veto_reasons,accounting_mode,origin,
             accounting_quality,strategy_version,prompt_version,schema_version,
             position_size,notional_value,risk_budget,net_rr,instrument_type,trade_plan_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now,
                sanitized["symbol"],
                "5m/15m/1h",
                sanitized.get("scanner_score"),
                final_dec,
                sanitized["portfolio"]["confidence"],
                entry_v,
                sl_v,
                tp_v,
                rr_v,
                sanitized["packet"]["market_regime"],
                consensus_val,
                sanitized["portfolio"]["reasoning"],
                sanitized.get("smc_score"),
                sanitized.get("entry_status"),
                sanitized.get("entry_zone_low") if final_dec != "WAIT" else None,
                sanitized.get("entry_zone_high") if final_dec != "WAIT" else None,
                sanitized.get("smc_bias"),
                aid,
                ikey,
                raw_dec,
                veto_reasons_str,
                acc_mode,
                origin_str,
                acc_quality,
                strat_ver,
                prompt_ver,
                schema_ver,
                sanitized.get("position_size") if final_dec != "WAIT" else 0.0,
                sanitized.get("notional_value") if final_dec != "WAIT" else 0.0,
                sanitized.get("risk_budget"),
                sanitized.get("net_rr", sanitized.get("gate_report", {}).get("net_rr", 0.0)) if final_dec != "WAIT" else 0.0,
                sanitized.get("instrument_type") or (sanitized.get("instrument", {}).get("instrument_type") if isinstance(sanitized.get("instrument"), dict) else None),
                json.dumps(sanitized.get("trade_plan") or {}, ensure_ascii=False) if sanitized.get("trade_plan") else None,
            ),
        )
    except sqlite3.IntegrityError:
        con.rollback()
        existing_id = _find_existing_signal_id(con, aid, ikey)
        con.close()
        if existing_id is not None:
            return existing_id
        raise

    signal_id = int(cur.lastrowid)

    all_agent_entries: List[Dict[str, Any]] = []
    for group in [
        sanitized.get("analysts") or [],
        [
            {"agent": "🐂 Bull Researcher", **(sanitized.get("bull") or {})},
            {"agent": "🐻 Bear Researcher", **(sanitized.get("bear") or {})},
        ],
        [
            {"agent": "🧠 Research Manager", **(sanitized.get("research_manager") or {})},
            {"agent": "💹 Trader", **(sanitized.get("trader") or {})},
        ],
        sanitized.get("risk_agents") or [],
        [{"agent": "👨‍⚖️ Portfolio Manager", **(sanitized.get("portfolio") or {})}],
    ]:
        for x in group:
            if not isinstance(x, dict) or not x.get("agent"):
                continue
            all_agent_entries.append(x)
            con.execute(
                """
                INSERT INTO agent_votes
                (signal_id,agent,decision,confidence,reasoning)
                VALUES (?,?,?,?,?)
                """,
                (
                    signal_id,
                    x["agent"],
                    x.get("decision", "WAIT"),
                    x.get("confidence", 0.0),
                    x.get("reasoning", ""),
                ),
            )

    con.execute(
        """
        INSERT INTO memories
        (created_at,symbol,memory_type,content,signal_id)
        VALUES (?,?,?,?,?)
        """,
        (
            now,
            sanitized["symbol"],
            "SIGNAL_THESIS",
            sanitized["portfolio"]["reasoning"],
            signal_id,
        ),
    )

    effective_aid = aid or f"ana_sig_{signal_id}"
    usage_total = _aggregate_usage_total(all_agent_entries)
    gate_report_obj = sanitized.get("gate_report") or {
        "approved": final_dec in {"LONG", "SHORT"},
        "raw_model_decision": raw_dec,
        "gated_decision": final_dec,
        "veto_summary": veto_reasons_str,
    }
    con.execute(
        """
        INSERT OR REPLACE INTO analysis_runs
        (analysis_id,symbol,timeframe,created_at,completed_at,bar_time,
         scanner_score,score_consensus,status,raw_model_decision,gated_decision,
         final_confidence,veto_reasons,gate_report_json,modes_json,
         packet_snapshot_json,budget_summary_json,usage_total_json,
         model_config_hash,strategy_version,prompt_version,schema_version,
         origin,accounting_quality,idempotency_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            effective_aid,
            sanitized["symbol"],
            "5m/15m/1h",
            sanitized.get("analyzed_at") or now,
            now,
            sanitized.get("bar_time"),
            sanitized.get("scanner_score"),
            consensus_val,
            str(sanitized.get("portfolio", {}).get("status") or "OK"),
            raw_dec,
            final_dec,
            sanitized["portfolio"]["confidence"],
            veto_reasons_str,
            json.dumps(gate_report_obj, ensure_ascii=False),
            json.dumps(sanitized.get("modes") or {}, ensure_ascii=False),
            json.dumps(sanitized.get("packet") or {}, ensure_ascii=False),
            json.dumps(sanitized.get("budget_summary") or {}, ensure_ascii=False),
            json.dumps(usage_total, ensure_ascii=False),
            model_cfg_hash,
            strat_ver,
            prompt_ver,
            schema_ver,
            origin_str,
            acc_quality,
            ikey,
        ),
    )

    for x in all_agent_entries:
        u = x.get("usage") if isinstance(x.get("usage"), dict) else {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_known": False,
            "cost_status": "UNKNOWN_USAGE",
        }
        role_name = str(x.get("agent", "unknown"))
        status_str = str(x.get("status") or "OK")
        provider_str = str(x.get("provider") or "groq")
        model_str = str(x.get("model") or "")
        con.execute(
            """
            INSERT INTO agent_runs
            (analysis_id,role,tier,status,decision,confidence,reasoning,key_risk,
             evidence_json,provider,model_id,finish_reason,requested_max_tokens,
             effective_max_tokens,usage_json,prompt_version,schema_version,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                effective_aid,
                role_name,
                x.get("tier"),
                status_str,
                x.get("decision", "WAIT"),
                x.get("confidence", 0.0),
                x.get("reasoning", ""),
                x.get("key_risk", ""),
                json.dumps(x.get("evidence") or [], ensure_ascii=False),
                provider_str,
                model_str,
                x.get("finish_reason"),
                x.get("requested_max_tokens"),
                x.get("effective_max_tokens"),
                json.dumps(u, ensure_ascii=False),
                prompt_ver,
                schema_ver,
                now,
            ),
        )
        con.execute(
            """
            INSERT INTO usage_events
            (analysis_id,role,provider,model_id,status,input_tokens,output_tokens,
             reasoning_tokens,total_tokens,usage_known,cost_status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                effective_aid,
                role_name,
                provider_str,
                model_str,
                status_str,
                u.get("input_tokens"),
                u.get("output_tokens"),
                u.get("reasoning_tokens"),
                u.get("total_tokens"),
                1 if u.get("usage_known") else 0,
                str(u.get("cost_status") or "UNKNOWN_USAGE"),
                now,
            ),
        )

    # Persist explicit position_state on signals and create paper_positions record for LONG/SHORT (PH06-01 / F04, F33, F36)
    requested_order_state = str(sanitized.get("order_state") or sanitized.get("position_state") or "").upper().strip()
    if final_dec in {"LONG", "SHORT"}:
        pos_state = (
            PaperPositionState.PENDING_ENTRY.value
            if requested_order_state == PaperPositionState.PENDING_ENTRY.value
            else PaperPositionState.OPEN.value
        )
    else:
        pos_state = PaperPositionState.OBSERVED_WAIT.value

    opened_ts = (sanitized.get("bar_time") or sanitized.get("analyzed_at") or now) if pos_state == PaperPositionState.OPEN.value else None
    sig_lc_state = str(
        sanitized.get("signal_lifecycle_state")
        or ("TRIGGERED" if final_dec in {"LONG", "SHORT"} else "WAIT")
    ).upper().strip()
    inv_lvl_v = sanitized.get("invalidation_level") if is_finite_number(sanitized.get("invalidation_level")) else (sl_v if final_dec in {"LONG", "SHORT"} else None)
    exp_at_v = sanitized.get("expires_at")
    con.execute(
        "UPDATE signals SET position_state=?, opened_at=?, signal_lifecycle_state=?, invalidation_level=?, expires_at=? WHERE id=?",
        (pos_state, opened_ts, sig_lc_state, inv_lvl_v, exp_at_v, signal_id),
    )

    if final_dec in {"LONG", "SHORT"} and is_finite_number(entry_v) and is_finite_number(sl_v) and is_finite_number(tp_v):
        pos_id = f"pos_sig_{signal_id}"
        tp_plan = sanitized.get("trade_plan") if isinstance(sanitized.get("trade_plan"), dict) else {}
        qty_val = float(sanitized.get("position_size") or sanitized.get("quantity") or tp_plan.get("position_size") or 0.0)
        if qty_val <= 0.0:
            stop_d = max(abs(float(entry_v) - float(sl_v)), 1e-8)
            rb_fallback = float(sanitized.get("risk_budget") or 50.0)
            qty_val = round(rb_fallback / stop_d, 8)
        notional_v = float(sanitized.get("notional_value") or tp_plan.get("notional_value") or (qty_val * float(entry_v)))
        margin_v = float(tp_plan.get("margin_required") or notional_v)
        rb_val = float(sanitized.get("risk_budget") or tp_plan.get("risk_budget") or 50.0)
        fee_rt_v = float(tp_plan.get("fee_pct_round_trip", 0.08) or 0.08)
        slip_rt_v = float(tp_plan.get("slippage_pct_round_trip", 0.04) or 0.04)
        spread_v = float(sanitized.get("packet", {}).get("order_book", {}).get("spread", 0.0) or 0.0)
        inst_type_v = str(
            sanitized.get("instrument_type")
            or (sanitized.get("instrument", {}).get("instrument_type") if isinstance(sanitized.get("instrument"), dict) else None)
            or ("SPOT_LONG_PAPER" if final_dec == "LONG" else "SPOT_SYNTHETIC_SHORT_PAPER")
        )
        con.execute(
            """
            INSERT OR REPLACE INTO paper_positions
            (position_id, signal_id, analysis_id, symbol, timeframe, mode, direction, state,
             instrument_type, entry, stop_loss, take_profit, entry_zone_low, entry_zone_high,
             quantity, notional_value, margin_required, risk_budget,
             fee_pct_round_trip, slippage_pct_round_trip, spread_pct,
             max_wait_bars, max_hold_bars, expires_at, time_stop_at,
             tie_breaker_policy, created_at, opened_at, last_evaluated_bar_time,
             origin, accounting_quality, market_regime, strategy_version, invalidation_level)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pos_id,
                signal_id,
                effective_aid,
                sanitized["symbol"],
                "15m",
                str(sanitized.get("mode") or "scalping"),
                final_dec,
                pos_state,
                inst_type_v,
                float(entry_v),
                float(sl_v),
                float(tp_v),
                sanitized.get("entry_zone_low"),
                sanitized.get("entry_zone_high"),
                qty_val,
                notional_v,
                margin_v,
                rb_val,
                fee_rt_v,
                slip_rt_v,
                spread_v,
                sanitized.get("max_wait_bars"),
                sanitized.get("max_hold_bars"),
                sanitized.get("expires_at"),
                sanitized.get("time_stop_at"),
                str(sanitized.get("tie_breaker_policy") or IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value),
                sanitized.get("analyzed_at") or now,
                opened_ts,
                sanitized.get("bar_time"),
                origin_str,
                REALISTIC_ACCOUNTING_QUALITY,
                sanitized.get("packet", {}).get("market_regime"),
                strat_ver,
                inv_lvl_v,
            ),
        )

    con.commit()
    con.close()
    return signal_id


def get_signal_provenance(
    signal_id: Optional[int] = None,
    analysis_id: Optional[str] = None,
    db_file: str = DEFAULT_DB_FILE,
) -> Optional[Dict[str, Any]]:
    con = connect_db(db_file)
    try:
        if signal_id is not None:
            sig_row = con.execute("SELECT * FROM signals WHERE id=? LIMIT 1", (int(signal_id),)).fetchone()
        elif analysis_id is not None:
            sig_row = con.execute("SELECT * FROM signals WHERE analysis_id=? LIMIT 1", (str(analysis_id),)).fetchone()
        else:
            return None

        if sig_row is None:
            return None

        sig_dict = dict(sig_row)
        aid = sig_dict.get("analysis_id")
        origin = sig_dict.get("origin") or ("legacy" if not aid else "v6_runtime")

        run_row = None
        agent_rows: List[Dict[str, Any]] = []
        usage_rows: List[Dict[str, Any]] = []

        existing_tables = {
            str(r[0])
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if aid and "analysis_runs" in existing_tables:
            r = con.execute("SELECT * FROM analysis_runs WHERE analysis_id=? LIMIT 1", (aid,)).fetchone()
            if r:
                run_row = dict(r)
                for json_col, target_key in (
                    ("gate_report_json", "gate_report"),
                    ("modes_json", "modes"),
                    ("packet_snapshot_json", "packet_snapshot"),
                    ("budget_summary_json", "budget_summary"),
                    ("usage_total_json", "usage_total"),
                ):
                    raw_j = run_row.get(json_col)
                    run_row[target_key] = json.loads(raw_j) if raw_j else None

        if aid and "agent_runs" in existing_tables:
            agent_rows = [
                dict(r)
                for r in con.execute("SELECT * FROM agent_runs WHERE analysis_id=? ORDER BY id ASC", (aid,)).fetchall()
            ]
        if aid and "usage_events" in existing_tables:
            usage_rows = [
                dict(r)
                for r in con.execute("SELECT * FROM usage_events WHERE analysis_id=? ORDER BY id ASC", (aid,)).fetchall()
            ]

        vote_rows = [
            dict(r)
            for r in con.execute("SELECT * FROM agent_votes WHERE signal_id=? ORDER BY id ASC", (sig_dict["id"],)).fetchall()
        ]

        return {
            "signal": sig_dict,
            "origin": origin,
            "accounting_quality": sig_dict.get("accounting_quality") or ("legacy_snapshot_only" if origin == "legacy" else "legacy_r_snapshot"),
            "strategy_version": sig_dict.get("strategy_version"),
            "prompt_version": sig_dict.get("prompt_version"),
            "schema_version": sig_dict.get("schema_version"),
            "raw_model_decision": sig_dict.get("raw_model_decision"),
            "gated_decision": sig_dict.get("final_decision"),
            "veto_reasons": sig_dict.get("veto_reasons"),
            "scanner_score": sig_dict.get("scanner_score"),
            "score_consensus": sig_dict.get("score_consensus") if origin != "legacy" else None,
            "analysis_run": run_row,
            "agent_runs": agent_rows,
            "usage_events": usage_rows,
            "agent_votes": vote_rows,
        }
    finally:
        con.close()


def save_pattern_event_record(
    snap: Dict[str, Any],
    fingerprint: str,
    mode: str = "scalping",
    notified: int = 0,
    db_file: str = DEFAULT_DB_FILE,
) -> Tuple[int, bool]:
    run_migrations(db_file)
    p = snap["pattern"]
    now = datetime.now(timezone.utc).isoformat()
    con = connect_db(db_file)
    try:
        row = con.execute(
            "SELECT id, notified FROM pattern_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if row:
            return int(row["id"]), bool(row["notified"])
        try:
            cur = con.execute(
                "INSERT INTO pattern_events(created_at,symbol,mode,state,direction,score,fingerprint,notified) VALUES(?,?,?,?,?,?,?,?)",
                (now, snap["symbol"], mode, p["state"], p["direction"], p["score"], fingerprint, int(notified)),
            )
            con.commit()
            return int(cur.lastrowid), bool(notified)
        except sqlite3.IntegrityError:
            con.rollback()
            row = con.execute(
                "SELECT id, notified FROM pattern_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if row:
                return int(row["id"]), bool(row["notified"])
            raise
    finally:
        con.close()


def _ensure_pattern_events_workflow_cols(con: sqlite3.Connection) -> None:
    pe_cols = {row[1] for row in con.execute("PRAGMA table_info(pattern_events)").fetchall()}
    for col_name, ddl in [
        ("validation_status", "ALTER TABLE pattern_events ADD COLUMN validation_status TEXT DEFAULT 'UNVALIDATED'"),
        ("persistence_status", "ALTER TABLE pattern_events ADD COLUMN persistence_status TEXT DEFAULT 'UNSAVED'"),
        ("notification_status", "ALTER TABLE pattern_events ADD COLUMN notification_status TEXT DEFAULT 'NOT_APPLICABLE'"),
        ("analysis_id", "ALTER TABLE pattern_events ADD COLUMN analysis_id TEXT"),
    ]:
        if col_name not in pe_cols:
            con.execute(ddl)


def update_pattern_event_workflow(
    *,
    event_id: Optional[int] = None,
    fingerprint: Optional[str] = None,
    validation_status: Optional[str] = None,
    persistence_status: Optional[str] = None,
    notification_status: Optional[str] = None,
    analysis_id: Optional[str] = None,
    notified: Optional[int] = None,
    db_file: str = DEFAULT_DB_FILE,
) -> Optional[Dict[str, Any]]:
    """Atomically checkpoint workflow stage statuses on pattern_events (F23)."""
    run_migrations(db_file)
    con = connect_db(db_file)
    try:
        _ensure_pattern_events_workflow_cols(con)
        if event_id is not None:
            row = con.execute("SELECT * FROM pattern_events WHERE id=? LIMIT 1", (int(event_id),)).fetchone()
        elif fingerprint is not None:
            row = con.execute("SELECT * FROM pattern_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1", (str(fingerprint),)).fetchone()
        else:
            return None
        if row is None:
            return None

        target_id = int(row["id"])
        updates: List[str] = []
        params: List[Any] = []
        if validation_status is not None:
            updates.append("validation_status=?")
            params.append(str(validation_status))
        if persistence_status is not None:
            updates.append("persistence_status=?")
            params.append(str(persistence_status))
        if notification_status is not None:
            updates.append("notification_status=?")
            params.append(str(notification_status))
        if analysis_id is not None:
            updates.append("analysis_id=?")
            params.append(str(analysis_id))
        if notified is not None:
            updates.append("notified=?")
            params.append(int(notified))

        if updates:
            params.append(target_id)
            con.execute(f"UPDATE pattern_events SET {', '.join(updates)} WHERE id=?", tuple(params))
            con.commit()

        updated_row = con.execute("SELECT * FROM pattern_events WHERE id=? LIMIT 1", (target_id,)).fetchone()
        return dict(updated_row) if updated_row else None
    finally:
        con.close()


def get_pattern_event_workflow(
    *,
    event_id: Optional[int] = None,
    fingerprint: Optional[str] = None,
    db_file: str = DEFAULT_DB_FILE,
) -> Optional[Dict[str, Any]]:
    """Retrieve durable workflow stage status for a pattern event (F23)."""
    run_migrations(db_file)
    con = connect_db(db_file)
    try:
        _ensure_pattern_events_workflow_cols(con)
        if event_id is not None:
            row = con.execute("SELECT * FROM pattern_events WHERE id=? LIMIT 1", (int(event_id),)).fetchone()
        elif fingerprint is not None:
            row = con.execute("SELECT * FROM pattern_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1", (str(fingerprint),)).fetchone()
        else:
            return None
        if row is None:
            return None
        d = dict(row)
        d.setdefault("validation_status", "UNVALIDATED")
        d.setdefault("persistence_status", "UNSAVED")
        d.setdefault("notification_status", "SENT" if d.get("notified") else "NOT_APPLICABLE")
        d.setdefault("analysis_id", None)
        return d
    finally:
        con.close()
@dataclass(frozen=True)
class QuotaScopeRecord:
    scope_id: str
    provider: str
    max_requests_per_day: Optional[int]
    max_tokens_per_day: Optional[int]
    max_physical_attempts_per_run: Optional[int]
    requests_today: int
    tokens_today: int
    physical_attempts_total: int
    exhausted_until_epoch: float
    exhausted_reason: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "provider": self.provider,
            "max_requests_per_day": self.max_requests_per_day,
            "max_tokens_per_day": self.max_tokens_per_day,
            "max_physical_attempts_per_run": self.max_physical_attempts_per_run,
            "requests_today": self.requests_today,
            "tokens_today": self.tokens_today,
            "physical_attempts_total": self.physical_attempts_total,
            "exhausted_until_epoch": self.exhausted_until_epoch,
            "exhausted_reason": self.exhausted_reason,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class BudgetReservationRecord:
    reservation_id: str
    analysis_id: str
    scope_id: str
    role: str
    stage: str
    status: str
    estimated_input_tokens: int
    reserved_completion_tokens: int
    reserved_physical_attempts: int
    actual_input_tokens: Optional[int]
    actual_output_tokens: Optional[int]
    actual_reasoning_tokens: Optional[int]
    actual_total_tokens: Optional[int]
    usage_known: bool
    cost_status: str
    deadline_epoch: Optional[float]
    created_at: str
    reconciled_at: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "analysis_id": self.analysis_id,
            "scope_id": self.scope_id,
            "role": self.role,
            "stage": self.stage,
            "status": self.status,
            "estimated_input_tokens": self.estimated_input_tokens,
            "reserved_completion_tokens": self.reserved_completion_tokens,
            "reserved_physical_attempts": self.reserved_physical_attempts,
            "actual_input_tokens": self.actual_input_tokens,
            "actual_output_tokens": self.actual_output_tokens,
            "actual_reasoning_tokens": self.actual_reasoning_tokens,
            "actual_total_tokens": self.actual_total_tokens,
            "usage_known": self.usage_known,
            "cost_status": self.cost_status,
            "deadline_epoch": self.deadline_epoch,
            "created_at": self.created_at,
            "reconciled_at": self.reconciled_at,
        }


def _is_judge_role(role: str = "", tier: str = "analyst") -> bool:
    if str(tier or "").strip().lower() == "judge":
        return True
    role_lower = str(role or "").strip().lower()
    return "final judge" in role_lower or "portfolio manager" in role_lower


class DurableBudgetManager:
    """
    Durable, process-safe and session-safe SQLite usage ledger and budget manager (PH05-02 / F02, F20, F21 / T20).
    Enforces atomic budget reservations, quota scope exhaustion persistence, preflight Final Judge protection,
    and honest UNKNOWN_USAGE reporting across browser reloads, crashes, and multi-worker threads.
    """

    def __init__(self, db_file: str = DEFAULT_DB_FILE) -> None:
        self.db_file = db_file
        self._lock = threading.Lock()

    def get_or_create_scope(
        self,
        scope_id: str = "groq_default",
        provider: str = "groq",
        max_physical_attempts: Optional[int] = None,
        max_requests_per_day: Optional[int] = None,
        max_tokens_per_day: Optional[int] = None,
        db_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_db = db_file or self.db_file
        run_migrations(target_db)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            con = connect_db(target_db)
            try:
                row = con.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                if row:
                    return dict(row)
                con.execute(
                    """INSERT INTO quota_scopes
                    (scope_id, provider, max_requests_per_day, max_tokens_per_day, max_physical_attempts_per_run, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (scope_id, provider, max_requests_per_day, max_tokens_per_day, max_physical_attempts, now_iso),
                )
                con.commit()
                row = con.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                return dict(row)
            finally:
                con.close()

    def is_scope_exhausted(
        self,
        scope_id: str = "groq_default",
        now_fn: Callable[[], float] = time.time,
        db_file: Optional[str] = None,
    ) -> Tuple[bool, str, float]:
        target_db = db_file or self.db_file
        run_migrations(target_db)
        now_epoch = float(now_fn())
        with self._lock:
            con = connect_db(target_db)
            try:
                row = con.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                if not row:
                    return False, "OK", 0.0
                ex_until = float(row["exhausted_until_epoch"] or 0.0)
                if ex_until > now_epoch:
                    reason = str(row["exhausted_reason"] or "DAILY_QUOTA_EXHAUSTED")
                    return True, reason, ex_until
                max_req = row["max_requests_per_day"]
                if max_req is not None and int(row["requests_today"]) >= int(max_req):
                    return True, "MAX_REQUESTS_PER_DAY_EXCEEDED", 86400.0
                max_tok = row["max_tokens_per_day"]
                if max_tok is not None and int(row["tokens_today"]) >= int(max_tok):
                    return True, "MAX_TOKENS_PER_DAY_EXCEEDED", 86400.0
                return False, "OK", 0.0
            finally:
                con.close()

    def mark_scope_exhausted(
        self,
        scope_id: str = "groq_default",
        seconds: float = 86400.0,
        reason: str = "DAILY_QUOTA_EXHAUSTED",
        now_fn: Callable[[], float] = time.time,
        db_file: Optional[str] = None,
    ) -> None:
        target_db = db_file or self.db_file
        self.get_or_create_scope(scope_id=scope_id, db_file=target_db)
        until_epoch = float(now_fn()) + max(1.0, float(seconds))
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            con = connect_db(target_db)
            try:
                con.execute(
                    """UPDATE quota_scopes
                    SET exhausted_until_epoch=?, exhausted_reason=?, updated_at=?
                    WHERE scope_id=?""",
                    (until_epoch, str(reason), now_iso, scope_id),
                )
                con.commit()
            finally:
                con.close()

    def preflight_reserve_judge(
        self,
        analysis_id: str,
        scope_id: str = "groq_default",
        judge_budget: int = 750,
        max_physical_attempts: Optional[int] = None,
        deadline_epoch: Optional[float] = None,
        now_fn: Callable[[], float] = time.time,
        db_file: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        target_db = db_file or self.db_file
        self.get_or_create_scope(scope_id=scope_id, max_physical_attempts=max_physical_attempts, db_file=target_db)
        now_epoch = float(now_fn())
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            con = connect_db(target_db)
            con.isolation_level = None
            try:
                con.execute("BEGIN IMMEDIATE")
                row_sc = con.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                if row_sc and float(row_sc["exhausted_until_epoch"] or 0.0) > now_epoch:
                    reason = str(row_sc["exhausted_reason"] or "DAILY_QUOTA_EXHAUSTED")
                    con.execute("COMMIT")
                    return False, f"QUOTA_SCOPE_EXHAUSTED:{reason}", None

                if deadline_epoch is not None and now_epoch > float(deadline_epoch):
                    con.execute("COMMIT")
                    return False, "DEADLINE_EXCEEDED", None

                existing_judge = con.execute(
                    """SELECT reservation_id FROM budget_reservations
                    WHERE analysis_id=? AND stage IN ('preflight_judge', 'final_judge')
                    AND status IN ('RESERVED', 'COMMITTED') LIMIT 1""",
                    (analysis_id,),
                ).fetchone()
                if existing_judge:
                    con.execute("COMMIT")
                    return True, "ALREADY_RESERVED", str(existing_judge["reservation_id"])

                eff_cap = (
                    int(max_physical_attempts)
                    if max_physical_attempts is not None
                    else (int(row_sc["max_physical_attempts_per_run"]) if (row_sc and row_sc["max_physical_attempts_per_run"] is not None) else 14)
                )

                current_res_cnt = con.execute(
                    """SELECT COUNT(*) FROM budget_reservations
                    WHERE analysis_id=? AND status IN ('RESERVED', 'COMMITTED')""",
                    (analysis_id,),
                ).fetchone()[0]

                if current_res_cnt + 1 > eff_cap:
                    con.execute("COMMIT")
                    return False, "CAP_CANNOT_ACCOMMODATE_JUDGE", None

                res_id = f"res_judge_{analysis_id}_{uuid.uuid4().hex[:8]}"
                con.execute(
                    """INSERT INTO budget_reservations
                    (reservation_id, analysis_id, scope_id, role, stage, status,
                     reserved_completion_tokens, reserved_physical_attempts,
                     deadline_epoch, created_at)
                    VALUES (?, ?, ?, '👨‍⚖️ Final Judge', 'preflight_judge', 'RESERVED', ?, 1, ?, ?)""",
                    (res_id, analysis_id, scope_id, int(judge_budget), deadline_epoch, now_iso),
                )
                con.execute("COMMIT")
                return True, "OK", res_id
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    def reserve_attempt(
        self,
        analysis_id: str,
        scope_id: str = "groq_default",
        role: str = "",
        tier: str = "analyst",
        requested_budget: int = 450,
        max_physical_attempts: Optional[int] = None,
        deadline_epoch: Optional[float] = None,
        now_fn: Callable[[], float] = time.time,
        db_file: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        target_db = db_file or self.db_file
        self.get_or_create_scope(scope_id=scope_id, max_physical_attempts=max_physical_attempts, db_file=target_db)
        now_epoch = float(now_fn())
        now_iso = datetime.now(timezone.utc).isoformat()
        is_judge = _is_judge_role(role, tier)

        with self._lock:
            con = connect_db(target_db)
            con.isolation_level = None
            try:
                con.execute("BEGIN IMMEDIATE")
                row_sc = con.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                if row_sc and float(row_sc["exhausted_until_epoch"] or 0.0) > now_epoch:
                    reason = str(row_sc["exhausted_reason"] or "DAILY_QUOTA_EXHAUSTED")
                    con.execute("COMMIT")
                    return False, f"QUOTA_SCOPE_EXHAUSTED:{reason}", None

                if deadline_epoch is not None and now_epoch > float(deadline_epoch):
                    con.execute("COMMIT")
                    return False, "DEADLINE_EXCEEDED", None

                eff_cap = (
                    int(max_physical_attempts)
                    if max_physical_attempts is not None
                    else (int(row_sc["max_physical_attempts_per_run"]) if (row_sc and row_sc["max_physical_attempts_per_run"] is not None) else 14)
                )

                if is_judge:
                    existing_preflight = con.execute(
                        """SELECT reservation_id FROM budget_reservations
                        WHERE analysis_id=? AND stage='preflight_judge' AND status='RESERVED' LIMIT 1""",
                        (analysis_id,),
                    ).fetchone()
                    if existing_preflight:
                        res_id = str(existing_preflight["reservation_id"])
                        con.execute(
                            """UPDATE budget_reservations
                            SET stage='final_judge', reserved_completion_tokens=?
                            WHERE reservation_id=?""",
                            (int(requested_budget), res_id),
                        )
                        con.execute("COMMIT")
                        return True, "OK", res_id

                    total_used = con.execute(
                        """SELECT COUNT(*) FROM budget_reservations
                        WHERE analysis_id=? AND status IN ('RESERVED', 'COMMITTED')""",
                        (analysis_id,),
                    ).fetchone()[0]
                    if total_used + 1 > eff_cap:
                        con.execute("COMMIT")
                        return False, "PHYSICAL_CAP_REACHED", None

                    res_id = f"res_judge_{analysis_id}_{uuid.uuid4().hex[:8]}"
                    con.execute(
                        """INSERT INTO budget_reservations
                        (reservation_id, analysis_id, scope_id, role, stage, status,
                         reserved_completion_tokens, reserved_physical_attempts,
                         deadline_epoch, created_at)
                        VALUES (?, ?, ?, ?, 'final_judge', 'RESERVED', ?, 1, ?, ?)""",
                        (res_id, analysis_id, scope_id, role, int(requested_budget), deadline_epoch, now_iso),
                    )
                    con.execute("COMMIT")
                    return True, "OK", res_id

                # For optional non-judge roles: enforce PREFLIGHT JUDGE PRESERVATION!
                non_judge_attempts = con.execute(
                    """SELECT COUNT(*) FROM budget_reservations
                    WHERE analysis_id=? AND stage NOT IN ('preflight_judge', 'final_judge')
                    AND status IN ('RESERVED', 'COMMITTED')""",
                    (analysis_id,),
                ).fetchone()[0]

                judge_slot_needed = 1  # Always preserve at least 1 attempt slot for Final Judge!
                if non_judge_attempts + 1 + judge_slot_needed > eff_cap:
                    con.execute("COMMIT")
                    return False, "PHYSICAL_CAP_REACHED_PRESERVING_JUDGE", None

                clean_role_prefix = re.sub(r"[^a-zA-Z0-9]", "", role)[:6] or "agent"
                res_id = f"res_{clean_role_prefix}_{analysis_id}_{uuid.uuid4().hex[:8]}"
                con.execute(
                    """INSERT INTO budget_reservations
                    (reservation_id, analysis_id, scope_id, role, stage, status,
                     reserved_completion_tokens, reserved_physical_attempts,
                     deadline_epoch, created_at)
                    VALUES (?, ?, ?, ?, ?, 'RESERVED', ?, 1, ?, ?)""",
                    (res_id, analysis_id, scope_id, role, tier, int(requested_budget), deadline_epoch, now_iso),
                )
                con.execute("COMMIT")
                return True, "OK", res_id
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    def commit_usage(
        self,
        reservation_id: str,
        usage_dict: Optional[Dict[str, Any]] = None,
        status_str: str = "OK",
        role: str = "",
        provider: str = "groq",
        model_id: str = "",
        finish_reason: Optional[str] = None,
        db_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_db = db_file or self.db_file
        now_iso = datetime.now(timezone.utc).isoformat()
        u = usage_dict if isinstance(usage_dict, dict) else {}
        usage_known = bool(u.get("usage_known", False))
        cost_status = "MEASURED" if usage_known else "UNKNOWN_USAGE"
        in_tok = u.get("input_tokens") if usage_known else None
        out_tok = u.get("output_tokens") if usage_known else None
        reas_tok = u.get("reasoning_tokens") if usage_known else None
        tot_tok = u.get("total_tokens") if usage_known else None

        with self._lock:
            con = connect_db(target_db)
            con.isolation_level = None
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT * FROM budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                analysis_id = str(row["analysis_id"]) if row else "unknown_analysis"
                scope_id = str(row["scope_id"]) if row else "groq_default"
                active_role = role or (str(row["role"]) if row else "unknown_role")

                con.execute(
                    """UPDATE budget_reservations
                    SET status='COMMITTED',
                        actual_input_tokens=?,
                        actual_output_tokens=?,
                        actual_reasoning_tokens=?,
                        actual_total_tokens=?,
                        usage_known=?,
                        cost_status=?,
                        reconciled_at=?
                    WHERE reservation_id=?""",
                    (in_tok, out_tok, reas_tok, tot_tok, 1 if usage_known else 0, cost_status, now_iso, reservation_id),
                )

                tok_add = int(tot_tok) if (usage_known and isinstance(tot_tok, int)) else 0
                con.execute(
                    """UPDATE quota_scopes
                    SET requests_today = requests_today + 1,
                        physical_attempts_total = physical_attempts_total + 1,
                        tokens_today = tokens_today + ?,
                        updated_at = ?
                    WHERE scope_id=?""",
                    (tok_add, now_iso, scope_id),
                )

                con.execute(
                    """INSERT OR IGNORE INTO analysis_runs
                    (analysis_id, symbol, timeframe, created_at, status, raw_model_decision, gated_decision, model_config_hash, strategy_version, prompt_version, schema_version)
                    VALUES (?, 'UNKNOWN', 'in_flight', ?, 'IN_PROGRESS', 'WAIT', 'WAIT', 'in_flight', 'v6_runtime', 'v6_runtime', 'v6_runtime')""",
                    (analysis_id, now_iso),
                )
                con.execute(
                    """INSERT INTO usage_events
                    (analysis_id, role, provider, model_id, status,
                     input_tokens, output_tokens, reasoning_tokens, total_tokens,
                     usage_known, cost_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        analysis_id,
                        active_role,
                        provider,
                        model_id,
                        status_str,
                        in_tok,
                        out_tok,
                        reas_tok,
                        tot_tok,
                        1 if usage_known else 0,
                        cost_status,
                        now_iso,
                    ),
                )
                con.execute("COMMIT")
                return {
                    "reservation_id": reservation_id,
                    "analysis_id": analysis_id,
                    "scope_id": scope_id,
                    "role": active_role,
                    "status": "COMMITTED",
                    "usage_known": usage_known,
                    "cost_status": cost_status,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "reasoning_tokens": reas_tok,
                    "total_tokens": tot_tok,
                }
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    def release_reservation(
        self,
        reservation_id: str,
        reason: str = "CANCELLED",
        db_file: Optional[str] = None,
    ) -> bool:
        target_db = db_file or self.db_file
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            con = connect_db(target_db)
            try:
                cur = con.execute(
                    """UPDATE budget_reservations
                    SET status='RELEASED', cost_status=?, reconciled_at=?
                    WHERE reservation_id=? AND status='RESERVED'""",
                    (f"RELEASED:{reason}", now_iso, reservation_id),
                )
                con.commit()
                return bool(cur.rowcount > 0)
            finally:
                con.close()

    def get_scope_stats(self, scope_id: str = "groq_default", db_file: Optional[str] = None) -> Dict[str, Any]:
        target_db = db_file or self.db_file
        self.get_or_create_scope(scope_id=scope_id, db_file=target_db)
        with self._lock:
            con = connect_db(target_db)
            try:
                row = con.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                active_res = con.execute(
                    """SELECT COUNT(*) FROM budget_reservations
                    WHERE scope_id=? AND status='RESERVED'""",
                    (scope_id,),
                ).fetchone()[0]
                committed_res = con.execute(
                    """SELECT COUNT(*) FROM budget_reservations
                    WHERE scope_id=? AND status='COMMITTED'""",
                    (scope_id,),
                ).fetchone()[0]
                d = dict(row) if row else {}
                d["active_reservations_count"] = int(active_res)
                d["committed_reservations_count"] = int(committed_res)
                return d
            finally:
                con.close()

    def get_analysis_reservations(self, analysis_id: str, db_file: Optional[str] = None) -> List[Dict[str, Any]]:
        target_db = db_file or self.db_file
        run_migrations(target_db)
        with self._lock:
            con = connect_db(target_db)
            try:
                rows = con.execute(
                    """SELECT * FROM budget_reservations
                    WHERE analysis_id=? ORDER BY created_at ASC""",
                    (analysis_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def reset_scope(self, scope_id: str = "groq_default", db_file: Optional[str] = None) -> None:
        target_db = db_file or self.db_file
        self.get_or_create_scope(scope_id=scope_id, db_file=target_db)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            con = connect_db(target_db)
            try:
                con.execute(
                    """UPDATE quota_scopes
                    SET requests_today=0, tokens_today=0, physical_attempts_total=0,
                        exhausted_until_epoch=0.0, exhausted_reason='', updated_at=?
                    WHERE scope_id=?""",
                    (now_iso, scope_id),
                )
                con.commit()
            finally:
                con.close()


GLOBAL_DURABLE_BUDGET_MANAGER = DurableBudgetManager()


# ---------------------------------------------------------
# DAG Checkpoint Repository (PH05-03 / F05 / T22)
# ---------------------------------------------------------
@dataclass(frozen=True)
class DAGCheckpointRecord:
    analysis_id: str
    node_id: str
    stage: str
    status: str
    dependencies: Tuple[str, ...]
    payload: Dict[str, Any]
    attempt_count: int
    started_at: str
    completed_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "node_id": self.node_id,
            "stage": self.stage,
            "status": self.status,
            "dependencies": list(self.dependencies),
            "payload": dict(self.payload) if isinstance(self.payload, dict) else self.payload,
            "attempt_count": self.attempt_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


_DAG_CKPT_LOCK = threading.Lock()


def save_dag_checkpoint(
    analysis_id: str,
    node_id: str,
    stage: str,
    status: str,
    payload: Any,
    *,
    dependencies: Iterable[str] = (),
    attempt_count: int = 1,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    db_file: str = DEFAULT_DB_FILE,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Durably persist a DAG node checkpoint in SQLite (PH05-03 / F05 / T22).
    Thread-safe and redacts any secret tokens before writing payload_json.
    """
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    s_at = str(started_at or now_iso)
    c_at = str(completed_at or now_iso)
    deps_list = [str(d) for d in (dependencies or ())]
    sanitized_payload = redact_secrets(payload if isinstance(payload, dict) else {"value": payload}, extra_secrets=extra_secrets)

    with _DAG_CKPT_LOCK:
        con = connect_db(db_file)
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO dag_checkpoints
                (analysis_id, node_id, stage, status, dependencies_json, payload_json, attempt_count, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(analysis_id),
                    str(node_id),
                    str(stage),
                    str(status),
                    json.dumps(deps_list, ensure_ascii=False),
                    json.dumps(sanitized_payload, ensure_ascii=False),
                    int(max(1, attempt_count)),
                    s_at,
                    c_at,
                ),
            )
            con.commit()
        finally:
            con.close()

    return DAGCheckpointRecord(
        analysis_id=str(analysis_id),
        node_id=str(node_id),
        stage=str(stage),
        status=str(status),
        dependencies=tuple(deps_list),
        payload=sanitized_payload if isinstance(sanitized_payload, dict) else {"value": sanitized_payload},
        attempt_count=int(max(1, attempt_count)),
        started_at=s_at,
        completed_at=c_at,
    ).to_dict()


def load_dag_checkpoints(
    analysis_id: str,
    db_file: str = DEFAULT_DB_FILE,
) -> Dict[str, Dict[str, Any]]:
    """
    Load all persisted DAG node checkpoints for an analysis_id from SQLite (PH05-03 / F05 / T22).
    Returns a dictionary keyed by node_id.
    """
    run_migrations(db_file)
    with _DAG_CKPT_LOCK:
        con = connect_db(db_file)
        try:
            rows = con.execute(
                """
                SELECT analysis_id, node_id, stage, status, dependencies_json, payload_json, attempt_count, started_at, completed_at
                FROM dag_checkpoints
                WHERE analysis_id=?
                ORDER BY completed_at ASC, rowid ASC
                """,
                (str(analysis_id),),
            ).fetchall()
        finally:
            con.close()

    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            deps = json.loads(r["dependencies_json"] or "[]")
        except Exception:
            deps = []
        try:
            pay = json.loads(r["payload_json"] or "{}")
        except Exception:
            pay = {}
        rec = DAGCheckpointRecord(
            analysis_id=str(r["analysis_id"]),
            node_id=str(r["node_id"]),
            stage=str(r["stage"]),
            status=str(r["status"]),
            dependencies=tuple(str(x) for x in deps),
            payload=pay if isinstance(pay, dict) else {"value": pay},
            attempt_count=int(r["attempt_count"] or 1),
            started_at=str(r["started_at"]),
            completed_at=str(r["completed_at"]),
        )
        out[rec.node_id] = rec.to_dict()
    return out


def clear_dag_checkpoints(
    analysis_id: Optional[str] = None,
    db_file: str = DEFAULT_DB_FILE,
) -> int:
    """Delete DAG checkpoints for a specific analysis_id or all analyses."""
    run_migrations(db_file)
    with _DAG_CKPT_LOCK:
        con = connect_db(db_file)
        try:
            if analysis_id is not None:
                cur = con.execute("DELETE FROM dag_checkpoints WHERE analysis_id=?", (str(analysis_id),))
            else:
                cur = con.execute("DELETE FROM dag_checkpoints")
            con.commit()
            return int(cur.rowcount or 0)
        finally:
            con.close()


# ---------------------------------------------------------
# Realistic Paper Position Reconciliation & Dual Ledger (PH06-01 / F04, F33, F36 / T23, T24, T25)
# ---------------------------------------------------------
def _ensure_backfilled_paper_positions(con: sqlite3.Connection) -> None:
    """
    Ensure any open LONG/SHORT rows in `signals` (e.g. inserted by legacy/direct SQL in tests)
    have a corresponding row in `paper_positions` so unified reconciliation evaluates them cleanly.
    """
    open_sigs = con.execute(
        """
        SELECT * FROM signals
        WHERE result IS NULL AND final_decision IN ('LONG', 'SHORT')
        ORDER BY id ASC
        """
    ).fetchall()
    for s in open_sigs:
        sid = int(s["id"])
        existing = con.execute("SELECT position_id FROM paper_positions WHERE signal_id=? LIMIT 1", (sid,)).fetchone()
        if existing is not None:
            continue
        s_keys = s.keys()
        entry_v = float(s["entry"] or 0.0)
        sl_v = float(s["stop_loss"] or 0.0)
        tp_v = float(s["take_profit"] or 0.0)
        if entry_v <= 0.0 or sl_v <= 0.0 or tp_v <= 0.0:
            continue
        stop_d = max(abs(entry_v - sl_v), 1e-8)
        rb_v = float(s["risk_budget"]) if ("risk_budget" in s_keys and is_finite_number(s["risk_budget"]) and float(s["risk_budget"]) > 0) else 50.0
        qty_v = float(s["position_size"]) if ("position_size" in s_keys and is_finite_number(s["position_size"]) and float(s["position_size"]) > 0) else round(rb_v / stop_d, 8)
        notional_v = round(qty_v * entry_v, 4)
        pos_state = str(s["position_state"] if ("position_state" in s_keys and s["position_state"]) else PaperPositionState.OPEN.value)
        origin_v = str(s["origin"] if ("origin" in s_keys and s["origin"]) else "legacy")
        acc_qual = REALISTIC_ACCOUNTING_QUALITY if origin_v != "legacy" else "legacy_snapshot_only"
        created_ts = str(s["created_at"] or datetime.now(timezone.utc).isoformat())
        opened_ts = str(s["opened_at"] if ("opened_at" in s_keys and s["opened_at"]) else created_ts) if pos_state == PaperPositionState.OPEN.value else None
        con.execute(
            """
            INSERT OR IGNORE INTO paper_positions
            (position_id, signal_id, analysis_id, symbol, timeframe, mode, direction, state,
             instrument_type, entry, stop_loss, take_profit, entry_zone_low, entry_zone_high,
             quantity, notional_value, margin_required, risk_budget,
             fee_pct_round_trip, slippage_pct_round_trip, spread_pct,
             tie_breaker_policy, created_at, opened_at, origin, accounting_quality)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"pos_sig_{sid}",
                sid,
                s["analysis_id"] if "analysis_id" in s_keys else None,
                s["symbol"],
                "15m",
                "scalping",
                s["final_decision"],
                pos_state,
                s["instrument_type"] if ("instrument_type" in s_keys and s["instrument_type"]) else ("SPOT_LONG_PAPER" if s["final_decision"] == "LONG" else "SPOT_SYNTHETIC_SHORT_PAPER"),
                entry_v,
                sl_v,
                tp_v,
                s["entry_zone_low"] if "entry_zone_low" in s_keys else None,
                s["entry_zone_high"] if "entry_zone_high" in s_keys else None,
                qty_v,
                notional_v,
                notional_v,
                rb_v,
                0.08,
                0.04,
                0.0,
                IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value,
                created_ts,
                opened_ts,
                origin_v,
                acc_qual,
            ),
        )


def reconcile_paper_positions(
    db_file: str = DEFAULT_DB_FILE,
    *,
    bars_by_symbol: Optional[Dict[str, Any]] = None,
    kline_fetcher: Optional[Callable[[str, str, int], Any]] = None,
    price_fetcher: Optional[Callable[[str], float]] = None,
    tie_breaker: str = IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value,
    max_wait_bars: Optional[int] = None,
    max_hold_bars: Optional[int] = None,
    initial_equity: float = 10_000.0,
    return_detailed: bool = False,
) -> List[Any]:
    """
    Reconcile all PENDING_ENTRY and OPEN paper positions against closed OHLC bars since `opened_at`
    plus optional fallback spot price (PH06-01 / F04, F33, F36 / T23).
    """
    run_migrations(db_file)
    con = connect_db(db_file)
    try:
        _ensure_backfilled_paper_positions(con)
        active_rows = con.execute(
            """
            SELECT * FROM paper_positions
            WHERE state IN ('PENDING_ENTRY', 'OPEN')
            ORDER BY signal_id ASC, created_at ASC
            """
        ).fetchall()

        last_ledger = con.execute("SELECT equity_after FROM paper_account_ledger ORDER BY id DESC LIMIT 1").fetchone()
        running_equity = float(last_ledger["equity_after"]) if last_ledger else float(initial_equity)

        updated_legacy_tuples: List[Tuple[str, str, float]] = []
        detailed_updates: List[Dict[str, Any]] = []

        for row in active_rows:
            pos_dict = dict(row)
            sym = str(pos_dict["symbol"])
            tf = str(pos_dict.get("timeframe") or "15m")
            if "/" in tf:
                tf = "15m"

            sym_bars = None
            if isinstance(bars_by_symbol, dict) and sym in bars_by_symbol:
                sym_bars = bars_by_symbol[sym]
            elif kline_fetcher is not None:
                try:
                    sym_bars = kline_fetcher(sym, tf, 120)
                except Exception:
                    sym_bars = None

            spot_p: Optional[float] = None
            if price_fetcher is not None:
                try:
                    val_p = float(price_fetcher(sym))
                    if is_finite_number(val_p) and val_p > 0:
                        spot_p = val_p
                except Exception:
                    spot_p = None

            if sym_bars is None and spot_p is None:
                continue

            eff_tie = str(pos_dict.get("tie_breaker_policy") or tie_breaker or IntrabarTieBreaker.SL_FIRST_CONSERVATIVE.value)
            eval_out = evaluate_position_on_bars(
                pos_dict,
                bars=sym_bars,
                fallback_spot_price=spot_p,
                tie_breaker=eff_tie,
                max_wait_bars=max_wait_bars,
                max_hold_bars=max_hold_bars,
                interval=tf,
            )

            # Always persist bar progress counters even if still open/pending
            new_state = str(eval_out["state"])
            con.execute(
                """
                UPDATE paper_positions
                SET state=?, opened_at=?, closed_at=?, last_evaluated_bar_time=?,
                    bars_waited=?, bars_held=?, intrabar_conflict=?, tie_breaker_policy=?,
                    entry_fill_price=?, exit_price=?, exit_fill_price=?, exit_reason=?,
                    entry_fee=?, exit_fee=?, total_fees=?, slippage_cost=?, spread_cost=?, total_cost=?,
                    gross_pnl=?, net_pnl=?, gross_r_multiple=?, net_r_multiple=?
                WHERE position_id=?
                """,
                (
                    new_state,
                    eval_out.get("opened_at"),
                    eval_out.get("closed_at"),
                    eval_out.get("last_evaluated_bar_time"),
                    int(eval_out.get("bars_waited", 0) or 0),
                    int(eval_out.get("bars_held", 0) or 0),
                    1 if eval_out.get("intrabar_conflict") else 0,
                    str(eval_out.get("tie_breaker_policy") or eff_tie),
                    eval_out.get("entry_fill_price"),
                    eval_out.get("exit_price"),
                    eval_out.get("exit_fill_price"),
                    eval_out.get("exit_reason"),
                    eval_out.get("entry_fee", 0.0),
                    eval_out.get("exit_fee", 0.0),
                    eval_out.get("total_fees", 0.0),
                    eval_out.get("slippage_cost", 0.0),
                    eval_out.get("spread_cost", 0.0),
                    eval_out.get("total_cost", 0.0),
                    eval_out.get("gross_pnl"),
                    eval_out.get("net_pnl"),
                    eval_out.get("gross_r_multiple"),
                    eval_out.get("net_r_multiple"),
                    pos_dict["position_id"],
                ),
            )

            sig_id = pos_dict.get("signal_id")
            if new_state in TERMINAL_POSITION_STATES and eval_out.get("changed"):
                res_label = str(eval_out.get("result") or eval_out.get("exit_reason") or new_state)
                r_gross = float(eval_out.get("gross_r_multiple") or 0.0)
                r_net = float(eval_out.get("net_r_multiple") or 0.0)
                net_pnl_v = float(eval_out.get("net_pnl") or 0.0)
                gross_pnl_v = float(eval_out.get("gross_pnl") or 0.0)
                tot_cost_v = float(eval_out.get("total_cost") or 0.0)
                closed_iso = str(eval_out.get("closed_at") or datetime.now(timezone.utc).isoformat())

                if sig_id is not None:
                    con.execute(
                        """
                        UPDATE signals
                        SET closed_at=?, result=?, r_multiple=?, exit_price=?,
                            position_state=?, gross_pnl=?, net_pnl=?,
                            total_fees=?, slippage_cost=?, total_cost=?,
                            gross_r_multiple=?, net_r_multiple=?, exit_reason=?,
                            intrabar_conflict=?, tie_breaker_policy=?,
                            opened_at=?, last_evaluated_bar_time=?
                        WHERE id=?
                        """,
                        (
                            closed_iso,
                            res_label,
                            r_gross,
                            eval_out.get("exit_price"),
                            new_state,
                            gross_pnl_v,
                            net_pnl_v,
                            eval_out.get("total_fees", 0.0),
                            eval_out.get("slippage_cost", 0.0),
                            tot_cost_v,
                            r_gross,
                            r_net,
                            res_label,
                            1 if eval_out.get("intrabar_conflict") else 0,
                            str(eval_out.get("tie_breaker_policy") or eff_tie),
                            eval_out.get("opened_at"),
                            eval_out.get("last_evaluated_bar_time"),
                            int(sig_id),
                        ),
                    )
                    memory_txt = (
                        f"{sym} {pos_dict['direction']} selesai {res_label} (state={new_state}). "
                        f"Entry={pos_dict['entry']}, Exit={eval_out.get('exit_price')}, "
                        f"GrossR={r_gross:+.2f}R, NetR={r_net:+.2f}R, NetPnL=${net_pnl_v:+.2f}. "
                        "Gunakan hasil ini sebagai histori performa, bukan jaminan."
                    )
                    con.execute(
                        """
                        INSERT INTO memories
                        (created_at,symbol,memory_type,content,signal_id)
                        VALUES (?,?,?,?,?)
                        """,
                        (closed_iso, sym, "TRADE_OUTCOME", memory_txt, int(sig_id)),
                    )

                eq_before = running_equity
                eq_after = round(eq_before + net_pnl_v, 4)
                running_equity = eq_after
                rem_margin_row = con.execute(
                    "SELECT COALESCE(SUM(margin_required), 0.0) FROM paper_positions WHERE state='OPEN'"
                ).fetchone()
                rem_margin = float(rem_margin_row[0] or 0.0) if rem_margin_row else 0.0
                free_eq = round(max(0.0, eq_after - rem_margin), 4)

                con.execute(
                    """
                    INSERT INTO paper_account_ledger
                    (event_type, position_id, signal_id, symbol, equity_before, equity_after,
                     cash_delta, reserved_margin, free_equity, gross_pnl, total_cost, net_pnl, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "POSITION_CLOSED",
                        pos_dict["position_id"],
                        sig_id,
                        sym,
                        eq_before,
                        eq_after,
                        net_pnl_v,
                        rem_margin,
                        free_eq,
                        gross_pnl_v,
                        tot_cost_v,
                        net_pnl_v,
                        closed_iso,
                    ),
                )

                updated_legacy_tuples.append((sym, res_label, r_gross))
                detailed_updates.append(eval_out)
            elif sig_id is not None and eval_out.get("changed"):
                con.execute(
                    "UPDATE signals SET position_state=?, opened_at=?, last_evaluated_bar_time=? WHERE id=?",
                    (new_state, eval_out.get("opened_at"), eval_out.get("last_evaluated_bar_time"), int(sig_id)),
                )
                detailed_updates.append(eval_out)

        con.commit()
        return detailed_updates if return_detailed else updated_legacy_tuples
    finally:
        con.close()


def compute_dual_performance_metrics(
    db_file: str = DEFAULT_DB_FILE,
    initial_equity: float = 10_000.0,
) -> Dict[str, Any]:
    """
    Compute performance metrics while keeping legacy R-snapshot rows (`origin='legacy'`)
    and V6 realistic ledger rows (`origin='v6_runtime'`, `paper_positions`) explicitly separated (F33, F36, T23, T24, T25).
    """
    run_migrations(db_file)
    con = connect_db(db_file)
    try:
        closed_sigs = con.execute(
            """
            SELECT * FROM signals
            WHERE result IN ('TP','SL','TIME_STOP')
            AND final_decision IN ('LONG','SHORT')
            ORDER BY id DESC
            """
        ).fetchall()

        open_count = con.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE result IS NULL
            AND final_decision IN ('LONG','SHORT')
            AND COALESCE(position_state, 'OPEN') = 'OPEN'
            """
        ).fetchone()[0]

        pending_count = con.execute(
            """
            SELECT COUNT(*) FROM paper_positions
            WHERE state = 'PENDING_ENTRY'
            """
        ).fetchone()[0]

        expired_count = con.execute(
            """
            SELECT COUNT(*) FROM paper_positions
            WHERE state = 'EXPIRED_UNFILLED'
            """
        ).fetchone()[0]

        wait_count = con.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE final_decision = 'WAIT'
            """
        ).fetchone()[0]

        v6_closed_positions = [
            dict(r)
            for r in con.execute(
                """
                SELECT p.*, s.confidence AS signal_confidence
                FROM paper_positions p
                LEFT JOIN signals s ON p.signal_id = s.id
                WHERE p.state IN ('CLOSED_TP', 'CLOSED_SL', 'CLOSED_TIME_STOP', 'CLOSED_INVALIDATED')
                AND p.origin != 'legacy'
                ORDER BY p.closed_at DESC
                """
            ).fetchall()
        ]
        for _p in v6_closed_positions:
            if _p.get("confidence") is None and _p.get("signal_confidence") is not None:
                _p["confidence"] = _p["signal_confidence"]

        legacy_closed_sigs = [
            dict(r)
            for r in closed_sigs
            if str(r["origin"] if "origin" in r.keys() and r["origin"] else "legacy") == "legacy"
        ]

        open_margin_row = con.execute(
            "SELECT COALESCE(SUM(margin_required), 0.0), COALESCE(SUM(notional_value), 0.0) FROM paper_positions WHERE state='OPEN'"
        ).fetchone()
        reserved_margin = float(open_margin_row[0] or 0.0) if open_margin_row else 0.0
        reserved_notional = float(open_margin_row[1] or 0.0) if open_margin_row else 0.0
    finally:
        con.close()

    # V6 Realistic Ledger summary
    v6_Realized_trades = [p for p in v6_closed_positions if p["state"] in {"CLOSED_TP", "CLOSED_SL", "CLOSED_TIME_STOP"}]
    v6_tp = sum(1 for p in v6_Realized_trades if p["state"] == "CLOSED_TP")
    v6_sl = sum(1 for p in v6_Realized_trades if p["state"] == "CLOSED_SL")
    v6_tstop = sum(1 for p in v6_Realized_trades if p["state"] == "CLOSED_TIME_STOP")
    v6_gross_pnl = round(sum(float(p.get("gross_pnl") or 0.0) for p in v6_Realized_trades), 4)
    v6_fees = round(sum(float(p.get("total_fees") or 0.0) for p in v6_Realized_trades), 4)
    v6_slip = round(sum(float(p.get("slippage_cost") or 0.0) for p in v6_Realized_trades), 4)
    v6_spread = round(sum(float(p.get("spread_cost") or 0.0) for p in v6_Realized_trades), 4)
    v6_cost = round(sum(float(p.get("total_cost") or 0.0) for p in v6_Realized_trades), 4)
    v6_net_pnl = round(sum(float(p.get("net_pnl") or 0.0) for p in v6_Realized_trades), 4)
    v6_avg_gross_r = (
        round(sum(float(p.get("gross_r_multiple") or 0.0) for p in v6_Realized_trades) / len(v6_Realized_trades), 4)
        if v6_Realized_trades else 0.0
    )
    v6_avg_net_r = (
        round(sum(float(p.get("net_r_multiple") or 0.0) for p in v6_Realized_trades) / len(v6_Realized_trades), 4)
        if v6_Realized_trades else 0.0
    )
    v6_pos_net = sum(max(float(p.get("net_pnl") or 0.0), 0.0) for p in v6_Realized_trades)
    v6_neg_net = abs(sum(min(float(p.get("net_pnl") or 0.0), 0.0) for p in v6_Realized_trades))
    v6_net_pf = round(v6_pos_net / v6_neg_net, 4) if v6_neg_net > 0 else None

    curr_equity = round(float(initial_equity) + v6_net_pnl, 4)
    free_equity = round(max(0.0, curr_equity - reserved_margin), 4)
    reconciled = bool(
        math.isclose(v6_net_pnl, v6_gross_pnl - v6_cost, rel_tol=1e-9, abs_tol=1e-6)
        and math.isclose(curr_equity, float(initial_equity) + v6_net_pnl, rel_tol=1e-9, abs_tol=1e-6)
        and math.isclose(free_equity + reserved_margin, curr_equity, rel_tol=1e-9, abs_tol=1e-6)
    )

    v6_realistic_summary = {
        "accounting_mode": REALISTIC_ACCOUNTING_MODE,
        "accounting_quality": REALISTIC_ACCOUNTING_QUALITY,
        "closed": len(v6_Realized_trades),
        "open": int(open_count),
        "pending_entry": int(pending_count),
        "expired_unfilled": int(expired_count),
        "observed_wait": int(wait_count),
        "closed_tp": v6_tp,
        "closed_sl": v6_sl,
        "closed_time_stop": v6_tstop,
        "win_rate_net": round(sum(1 for p in v6_Realized_trades if float(p.get("net_pnl") or 0.0) > 0) / len(v6_Realized_trades) * 100.0, 2) if v6_Realized_trades else 0.0,
        "total_gross_pnl": v6_gross_pnl,
        "total_fees": v6_fees,
        "total_slippage_cost": v6_slip,
        "total_spread_cost": v6_spread,
        "total_cost": v6_cost,
        "total_net_pnl": v6_net_pnl,
        "avg_gross_r": v6_avg_gross_r,
        "avg_net_r": v6_avg_net_r,
        "net_profit_factor": v6_net_pf,
        "initial_equity": round(float(initial_equity), 4),
        "current_equity": curr_equity,
        "reserved_margin": round(reserved_margin, 4),
        "reserved_notional": round(reserved_notional, 4),
        "free_equity": free_equity,
        "accounting_reconciled": reconciled,
        "analytics": compute_expectancy_and_drawdown_analytics(v6_Realized_trades, initial_equity=initial_equity),
        "calibration": compute_confidence_calibration_report(v6_Realized_trades),
        "positions": v6_closed_positions,
    }

    legacy_summary = {
        "accounting_mode": LEGACY_ACCOUNTING_MODE,
        "accounting_quality": "legacy_snapshot_only",
        "closed": len(legacy_closed_sigs),
        "wins": sum(1 for x in legacy_closed_sigs if x.get("result") == "TP"),
        "losses": sum(1 for x in legacy_closed_sigs if x.get("result") == "SL"),
        "avg_r": round(sum(float(x.get("r_multiple") or 0.0) for x in legacy_closed_sigs) / len(legacy_closed_sigs), 4) if legacy_closed_sigs else 0.0,
    }

    if not closed_sigs:
        return {
            "closed": 0,
            "open": int(open_count),
            "pending_entry": int(pending_count),
            "expired_unfilled": int(expired_count),
            "observed_wait": int(wait_count),
            "sample_size": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "avg_net_r": 0.0,
            "total_gross_pnl": 0.0,
            "total_cost": 0.0,
            "total_net_pnl": 0.0,
            "profit_factor": None,
            "profit_factor_status": "NO_CLOSED_TRADES",
            "accounting_mode": LEGACY_ACCOUNTING_MODE,
            "v6_realistic": v6_realistic_summary,
            "legacy_snapshot": legacy_summary,
            "accounting_disclaimer": (
                "Metrik R-multiple legacy (snapshot TP=+2R / SL=-1R) dipisahkan dari buku kas/equity bersih V6 (`v6_realistic`)."
            ),
            "confidence_disclaimer": "Confidence adalah skor internal model (0-100), bukan probabilitas menang terkalibrasi.",
            "rows": [],
        }

    wins = sum(1 for x in closed_sigs if x["result"] == "TP")
    losses = sum(1 for x in closed_sigs if x["result"] == "SL")
    positive = sum(max(float(x["r_multiple"] or 0.0), 0.0) for x in closed_sigs)
    negative = abs(sum(min(float(x["r_multiple"] or 0.0), 0.0) for x in closed_sigs))

    if negative > 0:
        pf_val: Optional[float] = round(positive / negative, 4)
        pf_status = "MEASURED_R_RATIO"
    elif positive > 0:
        pf_val = None
        pf_status = "UNDEFINED_NO_LOSSES"
    else:
        pf_val = None
        pf_status = "ZERO_ACTIVITY"

    return {
        "closed": len(closed_sigs),
        "open": int(open_count),
        "pending_entry": int(pending_count),
        "expired_unfilled": int(expired_count),
        "observed_wait": int(wait_count),
        "sample_size": len(closed_sigs),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(closed_sigs) * 100.0, 2),
        "avg_r": round(sum(float(x["r_multiple"] or 0.0) for x in closed_sigs) / len(closed_sigs), 4),
        "avg_net_r": v6_avg_net_r,
        "total_gross_pnl": v6_gross_pnl,
        "total_cost": v6_cost,
        "total_net_pnl": v6_net_pnl,
        "profit_factor": pf_val,
        "profit_factor_status": pf_status,
        "accounting_mode": LEGACY_ACCOUNTING_MODE,
        "v6_realistic": v6_realistic_summary,
        "legacy_snapshot": legacy_summary,
        "accounting_disclaimer": (
            "Metrik R-multiple legacy (snapshot TP=+2R / SL=-1R) dipisahkan dari buku kas/equity bersih V6 (`v6_realistic`)."
        ),
        "confidence_disclaimer": "Confidence adalah skor internal model (0-100), bukan probabilitas menang terkalibrasi.",
        "rows": closed_sigs,
    }


# ---------------------------------------------------------
# Durable Monitor Worker Heartbeat Repository (PH07-01 / F29)
# ---------------------------------------------------------
_MONITOR_HB_LOCK = threading.Lock()


def record_worker_heartbeat(
    worker_id: str = "default_worker",
    *,
    execution_mode: str = "worker",
    status: str = "RUNNING",
    pid: Optional[int] = None,
    interval_seconds: int = 45,
    monitor_n: int = 8,
    max_validations: int = 1,
    cycles_delta: int = 0,
    candidates_delta: int = 0,
    validations_delta: int = 0,
    signals_delta: int = 0,
    reconciled_delta: int = 0,
    heartbeat_ms: Optional[int] = None,
    last_cycle_at: Optional[str] = None,
    last_error: str = "",
    db_file: str = DEFAULT_DB_FILE,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Durably record or update a background/CLI worker heartbeat in SQLite (`monitor_runtime_state`)
    with secret redaction on `last_error` (PH07-01 / F29, F35).
    """
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    hb_ms = int(heartbeat_ms) if heartbeat_ms is not None else int(time.time() * 1000)
    eff_pid = int(pid) if pid is not None else os.getpid()
    safe_err = str(redact_secrets(str(last_error or ""), extra_secrets=extra_secrets))[:500]
    wid = str(worker_id or "default_worker").strip()

    with _MONITOR_HB_LOCK:
        con = connect_db(db_file)
        con.isolation_level = None
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM monitor_runtime_state WHERE worker_id=? LIMIT 1",
                (wid,),
            ).fetchone()
            if existing is None:
                con.execute(
                    """
                    INSERT INTO monitor_runtime_state
                    (worker_id, execution_mode, status, pid, interval_seconds, monitor_n, max_validations,
                     cycles_completed, candidates_scanned, ai_validations_run, signals_saved,
                     paper_trades_reconciled, last_heartbeat_ms, last_cycle_at, last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        wid,
                        str(execution_mode or "worker"),
                        str(status or "RUNNING").upper(),
                        eff_pid,
                        max(1, int(interval_seconds or 45)),
                        max(1, int(monitor_n or 8)),
                        max(1, int(max_validations or 1)),
                        max(0, int(cycles_delta)),
                        max(0, int(candidates_delta)),
                        max(0, int(validations_delta)),
                        max(0, int(signals_delta)),
                        max(0, int(reconciled_delta)),
                        hb_ms,
                        last_cycle_at,
                        safe_err,
                        now_iso,
                    ),
                )
            else:
                new_cycles = int(existing["cycles_completed"] or 0) + max(0, int(cycles_delta))
                new_cands = int(existing["candidates_scanned"] or 0) + max(0, int(candidates_delta))
                new_vals = int(existing["ai_validations_run"] or 0) + max(0, int(validations_delta))
                new_sigs = int(existing["signals_saved"] or 0) + max(0, int(signals_delta))
                new_recs = int(existing["paper_trades_reconciled"] or 0) + max(0, int(reconciled_delta))
                eff_cycle_at = last_cycle_at if last_cycle_at is not None else existing["last_cycle_at"]
                con.execute(
                    """
                    UPDATE monitor_runtime_state
                    SET execution_mode=?, status=?, pid=?, interval_seconds=?, monitor_n=?, max_validations=?,
                        cycles_completed=?, candidates_scanned=?, ai_validations_run=?, signals_saved=?,
                        paper_trades_reconciled=?, last_heartbeat_ms=?, last_cycle_at=?, last_error=?, updated_at=?
                    WHERE worker_id=?
                    """,
                    (
                        str(execution_mode or "worker"),
                        str(status or "RUNNING").upper(),
                        eff_pid,
                        max(1, int(interval_seconds or 45)),
                        max(1, int(monitor_n or 8)),
                        max(1, int(max_validations or 1)),
                        new_cycles,
                        new_cands,
                        new_vals,
                        new_sigs,
                        new_recs,
                        hb_ms,
                        eff_cycle_at,
                        safe_err,
                        now_iso,
                        wid,
                    ),
                )
            con.execute("COMMIT")
            row = con.execute("SELECT * FROM monitor_runtime_state WHERE worker_id=? LIMIT 1", (wid,)).fetchone()
            return dict(row) if row else {}
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def get_worker_heartbeat_state(
    worker_id: Optional[str] = "default_worker",
    db_file: str = DEFAULT_DB_FILE,
) -> Optional[Dict[str, Any]]:
    """Retrieve durable worker heartbeat state from SQLite (`monitor_runtime_state`)."""
    run_migrations(db_file)
    with _MONITOR_HB_LOCK:
        con = connect_db(db_file)
        try:
            if worker_id:
                row = con.execute(
                    "SELECT * FROM monitor_runtime_state WHERE worker_id=? LIMIT 1",
                    (str(worker_id),),
                ).fetchone()
                if row is not None:
                    return dict(row)
            row_any = con.execute(
                "SELECT * FROM monitor_runtime_state ORDER BY last_heartbeat_ms DESC LIMIT 1"
            ).fetchone()
            return dict(row_any) if row_any is not None else None
        finally:
            con.close()


def clear_worker_heartbeats(
    worker_id: Optional[str] = None,
    db_file: str = DEFAULT_DB_FILE,
) -> int:
    """Delete worker heartbeat records (all or specific worker_id)."""
    run_migrations(db_file)
    with _MONITOR_HB_LOCK:
        con = connect_db(db_file)
        try:
            if worker_id:
                cur = con.execute("DELETE FROM monitor_runtime_state WHERE worker_id=?", (str(worker_id),))
            else:
                cur = con.execute("DELETE FROM monitor_runtime_state")
            con.commit()
            return int(cur.rowcount or 0)
        finally:
            con.close()

# ---------------------------------------------------------
# Persistent Risk Guard State & Telegram Outbox Repositories (Phases 3, 4, 5)
# ---------------------------------------------------------
_OUTBOX_LOCK = threading.Lock()
_RISK_STATE_LOCK = threading.Lock()


def get_persisted_risk_guard_state(
    db_file: str = DEFAULT_DB_FILE,
    *,
    target_symbol: Optional[str] = None,
    initial_equity: float = 10_000.0,
    max_daily_loss_fraction: float = 0.030,
    max_consecutive_losses: int = 4,
    today_utc_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load and compute durable Risk Guard portfolio state from SQLite across restarts (Phase 3.2):
    - Current equity (initial_equity + realized net PnL)
    - Active open paper positions (`state='OPEN'`), total open notional, total open risk, open symbols
    - Correlated open positions & correlated notional for `target_symbol`
    - Today's realized loss in USD (`daily_realized_loss_usd`)
    - Current consecutive losses streak (`consecutive_losses`)
    """
    from .risk import GLOBAL_RISK_LEDGER, get_correlation_group

    run_migrations(db_file)
    day_prefix = str(today_utc_str or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]

    with _RISK_STATE_LOCK:
        con = connect_db(db_file)
        try:
            open_rows = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM paper_positions WHERE state='OPEN' ORDER BY created_at ASC"
                ).fetchall()
            ]
            closed_rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT * FROM paper_positions
                    WHERE state IN ('CLOSED_TP', 'CLOSED_SL', 'CLOSED_TIME_STOP')
                    AND origin != 'legacy'
                    ORDER BY closed_at ASC, created_at ASC
                    """
                ).fetchall()
            ]
            rg_row = con.execute(
                "SELECT * FROM risk_guard_state WHERE account_id='default' LIMIT 1"
            ).fetchone()
            rg_meta = dict(rg_row) if rg_row else {}
        finally:
            con.close()

    realized_net_total = sum(float(r.get("net_pnl") or 0.0) for r in closed_rows)
    curr_equity = round(max(0.0, float(initial_equity) + realized_net_total), 4)

    open_notional = round(sum(float(r.get("notional_value") or 0.0) for r in open_rows), 4)
    open_margin = round(sum(float(r.get("margin_required") or r.get("notional_value") or 0.0) for r in open_rows), 4)
    open_risk = round(sum(float(r.get("risk_budget") or 0.0) for r in open_rows), 4)
    open_syms = [str(r.get("symbol", "")).upper().strip() for r in open_rows if r.get("symbol")]

    corr_positions = 0
    corr_notional = 0.0
    if target_symbol:
        t_grp = get_correlation_group(target_symbol)
        for r in open_rows:
            if get_correlation_group(str(r.get("symbol", ""))) == t_grp:
                corr_positions += 1
                corr_notional += float(r.get("notional_value") or 0.0)
    corr_notional = round(corr_notional, 4)

    # Compute daily realized loss (sum of negative net_pnl on today_utc_str)
    daily_loss_from_trades = 0.0
    for r in closed_rows:
        c_at = str(r.get("closed_at") or "")
        pnl = float(r.get("net_pnl") or 0.0)
        if c_at.startswith(day_prefix) and pnl < 0.0:
            daily_loss_from_trades += abs(pnl)

    # Compute current consecutive losses from tail of closed_rows
    consec_losses = 0
    for r in reversed(closed_rows):
        pnl = float(r.get("net_pnl") or 0.0)
        nr = float(r.get("net_r_multiple") or 0.0)
        if pnl < 0.0 or (pnl == 0.0 and nr < 0.0) or r.get("state") == "CLOSED_SL":
            consec_losses += 1
        elif pnl > 0.0 or r.get("state") == "CLOSED_TP":
            break

    if rg_meta.get("trading_day_utc") == day_prefix:
        daily_loss = round(max(daily_loss_from_trades, float(rg_meta.get("daily_realized_loss_usd") or 0.0)), 4)
    else:
        daily_loss = round(daily_loss_from_trades, 4)
    consec_losses = max(consec_losses, int(rg_meta.get("consecutive_losses") or 0) if not closed_rows else consec_losses)

    max_daily_loss_usd = round(curr_equity * float(max_daily_loss_fraction), 4)
    daily_breached = bool(daily_loss >= max_daily_loss_usd - 1e-9 and max_daily_loss_usd > 0)
    consec_breached = bool(consec_losses >= int(max_consecutive_losses) and int(max_consecutive_losses) > 0)

    # Sync in-memory GLOBAL_RISK_LEDGER
    GLOBAL_RISK_LEDGER.sync_from_persisted_state(
        current_equity=curr_equity,
        open_positions=open_rows,
        daily_realized_loss_usd=daily_loss,
        consecutive_losses=consec_losses,
    )

    return {
        "current_equity": curr_equity,
        "free_equity": round(max(0.0, curr_equity - open_margin), 4),
        "current_open_positions": len(open_rows),
        "current_open_notional": open_notional,
        "current_open_margin": open_margin,
        "current_open_risk": open_risk,
        "open_symbols": open_syms,
        "open_positions": open_rows,
        "correlated_open_positions": corr_positions,
        "correlated_open_notional": corr_notional,
        "trading_day_utc": day_prefix,
        "daily_realized_loss_usd": daily_loss,
        "max_daily_loss_usd": max_daily_loss_usd,
        "daily_loss_limit_breached": daily_breached,
        "consecutive_losses": consec_losses,
        "max_consecutive_losses": int(max_consecutive_losses),
        "consecutive_loss_limit_breached": consec_breached,
        "last_rejection_reason": str(rg_meta.get("last_rejection_reason") or ""),
        "last_rejected_symbol": str(rg_meta.get("last_rejected_symbol") or ""),
        "last_rejected_at": rg_meta.get("last_rejected_at"),
    }


def record_risk_guard_rejection(
    symbol: str,
    veto_codes: Iterable[str],
    reason: str = "",
    *,
    daily_realized_loss_usd: Optional[float] = None,
    consecutive_losses: Optional[int] = None,
    db_file: str = DEFAULT_DB_FILE,
) -> Dict[str, Any]:
    """Persist a Risk Guard rejection event in SQLite `risk_guard_state` (Phase 3.2)."""
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    day_prefix = now_iso[:10]
    codes_list = [str(c) for c in (veto_codes or ()) if c]
    reason_str = str(reason or ", ".join(codes_list) or "RISK_GUARD_REJECTED")[:500]
    sym_u = str(symbol or "UNKNOWN").upper().strip()

    with _RISK_STATE_LOCK:
        con = connect_db(db_file)
        try:
            existing = con.execute("SELECT * FROM risk_guard_state WHERE account_id='default' LIMIT 1").fetchone()
            d_loss = float(daily_realized_loss_usd) if daily_realized_loss_usd is not None else (float(existing["daily_realized_loss_usd"]) if existing else 0.0)
            c_loss = int(consecutive_losses) if consecutive_losses is not None else (int(existing["consecutive_losses"]) if existing else 0)
            con.execute(
                """
                INSERT OR REPLACE INTO risk_guard_state
                (account_id, trading_day_utc, daily_realized_loss_usd, consecutive_losses,
                 last_rejection_reason, last_rejected_symbol, last_rejected_at, updated_at)
                VALUES ('default', ?, ?, ?, ?, ?, ?, ?)
                """,
                (day_prefix, d_loss, c_loss, reason_str, sym_u, now_iso, now_iso),
            )
            con.commit()
            row = con.execute("SELECT * FROM risk_guard_state WHERE account_id='default' LIMIT 1").fetchone()
            return dict(row) if row else {}
        finally:
            con.close()


def check_and_record_telegram_outbox(
    *,
    event_key: str,
    event_type: str,
    signal_id: str,
    symbol: str,
    direction: str = "WAIT",
    signal_state: str = "WAIT",
    timeframe: str = "15m",
    message_hash: str = "",
    db_file: str = DEFAULT_DB_FILE,
) -> Dict[str, Any]:
    """
    Atomically check if a Telegram notification `event_key` has already been sent (`status='SENT'`),
    and if not, insert or increment its attempt counter in `telegram_outbox` (Phase 4).
    """
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _OUTBOX_LOCK:
        con = connect_db(db_file)
        con.isolation_level = None
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM telegram_outbox WHERE event_key=? LIMIT 1",
                (str(event_key),),
            ).fetchone()
            if row is not None and str(row["status"]).upper() == "SENT":
                con.execute("COMMIT")
                return {"already_sent": True, "record": dict(row)}

            if row is None:
                con.execute(
                    """
                    INSERT INTO telegram_outbox
                    (event_key, event_type, signal_id, symbol, direction, signal_state, timeframe,
                     status, attempt_count, message_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 1, ?, ?)
                    """,
                    (
                        str(event_key),
                        str(event_type),
                        str(signal_id),
                        str(symbol),
                        str(direction),
                        str(signal_state),
                        str(timeframe),
                        str(message_hash),
                        now_iso,
                    ),
                )
            else:
                con.execute(
                    "UPDATE telegram_outbox SET attempt_count = attempt_count + 1 WHERE event_key=?",
                    (str(event_key),),
                )
            con.execute("COMMIT")
            updated = con.execute("SELECT * FROM telegram_outbox WHERE event_key=? LIMIT 1", (str(event_key),)).fetchone()
            return {"already_sent": False, "record": dict(updated) if updated else {}}
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def mark_telegram_outbox_status(
    *,
    event_key: str,
    status: str,
    last_error: str = "",
    db_file: str = DEFAULT_DB_FILE,
    extra_secrets: Optional[Iterable[str]] = None,
) -> None:
    """Update the delivery status of a `telegram_outbox` entry (Phase 4)."""
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    st_u = str(status or "PENDING").upper().strip()
    safe_err = str(redact_secrets(str(last_error or ""), extra_secrets=extra_secrets))[:400]
    with _OUTBOX_LOCK:
        con = connect_db(db_file)
        try:
            con.execute(
                """
                UPDATE telegram_outbox
                SET status=?, last_error=?, sent_at=CASE WHEN ?='SENT' THEN ? ELSE sent_at END
                WHERE event_key=?
                """,
                (st_u, safe_err, st_u, now_iso, str(event_key)),
            )
            con.commit()
        finally:
            con.close()



# ---------------------------------------------------------
# Master Prompt 2: Live/Testnet Account, Orders, Positions, Circuit Breakers & Audit Persistence
# ---------------------------------------------------------
_LIVE_EXEC_LOCK = threading.Lock()


def save_account_snapshot(
    snapshot: Dict[str, Any],
    *,
    db_file: str = DEFAULT_DB_FILE,
    extra_secrets: Optional[Iterable[str]] = None,
) -> int:
    """Persist an authenticated exchange account snapshot to SQLite (Section 3)."""
    run_migrations(db_file)
    now_iso = str(snapshot.get("last_sync_iso") or datetime.now(timezone.utc).isoformat())
    safe_snap = redact_secrets(snapshot, extra_secrets=extra_secrets)
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            cur = con.execute(
                """
                INSERT INTO account_snapshots
                (exchange, market_type, execution_mode, total_wallet_balance, available_balance,
                 account_equity, unrealized_pnl, realized_pnl, used_margin, available_margin,
                 open_positions_count, active_orders_count, withdrawal_enabled, sync_ok,
                 is_consistent, api_latency_ms, error_code, error_message, snapshot_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(safe_snap.get("exchange") or "BINANCE"),
                    str(safe_snap.get("market_type") or "FUTURES_USDM"),
                    str(safe_snap.get("execution_mode") or "PAPER"),
                    float(safe_snap.get("total_wallet_balance") or 0.0),
                    float(safe_snap.get("available_balance") or 0.0),
                    float(safe_snap.get("account_equity") or 0.0),
                    float(safe_snap.get("unrealized_pnl") or 0.0),
                    float(safe_snap.get("realized_pnl") or 0.0),
                    float(safe_snap.get("used_margin") or 0.0),
                    float(safe_snap.get("available_margin") or 0.0),
                    len(safe_snap.get("open_positions") or []),
                    len(safe_snap.get("active_orders") or []),
                    1 if safe_snap.get("withdrawal_enabled") else 0,
                    1 if safe_snap.get("sync_ok", True) else 0,
                    1 if safe_snap.get("is_consistent", True) else 0,
                    float(safe_snap.get("api_latency_ms") or 0.0),
                    str(safe_snap.get("error_code") or ""),
                    str(safe_snap.get("error_message") or "")[:400],
                    json.dumps(safe_snap, ensure_ascii=False, default=str),
                    now_iso,
                ),
            )
            con.commit()
            return int(cur.lastrowid or 0)
        finally:
            con.close()


def get_latest_account_snapshot(
    db_file: str = DEFAULT_DB_FILE,
    *,
    execution_mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve the latest persisted exchange account snapshot from SQLite."""
    run_migrations(db_file)
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            if execution_mode:
                row = con.execute(
                    "SELECT * FROM account_snapshots WHERE execution_mode=? ORDER BY id DESC LIMIT 1",
                    (str(execution_mode).upper().strip(),),
                ).fetchone()
            else:
                row = con.execute("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def upsert_live_order(
    order: Dict[str, Any],
    *,
    db_file: str = DEFAULT_DB_FILE,
    extra_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Insert or update a live/testnet order record in SQLite with idempotency on `client_order_id` (Section 7)."""
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    safe_o = redact_secrets(order, extra_secrets=extra_secrets)
    coid = str(safe_o["client_order_id"]).strip()
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            existing = con.execute("SELECT created_at FROM live_orders WHERE client_order_id=? LIMIT 1", (coid,)).fetchone()
            c_at = str(existing["created_at"]) if existing else str(safe_o.get("created_at") or now_iso)
            con.execute(
                """
                INSERT OR REPLACE INTO live_orders
                (client_order_id, exchange_order_id, signal_id, position_id, symbol, execution_mode,
                 market_type, order_leg, side, order_type, status, price, stop_price, requested_qty,
                 executed_qty, avg_fill_price, fee_usd, reduce_only, raw_response_json, error_message,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    coid,
                    str(safe_o.get("exchange_order_id") or "") or None,
                    str(safe_o.get("signal_id") or "") or None,
                    str(safe_o.get("position_id") or "") or None,
                    str(safe_o.get("symbol") or "UNKNOWN").upper().strip(),
                    str(safe_o.get("execution_mode") or "TESTNET").upper().strip(),
                    str(safe_o.get("market_type") or "FUTURES_USDM").upper().strip(),
                    str(safe_o.get("order_leg") or "ENTRY").upper().strip(),
                    str(safe_o.get("side") or "BUY").upper().strip(),
                    str(safe_o.get("order_type") or "MARKET").upper().strip(),
                    str(safe_o.get("status") or "PENDING_SUBMIT").upper().strip(),
                    float(safe_o["price"]) if is_finite_number(safe_o.get("price")) else None,
                    float(safe_o["stop_price"]) if is_finite_number(safe_o.get("stop_price")) else None,
                    float(safe_o.get("requested_qty") or 0.0),
                    float(safe_o.get("executed_qty") or 0.0),
                    float(safe_o["avg_fill_price"]) if is_finite_number(safe_o.get("avg_fill_price")) else None,
                    float(safe_o.get("fee_usd") or 0.0),
                    1 if safe_o.get("reduce_only") else 0,
                    json.dumps(safe_o.get("raw_response") or {}, ensure_ascii=False, default=str),
                    str(safe_o.get("error_message") or "")[:400],
                    c_at,
                    now_iso,
                ),
            )
            con.commit()
            row = con.execute("SELECT * FROM live_orders WHERE client_order_id=? LIMIT 1", (coid,)).fetchone()
            return dict(row) if row else {}
        finally:
            con.close()


def get_live_order(client_order_id: str, db_file: str = DEFAULT_DB_FILE) -> Optional[Dict[str, Any]]:
    run_migrations(db_file)
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            row = con.execute("SELECT * FROM live_orders WHERE client_order_id=? LIMIT 1", (str(client_order_id),)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def list_live_orders(
    db_file: str = DEFAULT_DB_FILE,
    *,
    symbol: Optional[str] = None,
    active_only: bool = False,
    execution_mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    run_migrations(db_file)
    clauses: List[str] = []
    params: List[Any] = []
    if symbol:
        clauses.append("symbol=?")
        params.append(str(symbol).upper().strip())
    if execution_mode:
        clauses.append("execution_mode=?")
        params.append(str(execution_mode).upper().strip())
    if active_only:
        clauses.append("status IN ('PENDING_SUBMIT', 'NEW', 'PARTIALLY_FILLED', 'UNCERTAIN_TIMEOUT')")
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            rows = con.execute(f"SELECT * FROM live_orders {where_sql} ORDER BY updated_at DESC", tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def upsert_live_position(
    position: Dict[str, Any],
    *,
    db_file: str = DEFAULT_DB_FILE,
) -> Dict[str, Any]:
    """Insert or update a live/testnet position lifecycle record in SQLite, strictly separate from paper_positions (Section 8)."""
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    pid = str(position["position_id"]).strip()
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            existing = con.execute("SELECT created_at, opened_at FROM live_positions WHERE position_id=? LIMIT 1", (pid,)).fetchone()
            c_at = str(existing["created_at"]) if existing else str(position.get("created_at") or now_iso)
            o_at = str(position.get("opened_at") or (existing["opened_at"] if existing else "") or "") or None
            con.execute(
                """
                INSERT OR REPLACE INTO live_positions
                (position_id, signal_id, symbol, execution_mode, market_type, direction,
                 lifecycle_state, requested_qty, open_qty, closed_qty, entry_price,
                 avg_entry_fill_price, stop_loss, take_profit, avg_exit_fill_price,
                 notional_value, used_margin, leverage, risk_budget_usd, realized_pnl_usd,
                 unrealized_pnl_usd, total_fees_usd, entry_client_order_id, sl_client_order_id,
                 tp_client_order_id, exit_client_order_id, protection_verified,
                 reconciliation_status, reconciliation_detail, created_at, opened_at, closed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    str(position.get("signal_id") or "") or None,
                    str(position.get("symbol") or "UNKNOWN").upper().strip(),
                    str(position.get("execution_mode") or "TESTNET").upper().strip(),
                    str(position.get("market_type") or "FUTURES_USDM").upper().strip(),
                    str(position.get("direction") or "LONG").upper().strip(),
                    str(position.get("lifecycle_state") or "ENTRY_REQUESTED").upper().strip(),
                    float(position.get("requested_qty") or 0.0),
                    float(position.get("open_qty") or 0.0),
                    float(position.get("closed_qty") or 0.0),
                    float(position["entry_price"]) if is_finite_number(position.get("entry_price")) else None,
                    float(position["avg_entry_fill_price"]) if is_finite_number(position.get("avg_entry_fill_price")) else None,
                    float(position["stop_loss"]) if is_finite_number(position.get("stop_loss")) else None,
                    float(position["take_profit"]) if is_finite_number(position.get("take_profit")) else None,
                    float(position["avg_exit_fill_price"]) if is_finite_number(position.get("avg_exit_fill_price")) else None,
                    float(position.get("notional_value") or 0.0),
                    float(position.get("used_margin") or 0.0),
                    float(position.get("leverage") or 1.0),
                    float(position.get("risk_budget_usd") or 0.0),
                    float(position.get("realized_pnl_usd") or 0.0),
                    float(position.get("unrealized_pnl_usd") or 0.0),
                    float(position.get("total_fees_usd") or 0.0),
                    str(position.get("entry_client_order_id") or "") or None,
                    str(position.get("sl_client_order_id") or "") or None,
                    str(position.get("tp_client_order_id") or "") or None,
                    str(position.get("exit_client_order_id") or "") or None,
                    1 if position.get("protection_verified") else 0,
                    str(position.get("reconciliation_status") or "OK"),
                    str(position.get("reconciliation_detail") or "")[:400],
                    c_at,
                    o_at,
                    str(position.get("closed_at") or "") or None,
                    now_iso,
                ),
            )
            con.commit()
            row = con.execute("SELECT * FROM live_positions WHERE position_id=? LIMIT 1", (pid,)).fetchone()
            return dict(row) if row else {}
        finally:
            con.close()


def get_live_position(position_id: str, db_file: str = DEFAULT_DB_FILE) -> Optional[Dict[str, Any]]:
    run_migrations(db_file)
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            row = con.execute("SELECT * FROM live_positions WHERE position_id=? LIMIT 1", (str(position_id),)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def list_live_positions(
    db_file: str = DEFAULT_DB_FILE,
    *,
    open_only: bool = False,
    execution_mode: Optional[str] = None,
    symbol: Optional[str] = None,
) -> List[Dict[str, Any]]:
    run_migrations(db_file)
    clauses: List[str] = []
    params: List[Any] = []
    if open_only:
        clauses.append(
            "lifecycle_state NOT IN ('POSITION_CLOSED', 'CANCELED')"
            " AND (open_qty > 0 OR lifecycle_state IN ('ENTRY_REQUESTED', 'ENTRY_PARTIALLY_FILLED', 'POSITION_OPEN', 'PROTECTIVE_ORDERS_CONFIRMED', 'PROTECTION_MISSING_ALERT', 'RECONCILIATION_REQUIRED'))"
        )
    if execution_mode:
        clauses.append("execution_mode=?")
        params.append(str(execution_mode).upper().strip())
    if symbol:
        clauses.append("symbol=?")
        params.append(str(symbol).upper().strip())
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            rows = con.execute(f"SELECT * FROM live_positions {where_sql} ORDER BY updated_at DESC", tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def get_circuit_breaker_and_kill_switch_state(
    db_file: str = DEFAULT_DB_FILE,
    *,
    account_id: str = "default",
    today_utc_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read persistent circuit breaker and kill switch state from SQLite (Section 10 & 11).
    Automatically handles UTC daily boundary reset for daily realized/unrealized loss counters
    while preserving active Kill Switch or unresolved reconciliation/protection circuit breakers!
    """
    run_migrations(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    day_prefix = str(today_utc_str or now_iso[:10])
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            row = con.execute(
                "SELECT * FROM circuit_breaker_state WHERE account_id=? LIMIT 1",
                (str(account_id),),
            ).fetchone()
            if row is None:
                con.execute(
                    """
                    INSERT INTO circuit_breaker_state
                    (account_id, execution_mode, live_trading_authorized, kill_switch_active,
                     circuit_breaker_active, active_reasons_json, trading_day_utc, updated_at)
                    VALUES (?, 'PAPER', 0, 0, 0, '[]', ?, ?)
                    """,
                    (str(account_id), day_prefix, now_iso),
                )
                con.commit()
                row = con.execute(
                    "SELECT * FROM circuit_breaker_state WHERE account_id=? LIMIT 1",
                    (str(account_id),),
                ).fetchone()
            data = dict(row) if row else {}
            if data.get("trading_day_utc") != day_prefix:
                # Reset daily loss counters ONLY when the UTC date changes; preserve kill_switch_active
                con.execute(
                    """
                    UPDATE circuit_breaker_state
                    SET trading_day_utc=?, daily_realized_loss_usd=0.0, daily_unrealized_loss_usd=0.0, updated_at=?
                    WHERE account_id=?
                    """,
                    (day_prefix, now_iso, str(account_id)),
                )
                con.commit()
                row = con.execute(
                    "SELECT * FROM circuit_breaker_state WHERE account_id=? LIMIT 1",
                    (str(account_id),),
                ).fetchone()
                data = dict(row) if row else {}
            try:
                reasons = json.loads(data.get("active_reasons_json") or "[]")
            except Exception:
                reasons = []
            data["active_reasons"] = reasons if isinstance(reasons, list) else []
            data["kill_switch_active"] = bool(data.get("kill_switch_active"))
            data["circuit_breaker_active"] = bool(data.get("circuit_breaker_active"))
            data["live_trading_authorized"] = bool(data.get("live_trading_authorized"))
            return data
        finally:
            con.close()


def update_circuit_breaker_and_kill_switch_state(
    updates: Dict[str, Any],
    *,
    db_file: str = DEFAULT_DB_FILE,
    account_id: str = "default",
) -> Dict[str, Any]:
    """Atomically update persistent circuit breaker and kill switch state in SQLite."""
    current = get_circuit_breaker_and_kill_switch_state(db_file=db_file, account_id=account_id, today_utc_str=(updates or {}).get("trading_day_utc"))
    now_iso = datetime.now(timezone.utc).isoformat()
    merged = dict(current)
    merged.update(updates or {})
    reasons = merged.get("active_reasons", [])
    if isinstance(reasons, (list, tuple)):
        reasons_json = json.dumps(list(dict.fromkeys(str(r) for r in reasons if r)), ensure_ascii=False)
    else:
        reasons_json = str(merged.get("active_reasons_json") or "[]")

    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            con.execute(
                """
                UPDATE circuit_breaker_state
                SET execution_mode=?,
                    live_trading_authorized=?,
                    kill_switch_active=?,
                    kill_switch_reason=?,
                    kill_switch_activated_at=?,
                    kill_switch_verification_status=?,
                    circuit_breaker_active=?,
                    active_reasons_json=?,
                    trading_day_utc=?,
                    daily_realized_loss_usd=?,
                    daily_unrealized_loss_usd=?,
                    consecutive_losses=?,
                    consecutive_execution_failures=?,
                    consecutive_auth_failures=?,
                    reconciliation_status=?,
                    last_reconciled_at=?,
                    updated_at=?
                WHERE account_id=?
                """,
                (
                    str(merged.get("execution_mode") or "PAPER").upper().strip(),
                    1 if merged.get("live_trading_authorized") else 0,
                    1 if merged.get("kill_switch_active") else 0,
                    str(merged.get("kill_switch_reason") or "")[:400],
                    merged.get("kill_switch_activated_at"),
                    str(merged.get("kill_switch_verification_status") or "IDLE"),
                    1 if merged.get("circuit_breaker_active") else 0,
                    reasons_json,
                    str(merged.get("trading_day_utc") or now_iso[:10]),
                    float(merged.get("daily_realized_loss_usd") or 0.0),
                    float(merged.get("daily_unrealized_loss_usd") or 0.0),
                    int(merged.get("consecutive_losses") or 0),
                    int(merged.get("consecutive_execution_failures") or 0),
                    int(merged.get("consecutive_auth_failures") or 0),
                    str(merged.get("reconciliation_status") or "OK"),
                    merged.get("last_reconciled_at"),
                    now_iso,
                    str(account_id),
                ),
            )
            con.commit()
        finally:
            con.close()
    return get_circuit_breaker_and_kill_switch_state(db_file=db_file, account_id=account_id, today_utc_str=(updates or {}).get("trading_day_utc"))


def append_execution_audit_log(
    entry: Dict[str, Any],
    *,
    db_file: str = DEFAULT_DB_FILE,
    extra_secrets: Optional[Iterable[str]] = None,
) -> int:
    """
    Append a structured, secret-redacted audit log record for every trading decision,
    risk calculation, order submission, fill, protective order check, reconciliation,
    circuit breaker, or kill switch event (Section 1.10 & Section 14).
    """
    run_migrations(db_file)
    now_iso = str(entry.get("created_at") or datetime.now(timezone.utc).isoformat())
    safe_e = redact_secrets(entry, extra_secrets=extra_secrets)
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            cur = con.execute(
                """
                INSERT INTO execution_audit_log
                (created_at, event_type, execution_mode, symbol, signal_id, position_id,
                 decision_outcome, state_before, state_after, risk_inputs_json, order_ids_json,
                 exchange_response_json, fills_json, fees_usd, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    str(safe_e.get("event_type") or "DECISION_AUDIT").upper().strip(),
                    str(safe_e.get("execution_mode") or "PAPER").upper().strip(),
                    str(safe_e.get("symbol") or "SYSTEM").upper().strip(),
                    str(safe_e.get("signal_id") or "") or None,
                    str(safe_e.get("position_id") or "") or None,
                    str(safe_e.get("decision_outcome") or "RECORDED").upper().strip(),
                    str(safe_e.get("state_before") or "") or None,
                    str(safe_e.get("state_after") or "") or None,
                    json.dumps(safe_e.get("risk_inputs") or {}, ensure_ascii=False, default=str),
                    json.dumps(safe_e.get("order_ids") or {}, ensure_ascii=False, default=str),
                    json.dumps(safe_e.get("exchange_response") or {}, ensure_ascii=False, default=str),
                    json.dumps(safe_e.get("fills") or [], ensure_ascii=False, default=str),
                    float(safe_e.get("fees_usd") or 0.0),
                    str(safe_e.get("reason") or "")[:500],
                ),
            )
            con.commit()
            return int(cur.lastrowid or 0)
        finally:
            con.close()


def list_execution_audit_logs(
    db_file: str = DEFAULT_DB_FILE,
    *,
    limit: int = 100,
    symbol: Optional[str] = None,
) -> List[Dict[str, Any]]:
    run_migrations(db_file)
    with _LIVE_EXEC_LOCK:
        con = connect_db(db_file)
        try:
            if symbol:
                rows = con.execute(
                    "SELECT * FROM execution_audit_log WHERE symbol=? ORDER BY id DESC LIMIT ?",
                    (str(symbol).upper().strip(), int(limit)),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM execution_audit_log ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

# SecretAgent: Momentum Intelligence & Autonomous Live Trading Engine

SecretAgent is a risk-aware Momentum Trading Assistant and Controlled Autonomous Execution Engine for crypto markets (Binance Spot and USD-M Perpetual Futures). Its primary interface is **Momentum Radar**, backed by deterministic multi-timeframe Smart Money Concepts (SMC), a 5-pillar Momentum Evidence Engine, Walk-Forward Backtesting, SQLite-persisted Risk Guard & Circuit Breakers, an Emergency Kill Switch, and an Idempotent Order Execution & Position Reconciliation Engine.

> **Safety Default**: Live execution is **disabled by default** (`EXECUTION_MODE=PAPER`, `LIVE_TRADING_ENABLED=false`). The system never switches automatically from `PAPER` or `TESTNET` to `LIVE`, and never places real orders without passing the 13-point Live Readiness Gate and receiving explicit user authorization.

## 1. Architecture & Core Workflow

```
Exchange Account → Balance Synchronization → Momentum Signal → Risk Calculation → Risk Guard → Execution Gate → Order Execution → Protective SL/TP Confirmation → Position Management → PnL Reconciliation
```

* **`app.py`**: Primary Streamlit **Momentum Radar** dashboard, sidebar **Emergency Kill Switch**, secondary expandable panels (`Account Intelligence, Execution Gate & Audit Log`, `Risk Guard & Active Paper Positions`, `Performance Lab: Walk-Forward & Backtest`, `System Health`), and headless CLI (`--status`, `--reconcile-only`, `--monitor-once`, `--worker`).
* **`src/ai_consensus/exchange.py`**:
  * `BinanceExchangeAdapter`: Authenticated HMAC-SHA256 client for Binance Spot (`https://api.binance.com` / `https://testnet.binance.vision`) and Binance USD-M Futures (`https://fapi.binance.com` / `https://testnet.binancefuture.com`).
  * `parse_binance_spot_account` & `parse_binance_futures_account`: Authoritative account balance, equity, available margin, used margin, unrealized/realized PnL, open positions, active orders, and `exchangeInfo` precision/constraint parsers.
  * `AutonomousExecutionEngine`: Idempotent order submission (`generate_client_order_id`), pre-retry order status lookup (`query_order`) on network timeout, partial-fill handling, native protective Stop-Loss & Take-Profit placement and verification, and missing-protection circuit breaker / emergency handling.
  * `reconcile_live_exchange_state`: Startup and periodic exchange-to-local SQLite reconciliation where the exchange is authoritative.
  * `evaluate_and_persist_circuit_breakers`, `activate_kill_switch`, `deactivate_kill_switch`, `emergency_close_all_positions`, and `evaluate_live_readiness_gate`.
* **`src/ai_consensus/risk.py`**: Deterministic position sizing (`compute_position_sizing`), linear and inverse contract payoff formulas, tick/lot rounding (`round_price_to_tick`, `floor_quantity_to_lot`), pre-trade `evaluate_risk_guard`, and deterministic dynamic Stop-Loss / break-even / trailing-stop validation (`evaluate_dynamic_protective_levels`).
* **`src/ai_consensus/domain.py`**: Canonical Signal Lifecycle state machine (`DEVELOPING`, `TRIGGERED`, `WAIT`, `INVALIDATED`), 5-pillar orthogonal Momentum Evidence (`build_momentum_evidence_report`), and preflight market data integrity checks (`preflight_market_check`).
* **`src/ai_consensus/paper.py`**: Chronological walk-forward backtester (`run_walk_forward_replay_backtest`), 3-split Walk-Forward Validation (`run_walk_forward_folds`), segmented performance analytics (`compute_expectancy_and_drawdown_analytics`), and out-of-sample confidence calibration (`compute_confidence_calibration_report`).
* **`src/ai_consensus/storage.py`**: SQLite database migrations up to **Schema v8** (`008_autonomous_live_execution`), managing `signals`, `paper_positions`, `account_snapshots`, `live_orders`, `live_positions`, `risk_guard_state`, `circuit_breaker_state`, `execution_audit_log`, and `telegram_outbox`.
* **`src/ai_consensus/services.py` & `monitor.py`**: Event-based deduplicated Telegram notifications (`dispatch_telegram_event`), secret redaction (`redact_secrets`), and 9-component System Health Monitor (`get_system_health_report`).

## 2. Exchange Setup & API Security Instructions

1. **Never Hardcode Credentials**: Copy `.env.example` to `.env` and store API keys only in `.env` or environment variables. `.env` is excluded from archives and version control.
2. **Least-Privilege API Keys**:
   * **Disable Withdrawals & Internal Transfers** on all API keys (`enableWithdrawals=false`). The Live Readiness Gate explicitly blocks live execution if withdrawal permissions are detected on the API key.
   * **Enable IP Allowlisting** on the exchange API management page.
   * **Use a Dedicated Subaccount** with bounded trading capital so account-level risk is physically isolated.
3. **Separate Testnet vs. Production Credentials**:
   * Populate `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET` for `EXECUTION_MODE=TESTNET`.
   * Populate `BINANCE_API_KEY` and `BINANCE_API_SECRET` only when preparing for `EXECUTION_MODE=LIVE`.

## 3. Operating Instructions: PAPER, TESTNET, and LIVE Modes

### Mode 1: PAPER Simulation (Default)
```bash
cp .env.example .env
# EXECUTION_MODE=PAPER and LIVE_TRADING_ENABLED=false are set by default
streamlit run app.py
```
* Evaluates live/cached market data, generates momentum signals, validates risk, and simulates paper trades in SQLite (`paper_positions`) without submitting any orders to the exchange.

### Mode 2: TESTNET Sandbox Execution
```bash
export EXECUTION_MODE=TESTNET
export TESTNET_TRADING_ENABLED=true
export EXCHANGE_MARKET_TYPE=FUTURES_USDM
export BINANCE_TESTNET_API_KEY="your_testnet_key"
export BINANCE_TESTNET_API_SECRET="your_testnet_secret"
streamlit run app.py
```
* Synchronizes your Binance Testnet balance, equity, available margin, and open positions, sizes positions from actual Testnet equity, submits idempotent Testnet orders, verifies protective Stop-Loss and Take-Profit orders, and reconciles positions in `live_positions` / `live_orders`.

### Mode 3: LIVE Exchange Execution (Explicit Two-Step Authorization Required)
1. Validate strategy and execution behavior in `PAPER` and `TESTNET` modes first.
2. Configure environment variables:
   ```bash
   export EXECUTION_MODE=LIVE
   export LIVE_TRADING_ENABLED=true
   export EXCHANGE_MARKET_TYPE=FUTURES_USDM
   export BINANCE_API_KEY="your_production_key_without_withdrawals"
   export BINANCE_API_SECRET="your_production_secret"
   ```
3. Verify that all 12 technical checks in `evaluate_live_readiness_gate` pass (valid credentials, withdrawal permissions disabled, fresh/consistent account snapshot, persisted risk limits, instrument metadata loaded, zero reconciliation mismatches, and inactive circuit breakers / kill switch).
4. Grant explicit runtime user confirmation (`runtime_live_confirmed=True`). To stop immediately at any time, click **🛑 AKTIFKAN KILL SWITCH DARURAT** in the sidebar, which blocks all new entries, cancels unfilled entry orders, and preserves protective SL/TP orders on open positions.

## 4. Database Migrations & CLI Commands

Database migrations (`v1` through `v8`) run automatically and atomically via `run_migrations()` on startup.

```bash
# Check runtime & database status
python3 app.py --status --db-file trading_v5.db

# Reconcile open paper trades
python3 app.py --reconcile-only --db-file trading_v5.db

# Run a single headless monitor cycle
python3 app.py --monitor-once --db-file trading_v5.db

# Run the complete unit, integration, and regression test suite (27 tests)
python3 -m unittest discover -s tests -p "test_*.py" -v
```

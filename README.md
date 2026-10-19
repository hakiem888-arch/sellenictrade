# SecretAgent: Momentum Intelligence Engine

SecretAgent is an explainable, risk-aware **Momentum Trading Assistant** focused on multi-timeframe cryptocurrency momentum detection, deterministic Smart Money Concepts (SMC) & technical validation, AI Council interpretation, pre-trade Risk Guard enforcement, paper trading, and walk-forward performance evaluation.

> **Safety Notice:** SecretAgent operates strictly in **research and paper-trading mode**. It never submits live orders to any exchange.

## Core Workflow

`Market Data (Closed Candles) → Momentum Detection → Multi-Timeframe Validation → Signal Quality & Evidence → Deterministic Risk Guard → Paper Trading → Performance Lab & Walk-Forward Evaluation`

1. **Momentum Radar (Primary Interface)**:
   - Displays system & market-data health, active setups (`DEVELOPING` + `TRIGGERED`), triggered setups, open paper positions, and prioritized momentum opportunities.
   - Canonical Signal Lifecycle state machine (`DEVELOPING`, `TRIGGERED`, `WAIT`, `INVALIDATED`) with explicit transition rules, signal IDs, timestamps, expiry windows, and price/structure invalidation checks.
   - Orthogonal 5-pillar Momentum Evidence report separating deterministic technical validation from AI Council commentary without double-counting correlated indicators.
   - Validated theoretical Entry, Stop-Loss, Take-Profit (`TP1/TP2/TP3`), setup invalidation level, and gross/net Risk:Reward ratios distinguished from simulated paper execution fills.
2. **Deterministic Risk Guard**:
   - Position sizing from monetary risk budget (`Position Size = Monetary Risk Budget / Risk Per Unit`), adjusted for tick size, lot size, minimum order quantity, minimum notional, contract multiplier, fees, slippage, and spread.
   - Enforces mandatory limits on risk per trade, daily realized loss, total open risk, concurrent positions, correlated asset exposure, and consecutive losses.
   - Persists Risk Guard state across restarts in SQLite and never fabricates account equity when sizing inputs are missing.
3. **Performance Lab & Strategy Validation**:
   - Chronological candle-by-candle replay backtesting with zero look-ahead bias, causal higher-timeframe (`1h`, `4h`, `1d`) alignment, conservative intrabar TP/SL tie-breaking (`SL_FIRST_CONSERVATIVE`), gap-open fill clamping, trading fees, configurable slippage, and funding cost modeling.
   - 3-split Walk-Forward Validation (`Train` → `Validation` → `Out-of-Sample`) preventing boundary leakage.
   - Sample-size-aware performance metrics with breakdowns by `LONG`/`SHORT`, timeframe, strategy/mode, and market regime, plus empirical confidence calibration (`SCORE_INDICATOR_ONLY` vs `CALIBRATED_PROBABILITY`).
4. **Event-Based Telegram Notifications**:
   - Deduplicated notifications backed by SQLite (`telegram_outbox`) with bounded retry/backoff and secret redaction.

## Setup & Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` (or `.streamlit/secrets.toml`) to provide `GROQ_API_KEY` (and optionally `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`).

## Database Migrations

Database schema migrations (up to `v7`) run automatically and atomically on startup (`init_db()` / `run_migrations()`). To run migrations and verify database integrity manually:

```bash
python3 -c "import app; app.init_db(); print(app.verify_database_integrity(app.DB_FILE))"
```

## Running the Application

### 1. Interactive Streamlit Dashboard (Momentum Radar)

```bash
streamlit run app.py
```

### 2. Independent Headless CLI & Background Worker

```bash
# Check durable worker and runtime status:
python3 app.py --status --db-file trading_v5.db

# Run a single headless monitor + paper-trade reconciliation cycle:
python3 app.py --monitor-once --monitor-n 8 --max-ai 1 --db-file trading_v5.db

# Reconcile open/pending paper trades only:
python3 app.py --reconcile-only --db-file trading_v5.db

# Run continuous independent background worker loop:
python3 app.py --worker --interval 45 --monitor-n 8 --max-ai 1 --db-file trading_v5.db
```

## Running the Test Suite

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

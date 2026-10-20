# SecretAgent v5.5 vs v5.6: Telegram Alert Audit

## Scope and evidence

Compared the supplied `v 5.5.zip` and `v 5.6.zip`, including the Python modules embedded inside the v5.5 `app.py` bundle. The archives contain source code and requirements, but no production logs, historical candle dataset, SQLite database, or real `.env` credentials.

This is therefore a source-level root-cause audit plus regression testing. A quantitative same-market alert-count replay was not possible without historical market snapshots and equivalent runtime configuration.

## Findings: why v5.6 sends fewer standard Momentum Radar alerts

### 1. The momentum radar thresholds did not change

The `_pattern_score` implementation retains the same state thresholds in both versions:

- Below score 35: `QUIET` / `WAIT`.
- Score 45 or above: at least `DEVELOPING`.
- Score 58 or above plus a 5m/15m directional trigger: `TRIGGERED`.

Source references: v5.5 `app.py` around lines 4471-4474; v5.6 `app.py` around lines 4772-4775.

The `momentum_candidates`, `pattern_snapshot`, `pattern_fingerprint`, `save_pattern_event`, `process_monitor_candidate_workflow`, and `save_rendered_analysis` function bodies are unchanged between the supplied versions. The standard Momentum Radar candidate alert flow still sends Telegram directly after a candidate passes validation and is saved. The new `dispatch_telegram_event` outbox is used primarily by exchange execution and operational events, so it is not the primary source-level explanation for fewer standard Momentum Radar signal alerts.

### 2. The default for Proactive AI mode changed from enabled to disabled

- v5.5: `st.session_state.get("v6_proactive_ai_mode", True)` in `_run_post_ai_gate` and a UI checkbox default of `True`.
- v5.6: the same setting defaults to `False` in both places.

Source references: v5.5 `app.py` around lines 3482 and 5187; v5.6 `app.py` around lines 3709 and 5529.

This is a material behavior change. With Proactive AI off by default, v5.6 no longer synthesizes a directional decision from the supporting agents when the Final Judge returns `WAIT`. V5.6 also applies checks more consistently, including low judge confidence, analyst/risk opposition, risk-agent vetoes, late entries, retest waits, and timeframe conflicts. V5.5's proactive path bypassed several of these checks and rewrote timing to a ready state. The v5.6 behavior is more conservative; it naturally produces fewer approved entries.

### 3. Market preflight is stricter in v5.6

The v5.5 preflight primarily checked required timeframe presence, numeric validity, positive price/ATR/support/resistance, and spread. V5.6 adds checks for OHLCV validation errors, duplicate or out-of-order timestamps, incomplete candles, invalid quality states, stale market data, indicator-input problems, and cross-timeframe price consistency.

Source references: v5.5 embedded `domain.py` around line 248; v5.6 `src/ai_consensus/domain.py` around line 510, plus new validation helpers in `src/ai_consensus/market.py`.

These checks can prevent a candidate from reaching AI validation. They should not be relaxed without evidence that valid market snapshots are being rejected incorrectly.

### 4. Risk validation is more comprehensive in v5.6

V5.6 passes persisted equity and current portfolio state into the risk engine. It adds checks for current open notional and risk, daily realized loss, consecutive losses, concurrent positions, correlated positions/exposure, and maximum risk per trade. V5.5 used the configured initial paper equity and a smaller set of sizing constraints in this gate.

Source references: v5.5 `app.py` around lines 3565-3575; v5.6 `app.py` around lines 3785-3821; v5.6 `src/ai_consensus/risk.py` around line 1080.

These are intentional risk controls, not a demonstrated defect. They can reduce the number of alerts that represent actionable entries.

## Deduplication audit and fix applied

The regular Momentum Radar fingerprint includes symbol, mode, closed 15m bar timestamp, pattern state/direction, timing, structure events, and entry zone. That logic is the same in both supplied versions and is intended to suppress repeated alerts for an unchanged setup within the same bar.

V5.6 additionally introduced a persistent Telegram outbox for execution/operational events. Its original event key only included event type, symbol, signal/position ID, state, and direction. Consequently, multiple distinct partial-exit events tied to the same signal/position could share one key and the later event could be suppressed after the first was sent.

The audited copy fixes this separate execution-alert issue:

- Event identity now includes relevant order/fill IDs, executed quantity, prices, PnL, event timestamps, and other event-specific details.
- `lifecycle_state` is recognized when resolving the event state and when displaying status.
- Entry/exit notifications include exchange order IDs and fill details.
- Reconciliation mismatch alerts are emitted on transition into a mismatch episode rather than on every polling cycle. A later recurrence after recovery receives a new event timestamp.
- A regression test confirms that two distinct partial-exit orders for the same position both send, while replaying the exact same event is deduplicated.

This fix improves execution and operational alerts. It does not change momentum thresholds or intentionally loosen signal/risk gates.

## Validation performed

- `python -m py_compile app.py` on v5.5: passed.
- `python -m compileall -q app.py src tests` on the audited v5.6 source: passed.
- `python -m unittest discover -s tests -v` on the audited v5.6 source: **28 tests passed**.
- CLI status smoke test with a temporary database: passed; monitor reported `MANUAL_ONLY` and no worker was started.
- Live trading was not enabled and no real orders were placed.

The test suite uses mocks/synthetic data for automated validation. It does not verify real exchange credentials, real Groq responses, real Telegram delivery, or profitability in live markets.

## Remaining uncertainty

The exact number of alerts missed or suppressed in real operation cannot be calculated from these ZIPs alone. Neither archive includes historical market data, production logs, Telegram delivery records, or the running database. To quantify the alert difference, replay both versions against the same candle snapshots with identical settings and capture the funnel: candidates -> preflight pass -> AI result -> gate approval/rejection reason -> saved signal -> Telegram send result.

## Conclusion

The strongest source-level explanation is the combination of Proactive AI defaulting to `False`, stricter market-data preflight, and expanded persisted Risk Guard checks in v5.6. Momentum score thresholds and the main candidate fingerprint/workflow functions are unchanged. No strategy thresholds were changed during this audit. Keep live trading disabled until an equivalent-data replay and paper/testnet validation are complete.

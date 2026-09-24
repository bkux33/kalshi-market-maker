# CLAUDE.md — Kalshi Alpha Lab

Research-first system for testing whether Kalshi strategies have a repeatable **net** edge after
fees, slippage, fill risk and adverse selection. Read README.md, ARCHITECTURE.md and BACKTESTING.md
before changing trading or simulation code.

## Commands
```bash
pip install -e ".[dev,llm]"
pytest -q                         # must pass before every commit
alphalab --help                   # CLI (see DEPLOYMENT.md)
```
`legacy/` is the untouched upstream kalshi-market-maker (Apache-2.0). Do not modify it; its tests
run from inside `legacy/`.

## Invariants — do not break
* Prices are integer units of $0.0001 (`core/prices.py`); YES ask = 1 − best NO bid.
* Backtest, paper and live share `TradingEngine`, `RiskEngine` and (for backtest/paper) `SimBroker`.
  Never give paper or backtest a more optimistic fill path than the other.
* Replay must stay deterministic (`result_hash`); no wall clock or unseeded randomness in `sim/`.
* Strategies act only through `ctx`; no look-ahead (tested).
* Headline results are **net** of fees; unknown series pay maker fees (conservative).
* Experiments cannot assign LIVE-CANDIDATE; only `paper-evaluate` can. Live mode keeps all gates
  in `execution/live.py`, including the hard caps.
* Secrets only from environment variables; never log or commit keys; the LLM assistant stays
  read-only and must not import `alphalab.execution` or `alphalab.kalshi`.
* Long-running services write JSONL; only short jobs write DuckDB.
* Exchange connectivity follows the official Kalshi spec (see DEMO_CONNECTIVITY_REPORT.md): V2 orders on
  `/portfolio/events/orders` (`side` bid/ask, dollar-string `price`, string `count`), `orderbook_fp`,
  `ts_ms`. Never reintroduce the legacy `/portfolio/orders` create/cancel shape.
* `KALSHI_ENV=demo` must never be pointed at a production host (config refuses); `demo-orders` and
  `demo-check` are demo-only and must never call or weaken `assert_live_allowed`.
* Every timestamp column is UTC epoch nanoseconds; convert with integer arithmetic.

## Wording
Never describe a strategy as profitable because a backtest is positive. Always label results as
in-sample, walk-forward out-of-sample, holdout, paper or live.

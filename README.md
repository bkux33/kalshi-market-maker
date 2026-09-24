# Kalshi Alpha Lab

A research-first system for one question:

> **Is there a repeatable, positive-net-expectancy edge in Kalshi markets after fees, slippage,
> fill risk and adverse selection?**

> ⚠️ **Profitability is not guaranteed — and nothing in this repository shows that any strategy is
> profitable.** Backtests are simulations under explicit assumptions. Every strategy starts as a
> hypothesis and must earn its status from out-of-sample data, then from paper trading. Live trading
> is disabled by default and refused unless every safety gate passes. Use at your own risk; this is
> not financial advice. Kalshi is a trademark of KalshiEX LLC; this project is not affiliated with it.

## What is in the box

| Layer | What it does |
|---|---|
| Market data | Authenticated Kalshi WebSocket client (order-book snapshots/deltas, trades, ticker, lifecycle), sequence-gap detection + resubscribe, reconnect with backoff; raw frames recorded verbatim to an append-only tape |
| Storage | DuckDB (markets, book events, trades, top of book, settlements, external prices, runs, orders, fills, P&L, signals, experiments, risk events); idempotent ingest |
| Order book | Integer fixed-point L2 book, YES/NO unified view, depth, imbalance, microprice, VWAP sweep |
| Features | One streaming feature engine used by both research and trading (spread, depth, imbalance, microprice, returns, volatility, trade intensity/imbalance, acceleration, time to settlement, market age, distance to strike, spot-implied fair value) |
| Simulator | Deterministic replay with latency, queue position, partial fills, cancel latency, impact overlay, settlement; P&L split into mid P&L / execution cost / fees / net |
| Strategies | Order-book imbalance, inventory-aware market maker, logical arbitrage, crypto 15-minute fair value, momentum / mean reversion / volatility baselines, random-direction null benchmark |
| Research | Predictive-feature study (IC vs cost hurdle), incremental-information test, arbitrage scan, experiment engine (bounded grid, walk-forward, stress, sensitivity, random benchmark, deflated Sharpe, locked holdout), objective classification, scorecards and reports, automated research loop |
| Execution | Risk engine (size, position, exposure, rate, duplicates, stale data, disconnect, settlement, daily loss, drawdown), latched kill switch, paper trading on live or recorded data, guarded live broker |
| Interfaces | CLI (`alphalab …`), dashboard (11 pages), optional read-only LLM research assistant |

Status ladder: **REJECT → RESEARCH → PAPER → LIVE-CANDIDATE**. Experiments can reach PAPER at most;
only paper-trading evidence can reach LIVE-CANDIDATE; live trading requires LIVE-CANDIDATE.

## Quick start
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm]"
pytest -q                                         # 89 tests, no network needed

# No credentials needed: validate the pipeline on synthetic data
alphalab synthetic --markets 20
alphalab experiment --strategy imbalance --grid '{"threshold":[0.6,0.7],"horizon_s":[5,10]}'
alphalab dashboard                                 # http://127.0.0.1:8080

# Real data from the Kalshi DEMO exchange (API key required: Kalshi authenticates the WebSocket)
cp .env.example .env                               # set KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (keep KALSHI_ENV=demo)
alphalab markets --active --limit 30               # what is listed right now (nothing hard-coded)
alphalab demo-check --n 3 --seconds 120            # auth + WS + book rebuild + REST cross-check, no orders
alphalab record --search BTC --max-markets 10 --duration 3600
alphalab data-quality && alphalab ingest && alphalab research-loop && alphalab scorecard
```
See [DEPLOYMENT.md](DEPLOYMENT.md) for paper trading, Docker and production.

## Environment variables
| Variable | Required | Purpose |
|---|---|---|
| `TRADING_MODE` | no (default `paper`) | `paper` or `live` |
| `LIVE_TRADING_ACK` | only for live | must equal `I_ACCEPT_REAL_MONEY_RISK` |
| `KALSHI_ENV` | no (default `demo`) | `demo` or `prod`; with `demo`, non-demo URLs are refused |
| `KALSHI_HOST_PROFILE` | no (default `external`) | `external` (recommended hosts) or `legacy` (shared hosts) |
| `KALSHI_REST_URL` / `KALSHI_WS_URL` | no | explicit endpoint overrides |
| `KALSHI_API_KEY_ID` | for recording/paper-on-live/live | Kalshi API key id |
| `KALSHI_PRIVATE_KEY_PATH` | same | path to the RSA private key PEM |
| `DATA_DIR` | no (default `./data`) | tape, journals, DuckDB, state, kill switch |
| `ALPHALAB_CONFIG` | no | YAML config (see `config/alphalab.example.yaml`) |
| `KILL_SWITCH_FILE` | no | override kill-switch path |
| `DASHBOARD_TOKEN` | recommended if exposed | bearer token for the dashboard API |
| `ANTHROPIC_API_KEY` + `ALPHALAB_LLM=1` | no | enable the LLM research assistant |

## Exchange connectivity status
See [DEMO_CONNECTIVITY_REPORT.md](DEMO_CONNECTIVITY_REPORT.md) and [DATA_QUALITY_REPORT.md](DATA_QUALITY_REPORT.md).
The API layer was audited against the official Kalshi SDK 3.30.0 (2026-09-15) and fixed (V2 orders,
recommended hosts, `ts_ms`, duplicate/gap/reconnect handling). It is proven end to end against a local mock
of the current API; **it has not yet been run against the real DEMO exchange** because the build
environment could not reach Kalshi.

## Current research status
See [RESEARCH_RESULTS.md](RESEARCH_RESULTS.md). Summary: on the only real Kalshi data available while
building this (two small third-party samples, < 2 hours in total), **no strategy survived**; the few
positive out-of-sample numbers are on tiny samples and are classified RESEARCH, not tradable.
Recording weeks of your own data is the necessary next step.

## Documentation
[AUDIT.md](AUDIT.md) (repositories audited, base selection) ·
[ARCHITECTURE.md](ARCHITECTURE.md) · [STRATEGIES.md](STRATEGIES.md) ·
[BACKTESTING.md](BACKTESTING.md) · [RISK.md](RISK.md) · [DEPLOYMENT.md](DEPLOYMENT.md) ·
[SECURITY.md](SECURITY.md) · [DATA.md](DATA.md) · [RESEARCH_RESULTS.md](RESEARCH_RESULTS.md)

## Provenance and licence
Built on [zachdaube/kalshi-market-maker](https://github.com/zachdaube/kalshi-market-maker)
(Apache-2.0). The original code is preserved in `legacy/`; see [NOTICE](NOTICE). Licensed under
Apache-2.0 (see [LICENSE](LICENSE)).

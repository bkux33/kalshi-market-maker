# CLAUDE_HANDOFF.md — Kalshi Alpha Lab: complete project state

Last updated: **2026-09-24**. This file is the starting point for a new session. It does not rely
on any earlier conversation. Read it, then `CLAUDE.md` (the invariants), then the docs it points to.

> **No credentials are in this file or in this repository, and none must ever be added.**
> **No strategy has been shown to be profitable.** **Live trading is disabled.**

---

## 1. Project objective

Kalshi Alpha Lab is a **research-first** system built to answer one question:

> Is there a repeatable, positive **net** expectancy edge in Kalshi markets after fees, slippage,
> fill risk and adverse selection?

Profitability is **not assumed**. Every strategy is a hypothesis. It moves up a status ladder
(**REJECT → RESEARCH → PAPER → LIVE-CANDIDATE**) only on evidence:

| Mode | What it is | Money | Evidence value |
|---|---|---|---|
| **Research / backtest** | Deterministic replay of recorded data through the simulated broker (latency, queue position, partial fills, fees). Includes experiments with walk-forward out-of-sample validation. | none | Can reach **PAPER** at most. In-sample results are never evidence. |
| **Paper trading** | The same engine, risk engine and fill model, driven by live (or replayed) market data. Fills are simulated. | none | Only `alphalab paper-evaluate` on paper evidence can award **LIVE-CANDIDATE**. |
| **Demo execution** | Real orders on Kalshi's **DEMO** exchange (fake money) to test the order path: create, ack, cancel, duplicates, reconnect, kill switch, fills. | fake | Tests plumbing only. Says nothing about edge. |
| **Live trading** | Real orders on production. | **real** | **Disabled.** Refused unless every gate in `execution/live.py` passes (§8). |

## 2. Repository

| Item | Value |
|---|---|
| Original upstream | [zachdaube/kalshi-market-maker](https://github.com/zachdaube/kalshi-market-maker) (Apache-2.0). Its code is preserved untouched in `legacy/` (see `NOTICE`). |
| Our repository | [bkux33/kalshi-market-maker](https://github.com/bkux33/kalshi-market-maker) |
| Working branch | `claude/kalshi-quant-trading-system-2pafec` (no PR opened) |
| Base commit | `4f64bea` ("Pin authoritative Kalshi DEMO endpoints; add credential-free net-check"). This handoff file is committed on top of it. |
| Other repos audited | reedjacobp/kalshi-trading-bot, kapelame/kalshi-crypto-bot, rnop/…-Public-Baseline (forks in `bkux33`) and `bkux33/kmmx`. See `AUDIT.md`. No code was copied from them. Their sample CSVs were imported locally only and are not redistributed. |
| Package | `alphalab/` (Python 3.11). CLI entry point `alphalab`. `pip install -e ".[dev,llm]"`. |

## 3. Current architecture

Full diagram: `ARCHITECTURE.md`. Data flow:

```
Kalshi WS ─► alphalab.kalshi.ws ─► data/raw/YYYYMMDD/HH/*.jsonl (append-only tape)
                 │                        │ alphalab ingest
                 ▼                        ▼
          BookManager/OrderBook     DuckDB data/alphalab.duckdb ─► ReplaySource
                 │                                                   │
                 └──────────► FeatureEngine ◄────────────────────────┘
                                    ▼
                         Strategy (sees only ctx) ─► RiskEngine ─► SimBroker (backtest/paper)
                                                                  └► LiveBroker (live; gated)
                                    ▼
             TradingEngine ─► metrics ─► experiments ─► validation ─► REJECT/RESEARCH/PAPER
```

| Component | Module(s) | Notes |
|---|---|---|
| **Data ingestion** | `data/recorder.py`, `data/tape.py`, `data/ingest.py`, `data/importers.py`, `data/synthetic.py` | Raw frames are written to the tape before parsing. Hourly rotation; `recover_open_files` handles crash recovery. Ingest is idempotent, one transaction per closed file. Includes CSV importers for the third-party samples and a synthetic data generator. |
| **WebSocket** | `kalshi/ws.py`, `kalshi/messages.py` | RSA-PSS auth headers on the upgrade request (signs `GET /trade-api/ws/v2`). Channels: `orderbook_delta`, `trade`, `ticker`, `market_lifecycle_v2` (plus `fill`/`user_orders` when live). Handles gap → resubscribe, reconnect with backoff, duplicate-frame and `trade_id` de-dup, and drops frames for closed sids. Prefers `ts_ms`. |
| **REST** | `kalshi/rest.py`, `kalshi/auth.py`, `kalshi/discovery.py`, `kalshi/netcheck.py` | Throttled (8 req/s default), retries, raises `KalshiAPIError`. Parses `orderbook_fp`. **V2 orders**: `create_order_v2`, `cancel_order_v2`, `batch_cancel_v2`, `cancel_all_orders_v2`. Market discovery uses `GET /series` for categories. `netcheck` is a credential-free reachability check. |
| **Order-book reconstruction** | `core/orderbook.py`, `core/book_manager.py` | Integer L2 book. NO bids become YES asks (YES ask = 1 − best NO bid). Sequence rules: seq ≤ last is a duplicate (ignored); seq > last+1 is a gap, which marks every market on that sid unsynced until a snapshot; a snapshot restarts the stream; `reset_all` on disconnect. A closed market's empty snapshot is a valid empty book. |
| **DuckDB** | `data/db.py` | Tables: markets, book_events, tob, trades, settlements (`inferred` flag), external_prices, runs, orders, fills, round_trips, pnl, signals, experiments, experiment_results, strategy_status, predictive_results, risk_events. Single writer: long-running services write JSONL, short jobs write the DB. |
| **Feature engine** | `research/features.py` | One streaming engine shared by research and trading. Features: spread, depth, imbalance, microprice, returns, volatility, trade intensity/imbalance, time to settlement, distance to strike, spot-implied fair value. |
| **Strategies** | `strategies/` | See §9. |
| **Backtester** | `sim/replay.py`, `sim/broker.py`, `sim/engine.py`, `sim/portfolio.py`, `sim/metrics.py`, `sim/backtest.py` | Deterministic (`result_hash`). Fill model: order and cancel latency, queue position (`maker_fill_mode: queue`), partial fills, impact overlay, cross fills, settlement. P&L is split into mid / exec cost / fees / **net**. |
| **Walk-forward validation** | `research/experiments.py`, `research/validation.py`, `research/analysis.py` | Bounded grid, walk-forward OOS folds, locked holdout. Stress tests (2× fees, +1 tick, +250 ms latency, conservative queue) and a random-direction benchmark, both run **on the walk-forward path**. Also sensitivity analysis and deflated Sharpe (PSR). Classification thresholds are in the `validation:` config. |
| **Paper execution** | `execution/session.py`, `execution/journal.py` | The same `TradingEngine` + `RiskEngine` + `SimBroker`, on live data or a replay clock. Writes `data/journal/<run>.jsonl` and `data/state/paper_state.json`. |
| **Demo execution** | `execution/demo_orders.py`, `kalshi/healthcheck.py` | `demo-orders` is demo-only (`assert_demo`). Steps: create+ack, local duplicate block, exchange duplicate `client_order_id`, reconnect with resting order, cancel+ack, stale-price block, kill switch, optional fill/partial fill. `demo-check` covers auth, WS, book rebuild and a REST cross-check, with no orders. |
| **Risk engine** | `execution/risk.py`, `execution/killswitch.py`, `execution/live.py` | Pre-trade checks: kill switch, disconnect, stale data, settlement buffer, price sanity, size, duplicates, rate, open orders, position, exposure. Daily-loss and drawdown halts trip a latched kill-switch file. Live gates and hard caps are described in `RISK.md`. |
| **Dashboard** | `dashboard/app.py`, `dashboard/static/index.html` | FastAPI + a single-page UI (11 pages), bound to 127.0.0.1:8080. Optional `DASHBOARD_TOKEN`. Read-only except the kill button. |
| **LLM assistant** | `research/assistant.py` | Read-only tools; must not import `alphalab.execution` or `alphalab.kalshi` (tested). Off unless `ANTHROPIC_API_KEY` and `ALPHALAB_LLM=1` are set. |
| **CLI** | `cli.py` | `discover, markets, net-check, demo-check, demo-orders, record, ingest, import-csv, synthetic, backtest, experiment, features, research-loop, scorecard, report, paper, live, paper-evaluate, kill, dashboard, ask, db-check, data-quality, config` |
| **Config** | `core/config.py`, `config/alphalab.example.yaml`, `.env.example` | YAML plus environment variables. Secrets are rejected in YAML. `KALSHI_ENV=demo` refuses any non-demo URL. |

## 4. Current implementation status

**Complete (implemented and tested locally / against the mock):**
- Everything in §3, including the API audit against the official SDK `kalshi-python-sync` 3.30.0 (2026-09-15).
- End-to-end tests against `tests/mock_kalshi.py`, a local mock exchange. It checks every RSA-PSS signature, validates the V2 schemas and injects faults: duplicates, a gap, a malformed frame, a disconnect.
- Recorder → ingest → deterministic replay, including crash recovery of the tape.
- Research loop, scorecards and reports, run on the two real third-party samples and on synthetic data.
- `net-check`, `demo-check`, `demo-orders` and `data-quality` tooling.
- Docs: README, AUDIT, ARCHITECTURE, STRATEGIES, BACKTESTING, RISK, DEPLOYMENT, SECURITY, DATA, RESEARCH_RESULTS, DEMO_CONNECTIVITY_REPORT, DATA_QUALITY_REPORT.

**Partially complete:**
- **Logical arbitrage**: strategy and offline scan exist, but no multi-market event data has been available to exercise them.
- **Maker strategies (market maker, imbalance)**: the queue fill model needs trade prints. Neither real sample has a trade feed, so maker results are not meaningful yet.
- **Crypto fair value**: uses Coinbase spot, not Kalshi's settlement benchmark average. Drift, basis and funding are not modelled.
- **WS `get_snapshot`** (added to Kalshi 2026-04-20) is not used. Resync is unsubscribe + resubscribe instead.

**Untested against the real exchange (only against the mock):**
- REST auth, WS auth/subscribe, snapshot/delta parsing, book reconstruction and REST/WS top-of-book consistency.
- Market discovery on the live listing.
- The V2 order path (`demo-orders`), `LiveBroker` fills/reconcile, and the `fill`/`user_orders` payload fields.
- Paper trading on live data (never run).

**Blocked:**
- **All Kalshi network access from the cloud sessions used so far.** The egress proxy refuses `CONNECT` with 403 (§6).
- **Credentials are not configured** in the session environment (§7).
- Consequently: no real DEMO data has been recorded, no DEMO order has been sent, and no paper evidence exists.

## 5. Test status

- **93 tests, all passing** (`pytest -q`, no network needed). Last run: 2026-09-24, on this commit.
- Per file:

| File | Tests | Covers |
|---|---|---|
| `test_core.py` | 12 | Prices, NO→YES conversion, deltas, sweep, sequence gaps, fees and rounding, RSA-PSS signing, secret redaction, secrets only from env. |
| `test_kalshi_api.py` | 5 | Order-book parsing, REST pagination/retry/errors, discovery, WS frames, WS gap/resubscribe/reconnect. |
| `test_api_current.py` | 17 functions + parametrized cases | Current API formats. Duplicates vs gaps, reconnect seq restart, closed-market empty snapshot, `ts_ms`, exact timestamp conversion, `orderbook_fp`, V2 paths, host defaults, the prod guard, removed/deprecated market fields, combo maker fees, the **exact authoritative DEMO endpoints** in defaults, env and templates, and `net-check` classification (proxy block vs server rejection vs closed port). |
| `test_simulation.py` | 15 | Fill model, determinism, no look-ahead, P&L decomposition. |
| `test_research.py` | 10 | Experiments, walk-forward, stress and random benchmark on the WF path, `rerun_oos` reproducing OOS exactly, classification, feature study. |
| `test_risk_live.py` | 10 | Every risk limit, daily-loss/drawdown halts, kill switch, disconnect. **Live refused by default and for each missing gate.** Demo integration only on demo; V2 order body; `LiveBroker` fill de-dup; CLI live refusal. |
| `test_services.py` | 8 | Tape/ingest idempotency, official vs inferred settlement, CSV importers, paper replay session, kill switch mid-session, stale/disconnect cancels, dashboard token, assistant read-only. |
| `test_demo_e2e.py` | 7 | Against the mock: discovery, health check (rebuilt books equal REST), fault injection, record→ingest→replay determinism + restart recovery, demo order path (incl. partial fill 2 of 3), demo-orders refusing non-demo hosts, data-quality fault counts. |

- `legacy/` has its own upstream tests (run from inside `legacy/`). They are not part of the suite.
- Environment note: the container's system `cryptography` package was broken (pyo3 panic). Use a venv (`python3.11 -m venv .venv`).

## 6. Kalshi API status

**Authoritative DEMO endpoints (designated by the project owner; defaults in code and all templates):**
- REST: `https://external-api.demo.kalshi.co/trade-api/v2`
- WebSocket: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`

Production equivalents (`external-api.kalshi.com`, `external-api-ws.kalshi.com`) exist in `core/config.py` but are unused.

- The legacy shared hosts (`demo-api.kalshi.co`, `api.elections.kalshi.com`) are selectable only with `KALSHI_HOST_PROFILE=legacy`. Use them only for a specific compatibility problem.
- With `KALSHI_ENV=demo`, config refuses to start if either URL is not a demo host.

**Connectivity has never been achieved.** Every cloud session so far was blocked by the environment's egress proxy (`CONNECT tunnel failed, response 403`) for every Kalshi host, including docs.kalshi.com. Last `alphalab net-check` (2026-09-24, run once):

```
REST external-api.demo.kalshi.co     DNS: resolves -> BLOCKED_BY_PROXY (403 Forbidden)
WS   external-api-ws.demo.kalshi.co  DNS: resolves -> BLOCKED_BY_PROXY (proxy rejected connection: HTTP 403)
NETWORK: BLOCKED: external-api.demo.kalshi.co, external-api-ws.demo.kalshi.co
```

**This must be re-verified after the network permissions are changed.** The fix is in the cloud environment settings: session title bar → environment → Edit → Network access, then allow both hosts. Start a new session afterwards. If `net-check` is still blocked, **stop and report the blocked host; do not retry in a loop.**

**API facts implemented** (source: official SDK 3.30.0 plus docs snippets; see `DEMO_CONNECTIVITY_REPORT.md`):
- **Auth**: RSA-PSS SHA-256 (salt = digest length) over `{timestamp_ms}{METHOD}{path without query}`. Headers `KALSHI-ACCESS-KEY`, `-TIMESTAMP`, `-SIGNATURE`.
- **REST book**: `orderbook_fp{yes_dollars,no_dollars}` as `[price, count]` strings.
- **WS book**: `orderbook_snapshot` (`yes_dollars_fp`/`no_dollars_fp`), `orderbook_delta` (`price_dollars`, `delta_fp`, `side`, `ts_ms`), per-sid `seq`. `trade` messages carry `trade_id` and `ts_ms`.
- **V2 orders**:
  - Create: `POST /portfolio/events/orders` with `side` bid/ask on the YES book, dollar-string `price`, string `count`, and required `time_in_force` and `self_trade_prevention_type`.
  - Cancel: `DELETE /portfolio/events/orders/{id}?market_ticker=…`.
  - Batch: `/portfolio/events/orders/batched`.
  - The legacy `/portfolio/orders` create/cancel is deprecated and **must not be reintroduced**.
- **Fee types**: quadratic, quadratic_with_maker_fees, quadratic_with_combo_maker_fees, flat. Unknown series are charged maker fees (conservative).
- **Market fields**: `volume_24h` has been removed and `liquidity_dollars` is deprecated. Categories come from `GET /series`.

**Still unverified on the real exchange:**
- The WS host itself.
- `fill`/`user_orders` fields in the V2 era.
- Shard (`exchange_index`) behaviour. We omit it and rely on auto-routing.
- `get_snapshot` syntax.
- The account's rate-limit tier.

## 7. Credential configuration (variable names only)

| Variable | Purpose |
|---|---|
| `KALSHI_API_KEY_ID` | Kalshi API key id (DEMO key) |
| `KALSHI_PRIVATE_KEY_PATH` | Filesystem path to the RSA private key PEM (`chmod 600`, outside the repo) |
| `KALSHI_ENV` | `demo` (keep it) |
| `TRADING_MODE` | `paper` (keep it) |

Optional: `KALSHI_REST_URL`, `KALSHI_WS_URL`, `KALSHI_HOST_PROFILE`, `DATA_DIR`, `ALPHALAB_CONFIG`, `KILL_SWITCH_FILE`, `DASHBOARD_TOKEN`.

- Set credentials in the cloud environment's settings (environment variables / setup script that writes the key file from a stored secret), **never in chat, source, YAML or commits**.
- Earlier in the project the owner pasted a key id and a private key into chat. They were never used or stored, and the owner was told to **revoke them**. Use a newly generated DEMO key.
- Neither `KALSHI_API_KEY_ID` nor `KALSHI_PRIVATE_KEY_PATH` was set in any session so far.
- The public REST market listing needs no key. The WebSocket and anything under `/portfolio` do.

## 8. Current safety state

- `KALSHI_ENV=demo`, `TRADING_MODE=paper`: the defaults in code, `.env.example`, `config/alphalab.example.yaml` and `docker-compose.yml` (compose never enables live).
- **Production/live trading is disabled.** `assert_live_allowed` (`execution/live.py`) refuses unless **all** of the following hold:
  1. `TRADING_MODE=live`;
  2. `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`;
  3. credentials are present;
  4. the kill switch is not tripped;
  5. limits are within the hard caps in code (`max_order_size ≤ 100`, `max_total_exposure_usd ≤ 2000`, `max_daily_loss_usd ≤ 500`);
  6. every strategy's exact parameter set is **LIVE-CANDIDATE**, which only `paper-evaluate` can award (≥ 200 fills, ≥ 10 days, expectancy consistent with the backtest).
- `--demo-integration` is the only exception, and it works only against demo hosts.
- `demo-orders` / `demo-check` are demo-only and never call or weaken `assert_live_allowed`.
- **Live trading must not be enabled, and the gates must not be modified or bypassed**, without explicit validation evidence and an explicit owner decision.
- No strategy is anywhere near LIVE-CANDIDATE.

## 9. Strategies and validation status

Details: `STRATEGIES.md`. All are hypotheses. Status is from walk-forward OOS on tiny real samples (§10).

| Strategy (key) | Idea | Current status |
|---|---|---|
| `imbalance` | Top-of-book / top-5 depth imbalance predicts the next mid move. Taker entry, exit after a horizon. | **REJECT** on ETH sample (−$3.66 OOS net). Not testable on the crypto sample (depth not recorded). |
| `market_maker` | Avellaneda–Stoikov adapted to binaries. Inventory skew, fee-floored half-spread, post-only, cancel/replace. | **REJECT** on ETH sample (−$3.54, 7 trades). Needs trade prints for a meaningful queue model. |
| `logical_arb` | Mutually exclusive sums, strike-ladder monotonicity. Non-atomic legs with unwind cost. | **Untested** (no multi-market event data). |
| `crypto15m_fair_value` | Spot-implied Φ(ln(S/K)/(σ√τ)) vs contract price, trade on edge ≥ threshold, hold to settlement. | **RESEARCH** (+$0.92 OOS on 16 trades; negative at 2× fees). |
| `momentum` | Follow the `lookback_s` mid move. | **RESEARCH** on ETH (+$4.29 OOS, 22 trades). **REJECT** on the crypto sample. |
| `mean_reversion` | Fade the `lookback_s` mid move. | **REJECT** on both samples. |
| `vol_breakout` / `vol_fade` | Short/long realised-variation bursts: follow / fade. | **REJECT** on both samples. |
| `random_entry` | Benchmark: same entry times, coin-flip direction, same exits and costs. | Benchmark only. |

Nothing is PAPER or LIVE-CANDIDATE.

## 10. Research findings (actual numbers; all simulated)

**Data used:** the only real Kalshi data so far is two third-party samples (< 2 h total, no trade feeds, inferred settlements). Full detail: `RESEARCH_RESULTS.md`, `docs/research_report_2026-09-24_real_samples.md`.
- **ETH full-depth sample**: 2 × KXETH15M, 29 min.
- **Crypto sample**: 25 × BTC/ETH/SOL/XRP 15M, 82 min, top of book only with *assumed* sizes. Quotes change only about every 33 s. The 25 markets are ~6 effective independent episodes.

**Walk-forward out-of-sample** (200 random-benchmark trials each; net = gross − fees):

| Strategy @ data | Status | OOS trades | Net | p vs random | Net @ 2× fees |
|---|---|---|---|---|---|
| momentum @ ETH | RESEARCH | 22 | +$4.29 | 0.010 | +$2.19 |
| crypto15m fair value @ crypto | RESEARCH | 16 | +$0.92 | 0.005 | −$0.51 |
| imbalance @ ETH | REJECT | 26 | −$3.66 | 0.34 | −$5.98 |
| market maker @ ETH | REJECT | 7 | −$3.54 | n/a | −$3.83 |
| mean reversion @ ETH | REJECT | 23 | −$6.27 | 0.995 | −$8.40 |
| vol breakout @ ETH | REJECT | 14 | −$0.80 | 0.005 | −$2.29 |
| vol fade @ ETH | REJECT | 9 | −$3.20 | 0.955 | −$4.15 |
| momentum @ crypto | REJECT | 154 | −$30.18 | 0.48 | −$52.16 |
| mean reversion @ crypto | REJECT | 154 | −$39.86 | 0.98 | −$61.87 |
| vol breakout @ crypto | REJECT | 101 | −$19.36 | 0.22 | −$33.67 |
| vol fade @ crypto | REJECT | 110 | −$25.11 | 0.985 | −$41.07 |

**The two RESEARCH results are fragile:**
- **Momentum @ ETH**: all positive P&L comes from one market on one day, only 50 % of folds were profitable, and the deflated PSR is 0.05. As **paper replay** over the whole sample (same params, paper limits) it produced **−$1.98 on 32 trades**.
- **Crypto fair value**: in-sample expectancy was $0.57 vs **OOS $0.06**, it turns negative at 2× fees, and neighbouring parameters are unprofitable.
- They are reasons to collect more data on these hypotheses, not to trade.

**In-sample default-parameter replays** (not validation): imbalance −$9.60 (52 trades), market maker −$1.29 (5), momentum −$6.89 (42), mean reversion −$15.14 (64) on ETH. On the crypto sample, momentum and mean reversion were −$59.87 each (spread-only, because the mid never moved during 10 s holds) and crypto fair value +$8.27 (24).

**Predictive-feature study:** no feature survived. On ETH, `micro_minus_mid` (IC 0.40, t = 5.3) and `imbalance_1` (IC 0.29) predict 1–5 s moves. Those moves (≈ 0.5–1¢) are far below the ≈ 2.8¢ round-trip cost: real signal, but not tradable as a taker. Crypto-sample relationships did not replicate.

**Crypto incremental-information test** (OOS, 10 markets): price-only log-loss 0.700 vs price + `strike_z` 0.734 (t = −1.9). No incremental information from spot.

**Paper:** the engine is verified in replay mode only. **No paper trading on live Kalshi data has been run.**

**Live:** **none. No live trading has ever been run.**

**Synthetic sanity checks** (not Kalshi evidence):
- With no edge planted, the result is REJECT.
- A predictive edge smaller than costs is REJECT (`dies_after_costs`).
- An edge larger than costs is held at RESEARCH (synthetic data is flagged).

## 11. Known bugs / limitations

**API**
- Nothing verified against the real exchange (§6). The first real run may reveal schema differences in WS messages, V2 order responses or `fill`/`user_orders` fields.
- `get_snapshot` is unused (resync costs a round trip). Shard routing is unverified. The rate-limit tier is unknown (the client throttles to 8 req/s).

**Data**
- No self-recorded data exists. The samples are tiny, have no trade feed, use inferred settlements, and the crypto sample has assumed depth and stale quotes.
- `DATA_QUALITY_REPORT.md` part 1 (real DEMO collection) is empty until recording runs.

**Modelling**
- Queue-position fills for makers are only meaningful with trade prints.
- The impact overlay and cancel-queue model (`pro_rata`) are assumptions. `back` is the conservative alternative and is used in stress tests.
- Crypto fair value uses Coinbase spot rather than Kalshi's benchmark average. Drift, basis, funding and liquidations are not modelled.
- Arbitrage legs are not atomic. With few OOS trades the random benchmark is flagged `benchmark_underpowered`.
- Results on < 2 h of data cannot support any profitability conclusion.

**Execution**
- `LiveBroker`, `demo-orders` and reconciliation are tested only against the mock.
- Paper-on-live has never run, so no strategy can be promoted.

**Infrastructure**
- Cloud sessions: the egress proxy blocks Kalshi, and credentials are not set.
- The container's system `cryptography` package is broken (use a venv).
- DuckDB is single-writer: don't run `ingest` while another job holds the DB.
- A busy recorder can produce GBs of tape per week.
- The dashboard is unauthenticated unless `DASHBOARD_TOKEN` is set (it binds to localhost).
- `legacy/` keeps the upstream's known bugs by design. It is not used by `alphalab`.

## 12. Recent fixes (important)

- **Timestamp precision.**
  - pandas 3 parsed CSV timestamps at µs, which were stored as if they were ns (1000× off). Fixed with `.dt.as_unit("ns")` in `data/importers.py`, with a regression test.
  - WS timestamps now prefer `ts_ms` with exact integer ns conversion (`_ts_to_ns`, `iso_to_ns`, `exch_ts_ns`); the old float path lost precision.
- **Order-book parsing / API changes.**
  - REST book parsed from `orderbook_fp` dollar strings (the upstream bug produced empty books), with regression tests.
  - WS `*_dollars_fp` levels.
  - Closed-market empty snapshots are handled.
  - Orders migrated to V2 `/portfolio/events/orders` (bid/ask, dollar price, string count). Cancel/batch migrated.
  - Hosts switched to `external-api(-ws).demo.kalshi.co` (now pinned by tests).
  - Discovery no longer depends on the removed `volume_24h`. Combo maker fee type added.
- **Sequence handling.**
  - Duplicates (seq ≤ last) are now ignored instead of being treated as gaps.
  - A gap unsyncs every market on the sid until a fresh snapshot.
  - Seq restarts after reconnect/resubscribe.
  - Trades are de-duplicated by `trade_id`, and frames for unsubscribed sids are dropped.
- **Stress-test methodology.**
  - Stress tests and the random benchmark were partly in-sample. They now run on the walk-forward path (each fold's own params and block), and `rerun_oos` reproduces OOS exactly.
  - Added the `benchmark_underpowered` flag.
  - The research loop now runs per data source and skips depth strategies on assumed-depth data.
  - Fixed a duplicate `spread` column in the feature study.
- **Demo order-path fixes.**
  - Demo harness (`demo_orders.py`) records acks and cancel acks (`SimOrder.ack`/`cancel_ack`).
  - The exchange's duplicate-`client_order_id` rejection is tested.
  - `assert_demo` refuses non-demo hosts.
  - The health check captures sync state before `ws.stop()` (which resets books) and writes connection notes to the tape (the quality report had shown 0 connections).
  - Mock fixes: gap injection only on order-book frames, no streaming on unsubscribed sids, and a thread-safe broadcast.
- **Partial-fill testing.**
  - The partial fill was skipped because the mock stream enlarged the top level. The harness now sizes from the REST top of book, and the test uses a static mock book (`stream_deltas=0`).
  - Partial fill (2 of 3) is now verified against the mock.
- **Other fixes.**
  - DuckDB reserved alias `source` renamed to `src`.
  - Experiments sped up (materialized replay cache, O(1) rolling variance).
  - Dashboard costs are no longer coloured green.
  - CLI `live` refuses cleanly with no DB.
  - `KalshiAPIError` shown as one line.
- **Latest.** Authoritative DEMO endpoints pinned in all templates and tests, plus the new `alphalab net-check`.

## 13. Exact next step

The next priority, in this order:

**REAL KALSHI DEMO CONNECTIVITY → REAL ORDER BOOK → REAL DATA RECORDING → DEMO ORDER TEST → PAPER REPLAY → MORE DATA → STRATEGY VALIDATION**

1. **Owner action**: allow `external-api.demo.kalshi.co` and `external-api-ws.demo.kalshi.co` in the environment's network access. Set `KALSHI_API_KEY_ID` and a key file at `KALSHI_PRIVATE_KEY_PATH` (new DEMO key). Start a new session.
2. Run `alphalab net-check`. If it is blocked, stop and report the host; don't retry.
3. `alphalab markets --active`, then `alphalab demo-check`. This verifies REST, authenticated discovery, WS auth, subscribe, snapshots, book reconstruction and REST/WS top-of-book consistency. Fix any real schema differences at the root, with tests.
4. Record (hours first, then weeks), run `data-quality`, then `ingest`.
5. `demo-orders --confirm-demo`, then `--attempt-fill`.
6. Paper replay, then paper on live data for momentum and crypto fair value first, then maker strategies once trade prints exist.
7. Re-run `research-loop`. Update `DEMO_CONNECTIVITY_REPORT.md`, `DATA_QUALITY_REPORT.md`, `RESEARCH_RESULTS.md` and this file.

## 14. Exact commands

```bash
# setup + tests (no network needed)
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm]"
pytest -q                                              # expect 93 passed

# environment (names only; values come from the environment settings, never from chat/commits)
export KALSHI_ENV=demo TRADING_MODE=paper
export KALSHI_REST_URL=https://external-api.demo.kalshi.co/trade-api/v2
export KALSHI_WS_URL=wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2
# KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must already be set by the environment

# 1. network pre-flight (no credentials; exit 2 = blocked -> stop)
alphalab net-check

# 2. market discovery (public REST; nothing hard-coded)
alphalab markets --active --limit 30
alphalab markets --active --search btc --sort volume --two-sided

# 3. demo connectivity: auth, WS subscribe, snapshots, book rebuild, REST/WS top-of-book cross-check (no orders)
alphalab demo-check --n 3 --seconds 120

# 4. recording (needs API key: WS auth); --external adds Coinbase spot
alphalab record --search BTC --max-markets 10 --duration 3600 --external
alphalab record --series KXBTC15M KXETH15M KXSOL15M KXXRP15M --external      # long-running

# 5. data quality
alphalab data-quality --out DATA_QUALITY_REPORT.md

# 6. ingest into DuckDB (closed hourly files; --include-open for the current one)
alphalab ingest
alphalab db-check

# 7. demo order-path test (DEMO exchange only, fake money)
alphalab demo-orders --confirm-demo
alphalab demo-orders --confirm-demo --attempt-fill

# 8. research / paper
alphalab research-loop && alphalab scorecard
alphalab paper --replay --strategy momentum --params '<exact params>'
alphalab paper --strategy crypto15m_fair_value --params '<exact params>' --series KXBTC15M --record
alphalab paper-evaluate '<strategy_key>'

# emergency stop
alphalab kill --reason "..."      # reset only via: alphalab kill --status && alphalab kill --reset
```

## 15. Development rules

- **Don't use real money.** Keep `KALSHI_ENV=demo` and `TRADING_MODE=paper`. Never send orders to production.
- **Don't expose credentials.** Never ask for them in chat, and never print or log them.
- **Don't commit secrets**: no `.env`, keys, PEMs or tokens. Secrets come only from environment variables.
- **Don't claim profitability without evidence.** Always label results in-sample / walk-forward OOS / holdout / paper / live.
- **Don't optimise to historical data without out-of-sample validation.** Don't tune thresholds to make P&L positive.
- **Don't bypass risk controls.** Don't modify or weaken the live gates, the hard caps, the kill switch or the demo-only guards.
- **Prefer fixing root causes over workarounds**, and add a regression test for every fix.
- If the network is blocked, report the exact host once and stop. Don't loop retries.
- `pytest -q` must pass before every commit. Keep the `CLAUDE.md` invariants (integer prices, shared engine, deterministic replay, no look-ahead, V2 orders only, UTC ns timestamps).
- Don't modify `legacy/`.

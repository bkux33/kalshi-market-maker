# Architecture

```
Kalshi WebSocket (orderbook_delta, trade, ticker, market_lifecycle_v2; fill/user_orders when live)
   │  raw frames, recv timestamp (ns)                       Coinbase ticker (optional, explanatory only)
   ▼                                                                 │
alphalab.kalshi.ws ──► data/raw/<YYYYMMDD>/<HH>/*.jsonl  (append-only tape, hourly rotation)
   │                        │
   │                        ▼  alphalab ingest (idempotent, one transaction per closed file)
   │                  DuckDB  data/alphalab.duckdb
   │                  (book_events, trades, tob, markets, settlements, external_prices,
   │                   runs, orders, fills, round_trips, pnl, signals, experiments, ...)
   │                        │
   ▼                        ▼  ReplaySource (deterministic order: ts, kind, ord)
BookManager ──► OrderBook (L2, YES/NO unified, seq-gap aware)
   │
   ▼
FeatureEngine (same code for research datasets and live strategies)
   │
   ▼
Strategy (deterministic; only sees ctx)  ──► ctx.place()
   │                                            │
   │                                            ▼
   │                                      RiskEngine (pre-trade checks, daily loss, kill switch)
   │                                            │
   │                                            ▼
   │                     SimBroker (backtest & paper: latency, queue, partial fills, fees)
   │                     LiveBroker (live: REST orders, WS fills) — disabled by default
   ▼
TradingEngine records orders/fills/signals/equity ──► metrics ──► experiments ──► validation
                                                                         │
                                                                         ▼
                                                    REJECT / RESEARCH / PAPER (/ LIVE-CANDIDATE via paper)
```

## Packages

| Package | Responsibility |
|---|---|
| `alphalab.core` | fixed-point prices (`prices`), L2 order book, book manager (sequence gaps), fee engine, events, config (YAML + env), JSON logging with secret redaction |
| `alphalab.kalshi` | RSA-PSS signer, REST client (throttle, retries, raises on error), WebSocket client (subscribe, gap resubscribe, reconnect/backoff, raw capture), message parsing, market discovery |
| `alphalab.data` | append-only tape writer/reader, DuckDB schema/access, ingest, recorder service, CSV importers, synthetic data generator |
| `alphalab.sim` | replay source, simulated broker (fill model), portfolio/P&L, trading engine (shared by backtest/paper/live), metrics, backtester |
| `alphalab.strategies` | strategy base, imbalance, market maker, logical arbitrage, crypto-15m fair value, momentum/mean-reversion/volatility baselines, random-direction benchmark |
| `alphalab.research` | features, predictive-feature study, experiments, validation/classification, arbitrage scan, reports/scorecards, analyses, research loop, read-only LLM assistant |
| `alphalab.execution` | risk engine, kill switch, trading session (paper/live drivers), journal, live broker + live gate |
| `alphalab.dashboard` | FastAPI JSON API + static single-page UI |
| `legacy/` | the untouched upstream kalshi-market-maker code (reference/provenance) |

## Key design decisions

* **One engine, three modes.** `TradingEngine` + `RiskEngine` + a broker are identical in
  backtest, paper and live; only the broker and the clock differ. Paper cannot be more optimistic
  than the backtest because it *is* the backtest fill model driven by live data.
* **Raw first.** The recorder stores frames verbatim before parsing, so parser bugs can be fixed and
  data re-ingested. Receive timestamps (ns) are the replay clock; exchange timestamps are kept.
* **Integer price units** ($0.0001) avoid float keys and support deci-cent markets.
* **Single DuckDB writer.** Long-running services write JSONL; short jobs take the DB lock
  briefly; the dashboard is read-only.
* **Determinism.** No wall clock or unseeded randomness in simulation; `result_hash` proves it.
* **Validation is data-driven.** Status comes from configurable rules applied to out-of-sample
  evidence; experiments can never mark anything live-eligible.
* **LLMs are advisory only.** The assistant has read-only analysis tools and no import path to
  execution code.

## Data model highlights

* `book_events`: `snapshot` rows carry JSON level arrays, `delta` rows carry side/price/delta; `sid`/`seq`
  preserved for gap detection during replay.
* `tob`: derived top of book per book change (bid/ask/sizes/depth/imbalance/microprice/synced).
* `settlements.inferred`: true when a result was inferred (e.g. from pinned quotes in a third-party CSV);
  an official result is never overwritten by an inferred one.
* `runs` (+ `orders`, `fills`, `round_trips`, `pnl`, `signals`): every backtest/experiment/paper run,
  with config, data hash and result hash.
* `experiments`, `experiment_results`, `strategy_status`, `predictive_results`, `risk_events`.

# Repository audit (phases 1–5)

Audit date: 2026-09-24. Scope: the four repositories named in the brief (forks in the
`bkux33` account were inspected at their current `main`), the user's `kmmx` repository,
and newer public alternatives found by web search (inspected from their public pages
only; no code was copied from any of them).

## Summary table

| Repository | Licence | Last commit | Size | Kalshi API | Real-time data | Order book | Backtest | Paper | Risk | Tests |
|---|---|---|---|---|---|---|---|---|---|---|
| **zachdaube/kalshi-market-maker** (selected base) | **Apache-2.0** (LICENSE file; its README says "MIT", the LICENSE file governs) | 2026-02-09 | ~6.7k LoC Python | `kalshi_python_sync` SDK + raw calls | REST polling (1 s) | YES/NO→unified view, VWAP, depth | none | dry-run only (no fill simulation) | stop-loss, global position cap, size taper | 175 pytest (174 pass) |
| reedjacobp/kalshi-trading-bot | MIT per `package.json`; no LICENSE file | 2026-04-23 | ~15k LoC Python + ~10k TS | own RSA-PSS client | WS `ticker` channel + REST | top-of-book parser (`parse_book_top`) | Monte-Carlo optimiser for one strategy | yes | daily/weekly loss, cooldowns | 77 unittest |
| kapelame/kalshi-crypto-bot | MIT per README; no LICENSE file | 2026-02-27 | ~5.5k LoC | aiohttp REST | REST polling (2 s) | top of book + imbalance | rule/XGBoost backtester (fills at quote) | yes | exposure/Kelly | none |
| rnop/Kalshi-Prediction-Market-Trading-Bot-Public-Baseline | **none** (all rights reserved) | 2026-07-15 | 825 LoC single file | `requests` REST | REST polling | full-depth snapshots | none (notebook analysis) | yes (logging) | entry-zone filter | none |
| bkux33/kmmx (user's repo) | none stated | 2026-08-12 | ~2.6k LoC | stdlib `urllib`, V2 batched orders | REST polling | fixed-point book | JSONL replay | yes | reward-aware EV gate, approved-ticker list | 11 unittest |
| ericsohel/KalshiTradingProject (web) | MIT | active (69 commits) | Python + TS | own | WS "flight recorder" to zstd tape | exact book rebuild | deterministic replay, simulator "ahead" | – | – | 771 tests reported |
| Viprasol-Tech/kalshi-trading-bot (web) | MIT | 4 commits | Python | async REST + WS | WS | yes | simple backtester | dry-run | Kelly + limits | 56 tests reported |
| suryacks/MarketsBot (web) | not stated | 82 commits | Rust | own | WS | recorded books | tape/book replay, queue model | yes | caps | – |

## Per-repository notes

### zachdaube/kalshi-market-maker (selected)
* **Architecture**: `src/` library (client, orderbook, quotes (Avellaneda-Stoikov), fees, flow
  toxicity analyser, scanner, config) plus three entry points (`run_market_maker.py`,
  `dashboard.py` Flask/SocketIO, `trading_worker.py`) that each re-implement the trading loop.
* **Kalshi API**: wraps `kalshi_python_sync`, bypassing it with raw `call_api` for most GETs.
  Errors are printed and swallowed (methods return `None`/`[]`).
* **WebSocket**: none; REST polling of the order book each second.
* **Order book / fees / execution**: correct NO→YES conversion; fees modelled at 7%/1.75% without
  Kalshi's per-order rounding; cancel-replace quoting of a YES bid + NO bid.
* **Backtest / paper**: no backtester; "dry run" only logs quotes — nothing measures fills,
  adverse selection or P&L.
* **Code quality**: readable, well commented, decent unit tests of the maths. The bundled
  `CLAUDE.md` is stale (it says flow detection is not integrated; `execution.py` now integrates it).
* **Why selected**: the only candidate with an explicit OSI licence file (Apache-2.0), it has the
  most relevant building blocks for market making (AS quoting, book handling, scanner, inventory
  logic), and it is the brief's preferred base. No candidate was *clearly* superior as a
  foundation: ericsohel's recorder/replay design is stronger for data capture, and that design
  idea (raw frames to an append-only tape before parsing) was adopted — but not its code.

### reedjacobp/kalshi-trading-bot
Most production-hardened of the four (systemd units, SSE dashboard, reconciliation against the
settlement API, rate-limit handling, vendored Kalshi OpenAPI/AsyncAPI specs). It is built around one
strategy ("Resolution Rider": buy 95–98¢ near settlement) and a parameter optimiser whose deploy gate
compares in-sample CV cells — useful ideas, but the fill model is a probabilistic slippage model and
there is no order-book replay. Its notes confirm maker fees of $0 on the crypto 15-minute series.
Used as a reference for: the fixed-point (`*_fp`, `*_dollars`) message formats (via the vendored
Kalshi specs), the ticker-channel staleness caveat, and reconciliation practice. No code copied.

### kapelame/kalshi-crypto-bot
Clean, well-documented framework with the strategy intentionally stripped. 2-second REST polling,
XGBoost training, backtester that fills at the quoted price. Its 82-minute BTC/ETH/SOL/XRP sample
(top of book with strikes and spot) is used — imported, not redistributed — as one of the two
real-data samples. No tests.

### rnop/…Public-Baseline
Single-file live bot (RSI on Binance + entry zone), DuckDB logging, Discord alerts. No licence,
so nothing may be reused; only its 29-minute full-depth ETH sample CSV was read locally to
exercise the pipeline (not redistributed). Resolutions in that CSV may be inferred from pinned
quotes (see its `_infer_resolution_from_snapshots`), so they are imported as `inferred`.

### bkux33/kmmx
The user's own clean-room market maker: stdlib-only, fixed-point order book, V2 batched order API,
reward-aware quoting, JSONL recorder and replay. Its research notes on the Liquidity Incentive
Program and V2 order semantics were read. No licence file, so treated as reference only.

## Phase 4 – running the base exactly as provided

| Step | Result |
|---|---|
| `pip install -r requirements.txt` | OK (`kalshi_python_sync` 3.2.0) |
| `pytest tests/` | **174 passed, 1 failed** — `test_scanner.py::test_basic_parsing` hard-codes a close time of 2026-03-15, now in the past (time bomb, not a logic bug) |
| `python scan_markets.py` | fails without `KALSHI_KEY_ID`; with it, needs network access |
| `python run_market_maker.py --env demo` | starts, then `No such file: kalshidemo.txt`; the demo config ticker is the placeholder `REPLACE_WITH_DEMO_MARKET_TICKER` |
| Live API | not reachable from this build environment (egress to `api.elections.kalshi.com` is blocked), so no live run was possible |

## Phase 5 – what works and what does not

Works: AS reservation price/spread maths, NO→YES conversion, VWAP/depth helpers, config loader,
scanner scoring, flow-toxicity metrics, the unit tests of all of these.

Does not work / risky (verified by reading code and by targeted tests):

1. **Order book is empty against the current API.** `KalshiClient.get_orderbook` reads
   `data["orderbook"]`; the current `GET /markets/{t}/orderbook` response (per Kalshi's own OpenAPI
   spec vendored in reedjacobp's repo) carries `orderbook_fp` with dollar-string prices. Fed that
   response, the upstream client returns `{"yes": [], "no": []}`, so the bot never quotes.
2. **No fill simulation, no P&L measurement** — "dry run" cannot tell whether quoting makes money.
3. **Stop loss uses mid**, positions sync every 5 s, cancels are cleared even when they fail
   (ghost orders), stats are not thread-safe; the dashboard path lacks the engine's risk checks.
4. **Fee model** ignores Kalshi's round-up-to-the-cent and per-series maker-fee applicability.
5. **Live-trading defaults**: `docker-compose.yml` runs `trading_worker.py --env prod --live`
   by default.
6. Tests: one date-dependent failure (above).

## Phase 6 – refactor decision

The upstream code is preserved unchanged in `legacy/` (still runnable, still Apache-2.0). The new
`alphalab/` package re-implements the useful pieces for fixed-point prices, streaming data and
simulation, crediting the upstream design where it is followed (order-book conversion, AS quoting,
scanner heuristics). Everything else (tape recorder, replay simulator, experiment/validation engine,
risk engine, paper/live sessions) is new.

Sources for web-audited alternatives:
[ericsohel/KalshiTradingProject](https://github.com/ericsohel/KalshiTradingProject),
[Viprasol-Tech/kalshi-trading-bot](https://github.com/Viprasol-Tech/kalshi-trading-bot),
[suryacks/MarketsBot](https://github.com/suryacks/MarketsBot),
[goldenteeplanet/kalshi-predictive-bot](https://github.com/goldenteeplanet/kalshi-predictive-bot),
[TheMarinoGroup/kalshi-demo-bot](https://github.com/TheMarinoGroup/kalshi-demo-bot).

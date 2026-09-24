# Data quality report (2026-09-24)

> **No real Kalshi market data was collected in this stage.** The environment's egress proxy refuses every
> Kalshi host and no API credentials are configured here (see DEMO_CONNECTIVITY_REPORT.md). This report
> therefore has three parts: (1) the collection that is still outstanding, (2) the quality of the only real
> Kalshi data available (two third-party samples imported earlier), and (3) a validation of the new
> data-quality tool on a local mock session with injected faults. Regenerate part 1 with
> `alphalab data-quality --out DATA_QUALITY_REPORT.md` once recording runs against DEMO.

## 1. Real DEMO collection
| Metric | Value |
|---|---|
| Collection period | none — not started (network blocked) |
| Markets recorded | 0 |
| Total messages / order-book updates / trades | 0 / 0 / 0 |
| Sequence gaps / reconnects / duplicates | n/a |
| Storage | 0 MB |

## 2. Real Kalshi samples imported earlier (third-party CSVs, not recorded by us)
| Metric | ETH full-depth sample | Crypto top-of-book sample |
|---|---|---|
| Collection period (UTC) | 2026-03-02 19:46–20:15 (29 min) | 2026-02-25 04:22–05:45 (82 min) |
| Markets | 2 | 25 |
| Order-book records | 1,432 full-depth snapshots (~1.3 s) | 7,618 top-of-book snapshots (2 s) |
| Distinct top-of-book states | 1,185 | 467 (quotes change only ~every 33 s per market) |
| Trades | 0 (no trade feed in source) | 0 (no trade feed in source) |
| Sequence numbers | none in source (REST polling) | none in source |
| Depth sizes | recorded | **not recorded** (assumed 10 per level, flagged) |
| Settlements | 2, inferred | 24, inferred from pinned final quotes |
| Timestamps | fixed this stage: µs→ns import bug corrected, regression-tested | epoch seconds × 1e9 (correct) |
| Crossed books | 0 | 0 |
| Consequence | usable for 1–30 s microstructure checks on 2 markets only | the mid never moved during any 10 s hold in 265 replayed trades: short-horizon results on this sample only measure spread paid |

Deterministic replay on these samples (each strategy run twice, identical result hashes; in-sample, default
parameters, not validation): imbalance −$9.60 net (52 trades), market maker −$1.29 (5), momentum −$6.89 (42)
and mean reversion −$15.14 (64) on the ETH sample; momentum −$59.87 and mean reversion −$59.87 (265 each,
spread-only as explained above) and crypto fair value +$8.27 (24 trades) on the crypto sample. These are
simulations on < 2 hours of data and say nothing about profitability.

## 3. Tool validation on a local mock (NOT Kalshi data)
The mock injected one malformed frame, one sequence gap, periodic exact re-deliveries and a forced
disconnect; the tool must find all of them and the books must still reconstruct and match the exchange.

| Metric | Value |
|---|---|
| Collection period (UTC) | 2026-09-24T23:27:03.779+00:00 → 2026-09-24T23:27:07.788+00:00 (4.009 s = 0.001 h) |
| Tape files / open (unfinalized) | 1 / 0 |
| Estimated storage size | 0.04 MB |
| Records (all kinds) | 140 |
| Markets recorded | 2 |
| WebSocket messages by type | {'subscribed': 9, 'orderbook_snapshot': 7, 'orderbook_delta': 100, 'trade': 15, 'unsubscribed': 1} |
| Order-book snapshots / deltas | 7 / 100 |
| Trades (duplicate trade ids) | 15 (9) |
| Sequence gaps | 1 |
| Duplicate messages (sequence / exact frame) | 10 / 14 |
| Connections / reconnects / disconnects | 2 / 1 / 2 |
| Missing receive timestamps | 0 |
| Deltas/trades without exchange timestamp | 0 |
| Malformed WS frames / torn lines | 1 / 0 |
| Error frames from exchange | 0 |
| Books reconstructed / synced at end | 2 / 2 |
| Crossed-book observations (should be 0) | 0 |
| Valid records | 99.286% |

Duplicate trade ids come from the mock re-sending the same trades after the reconnect; the recorder keeps
raw frames verbatim, and de-duplication happens at replay/ingest (`trade_id`, sequence numbers).

## Timestamp handling
* Receive time: `time.time_ns()` at frame arrival, stored verbatim as integer epoch **nanoseconds** (`r`).
* Exchange time: `ts_ms` (epoch ms) preferred, else `ts` (epoch s or ISO-8601); converted with integer
  arithmetic, no float rounding (tests cover s, ms, float s, ISO with 9-digit fractions, offsets, garbage).
* All stored `*_ns` columns are UTC epoch nanoseconds; an end-to-end test asserts recorded values fall in
  the 1.7e18–2.2e18 range, so a µs/ms mix-up fails the build.

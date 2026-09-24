# DEMO connectivity report (2026-09-24)

> **Status: Kalshi DEMO connectivity has NOT been achieved from the build environment.**
> Every Kalshi host (`external-api.demo.kalshi.co`, `external-api-ws.demo.kalshi.co`,
> `demo-api.kalshi.co`, `external-api.kalshi.com`, `api.elections.kalshi.com`, `docs.kalshi.com`,
> `kalshi.com`) is refused by the environment's egress proxy (`CONNECT tunnel failed, response 403`),
> and `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` are not set in this environment.
> No real Kalshi message was received, no real order was placed, no real data was collected.
> Everything below marked **mock** was proven against a local mock of the current API, not Kalshi.

## API specification checked
| Source | Version / date | How used |
|---|---|---|
| Official Kalshi Python SDK `kalshi-python-sync` (generated from Kalshi's OpenAPI spec) | **3.30.0, published 2026-09-15** (PyPI) | REST hosts, auth signing, order-book schema, V2 order request/response schemas, endpoint paths, market/series fields, fee types |
| Kalshi AsyncAPI (WebSocket) and OpenAPI specs vendored in reedjacobp/kalshi-trading-bot | API v2 spec, April 2026 | WebSocket command/message formats, market status enums |
| Kalshi docs pages (docs.kalshi.com) — via search-engine snippets only (site blocked) | 2026 | recommended hosts, `ts_ms`, `get_snapshot` action (2026-04-20), shard (`exchange_index`) changes (2026-08-24), fee schedule (7 Jul 2026) |

The docs site itself could not be read directly; where only snippets were available this is stated.

## Outdated assumptions found and fixed
| # | Area | Old assumption | Current API (source) | Fix |
|---|---|---|---|---|
| 1 | Order book (upstream bug) | REST order book under `orderbook` with integer cents | `orderbook_fp` with `yes_dollars` / `no_dollars` `[price, count]` strings (SDK 3.30.0) | Parser already handled it in Alpha Lab; regression tests added so the upstream empty-book bug cannot recur |
| 2 | Order entry | `POST /portfolio/orders` with `yes_price`/`no_price`, `action`, `side yes/no` | **V2** `POST /portfolio/events/orders`: `side: bid/ask` on one YES book, `price` dollar string, `count` fixed-point string, required `time_in_force` and `self_trade_prevention_type`; legacy endpoint deprecated from May 2026 (SDK) | `order_body` + `LiveBroker` + REST client moved to V2 |
| 3 | Cancellation | `DELETE /portfolio/orders/{id}`; batch body `{"ids": [...]}` | `DELETE /portfolio/events/orders/{id}?market_ticker=…` → `reduced_by`, `ts_ms`; batch `DELETE /portfolio/events/orders/batched` `{"orders":[{order_id, market_ticker}]}` | Migrated; cancel acknowledgements recorded |
| 4 | Hosts | `demo-api.kalshi.co` / `api.elections.kalshi.com` for REST and WS | Recommended: REST `external-api(.demo).kalshi(.co)`, WS `external-api-ws(.demo).kalshi(.co)`; old shared hosts "also supported" | Defaults switched; `KALSHI_HOST_PROFILE=legacy` or `KALSHI_REST_URL`/`KALSHI_WS_URL` override |
| 5 | Timestamps | deltas/trades carry `ts` (ISO or seconds) | matching-engine `ts_ms` (epoch ms) on deltas, trades, fills, user_orders | `ts_ms` preferred; exact integer-nanosecond conversion (the old float path lost precision) |
| 6 | Duplicates / reconnects | any seq ≠ last+1 is a gap | duplicates (seq ≤ last) must be ignored; seq restarts after reconnect/resubscribe | Book manager distinguishes duplicate vs gap; snapshot restarts a stream; trade de-dup by `trade_id`; frames for closed sids dropped |
| 7 | Closed markets | snapshot always has levels | closed markets send a snapshot with no level arrays and empty `market_id` | Treated as a valid empty book (tested) |
| 8 | Market fields | `volume_24h`, `liquidity_dollars` | `volume_24h` absent from the Market model; `liquidity_dollars` deprecated; category lives on the Series | Discovery ranks by `volume_fp` fallback; categories from `GET /series` |
| 9 | Fee types | `quadratic`, `quadratic_with_maker_fees`, `flat` | adds `quadratic_with_combo_maker_fees` | Charged maker fees (conservative) |
| 10 | Timestamp import bug (found last stage) | pandas timestamps are ns | pandas 3 can parse at µs | fixed + regression tests |

Unchanged and confirmed: RSA-PSS SHA-256 signing of `{timestamp_ms}{METHOD}{path}` (path without query,
including `/trade-api/v2`), headers `KALSHI-ACCESS-KEY/-TIMESTAMP/-SIGNATURE`; WebSocket auth via the same
headers on the upgrade request signing `GET /trade-api/ws/v2`; subscribe
`{"id", "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}`; `orderbook_snapshot`
(`yes_dollars_fp`, `no_dollars_fp`) then `orderbook_delta` (`price_dollars`, `delta_fp`, `side`), per-sid `seq`;
`trade` (`yes_price_dollars`, `count_fp`, `taker_side`, `trade_id`); markets status filter
`unopened|open|paused|closed|settled`; taker 0.07·C·P·(1−P) rounded up, makers 0.0175 where charged.

## Results
| Check | Real Kalshi DEMO | Local mock of current API |
|---|---|---|
| REST connectivity (public) | **blocked** (egress 403) | pass |
| REST authentication (signed `GET /portfolio/balance`) | **not run** (no credentials in env; blocked) | pass — mock verifies every RSA-PSS signature |
| WebSocket connect + auth | **blocked** | pass — mock verifies the upgrade signature |
| Subscribe + receive messages | **not run** | pass (snapshots, deltas, trades) |
| Order-book reconstruction | **not run on real messages** | pass — reconstructed top of book equals REST order book, including after injected duplicates, a sequence gap, a malformed frame and a forced disconnect |
| Market discovery (`alphalab markets`) | **blocked** | pass (search, category, status, close-time, volume, spread filters) |
| Recorder → ingest → deterministic replay | **no real data** | pass (identical result hashes; crash-recovered tape) |
| DEMO order path (`alphalab demo-orders`) | **not run** | pass: create+ack, local duplicate block, exchange duplicate `client_order_id` rejection (409), reconnect with resting order, cancel+ack, stale-price block, kill switch, fill, partial fill (2 of 3) |
| Live trading | refused (unchanged gates) | refused |

## Known remaining API issues / unverified items
1. **Nothing has been verified against the real exchange.** First real run must be `alphalab demo-check`.
2. WebSocket host `external-api-ws.demo.kalshi.co` comes from docs snippets and third-party guides; if it
   fails, use `KALSHI_HOST_PROFILE=legacy` (`wss://demo-api.kalshi.co/trade-api/ws/v2`).
3. `get_snapshot` (added 2026-04-20) is not used; resync uses unsubscribe + resubscribe, which is
   documented and works, but costs a round trip. Exact `get_snapshot` command syntax was not verifiable.
4. The `fill` / `user_orders` payload fields are taken from the April 2026 AsyncAPI spec; the V2 era may
   add `side: bid/ask` fields. The live broker maps fills by `order_id`, so it does not depend on side fields,
   but this should be checked on DEMO.
5. Exchange shards (`exchange_index`, crypto moved to shard 2 on 2026-08-24): V2 orders auto-route by ticker
   when `exchange_index` is omitted, which is what we do. WebSocket shard parameters exist only for some
   channels (`communications`); not needed for market data as far as the spec shows.
6. Rate limits: the client throttles to 8 requests/s by default; the account's actual tier is unknown.

## How to complete this stage
1. In the environment settings: allow network access to `external-api.demo.kalshi.co`,
   `external-api-ws.demo.kalshi.co` (and `demo-api.kalshi.co` as fallback); add `KALSHI_API_KEY_ID` and a
   way to provide the key file at `KALSHI_PRIVATE_KEY_PATH` (e.g. a setup script writing it from a secret,
   `chmod 600`). Use a **new** DEMO key.
2. `alphalab markets --active --limit 30`
3. `alphalab demo-check --n 3 --seconds 120`
4. `alphalab record --search BTC --max-markets 10 --duration 3600` (and other liquid markets)
5. `alphalab data-quality --out DATA_QUALITY_REPORT.md` then `alphalab ingest`
6. `alphalab demo-orders --confirm-demo` (add `--attempt-fill` to test fills with DEMO money)

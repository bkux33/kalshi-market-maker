# Risk controls

The same `RiskEngine` runs in backtest, paper and live. Backtests keep every limit except the
P&L-based halts (so a strategy's full loss distribution is observed); paper and live apply all.

## Pre-trade checks (every order, in this order)
| Check | Config | Behaviour |
|---|---|---|
| halted / kill switch | `kill_switch_file` | reject everything |
| disconnected | — | reject while the market-data connection is down |
| stale data | `stale_data_s` (5) | reject if the market's book has not updated recently |
| settlement protection | `settlement_buffer_s` (60) | no new orders this close to close time |
| price sanity | `min/max_price_units`, `max_distance_from_mid_units` (15¢) | reject out-of-range or far-from-mid prices; reject if no two-sided market |
| order size | `max_order_size` | |
| duplicates | `duplicate_window_s` (2) | same strategy/market/side/price/qty within the window, a re-used client order id, or an identical resting order |
| order rate | `max_orders_per_second` | rolling one-second window |
| open orders | `max_open_orders` | |
| position | `max_position_per_market` | worst case including resting orders on the same side |
| exposure | `max_market_exposure_usd`, `max_strategy_exposure_usd`, `max_total_exposure_usd` | capital at risk (long: qty×price; short: qty×(1−price)), resting orders counted as filled |

Orders that strictly reduce an existing position skip position/exposure limits so the system can
always de-risk, but never skip the kill switch, stale-data, connectivity or price checks.

## Continuous monitoring (paper/live)
* **Daily loss** (`max_daily_loss_usd`, UTC day, realised + unrealised net of fees) and **max
  drawdown** (`max_drawdown_usd`) → halt + trip the kill switch.
* **Stale data**: resting orders in a market with no book update for `stale_data_s` are cancelled.
* **Disconnect**: `on_disconnect: cancel_all` (default) cancels every order when the WebSocket drops;
  the WS client reconnects with exponential backoff and resubscribes (fresh snapshots).
* **Sequence gaps**: the affected books are marked unsynced (no trading on them) and the order-book
  subscription is re-requested.
* **Settlement**: resting orders are cancelled at market close; positions settle at the official value.
* **Live reconciliation**: `LiveBroker.reconcile()` compares local and exchange positions.

## Kill switch
A latched file (`data/KILL_SWITCH` by default). While it exists every session cancels all orders
and refuses new ones; a restarted process stays halted. Trip it with any of:
* `alphalab kill --reason "..."`
* the red button on the dashboard (`POST /api/kill`)
* the risk engine (daily loss / drawdown)
* **a physical button**: anything that can create a file. Example for a Raspberry Pi GPIO button:
  ```python
  from gpiozero import Button; import pathlib, signal
  Button(17).when_pressed = lambda: pathlib.Path("/srv/alphalab/data/KILL_SWITCH").write_text('{"reason":"physical button"}')
  signal.pause()
  ```
  or a USB foot switch mapped to a key that runs `alphalab kill`.

Reset is deliberately CLI-only: `alphalab kill --reset` (check `alphalab kill --status` first).

## Live trading gates (`execution/live.py::assert_live_allowed`)
Live mode is refused unless **all** hold:
1. `TRADING_MODE=live` (default is `paper`);
2. `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_RISK`;
3. `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` set;
4. kill switch not tripped;
5. `max_order_size ≤ 100`, `max_total_exposure_usd ≤ 2000`, `max_daily_loss_usd ≤ 500`, all finite
   (hard caps in code — raising them requires a code change and review);
6. every strategy's exact parameter set has status **LIVE-CANDIDATE**, which only paper trading can
   award (`alphalab paper-evaluate`). The single exception, `--demo-integration`, works only against
   the Kalshi **demo** exchange (fake money) for connectivity testing.

No LLM can place orders: the research assistant has only read-only analysis tools and does not
import execution code (tested).

## Steps before enabling live trading
1. Record your own data for weeks (`alphalab record`), not the bundled samples.
2. Run experiments; only PAPER-status strategies proceed.
3. Paper trade the exact parameter set on live data until `paper-evaluate` promotes it (≥ 200 fills,
   ≥ 10 days, paper expectancy consistent with the backtest).
4. Run `--demo-integration` on the demo exchange to verify order placement, fills, cancels,
   reconnects and the kill switch end to end with the live broker.
5. Set small limits, set `TRADING_MODE=live` and `LIVE_TRADING_ACK`, start, and watch the dashboard.
   Test the kill switch on day one.

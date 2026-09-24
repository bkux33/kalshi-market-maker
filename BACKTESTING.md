# Backtesting, experiments and validation

## Replay engine
`ReplaySource` merges recorded order-book snapshots/deltas, public trades, external prices, market
close markers and settlements, ordered by `(receive timestamp, event kind, insertion order)`. Every
run of the same data and configuration produces an identical fill sequence (`result_hash`; tested).
Time-range blocks start from the latest snapshot before the block ("warm-up") so books are complete
before any strategy sees them. Strategies only receive the book/feature state *as of* the event
being processed (tested: no feature timestamp is ever ahead of the clock).

## Fill model (explicit and configurable: `fill:` in the config)
| Assumption | Default | Meaning |
|---|---|---|
| `order_latency_ms` | 150 | a decision at t reaches the exchange at t + latency and is matched against the book *then* |
| `cancel_latency_ms` | 150 | a resting order can still fill until the cancel lands |
| taker execution | walk the book | partial fills across levels up to the limit price; IOC remainders cancelled |
| impact overlay | 5 s | liquidity we took cannot be taken again while the recorded book still shows it |
| `taker_slippage_ticks` | 0 | extra adverse ticks on every taker fill (stress uses +1) |
| `maker_fill_mode` | `queue` | join the back of the queue; trades at our price consume the queue ahead first |
| `queue_cancel_model` | `pro_rata` | how unexplained size decreases at our level are split ahead/behind (`back` = conservative) |
| `trade_through_fills_all` | true | a print through our price fills us (up to the print size) |
| `allow_touch_fill` | false | if true, the opposite side merely touching our price fills us |
| cross fills | always | if the recorded opposite side moves *through* our price, we fill up to its displayed size — the adverse-selection case |
| after close / settlement | no fills | resting orders cancel at close; positions settle at the official (or flagged inferred) value |

Never assumed: fills at the mid, fills better than the limit, fills before an order is live, fills
larger than the visible counterparty, or that every quote fills.

Data without trade prints (e.g. snapshot-only samples) supports only cross fills for makers, which
is conservative. Data without depth sizes (top-of-book-only samples) is imported with an *assumed*
size that is recorded in `markets.depth_quality`; any experiment on such data gets a gating
`assumed_depth` flag.

## Costs
Fees come from `core/fees.py` (Kalshi's quadratic schedule, rounded up to the cent per fill,
per-series fee type and multiplier). **Unknown series are charged the 1.75% maker fee** (conservative).
Every result reports, separately:

* `mid_pnl` — P&L had every fill happened at the mid
* `exec_cost_vs_mid` — paid versus mid (negative for makers = spread captured)
* `est_slippage` — the taker part of that cost
* `gross_pnl` = mid_pnl − exec_cost_vs_mid
* `fees`
* **`net_pnl` = gross_pnl − fees** — the only headline number

## Metrics reported for every run
trades (round trips), fills, gross, fees, estimated slippage, net, win rate, average and median
trade, profit factor, expectancy, per-trade Sharpe, daily annualised Sharpe (≥ 5 days only),
t-statistic, maximum drawdown (on the equity curve incl. unrealised), average/max exposure (capital
at risk), traded notional and turnover, average holding period, fill rate, markouts at 1/5/30/60 s
(all/maker/taker) and adverse selection, inventory statistics, P&L by market and by day,
top-market and top-day share of profits, open positions at the end.

## Experiment protocol (`alphalab experiment`, `research/experiments.py`)
1. **Universe** ordered by close time. **Holdout test** = last 20 % (default), locked.
2. **Development set** split into `walk_forward_folds + 1` chronological blocks (by market when there
   are enough markets, otherwise by time). Grid size is capped (`max_grid_size`, default 64) — the
   engine refuses larger searches.
3. **Walk-forward:** for fold k, choose parameters on blocks < k only, evaluate on block k.
   The concatenated folds are the **out-of-sample (OOS)** record; the last fold is the validation set.
4. **Final parameters** (chosen on all development blocks except the last) are stressed on the OOS
   blocks: 2× fees, +1 tick taker slippage, +250 ms latency, conservative queue model.
5. **Sensitivity:** neighbouring grid points on the validation block.
6. **Random benchmark:** random direction at the same entry times (taker strategies), ≥ 200 trials
   by default; fewer trials than needed to resolve p < 0.05 raise `benchmark_underpowered`.
7. **Deflated/probabilistic Sharpe:** PSR of OOS per-trade returns versus the expected maximum Sharpe
   of the number of grid points tried.
8. **Classification** (below). The **holdout** is evaluated only if every other gate passed, at most
   once per experiment; re-use across experiments of the same strategy raises `holdout_reused`.

## Flags (gating unless noted)
`small_sample`, `suspicious_sharpe`, `concentrated_market`, `concentrated_day`, `few_days`,
`few_markets`, `dies_after_costs` (REJECT), `fails_fees_x2`, `fails_slippage_plus_1tick`,
`fails_latency_plus_250ms`, `fails_conservative_queue`, `parameter_sensitive`,
`too_many_parameters`, `is_oos_degradation`, `walk_forward_inconsistent`,
`not_better_than_random`, `benchmark_underpowered`, `low_deflated_sharpe`, `fails_holdout`,
`holdout_reused`, `assumed_depth`, `synthetic_data`; informational: `no_random_benchmark`,
`inferred_settlements`.

## Classification (all thresholds in `validation:` config)
| Status | Rule |
|---|---|
| REJECT | no OOS trades, OOS net ≤ 0, or positive before costs but not after |
| RESEARCH | OOS net > 0 but fewer than `min_trades_research` trades, or any gating flag, or holdout not yet evaluated |
| PAPER | OOS net > 0, no gating flags, holdout positive — eligible for **paper trading only** |
| LIVE-CANDIDATE | only via `alphalab paper-evaluate`: ≥ `min_paper_fills_live` paper fills over ≥ `min_paper_days_live` days, paper net > 0 and paper expectancy ≥ 50 % of backtest OOS expectancy |

## Validating the validator
The test suite runs the full protocol on synthetic data with **no** edge (must be REJECT, holdout
untouched) and with a **planted** edge (must show positive OOS net P&L, yet stay RESEARCH because
synthetic data is a gating flag). A planted order-book signal that is real but smaller than costs is
correctly REJECTed as `dies_after_costs` while beating the random benchmark — the most common real-
world outcome.

## Reading results correctly
Always say which sample a number comes from: **historical in-sample**, **walk-forward out-of-sample**,
**holdout**, **paper**, **live**. A positive backtest is never evidence of profitability by itself.

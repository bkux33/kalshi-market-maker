# Strategies

Every strategy here is a **hypothesis under test**, not a claim. Each is deterministic, uses only
information available at decision time (enforced by the replay engine and tested), and trades
through the same risk engine and fill model in every mode. Parameters searched by the experiment
engine are listed as `tunable`; default grids are in `alphalab/research/loop.py`.

## 1. Order-book imbalance (`imbalance`)
*Hypothesis:* lopsided visible depth predicts the next mid move within a short horizon.
*Signal:* `imbalance = bid_depth / (bid_depth + ask_depth)` over the top 5 levels (or level 1).
Buy YES when `imbalance >= threshold`, sell YES when `<= 1 - threshold`.
*Execution:* IOC taker entry at the best opposite price; exit after `horizon_s` with IOC retries,
otherwise held to settlement. Filters: max spread, time-to-settlement, price band, optional volatility cap.
*Research companion:* `alphalab features` measures the IC of imbalance/microprice/trade-flow features
against forward mid moves at 1/3/5/10/30/60 s and compares the extreme-quintile move to the round-trip
cost hurdle (spread + 2 × taker fee).
*Default grid:* threshold ∈ {0.55, 0.60, 0.65, 0.70} × horizon ∈ {1, 3, 5, 10, 30} s.

## 2. Inventory-aware market making (`market_maker`)
Avellaneda–Stoikov adapted to binary contracts (formulas in the module docstring): reservation
price shifted by inventory × γσ²τ, half-spread from γ and k, floored at the maker fee + `min_edge_c`,
optional imbalance skew, hard inventory limits, stop quoting near settlement, post-only quotes,
cancel-then-replace. *What is measured:* spread capture (`exec_cost_vs_mid` is negative for makers),
adverse selection (markouts at 1/5/30/60 s after each fill), inventory statistics and settlement risk.
A maker whose spread capture is outweighed by adverse selection shows positive spread capture and
negative net P&L — and is classified REJECT.
*Default grid:* min_edge_c ∈ {0.25, 1, 2} × gamma ∈ {0.05, 0.2}.

## 3. Cross-market / logical arbitrage (`logical_arb`)
Relationships among markets of the same event:
* mutually exclusive outcomes, **sell side**: Σ best YES bids > $1 + fees → sell every YES;
* mutually exclusive **and exhaustive**, buy side (opt-in, exhaustiveness is not guaranteed by the
  API flag): Σ best YES asks < $1 − fees → buy every YES;
* strike ladders: P(S ≥ K1) ≥ P(S ≥ K2) for K1 < K2 → buy K1 / sell K2 when ask(K1) < bid(K2) − fees;
* single-market YES/NO complement: impossible on Kalshi's unified book (checked as data integrity).
Legs are not atomic; residual leg exposure is flattened after `unwind_after_ms`, and that cost is
in the P&L. `alphalab.research.arb_scan` counts violations offline, their persistence versus the
order latency, and net edge after taker fees on every leg.

## 4. Short-duration crypto (`crypto15m_fair_value`)
Uses spot prices only as an explanatory variable: `fair = Φ(ln(S/K) / (σ·√τ))` with σ from recent
spot returns. Trades when the contract is mispriced versus that fair value by more than `edge_c`
after crossing the spread; holds to settlement by default. The research loop separately runs an
**incremental-information test**: out-of-sample log-loss of a logistic model of settlement using
only the market's own price versus price + spot features, compared market by market (markets are
the independent units). Known model gaps: Kalshi settles on a benchmark average (not one exchange's
spot), and drift/basis/funding/liquidations are not modelled (hooks exist in the feature engine).

## 5. Momentum / mean-reversion / volatility baselines
`momentum`, `mean_reversion` (sign of the `lookback_s` mid return, entry threshold `min_move_c`),
`vol_breakout` (short/long realised-variation ratio, follow the burst) and `vol_fade` (fade it).
These are baselines any claimed edge should beat.

## Benchmark: `random_entry`
Enters at the tested strategy's own entry timestamps with a coin-flip direction and identical exit
rules, paying identical spreads and fees. The experiment engine runs it with many seeds; the p-value
is the share of random runs doing at least as well. It isolates whether the *direction* of a signal
carries information.

## Adding a strategy
Subclass `Strategy` (or `TakerHorizonStrategy`), implement callbacks, declare `default_params()` and
`tunable`, register it in `alphalab/strategies/__init__.py`, add a bounded default grid in
`research/loop.py`, and add tests. Never read data from outside `ctx`.

# Research results (2026-09-24)

> **Bottom line: no strategy has demonstrated a repeatable net edge.** Nothing is classified PAPER
> or LIVE-CANDIDATE. This is expected given the data available so far and is the honest starting
> point, not a failure of the system.

## Data actually available
Kalshi's API was not reachable from the build environment, so the only **real** Kalshi data used
was two small third-party samples (imported locally, not redistributed — see DATA.md):

| Source | Markets | Span | Depth | Trades feed | Settlements |
|---|---|---|---|---|---|
| Public-Baseline `example_data.csv` | 2 × KXETH15M | 29 min (2026-03-02) | full depth snapshots (~1.3 s) | none | 2, inferred |
| crypto-bot `sample_features.csv` | 25 × BTC/ETH/SOL/XRP 15M | 82 min (2026-02-25) | top of book, **size assumed** | none | 24, inferred from pinned final quotes |

The 25 crypto markets are 6–7 consecutive 15-minute windows × 4 coins that settled identically
within each window (all four coins moved together), so the effective sample is ~6 independent
episodes. Everything below is therefore **too small to support any conclusion about profitability**.

## Experiment results (walk-forward out-of-sample; 200 random-benchmark trials each)
Net = gross − fees; "mid" = P&L had every fill been at mid. All numbers are simulated.

| Strategy @ data | Status | OOS trades | Mid P&L | Gross | Fees | **Net** | p vs random | Net @ 2× fees |
|---|---|---|---|---|---|---|---|---|
| momentum @ ETH full depth | RESEARCH | 22 | $8.09 | $6.39 | $2.10 | **$4.29** | 0.010 | $2.19 |
| crypto15m fair value @ crypto sample | RESEARCH | 16 | $3.03 | $2.35 | $1.43 | **$0.92** | 0.005 | −$0.51 |
| imbalance @ ETH | REJECT | 26 | $0.41 | −$1.34 | $2.32 | **−$3.66** | 0.34 | −$5.98 |
| market maker @ ETH | REJECT | 7 | −$1.90 | −$3.25 | $0.29 | **−$3.54** | n/a | −$3.83 |
| mean reversion @ ETH | REJECT | 23 | −$2.60 | −$4.14 | $2.13 | **−$6.27** | 0.995 | −$8.40 |
| vol breakout @ ETH | REJECT | 14 | $1.80 | $0.69 | $1.49 | **−$0.80** | 0.005 | −$2.29 |
| vol fade @ ETH | REJECT | 9 | −$1.58 | −$2.25 | $0.95 | **−$3.20** | 0.955 | −$4.15 |
| momentum @ crypto sample | REJECT | 154 | $4.83 | −$8.20 | $21.98 | **−$30.18** | 0.48 | −$52.16 |
| mean reversion @ crypto sample | REJECT | 154 | −$4.82 | −$17.85 | $22.01 | **−$39.86** | 0.98 | −$61.87 |
| vol breakout @ crypto sample | REJECT | 101 | $3.50 | −$5.05 | $14.31 | **−$19.36** | 0.22 | −$33.67 |
| vol fade @ crypto sample | REJECT | 110 | $0.00 | −$9.15 | $15.96 | **−$25.11** | 0.985 | −$41.07 |

Not tested (and not counted as rejected): imbalance and market making on the crypto sample (depth
sizes were not recorded, so queue/imbalance dynamics would be fiction); logical arbitrage (neither
sample contains events with several related markets); crypto fair value on the ETH sample (no strike).

### The two RESEARCH results, read carefully
* **momentum @ ETH** (lookback 5 s, move ≥ 2¢, hold 30 s): 22 OOS trades from one 29-minute window,
  100 % of the positive P&L from one market on one day, walk-forward folds only 50 % profitable,
  deflated probabilistic Sharpe 0.05. Driving the *same parameters* through the paper-trading
  engine over the whole sample (paper risk limits, replay clock) produced **−$1.98 on 32 trades** —
  a live illustration of how fragile a 22-trade result is.
* **crypto 15m fair value** (edge ≥ 8¢, hold to settlement): 16 OOS trades, one day, in-sample
  expectancy $0.57 vs OOS $0.06 (degradation), negative under 2× fees, neighbouring parameters
  unprofitable, settlements inferred and depth assumed.

Both beat the random-direction benchmark (p ≈ 0.01), which is a reason to **collect more data on
these specific hypotheses**, not to trade them.

## Predictive-feature study
No (feature, horizon) pair survived: survival requires the same-sign, |t| > 2 IC in both the
discovery and the later confirmation set **and** an extreme-quintile move larger than the
round-trip cost hurdle (spread + 2 × taker fee ≈ 2.8¢ on the ETH sample, ≈ 4.3¢ on the crypto sample).

* ETH full-depth sample: top-of-book pressure is genuinely predictive at 1–5 s —
  `micro_minus_mid` IC 0.40 (t = 5.3) and `imbalance_1` IC 0.29 (t = 3.7) at 3 s — but the predicted
  moves (≈ 0.5–1¢) are several times smaller than the ≈ 2.8¢ cost of crossing the spread twice and
  paying fees. This is the classic microstructure result: real signal, not tradable as a taker.
* Crypto sample: the strongest relationships (e.g. `vol_60s` at 30–60 s) did not replicate between
  discovery and confirmation and are plausibly an artefact of all four coins falling together in
  the sampled 80 minutes.

## Crypto incremental-information test
Does the spot-implied distance to strike add information beyond the contract's own price?
Out-of-sample on 10 later markets: log-loss 0.700 (price only) vs 0.734 (price + `strike_z`),
t = −1.9 across markets; adding 60 s spot momentum: 0.687 vs 0.746, t = −2.2.
**No demonstrated incremental information** — the Kalshi price already reflected it in this sample.

## Paper-trading status
* Paper engine verified end to end in replay mode on real and synthetic data (journals, state file,
  ingest, dashboard, kill switch, stale-data and disconnect handling are tested).
* **No paper trading on live Kalshi data has been run** (API unreachable from the build
  environment), so no strategy has paper evidence and none can be LIVE-CANDIDATE.

## Pipeline validation on synthetic data (sanity checks, not evidence about Kalshi)
* No planted edge → REJECT (mid P&L ≈ 0; spread + fees make it negative); holdout never touched.
* Strong planted order-book signal smaller than costs → beats the random benchmark but REJECT
  (`dies_after_costs`) — the pipeline does not mistake "predictive" for "profitable".
* Planted spot-leads-quote edge larger than costs → positive OOS net P&L, but held at RESEARCH
  (synthetic data is a gating flag).

## What to do next
1. `alphalab record --series KXBTC15M KXETH15M KXSOL15M KXXRP15M --external` for several weeks
   (full-depth deltas **and** the trade feed, which enables the queue fill model for makers).
2. Also record multi-strike events (e.g. daily index/crypto ladders) to test logical arbitrage.
3. Re-run `alphalab research-loop`; focus first on momentum and the crypto fair-value hypothesis,
   and on maker strategies once trade prints make queue fills measurable.

Full generated report: [docs/research_report_2026-09-24_real_samples.md](docs/research_report_2026-09-24_real_samples.md).

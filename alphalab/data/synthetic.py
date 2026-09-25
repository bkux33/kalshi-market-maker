"""Synthetic Kalshi-like markets for validating the research pipeline.

The generator is NOT a model of real Kalshi behaviour. It exists to answer
one question about our own tooling: *does the pipeline find an edge when one
is planted, and reject everything when there is none?*

Each market is a 15-minute binary "underlying >= strike at close" contract.
The underlying follows a Gaussian random walk; the fair probability is
``Phi((S - K) / (sigma * sqrt(tau)))`` (a martingale), and settlement is the
realised outcome, so the fair price is unbiased by construction.

Quoted books sit around fair value with a 1-3c spread. Trade prints arrive as
a Poisson process. Two optional, independently configurable "leaks":

* ``imbalance_signal``: displayed depth imbalance is correlated with the NEXT
  ``lead_steps`` move of fair value (a planted order-book-imbalance edge).
* ``quote_lag_steps``: the Kalshi quote follows fair value with a lag, so the
  external spot price leads the contract (a planted crypto-15m edge).

With both set to zero the data contains no exploitable information beyond
fair value, and every strategy should fail after costs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List

import numpy as np

from alphalab.core.events import BookSnapshot, ExternalPrice, Settlement, TradeEvent
from alphalab.core.prices import CENT, PRICE_SCALE
from alphalab.data.db import Database
from alphalab.data.ingest import EventSink, derive_tob

_SQRT2 = math.sqrt(2.0)


def _phi(x: np.ndarray) -> np.ndarray:
    from math import erf
    return 0.5 * (1.0 + np.vectorize(erf)(x / _SQRT2))


@dataclass
class SyntheticSpec:
    n_markets: int = 12
    duration_s: int = 900
    step_ms: int = 500
    start_ns: int = 1_767_225_600_000_000_000  # 2026-01-01T00:00:00Z
    gap_between_markets_s: int = 0
    spot0: float = 100.0
    spot_vol_per_sqrt_s: float = 0.02
    base_depth: float = 60.0
    trade_rate_per_s: float = 0.6
    imbalance_signal: float = 0.0     # 0 = none; 0.3 = strong planted imbalance edge
    lead_steps: int = 10
    quote_lag_steps: int = 0          # >0 plants a spot-leads-quote edge
    series: str = "KXSYN15M"
    seed: int = 1


def generate(spec: SyntheticSpec) -> Dict[str, List]:
    rng = np.random.default_rng(spec.seed)
    events: List = []
    markets: List[dict] = []
    settles: List[Settlement] = []
    n_steps = spec.duration_s * 1000 // spec.step_ms
    dt_s = spec.step_ms / 1000.0
    spot = spec.spot0
    t0 = spec.start_ns
    for m in range(spec.n_markets):
        ticker = f"{spec.series}-SYN{m:03d}"
        strike = round(spot, 4)
        steps = rng.normal(0.0, spec.spot_vol_per_sqrt_s * math.sqrt(dt_s), n_steps)
        path = strike + np.cumsum(steps)
        taus = np.maximum((n_steps - np.arange(1, n_steps + 1)) * dt_s, 1e-6)
        z = (path - strike) / (spec.spot_vol_per_sqrt_s * np.sqrt(taus))
        fair = np.clip(_phi(z), 0.001, 0.999)
        fair[-1] = 1.0 if path[-1] >= strike else 0.0
        lag = spec.quote_lag_steps
        quoted = np.concatenate([np.full(lag, fair[0]), fair[:-lag]]) if lag > 0 else fair.copy()
        future_move = np.zeros(n_steps)
        L = spec.lead_steps
        future_move[:-L] = fair[L:] - fair[:-L]
        scale = np.std(future_move) + 1e-12
        close_ns = t0 + spec.duration_s * 1_000_000_000
        for i in range(n_steps - 1):
            ts = t0 + (i + 1) * spec.step_ms * 1_000_000
            q = quoted[i]
            spread_u = int(rng.choice([1, 1, 1, 2])) * CENT  # target quoted width 1c-2c (+ rounding)
            mid_u = q * PRICE_SCALE
            bid = int(max(CENT, min(PRICE_SCALE - 2 * CENT, math.floor((mid_u - spread_u / 2) / CENT) * CENT)))
            ask = int(max(bid + CENT, min(PRICE_SCALE - CENT, math.ceil((mid_u + spread_u / 2) / CENT) * CENT)))
            imb_target = 0.5
            if spec.imbalance_signal:
                imb_target = 0.5 + spec.imbalance_signal * np.tanh(future_move[i] / scale)
            imb_target = float(np.clip(imb_target + rng.normal(0, 0.08), 0.05, 0.95))
            tot = spec.base_depth * rng.uniform(0.6, 1.4)
            bq, aq = max(1.0, round(tot * imb_target)), max(1.0, round(tot * (1 - imb_target)))
            yes = [(bid, bq), (bid - CENT, round(bq * 1.5))] if bid - CENT >= CENT else [(bid, bq)]
            no_best = PRICE_SCALE - ask
            no = [(no_best, aq), (no_best - CENT, round(aq * 1.5))] if no_best - CENT >= CENT else [(no_best, aq)]
            events.append(BookSnapshot(ts, ticker, yes, no))
            events.append(ExternalPrice(ts, f"{spec.series}-SPOT", float(path[i]), "synthetic"))
            if rng.random() < spec.trade_rate_per_s * dt_s:
                # Takers are uninformed in the null; they lean with fair drift otherwise.
                p_buy = 0.5
                if spec.imbalance_signal:
                    p_buy = 0.5 + 0.3 * np.tanh(future_move[i] / scale)
                side = "yes" if rng.random() < p_buy else "no"
                price = ask if side == "yes" else bid
                events.append(TradeEvent(ts + 1, ticker, price, float(rng.integers(1, 20)), side,
                                         trade_id=f"{ticker}-{i}"))
        result = "yes" if fair[-1] >= 0.5 else "no"
        settles.append(Settlement(close_ns, ticker, 1.0 if result == "yes" else 0.0, result, inferred=False))
        markets.append(dict(ticker=ticker, event_ticker=ticker, series_ticker=spec.series,
                            title=f"Synthetic market {m}", status="settled", market_type="binary",
                            open_ts=t0 / 1e9, close_ts=close_ns / 1e9, strike_type="greater_or_equal",
                            floor_strike=strike, result=result, settlement_value=1.0 if result == "yes" else 0.0,
                            depth_quality="synthetic", underlying=f"{spec.series}-SPOT", source="synthetic"))
        spot = float(path[-1])
        t0 = close_ns + spec.gap_between_markets_s * 1_000_000_000
    return {"events": events, "markets": markets, "settlements": settles}


def write_synthetic(db: Database, spec: SyntheticSpec) -> Dict[str, int]:
    data = generate(spec)
    db.begin()
    try:
        sink = EventSink(db, f"synthetic:seed{spec.seed}")
        for ev in data["events"]:
            sink.add(ev)
        for s in data["settlements"]:
            sink.add(s)
        for m in data["markets"]:
            sink.add_market(m)
        n = sink.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    tob = derive_tob(db, [m["ticker"] for m in data["markets"]])
    return {"events": n, "tob_rows": tob, "markets": len(data["markets"])}

"""Strategy family 3: cross-market / logical arbitrage.

Relationships checked (only among markets of the same event):

1. Mutually exclusive outcomes - SELL side (needs exclusivity only):
   at most one YES can pay, so if  sum(best YES bids) > $1 + costs, selling
   one YES in every market (== buying every NO) locks in the excess.
2. Mutually exclusive AND exhaustive - BUY side (``assume_exhaustive``):
   exactly one YES pays, so if  sum(best YES asks) < $1 - costs, buying every
   YES locks in the gap. Kalshi's ``mutually_exclusive`` flag does NOT prove
   exhaustiveness, so this is off by default.
3. Strike ladders ("above K" markets): P(S >= K1) >= P(S >= K2) for K1 < K2.
   If ask(K1) < bid(K2) - costs: buy YES K1, sell YES K2; payoff is >= 0 in
   every state, plus the price gap.
4. Complementary YES/NO in one market cannot be arbitraged on Kalshi's
   unified book (YES ask = 1 - NO bid), so it is only checked as a data
   integrity test (``research.arb_scan``).

Execution is NOT atomic. Legs are IOC orders sent back to back; if a leg fails
the filled legs are unhedged. ``unwind_after_ms`` later any residual imbalance
is flattened with IOC orders at the prevailing price, and that cost is part of
the measured P&L. A candidate is only traded if the net edge per set, after
taker fees on every leg and ``min_edge_c``, is positive.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from alphalab.core.fees import TAKER_RATE, fee_per_contract
from alphalab.core.prices import CENT, PRICE_SCALE
from alphalab.strategies.base import Strategy


def _fee_c(price_units: int, rate: float) -> float:
    # per-contract fee, rounded up to a cent per contract (conservative for small sizes)
    import math
    return math.ceil(fee_per_contract(price_units, rate) * 100 - 1e-9)


class LogicalArbStrategy(Strategy):
    name = "logical_arb"
    family = "cross_market_arbitrage"
    tunable = ("min_edge_c",)

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"min_edge_c": 1.0, "qty": 1, "assume_exhaustive": False, "taker_rate": TAKER_RATE,
                "unwind_after_ms": 2000, "cooldown_s": 5.0, "min_tts_s": 60.0}

    def on_start(self, ctx) -> None:
        groups: Dict[str, List[str]] = defaultdict(list)
        for m in ctx.markets():
            ev = ctx.meta(m).get("event_ticker")
            if ev:
                groups[ev].append(m)
        self.state["groups"] = {e: sorted(ms) for e, ms in groups.items() if len(ms) >= 2}
        self.state["event_of"] = {m: e for e, ms in self.state["groups"].items() for m in ms}
        self.state["cool"] = {}
        self.state["legs"] = {}

    def candidates(self, ctx, event: str) -> List[Dict[str, Any]]:
        p = self.params
        ms = self.state["groups"][event]
        books = {m: ctx.book(m) for m in ms}
        if any(b is None for b in books.values()):
            return []
        out = []
        metas = {m: ctx.meta(m) for m in ms}
        excl = any(bool(metas[m].get("mutually_exclusive")) for m in ms)
        rate = p["taker_rate"]
        if excl:
            bids = {m: books[m].bid_price for m in ms}
            if all(v is not None for v in bids.values()):
                gross = sum(bids.values()) / CENT - 100.0
                fees = sum(_fee_c(v, rate) for v in bids.values())
                net = gross - fees
                if net >= p["min_edge_c"]:
                    out.append({"type": "exclusive_sell", "legs": [(m, "sell", bids[m]) for m in ms],
                                "gross_c": gross, "fees_c": fees, "net_c": net})
            if p["assume_exhaustive"]:
                asks = {m: books[m].ask_price for m in ms}
                if all(v is not None for v in asks.values()):
                    gross = 100.0 - sum(asks.values()) / CENT
                    fees = sum(_fee_c(v, rate) for v in asks.values())
                    net = gross - fees
                    if net >= p["min_edge_c"]:
                        out.append({"type": "exhaustive_buy", "legs": [(m, "buy", asks[m]) for m in ms],
                                    "gross_c": gross, "fees_c": fees, "net_c": net})
        ladder = [(metas[m].get("floor_strike"), m) for m in ms
                  if metas[m].get("floor_strike") is not None
                  and str(metas[m].get("strike_type", "")).startswith("greater")]
        ladder.sort()
        for (k1, m1), (k2, m2) in zip(ladder, ladder[1:]):
            if k1 == k2:
                continue
            a1, b2 = books[m1].ask_price, books[m2].bid_price
            if a1 is None or b2 is None:
                continue
            gross = (b2 - a1) / CENT
            fees = _fee_c(a1, rate) + _fee_c(b2, rate)
            net = gross - fees
            if net >= p["min_edge_c"]:
                out.append({"type": "ladder", "legs": [(m1, "buy", a1), (m2, "sell", b2)],
                            "gross_c": gross, "fees_c": fees, "net_c": net})
        return out

    def on_book(self, ctx, market: str) -> None:
        ev = self.state.get("event_of", {}).get(market)
        if ev is None:
            return
        if ctx.now_ns < self.state["cool"].get(ev, 0):
            return
        tts = ctx.features(market).get("tts_s")
        if tts is not None and tts < self.params["min_tts_s"]:
            return
        for c in self.candidates(ctx, ev):
            ctx.signal(market, f"arb_{c['type']}", c["net_c"], gross_c=c["gross_c"], fees_c=c["fees_c"])
            for m, side, price in c["legs"]:
                ctx.place(m, side, price, self.params["qty"], tif="ioc", tag=f"arb:{c['type']}")
            self.state["cool"][ev] = ctx.now_ns + int(self.params["cooldown_s"] * 1e9)
            ctx.schedule(self.params["unwind_after_ms"], f"check:{ev}")
            break

    def on_timer(self, ctx, key: str) -> None:
        kind, ev = key.split(":", 1)
        if kind != "check":
            return
        ms = self.state["groups"].get(ev, [])
        pos = {m: ctx.position(m) for m in ms}
        held = [abs(q) for q in pos.values() if abs(q) > 1e-9]
        if not held:
            return
        target = min(held) if len(held) == len(ms) else 0.0
        # flatten any leg exposure above the fully hedged amount
        for m, q in pos.items():
            excess = abs(q) - target
            if excess <= 1e-9:
                continue
            b = ctx.book(m)
            if b is None:
                continue
            if q > 0 and b.bid_price is not None:
                ctx.place(m, "sell", b.bid_price, excess, tif="ioc", tag="arb:unwind")
            elif q < 0 and b.ask_price is not None:
                ctx.place(m, "buy", b.ask_price, excess, tif="ioc", tag="arb:unwind")

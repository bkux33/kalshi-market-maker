"""Strategy family 2: inventory-aware market making (Avellaneda-Stoikov style).

Quoting model (prices in cents, inventory q in contracts)::

    tau   = min(time_to_settlement, horizon_s) / horizon_s        in (0, 1]
    sigma = max(realised mid variation over 60s, sigma_floor_c)
    r     = reference - q * gamma * sigma^2 * tau + imb_skew_c * (2*imbalance - 1)
    half  = 0.5 * gamma * sigma^2 * tau + (1/gamma) * ln(1 + gamma/k)
    half  = max(half, fee_per_contract + min_edge_c)
    bid   = floor(r - half),  ask = ceil(r + half)

The reservation-price / optimal-spread formulas follow ``src/quotes.py`` of
zachdaube/kalshi-market-maker (Apache-2.0), extended with a time-to-settlement
term, a fee floor, an optional imbalance skew and hard inventory limits.

Quotes are post-only, so every fill is a maker fill. The experiments measure
the part a naive spread-capture calculation ignores: adverse selection
(markouts after fills), inventory risk into settlement, and maker fees.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from alphalab.core.fees import fee_per_contract
from alphalab.core.prices import CENT, MAX_PRICE, MIN_PRICE
from alphalab.strategies.base import Strategy


class MarketMakerStrategy(Strategy):
    name = "market_maker"
    family = "market_making"
    tunable = ("gamma", "k", "min_edge_c", "max_inventory")

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"gamma": 0.1, "k": 1.5, "sigma_floor_c": 0.5, "horizon_s": 900.0, "min_edge_c": 0.25,
                "maker_fee_rate": 0.0175, "qty": 5, "max_inventory": 20, "min_tts_s": 180.0,
                "imb_skew_c": 0.0, "use_microprice": False, "requote_c": 1.0, "min_price_c": 5.0,
                "max_price_c": 95.0, "max_spread_to_quote_c": 20.0}

    def quotes(self, f: Dict[str, Any], q: float) -> Optional[Dict[str, Any]]:
        p = self.params
        ref = f.get("microprice") if p["use_microprice"] else f.get("mid")
        if ref is None or f.get("spread") is None:
            return None
        tts = f.get("tts_s")
        tau = 1.0 if tts is None else max(0.0, min(tts, p["horizon_s"])) / p["horizon_s"]
        sigma = max(f.get("vol_60s") or 0.0, p["sigma_floor_c"])
        g = p["gamma"]
        r = ref - q * g * sigma * sigma * tau
        imb = f.get("imbalance")
        if imb is not None and p["imb_skew_c"]:
            r += p["imb_skew_c"] * (2 * imb - 1)
        half = 0.5 * g * sigma * sigma * tau + (1.0 / g) * math.log(1.0 + g / p["k"])
        fee_c = fee_per_contract(int(round(ref * CENT)), p["maker_fee_rate"]) * 100.0
        half = max(half, fee_c + p["min_edge_c"])
        bid_c = math.floor(r - half)
        ask_c = math.ceil(r + half)
        if ask_c - bid_c < 1:
            return None
        return {"bid_c": bid_c, "ask_c": ask_c, "reservation": r, "half": half, "sigma": sigma, "tau": tau}

    def on_book(self, ctx, market: str) -> None:
        p = self.params
        f = ctx.features(market)
        q = ctx.position(market)
        tts = f.get("tts_s")
        mid = f.get("mid")
        stop = (mid is None or f.get("spread") is None or f["spread"] > p["max_spread_to_quote_c"]
                or (tts is not None and tts < p["min_tts_s"])
                or mid < p["min_price_c"] or mid > p["max_price_c"] or ctx.is_halted())
        if stop:
            for o in ctx.open_orders(market):
                if o.cancel_requested_ns is None:
                    ctx.cancel(o.order_id)
            return
        qt = self.quotes(f, q)
        if qt is None:
            return
        want: Dict[str, Optional[int]] = {"buy": None, "sell": None}
        if q < p["max_inventory"]:
            want["buy"] = int(min(qt["bid_c"], f["ask"] - 1) * CENT)  # never cross: post-only
        if q > -p["max_inventory"]:
            want["sell"] = int(max(qt["ask_c"], f["bid"] + 1) * CENT)
        for side in ("buy", "sell"):
            price = want[side]
            if price is not None and not (MIN_PRICE <= price <= MAX_PRICE):
                price = None
            side_orders = [o for o in ctx.open_orders(market) if o.req.action == side]
            live = [o for o in side_orders if o.cancel_requested_ns is None]
            keep = None
            for o in live:
                if price is not None and keep is None and abs(o.req.price - price) < p["requote_c"] * CENT:
                    keep = o
                else:
                    ctx.cancel(o.order_id)
            # cancel-then-replace: wait until the old order is gone before quoting this side again
            if keep is None and price is not None and len(side_orders) == 0:
                room = p["max_inventory"] - q if side == "buy" else p["max_inventory"] + q
                qty = min(p["qty"], max(0.0, room))
                if qty >= 1:
                    ctx.place(market, side, price, qty, tif="gtc", post_only=True, tag="quote")
        ctx.signal(market, "mm_reservation", qt["reservation"], half=qt["half"], inventory=q)

    def on_settlement(self, ctx, market: str, value: float) -> None:
        self.state.pop(market, None)

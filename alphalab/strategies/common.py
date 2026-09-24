"""Shared machinery for taker strategies with a fixed holding horizon.

Entry: an IOC order at the current best opposite price (plus an optional
``entry_slip_c`` allowance), so entries pay the spread and fees - no midpoint
fantasy fills. Exit: after ``horizon_s`` an IOC at the best opposite price;
unfilled remainder is retried every second up to ``exit_retries`` times, then
the position is held to settlement (``exit_mode="settle"`` holds from the
start). All costs are therefore realised in the P&L.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from alphalab.core.prices import CENT, MAX_PRICE, MIN_PRICE
from alphalab.strategies.base import Strategy

NS = 1_000_000_000


class TakerHorizonStrategy(Strategy):
    family = "taker"

    @classmethod
    def common_params(cls) -> Dict[str, Any]:
        return {"qty": 5, "horizon_s": 10.0, "max_spread_c": 3.0, "min_tts_s": 120.0, "max_tts_s": 1e12,
                "cooldown_s": 5.0, "exit_mode": "horizon", "entry_slip_c": 0.0, "exit_retries": 5,
                "min_price_c": 5.0, "max_price_c": 95.0}

    # subclasses return (+1 buy YES | -1 sell YES | 0, signal value)
    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        raise NotImplementedError

    def signal_name(self) -> str:
        return self.name

    def _st(self, market: str) -> Dict[str, Any]:
        return self.state.setdefault(market, {"cool_until": 0, "exit_scheduled": False, "retries": 0,
                                              "pending": False})

    def on_book(self, ctx, market: str) -> None:
        p = self.params
        st = self._st(market)
        if st["pending"] or ctx.open_orders(market):
            return
        if abs(ctx.position(market)) > 1e-9:
            return
        if ctx.now_ns < st["cool_until"]:
            return
        f = ctx.features(market)
        spread, bid, ask = f.get("spread"), f.get("bid"), f.get("ask")
        if spread is None or bid is None or ask is None or spread > p["max_spread_c"]:
            return
        tts = f.get("tts_s")
        if tts is not None and (tts < p["min_tts_s"] or tts > p["max_tts_s"]):
            return
        mid = f.get("mid")
        if mid is None or mid < p["min_price_c"] or mid > p["max_price_c"]:
            return
        d, val = self.decide(f)
        if not d:
            return
        ctx.signal(market, self.signal_name(), val if val is not None else float(d), direction=d)
        if d > 0:
            price = min(MAX_PRICE, int(round((ask + p["entry_slip_c"]) * CENT)))
            oid = ctx.place(market, "buy", price, p["qty"], tif="ioc", tag="entry")
        else:
            price = max(MIN_PRICE, int(round((bid - p["entry_slip_c"]) * CENT)))
            oid = ctx.place(market, "sell", price, p["qty"], tif="ioc", tag="entry")
        st["cool_until"] = ctx.now_ns + int(p["cooldown_s"] * NS)
        if oid:
            st["pending"] = True
            ctx.schedule(1000, f"entry_done:{market}")

    def on_fill(self, ctx, fill) -> None:
        st = self._st(fill.market)
        if fill.tag == "entry" and not st["exit_scheduled"] and self.params["exit_mode"] == "horizon":
            st["exit_scheduled"] = True
            st["retries"] = 0
            ctx.schedule(self.params["horizon_s"] * 1000, f"exit:{fill.market}")

    def on_timer(self, ctx, key: str) -> None:
        kind, market = key.split(":", 1)
        st = self._st(market)
        if kind == "entry_done":
            st["pending"] = False
            return
        if kind != "exit":
            return
        pos = ctx.position(market)
        if abs(pos) < 1e-9:
            st["exit_scheduled"] = False
            return
        book = ctx.book(market)
        if book is not None:
            if pos > 0 and book.bid_price is not None:
                ctx.place(market, "sell", book.bid_price, abs(pos), tif="ioc", tag="exit")
            elif pos < 0 and book.ask_price is not None:
                ctx.place(market, "buy", book.ask_price, abs(pos), tif="ioc", tag="exit")
        st["retries"] += 1
        if st["retries"] <= self.params["exit_retries"]:
            ctx.schedule(1000, f"exit:{market}")
        else:
            st["exit_scheduled"] = False  # give up: hold to settlement

    def on_settlement(self, ctx, market: str, value: float) -> None:
        self.state.pop(market, None)

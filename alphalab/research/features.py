"""Streaming feature engine.

One implementation is used everywhere (strategies in backtest/paper/live and
research datasets), so research features and traded features cannot drift.

All prices in features are in *cents* (floats) for readability; times in
seconds. Features are ``None`` when not computable (e.g. one-sided book).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from alphalab.core.events import ExternalPrice, TradeEvent
from alphalab.core.orderbook import OrderBook
from alphalab.core.prices import CENT

NS = 1_000_000_000
_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


class _Series:
    """Time series of (ts_ns, value) with as-of lookup and trimming."""

    __slots__ = ("ts", "vals", "keep_ns")

    def __init__(self, keep_s: float):
        self.ts: List[int] = []
        self.vals: List[float] = []
        self.keep_ns = int(keep_s * NS)

    def add(self, ts: int, v: float) -> None:
        if self.ts and ts < self.ts[-1]:
            ts = self.ts[-1]
        self.ts.append(ts)
        self.vals.append(v)
        if len(self.ts) > 64 and self.ts[0] < ts - 2 * self.keep_ns:
            cut = bisect_right(self.ts, ts - self.keep_ns)
            del self.ts[:cut]
            del self.vals[:cut]

    def asof(self, ts: int) -> Optional[float]:
        i = bisect_right(self.ts, ts) - 1
        return self.vals[i] if i >= 0 else None

    def last(self) -> Optional[float]:
        return self.vals[-1] if self.vals else None

    def window(self, start_ts: int, end_ts: int) -> List[float]:
        i = bisect_right(self.ts, start_ts)
        j = bisect_right(self.ts, end_ts)
        prev = [self.vals[i - 1]] if i > 0 else []
        return prev + self.vals[i:j]


class _RollingVar:
    """Rolling sum of squared log returns over a time window (O(1) per update)."""

    __slots__ = ("window_ns", "q", "sum", "last")

    def __init__(self, window_s: float):
        self.window_ns = int(window_s * NS)
        self.q: Deque[Tuple[int, float]] = deque()
        self.sum = 0.0
        self.last: Optional[float] = None

    def add(self, ts: int, price: float) -> None:
        if self.last is not None and self.last > 0 and price > 0:
            lr2 = math.log(price / self.last) ** 2
            self.q.append((ts, lr2))
            self.sum += lr2
        self.last = price
        lo = ts - self.window_ns
        while self.q and self.q[0][0] < lo:
            self.sum -= self.q.popleft()[1]

    def var_per_s(self, now_ns: int) -> Optional[float]:
        if len(self.q) < 10:
            return None
        span = max(1.0, (now_ns - self.q[0][0]) / NS)
        return max(self.sum, 0.0) / span


class MarketFeatures:
    def __init__(self, keep_s: float):
        self.mid = _Series(keep_s)
        self.trades: Deque[Tuple[int, float, int]] = deque()  # (ts, qty, +1 buy YES / -1 sell)
        self.first_seen_ns: Optional[int] = None
        self.book_stats: Dict[str, Any] = {}


class FeatureEngine:
    def __init__(self, windows_s: Iterable[float] = (1, 3, 5, 10, 30, 60), depth_levels: int = 5,
                 market_meta: Optional[Dict[str, Dict[str, Any]]] = None,
                 spot_vol_window_s: float = 300.0):
        self.windows_s = tuple(windows_s)
        self.depth_levels = depth_levels
        self.keep_s = max(max(self.windows_s), 60.0) * 2 + 1
        self.meta = market_meta or {}
        self.m: Dict[str, MarketFeatures] = {}
        self.spot: Dict[str, _Series] = {}
        self.spot_var: Dict[str, _RollingVar] = {}
        self.spot_vol_window_s = spot_vol_window_s

    def _mf(self, market: str) -> MarketFeatures:
        mf = self.m.get(market)
        if mf is None:
            mf = self.m[market] = MarketFeatures(self.keep_s)
        return mf

    # ------------------------------------------------------------------ updates
    def on_book(self, market: str, ob: OrderBook, ts_ns: int, synced: bool = True) -> None:
        mf = self._mf(market)
        if mf.first_seen_ns is None:
            mf.first_seen_ns = ts_ns
        if not synced:
            mf.book_stats = {}
            return
        mid = ob.mid()
        bb, ba = ob.best_bid(), ob.best_ask()
        bd, ad = ob.depth("bid", self.depth_levels), ob.depth("ask", self.depth_levels)
        micro = ob.microprice()
        mf.book_stats = {
            "bid": None if bb is None else bb[0] / CENT,
            "ask": None if ba is None else ba[0] / CENT,
            "bid_qty": None if bb is None else bb[1],
            "ask_qty": None if ba is None else ba[1],
            "bid_depth": bd,
            "ask_depth": ad,
            "imbalance_1": ob.imbalance(1),
            "imbalance": (bd / (bd + ad)) if bd + ad > 0 else None,
            "mid": None if mid is None else mid / CENT,
            "spread": None if ob.spread() is None else ob.spread() / CENT,
            "microprice": None if micro is None else micro / CENT,
        }
        if mid is not None:
            if mf.mid.last() != mid / CENT:
                mf.mid.add(ts_ns, mid / CENT)

    def on_trade(self, ev: TradeEvent) -> None:
        mf = self._mf(ev.market)
        mf.trades.append((ev.ts_ns, ev.qty, 1 if ev.taker_side == "yes" else -1))
        horizon = ev.ts_ns - int(self.keep_s * NS)
        while mf.trades and mf.trades[0][0] < horizon:
            mf.trades.popleft()

    def on_external(self, ev: ExternalPrice) -> None:
        s = self.spot.get(ev.symbol)
        if s is None:
            s = self.spot[ev.symbol] = _Series(self.spot_vol_window_s * 2 + 10)
            self.spot_var[ev.symbol] = _RollingVar(self.spot_vol_window_s)
        s.add(ev.ts_ns, ev.price)
        self.spot_var[ev.symbol].add(ev.ts_ns, ev.price)

    # ------------------------------------------------------------------ features
    def features(self, market: str, ts_ns: int) -> Dict[str, Any]:
        mf = self.m.get(market)
        f: Dict[str, Any] = {"ts_ns": ts_ns, "market": market}
        if mf is None:
            return f
        f.update(mf.book_stats)
        mid = f.get("mid")
        spread = f.get("spread")
        f["spread_pct"] = (spread / mid) if (spread is not None and mid) else None
        micro = f.get("microprice")
        f["micro_minus_mid"] = (micro - mid) if (micro is not None and mid is not None) else None
        imb = f.get("imbalance")
        f["imbalance_signed"] = None if imb is None else 2 * imb - 1
        for w in self.windows_s:
            past = mf.mid.asof(ts_ns - int(w * NS))
            f[f"ret_{w:g}s"] = (mid - past) if (mid is not None and past is not None) else None
        for w in (10, 30, 60):
            vals = mf.mid.window(ts_ns - int(w * NS), ts_ns)
            if len(vals) >= 2:
                diffs = [b - a for a, b in zip(vals, vals[1:])]
                f[f"vol_{w}s"] = math.sqrt(sum(d * d for d in diffs))
            else:
                f[f"vol_{w}s"] = 0.0 if vals else None
        r5, r30 = f.get("ret_5s"), f.get("ret_30s")
        f["accel"] = (r5 - r30 * 5 / 30) if (r5 is not None and r30 is not None) else None
        for w in (10, 60):
            lo = ts_ns - int(w * NS)
            n = vol = signed = 0.0
            for t, q, s in reversed(mf.trades):
                if t < lo:
                    break
                if t > ts_ns:
                    continue
                n += 1
                vol += q
                signed += q * s
            f[f"trade_count_{w}s"] = n
            f[f"trade_intensity_{w}s"] = n / w
            f[f"trade_imbalance_{w}s"] = (signed / vol) if vol > 0 else 0.0
        meta = self.meta.get(market) or {}
        close_ts, open_ts = meta.get("close_ts"), meta.get("open_ts")
        now_s = ts_ns / NS
        f["tts_s"] = (close_ts - now_s) if close_ts else None
        start = open_ts if open_ts else (mf.first_seen_ns / NS if mf.first_seen_ns else None)
        f["age_s"] = (now_s - start) if start else None
        und, strike = meta.get("underlying"), meta.get("floor_strike")
        f["spot"] = f["dist_to_strike_pct"] = f["strike_z"] = f["fair_prob"] = None
        if und and und in self.spot:
            s = self.spot[und]
            spot = s.asof(ts_ns)
            f["spot"] = spot
            for w in (10, 60):
                past = s.asof(ts_ns - int(w * NS))
                f[f"spot_ret_{w}s"] = (math.log(spot / past) if (spot and past) else None)
            if spot and strike:
                f["dist_to_strike_pct"] = (spot - strike) / strike * 100.0
                var_per_s = self.spot_var[und].var_per_s(ts_ns)
                f["spot_vol_per_s"] = None if var_per_s is None else math.sqrt(var_per_s)
                if var_per_s is not None and f["tts_s"] is not None and f["tts_s"] > 0:
                    sig = math.sqrt(max(var_per_s, 1e-14) * f["tts_s"])
                    z = math.log(spot / strike) / sig
                    f["strike_z"] = z
                    f["fair_prob"] = norm_cdf(z) * 100.0  # cents
        return f

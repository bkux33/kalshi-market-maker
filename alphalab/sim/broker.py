"""Simulated exchange: order lifecycle and an explicit, configurable fill model.

This broker is shared by the backtester and the paper trader so both use the
same assumptions. Every assumption is a ``FillConfig`` field:

Latency
    ``order_latency_ms``: an order decided at t becomes live at t + latency and
    is matched against the book *as it is then*, not as it was at decision time.
    ``cancel_latency_ms``: a cancel requested at t takes effect at t + latency;
    the order can still be filled in between.

Taker (marketable) orders
    Walk the visible opposite side up to the limit price, level by level, so
    large orders get partial fills and worse prices. Liquidity we consume is
    remembered in an *impact overlay* (``impact_ttl_ms``) so the same displayed
    size cannot be filled twice while the recorded book (which never saw our
    order) still shows it. ``taker_slippage_ticks`` adds extra adverse ticks.
    IOC remainders are cancelled; GTC remainders rest.

Maker (resting) orders - ``maker_fill_mode``
    ``queue``: we join the back of the queue at our price. Queue ahead of us
    shrinks when (a) public trades print at our price (trade size consumes the
    queue first; only the excess fills us) and (b) displayed size at our level
    decreases for reasons other than trades, attributed by
    ``queue_cancel_model`` (``pro_rata`` default, ``back`` = cancels are all
    behind us (conservative), ``front`` = all ahead (optimistic)). A print
    *through* our price fills us (``trade_through_fills_all``). If the opposite
    side of the recorded book moves to our price (touch, ``allow_touch_fill``)
    or through it, a counterparty was willing to trade at our price, so we fill
    up to the size they showed - these are exactly the adverse-selection fills.
    ``cross_only``: ignore trade prints; fill only on the opposite side
    touching/crossing our price (for data without a trade feed).

Never: fills at prices better than our limit, fills before the order is live,
fills after a market's close, or fills larger than the visible counterparty.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from alphalab.core.book_manager import BookManager
from alphalab.core.config import FillConfig
from alphalab.core.events import BookDelta, BookSnapshot, Settlement, TradeEvent
from alphalab.core.fees import FeeModel
from alphalab.core.orderbook import OrderBook
from alphalab.core.prices import CENT, MAX_PRICE, MIN_PRICE, PRICE_SCALE, complement
from alphalab.sim.portfolio import Portfolio

log = logging.getLogger(__name__)
_EPS = 1e-9
NS_PER_MS = 1_000_000


@dataclass
class OrderRequest:
    strategy: str
    market: str
    action: str                 # "buy" | "sell" (YES)
    price: int                  # limit, YES price units
    qty: float
    tif: str = "gtc"            # "gtc" | "ioc"
    post_only: bool = False
    tag: str = ""
    client_order_id: Optional[str] = None


@dataclass
class SimOrder:
    order_id: str
    req: OrderRequest
    ts_decision: int
    ts_active: int
    status: str = "pending"     # pending | resting | filled | canceled | rejected
    remaining: float = 0.0
    filled: float = 0.0
    fill_notional: float = 0.0
    queue_ahead: float = 0.0
    cancel_requested_ns: Optional[int] = None
    ts_done: Optional[int] = None
    reason: str = ""

    @property
    def market(self) -> str:
        return self.req.market

    @property
    def is_open(self) -> bool:
        return self.status in ("pending", "resting")

    @property
    def avg_fill_price(self) -> Optional[float]:
        return self.fill_notional / self.filled if self.filled > _EPS else None


@dataclass
class Fill:
    fill_id: str
    order_id: str
    strategy: str
    market: str
    ts_ns: int
    action: str
    price: int
    qty: float
    liquidity: str              # maker | taker
    fee: float
    mid_at_fill: Optional[float]
    tag: str = ""
    position_after: float = 0.0


@dataclass
class _Timer:
    ts: int
    seq: int
    kind: str
    ref: str

    def __lt__(self, other: "_Timer") -> bool:
        return (self.ts, self.seq) < (other.ts, other.seq)


class SimBroker:
    def __init__(self, books: BookManager, fees: FeeModel, cfg: FillConfig, portfolio: Portfolio,
                 market_close_ns: Optional[Dict[str, int]] = None,
                 on_fill: Optional[Callable[[Fill], None]] = None,
                 on_order: Optional[Callable[[SimOrder], None]] = None,
                 impact_ttl_ms: int = 5000):
        self.books = books
        self.fees = fees
        self.cfg = cfg
        self.portfolio = portfolio
        self.market_close_ns = dict(market_close_ns or {})
        self.on_fill = on_fill
        self.on_order = on_order
        self.impact_ttl_ns = impact_ttl_ms * NS_PER_MS
        self.orders: Dict[str, SimOrder] = {}
        self.fills: List[Fill] = []
        self._open_by_market: Dict[str, Dict[str, SimOrder]] = {}
        self._timers: List[_Timer] = []
        self._seq = itertools.count()
        self._oid = itertools.count(1)
        self._fid = itertools.count(1)
        # impact overlay: market -> {(side, price): (qty, expiry_ns)}
        self._overlay: Dict[str, Dict[Tuple[str, int], Tuple[float, int]]] = {}
        # trade depletion awaiting the matching book decrease: market -> {(side, price): qty}
        self._trade_depletion: Dict[str, Dict[Tuple[str, int], float]] = {}
        self._level_before: Dict[str, float] = {}
        self.closed_markets: set = set()
        self.stats = {"submitted": 0, "rejected": 0, "canceled": 0, "maker_fills": 0, "taker_fills": 0,
                      "submitted_qty": 0.0, "filled_qty": 0.0}

    # ------------------------------------------------------------------ helpers
    def _push(self, ts: int, kind: str, ref: str) -> None:
        heapq.heappush(self._timers, _Timer(ts, next(self._seq), kind, ref))

    def next_timer_ns(self) -> Optional[int]:
        return self._timers[0].ts if self._timers else None

    def open_orders(self, market: Optional[str] = None, strategy: Optional[str] = None) -> List[SimOrder]:
        if market is not None:
            orders = list(self._open_by_market.get(market, {}).values())
        else:
            orders = [o for d in self._open_by_market.values() for o in d.values()]
        if strategy is not None:
            orders = [o for o in orders if o.req.strategy == strategy]
        return orders

    def _finish(self, o: SimOrder, status: str, ts: int, reason: str = "") -> None:
        o.status = status
        o.ts_done = ts
        if reason:
            o.reason = reason
        self._open_by_market.get(o.market, {}).pop(o.order_id, None)
        if status == "canceled":
            self.stats["canceled"] += 1
        if self.on_order:
            self.on_order(o)

    def _level_side(self, action: str) -> str:
        return "bid" if action == "buy" else "ask"

    def _visible(self, market: str, side: str, price: int, now: int, ob: OrderBook) -> float:
        qty = ob.qty_at(side, price)
        ov = self._overlay.get(market, {}).get((side, price))
        if ov and ov[1] > now:
            qty -= ov[0]
        return max(0.0, qty)

    def _consume(self, market: str, side: str, price: int, qty: float, now: int) -> None:
        d = self._overlay.setdefault(market, {})
        cur = d.get((side, price))
        base = cur[0] if cur and cur[1] > now else 0.0
        d[(side, price)] = (base + qty, now + self.impact_ttl_ns)

    def _consumed_map(self, market: str, now: int) -> Dict[Tuple[str, int], float]:
        return {k: v[0] for k, v in self._overlay.get(market, {}).items() if v[1] > now}

    # ------------------------------------------------------------------ order entry
    def submit(self, req: OrderRequest, now_ns: int) -> SimOrder:
        oid = f"o{next(self._oid)}"
        o = SimOrder(oid, req, now_ns, now_ns + self.cfg.order_latency_ms * NS_PER_MS, remaining=req.qty)
        self.orders[oid] = o
        self.stats["submitted"] += 1
        self.stats["submitted_qty"] += req.qty
        reason = self._validate(req, now_ns)
        if reason:
            self.stats["rejected"] += 1
            self._finish(o, "rejected", now_ns, reason)
            return o
        self._open_by_market.setdefault(req.market, {})[oid] = o
        self._push(o.ts_active, "activate", oid)
        if self.on_order:
            self.on_order(o)
        return o

    def _validate(self, req: OrderRequest, now_ns: int) -> str:
        if req.action not in ("buy", "sell"):
            return "bad_action"
        if req.qty <= 0:
            return "bad_qty"
        if not (MIN_PRICE <= req.price <= MAX_PRICE):
            return "price_out_of_range"
        if req.market in self.closed_markets:
            return "market_closed"
        close = self.market_close_ns.get(req.market)
        if close is not None and now_ns >= close:
            return "market_closed"
        return ""

    def cancel(self, order_id: str, now_ns: int) -> bool:
        o = self.orders.get(order_id)
        if o is None or not o.is_open or o.cancel_requested_ns is not None:
            return False
        o.cancel_requested_ns = now_ns
        self._push(now_ns + self.cfg.cancel_latency_ms * NS_PER_MS, "cancel", order_id)
        return True

    def cancel_all(self, now_ns: int, market: Optional[str] = None, strategy: Optional[str] = None) -> int:
        n = 0
        for o in self.open_orders(market, strategy):
            n += bool(self.cancel(o.order_id, now_ns))
        return n

    # ------------------------------------------------------------------ timers
    def process_timers(self, up_to_ns: int) -> None:
        while self._timers and self._timers[0].ts <= up_to_ns:
            t = heapq.heappop(self._timers)
            o = self.orders.get(t.ref)
            if o is None or not o.is_open:
                continue
            if t.kind == "activate":
                self._activate(o, t.ts)
            elif t.kind == "cancel":
                self._finish(o, "canceled", t.ts, "user_cancel")

    def _activate(self, o: SimOrder, now: int) -> None:
        close = self.market_close_ns.get(o.market)
        if o.market in self.closed_markets or (close is not None and now >= close):
            self._finish(o, "canceled", now, "market_closed")
            return
        if not self.books.is_synced(o.market):
            self._finish(o, "rejected", now, "book_unsynced")
            self.stats["rejected"] += 1
            return
        ob = self.books.book(o.market)
        req = o.req
        marketable = (req.action == "buy" and ob.ask_price is not None and req.price >= ob.ask_price) or \
                     (req.action == "sell" and ob.bid_price is not None and req.price <= ob.bid_price)
        # Self-trade prevention: do not cross our own resting orders.
        for other in self.open_orders(o.market):
            if other is o or other.status != "resting":
                continue
            if (req.action == "buy" and other.req.action == "sell" and req.price >= other.req.price) or \
               (req.action == "sell" and other.req.action == "buy" and req.price <= other.req.price):
                self.stats["rejected"] += 1
                self._finish(o, "rejected", now, "self_cross")
                return
        if marketable:
            if req.post_only:
                self.stats["rejected"] += 1
                self._finish(o, "rejected", now, "post_only_would_cross")
                return
            self._take(o, ob, now)
            if o.remaining <= _EPS:
                self._finish(o, "filled", now)
                return
        if req.tif == "ioc":
            self._finish(o, "canceled", now, "ioc_remainder" if o.filled > 0 else "ioc_no_fill")
            return
        o.status = "resting"
        side = self._level_side(req.action)
        o.queue_ahead = self._visible(o.market, side, req.price, now, ob)
        if self.on_order:
            self.on_order(o)

    def _take(self, o: SimOrder, ob: OrderBook, now: int) -> None:
        req = o.req
        levels = ob.sweep(req.action, o.remaining, limit=req.price,
                          consumed=self._consumed_map(o.market, now))
        side = "ask" if req.action == "buy" else "bid"
        for price, qty in levels:
            self._consume(o.market, side, price, qty, now)
            slip = self.cfg.taker_slippage_ticks * CENT
            px = price + slip if req.action == "buy" else price - slip
            px = min(MAX_PRICE, max(MIN_PRICE, px))
            self._fill(o, px, qty, "taker", now)

    # ------------------------------------------------------------------ fills
    def _fill(self, o: SimOrder, price: int, qty: float, liquidity: str, now: int) -> None:
        qty = min(qty, o.remaining)
        if qty <= _EPS:
            return
        req = o.req
        fee = self.fees.fee(req.market, price, qty, is_taker=(liquidity == "taker"))
        o.remaining -= qty
        o.filled += qty
        o.fill_notional += qty * price / PRICE_SCALE
        self.portfolio.on_fill(req.strategy, req.market, req.action, price, qty, fee, now)
        ob = self.books.books.get(req.market)
        mid = ob.mid() if ob else None
        f = Fill(f"f{next(self._fid)}", o.order_id, req.strategy, req.market, now, req.action, price, qty,
                 liquidity, fee, mid, req.tag, self.portfolio.pos(req.strategy, req.market).qty)
        self.fills.append(f)
        self.stats["maker_fills" if liquidity == "maker" else "taker_fills"] += 1
        self.stats["filled_qty"] += qty
        if o.remaining <= _EPS and o.status == "resting":
            self._finish(o, "filled", now)
        if self.on_fill:
            self.on_fill(f)

    # ------------------------------------------------------------------ market events
    def before_book(self, ev) -> None:
        self._level_before.clear()
        orders = self._open_by_market.get(ev.market)
        if not orders:
            return
        ob = self.books.book(ev.market)
        for o in orders.values():
            if o.status == "resting":
                self._level_before[o.order_id] = ob.qty_at(self._level_side(o.req.action), o.req.price)

    def after_book(self, ev) -> None:
        market = ev.market
        if isinstance(ev, BookSnapshot):
            self._trade_depletion.pop(market, None)
        orders = [o for o in self._open_by_market.get(market, {}).values() if o.status == "resting"]
        if not orders:
            return
        now = ev.ts_ns
        if not self.books.is_synced(market):
            return
        ob = self.books.book(market)
        depl = self._trade_depletion.setdefault(market, {})
        for o in orders:
            side = self._level_side(o.req.action)
            before = self._level_before.get(o.order_id)
            after = ob.qty_at(side, o.req.price)
            if before is not None and after < before - _EPS:
                decrease = before - after
                explained = min(decrease, depl.get((side, o.req.price), 0.0))
                if explained:
                    depl[(side, o.req.price)] -= explained
                unexplained = decrease - explained
                if unexplained > _EPS and o.queue_ahead > _EPS:
                    model = self.cfg.queue_cancel_model
                    if model == "front":
                        o.queue_ahead -= unexplained
                    elif model == "pro_rata":
                        o.queue_ahead -= unexplained * (o.queue_ahead / before)
            o.queue_ahead = max(0.0, min(o.queue_ahead, after))
        self._cross_fills(market, ob, now)

    def _cross_fills(self, market: str, ob: OrderBook, now: int) -> None:
        """Fill resting orders when the recorded opposite side reaches our price."""
        for o in sorted(self.open_orders(market), key=lambda x: (x.ts_active, x.order_id)):
            if o.status != "resting" or o.remaining <= _EPS:
                continue
            p = o.req.price
            if o.req.action == "buy":
                levels = [(ap, q) for ap, q in ob.asks() if ap < p or (ap == p and self.cfg.allow_touch_fill)]
                opp_side = "ask"
            else:
                levels = [(bp, q) for bp, q in ob.bids() if bp > p or (bp == p and self.cfg.allow_touch_fill)]
                opp_side = "bid"
            for lp, _ in levels:
                avail = self._visible(market, opp_side, lp, now, ob)
                take = min(avail, o.remaining)
                if take <= _EPS:
                    continue
                self._consume(market, opp_side, lp, take, now)
                self._fill(o, p, take, "maker", now)
                if o.remaining <= _EPS:
                    break

    def on_trade(self, ev: TradeEvent) -> None:
        market = ev.market
        # taker bought YES -> consumed asks; taker bought NO -> consumed YES bids
        hit_side = "ask" if ev.taker_side == "yes" else "bid"
        depl = self._trade_depletion.setdefault(market, {})
        depl[(hit_side, ev.price)] = depl.get((hit_side, ev.price), 0.0) + ev.qty
        if self.cfg.maker_fill_mode != "queue":
            return
        our_action = "sell" if hit_side == "ask" else "buy"
        cands = [o for o in self.open_orders(market) if o.status == "resting" and o.req.action == our_action
                 and ((our_action == "buy" and o.req.price >= ev.price) or
                      (our_action == "sell" and o.req.price <= ev.price))]
        if not cands:
            return
        # Better-priced orders would have been hit first; FIFO within a price.
        cands.sort(key=lambda o: ((-o.req.price if our_action == "buy" else o.req.price), o.ts_active, o.order_id))
        available = ev.qty
        for o in cands:
            if available <= _EPS:
                break
            if o.req.price != ev.price:
                if not self.cfg.trade_through_fills_all:
                    continue
                take = min(o.remaining, available)
            else:
                used = min(o.queue_ahead, available)
                o.queue_ahead -= used
                available -= used
                take = min(o.remaining, available)
            if take > _EPS:
                available -= take
                self._fill(o, o.req.price, take, "maker", ev.ts_ns)

    def on_settlement(self, ev: Settlement) -> float:
        self.closed_markets.add(ev.market)
        for o in self.open_orders(ev.market):
            self._finish(o, "canceled", ev.ts_ns, "settled")
        return self.portfolio.settle(ev.market, ev.value, ev.ts_ns)

    def close_market(self, market: str, now: int) -> None:
        """Trading halts at close: cancel everything resting (settlement comes later)."""
        self.closed_markets.add(market)
        for o in self.open_orders(market):
            self._finish(o, "canceled", now, "market_closed")

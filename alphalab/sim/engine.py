"""TradingEngine: the single event-processing core for backtest, paper and live.

Event flow for every market event (identical in all modes)::

    timers due (order activations, cancels, strategy timers)
      -> book update (+ broker queue/cross-fill bookkeeping)
      -> feature update
      -> strategy callbacks -> ctx.place() -> RiskEngine -> broker.submit()

The engine records signals, orders, fills and an equity curve so every run is
auditable after the fact.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
from bisect import bisect_right
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from alphalab.core.book_manager import BookManager
from alphalab.core.events import (BookDelta, BookSnapshot, ExternalPrice, MarketStatus, Settlement, Timer,
                                  TradeEvent)
from alphalab.core.prices import CENT, PRICE_SCALE
from alphalab.execution.risk import RiskEngine
from alphalab.research.features import FeatureEngine
from alphalab.sim.broker import Fill, OrderRequest, SimOrder
from alphalab.strategies.base import Strategy

log = logging.getLogger(__name__)
NS = 1_000_000_000
MARKOUT_HORIZONS_S = (1, 5, 30, 60)


class StrategyContext:
    """What a strategy is allowed to see and do."""

    def __init__(self, engine: "TradingEngine", strategy: Strategy):
        self._e = engine
        self._s = strategy

    @property
    def now_ns(self) -> int:
        return self._e.now_ns

    @property
    def mode(self) -> str:
        return self._e.mode

    def markets(self) -> List[str]:
        return [m for m in self._e.market_meta if self._s.accepts(m, self._e.market_meta[m])]

    def meta(self, market: str) -> Dict[str, Any]:
        return self._e.market_meta.get(market, {})

    def book(self, market: str):
        if not self._e.books.is_synced(market):
            return None
        return self._e.books.books.get(market)

    def features(self, market: str) -> Dict[str, Any]:
        f = self._e.features.features(market, self._e.now_ns)
        f["inventory"] = self.position(market)
        return f

    def position(self, market: str) -> float:
        return self._e.broker.portfolio.pos(self._s.name, market).qty

    def avg_price(self, market: str) -> float:
        return self._e.broker.portfolio.pos(self._s.name, market).avg_price

    def open_orders(self, market: Optional[str] = None) -> List[SimOrder]:
        return self._e.broker.open_orders(market, self._s.name)

    def place(self, market: str, action: str, price: int, qty: float, tif: str = "gtc",
              post_only: bool = False, tag: str = "") -> Optional[str]:
        return self._e.place(self._s, OrderRequest(self._s.name, market, action, int(price), float(qty),
                                                   tif, post_only, tag))

    def cancel(self, order_id: str) -> bool:
        return self._e.broker.cancel(order_id, self._e.now_ns)

    def cancel_all(self, market: Optional[str] = None) -> int:
        return self._e.broker.cancel_all(self._e.now_ns, market, self._s.name)

    def schedule(self, delay_ms: float, key: str) -> None:
        self._e.schedule(self._s, self._e.now_ns + int(delay_ms * 1_000_000), key)

    def signal(self, market: str, name: str, value: float, **payload: Any) -> None:
        self._e.record_signal(self._s.name, market, name, value, payload)

    def is_halted(self) -> bool:
        r = self._e.risk
        return bool(r and (r.halted or r.is_kill_tripped()))


class TradingEngine:
    def __init__(self, strategies: List[Strategy], broker, books: BookManager, features: FeatureEngine,
                 risk: Optional[RiskEngine], market_meta: Dict[str, Dict[str, Any]], mode: str = "backtest",
                 run_id: str = "", pnl_interval_s: float = 60.0, record_signals: bool = True,
                 on_record: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.strategies = strategies
        self.broker = broker
        self.books = books
        self.features = features
        self.risk = risk
        self.market_meta = market_meta
        self.mode = mode
        self.run_id = run_id
        self.now_ns = 0
        self.ctx = {s.name: StrategyContext(self, s) for s in strategies}
        self._timers: List[Tuple[int, int, str, str]] = []
        self._tseq = itertools.count()
        self.signals: List[Dict[str, Any]] = []
        self.pnl_curve: List[Dict[str, Any]] = []
        self.record_signals = record_signals
        self.on_record = on_record
        self._pnl_interval = int(pnl_interval_s * NS)
        self._next_pnl = None
        self._mid_hist: Dict[str, Tuple[List[int], List[float]]] = {}
        self._started = False
        self._by_name = {s.name: s for s in strategies}
        self.event_count = 0
        broker.on_fill = self._on_fill
        prev_on_order = broker.on_order
        def _on_order(o: SimOrder) -> None:
            if prev_on_order:
                prev_on_order(o)
            if self.on_record and o.status in ("filled", "canceled", "rejected"):
                self.on_record("order", self.order_row(o))
        broker.on_order = _on_order

    # ------------------------------------------------------------------ lifecycle
    def start(self, ts_ns: int) -> None:
        self.now_ns = ts_ns
        self._next_pnl = ts_ns
        for s in self.strategies:
            s.on_start(self.ctx[s.name])
        self._started = True

    def stop(self) -> None:
        for s in self.strategies:
            s.on_stop(self.ctx[s.name])
        self._snapshot_pnl(force=True)

    # ------------------------------------------------------------------ timers
    def schedule(self, strategy: Strategy, ts_ns: int, key: str) -> None:
        heapq.heappush(self._timers, (ts_ns, next(self._tseq), strategy.name, key))

    def advance_to(self, ts_ns: int) -> None:
        """Process every broker/strategy timer due at or before ``ts_ns`` in time order."""
        while True:
            tb = self.broker.next_timer_ns()
            ts_s = self._timers[0][0] if self._timers else None
            nxt = min(t for t in (tb, ts_s) if t is not None) if (tb is not None or ts_s is not None) else None
            if nxt is None or nxt > ts_ns:
                break
            self.now_ns = max(self.now_ns, nxt)
            if tb is not None and tb == nxt:
                self.broker.process_timers(nxt)
            else:
                _, _, sname, key = heapq.heappop(self._timers)
                s = self._by_name[sname]
                s.on_timer(self.ctx[sname], key)
        self.now_ns = max(self.now_ns, ts_ns)
        self._snapshot_pnl()

    # ------------------------------------------------------------------ events
    def warm(self, ev: Any) -> None:
        """Apply a pre-start event to books/features only (no strategy, no broker)."""
        kind = ev.kind
        if kind in ("snapshot", "delta"):
            self.books.apply(ev)
            synced = self.books.is_synced(ev.market)
            ob = self.books.book(ev.market)
            self.features.on_book(ev.market, ob, ev.ts_ns, synced)
            if synced:
                self._record_mid(ev.market, ob.mid(), ev.ts_ns)
                if self.risk is not None:
                    self.risk.on_data(ev.market, ev.ts_ns)
        elif kind == "trade":
            self.features.on_trade(ev)
        elif kind == "external":
            self.features.on_external(ev)
        self.now_ns = max(self.now_ns, ev.ts_ns)

    def handle(self, ev: Any) -> None:
        self.advance_to(ev.ts_ns)
        self.event_count += 1
        kind = ev.kind
        if kind in ("snapshot", "delta"):
            self.broker.before_book(ev)
            changed = self.books.apply(ev)
            synced = self.books.is_synced(ev.market)
            ob = self.books.book(ev.market)
            if self.risk is not None and synced:
                self.risk.on_data(ev.market, ev.ts_ns)
            self.features.on_book(ev.market, ob, ev.ts_ns, synced)
            self.broker.after_book(ev)
            if synced:
                self._record_mid(ev.market, ob.mid(), ev.ts_ns)
            if changed and synced:
                for s in self.strategies:
                    if s.accepts(ev.market, self.market_meta.get(ev.market, {})):
                        s.on_book(self.ctx[s.name], ev.market)
        elif kind == "trade":
            self.features.on_trade(ev)
            self.broker.on_trade(ev)
            if self.risk is not None:
                self.risk.on_data(ev.market, ev.ts_ns)
            for s in self.strategies:
                if s.accepts(ev.market, self.market_meta.get(ev.market, {})):
                    s.on_trade(self.ctx[s.name], ev)
        elif kind == "external":
            self.features.on_external(ev)
            for s in self.strategies:
                s.on_external(self.ctx[s.name], ev)
        elif kind == "settlement":
            self.broker.on_settlement(ev)
            self._record_mid(ev.market, ev.value * PRICE_SCALE, ev.ts_ns)
            for s in self.strategies:
                s.on_settlement(self.ctx[s.name], ev.market, ev.value)
            self._snapshot_pnl(force=True)
        elif kind == "status":
            if ev.event_type in ("deactivated", "closed", "determined", "settled"):
                self.broker.close_market(ev.market, ev.ts_ns)

    # ------------------------------------------------------------------ orders
    def place(self, strategy: Strategy, req: OrderRequest) -> Optional[str]:
        if self.risk is not None:
            ob = self.books.books.get(req.market)
            mid = ob.mid() if (ob is not None and self.books.is_synced(req.market)) else None
            positions = {k: (p.qty, p.avg_price) for k, p in self.broker.portfolio.positions.items()}
            d = self.risk.check_order(strategy=req.strategy, market=req.market, action=req.action,
                                      price=req.price, qty=req.qty, now_ns=self.now_ns, mid=mid,
                                      positions=positions, open_orders=self.broker.open_orders(),
                                      client_order_id=req.client_order_id)
            if not d.ok:
                return None
        o = self.broker.submit(req, self.now_ns)
        if self.on_record:
            self.on_record("order_submitted", self.order_row(o))
        return o.order_id if o.status != "rejected" else None

    def _on_fill(self, f: Fill) -> None:
        s = self._by_name.get(f.strategy)
        if self.on_record:
            self.on_record("fill", asdict(f))
        if s is not None:
            s.on_fill(self.ctx[s.name], f)
        self._snapshot_pnl(force=True)

    # ------------------------------------------------------------------ records
    def record_signal(self, strategy: str, market: str, name: str, value: float, payload: Dict[str, Any]) -> None:
        if not self.record_signals:
            return
        row = dict(run_id=self.run_id, ts_ns=self.now_ns, strategy=strategy, market=market, name=name,
                   value=float(value) if value is not None and not (isinstance(value, float) and math.isnan(value)) else None,
                   payload=payload)
        self.signals.append(row)
        if self.on_record:
            self.on_record("signal", row)

    def _record_mid(self, market: str, mid: Optional[float], ts: int) -> None:
        if mid is None:
            return
        ts_list, mids = self._mid_hist.setdefault(market, ([], []))
        if mids and mids[-1] == mid:
            return
        ts_list.append(ts)
        mids.append(mid)

    def mid_asof(self, market: str, ts: int) -> Optional[float]:
        h = self._mid_hist.get(market)
        if not h:
            return None
        i = bisect_right(h[0], ts) - 1
        return h[1][i] if i >= 0 else None

    def marks(self) -> Dict[str, float]:
        out = {}
        for m, (ts, mids) in self._mid_hist.items():
            if mids:
                out[m] = mids[-1] / PRICE_SCALE
        return out

    def equity(self) -> float:
        pf = self.broker.portfolio
        marks = self.marks()
        return pf.cash + sum(p.qty * marks.get(m, p.avg_price) for (s, m), p in pf.positions.items() if p.qty)

    def _snapshot_pnl(self, force: bool = False) -> None:
        if self._next_pnl is None:
            return
        if not force and self.now_ns < self._next_pnl:
            return
        pf = self.broker.portfolio
        eq = self.equity()
        row = dict(run_id=self.run_id, ts_ns=self.now_ns, cash=pf.cash, realized=pf.realized(),
                   unrealized=pf.unrealized(self.marks()), fees=pf.fees_paid, equity=eq,
                   gross_exposure=pf.capital_at_risk(),
                   positions={f"{s}|{m}": p.qty for (s, m), p in pf.open_positions().items()})
        if self.pnl_curve and self.pnl_curve[-1]["ts_ns"] == self.now_ns:
            self.pnl_curve[-1] = row
        else:
            self.pnl_curve.append(row)
        if self.risk is not None:
            self.risk.on_equity(self.now_ns, eq)
        if not force:
            self._next_pnl = self.now_ns + self._pnl_interval

    def markouts(self, fills: List[Fill]) -> List[Dict[str, Any]]:
        """Per-fill markouts: signed mid move after the fill, in $/contract.
        Positive = the market moved in our favour (no adverse selection)."""
        out = []
        for f in fills:
            d = 1 if f.action == "buy" else -1
            row = {"fill_id": f.fill_id}
            base = f.mid_at_fill
            for h in MARKOUT_HORIZONS_S:
                m = self.mid_asof(f.market, f.ts_ns + h * NS)
                row[f"markout_{h}s"] = None if (m is None or base is None) else d * (m - base) / PRICE_SCALE
            out.append(row)
        return out

    def order_row(self, o: SimOrder) -> Dict[str, Any]:
        return dict(run_id=self.run_id, mode=self.mode, order_id=o.order_id,
                    client_order_id=o.req.client_order_id, strategy=o.req.strategy, market=o.req.market,
                    action=o.req.action, price=o.req.price, qty=o.req.qty, tif=o.req.tif,
                    post_only=o.req.post_only, ts_decision=o.ts_decision, ts_submit=o.ts_decision,
                    ts_active=o.ts_active, ts_done=o.ts_done, status=o.status, filled_qty=o.filled,
                    avg_fill_price=o.avg_fill_price, reason=o.reason, tag=o.req.tag)

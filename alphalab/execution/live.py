"""Live execution - DISABLED BY DEFAULT.

Live trading is refused unless ALL of the following hold (``assert_live_allowed``):

1. ``TRADING_MODE=live``
2. ``LIVE_TRADING_ACK`` equals ``I_ACCEPT_REAL_MONEY_RISK`` exactly
3. API credentials are present in the environment
4. the kill switch is not tripped
5. every strategy's exact parameter set has status ``LIVE-CANDIDATE`` in the
   ``strategy_status`` table (earned only through paper trading; see
   ``research.validation.evaluate_paper``) - the only exception is
   ``--demo-integration`` against the Kalshi *demo* exchange (fake money)
6. risk limits are finite and within the hard caps below

The live broker mirrors the simulator interface. Orders are sent over REST;
fills arrive on the authenticated ``fill`` WebSocket channel and order state
on ``user_orders``. Order entry never originates from an LLM: the research
assistant has no access to this module.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from alphalab.core.config import LIVE_ACK_VALUE, Settings
from alphalab.core.fees import FeeModel
from alphalab.core.prices import PRICE_SCALE, dollars_to_units, parse_count
from alphalab.data.db import Database
from alphalab.execution.killswitch import KillSwitch
from alphalab.kalshi.rest import KalshiAPIError, KalshiREST
from alphalab.sim.broker import Fill, OrderRequest, SimOrder
from alphalab.sim.portfolio import Portfolio

log = logging.getLogger(__name__)

HARD_MAX_ORDER_SIZE = 100
HARD_MAX_TOTAL_EXPOSURE_USD = 2_000.0
HARD_MAX_DAILY_LOSS_USD = 500.0


class LiveTradingRefused(RuntimeError):
    pass


def assert_live_allowed(settings: Settings, db: Optional[Database], strategy_keys: List[str],
                        demo_integration: bool = False) -> None:
    problems = []
    if settings.trading_mode != "live":
        problems.append("TRADING_MODE is not 'live' (default is paper)")
    if settings.live_ack != LIVE_ACK_VALUE:
        problems.append(f"LIVE_TRADING_ACK must be set to {LIVE_ACK_VALUE}")
    if not settings.kalshi.has_credentials:
        problems.append("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set")
    if KillSwitch(settings.risk.kill_switch_file).is_tripped(force=True):
        problems.append(f"kill switch is tripped ({settings.risk.kill_switch_file}); reset it deliberately first")
    r = settings.risk
    for name, val, cap in (("max_order_size", r.max_order_size, HARD_MAX_ORDER_SIZE),
                           ("max_total_exposure_usd", r.max_total_exposure_usd, HARD_MAX_TOTAL_EXPOSURE_USD),
                           ("max_daily_loss_usd", r.max_daily_loss_usd, HARD_MAX_DAILY_LOSS_USD)):
        if not (isinstance(val, (int, float)) and math.isfinite(val) and 0 < val <= cap):
            problems.append(f"risk.{name}={val} must be finite, > 0 and <= hard cap {cap}")
    if demo_integration:
        if settings.kalshi.env != "demo":
            problems.append("--demo-integration is only allowed with KALSHI_ENV=demo")
    else:
        if db is None:
            problems.append("validation database unavailable; cannot verify strategy status")
        else:
            for key in strategy_keys:
                row = db.query_df("SELECT status FROM strategy_status WHERE strategy_key = ?", [key])
                status = row["status"].iloc[0] if not row.empty else "UNVALIDATED"
                if status != "LIVE-CANDIDATE":
                    problems.append(f"strategy {key} has status {status}; LIVE-CANDIDATE required "
                                    "(earned via paper trading, see RISK.md)")
    if problems:
        raise LiveTradingRefused("Live trading refused:\n  - " + "\n  - ".join(problems))


def order_body(req: OrderRequest, client_order_id: str) -> Dict[str, Any]:
    """Map a YES-book order to Kalshi's V2 order request (POST /portfolio/events/orders).

    V2 uses a single YES book: ``side="bid"`` buys YES at ``price``; ``side="ask"``
    sells YES at ``price`` (economically buying NO at 1 - price; Kalshi nets YES/NO
    holdings). Prices are fixed-point dollar strings, counts fixed-point strings.
    ``self_trade_prevention_type`` is required by the V2 schema.
    """
    return {
        "ticker": req.market,
        "client_order_id": client_order_id,
        "side": "bid" if req.action == "buy" else "ask",
        "count": f"{req.qty:.2f}",
        "price": f"{req.price / PRICE_SCALE:.4f}",
        "time_in_force": "immediate_or_cancel" if req.tif == "ioc" else "good_till_canceled",
        "post_only": bool(req.post_only),
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }


class LiveBroker:
    """Broker interface backed by the Kalshi REST API + private WS channels."""

    def __init__(self, rest: KalshiREST, books, fees: FeeModel, portfolio: Portfolio,
                 market_close_ns: Dict[str, int]):
        self.rest = rest
        self.books = books
        self.fees = fees
        self.portfolio = portfolio
        self.market_close_ns = dict(market_close_ns)
        self.on_fill = None
        self.on_order = None
        self.orders: Dict[str, SimOrder] = {}
        self.fills: List[Fill] = []
        self._by_exch: Dict[str, str] = {}
        self._oid = itertools.count(1)
        self._fid = itertools.count(1)
        self._lock = threading.Lock()
        self._seen_trade_ids: set = set()
        self._pending_cancel: set = set()
        self.stats = {"submitted": 0, "rejected": 0, "canceled": 0, "maker_fills": 0, "taker_fills": 0,
                      "submitted_qty": 0.0, "filled_qty": 0.0, "api_errors": 0}

    # ---------------------------------------------------------- interface parity with SimBroker
    def next_timer_ns(self) -> Optional[int]:
        return None

    def process_timers(self, up_to_ns: int) -> None:
        return None

    def before_book(self, ev) -> None: ...
    def after_book(self, ev) -> None: ...
    def on_trade(self, ev) -> None: ...

    def open_orders(self, market: Optional[str] = None, strategy: Optional[str] = None) -> List[SimOrder]:
        return [o for o in self.orders.values() if o.is_open and (market is None or o.market == market)
                and (strategy is None or o.req.strategy == strategy)]

    def _run(self, fn, *args):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return fn(*args)
        return loop.run_in_executor(None, fn, *args)

    # ---------------------------------------------------------- orders
    def submit(self, req: OrderRequest, now_ns: int) -> SimOrder:
        oid = f"L{next(self._oid)}"
        req.client_order_id = req.client_order_id or str(uuid.uuid4())
        o = SimOrder(oid, req, now_ns, now_ns, status="pending", remaining=req.qty)
        self.orders[oid] = o
        self.stats["submitted"] += 1
        self.stats["submitted_qty"] += req.qty
        self._run(self._send, o)
        return o

    def _send(self, o: SimOrder) -> None:
        body = order_body(o.req, o.req.client_order_id)
        try:
            resp = self.rest.create_order_v2(body)
        except KalshiAPIError as exc:
            with self._lock:
                self.stats["api_errors"] += 1
                self.stats["rejected"] += 1
                o.status, o.reason, o.ts_done = "rejected", f"api:{exc.status}", time.time_ns()
            log.warning("live_order_rejected", extra={"fields": {"order": o.order_id, "status": exc.status}})
            if self.on_order:
                self.on_order(o)
            return
        with self._lock:
            exch = resp.get("order_id")
            if exch:
                self._by_exch[exch] = o.order_id
                o.reason = f"exch:{exch}"
            # V2 ack: fill_count / remaining_count (fixed-point strings), ts_ms. Fills themselves are
            # applied only from the authenticated `fill` channel to avoid double counting.
            o.ack = {"fill_count": parse_count(resp.get("fill_count") or 0),
                     "remaining_count": parse_count(resp.get("remaining_count") or 0),
                     "ts_ms": resp.get("ts_ms")}
            if o.status == "pending":
                if o.req.tif == "ioc" or o.ack["remaining_count"] <= 1e-9:
                    o.status = "filled" if o.ack["fill_count"] >= o.req.qty - 1e-9 else "canceled"
                    o.ts_done = time.time_ns()
                else:
                    o.status = "resting"
        if o.order_id in self._pending_cancel and o.is_open:
            self._pending_cancel.discard(o.order_id)
            self._cancel_exch(o, exch)
            return
        if self.on_order:
            self.on_order(o)

    def _exch_id(self, o: SimOrder) -> Optional[str]:
        return o.reason[5:] if o.reason.startswith("exch:") else None

    def cancel(self, order_id: str, now_ns: int) -> bool:
        o = self.orders.get(order_id)
        if o is None or not o.is_open or o.cancel_requested_ns is not None:
            return False
        o.cancel_requested_ns = now_ns
        exch = self._exch_id(o)
        if exch:
            self._run(self._cancel_exch, o, exch)
        else:
            self._pending_cancel.add(order_id)  # cancel as soon as the create ack arrives
        return True

    def _cancel_exch(self, o: SimOrder, exch: str) -> None:
        try:
            resp = self.rest.cancel_order_v2(exch, market_ticker=o.market)
            o.cancel_ack = {"reduced_by": parse_count(resp.get("reduced_by") or 0), "ts_ms": resp.get("ts_ms")}
            with self._lock:
                if o.is_open:
                    o.status, o.ts_done = "canceled", time.time_ns()
                    self.stats["canceled"] += 1
        except KalshiAPIError as exc:
            self.stats["api_errors"] += 1
            log.error("live_cancel_failed", extra={"fields": {"order": o.order_id, "status": exc.status}})
        if self.on_order:
            self.on_order(o)

    def cancel_all(self, now_ns: int, market: Optional[str] = None, strategy: Optional[str] = None) -> int:
        n = 0
        for o in self.open_orders(market, strategy):
            n += bool(self.cancel(o.order_id, now_ns))
        return n

    def shutdown(self) -> None:
        """Best-effort: cancel every resting order on the account for markets we traded."""
        markets = {o.market for o in self.orders.values()}
        for m in markets:
            try:
                for od in self.rest.get_orders(ticker=m, status="resting"):
                    try:
                        self.rest.cancel_order_v2(od["order_id"], market_ticker=m)
                    except KalshiAPIError:
                        pass
            except KalshiAPIError:
                pass

    # ---------------------------------------------------------- exchange callbacks
    def on_private(self, frame: Dict[str, Any]) -> None:
        typ, msg = frame.get("private"), frame.get("msg") or {}
        if typ == "fill":
            self._on_fill_msg(msg, frame.get("recv_ns") or time.time_ns())
        elif typ in ("user_order", "user_orders"):
            local = self._by_exch.get(msg.get("order_id", ""))
            o = self.orders.get(local) if local else None
            if o is not None and msg.get("status") in ("canceled", "executed") and o.is_open:
                o.status = "canceled" if msg["status"] == "canceled" else "filled"
                o.ts_done = time.time_ns()
                if self.on_order:
                    self.on_order(o)

    def _on_fill_msg(self, msg: Dict[str, Any], recv_ns: int) -> None:
        tid = msg.get("trade_id")
        if tid in self._seen_trade_ids:
            return  # duplicate delivery
        self._seen_trade_ids.add(tid)
        local = self._by_exch.get(msg.get("order_id", ""))
        o = self.orders.get(local) if local else None
        if o is None:
            log.warning("live_fill_unknown_order", extra={"fields": {"order_id": msg.get("order_id")}})
            return
        qty = parse_count(msg.get("count_fp", msg.get("count", 0)))
        yes_units = dollars_to_units(msg["yes_price_dollars"]) if msg.get("yes_price_dollars") else o.req.price
        is_taker = bool(msg.get("is_taker"))
        fee = self.fees.fee(o.market, yes_units, qty, is_taker)
        with self._lock:
            o.remaining = max(0.0, o.remaining - qty)
            o.filled += qty
            o.fill_notional += qty * yes_units / PRICE_SCALE
            self.portfolio.on_fill(o.req.strategy, o.market, o.req.action, yes_units, qty, fee, recv_ns)
            ob = self.books.books.get(o.market)
            f = Fill(f"F{next(self._fid)}", o.order_id, o.req.strategy, o.market, recv_ns, o.req.action, yes_units,
                     qty, "taker" if is_taker else "maker", fee, ob.mid() if ob else None, o.req.tag,
                     self.portfolio.pos(o.req.strategy, o.market).qty)
            self.fills.append(f)
            self.stats["taker_fills" if is_taker else "maker_fills"] += 1
            self.stats["filled_qty"] += qty
            if o.remaining <= 1e-9 and o.is_open:
                o.status, o.ts_done = "filled", recv_ns
        if self.on_fill:
            self.on_fill(f)

    def on_settlement(self, ev) -> float:
        for o in self.open_orders(ev.market):
            o.status, o.ts_done = "canceled", ev.ts_ns
        return self.portfolio.settle(ev.market, ev.value, ev.ts_ns)

    def close_market(self, market: str, now: int) -> None:
        for o in self.open_orders(market):
            o.status, o.ts_done = "canceled", now

    def reconcile(self) -> Dict[str, Any]:
        """Compare local positions with the exchange; mismatches are reported."""
        out: Dict[str, Any] = {"mismatches": []}
        try:
            positions = self.rest.get_positions()
        except KalshiAPIError as exc:
            return {"error": exc.status}
        local: Dict[str, float] = {}
        for (s, m), p in self.portfolio.positions.items():
            local[m] = local.get(m, 0.0) + p.qty
        for p in positions:
            m = p.get("ticker")
            ex = parse_count(p.get("position_fp", p.get("position", 0)))
            if m in local and abs(local[m] - ex) > 1e-6:
                out["mismatches"].append({"market": m, "local": local[m], "exchange": ex})
        return out

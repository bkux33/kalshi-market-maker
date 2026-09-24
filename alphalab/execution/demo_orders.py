"""DEMO-only order-path verification (``alphalab demo-orders``).

Exercises the real order path (V2 REST order entry, authenticated ``fill`` /
``user_orders`` channels, ``LiveBroker``, ``RiskEngine``, kill switch) against the
Kalshi **DEMO** exchange (fake money) or a local mock. It is deliberately separate
from live trading: it never calls ``assert_live_allowed``, does not change
``TRADING_MODE``, and refuses to run unless every URL is a demo/local host.

Steps (each reported pass/fail/skipped):
  create_and_ack            post-only resting bid far from the market, wait for the ack
  local_duplicate_block     identical order within the duplicate window is refused by the risk engine
  exchange_duplicate_block  re-using the same client_order_id is refused by the exchange
  reconnect_with_order      drop the WebSocket while the order rests; order survives, stream resumes
  cancel_and_ack            cancel, wait for the cancel ack and the user_orders update
  stale_price_block         the risk engine refuses orders when market data is stale
  kill_switch               trip a *temporary* kill switch: open orders cancelled, new orders refused
  fill / partial_fill       (only with attempt_fill) marketable IOC orders; fills via the fill channel
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from alphalab.core.book_manager import BookManager
from alphalab.core.config import RiskConfig, Settings, is_demo_url
from alphalab.core.fees import FeeModel
from alphalab.core.prices import CENT, MAX_PRICE, MIN_PRICE
from alphalab.execution.killswitch import KillSwitch
from alphalab.execution.live import LiveBroker, order_body
from alphalab.execution.risk import RiskEngine
from alphalab.kalshi.rest import KalshiAPIError
from alphalab.kalshi.ws import PRIVATE_CHANNELS, KalshiWebSocket
from alphalab.sim.broker import OrderRequest
from alphalab.sim.portfolio import Portfolio


class DemoOnlyError(RuntimeError):
    pass


def assert_demo(settings: Settings, rest_url: str, ws_url: str) -> None:
    if settings.kalshi.env != "demo" or not is_demo_url(rest_url) or not is_demo_url(ws_url):
        raise DemoOnlyError("demo-orders only runs against the Kalshi DEMO environment (or a local mock); refusing")
    if settings.trading_mode != "paper":
        raise DemoOnlyError("demo-orders requires TRADING_MODE=paper; it never needs live mode")


async def _wait(pred, timeout: float, step: float = 0.05) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
    return bool(pred())


async def run_demo_orders(settings: Settings, rest, signer, ws_url: str, market: str, attempt_fill: bool = False,
                          connect=None, timeout: float = 15.0) -> Dict[str, Any]:
    assert_demo(settings, rest.base_url, ws_url)
    steps: Dict[str, Dict[str, Any]] = {}
    books = BookManager()
    portfolio = Portfolio()
    broker = LiveBroker(rest, books, FeeModel(), portfolio, {})
    fills: List[Any] = []
    broker.on_fill = fills.append
    kill = KillSwitch(Path(tempfile.mkdtemp(prefix="alphalab-demo-kill-")) / "KILL_SWITCH")
    risk = RiskEngine(RiskConfig(max_order_size=5, max_position_per_market=10, max_market_exposure_usd=20,
                                 max_total_exposure_usd=20, max_strategy_exposure_usd=20, max_orders_per_second=5,
                                 max_open_orders=5, stale_data_s=5.0, settlement_buffer_s=0),
                      mode="paper", kill_switch=kill)
    statuses: List[str] = []

    def on_event(ev):
        if isinstance(ev, dict):
            broker.on_private(ev)
            return
        if ev.kind in ("snapshot", "delta") and getattr(ev, "market", None) == market:
            books.apply(ev)
            risk.on_data(market, time.time_ns())

    kw = {"connect": connect} if connect else {}
    ws = KalshiWebSocket(ws_url, signer, [market], channels=("orderbook_delta",) + PRIVATE_CHANNELS,
                         on_event=on_event, on_status=lambda s, i: statuses.append(s), max_backoff_s=1.0, **kw)
    ws_task = asyncio.create_task(ws.run())

    def ok(name: str, passed: bool, **detail: Any) -> None:
        steps[name] = {"result": "pass" if passed else "fail", **detail}

    def place(action: str, price: int, qty: float, tif: str = "gtc", post_only: bool = False, tag: str = ""):
        d = risk.check_order(strategy="demo", market=market, action=action, price=price, qty=qty,
                             now_ns=time.time_ns(), mid=books.book(market).mid(),
                             positions={k: (p.qty, p.avg_price) for k, p in portfolio.positions.items()},
                             open_orders=broker.open_orders())
        if not d.ok:
            return None, d.reason
        return broker.submit(OrderRequest("demo", market, action, price, qty, tif, post_only, tag), time.time_ns()), ""

    try:
        if not await _wait(lambda: books.is_synced(market), timeout):
            ok("market_data", False, error="no order-book snapshot received")
            return {"market": market, "steps": steps, "statuses": statuses}
        ob = books.book(market)
        steps["market_data"] = {"result": "pass", "best_bid_c": (ob.bid_price or 0) / CENT,
                                "best_ask_c": (ob.ask_price or 0) / CENT}
        ref = ob.bid_price or ob.mid() or 50 * CENT
        far = int(max(MIN_PRICE, min(int(ref) - 10 * CENT, MAX_PRICE)))
        far = max(MIN_PRICE, far - far % CENT)
        # 1. create + ack
        o, why = place("buy", far, 1, post_only=True, tag="demo-rest")
        acked = o is not None and await _wait(lambda: o.reason.startswith("exch:") or o.status == "rejected", timeout)
        ok("create_and_ack", bool(o and acked and o.status == "resting"), price_c=far / CENT,
           status=getattr(o, "status", why), ack=getattr(o, "ack", None))
        # 2. duplicates
        o2, why2 = place("buy", far, 1, post_only=True)
        ok("local_duplicate_block", o2 is None and why2 in ("duplicate_order", "duplicate_resting_order"), reason=why2)
        if o is not None and o.req.client_order_id:
            try:
                rest.create_order_v2(order_body(o.req, o.req.client_order_id))
                ok("exchange_duplicate_block", False, error="exchange accepted a reused client_order_id")
            except KalshiAPIError as exc:
                ok("exchange_duplicate_block", 400 <= exc.status < 500, http_status=exc.status)
        # 3. reconnect while the order exists
        before = ws.stats["connections"]
        if ws._ws is not None:
            await ws._ws.close()
        reconnected = await _wait(lambda: ws.stats["connections"] > before and books.is_synced(market), timeout)
        still = [x for x in rest.get_orders(ticker=market, status="resting")
                 if o is not None and x.get("order_id") == o.reason[5:]]
        ok("reconnect_with_order", reconnected and bool(still) and o.is_open, reconnects=ws.stats["reconnects"])
        # 4. cancel + ack
        if o is not None and o.is_open:
            broker.cancel(o.order_id, time.time_ns())
            done = await _wait(lambda: o.status == "canceled", timeout)
            ok("cancel_and_ack", done and bool(o.cancel_ack), cancel_ack=o.cancel_ack)
        else:
            steps["cancel_and_ack"] = {"result": "skipped", "reason": "no resting order"}
        # 5. stale-price protection
        d = risk.check_order(strategy="demo", market=market, action="buy", price=far, qty=1,
                             now_ns=time.time_ns() + 60 * 1_000_000_000, mid=ob.mid(), positions={}, open_orders=[])
        ok("stale_price_block", not d.ok and d.reason == "stale_data", reason=d.reason)
        # 6. kill switch
        risk._fingerprints.clear()
        o3, _ = place("buy", max(MIN_PRICE, far - CENT), 1, post_only=True, tag="demo-kill")
        await _wait(lambda: o3 is not None and o3.status == "resting", timeout)
        kill.trip("demo-orders test", source="demo_orders")
        n = broker.cancel_all(time.time_ns())
        cancelled = await _wait(lambda: o3 is None or o3.status == "canceled", timeout)
        o4, why4 = place("buy", max(MIN_PRICE, far - 2 * CENT), 1, post_only=True)
        ok("kill_switch", cancelled and o4 is None and why4 == "kill_switch", cancelled_orders=n, refused=why4)
        kill.reset()
        risk.resume()
        # 7. fills (demo money only)
        if attempt_fill:
            risk._fingerprints.clear()
            ask = books.book(market).best_ask()
            if ask is None:
                steps["fill"] = {"result": "skipped", "reason": "no ask"}
            else:
                px, sz = ask
                o5, why5 = place("buy", px, 1, tif="ioc", tag="demo-fill")
                got = await _wait(lambda: any(f.order_id == getattr(o5, "order_id", None) for f in fills), timeout)
                ok("fill", got, reason=why5, price_c=px / CENT)
                await asyncio.sleep(0.3)
                _, no_lv = rest.get_orderbook(market)       # authoritative top of book after our fill
                ask = ((10000 - max(p for p, _ in no_lv)), dict(no_lv)[max(p for p, _ in no_lv)]) if no_lv else None
                risk._fingerprints.clear()
                if ask is not None and ask[1] + 1 <= 5:
                    px, sz = ask
                    o6, why6 = place("buy", px, sz + 1, tif="ioc", tag="demo-partial")
                    got = await _wait(lambda: o6 is not None and o6.status in ("canceled", "filled"), timeout)
                    await asyncio.sleep(0.3)
                    part = [f for f in fills if f.order_id == getattr(o6, "order_id", None)]
                    filled = sum(f.qty for f in part)
                    ok("partial_fill", got and 0 < filled < sz + 1, requested=sz + 1, filled=filled,
                       status=getattr(o6, "status", why6))
                else:
                    steps["partial_fill"] = {"result": "skipped",
                                             "reason": "top ask too large to test a partial fill with <= 5 contracts"}
        else:
            steps["fill"] = {"result": "skipped", "reason": "run with --attempt-fill (DEMO money) to test fills"}
    finally:
        try:
            broker.cancel_all(time.time_ns())
            await asyncio.sleep(0.2)
        except Exception:
            pass
        await ws.stop()
        ws_task.cancel()
        await asyncio.gather(ws_task, return_exceptions=True)
    return {"market": market, "steps": steps, "fills": len(fills), "statuses": statuses[-20:],
            "ok": all(v["result"] != "fail" for v in steps.values())}


def format_demo_orders(rep: Dict[str, Any]) -> str:
    lines = [f"DEMO ORDER PATH  market={rep['market']}"]
    for k, v in rep["steps"].items():
        extra = {x: y for x, y in v.items() if x != "result"}
        lines.append(f"  {v['result'].upper():7s} {k:26s} {extra}")
    lines.append("RESULT: " + ("OK" if rep.get("ok") else "FAILED"))
    return "\n".join(lines)

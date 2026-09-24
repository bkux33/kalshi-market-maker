"""A small local mock of the Kalshi Trade API (REST + WebSocket) for end-to-end tests.

It is NOT Kalshi. It implements the current wire formats our client depends on
(fixed-point ``*_dollars`` / ``*_fp`` fields, ``ts_ms``, V2 orders on
``/portfolio/events/orders``), verifies RSA-PSS request signatures exactly as
Kalshi documents them, and can inject faults (duplicates, sequence gaps,
disconnects, malformed frames) so the client's handling can be proven.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Set

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

WS_PATH = "/trade-api/ws/v2"
REST_PREFIX = "/trade-api/v2"


def _d(units: int) -> str:
    return f"{units / 10000:.4f}"


def _q(x: float) -> str:
    return f"{x:.2f}"


class MockKalshi:
    def __init__(self, public_key, markets: Optional[Dict[str, Dict[str, Any]]] = None):
        self.public_key = public_key
        now = time.time()
        self.markets = markets or {
            "KXBTC15M-26SEP241500-00": {"series": "KXBTC15M", "category": "Crypto", "title": "BTC up in 15 min?",
                                        "yes": {4200: 100.0, 4100: 50.0}, "no": {5500: 80.0, 5400: 40.0},
                                        "close": now + 3600, "volume": 1200.0},
            "KXETH15M-26SEP241500-00": {"series": "KXETH15M", "category": "Crypto", "title": "ETH up in 15 min?",
                                        "yes": {3000: 20.0}, "no": {6800: 1.0, 6700: 30.0},
                                        "close": now + 3600, "volume": 800.0},
            "KXCPI-26OCT-T3.0": {"series": "KXCPI", "category": "Economics", "title": "CPI above 3.0%?",
                                 "yes": {3100: 5.0}, "no": {6500: 5.0}, "close": now + 86400 * 20, "volume": 50.0},
        }
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.client_ids: Set[str] = set()
        self.fills: List[Dict[str, Any]] = []
        self.auth_failures = 0
        self.requests: List[tuple] = []
        self.ws_connections = 0
        self._conns: List[Any] = []
        self._oid = itertools.count(1)
        self.fault: Dict[str, Any] = {}      # e.g. {"duplicate_every": 5, "gap_at": 7, "drop_after": 12, "malformed_at": 3}
        self.stream_deltas = 30

    # ------------------------------------------------------------------ auth
    def _verify(self, headers, method: str, path: str) -> bool:
        try:
            ts = headers["kalshi-access-timestamp"]
            sig = base64.b64decode(headers["kalshi-access-signature"])
            assert headers["kalshi-access-key"]
            self.public_key.verify(sig, f"{ts}{method.upper()}{path}".encode(),
                                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                               salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            return abs(int(ts) / 1000 - time.time()) < 60
        except Exception:
            self.auth_failures += 1
            return False

    # ------------------------------------------------------------------ REST
    def _market_json(self, t: str) -> Dict[str, Any]:
        m = self.markets[t]
        yb = max(m["yes"]) if m["yes"] else None
        ya = (10000 - max(m["no"])) if m["no"] else None
        return {"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "market_type": "binary", "title": m["title"],
                "yes_sub_title": m["title"], "no_sub_title": "No", "status": m.get("status", "active"),
                "open_time": "2026-09-24T00:00:00Z",
                "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m["close"])),
                "yes_bid_dollars": _d(yb) if yb else "0.0000", "yes_ask_dollars": _d(ya) if ya else "1.0000",
                "volume_fp": _q(m["volume"]), "open_interest_fp": "10.00", "result": "", "can_close_early": True,
                "price_level_structure": "linear_cent", "exchange_index": 2 if m["category"] == "Crypto" else 0,
                "floor_strike": None, "strike_type": None}

    def handle_rest(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.requests.append((req.method, path, dict(req.url.params)))
        if not path.startswith(REST_PREFIX):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        p = path[len(REST_PREFIX):]
        private = p.startswith("/portfolio")
        if private and not self._verify(req.headers, req.method, path):
            return httpx.Response(401, json={"error": {"code": "authentication_error", "message": "bad signature"}})
        if p == "/exchange/status":
            return httpx.Response(200, json={"exchange_active": True, "trading_active": True})
        if p == "/portfolio/balance":
            return httpx.Response(200, json={"balance": 1_000_000, "portfolio_value": 0})
        if p == "/series":
            cat = req.url.params.get("category")
            ser = {}
            for t, m in self.markets.items():
                ser[m["series"]] = {"ticker": m["series"], "category": m["category"], "title": m["series"],
                                    "fee_type": "quadratic", "fee_multiplier": 1}
            out = [v for v in ser.values() if not cat or v["category"] == cat]
            return httpx.Response(200, json={"series": out})
        if p.startswith("/series/"):
            s = p.split("/")[2]
            return httpx.Response(200, json={"series": {"ticker": s, "fee_type": "quadratic", "fee_multiplier": 1}})
        if p == "/markets":
            st, series = req.url.params.get("status"), req.url.params.get("series_ticker")
            rows = [self._market_json(t) for t, m in sorted(self.markets.items())
                    if (not series or m["series"] == series)
                    and (st is None or (st == "open") == (m.get("status", "active") == "active"))]
            cursor = req.url.params.get("cursor")
            page = rows[:2] if not cursor else rows[2:]
            return httpx.Response(200, json={"markets": page, "cursor": "p2" if not cursor and len(rows) > 2 else ""})
        if p.startswith("/markets/") and p.endswith("/orderbook"):
            t = p.split("/")[2]
            m = self.markets[t]
            return httpx.Response(200, json={"orderbook_fp": {
                "yes_dollars": [[_d(k), _q(v)] for k, v in sorted(m["yes"].items())],
                "no_dollars": [[_d(k), _q(v)] for k, v in sorted(m["no"].items())]}})
        if p.startswith("/markets/"):
            return httpx.Response(200, json={"market": self._market_json(p.split("/")[2])})
        if p == "/portfolio/events/orders" and req.method == "POST":
            return self._create_order(json.loads(req.content))
        if p.startswith("/portfolio/events/orders/") and req.method == "DELETE":
            return self._cancel_order(p.rsplit("/", 1)[1])
        if p == "/portfolio/orders" and req.method == "GET":
            st = req.url.params.get("status")
            rows = [o for o in self.orders.values() if st is None or o["status"] == st]
            return httpx.Response(200, json={"orders": rows, "cursor": ""})
        if p == "/portfolio/orders" and req.method == "POST":
            return httpx.Response(404, json={"error": {"message": "legacy order endpoint not supported by mock"}})
        return httpx.Response(404, json={"error": {"message": f"no route {req.method} {p}"}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_rest)

    # ------------------------------------------------------------------ matching (V2 single YES book)
    def _create_order(self, body: Dict[str, Any]) -> httpx.Response:
        required = {"ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type"}
        if not required <= set(body) or body["side"] not in ("bid", "ask") or not isinstance(body["price"], str) \
                or not isinstance(body["count"], str):
            return httpx.Response(400, json={"error": {"code": "invalid_parameters", "message": "bad V2 order"}})
        cid = body.get("client_order_id")
        if cid and cid in self.client_ids:
            return httpx.Response(409, json={"error": {"code": "order_already_exists", "message": "duplicate client_order_id"}})
        if cid:
            self.client_ids.add(cid)
        m = self.markets[body["ticker"]]
        price = int(round(float(body["price"]) * 10000))
        qty = float(body["count"])
        oid = f"ord-{next(self._oid)}"
        filled = 0.0
        if body["side"] == "bid":   # buy YES: match resting YES asks (= NO bids)
            best_no = max(m["no"]) if m["no"] else None
            crosses = best_no is not None and price >= 10000 - best_no
        else:
            best_yes = max(m["yes"]) if m["yes"] else None
            crosses = best_yes is not None and price <= best_yes
        if crosses and body.get("post_only"):
            return httpx.Response(400, json={"error": {"code": "post_only_cross", "message": "post only would cross"}})
        while crosses and filled < qty:
            if body["side"] == "bid":
                lvl = max(m["no"]); px = 10000 - lvl
                if px > price:
                    break
                take = min(m["no"][lvl], qty - filled)
                m["no"][lvl] -= take
                if m["no"][lvl] <= 1e-9:
                    del m["no"][lvl]
            else:
                lvl = max(m["yes"]); px = lvl
                if px < price:
                    break
                take = min(m["yes"][lvl], qty - filled)
                m["yes"][lvl] -= take
                if m["yes"][lvl] <= 1e-9:
                    del m["yes"][lvl]
            filled += take
            self._emit_fill(oid, body, px, take, is_taker=True)
            if not (m["no"] if body["side"] == "bid" else m["yes"]):
                break
        remaining = qty - filled
        status = "executed" if remaining <= 1e-9 else "resting"
        if body["time_in_force"] == "immediate_or_cancel" and remaining > 1e-9:
            status = "canceled"
        o = {"order_id": oid, "client_order_id": cid, "ticker": body["ticker"], "side": body["side"],
             "price": body["price"], "remaining_count_fp": _q(remaining if status == "resting" else 0.0),
             "fill_count_fp": _q(filled), "status": status}
        self.orders[oid] = o
        self._emit_user_order(o)
        return httpx.Response(201, json={"order_id": oid, "client_order_id": cid, "fill_count": _q(filled),
                                         "remaining_count": _q(remaining if status == "resting" else 0.0),
                                         "ts_ms": int(time.time() * 1000)})

    def _cancel_order(self, oid: str) -> httpx.Response:
        o = self.orders.get(oid)
        if o is None or o["status"] != "resting":
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "order not resting"}})
        reduced = o["remaining_count_fp"]
        o["status"], o["remaining_count_fp"] = "canceled", "0.00"
        self._emit_user_order(o)
        return httpx.Response(200, json={"order_id": oid, "client_order_id": o["client_order_id"],
                                         "reduced_by": reduced, "ts_ms": int(time.time() * 1000)})

    def fill_resting(self, oid: str, qty: float) -> None:
        """Simulate another participant trading against one of our resting orders."""
        o = self.orders[oid]
        rem = float(o["remaining_count_fp"])
        take = min(rem, qty)
        o["remaining_count_fp"] = _q(rem - take)
        o["fill_count_fp"] = _q(float(o["fill_count_fp"]) + take)
        if rem - take <= 1e-9:
            o["status"] = "executed"
        self._emit_fill(oid, {"ticker": o["ticker"], "side": o["side"]}, int(round(float(o["price"]) * 10000)),
                        take, is_taker=False)
        self._emit_user_order(o)

    def _emit_fill(self, oid, body, px, qty, is_taker):
        msg = {"trade_id": str(uuid.uuid4()), "order_id": oid, "market_ticker": body["ticker"], "is_taker": is_taker,
               "side": "yes" if body["side"] == "bid" else "no", "action": "buy", "yes_price_dollars": _d(px),
               "count_fp": _q(qty), "ts_ms": int(time.time() * 1000)}
        self.fills.append(msg)
        self._broadcast("fill", {"type": "fill", "msg": msg})

    def _emit_user_order(self, o):
        self._broadcast("user_orders", {"type": "user_order", "msg": dict(o)})

    def _broadcast(self, channel: str, frame: Dict[str, Any]) -> None:
        for conn in list(self._conns):
            if channel in conn["channels"]:
                conn["loop"].call_soon_threadsafe(conn["queue"].put_nowait, frame)

    # ------------------------------------------------------------------ WebSocket
    async def ws_handler(self, ws):
        path = ws.request.path.split("?", 1)[0]
        if path != WS_PATH or not self._verify({k.lower(): v for k, v in ws.request.headers.items()}, "GET", WS_PATH):
            await ws.close(code=4001, reason="unauthorized")
            return
        self.ws_connections += 1
        conn = {"channels": set(), "queue": asyncio.Queue(), "loop": asyncio.get_running_loop()}
        self._conns.append(conn)
        sid_counter = itertools.count(1)
        seq = {}
        sent = 0
        gapped: list = []
        active: set = set()

        async def send(frame, sid=None):
            nonlocal sent
            if sid is not None:
                seq[sid] = seq.get(sid, 0) + 1
                frame = {**frame, "sid": sid, "seq": seq[sid]}
            sent += 1
            f = self.fault
            if f.get("malformed_at") == sent and self.ws_connections == 1:
                await ws.send("{not json")
            if (f.get("gap_at") and sent >= f["gap_at"] and sid is not None and not gapped
                    and self.ws_connections == 1 and frame.get("type") == "orderbook_delta"):
                seq[sid] += 1                       # skip one sequence number
                frame = {**frame, "seq": seq[sid]}
                gapped.append(True)
            await ws.send(json.dumps(frame))
            if f.get("duplicate_every") and sent % f["duplicate_every"] == 0:
                await ws.send(json.dumps(frame))    # exact duplicate delivery
            if f.get("drop_after") and sent >= f["drop_after"] and self.ws_connections == 1:
                await ws.close()
                raise asyncio.CancelledError

        async def pump():
            while True:
                frame = await conn["queue"].get()
                await ws.send(json.dumps(frame))

        pumper = asyncio.create_task(pump())
        try:
            async for raw in ws:
                cmd = json.loads(raw)
                params = cmd.get("params", {})
                if cmd.get("cmd") == "unsubscribe":
                    active.difference_update(params["sids"])
                    await ws.send(json.dumps({"id": cmd["id"], "type": "unsubscribed", "sid": params["sids"][0]}))
                    continue
                if cmd.get("cmd") == "update_subscription":
                    await ws.send(json.dumps({"id": cmd["id"], "type": "ok", "msg": {}}))
                    continue
                if cmd.get("cmd") != "subscribe":
                    await ws.send(json.dumps({"id": cmd.get("id"), "type": "error", "msg": {"code": 5, "msg": "Unknown command"}}))
                    continue
                ch = params["channels"][0]
                sid = next(sid_counter)
                active.add(sid)
                conn["channels"].add(ch)
                await ws.send(json.dumps({"id": cmd["id"], "type": "subscribed", "msg": {"channel": ch, "sid": sid}}))
                tickers = params.get("market_tickers") or []
                if ch == "orderbook_delta":
                    for t in tickers:
                        m = self.markets.get(t)
                        if m is None:
                            await ws.send(json.dumps({"id": cmd["id"], "type": "error",
                                                      "msg": {"code": 16, "msg": "Market not found", "market_ticker": t}}))
                            continue
                        await send({"type": "orderbook_snapshot", "msg": {
                            "market_ticker": t, "market_id": str(uuid.uuid5(uuid.NAMESPACE_URL, t)),
                            "yes_dollars_fp": [[_d(k), _q(v)] for k, v in sorted(m["yes"].items())],
                            "no_dollars_fp": [[_d(k), _q(v)] for k, v in sorted(m["no"].items())]}}, sid)
                    asyncio.create_task(self._stream(send, sid, tickers, active))
                if ch == "trade":
                    asyncio.create_task(self._trades(send, tickers))
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            pumper.cancel()
            if conn in self._conns:
                self._conns.remove(conn)

    async def _stream(self, send, sid, tickers, active):
        try:
            for i in range(self.stream_deltas):
                await asyncio.sleep(0.01)
                if sid not in active:
                    return
                t = tickers[i % len(tickers)]
                m = self.markets[t]
                side = "yes" if i % 2 == 0 else "no"
                ladder = m[side]
                px = (max(ladder) if ladder else 3000) - (100 if i % 3 == 0 else 0)
                d = 5.0 if i % 4 != 3 else -min(ladder.get(px, 0.0), 5.0)
                if d == 0:
                    d = 5.0
                ladder[px] = ladder.get(px, 0.0) + d
                if ladder[px] <= 1e-9:
                    del ladder[px]
                await send({"type": "orderbook_delta", "msg": {
                    "market_ticker": t, "market_id": "", "price_dollars": _d(px), "delta_fp": _q(d), "side": side,
                    "ts_ms": int(time.time() * 1000)}}, sid)
        except Exception:
            pass

    async def _trades(self, send, tickers):
        try:
            for i in range(6):
                await asyncio.sleep(0.02)
                t = tickers[i % len(tickers)]
                await send({"type": "trade", "msg": {"trade_id": f"tr-{t}-{i}", "market_ticker": t,
                                                     "yes_price_dollars": "0.4300", "no_price_dollars": "0.5700",
                                                     "count_fp": "2.00", "taker_side": "yes" if i % 2 else "no",
                                                     "ts": int(time.time()), "ts_ms": int(time.time() * 1000)}})
        except Exception:
            pass

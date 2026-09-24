"""Kalshi WebSocket client (asyncio).

* Authenticated handshake (Kalshi requires auth even for public channels).
* Subscribes to ``orderbook_delta`` (snapshot + deltas), ``trade``, ``ticker``
  and ``market_lifecycle_v2`` for a set of markets; optional private channels
  (``fill``, ``user_orders``) for live trading.
* Every raw frame is handed to ``on_raw(text, recv_ns)`` *before* parsing so
  the recorder captures exactly what was received (for faithful replay).
* Sequence gaps on the order-book subscription trigger an automatic
  resubscribe, which makes the exchange send fresh snapshots.
* Reconnects with exponential backoff and jitter; reports status transitions
  through ``on_status`` so the risk engine can react to disconnects.

Architecture reference: the "raw frames first, parse later" recorder design is
inspired by ericsohel/KalshiTradingProject (MIT); no code was copied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from alphalab.core.book_manager import BookManager
from alphalab.kalshi.auth import KalshiSigner
from alphalab.kalshi.messages import parse_frame

log = logging.getLogger(__name__)

WS_PATH = "/trade-api/ws/v2"
PUBLIC_CHANNELS = ("orderbook_delta", "trade", "ticker", "market_lifecycle_v2")
PRIVATE_CHANNELS = ("fill", "user_orders")

RawHandler = Callable[[str, int], None]
EventHandler = Callable[[Any], Optional[Awaitable[None]]]
StatusHandler = Callable[[str, Dict[str, Any]], None]


async def _default_connect(url: str, headers: Dict[str, str]):
    import websockets
    return await websockets.connect(url, additional_headers=headers, ping_interval=10,
                                    ping_timeout=20, max_size=16 * 1024 * 1024, open_timeout=15)


class KalshiWebSocket:
    def __init__(self, url: str, signer: Optional[KalshiSigner], markets: Iterable[str],
                 channels: Iterable[str] = PUBLIC_CHANNELS,
                 on_event: Optional[EventHandler] = None,
                 on_raw: Optional[RawHandler] = None,
                 on_status: Optional[StatusHandler] = None,
                 connect: Callable[..., Awaitable[Any]] = _default_connect,
                 max_backoff_s: float = 30.0,
                 book_manager: Optional[BookManager] = None):
        self.url = url
        self.signer = signer
        self.markets: List[str] = list(dict.fromkeys(markets))
        self.channels = list(channels)
        self.on_event = on_event
        self.on_raw = on_raw
        self.on_status = on_status
        self._connect = connect
        self.max_backoff_s = max_backoff_s
        self.books = book_manager or BookManager()
        self._ws = None
        self._cmd_id = 0
        self._running = False
        self._sids: Dict[int, str] = {}           # sid -> channel
        self._pending: Dict[int, str] = {}         # cmd id -> channel
        self._resync_requested = False
        self._seen_trades: Set[str] = set()
        self._closed_sids: Set[int] = set()
        self._seen_order: "deque[str]" = deque()
        self.connected = False
        self.stats: Dict[str, Any] = {"messages": 0, "reconnects": 0, "gaps": 0, "duplicates": 0,
                                      "malformed": 0, "error_frames": 0, "by_type": {},
                                      "last_msg_ns": 0, "errors": 0, "connected_since": None,
                                      "connections": 0}

    # ------------------------------------------------------------------ public api
    async def run(self) -> None:
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, handshake failures, protocol errors
                self.stats["errors"] += 1
                log.warning("ws_session_error", extra={"fields": {"error": f"{type(exc).__name__}: {exc}"[:300]}})
            finally:
                if self.connected:
                    self.connected = False
                    self._emit_status("disconnected", {})
                self.books.reset_all()  # sids/seqs restart on the next connection
            if not self._running:
                break
            self.stats["reconnects"] += 1
            delay = min(self.max_backoff_s, backoff) * (0.5 + random.random())
            self._emit_status("reconnecting", {"delay_s": round(delay, 2)})
            await asyncio.sleep(delay)
            backoff = min(self.max_backoff_s, backoff * 2)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def add_markets(self, markets: Iterable[str]) -> None:
        new = [m for m in markets if m not in self.markets]
        if not new:
            return
        self.markets.extend(new)
        if self._ws is None:
            return
        for sid, ch in list(self._sids.items()):
            if ch in PRIVATE_CHANNELS:
                continue
            await self._send({"id": self._next_id(), "cmd": "update_subscription",
                              "params": {"sids": [sid], "market_tickers": new, "action": "add_markets"}})

    def request_resync(self) -> None:
        self._resync_requested = True

    # ------------------------------------------------------------------ internals
    def _next_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    def _emit_status(self, status: str, info: Dict[str, Any]) -> None:
        log.info("ws_status", extra={"fields": {"status": status, **info}})
        if self.on_status:
            try:
                self.on_status(status, info)
            except Exception:
                log.exception("ws_status_handler_failed")

    async def _send(self, obj: Dict[str, Any]) -> None:
        await self._ws.send(json.dumps(obj))

    async def _subscribe_all(self) -> None:
        self._sids.clear()
        self._closed_sids.clear()  # sid numbering restarts on a new connection
        self._pending.clear()
        for ch in self.channels:
            params: Dict[str, Any] = {"channels": [ch]}
            if self.markets:
                params["market_tickers"] = self.markets
            cid = self._next_id()
            self._pending[cid] = ch
            await self._send({"id": cid, "cmd": "subscribe", "params": params})

    async def _resubscribe_orderbook(self) -> None:
        self._resync_requested = False
        for sid, ch in list(self._sids.items()):
            if ch == "orderbook_delta":
                await self._send({"id": self._next_id(), "cmd": "unsubscribe", "params": {"sids": [sid]}})
                self._sids.pop(sid, None)
                self._closed_sids.add(sid)
                self.books.reset_sid(sid)
        cid = self._next_id()
        self._pending[cid] = "orderbook_delta"
        await self._send({"id": cid, "cmd": "subscribe",
                          "params": {"channels": ["orderbook_delta"], "market_tickers": self.markets}})
        self._emit_status("resubscribed", {"channel": "orderbook_delta"})

    async def _session(self) -> None:
        headers = self.signer.headers("GET", WS_PATH) if self.signer else {}
        self._ws = await self._connect(self.url, headers)
        self.connected = True
        self.stats["connections"] += 1
        self.stats["connected_since"] = time.time()
        self._emit_status("connected", {"markets": len(self.markets)})
        try:
            await self._subscribe_all()
            async for raw in self._ws:
                recv_ns = time.time_ns()
                if isinstance(raw, bytes):
                    raw = raw.decode()
                self.stats["messages"] += 1
                self.stats["last_msg_ns"] = recv_ns
                if self.on_raw:
                    self.on_raw(raw, recv_ns)
                await self._handle(raw, recv_ns)
                if self._resync_requested:
                    await self._resubscribe_orderbook()
                if not self._running:
                    break
        finally:
            ws, self._ws = self._ws, None
            try:
                await ws.close()
            except Exception:
                pass

    async def _handle(self, raw: str, recv_ns: int) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            self.stats["malformed"] += 1
            log.warning("ws_bad_json", extra={"fields": {"sample": raw[:120]}})
            return
        if not isinstance(frame, dict):
            self.stats["malformed"] += 1
            return
        typ = frame.get("type")
        if frame.get("sid") in self._closed_sids and typ not in ("subscribed", "unsubscribed", "ok", "error"):
            self.stats["stale_sid_frames"] = self.stats.get("stale_sid_frames", 0) + 1
            return  # in-flight frames for a subscription we already closed
        bt = self.stats["by_type"]
        bt[typ] = bt.get(typ, 0) + 1
        if typ == "subscribed":
            msg = frame.get("msg") or {}
            ch = msg.get("channel") or self._pending.pop(frame.get("id"), "")
            if msg.get("sid") is not None:
                self._sids[int(msg["sid"])] = ch
            return
        if typ == "error":
            self.stats["errors"] += 1
            self.stats["error_frames"] += 1
            log.warning("ws_error_frame", extra={"fields": {"msg": frame.get("msg")}})
            return
        if typ in ("ok", "unsubscribed"):
            return
        events = parse_frame(frame, recv_ns)
        for ev in events:
            if ev.kind == "trade" and ev.trade_id:
                if ev.trade_id in self._seen_trades:
                    self.stats["duplicates"] += 1
                    continue
                self._seen_trades.add(ev.trade_id)
                self._seen_order.append(ev.trade_id)
                if len(self._seen_order) > 50_000:
                    self._seen_trades.discard(self._seen_order.popleft())
            if ev.kind in ("snapshot", "delta"):
                before, dup_before = self.books.gaps, self.books.duplicates
                self.books.apply(ev)
                if self.books.duplicates > dup_before:
                    self.stats["duplicates"] += 1
                    continue  # never forward a duplicate to the strategy/recorder consumers
                if self.books.gaps > before:
                    self.stats["gaps"] += 1
                    self._emit_status("gap", {"sid": ev.sid, "seq": ev.seq})
                    self.request_resync()
            if self.on_event:
                res = self.on_event(ev)
                if asyncio.iscoroutine(res):
                    await res
        # Private channels (fill/user_orders) are passed through untouched.
        if typ in ("fill", "user_order", "user_orders") and self.on_event:
            res = self.on_event({"private": typ, "msg": frame.get("msg"), "recv_ns": recv_ns})
            if asyncio.iscoroutine(res):
                await res

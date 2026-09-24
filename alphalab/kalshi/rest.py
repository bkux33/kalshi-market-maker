"""Kalshi REST client (Trade API v2).

* Market-data GETs work without credentials; portfolio/order calls require a
  ``KalshiSigner``.
* Client-side throttle (token bucket) plus retry with exponential backoff on
  429 / 5xx / network errors.
* Unlike the upstream bot, errors are raised as ``KalshiAPIError`` instead of
  being printed and swallowed, so callers (and the risk engine) can react.
* Order-book responses are normalised from the fixed-point ``orderbook_fp``
  schema (and the legacy integer-cent ``orderbook`` schema) into integer price
  units, see ``alphalab.core.prices``.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from alphalab.core.prices import cents_to_units, dollars_to_units, parse_count
from alphalab.kalshi.auth import KalshiSigner

log = logging.getLogger(__name__)

Levels = List[Tuple[int, float]]


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(f"Kalshi API {status}: {message}")
        self.status = status
        self.body = body[:500]


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: Optional[float] = None, clock=time.monotonic):
        self.rate = rate_per_s
        self.capacity = burst or max(1.0, rate_per_s)
        self.tokens = self.capacity
        self.clock = clock
        self.last = clock()
        self._lock = threading.Lock()

    def acquire(self, sleep=time.sleep) -> None:
        with self._lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                sleep((1 - self.tokens) / self.rate)


def parse_orderbook_response(data: Dict[str, Any]) -> Tuple[Levels, Levels]:
    """Return (yes_bids, no_bids) as [(price_units, qty)] from either schema."""
    if "orderbook_fp" in data and data["orderbook_fp"] is not None:
        ob = data["orderbook_fp"]
        yes = [(dollars_to_units(p), parse_count(q)) for p, q in (ob.get("yes_dollars") or [])]
        no = [(dollars_to_units(p), parse_count(q)) for p, q in (ob.get("no_dollars") or [])]
        return yes, no
    ob = data.get("orderbook") or {}
    if ob.get("yes_dollars") or ob.get("no_dollars"):
        yes = [(dollars_to_units(p), parse_count(q)) for p, q in (ob.get("yes_dollars") or [])]
        no = [(dollars_to_units(p), parse_count(q)) for p, q in (ob.get("no_dollars") or [])]
        return yes, no
    yes = [(cents_to_units(p), float(q)) for p, q in (ob.get("yes") or [])]
    no = [(cents_to_units(p), float(q)) for p, q in (ob.get("no") or [])]
    return yes, no


class KalshiREST:
    def __init__(self, base_url: str, signer: Optional[KalshiSigner] = None,
                 requests_per_second: float = 8.0, timeout_s: float = 10.0,
                 max_retries: int = 4, transport: Optional[httpx.BaseTransport] = None,
                 sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self._path_prefix = urlparse(self.base_url).path
        self.signer = signer
        self.max_retries = max_retries
        self._sleep = sleep
        self._bucket = TokenBucket(requests_per_second)
        self._http = httpx.Client(timeout=timeout_s, transport=transport,
                                  headers={"Accept": "application/json",
                                           "User-Agent": "kalshi-alpha-lab/0.1"})

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                json_body: Optional[Dict[str, Any]] = None, auth: bool = False) -> Dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.base_url + path
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._bucket.acquire(self._sleep)
            headers = {}
            if auth:
                if self.signer is None:
                    raise KalshiAPIError(401, "credentials required for this endpoint")
                headers.update(self.signer.headers(method, self._path_prefix + path))
            try:
                resp = self._http.request(method, url, params=params, json=json_body, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("kalshi_rest_network_error", extra={"fields": {
                    "path": path, "attempt": attempt, "error": type(exc).__name__}})
                self._backoff(attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = KalshiAPIError(resp.status_code, "retryable", resp.text)
                self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                msg = resp.text
                try:
                    j = resp.json()
                    msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("message", msg)
                except Exception:
                    pass
                raise KalshiAPIError(resp.status_code, str(msg), resp.text)
            if not resp.content:
                return {}
            return resp.json()
        if isinstance(last_exc, KalshiAPIError):
            raise last_exc
        raise KalshiAPIError(0, f"request failed after retries: {type(last_exc).__name__}")

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(8.0, 0.25 * (2 ** attempt)) + random.random() * 0.1)

    # ------------------------------------------------------------------ market data
    def get_markets(self, **params: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        params.setdefault("limit", 1000)
        data = self.request("GET", "/markets", params=params)
        return data.get("markets") or [], (data.get("cursor") or None)

    def iter_markets(self, max_pages: int = 100, **params: Any) -> Iterator[Dict[str, Any]]:
        cursor = None
        for _ in range(max_pages):
            markets, cursor = self.get_markets(cursor=cursor, **params)
            yield from markets
            if not cursor or not markets:
                return

    def get_market(self, ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/markets/{ticker}").get("market") or {}

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> Dict[str, Any]:
        return self.request("GET", f"/events/{event_ticker}",
                            params={"with_nested_markets": str(with_nested_markets).lower()})

    def get_events(self, **params: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        params.setdefault("limit", 200)
        data = self.request("GET", "/events", params=params)
        return data.get("events") or [], (data.get("cursor") or None)

    def get_series(self, series_ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/series/{series_ticker}").get("series") or {}

    def get_orderbook(self, ticker: str, depth: int = 0) -> Tuple[Levels, Levels]:
        data = self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return parse_orderbook_response(data)

    def get_trades(self, ticker: Optional[str] = None, min_ts: Optional[int] = None,
                   max_ts: Optional[int] = None, limit: int = 1000,
                   cursor: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        data = self.request("GET", "/markets/trades", params={
            "ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit, "cursor": cursor})
        return data.get("trades") or [], (data.get("cursor") or None)

    def get_exchange_status(self) -> Dict[str, Any]:
        return self.request("GET", "/exchange/status")

    # ------------------------------------------------------------------ portfolio (auth)
    def get_balance(self) -> Dict[str, Any]:
        return self.request("GET", "/portfolio/balance", auth=True)

    def get_positions(self, **params: Any) -> List[Dict[str, Any]]:
        params.setdefault("limit", 1000)
        data = self.request("GET", "/portfolio/positions", params=params, auth=True)
        return data.get("market_positions") or []

    def get_orders(self, ticker: Optional[str] = None, status: Optional[str] = "resting",
                   limit: int = 1000) -> List[Dict[str, Any]]:
        data = self.request("GET", "/portfolio/orders",
                            params={"ticker": ticker, "status": status, "limit": limit}, auth=True)
        return data.get("orders") or []

    def get_fills(self, ticker: Optional[str] = None, min_ts: Optional[int] = None,
                  limit: int = 1000) -> List[Dict[str, Any]]:
        data = self.request("GET", "/portfolio/fills",
                            params={"ticker": ticker, "min_ts": min_ts, "limit": limit}, auth=True)
        return data.get("fills") or []

    # ------------------------------------------------------------------ orders (V2)
    # Current API (official SDK 3.30.0): event-market orders use the V2 shape on
    # /portfolio/events/orders with a single-book ``side`` (``bid`` buys YES, ``ask``
    # sells YES), fixed-point dollar ``price`` and fixed-point ``count`` strings.
    # The legacy POST /portfolio/orders shape is deprecated and not used.
    def create_order_v2(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/portfolio/events/orders", json_body=body, auth=True)

    def cancel_order_v2(self, order_id: str, market_ticker: Optional[str] = None,
                        subaccount: Optional[int] = None) -> Dict[str, Any]:
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}",
                            params={"market_ticker": market_ticker, "subaccount": subaccount}, auth=True)

    def batch_cancel_v2(self, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """``orders``: [{"order_id": ..., "market_ticker": ...}, ...]"""
        return self.request("DELETE", "/portfolio/events/orders/batched", json_body={"orders": orders}, auth=True)

    def cancel_all_orders_v2(self, subaccount: Optional[int] = None) -> Dict[str, Any]:
        return self.request("DELETE", "/portfolio/events/orders", params={"subaccount": subaccount}, auth=True)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self.request("GET", f"/portfolio/orders/{order_id}", auth=True).get("order") or {}

    # ------------------------------------------------------------------ series / events (discovery)
    def get_series_list(self, category: Optional[str] = None, include_volume: bool = True) -> List[Dict[str, Any]]:
        data = self.request("GET", "/series", params={"category": category,
                                                       "include_volume": str(include_volume).lower()})
        return data.get("series") or []

    def iter_events(self, max_pages: int = 20, **params: Any) -> Iterator[Dict[str, Any]]:
        cursor = None
        for _ in range(max_pages):
            events, cursor = self.get_events(cursor=cursor, **params)
            yield from events
            if not cursor or not events:
                return

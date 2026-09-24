"""Parse Kalshi WebSocket frames into normalised events.

Handles the fixed-point schema (``yes_dollars_fp``, ``price_dollars``,
``delta_fp``, ``count_fp``, ``yes_price_dollars``) and falls back to the legacy
integer-cent fields (``yes``, ``price``, ``delta``, ``count``, ``yes_price``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from alphalab.core.events import BookDelta, BookSnapshot, MarketStatus, Settlement, TradeEvent
from alphalab.core.prices import cents_to_units, dollars_to_units, parse_count

Event = Union[BookSnapshot, BookDelta, TradeEvent, MarketStatus, Settlement]


def _levels(msg: Dict[str, Any], fp_key: str, legacy_key: str):
    if msg.get(fp_key) is not None:
        return [(dollars_to_units(p), parse_count(q)) for p, q in msg[fp_key]]
    if msg.get(legacy_key) is not None:
        return [(cents_to_units(p), float(q)) for p, q in msg[legacy_key]]
    return []


def _ts_to_ns(ts: Any) -> Optional[int]:
    """Exchange timestamp -> epoch nanoseconds, without float precision loss.

    Accepts epoch seconds (int/float, e.g. ``1669149841``), epoch milliseconds
    (``ts_ms``-style ints >= 1e11), and ISO-8601 strings with up to nanosecond
    fractions (``2026-09-24T12:00:00.123456789Z``)."""
    if ts is None or ts == "" or isinstance(ts, bool):
        return None
    if isinstance(ts, int):
        return ts * 1_000_000 if ts >= 100_000_000_000 else ts * 1_000_000_000
    if isinstance(ts, float):
        if ts >= 1e11:
            return int(round(ts * 1_000_000))
        return int(round(ts * 1_000_000_000))
    return iso_to_ns(str(ts))


def iso_to_ns(text: str) -> Optional[int]:
    import re
    from datetime import timezone
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$", text.strip())
    if not m:
        return None
    base, frac, tz = m.groups()
    try:
        dt = datetime.fromisoformat(base + ("+00:00" if tz in (None, "Z") else tz))
    except ValueError:
        return None
    secs = int(dt.astimezone(timezone.utc).timestamp())
    nanos = int((frac or "0").ljust(9, "0"))
    return secs * 1_000_000_000 + nanos


def exch_ts_ns(msg: Dict[str, Any]) -> Optional[int]:
    """Matching-engine timestamp: ``ts_ms`` (current API) preferred over ``ts``."""
    if msg.get("ts_ms") is not None:
        try:
            return int(msg["ts_ms"]) * 1_000_000
        except (TypeError, ValueError):
            pass
    return _ts_to_ns(msg.get("ts"))


def _price(msg: Dict[str, Any], dollars_key: str, cents_key: str) -> Optional[int]:
    if msg.get(dollars_key) is not None:
        return dollars_to_units(msg[dollars_key])
    if msg.get(cents_key) is not None:
        return cents_to_units(msg[cents_key])
    return None


def _count(msg: Dict[str, Any], fp_key: str, legacy_key: str) -> float:
    if msg.get(fp_key) is not None:
        return parse_count(msg[fp_key])
    return float(msg.get(legacy_key) or 0)


def parse_frame(frame: Dict[str, Any], recv_ns: int) -> List[Event]:
    """Convert one decoded WS frame into zero or more events."""
    typ = frame.get("type")
    msg = frame.get("msg") or {}
    sid = frame.get("sid")
    seq = frame.get("seq")
    market = msg.get("market_ticker") or msg.get("ticker") or ""
    if typ == "orderbook_snapshot":
        return [BookSnapshot(recv_ns, market, _levels(msg, "yes_dollars_fp", "yes"),
                             _levels(msg, "no_dollars_fp", "no"), seq=seq, sid=sid)]
    if typ == "orderbook_delta":
        price = _price(msg, "price_dollars", "price")
        if price is None:
            return []
        delta = parse_count(msg["delta_fp"]) if msg.get("delta_fp") is not None else float(msg.get("delta") or 0)
        return [BookDelta(recv_ns, market, msg.get("side", "yes"), price, delta, seq=seq, sid=sid,
                          exch_ts_ns=exch_ts_ns(msg))]
    if typ == "trade":
        price = _price(msg, "yes_price_dollars", "yes_price")
        if price is None:
            return []
        return [TradeEvent(recv_ns, market, price, _count(msg, "count_fp", "count"),
                           msg.get("taker_side", ""), str(msg.get("trade_id", "")),
                           exch_ts_ns=exch_ts_ns(msg))]
    if typ == "market_lifecycle_v2":
        ev = msg.get("event_type", "")
        out: List[Event] = [MarketStatus(recv_ns, market, ev, dict(msg))]
        if ev in ("determined", "settled"):
            result = (msg.get("result") or "").lower()
            value = None
            if msg.get("settlement_value_dollars") is not None:
                value = float(msg["settlement_value_dollars"])
            elif msg.get("settlement_value") is not None:
                value = float(msg["settlement_value"]) / 100.0
            elif result in ("yes", "no"):
                value = 1.0 if result == "yes" else 0.0
            if value is not None:
                out.append(Settlement(recv_ns, market, value, result))
        return out
    return []

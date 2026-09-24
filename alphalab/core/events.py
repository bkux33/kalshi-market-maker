"""Normalised market events shared by the live feed, the recorder and replay.

Every event carries ``ts_ns``: the local receive time in nanoseconds since the
epoch (what a live strategy would actually have seen), and where available the
exchange timestamp ``exch_ts_ns``. Replay orders events by ``(ts_ns, order)``.

Trade semantics: ``taker_side == "yes"`` means the taker bought YES (lifted
YES asks, i.e. consumed NO bids); ``"no"`` means the taker bought NO (hit YES
bids). ``price`` is always the YES price in integer units.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Levels = List[Tuple[int, float]]


@dataclass(slots=True)
class BookSnapshot:
    ts_ns: int
    market: str
    yes: Levels
    no: Levels
    seq: Optional[int] = None
    sid: Optional[int] = None
    kind: str = "snapshot"


@dataclass(slots=True)
class BookDelta:
    ts_ns: int
    market: str
    side: str            # "yes" | "no" (which raw bid ladder)
    price: int           # units on that side's own ladder
    delta: float
    seq: Optional[int] = None
    sid: Optional[int] = None
    exch_ts_ns: Optional[int] = None
    kind: str = "delta"


@dataclass(slots=True)
class TradeEvent:
    ts_ns: int
    market: str
    price: int           # YES price units
    qty: float
    taker_side: str      # "yes" | "no"
    trade_id: str = ""
    exch_ts_ns: Optional[int] = None
    kind: str = "trade"


@dataclass(slots=True)
class MarketStatus:
    ts_ns: int
    market: str
    event_type: str      # created | activated | deactivated | close_date_updated | determined | settled | ...
    data: Dict[str, Any] = field(default_factory=dict)
    kind: str = "status"


@dataclass(slots=True)
class Settlement:
    ts_ns: int
    market: str
    value: float         # YES payout in dollars per contract (1.0, 0.0, or scalar value)
    result: str = ""     # "yes" | "no" | "scalar" | ""
    inferred: bool = False
    kind: str = "settlement"


@dataclass(slots=True)
class ExternalPrice:
    ts_ns: int
    symbol: str          # e.g. "BTC-USD"
    price: float
    source: str = ""
    bid: Optional[float] = None
    ask: Optional[float] = None
    kind: str = "external"


@dataclass(slots=True)
class Timer:
    ts_ns: int
    key: str
    owner: str = ""
    kind: str = "timer"


EVENT_ORDER = {"settlement": 0, "status": 1, "snapshot": 2, "delta": 3, "trade": 4, "external": 5, "timer": 6}

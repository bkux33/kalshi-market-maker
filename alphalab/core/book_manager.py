"""Maintains local order books from snapshot/delta events with gap detection.

Kalshi numbers ``orderbook_delta`` channel messages with a per-subscription
``seq``. If a sequence number is skipped the local books for that subscription
can no longer be trusted: every market on that ``sid`` is marked *unsynced*
and deltas are ignored until a fresh snapshot arrives (the live WS client
resubscribes to obtain one). Strategies must treat unsynced books as unusable.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Set

from alphalab.core.events import BookDelta, BookSnapshot
from alphalab.core.orderbook import OrderBook

log = logging.getLogger(__name__)


class BookManager:
    def __init__(self, on_gap: Optional[Callable[[int, int, int], None]] = None):
        self.books: Dict[str, OrderBook] = {}
        self.synced: Set[str] = set()
        self._sid_seq: Dict[int, int] = {}
        self._sid_markets: Dict[int, Set[str]] = {}
        self.gaps = 0
        self.ignored_deltas = 0
        self.on_gap = on_gap

    def book(self, market: str) -> OrderBook:
        ob = self.books.get(market)
        if ob is None:
            ob = self.books[market] = OrderBook(market)
        return ob

    def is_synced(self, market: str) -> bool:
        return market in self.synced

    def _check_seq(self, sid: Optional[int], seq: Optional[int]) -> bool:
        """Return False if a gap was detected for this subscription."""
        if sid is None or seq is None:
            return True
        last = self._sid_seq.get(sid)
        self._sid_seq[sid] = seq
        if last is not None and seq != last + 1:
            self.gaps += 1
            for m in self._sid_markets.get(sid, set()):
                self.synced.discard(m)
            log.warning("book_seq_gap", extra={"fields": {"sid": sid, "expected": last + 1, "got": seq}})
            if self.on_gap:
                self.on_gap(sid, last + 1, seq)
            return False
        return True

    def apply(self, ev) -> bool:
        """Apply a snapshot or delta. Returns True if the book changed and is synced."""
        if isinstance(ev, BookSnapshot):
            self._check_seq(ev.sid, ev.seq)
            self.book(ev.market).apply_snapshot(ev.yes, ev.no, ev.ts_ns, ev.seq)
            self.synced.add(ev.market)
            if ev.sid is not None:
                self._sid_markets.setdefault(ev.sid, set()).add(ev.market)
            return True
        if isinstance(ev, BookDelta):
            ok = self._check_seq(ev.sid, ev.seq)
            if not ok or ev.market not in self.synced:
                self.ignored_deltas += 1
                return False
            res = self.book(ev.market).apply_delta(ev.side, ev.price, ev.delta, ev.ts_ns, ev.seq)
            if res.clamped:
                # A delta that drives a level negative means our book diverged.
                self.synced.discard(ev.market)
                log.warning("book_negative_level", extra={"fields": {"market": ev.market, "price": ev.price}})
                return False
            return True
        return False

    def reset_sid(self, sid: int) -> None:
        self._sid_seq.pop(sid, None)

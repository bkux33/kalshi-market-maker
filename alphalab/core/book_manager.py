"""Maintains local order books from snapshot/delta events.

Sequence handling (Kalshi numbers ``orderbook_delta``-channel messages with a
per-subscription ``seq``):

* ``seq == last + 1``      -> normal, applied.
* ``seq <= last`` (delta)  -> duplicate / replayed message, **ignored** and counted.
* ``seq > last + 1``       -> gap: every market on that ``sid`` is marked
  *unsynced* and deltas are ignored until a fresh snapshot arrives (the live
  WS client re-requests one). Strategies must treat unsynced books as unusable.
* A snapshot always (re)starts sequence tracking for its ``sid``: after a
  reconnect or resubscribe the exchange restarts ``seq`` and sends snapshots
  first, so a snapshot with a lower ``seq`` is a new stream, not a duplicate.
* A snapshot without price levels (Kalshi sends these for closed markets,
  sometimes with an empty ``market_id``) is a valid empty book.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Set

from alphalab.core.events import BookDelta, BookSnapshot
from alphalab.core.orderbook import OrderBook

log = logging.getLogger(__name__)

OK, GAP, DUP = "ok", "gap", "dup"


class BookManager:
    def __init__(self, on_gap: Optional[Callable[[int, int, int], None]] = None):
        self.books: Dict[str, OrderBook] = {}
        self.synced: Set[str] = set()
        self._sid_seq: Dict[int, int] = {}
        self._sid_markets: Dict[int, Set[str]] = {}
        self.gaps = 0
        self.duplicates = 0
        self.ignored_deltas = 0
        self.snapshots = 0
        self.deltas_applied = 0
        self.on_gap = on_gap

    def book(self, market: str) -> OrderBook:
        ob = self.books.get(market)
        if ob is None:
            ob = self.books[market] = OrderBook(market)
        return ob

    def is_synced(self, market: str) -> bool:
        return market in self.synced

    def _check_seq(self, sid: Optional[int], seq: Optional[int]) -> str:
        if sid is None or seq is None:
            return OK
        last = self._sid_seq.get(sid)
        if last is not None and seq <= last:
            self.duplicates += 1
            return DUP
        self._sid_seq[sid] = seq
        if last is not None and seq != last + 1:
            self.gaps += 1
            for m in self._sid_markets.get(sid, set()):
                self.synced.discard(m)
            log.warning("book_seq_gap", extra={"fields": {"sid": sid, "expected": last + 1, "got": seq}})
            if self.on_gap:
                self.on_gap(sid, last + 1, seq)
            return GAP
        return OK

    def apply(self, ev) -> bool:
        """Apply a snapshot or delta. Returns True if the book changed and is synced."""
        if isinstance(ev, BookSnapshot):
            if ev.sid is not None and ev.seq is not None:
                last = self._sid_seq.get(ev.sid)
                if last is not None and ev.seq <= last:
                    # sequence restarted (reconnect / resubscribe): new stream begins here
                    self._sid_seq.pop(ev.sid, None)
                status = self._check_seq(ev.sid, ev.seq)
                if status == GAP:
                    pass  # the snapshot itself is complete; it re-syncs its own market below
            self.book(ev.market).apply_snapshot(ev.yes, ev.no, ev.ts_ns, ev.seq)
            self.synced.add(ev.market)
            self.snapshots += 1
            if ev.sid is not None:
                self._sid_markets.setdefault(ev.sid, set()).add(ev.market)
            return True
        if isinstance(ev, BookDelta):
            status = self._check_seq(ev.sid, ev.seq)
            if status == DUP:
                return False
            if status == GAP or ev.market not in self.synced:
                self.ignored_deltas += 1
                return False
            res = self.book(ev.market).apply_delta(ev.side, ev.price, ev.delta, ev.ts_ns, ev.seq)
            if res.clamped:
                # A delta that drives a level negative means our book diverged.
                self.synced.discard(ev.market)
                log.warning("book_negative_level", extra={"fields": {"market": ev.market, "price": ev.price}})
                return False
            self.deltas_applied += 1
            return True
        return False

    def reset_sid(self, sid: int) -> None:
        self._sid_seq.pop(sid, None)

    def reset_all(self) -> None:
        """Forget sequence state (call on disconnect: sids/seqs restart on the next connection)."""
        self._sid_seq.clear()
        self._sid_markets.clear()
        self.synced.clear()

    def stats(self) -> Dict[str, int]:
        return {"snapshots": self.snapshots, "deltas_applied": self.deltas_applied, "gaps": self.gaps,
                "duplicates": self.duplicates, "ignored_deltas": self.ignored_deltas,
                "books": len(self.books), "synced_books": len(self.synced)}

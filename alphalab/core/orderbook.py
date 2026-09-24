"""Local L2 order book for a single Kalshi binary market.

Kalshi's book is bids-only on both sides: YES bids and NO bids. A NO bid at x
is equivalent to a YES ask at ``1 - x``. This class stores the two raw bid
ladders exactly as the exchange sends them (so snapshots and deltas apply
without translation) and exposes a unified YES-denominated view:

  * ``best_bid``  = highest YES bid
  * ``best_ask``  = 1 - highest NO bid
  * ``bids(n)``   = YES bids, best first
  * ``asks(n)``   = YES asks (derived from NO bids), best first

The conversion idea follows ``src/orderbook.py`` of zachdaube/kalshi-market-maker
(Apache-2.0); the implementation here is rewritten for fixed-point prices,
incremental deltas and sequence tracking.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from alphalab.core.prices import PRICE_SCALE, complement

Level = Tuple[int, float]  # (price_units, quantity)

_EPS = 1e-9


@dataclass
class DeltaResult:
    new_qty: float
    clamped: bool  # True if the delta would have made the level negative


class OrderBook:
    __slots__ = ("ticker", "yes", "no", "seq", "ts_ns", "n_updates", "inconsistencies")

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes: Dict[int, float] = {}
        self.no: Dict[int, float] = {}
        self.seq: Optional[int] = None
        self.ts_ns: int = 0
        self.n_updates: int = 0
        self.inconsistencies: int = 0

    # ------------------------------------------------------------------ updates
    def apply_snapshot(self, yes_levels: Iterable[Sequence], no_levels: Iterable[Sequence],
                       ts_ns: int, seq: Optional[int] = None) -> None:
        self.yes = {int(p): float(q) for p, q in yes_levels if float(q) > _EPS}
        self.no = {int(p): float(q) for p, q in no_levels if float(q) > _EPS}
        self.seq = seq
        self.ts_ns = ts_ns
        self.n_updates += 1

    def apply_delta(self, side: str, price: int, delta: float, ts_ns: int,
                    seq: Optional[int] = None) -> DeltaResult:
        ladder = self.yes if side == "yes" else self.no
        new_qty = ladder.get(price, 0.0) + delta
        clamped = False
        if new_qty < -_EPS:
            clamped = True
            self.inconsistencies += 1
        if new_qty <= _EPS:
            ladder.pop(price, None)
            new_qty = 0.0
        else:
            ladder[price] = new_qty
        if seq is not None:
            self.seq = seq
        self.ts_ns = ts_ns
        self.n_updates += 1
        return DeltaResult(new_qty, clamped)

    def clear(self) -> None:
        self.yes.clear()
        self.no.clear()
        self.seq = None

    # ------------------------------------------------------------------ views
    def best_bid(self) -> Optional[Level]:
        if not self.yes:
            return None
        p = max(self.yes)
        return p, self.yes[p]

    def best_ask(self) -> Optional[Level]:
        if not self.no:
            return None
        p = max(self.no)
        return complement(p), self.no[p]

    @property
    def bid_price(self) -> Optional[int]:
        return max(self.yes) if self.yes else None

    @property
    def ask_price(self) -> Optional[int]:
        return complement(max(self.no)) if self.no else None

    def mid(self) -> Optional[float]:
        b, a = self.bid_price, self.ask_price
        if b is None or a is None:
            return None
        return (b + a) / 2.0

    def spread(self) -> Optional[int]:
        b, a = self.bid_price, self.ask_price
        if b is None or a is None:
            return None
        return a - b

    def bids(self, n: Optional[int] = None) -> List[Level]:
        levels = sorted(self.yes.items(), key=lambda kv: -kv[0])
        return levels if n is None else levels[:n]

    def asks(self, n: Optional[int] = None) -> List[Level]:
        levels = sorted(((complement(p), q) for p, q in self.no.items()), key=lambda kv: kv[0])
        return levels if n is None else levels[:n]

    def depth(self, side: str, levels: int = 5) -> float:
        """Total quantity in the best ``levels`` levels of the YES ``bid``/``ask`` side."""
        book = self.bids(levels) if side == "bid" else self.asks(levels)
        return float(sum(q for _, q in book))

    def depth_within(self, side: str, ticks_units: int) -> float:
        """Quantity within ``ticks_units`` price units of the best price."""
        if side == "bid":
            best = self.bid_price
            if best is None:
                return 0.0
            return float(sum(q for p, q in self.yes.items() if p >= best - ticks_units))
        best = self.ask_price
        if best is None:
            return 0.0
        return float(sum(q for p, q in self.no.items() if complement(p) <= best + ticks_units))

    def imbalance(self, levels: int = 1) -> Optional[float]:
        """Bid share of visible depth in the top ``levels``: 1 = all bids, 0 = all asks."""
        bd, ad = self.depth("bid", levels), self.depth("ask", levels)
        if bd + ad <= _EPS:
            return None
        return bd / (bd + ad)

    def microprice(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        (bp, bq), (ap, aq) = bb, ba
        if bq + aq <= _EPS:
            return (bp + ap) / 2.0
        return (ap * bq + bp * aq) / (bq + aq)

    def qty_at(self, side: str, price: int) -> float:
        """Resting quantity at a YES-book price on the ``bid`` or ``ask`` side."""
        if side == "bid":
            return self.yes.get(price, 0.0)
        return self.no.get(complement(price), 0.0)

    def is_crossed(self) -> bool:
        b, a = self.bid_price, self.ask_price
        return b is not None and a is not None and b >= a

    def is_empty(self) -> bool:
        return not self.yes and not self.no

    def is_two_sided(self) -> bool:
        return bool(self.yes) and bool(self.no)

    # ------------------------------------------------------------------ execution
    def sweep(self, action: str, qty: float, limit: Optional[int] = None,
              consumed: Optional[Dict[Tuple[str, int], float]] = None) -> List[Level]:
        """Walk the opposite side for a marketable order and return the fills.

        ``action`` is ``buy`` (take YES asks) or ``sell`` (hit YES bids).
        ``limit`` is a YES price bound (max price to pay / min price to receive).
        ``consumed`` lets a simulator subtract liquidity it already took so the
        same displayed size cannot be filled twice. Does not mutate the book.
        """
        fills: List[Level] = []
        remaining = qty
        if action == "buy":
            levels = self.asks()
            key_side = "ask"
        else:
            levels = self.bids()
            key_side = "bid"
        for price, avail in levels:
            if remaining <= _EPS:
                break
            if limit is not None:
                if action == "buy" and price > limit:
                    break
                if action == "sell" and price < limit:
                    break
            if consumed:
                avail = avail - consumed.get((key_side, price), 0.0)
            if avail <= _EPS:
                continue
            take = min(avail, remaining)
            fills.append((price, take))
            remaining -= take
        return fills

    # ------------------------------------------------------------------ misc
    def copy(self) -> "OrderBook":
        ob = OrderBook(self.ticker)
        ob.yes = dict(self.yes)
        ob.no = dict(self.no)
        ob.seq = self.seq
        ob.ts_ns = self.ts_ns
        ob.n_updates = self.n_updates
        ob.inconsistencies = self.inconsistencies
        return ob

    def state_hash(self) -> str:
        h = hashlib.sha256()
        for p, q in sorted(self.yes.items()):
            h.update(f"y{p}:{q:.6f};".encode())
        for p, q in sorted(self.no.items()):
            h.update(f"n{p}:{q:.6f};".encode())
        return h.hexdigest()[:16]

    def to_dict(self, levels: int = 10) -> dict:
        return {
            "ticker": self.ticker,
            "ts_ns": self.ts_ns,
            "seq": self.seq,
            "bids": [[p / PRICE_SCALE, q] for p, q in self.bids(levels)],
            "asks": [[p / PRICE_SCALE, q] for p, q in self.asks(levels)],
            "mid": None if self.mid() is None else self.mid() / PRICE_SCALE,
            "spread": None if self.spread() is None else self.spread() / PRICE_SCALE,
            "imbalance": self.imbalance(5),
            "microprice": None if self.microprice() is None else self.microprice() / PRICE_SCALE,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"OrderBook({self.ticker} bid={self.bid_price} ask={self.ask_price} "
                f"levels={len(self.yes)}/{len(self.no)} seq={self.seq})")

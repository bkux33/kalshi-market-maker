"""Positions, cash, realised/unrealised P&L and round-trip bookkeeping.

Positions are signed YES contracts per (strategy, market): +10 = long 10 YES,
-10 = short 10 YES, which on Kalshi is held as long 10 NO. P&L is identical
either way (short YES at p == long NO at 1-p), but *capital at risk* differs,
and ``capital_at_risk`` reports the true maximum loss:
  long:  qty * avg_price          (YES goes to 0)
  short: qty * (1 - avg_price)    (YES goes to 1)

A *round trip* is one position episode: from flat, through any number of
fills, back to flat (or to settlement). Trade-level statistics (win rate,
expectancy, profit factor, holding period) are computed on round trips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from alphalab.core.prices import PRICE_SCALE

_EPS = 1e-9
Key = Tuple[str, str]  # (strategy, market)


@dataclass
class RoundTrip:
    strategy: str
    market: str
    direction: int
    qty: float = 0.0              # max absolute size reached
    entry_ns: int = 0
    exit_ns: int = 0
    entry_notional: float = 0.0   # $ paid/received opening
    entry_qty: float = 0.0
    exit_notional: float = 0.0
    exit_qty: float = 0.0
    gross: float = 0.0
    fees: float = 0.0
    exit_reason: str = ""

    @property
    def net(self) -> float:
        return self.gross - self.fees

    @property
    def entry_price(self) -> float:
        return self.entry_notional / self.entry_qty if self.entry_qty else 0.0

    @property
    def exit_price(self) -> float:
        return self.exit_notional / self.exit_qty if self.exit_qty else 0.0


@dataclass
class Position:
    qty: float = 0.0
    avg_price: float = 0.0        # dollars, average entry price of the open quantity
    realized: float = 0.0         # gross realised $
    fees: float = 0.0
    open_trip: Optional[RoundTrip] = None


@dataclass
class Portfolio:
    positions: Dict[Key, Position] = field(default_factory=dict)
    cash: float = 0.0
    fees_paid: float = 0.0
    trips: List[RoundTrip] = field(default_factory=list)
    traded_notional: float = 0.0

    def pos(self, strategy: str, market: str) -> Position:
        k = (strategy, market)
        p = self.positions.get(k)
        if p is None:
            p = self.positions[k] = Position()
        return p

    def market_qty(self, market: str) -> float:
        return sum(p.qty for (s, m), p in self.positions.items() if m == market)

    def on_fill(self, strategy: str, market: str, action: str, price_units: int, qty: float,
                fee: float, ts_ns: int) -> float:
        """Apply a fill; returns realised gross P&L from this fill."""
        price = price_units / PRICE_SCALE
        signed = qty if action == "buy" else -qty
        self.cash -= signed * price
        self.cash -= fee
        self.fees_paid += fee
        self.traded_notional += qty * price
        p = self.pos(strategy, market)
        p.fees += fee
        realized = 0.0
        if p.open_trip is None:
            p.open_trip = RoundTrip(strategy, market, 1 if signed > 0 else -1, entry_ns=ts_ns)
        trip = p.open_trip
        trip.fees += fee
        if abs(p.qty) < _EPS or (p.qty > 0) == (signed > 0):
            # opening / adding
            new_qty = p.qty + signed
            p.avg_price = (abs(p.qty) * p.avg_price + qty * price) / abs(new_qty)
            p.qty = new_qty
            trip.entry_notional += qty * price
            trip.entry_qty += qty
            trip.qty = max(trip.qty, abs(p.qty))
        else:
            closing = min(qty, abs(p.qty))
            direction = 1 if p.qty > 0 else -1
            realized = direction * (price - p.avg_price) * closing
            p.realized += realized
            trip.gross += realized
            trip.exit_notional += closing * price
            trip.exit_qty += closing
            p.qty -= direction * closing
            leftover = qty - closing
            if leftover > _EPS:
                trip.fees -= fee * leftover / qty  # the flip's opening share belongs to the new trip
            if abs(p.qty) < _EPS:
                p.qty = 0.0
                p.avg_price = 0.0
                trip.exit_ns = ts_ns
                trip.exit_reason = trip.exit_reason or "flat"
                self.trips.append(trip)
                p.open_trip = None
            if leftover > _EPS:
                # position flipped: start a new trip with the remainder
                p.qty = leftover if signed > 0 else -leftover
                p.avg_price = price
                p.open_trip = RoundTrip(strategy, market, 1 if signed > 0 else -1, qty=leftover,
                                        entry_ns=ts_ns, entry_notional=leftover * price, entry_qty=leftover,
                                        fees=fee * leftover / qty)
        return realized

    def settle(self, market: str, value: float, ts_ns: int, reason: str = "settlement") -> float:
        """Settle every strategy's position in ``market`` at ``value`` $/YES contract."""
        total = 0.0
        for (s, m), p in list(self.positions.items()):
            if m != market or abs(p.qty) < _EPS:
                continue
            realized = (value - p.avg_price) * p.qty
            self.cash += p.qty * value
            p.realized += realized
            total += realized
            if p.open_trip is not None:
                t = p.open_trip
                t.gross += realized
                t.exit_notional += abs(p.qty) * value
                t.exit_qty += abs(p.qty)
                t.exit_ns = ts_ns
                t.exit_reason = reason
                self.trips.append(t)
                p.open_trip = None
            p.qty = 0.0
            p.avg_price = 0.0
        return total

    def unrealized(self, marks: Dict[str, float]) -> float:
        u = 0.0
        for (s, m), p in self.positions.items():
            if abs(p.qty) > _EPS and m in marks and marks[m] is not None:
                u += (marks[m] - p.avg_price) * p.qty
        return u

    def realized(self) -> float:
        return sum(p.realized for p in self.positions.values())

    def capital_at_risk(self, market: Optional[str] = None, strategy: Optional[str] = None) -> float:
        tot = 0.0
        for (s, m), p in self.positions.items():
            if (market and m != market) or (strategy and s != strategy) or abs(p.qty) < _EPS:
                continue
            tot += p.qty * p.avg_price if p.qty > 0 else -p.qty * (1.0 - p.avg_price)
        return tot

    def open_positions(self) -> Dict[Key, Position]:
        return {k: p for k, p in self.positions.items() if abs(p.qty) > _EPS}

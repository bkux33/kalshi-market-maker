"""Kalshi fee engine.

Kalshi's general fee schedule charges, per order execution,

    fee = roundup_to_cent( rate * C * P * (1 - P) ) * fee_multiplier

where ``C`` is contracts, ``P`` the execution price in dollars. The taker rate
is 0.07. Series whose ``fee_type`` is ``quadratic_with_maker_fees`` also charge
makers at 0.0175; plain ``quadratic`` series charge makers nothing. A series'
``fee_multiplier`` scales the result (e.g. index series with reduced fees).
``flat`` series use a per-contract fee from the specific-fees table, which has
to be configured by hand.

Because rates and rounding can change, everything here is configurable, and
the *default is conservative*: if a series' fee type is unknown, makers are
charged 0.0175. Research results therefore understate rather than overstate
maker edges. Always check https://kalshi.com/docs/kalshi-fee-schedule.pdf.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, Optional

from alphalab.core.prices import PRICE_SCALE

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


@dataclass(frozen=True)
class FeeSchedule:
    taker_rate: float = TAKER_RATE
    maker_rate: float = MAKER_RATE
    multiplier: float = 1.0
    rounding: str = "ceil_cent"          # "ceil_cent" (per fill) or "none"
    flat_per_contract: Optional[float] = None  # dollars; only for fee_type == "flat"

    @classmethod
    def from_series(cls, fee_type: Optional[str], fee_multiplier: Optional[float],
                    base: "FeeSchedule") -> "FeeSchedule":
        mult = base.multiplier if fee_multiplier is None else float(fee_multiplier)
        if fee_type == "quadratic":
            return replace(base, maker_rate=0.0, multiplier=mult)
        if fee_type in ("quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"):
            # combo-maker schedules charge makers on (at least) some markets: stay conservative
            return replace(base, maker_rate=MAKER_RATE, multiplier=mult)
        return replace(base, multiplier=mult)


def _round(value: float, mode: str) -> float:
    if mode == "ceil_cent":
        # Guard against float noise such as 0.0700000001 -> 0.08
        return math.ceil(round(value * 100.0, 9)) / 100.0
    return value


@dataclass
class FeeModel:
    """Resolves the fee schedule per market and computes fees in dollars."""

    default: FeeSchedule = field(default_factory=FeeSchedule)
    series_overrides: Dict[str, FeeSchedule] = field(default_factory=dict)
    stress_multiplier: float = 1.0  # e.g. 2.0 for "does it survive 2x fees?"

    def schedule_for(self, market_ticker: str) -> FeeSchedule:
        series = market_ticker.split("-", 1)[0]
        return self.series_overrides.get(series, self.default)

    def register_series(self, series_ticker: str, fee_type: Optional[str],
                        fee_multiplier: Optional[float]) -> None:
        self.series_overrides[series_ticker] = FeeSchedule.from_series(
            fee_type, fee_multiplier, self.default)

    def fee(self, market_ticker: str, price_units: int, qty: float, is_taker: bool) -> float:
        """Fee in dollars for one execution of ``qty`` contracts at ``price_units``."""
        if qty <= 0:
            return 0.0
        s = self.schedule_for(market_ticker)
        if s.flat_per_contract is not None:
            raw = s.flat_per_contract * qty
        else:
            p = price_units / PRICE_SCALE
            rate = s.taker_rate if is_taker else s.maker_rate
            raw = rate * qty * p * (1.0 - p)
        fee = _round(raw * s.multiplier, s.rounding) if raw > 0 else 0.0
        return fee * self.stress_multiplier

    def with_stress(self, multiplier: float) -> "FeeModel":
        return FeeModel(self.default, dict(self.series_overrides), multiplier)

    def round_trip(self, market_ticker: str, entry_units: int, exit_units: int, qty: float,
                   entry_taker: bool, exit_taker: bool) -> float:
        return (self.fee(market_ticker, entry_units, qty, entry_taker)
                + self.fee(market_ticker, exit_units, qty, exit_taker))

    def breakeven_move_units(self, market_ticker: str, price_units: int,
                             entry_taker: bool = True, exit_taker: bool = True,
                             qty: float = 1.0) -> float:
        """Price move (units) needed to cover a round trip's fees at ``price_units``."""
        fees = self.round_trip(market_ticker, price_units, price_units, qty, entry_taker, exit_taker)
        return fees / qty * PRICE_SCALE


def fee_per_contract(price_units: int, rate: float = TAKER_RATE) -> float:
    """Unrounded fee per contract in dollars (useful for analytics)."""
    p = price_units / PRICE_SCALE
    return rate * p * (1.0 - p)

"""Fixed-point price and quantity helpers.

Kalshi publishes prices as fixed-point dollar strings (``"0.4500"``) and counts
as fixed-point strings (``"12.00"``). Some markets trade in deci-cent ticks, so
prices cannot be assumed to be whole cents.

Internally every price is an ``int`` in *units* of $0.0001 (``PRICE_SCALE``
units per dollar). One cent is 100 units; $1.00 (a settled YES contract) is
10_000 units. Integer units make dictionary keys exact and arithmetic
deterministic. Quantities are floats because fractional trading exists on some
markets; most markets trade whole contracts.

All prices are expressed on the YES book unless a name says otherwise:
  * a YES bid at p is a resting buy of YES at p;
  * a NO bid at x is economically a YES ask at ``PRICE_SCALE - x``.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

PRICE_SCALE = 10_000
CENT = PRICE_SCALE // 100
MIN_PRICE = CENT          # $0.01
MAX_PRICE = PRICE_SCALE - CENT  # $0.99

Number = Union[int, float, str, Decimal]


def dollars_to_units(value: Number) -> int:
    """Convert a dollar amount (``"0.4500"``, ``0.45``) to integer price units."""
    d = Decimal(str(value)) * PRICE_SCALE
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def cents_to_units(value: Number) -> int:
    """Convert a legacy integer-cent price (``45``) to integer price units."""
    d = Decimal(str(value)) * CENT
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def units_to_dollars(units: int) -> float:
    return units / PRICE_SCALE


def units_to_cents(units: int) -> float:
    return units / CENT


def parse_count(value: Number) -> float:
    """Parse a fixed-point count string (``"136.00"``) or number."""
    return float(Decimal(str(value)))


def complement(units: int) -> int:
    """Price of the opposite side: YES at p is NO at 1-p."""
    return PRICE_SCALE - units


def is_valid_price(units: int) -> bool:
    return MIN_PRICE <= units <= MAX_PRICE


def fmt_price(units: int | None) -> str:
    if units is None:
        return "-"
    cents = units / CENT
    return f"{cents:.1f}c" if units % CENT else f"{int(cents)}c"

"""Market discovery and metadata normalisation.

Discovery chooses *what to record*; it makes no claim about profitability.
The ranking heuristics (spread, activity, time to close) are adapted from the
scanner in zachdaube/kalshi-market-maker (Apache-2.0) but only used to pick
recording targets.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from alphalab.core.prices import dollars_to_units, cents_to_units


def _iso_to_ts(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _f(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _price_units(m: Dict[str, Any], dollars_key: str, cents_key: str) -> Optional[int]:
    if m.get(dollars_key) not in (None, ""):
        return dollars_to_units(m[dollars_key])
    if m.get(cents_key) not in (None, ""):
        return cents_to_units(m[cents_key])
    return None


@dataclass
class MarketMeta:
    ticker: str
    event_ticker: str = ""
    series_ticker: str = ""
    title: str = ""
    status: str = ""
    market_type: str = "binary"
    open_ts: Optional[float] = None
    close_ts: Optional[float] = None
    expected_expiration_ts: Optional[float] = None
    strike_type: Optional[str] = None
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None
    result: Optional[str] = None
    settlement_value: Optional[float] = None
    price_level_structure: Optional[str] = None
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    volume: Optional[float] = None
    volume_24h: Optional[float] = None
    open_interest: Optional[float] = None
    liquidity_usd: Optional[float] = None
    can_close_early: Optional[bool] = None
    mutually_exclusive: Optional[bool] = None
    fee_type: Optional[str] = None
    fee_multiplier: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def spread(self) -> Optional[int]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def seconds_to_close(self, now: Optional[float] = None) -> Optional[float]:
        if self.close_ts is None:
            return None
        return self.close_ts - (now if now is not None else time.time())

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("extra")
        return d


def series_from_event(event_ticker: str) -> str:
    return event_ticker.split("-", 1)[0] if event_ticker else ""


def market_from_api(m: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> MarketMeta:
    ev_t = m.get("event_ticker") or (event or {}).get("event_ticker", "")
    settle = _f(m.get("settlement_value_dollars"))
    if settle is None and m.get("settlement_value") not in (None, ""):
        settle = float(m["settlement_value"]) / 100.0
    return MarketMeta(
        ticker=m.get("ticker", ""),
        event_ticker=ev_t,
        series_ticker=(event or {}).get("series_ticker") or series_from_event(ev_t),
        title=m.get("title") or m.get("yes_sub_title") or "",
        status=m.get("status", ""),
        market_type=m.get("market_type", "binary"),
        open_ts=_iso_to_ts(m.get("open_time")),
        close_ts=_iso_to_ts(m.get("close_time")),
        expected_expiration_ts=_iso_to_ts(m.get("expected_expiration_time")),
        strike_type=m.get("strike_type"),
        floor_strike=_f(m.get("floor_strike")),
        cap_strike=_f(m.get("cap_strike")),
        result=(m.get("result") or None),
        settlement_value=settle,
        price_level_structure=m.get("price_level_structure"),
        yes_bid=_price_units(m, "yes_bid_dollars", "yes_bid"),
        yes_ask=_price_units(m, "yes_ask_dollars", "yes_ask"),
        volume=_f(m.get("volume_fp", m.get("volume"))),
        volume_24h=_f(m.get("volume_24h_fp", m.get("volume_24h"))),
        open_interest=_f(m.get("open_interest_fp", m.get("open_interest"))),
        liquidity_usd=_f(m.get("liquidity_dollars")),
        can_close_early=m.get("can_close_early"),
        mutually_exclusive=(event or {}).get("mutually_exclusive"),
    )


_CRYPTO15 = re.compile(r"^(KX(?:BTC|ETH|SOL|XRP|DOGE)15M)-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
                                        "SEP", "OCT", "NOV", "DEC"], start=1)}


def crypto15m_close_from_ticker(ticker: str) -> Optional[float]:
    """Parse the close time embedded in 15-minute crypto tickers.

    ``KXETH15M-26MAR021500-00`` closes 2026-03-02 15:00 US/Eastern. Returns UTC
    epoch seconds, or None if the ticker does not match.
    """
    m = _CRYPTO15.match(ticker)
    if not m:
        return None
    _, yy, mon, dd, hh, mi = m.groups()
    try:
        from zoneinfo import ZoneInfo
        dt = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mi), tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        return None
    return dt.astimezone(timezone.utc).timestamp()


_STRIKE = re.compile(r"\$\s?([0-9][0-9,]*\.?[0-9]*)")


def strike_from_text(text: str) -> Optional[float]:
    """Fallback strike parser for titles like 'BTC price at 3:15pm above $65,434.98'."""
    m = _STRIKE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def rank_for_recording(markets: Iterable[MarketMeta], now: Optional[float] = None,
                       min_seconds_to_close: float = 300, max_seconds_to_close: float = 30 * 86400,
                       min_volume_24h: float = 0) -> List[MarketMeta]:
    """Filter to open, two-sided markets and order by activity (volume, then OI)."""
    now = now or time.time()
    out = []
    for m in markets:
        if m.status not in ("open", "active", ""):
            continue
        ttc = m.seconds_to_close(now)
        if ttc is None or ttc < min_seconds_to_close or ttc > max_seconds_to_close:
            continue
        if m.yes_bid is None or m.yes_ask is None or m.yes_bid <= 0 or m.yes_ask >= 10_000:
            continue
        if (m.volume_24h or 0) < min_volume_24h:
            continue
        out.append(m)
    out.sort(key=lambda m: (-(m.volume_24h or 0), -(m.open_interest or 0), m.ticker))
    return out


def discover(rest, series: Iterable[str] = (), status: str = "open", max_pages: int = 20,
             with_events: bool = False) -> List[MarketMeta]:
    """Fetch markets for the given series (or all open markets if none)."""
    out: List[MarketMeta] = []
    series = list(series)
    if series:
        for s in series:
            for m in rest.iter_markets(max_pages=max_pages, series_ticker=s, status=status):
                out.append(market_from_api(m))
    else:
        for m in rest.iter_markets(max_pages=max_pages, status=status, mve_filter="exclude"):
            out.append(market_from_api(m))
    if with_events:
        cache: Dict[str, Dict[str, Any]] = {}
        for mm in out:
            if mm.event_ticker and mm.event_ticker not in cache:
                try:
                    cache[mm.event_ticker] = rest.get_event(mm.event_ticker, with_nested_markets=False).get("event") or {}
                except Exception:
                    cache[mm.event_ticker] = {}
            ev = cache.get(mm.event_ticker) or {}
            mm.mutually_exclusive = ev.get("mutually_exclusive")
            if ev.get("series_ticker"):
                mm.series_ticker = ev["series_ticker"]
    return out

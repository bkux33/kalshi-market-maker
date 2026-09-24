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
    category: Optional[str] = None
    exchange_index: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def activity(self) -> float:
        """Volume measure: 24h volume if the API still returns it, else lifetime volume."""
        return float(self.volume_24h if self.volume_24h is not None else (self.volume or 0.0))

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
        for k in ("extra", "category", "exchange_index"):
            d.pop(k)
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
        category=(event or {}).get("category"),
        exchange_index=m.get("exchange_index"),
        extra={k: m.get(k) for k in ("yes_sub_title", "no_sub_title", "subtitle") if m.get(k)},
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
        if m.activity < min_volume_24h:
            continue
        out.append(m)
    out.sort(key=lambda m: (-m.activity, -(m.open_interest or 0), m.ticker))
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


def series_categories(rest) -> Dict[str, str]:
    """series_ticker -> category, from GET /series (category lives on the series, not the market)."""
    try:
        return {s.get("ticker"): s.get("category") or "" for s in rest.get_series_list(include_volume=False)
                if s.get("ticker")}
    except Exception:
        return {}


def find_markets(rest, *, status: Optional[str] = "open", series: Iterable[str] = (), category: Optional[str] = None,
                 search: Optional[str] = None, min_volume: float = 0.0, min_seconds_to_close: Optional[float] = None,
                 max_seconds_to_close: Optional[float] = None, two_sided: bool = False, max_spread_c: Optional[float] = None,
                 max_pages: int = 10, sort: str = "volume", now: Optional[float] = None) -> List[MarketMeta]:
    """Discover markets currently listed on the exchange, robust to which series exist.

    Nothing is hard-coded: markets come from ``GET /markets`` (filtered server-side by
    status/series), categories from ``GET /series``. ``search`` is a case-insensitive
    match on ticker, event ticker, title and subtitles.
    """
    now = now or time.time()
    cats = series_categories(rest)
    series = list(series)
    if category:
        wanted = {t for t, c in cats.items() if (c or "").lower() == category.lower()}
        series = sorted(set(series) & wanted) if series else sorted(wanted)
        if not series:
            return []
    raw: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {}
    if status:
        params["status"] = status
    if series:
        for s in series:
            raw.extend(rest.iter_markets(max_pages=max_pages, series_ticker=s, **params))
    else:
        raw.extend(rest.iter_markets(max_pages=max_pages, mve_filter="exclude", **params))
    out: List[MarketMeta] = []
    needle = (search or "").lower().strip()
    for m in raw:
        mm = market_from_api(m)
        mm.category = cats.get(mm.series_ticker) or None
        if needle:
            hay = " ".join(str(x or "") for x in (mm.ticker, mm.event_ticker, mm.title, m.get("subtitle"),
                                                  m.get("yes_sub_title"), m.get("no_sub_title"), mm.category)).lower()
            if needle not in hay:
                continue
        if mm.activity < min_volume:
            continue
        ttc = mm.seconds_to_close(now)
        if min_seconds_to_close is not None and (ttc is None or ttc < min_seconds_to_close):
            continue
        if max_seconds_to_close is not None and (ttc is None or ttc > max_seconds_to_close):
            continue
        if two_sided and (mm.yes_bid is None or mm.yes_ask is None or mm.yes_bid <= 0 or mm.yes_ask >= 10_000):
            continue
        if max_spread_c is not None and (mm.spread is None or mm.spread > max_spread_c * 100):
            continue
        out.append(mm)
    keys = {"volume": lambda x: (-x.activity, x.ticker),
            "close": lambda x: (x.close_ts or float("inf"), x.ticker),
            "spread": lambda x: (x.spread if x.spread is not None else 10**9, -x.activity)}
    out.sort(key=keys.get(sort, keys["volume"]))
    return out

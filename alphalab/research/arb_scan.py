"""Offline scan for logical-arbitrage candidates in recorded top-of-book data.

For every event with >= 2 recorded markets, the latest top of book of each
market is carried forward in time and, at each update, the relationships in
``strategies.arbitrage`` are evaluated. Reported per candidate type:
occurrences, how long violations persisted, gross edge, taker fees on every
leg, and net edge. A violation that exists only for less than the assumed
order latency is not executable and is counted separately.

Also runs the unified-book integrity check: on Kalshi a single market's YES
ask equals 1 - best NO bid, so a crossed book (bid >= ask) is a data error,
not an opportunity.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List

import pandas as pd

from alphalab.core.fees import fee_per_contract
from alphalab.data.db import Database

NS = 1_000_000_000


def _fee_c(p_units: float, rate: float) -> float:
    return math.ceil(fee_per_contract(int(p_units), rate) * 100 - 1e-9)


def scan(db: Database, taker_rate: float = 0.07, min_net_c: float = 0.0, latency_ms: int = 150) -> Dict[str, Any]:
    out: Dict[str, Any] = {"crossed_books": int(db.query_df("SELECT COUNT(*) n FROM tob WHERE bid >= ask")["n"].iloc[0])}
    meta = db.query_df("SELECT ticker, event_ticker, mutually_exclusive, strike_type, floor_strike FROM markets")
    groups = meta.groupby("event_ticker")["ticker"].apply(list).to_dict()
    groups = {e: ms for e, ms in groups.items() if e and len(ms) >= 2}
    out["events_scanned"] = len(groups)
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"observations": 0, "executable": 0, "max_net_c": None,
                                                            "sum_net_c": 0.0, "episodes": 0})
    for ev, ms in groups.items():
        tob = db.query_df(f"SELECT ts_ns, market, bid, ask FROM tob WHERE synced AND market IN "
                          f"({','.join('?' for _ in ms)}) ORDER BY ts_ns", ms)
        if tob.empty:
            continue
        m = meta.set_index("ticker").loc[ms]
        excl = bool(m["mutually_exclusive"].fillna(False).any())
        ladder = m[m["strike_type"].fillna("").str.startswith("greater") & m["floor_strike"].notna()] \
            .sort_values("floor_strike")
        last: Dict[str, tuple] = {}
        active: Dict[str, int] = {}
        for r in tob.itertuples(index=False):
            last[r.market] = (r.bid, r.ask)
            if len(last) < len(ms):
                continue
            found: Dict[str, float] = {}
            if excl and all(v[0] == v[0] and v[0] is not None for v in last.values()):
                bids = [last[x][0] for x in ms]
                net = sum(bids) / 100 - 100 - sum(_fee_c(b, taker_rate) for b in bids)
                found["exclusive_sell"] = net
            lt = ladder.index.tolist()
            for a, b in zip(lt, lt[1:]):
                ask1, bid2 = last[a][1], last[b][0]
                if ask1 == ask1 and bid2 == bid2 and ask1 is not None and bid2 is not None:
                    net = (bid2 - ask1) / 100 - _fee_c(ask1, taker_rate) - _fee_c(bid2, taker_rate)
                    found[f"ladder"] = max(found.get("ladder", -1e9), net)
            for typ, net in found.items():
                key = f"{ev}|{typ}"
                if net > min_net_c:
                    s = stats[typ]
                    s["observations"] += 1
                    s["sum_net_c"] += net
                    s["max_net_c"] = net if s["max_net_c"] is None else max(s["max_net_c"], net)
                    if key not in active:
                        active[key] = r.ts_ns
                        s["episodes"] += 1
                elif key in active:
                    if r.ts_ns - active.pop(key) >= latency_ms * 1_000_000:
                        stats[typ]["executable"] += 1
    out["candidates"] = {k: dict(v) for k, v in stats.items()}
    out["note"] = ("net = gross edge minus taker fees on every leg, per 1-contract set, in cents; "
                   "'executable' counts episodes that persisted longer than the order latency")
    return out

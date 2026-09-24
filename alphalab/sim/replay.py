"""Deterministic historical event stream from DuckDB.

Merges book events, trades, external prices, market-close markers and
settlements for a set of markets, ordered by ``(ts_ns, kind priority, ord)``
so repeated runs see byte-identical event sequences. Data is streamed in time
chunks to bound memory on large datasets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from alphalab.core.events import (EVENT_ORDER, BookDelta, BookSnapshot, ExternalPrice, MarketStatus,
                                  Settlement, TradeEvent)
from alphalab.data.db import Database

NS = 1_000_000_000


@dataclass
class ReplaySpec:
    markets: Optional[List[str]] = None       # None = all markets with book data
    start_ns: Optional[int] = None
    end_ns: Optional[int] = None
    include_external: bool = True
    include_settlements: bool = True          # settlements after end_ns are still delivered at the end
    chunk_s: int = 6 * 3600

    def to_dict(self) -> Dict[str, Any]:
        return {"markets": self.markets, "start_ns": self.start_ns, "end_ns": self.end_ns,
                "include_external": self.include_external, "include_settlements": self.include_settlements}


def _in(col: str, vals: List[str]) -> str:
    return f"{col} IN ({','.join('?' for _ in vals)})"


class ReplaySource:
    def __init__(self, db: Database, spec: ReplaySpec):
        self.db = db
        self.spec = spec
        if spec.markets is None:
            spec.markets = db.query_df("SELECT DISTINCT market FROM book_events ORDER BY 1")["market"].tolist()
        self.markets = list(spec.markets)
        meta = db.query_df(f"SELECT * FROM markets WHERE {_in('ticker', self.markets)}", self.markets) \
            if self.markets else pd.DataFrame()
        self.meta: Dict[str, Dict[str, Any]] = {}
        for r in meta.to_dict("records"):
            self.meta[r["ticker"]] = {k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()}
        for m in self.markets:
            self.meta.setdefault(m, {"ticker": m})
        self.symbols = sorted({m.get("underlying") for m in self.meta.values() if m.get("underlying")})
        rng = db.query_df(f"SELECT MIN(ts_ns) lo, MAX(ts_ns) hi, COUNT(*) n FROM book_events WHERE {_in('market', self.markets)}",
                          self.markets) if self.markets else None
        lo = int(rng["lo"].iloc[0]) if rng is not None and rng["n"].iloc[0] else 0
        hi = int(rng["hi"].iloc[0]) if rng is not None and rng["n"].iloc[0] else 0
        self.start_ns = spec.start_ns if spec.start_ns is not None else lo
        self.end_ns = spec.end_ns if spec.end_ns is not None else hi + 1
        self.n_book_events = int(rng["n"].iloc[0]) if rng is not None else 0
        # Warm-up: when starting mid-stream, begin reading at the latest snapshot before start
        # so books are reconstructed; events before ``start_ns`` must not reach strategies.
        self.read_from_ns = self.start_ns
        if spec.start_ns is not None and self.markets:
            w = db.query_df(f"SELECT market, MAX(ts_ns) ts FROM book_events WHERE {_in('market', self.markets)} "
                            f"AND kind = 'snapshot' AND ts_ns <= ? GROUP BY market", self.markets + [spec.start_ns])
            if not w.empty:
                self.read_from_ns = min(self.start_ns, int(w["ts"].min()))

    def market_close_ns(self) -> Dict[str, int]:
        return {m: int(v["close_ts"] * NS) for m, v in self.meta.items() if v.get("close_ts")}

    def data_hash(self) -> str:
        if getattr(self, "_hash", None):
            return self._hash
        h = hashlib.sha256()
        h.update(json.dumps(self.spec.to_dict(), sort_keys=True, default=str).encode())
        if self.markets:
            for t in ("book_events", "trades"):
                r = self.db.query_df(
                    f"SELECT COUNT(*) n, COALESCE(SUM(ts_ns % 1000003), 0) s FROM {t} WHERE {_in('market', self.markets)} "
                    f"AND ts_ns >= ? AND ts_ns < ?", self.markets + [self.start_ns, self.end_ns])
                h.update(f"{t}:{int(r['n'].iloc[0])}:{int(r['s'].iloc[0])}".encode())
        return h.hexdigest()[:16]

    def _chunk(self, lo: int, hi: int) -> List[Any]:
        ms = self.markets
        evs: List[tuple] = []
        b = self.db.query_df(f"SELECT * FROM book_events WHERE {_in('market', ms)} AND ts_ns >= ? AND ts_ns < ?",
                             ms + [lo, hi])
        for r in b.itertuples(index=False):
            if r.kind == "snapshot":
                ev = BookSnapshot(int(r.ts_ns), r.market, [tuple(x) for x in json.loads(r.yes_levels)],
                                  [tuple(x) for x in json.loads(r.no_levels)],
                                  seq=None if pd.isna(r.seq) else int(r.seq), sid=None if pd.isna(r.sid) else int(r.sid))
            else:
                ev = BookDelta(int(r.ts_ns), r.market, r.side, int(r.price), float(r.delta),
                               seq=None if pd.isna(r.seq) else int(r.seq), sid=None if pd.isna(r.sid) else int(r.sid))
            evs.append((ev.ts_ns, EVENT_ORDER[ev.kind], int(r.ord), ev))
        t = self.db.query_df(f"SELECT * FROM trades WHERE {_in('market', ms)} AND ts_ns >= ? AND ts_ns < ?",
                             ms + [lo, hi])
        for r in t.itertuples(index=False):
            ev = TradeEvent(int(r.ts_ns), r.market, int(r.price), float(r.qty), r.taker_side or "",
                            r.trade_id or "")
            evs.append((ev.ts_ns, EVENT_ORDER["trade"], int(r.ord), ev))
        if self.spec.include_external and self.symbols:
            x = self.db.query_df(f"SELECT * FROM external_prices WHERE {_in('symbol', self.symbols)} "
                                 f"AND ts_ns >= ? AND ts_ns < ?", self.symbols + [lo, hi])
            for r in x.itertuples(index=False):
                ev = ExternalPrice(int(r.ts_ns), r.symbol, float(r.price), r.source or "",
                                   None if pd.isna(r.bid) else float(r.bid), None if pd.isna(r.ask) else float(r.ask))
                evs.append((ev.ts_ns, EVENT_ORDER["external"], int(r.ord), ev))
        for m, meta in self.meta.items():
            c = meta.get("close_ts")
            if c:
                cn = int(c * NS)
                if lo <= cn < hi:
                    evs.append((cn, EVENT_ORDER["status"], 0, MarketStatus(cn, m, "closed")))
        evs.sort(key=lambda e: (e[0], e[1], e[2], getattr(e[3], "market", getattr(e[3], "symbol", ""))))
        return [e[3] for e in evs]

    def settlements(self) -> List[Settlement]:
        if not self.spec.include_settlements or not self.markets:
            return []
        s = self.db.query_df(f"SELECT * FROM settlements WHERE {_in('market', self.markets)} ORDER BY ts_ns, market",
                             self.markets)
        return [Settlement(int(r.ts_ns), r.market, float(r.value), r.result or "", bool(r.inferred))
                for r in s.itertuples(index=False)]

    def materialize(self) -> "ReplaySource":
        """Load the whole stream into memory once so repeated runs (grid search,
        stress tests, benchmarks) skip the database. Events are immutable."""
        self._cached = list(self._generate())
        self._hash = self.data_hash()
        return self

    def __iter__(self) -> Iterator[Any]:
        cached = getattr(self, "_cached", None)
        if cached is not None:
            return iter(cached)
        return self._generate()

    def _generate(self) -> Iterator[Any]:
        settles = self.settlements()
        si = 0
        step = self.spec.chunk_s * NS
        lo = self.read_from_ns
        while lo < self.end_ns:
            hi = min(self.end_ns, lo + step)
            chunk = self._chunk(lo, hi)
            for ev in chunk:
                while si < len(settles) and settles[si].ts_ns <= ev.ts_ns:
                    if settles[si].ts_ns >= self.start_ns:
                        yield settles[si]
                    si += 1
                if ev.ts_ns < self.start_ns and ev.kind in ("status",):
                    continue
                yield ev
            lo = hi
        while si < len(settles):  # settlements at/after the end of the data window
            s = settles[si]
            if s.ts_ns < self.start_ns:
                si += 1
                continue
            yield s
            si += 1

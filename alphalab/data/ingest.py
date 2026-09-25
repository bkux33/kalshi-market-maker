"""Load tape files / normalised events into DuckDB and derive top-of-book.

``EventSink`` is the single place where normalised events become table rows,
used by tape ingest, CSV importers and the synthetic generator alike, so all
data paths share one storage format.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from alphalab.core.book_manager import BookManager
from alphalab.core.events import (BookDelta, BookSnapshot, ExternalPrice, MarketStatus, Settlement,
                                  TradeEvent)
from alphalab.data.db import Database
from alphalab.data.tape import closed_tape_files, read_tape
from alphalab.kalshi.discovery import market_from_api, strike_from_text

SERIES_UNDERLYING = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD", "KXXRP15M": "XRP-USD",
                     "KXBTCD": "BTC-USD", "KXETHD": "ETH-USD"}
from alphalab.kalshi.messages import parse_frame

log = logging.getLogger(__name__)


class EventSink:
    def __init__(self, db: Database, source: str):
        self.db = db
        self.source = source
        self.book: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.life: List[Dict[str, Any]] = []
        self.ext: List[Dict[str, Any]] = []
        self.settle: List[Settlement] = []
        self.markets: Dict[str, Dict[str, Any]] = {}
        self._book_ord = db.next_ord("book_events")
        self._trade_ord = db.next_ord("trades")
        self._ext_ord = db.next_ord("external_prices")
        self.count = 0

    def add(self, ev: Any) -> None:
        self.count += 1
        if isinstance(ev, BookSnapshot):
            self.book.append(dict(ts_ns=ev.ts_ns, ord=self._book_ord, market=ev.market, kind="snapshot",
                                  seq=ev.seq, sid=ev.sid, side=None, price=None, delta=None,
                                  yes_levels=json.dumps(ev.yes), no_levels=json.dumps(ev.no),
                                  source=self.source))
            self._book_ord += 1
        elif isinstance(ev, BookDelta):
            self.book.append(dict(ts_ns=ev.ts_ns, ord=self._book_ord, market=ev.market, kind="delta",
                                  seq=ev.seq, sid=ev.sid, side=ev.side, price=ev.price, delta=ev.delta,
                                  yes_levels=None, no_levels=None, source=self.source))
            self._book_ord += 1
        elif isinstance(ev, TradeEvent):
            self.trades.append(dict(ts_ns=ev.ts_ns, ord=self._trade_ord, market=ev.market,
                                    trade_id=ev.trade_id, price=ev.price, qty=ev.qty,
                                    taker_side=ev.taker_side, exch_ts_ns=ev.exch_ts_ns, source=self.source))
            self._trade_ord += 1
        elif isinstance(ev, MarketStatus):
            self.life.append(dict(ts_ns=ev.ts_ns, market=ev.market, event_type=ev.event_type,
                                  data=json.dumps(ev.data, default=str)))
        elif isinstance(ev, Settlement):
            self.settle.append(ev)
        elif isinstance(ev, ExternalPrice):
            self.ext.append(dict(ts_ns=ev.ts_ns, ord=self._ext_ord, symbol=ev.symbol, price=ev.price,
                                 bid=ev.bid, ask=ev.ask, source=ev.source or self.source))
            self._ext_ord += 1

    def add_market(self, row: Dict[str, Any]) -> None:
        cur = self.markets.get(row["ticker"], {})
        cur.update({k: v for k, v in row.items() if v is not None})
        self.markets[row["ticker"]] = cur

    def flush(self) -> int:
        n = 0
        n += self.db.insert_rows("book_events", self.book)
        n += self.db.insert_rows("trades", self.trades)
        n += self.db.insert_rows("lifecycle", self.life)
        n += self.db.insert_rows("external_prices", self.ext)
        if self.markets:
            for r in self.markets.values():
                r.setdefault("source", self.source)
            self.db.upsert_markets(self.markets.values())
        for s in self.settle:
            self.db.upsert_settlement(s.market, s.ts_ns, s.value, s.result, s.inferred, self.source)
        n += len(self.settle)
        self.book, self.trades, self.life, self.ext, self.settle = [], [], [], [], []
        self.markets = {}
        return n


def ingest_tape(db: Database, raw_dir: str | Path, include_open: bool = False) -> Dict[str, int]:
    done = set(db.query_df("SELECT path FROM ingested_files")["path"].tolist())
    stats = {"files": 0, "events": 0, "skipped": 0}
    touched_markets: set = set()
    for f in closed_tape_files(raw_dir, include_open=include_open):
        key = str(Path(f).resolve())
        if key in done:
            stats["skipped"] += 1
            continue
        db.begin()
        try:
            sink = EventSink(db, source=f"tape:{Path(f).name}")
            lines = 0
            for recv_ns, kind, payload in read_tape(f):
                lines += 1
                if kind == "ws":
                    try:
                        frame = json.loads(payload) if isinstance(payload, str) else payload
                    except json.JSONDecodeError:
                        continue
                    for ev in parse_frame(frame, recv_ns):
                        sink.add(ev)
                        if getattr(ev, "market", None):
                            touched_markets.add(ev.market)
                elif kind == "market":
                    mm = market_from_api(payload)
                    row = mm.to_row()
                    row["updated_ns"] = recv_ns
                    row["depth_quality"] = "full"
                    if mm.series_ticker in SERIES_UNDERLYING:
                        row["underlying"] = SERIES_UNDERLYING[mm.series_ticker]
                    if row.get("floor_strike") is None:
                        row["floor_strike"] = strike_from_text(" ".join(str(payload.get(k) or "") for k in
                                                                        ("yes_sub_title", "subtitle", "title")))
                    sink.add_market(row)
                    if mm.result in ("yes", "no") or mm.settlement_value is not None:
                        val = mm.settlement_value if mm.settlement_value is not None else (1.0 if mm.result == "yes" else 0.0)
                        sink.add(Settlement(recv_ns, mm.ticker, float(val), mm.result or "", inferred=False))
                elif kind == "series":
                    st = payload.get("ticker") or payload.get("series_ticker")
                    if st:
                        db.execute("UPDATE markets SET fee_type = ?, fee_multiplier = ? WHERE series_ticker = ?",
                                   [payload.get("fee_type"), payload.get("fee_multiplier"), st])
                        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                   [f"series_fee:{st}", json.dumps({"fee_type": payload.get("fee_type"),
                                                                    "fee_multiplier": payload.get("fee_multiplier")})])
                elif kind == "ext":
                    sink.add(ExternalPrice(recv_ns, payload["symbol"], float(payload["price"]),
                                           payload.get("source", ""), payload.get("bid"), payload.get("ask")))
            n = sink.flush()
            db.execute("INSERT INTO ingested_files VALUES (?,?,?,?,?)",
                       [key, Path(f).stat().st_size, lines, n, time.time_ns()])
            db.commit()
        except Exception:
            db.rollback()
            raise
        stats["files"] += 1
        stats["events"] += n
    if touched_markets:
        derive_tob(db, sorted(touched_markets))
    return stats


def load_book_events(db: Database, markets: Optional[List[str]] = None,
                     start_ns: Optional[int] = None, end_ns: Optional[int] = None) -> pd.DataFrame:
    where, params = [], []
    if markets:
        where.append(f"market IN ({','.join('?' for _ in markets)})")
        params += markets
    if start_ns is not None:
        where.append("ts_ns >= ?")
        params.append(start_ns)
    if end_ns is not None:
        where.append("ts_ns < ?")
        params.append(end_ns)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    return db.query_df(f"SELECT * FROM book_events {w} ORDER BY ts_ns, ord", params)


def row_to_book_event(r: Dict[str, Any]):
    if r["kind"] == "snapshot":
        return BookSnapshot(int(r["ts_ns"]), r["market"], [tuple(x) for x in json.loads(r["yes_levels"])],
                            [tuple(x) for x in json.loads(r["no_levels"])],
                            seq=None if pd.isna(r["seq"]) else int(r["seq"]),
                            sid=None if pd.isna(r["sid"]) else int(r["sid"]))
    return BookDelta(int(r["ts_ns"]), r["market"], r["side"], int(r["price"]), float(r["delta"]),
                     seq=None if pd.isna(r["seq"]) else int(r["seq"]),
                     sid=None if pd.isna(r["sid"]) else int(r["sid"]))


def derive_tob(db: Database, markets: Optional[List[str]] = None) -> int:
    """(Re)build the ``tob`` table for ``markets`` by replaying book events."""
    df = load_book_events(db, markets)
    if df.empty:
        return 0
    mk = sorted(df["market"].unique().tolist())
    db.execute(f"DELETE FROM tob WHERE market IN ({','.join('?' for _ in mk)})", mk)
    bm = BookManager()
    rows: List[Dict[str, Any]] = []
    last: Dict[str, tuple] = {}
    for r in df.to_dict("records"):
        ev = row_to_book_event(r)
        bm.apply(ev)
        ob = bm.book(ev.market)
        synced = bm.is_synced(ev.market)
        bb, ba = ob.best_bid(), ob.best_ask()
        key = (bb, ba, round(ob.depth("bid", 5), 6), round(ob.depth("ask", 5), 6), synced)
        if last.get(ev.market) == key:
            continue
        last[ev.market] = key
        rows.append(dict(ts_ns=ev.ts_ns, market=ev.market,
                         bid=bb[0] if bb else None, ask=ba[0] if ba else None,
                         bid_qty=bb[1] if bb else None, ask_qty=ba[1] if ba else None,
                         mid=ob.mid(), spread=ob.spread(), bid_depth5=ob.depth("bid", 5),
                         ask_depth5=ob.depth("ask", 5), imbalance1=ob.imbalance(1),
                         imbalance5=ob.imbalance(5), microprice=ob.microprice(), synced=synced))
    db.insert_rows("tob", rows)
    return len(rows)

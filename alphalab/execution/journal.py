"""Append-only JSONL journal for paper/live sessions, and loading it into DuckDB.

Long-running traders never hold the DuckDB write lock; they append to
``data/journal/<mode>-<session>.jsonl``. ``ingest_journals`` loads journals
into the ``runs``/``orders``/``fills``/``signals``/``risk_events``/``pnl``
tables (idempotently, tracked in ``ingested_files``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from alphalab.data.db import Database, dumps


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, kind: str, row: Dict[str, Any]) -> None:
        line = json.dumps({"k": kind, "t": time.time_ns(), "d": row}, default=str, separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            finally:
                self._fh.close()


_ORDER_COLS = ["run_id", "mode", "order_id", "client_order_id", "strategy", "market", "action", "price", "qty",
               "tif", "post_only", "ts_decision", "ts_submit", "ts_active", "ts_done", "status", "filled_qty",
               "avg_fill_price", "reason", "tag"]
_FILL_COLS = ["run_id", "mode", "fill_id", "order_id", "strategy", "market", "ts_ns", "action", "price", "qty",
              "liquidity", "fee", "mid_at_fill", "slippage_usd", "position_after", "markout_1s", "markout_5s",
              "markout_30s", "markout_60s", "tag"]


def ingest_journals(db: Database, journal_dir: str | Path, include_active: bool = False) -> Dict[str, int]:
    """Load finished journals (``*.jsonl``; active sessions write ``*.jsonl.active``)."""
    journal_dir = Path(journal_dir)
    if not journal_dir.exists():
        return {"files": 0}
    done = set(db.query_df("SELECT path FROM ingested_files")["path"].tolist())
    files = sorted(journal_dir.glob("*.jsonl"))
    if include_active:
        files += sorted(journal_dir.glob("*.jsonl.active"))
    stats = {"files": 0, "orders": 0, "fills": 0}
    for f in files:
        key = str(f.resolve())
        if key in done and not f.name.endswith(".active"):
            continue
        rows: Dict[str, List[Dict[str, Any]]] = {}
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.setdefault(rec["k"], []).append(rec["d"])
        db.begin()
        try:
            run_ids = {r.get("run_id") for r in rows.get("session", [])}
            for rid in run_ids:  # re-ingesting an active journal replaces its rows
                for t in ("runs", "orders", "fills", "signals", "pnl", "risk_events"):
                    db.execute(f"DELETE FROM {t} WHERE run_id = ?", [rid])
            for s in rows.get("session", [])[-1:]:
                db.insert_rows("runs", [dict(run_id=s["run_id"], kind=s["mode"], strategy=s.get("strategy"),
                                             params=dumps(s.get("params")), config=dumps(s.get("config")),
                                             data_spec=dumps(s.get("markets")), data_hash="",
                                             created_ns=s.get("started_ns"), code_version=s.get("version"),
                                             metrics=dumps(rows.get("metrics", [{}])[-1]), experiment_id=None,
                                             phase=s["mode"], result_hash="")])
            final_orders: Dict[str, Dict[str, Any]] = {}
            for o in rows.get("order_submitted", []) + rows.get("order", []):
                final_orders[o["order_id"]] = o
            if final_orders:
                db.insert_df("orders", pd.DataFrame([{c: o.get(c) for c in _ORDER_COLS} for o in final_orders.values()]))
            fills = rows.get("fill", [])
            if fills:
                mk = {m["fill_id"]: m for m in rows.get("markout", [])}
                recs = []
                for x in fills:
                    x = {**x, **mk.get(x["fill_id"], {})}
                    x.setdefault("mode", rows.get("session", [{}])[-1].get("mode"))
                    x.setdefault("run_id", rows.get("session", [{}])[-1].get("run_id"))
                    recs.append({c: x.get(c) for c in _FILL_COLS})
                db.insert_df("fills", pd.DataFrame(recs))
            if rows.get("signal"):
                db.insert_df("signals", pd.DataFrame([{**s, "payload": json.dumps(s.get("payload"), default=str)}
                                                      for s in rows["signal"]]))
            if rows.get("pnl"):
                db.insert_df("pnl", pd.DataFrame([{**p, "positions": json.dumps(p.get("positions"))} for p in rows["pnl"]]))
            if rows.get("risk"):
                db.insert_rows("risk_events", [dict(ts_ns=r.get("ts_ns"), mode=r.get("mode"), run_id=r.get("run_id"),
                                                    type=r.get("type"), severity=r.get("severity"),
                                                    detail=dumps(r)) for r in rows["risk"]])
            if not f.name.endswith(".active"):
                db.execute("DELETE FROM ingested_files WHERE path = ?", [key])
                db.execute("INSERT INTO ingested_files VALUES (?,?,?,?,?)",
                           [key, f.stat().st_size, sum(len(v) for v in rows.values()), len(fills), time.time_ns()])
            db.commit()
        except Exception:
            db.rollback()
            raise
        stats["files"] += 1
        stats["orders"] += len(final_orders)
        stats["fills"] += len(fills)
    return stats

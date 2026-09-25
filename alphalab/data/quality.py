"""Data-quality audit of raw tape recordings (``alphalab data-quality``).

Works directly on the append-only tape (the source of truth), independent of
DuckDB ingest, and reports what a researcher needs before trusting the data:
collection period, markets, message counts by type, order-book updates, trades,
sequence gaps, duplicates (exact re-deliveries and sequence duplicates),
reconnects, missing timestamps, malformed records, storage size, books
reconstructed, and the share of valid records.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from alphalab.core.book_manager import BookManager
from alphalab.data.tape import OPEN_SUFFIX, closed_tape_files
from alphalab.kalshi.messages import parse_frame


def _utc(ns: Optional[int]) -> Optional[str]:
    if not ns:
        return None
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="milliseconds")


def audit_tape(raw_dir: str | Path, include_open: bool = True) -> Dict[str, Any]:
    files = closed_tape_files(raw_dir, include_open=include_open)
    r: Dict[str, Any] = {"files": len(files), "open_files": sum(1 for f in files if str(f).endswith(OPEN_SUFFIX)),
                         "bytes": sum(f.stat().st_size for f in files)}
    kinds, types, notes = Counter(), Counter(), Counter()
    lines = valid = torn = malformed_frames = missing_recv_ts = missing_exch_ts = exact_dups = 0
    lo = hi = None
    markets: set = set()
    seen_hashes: set = set()
    trades = trade_ids = dup_trade_ids = 0
    trade_seen: set = set()
    bm = BookManager()
    crossed = 0
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                lines += 1
                try:
                    rec = json.loads(line)
                    recv, kind, payload = rec.get("r"), rec["c"], rec["f"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    torn += 1
                    continue
                kinds[kind] += 1
                if not isinstance(recv, int) or recv <= 0:
                    missing_recv_ts += 1
                    continue
                lo = recv if lo is None else min(lo, recv)
                hi = recv if hi is None else max(hi, recv)
                if kind == "note":
                    notes[str((payload or {}).get("status"))] += 1
                    valid += 1
                    continue
                if kind != "ws":
                    valid += 1
                    continue
                h = hashlib.blake2b(payload.encode() if isinstance(payload, str) else json.dumps(payload).encode(),
                                    digest_size=12).digest()
                if h in seen_hashes:
                    exact_dups += 1
                seen_hashes.add(h)
                try:
                    frame = json.loads(payload) if isinstance(payload, str) else payload
                    if not isinstance(frame, dict):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    malformed_frames += 1
                    continue
                valid += 1
                typ = frame.get("type")
                types[typ] += 1
                msg = frame.get("msg") or {}
                if typ in ("orderbook_delta", "trade") and msg.get("ts_ms") is None and msg.get("ts") is None:
                    missing_exch_ts += 1
                for ev in parse_frame(frame, recv):
                    m = getattr(ev, "market", None)
                    if m:
                        markets.add(m)
                    if ev.kind in ("snapshot", "delta"):
                        bm.apply(ev)
                        ob = bm.books.get(ev.market)
                        if ob is not None and bm.is_synced(ev.market) and ob.is_crossed():
                            crossed += 1
                    elif ev.kind == "trade":
                        trades += 1
                        if ev.trade_id:
                            trade_ids += 1
                            if ev.trade_id in trade_seen:
                                dup_trade_ids += 1
                            trade_seen.add(ev.trade_id)
    st = bm.stats()
    r.update({
        "collection_start_utc": _utc(lo), "collection_end_utc": _utc(hi),
        "duration_s": round((hi - lo) / 1e9, 3) if lo and hi else 0.0,
        "duration_hours": round((hi - lo) / 3.6e12, 3) if lo and hi else 0.0,
        "records": lines, "records_by_kind": dict(kinds), "ws_messages_by_type": dict(types),
        "markets_recorded": len(markets), "markets": sorted(markets)[:500],
        "orderbook_snapshots": types.get("orderbook_snapshot", 0), "orderbook_deltas": types.get("orderbook_delta", 0),
        "trades": trades, "duplicate_trade_ids": dup_trade_ids,
        "sequence_gaps": st["gaps"], "sequence_duplicates": st["duplicates"], "exact_duplicate_frames": exact_dups,
        "deltas_ignored_while_unsynced": st["ignored_deltas"],
        "reconnects": notes.get("reconnecting", 0), "disconnects": notes.get("disconnected", 0),
        "connections": notes.get("connected", 0), "notes": dict(notes),
        "missing_receive_timestamps": missing_recv_ts, "missing_exchange_timestamps": missing_exch_ts,
        "torn_or_malformed_lines": torn, "malformed_ws_frames": malformed_frames,
        "books_reconstructed": st["books"], "books_synced_at_end": st["synced_books"],
        "crossed_book_observations": crossed,
        "error_frames": types.get("error", 0),
        "valid_record_pct": round(100.0 * valid / lines, 3) if lines else None,
    })
    return r


def render_markdown(r: Dict[str, Any], title: str = "Data quality report", note: str = "") -> str:
    rows = [
        ("Collection period (UTC)", f"{r.get('collection_start_utc')} → {r.get('collection_end_utc')} "
                                     f"({r.get('duration_s')} s = {r.get('duration_hours')} h)"),
        ("Tape files / open (unfinalized)", f"{r['files']} / {r['open_files']}"),
        ("Estimated storage size", f"{r['bytes'] / 1e6:.2f} MB"),
        ("Records (all kinds)", r.get("records")),
        ("Markets recorded", r.get("markets_recorded")),
        ("WebSocket messages by type", r.get("ws_messages_by_type")),
        ("Order-book snapshots / deltas", f"{r.get('orderbook_snapshots')} / {r.get('orderbook_deltas')}"),
        ("Trades (duplicate trade ids)", f"{r.get('trades')} ({r.get('duplicate_trade_ids')})"),
        ("Sequence gaps", r.get("sequence_gaps")),
        ("Duplicate messages (sequence / exact frame)", f"{r.get('sequence_duplicates')} / {r.get('exact_duplicate_frames')}"),
        ("Connections / reconnects / disconnects", f"{r.get('connections')} / {r.get('reconnects')} / {r.get('disconnects')}"),
        ("Missing receive timestamps", r.get("missing_receive_timestamps")),
        ("Deltas/trades without exchange timestamp", r.get("missing_exchange_timestamps")),
        ("Malformed WS frames / torn lines", f"{r.get('malformed_ws_frames')} / {r.get('torn_or_malformed_lines')}"),
        ("Error frames from exchange", r.get("error_frames")),
        ("Books reconstructed / synced at end", f"{r.get('books_reconstructed')} / {r.get('books_synced_at_end')}"),
        ("Crossed-book observations (should be 0)", r.get("crossed_book_observations")),
        ("Valid records", f"{r.get('valid_record_pct')}%"),
    ]
    out = [f"# {title}", ""]
    if note:
        out += [note, ""]
    out += ["| Metric | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out) + "\n"

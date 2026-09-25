"""Append-only JSONL "tape" for raw captured data.

Each line is ``{"r": recv_ns, "c": kind, "f": payload}`` where ``kind`` is

* ``ws``      - a raw WebSocket frame, stored verbatim as the received text;
* ``market``  - a REST market object (metadata / result polling);
* ``series``  - a REST series object (fee type, fee multiplier);
* ``ext``     - an external reference price ``{"symbol","price","bid","ask","source"}``;
* ``note``    - recorder lifecycle notes (connect/disconnect/gap).

Files rotate hourly and are named ``<YYYYMMDD>/<HH>/<prefix>-<start_ns>.jsonl``.
A file is only ingested once it is closed (rotated) so a crash mid-write can
at most lose the last partially written line, which ingest skips.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

OPEN_SUFFIX = ".open"


class TapeWriter:
    def __init__(self, root: str | Path, prefix: str = "frames", fsync_every_s: float = 1.0):
        self.root = Path(root)
        self.prefix = prefix
        self.fsync_every_s = fsync_every_s
        self._fh = None
        self._path: Optional[Path] = None
        self._hour: Optional[str] = None
        self._last_sync = 0.0
        self._lock = threading.RLock()
        self.lines = 0

    def _rotate_if_needed(self, recv_ns: int) -> None:
        hour = datetime.fromtimestamp(recv_ns / 1e9, timezone.utc).strftime("%Y%m%d/%H")
        if hour == self._hour and self._fh is not None:
            return
        self.close()
        d = self.root / hour
        d.mkdir(parents=True, exist_ok=True)
        self._path = d / f"{self.prefix}-{recv_ns}.jsonl{OPEN_SUFFIX}"
        self._fh = open(self._path, "a", encoding="utf-8")
        self._hour = hour

    def write(self, kind: str, payload: Any, recv_ns: Optional[int] = None) -> None:
        recv_ns = recv_ns or time.time_ns()
        line = json.dumps({"r": recv_ns, "c": kind, "f": payload}, separators=(",", ":"))
        with self._lock:
            self._rotate_if_needed(recv_ns)
            self._fh.write(line + "\n")
            self.lines += 1
            now = time.monotonic()
            if now - self._last_sync >= self.fsync_every_s:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._last_sync = now

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
                final = self._path.with_name(self._path.name[: -len(OPEN_SUFFIX)])
                os.replace(self._path, final)
                self._fh = None
                self._path = None
                self._hour = None


def recover_open_files(root: str | Path, older_than_s: float = 0.0) -> list[Path]:
    """Finalize ``*.jsonl.open`` files left behind by a crashed/killed writer.

    Tape files are append-only line-delimited JSON, so an unclosed file is valid
    up to its last complete line (``read_tape`` skips a torn final line). Call this
    on recorder start-up *before* opening a new file; files modified within
    ``older_than_s`` seconds are left alone in case another writer is active.
    """
    root = Path(root)
    done = []
    if not root.exists():
        return done
    now = time.time()
    for p in sorted(root.rglob(f"*.jsonl{OPEN_SUFFIX}")):
        if now - p.stat().st_mtime < older_than_s:
            continue
        final = p.with_name(p.name[: -len(OPEN_SUFFIX)])
        os.replace(p, final)
        done.append(final)
    return done


def closed_tape_files(root: str | Path, include_open: bool = False) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    files = sorted(root.rglob("*.jsonl"))
    if include_open:
        files += sorted(root.rglob(f"*.jsonl{OPEN_SUFFIX}"))
    return files


def read_tape(path: str | Path) -> Iterator[Tuple[int, str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated final line after a crash
            yield int(rec["r"]), rec["c"], rec["f"]

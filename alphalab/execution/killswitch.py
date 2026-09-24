"""Kill switch.

The switch is a *latched file*: if ``kill_switch_file`` exists, trading is
halted. Anything can trip it - the CLI (``alphalab kill``), the dashboard
button, the risk engine, a cron job, or a physical button wired to a script
that runs ``touch data/KILL_SWITCH`` (see RISK.md for a GPIO / USB-button
example). Because it is a file, it survives process restarts: a restarted
trader stays halted until a human runs ``alphalab kill --reset``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


class KillSwitch:
    def __init__(self, path: str | Path, check_interval_s: float = 0.25):
        self.path = Path(path)
        self.check_interval_s = check_interval_s
        self._cached = False
        self._last_check = 0.0

    def trip(self, reason: str, source: str = "system") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "reason": reason, "source": source}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.path)
        self._cached = True
        self._last_check = time.monotonic()

    def reset(self) -> bool:
        existed = self.path.exists()
        if existed:
            self.path.unlink()
        self._cached = False
        return existed

    def is_tripped(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or now - self._last_check >= self.check_interval_s:
            self._cached = self.path.exists()
            self._last_check = now
        return self._cached

    def info(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            return {"reason": "unreadable kill file (treated as tripped)"}

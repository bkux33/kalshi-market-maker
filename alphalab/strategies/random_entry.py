"""Null benchmark: random direction at the tested strategy's own entry times.

Given the entry timestamps of a tested taker strategy, this enters at the
same moments (first book update at/after each timestamp) with a coin-flip
direction and the same holding/exit rules. It pays identical spreads and fees,
so comparing the tested strategy against many seeds of this benchmark isolates
whether the *direction* of the signal carries information.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

from alphalab.strategies.common import TakerHorizonStrategy


class RandomEntryStrategy(TakerHorizonStrategy):
    name = "random_entry"
    family = "benchmark"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {**cls.common_params(), "entry_times": [], "seed": 0, "cooldown_s": 0.0}

    def on_start(self, ctx) -> None:
        self._rng = random.Random(self.params["seed"])
        self._queue: Dict[str, List[int]] = {}
        for m, ts in sorted(self.params["entry_times"], key=lambda x: (x[1], x[0])):
            self._queue.setdefault(m, []).append(int(ts))

    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        q = self._queue.get(f["market"])
        if not q or f["ts_ns"] < q[0]:
            return 0, None
        while q and q[0] <= f["ts_ns"]:
            q.pop(0)
        return (1 if self._rng.random() < 0.5 else -1), None

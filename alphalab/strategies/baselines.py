"""Strategy family 5: momentum / mean-reversion / volatility baselines.

These are research *baselines*: simple, transparent rules that any claimed
edge should beat. They are not expected to be profitable.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from alphalab.strategies.common import TakerHorizonStrategy


class MomentumStrategy(TakerHorizonStrategy):
    name = "momentum"
    family = "momentum_mean_reversion"
    tunable = ("lookback_s", "min_move_c", "horizon_s")

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {**cls.common_params(), "lookback_s": 10, "min_move_c": 2.0}

    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        r = f.get(f"ret_{self.params['lookback_s']:g}s")
        if r is None:
            return 0, None
        if r >= self.params["min_move_c"]:
            return 1, r
        if r <= -self.params["min_move_c"]:
            return -1, r
        return 0, r


class MeanReversionStrategy(MomentumStrategy):
    name = "mean_reversion"

    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        d, r = super().decide(f)
        return -d, r


class VolBreakoutStrategy(TakerHorizonStrategy):
    """Volatility expansion: when short-window realised variation jumps relative
    to the longer window, follow the short-term move."""
    name = "vol_breakout"
    family = "volatility"
    tunable = ("ratio", "horizon_s")

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {**cls.common_params(), "ratio": 0.8, "min_move_c": 1.0, "fade": False}

    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        v10, v60, r5 = f.get("vol_10s"), f.get("vol_60s"), f.get("ret_5s")
        if v10 is None or v60 is None or r5 is None or v60 <= 0:
            return 0, None
        ratio = v10 / v60  # share of the last minute's variation that happened in the last 10s
        if ratio < self.params["ratio"] or abs(r5) < self.params["min_move_c"]:
            return 0, ratio
        d = 1 if r5 > 0 else -1
        return (-d if self.params["fade"] else d), ratio


class VolContractionStrategy(VolBreakoutStrategy):
    """Volatility contraction baseline: after a burst, fade the move."""
    name = "vol_fade"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {**super().default_params(), "fade": True}

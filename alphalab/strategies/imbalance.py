"""Strategy family 1: order-book imbalance (taker).

Hypothesis under test: when visible depth is lopsided, the mid tends to move
toward the thin side within ``horizon_s``. Enter in the direction of the heavy
side (bid-heavy -> buy YES), exit after the horizon. Whether this survives the
spread, fees and latency is what the experiments measure.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from alphalab.strategies.common import TakerHorizonStrategy


class ImbalanceStrategy(TakerHorizonStrategy):
    name = "imbalance"
    family = "order_book_imbalance"
    tunable = ("threshold", "horizon_s", "max_spread_c")

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {**cls.common_params(), "threshold": 0.65, "use_top_level": False, "max_vol_60s_c": 1e9}

    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        imb = f.get("imbalance_1") if self.params["use_top_level"] else f.get("imbalance")
        if imb is None:
            return 0, None
        vol = f.get("vol_60s")
        if vol is not None and vol > self.params["max_vol_60s_c"]:
            return 0, imb
        th = self.params["threshold"]
        if imb >= th:
            return 1, imb
        if imb <= 1 - th:
            return -1, imb
        return 0, imb

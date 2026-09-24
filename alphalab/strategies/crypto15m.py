"""Strategy family 4: short-duration crypto contracts (e.g. KXBTC15M).

Uses the external spot price only as an explanatory variable: a driftless
lognormal fair value ``fair = Phi(ln(S/K) / (sigma_spot * sqrt(tau)))`` with
sigma estimated from recent spot returns (see ``FeatureEngine``). The strategy
trades when the contract price disagrees with that fair value by more than
``edge_c`` *after* crossing the spread; the research module separately tests
whether any feature adds information beyond the contract price itself.

Caveats recorded for users: Kalshi settles these contracts on a benchmark
(CF Benchmarks RTI averages) that differs from any single exchange's spot
price; the fair-value model ignores that basis and any drift.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from alphalab.strategies.common import TakerHorizonStrategy


class Crypto15mFairValueStrategy(TakerHorizonStrategy):
    name = "crypto15m_fair_value"
    family = "crypto_short_duration"
    tunable = ("edge_c", "min_tts_s")

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        base = cls.common_params()
        base.update({"edge_c": 5.0, "exit_mode": "settle", "min_tts_s": 60.0, "max_tts_s": 840.0,
                     "max_spread_c": 4.0, "qty": 5, "cooldown_s": 30.0, "series_prefix": "KX"})
        return base

    def accepts(self, market: str, meta: Dict[str, Any]) -> bool:
        return bool(meta.get("underlying") and meta.get("floor_strike")) and \
            market.startswith(self.params["series_prefix"])

    def decide(self, f: Dict[str, Any]) -> Tuple[int, Optional[float]]:
        fair, bid, ask = f.get("fair_prob"), f.get("bid"), f.get("ask")
        if fair is None or bid is None or ask is None:
            return 0, None
        e = self.params["edge_c"]
        if fair - ask >= e:
            return 1, fair - ask
        if bid - fair >= e:
            return -1, bid - fair
        return 0, fair - (bid + ask) / 2

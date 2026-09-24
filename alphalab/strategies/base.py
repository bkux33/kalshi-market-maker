"""Strategy interface.

Strategies are deterministic functions of the events they receive and their
own parameters. They never talk to the exchange directly: every order goes
through ``ctx.place`` -> risk engine -> broker (simulated or live). The same
strategy object therefore runs unchanged in backtest, paper and live modes.
No strategy in this package is assumed to be profitable.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class Strategy:
    name: str = "base"
    family: str = "base"
    #: parameters exposed to the experiment engine (used for overfitting checks)
    tunable: tuple = ()

    def __init__(self, **params: Any):
        unknown = set(params) - set(self.default_params())
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params: Dict[str, Any] = {**self.default_params(), **params}
        self.state: Dict[str, Any] = {}

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {}

    @property
    def key(self) -> str:
        items = ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))
        return f"{self.name}({items})"

    # which markets to trade; meta is a dict row from the markets table
    def accepts(self, market: str, meta: Dict[str, Any]) -> bool:
        return True

    # ------------------------------------------------------------ callbacks
    def on_start(self, ctx) -> None: ...
    def on_book(self, ctx, market: str) -> None: ...
    def on_trade(self, ctx, ev) -> None: ...
    def on_external(self, ctx, ev) -> None: ...
    def on_fill(self, ctx, fill) -> None: ...
    def on_timer(self, ctx, key: str) -> None: ...
    def on_settlement(self, ctx, market: str, value: float) -> None: ...
    def on_stop(self, ctx) -> None: ...


def param_count(strategy_cls, grid: Optional[Dict[str, Any]] = None) -> int:
    """Number of parameters actually being searched (for overfitting flags)."""
    if grid:
        return sum(1 for v in grid.values() if isinstance(v, (list, tuple)) and len(v) > 1)
    return len(strategy_cls.tunable)

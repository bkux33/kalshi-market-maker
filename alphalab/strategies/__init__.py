"""Strategy registry."""

from alphalab.strategies.arbitrage import LogicalArbStrategy
from alphalab.strategies.baselines import (MeanReversionStrategy, MomentumStrategy, VolBreakoutStrategy,
                                           VolContractionStrategy)
from alphalab.strategies.crypto15m import Crypto15mFairValueStrategy
from alphalab.strategies.imbalance import ImbalanceStrategy
from alphalab.strategies.market_maker import MarketMakerStrategy
from alphalab.strategies.random_entry import RandomEntryStrategy

REGISTRY = {cls.name: cls for cls in (
    ImbalanceStrategy, MarketMakerStrategy, LogicalArbStrategy, Crypto15mFairValueStrategy,
    MomentumStrategy, MeanReversionStrategy, VolBreakoutStrategy, VolContractionStrategy,
    RandomEntryStrategy)}


def get_strategy(name: str):
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(REGISTRY)}") from None

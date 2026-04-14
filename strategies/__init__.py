"""
strategies/__init__.py — Strategy factory.

Registered strategy IDs (must match config.STRATEGY_UNIVERSES keys):
  orb_short
  impulse_short
  gap_fill_large
  gap_fill_small
  gap_fill_small_multi
  gap_fill_big
"""

from .base import BaseStrategy
from .orb_short import OrbShortStrategy
from .impulse_short import ImpulseShortStrategy
from .gap_fill_variants import (
    GapFillLargeStrategy,
    GapFillSmallStrategy,
    GapFillSmallMultiStrategy,
    GapFillBigStrategy,
)

_REGISTRY = {
    "orb_short":           OrbShortStrategy,
    "impulse_short":       ImpulseShortStrategy,
    "gap_fill_large":      GapFillLargeStrategy,
    "gap_fill_small":      GapFillSmallStrategy,
    "gap_fill_small_multi": GapFillSmallMultiStrategy,
    "gap_fill_big":        GapFillBigStrategy,
}


def build_strategy(strategy_id: str, symbol: str, params: dict) -> BaseStrategy:
    """
    Instantiate a strategy by ID.
    Raises KeyError for unknown strategy IDs so misconfigured config.STRATEGY_UNIVERSES
    fails loudly at startup rather than silently skipping symbols.
    """
    cls = _REGISTRY.get(strategy_id)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(
            f"Unknown strategy_id '{strategy_id}'. Known strategies: {known}"
        )
    return cls(strategy_id, symbol, params)


__all__ = [
    "BaseStrategy",
    "build_strategy",
    "OrbShortStrategy",
    "ImpulseShortStrategy",
    "GapFillLargeStrategy",
    "GapFillSmallStrategy",
    "GapFillSmallMultiStrategy",
    "GapFillBigStrategy",
]

"""
strategies/__init__.py — Strategy factory.
"""

from .orb_short import ORBShort
from .orb_long import ORBLong
from .impulse_short import ImpulseShort
from .gap_fill import GapFill
from .base import BaseStrategy


def build_strategy(strategy_id: str, symbol: str, params: dict) -> BaseStrategy:
    """Instantiate the correct strategy class by ID."""
    if strategy_id == "orb_short":
        return ORBShort(symbol, params)
    elif strategy_id == "orb_long":
        return ORBLong(symbol, params)
    elif strategy_id == "impulse_short":
        return ImpulseShort(symbol, params)
    elif strategy_id in ("gap_fill_large", "gap_fill_small", "gap_fill_big"):
        return GapFill(strategy_id, symbol, params)
    else:
        raise ValueError(f"Unknown strategy_id: {strategy_id!r}")


__all__ = [
    "ORBShort", "ORBLong", "ImpulseShort", "GapFill",
    "build_strategy", "BaseStrategy",
]

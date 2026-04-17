"""
models.py — Shared data models across the orchestrator.

NOTE: SessionContext lives in data/feed.py (owns its own context class).
      Import it from there, not here.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Signal:
    """
    Emitted by a strategy when it wants to enter a trade.
    The risk layer will calculate share size from entry/stop + RISK_PER_TRADE_DOLLARS.
    """
    strategy_id:   str
    symbol:        str
    direction:     str          # 'long' | 'short'
    entry_price:   float        # expected fill (next bar open)
    stop:          float        # stop loss level (pre-slippage trigger)
    tp:            float        # take profit level (pre-slippage trigger)
    R:             float        # raw R distance in dollars per share
    bar_time:      str          # bar that generated the signal (HH:MM)
    session_date:  str          # YYYY-MM-DD
    # Extra context fields (strategy-specific, written to signal log)
    meta:          dict = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop)


@dataclass
class OpenPosition:
    """Tracks a live position held by the orchestrator."""
    trade_id:      str
    strategy_id:   str
    symbol:        str
    direction:     str
    entry_price:   float
    stop:          float
    tp:            float
    R_dollars:     float        # dollar risk on this trade (1R)
    shares:        int
    entry_time:    str
    session_date:  str
    entry_bar_i:   int = 0


@dataclass
class Bar:
    """A single 1-minute OHLCV bar."""
    symbol:       str
    time:         str           # HH:MM
    date:         str           # YYYY-MM-DD
    open:         float
    high:         float
    low:          float
    close:        float
    volume:       float

    @property
    def datetime_str(self) -> str:
        return f"{self.date} {self.time}"

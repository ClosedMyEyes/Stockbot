"""
models.py — Shared data models across the orchestrator.
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


@dataclass
class SessionContext:
    """
    Pre-computed session-level context fed to every strategy on each bar.
    Populated by the data layer before the bar loop starts.
    """
    symbol:            str
    session_date:      str
    prior_close:       Optional[float] = None
    today_open:        Optional[float] = None
    daily_atr:         Optional[float] = None
    vol_median:        Optional[float] = None    # rolling 20-day median daily volume
    vol_regime_ratio:  Optional[float] = None    # yesterday_range / 20-day mean range
    first_bar_vol_ratio: Optional[float] = None  # first bar vol / rolling median first-bar vol
    gap_pct:           Optional[float] = None    # (today_open - prior_close) / prior_close
    gap_size:          Optional[float] = None    # abs(today_open - prior_close)

    # Updated bar by bar
    session_high:      float = 0.0
    session_low:       float = 9999999.0
    vwap:              float = 0.0
    cum_pv:            float = 0.0
    cum_v:             float = 0.0
    bar_index:         int   = 0

    def update_vwap(self, bar: Bar):
        tp = (bar.high + bar.low + bar.close) / 3.0
        self.cum_pv += tp * bar.volume
        self.cum_v  += bar.volume
        self.vwap    = self.cum_pv / self.cum_v if self.cum_v > 0 else bar.close

    def update_extremes(self, bar: Bar):
        self.session_high = max(self.session_high, bar.high)
        self.session_low  = min(self.session_low,  bar.low)
        self.bar_index   += 1

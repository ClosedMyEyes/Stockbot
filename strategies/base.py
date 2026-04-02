"""
strategies/base.py — Abstract base class for all strategy modules.

Each strategy subclass implements:
  - reset_session(ctx)    called at 09:30 with fresh SessionContext
  - on_bar(bar, ctx)      called on each 1-min bar close → returns Signal | None
  - on_exit(result)       called when a position closes (for state cleanup)
"""

from abc import ABC, abstractmethod
from typing import Optional
from ..models import Signal, Bar, SessionContext


class BaseStrategy(ABC):
    """
    One instance per (strategy_id, symbol) pair.
    Stateful — carries its state machine across bars within a session.
    """

    def __init__(self, strategy_id: str, symbol: str, params: dict):
        self.strategy_id = strategy_id
        self.symbol      = symbol
        self.params      = params
        self.state       = "IDLE"
        self._in_trade   = False   # set by orchestrator when signal is accepted

    @abstractmethod
    def reset_session(self, ctx: SessionContext) -> None:
        """Called at session start (pre-open). Reset all intra-session state."""
        ...

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        """
        Process one bar. Returns a Signal if the strategy wants to enter,
        otherwise None. Must NOT enter a trade on its own — that's the
        orchestrator's job.
        """
        ...

    def on_exit(self, result_r: float, exit_reason: str) -> None:
        """Called by orchestrator when the position exits. Override if needed."""
        self._in_trade = False
        self.state     = "DONE"

    def mark_in_trade(self):
        """Called by orchestrator after accepting signal."""
        self._in_trade = True
        self.state     = "IN_TRADE"

    # ── helpers shared across subclasses ─────────────────────────────────────

    @staticmethod
    def apply_slippage(price: float, direction: int, slippage_pct: float) -> float:
        """direction: +1 = long (pay more), -1 = short (receive less)."""
        return price + direction * price * slippage_pct

    @staticmethod
    def compute_tp_obs_level(entry: float, obs_level: float,
                              direction: int, tp_mult: float) -> float:
        """obs_level TP: scale between entry and obs extreme."""
        distance = abs(obs_level - entry)
        return entry + direction * tp_mult * distance

    @staticmethod
    def compute_tp_fixed_r(entry: float, R: float,
                            direction: int, tp_mult: float) -> float:
        return entry + direction * tp_mult * R

    def __repr__(self):
        return f"{self.strategy_id}({self.symbol}) [{self.state}]"

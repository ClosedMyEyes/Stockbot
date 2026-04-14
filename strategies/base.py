"""strategies/base.py — BaseStrategy abstract base class."""

from abc import ABC, abstractmethod
from typing import Optional


class BaseStrategy(ABC):

    def __init__(self, strategy_id: str, symbol: str, params: dict):
        self.strategy_id = strategy_id
        self.symbol      = symbol
        self.params      = params
        self._in_trade   = False

    # ── Orchestrator callbacks ────────────────────────────────────────────────

    @abstractmethod
    def reset_session(self, ctx) -> None:
        """Called once at the start of each new session. Reset all state."""

    @abstractmethod
    def on_bar(self, bar, ctx):
        """
        Called on every RTH bar. Return a Signal to enter a trade, or None.
        Must NOT be called while _in_trade=True (orchestrator gates this).
        """

    def mark_in_trade(self) -> None:
        """Called by orchestrator after a signal is accepted and executed."""
        self._in_trade = True

    def on_exit(self, result_r: float, reason: str) -> None:
        """
        Called by orchestrator after a position closes.
        Override to re-arm the strategy for a second entry (e.g. gap_fill_small_multi).
        Default: stay dormant for the rest of the session (one-trade-per-session behaviour).
        """
        self._in_trade = False
        # Subclasses that allow multiple trades override this to also reset state.

    def __str__(self):
        return f"{self.strategy_id}({self.symbol})"

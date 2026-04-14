"""
strategies/gap_fill_variants.py — Concrete gap fill strategy classes.

All inherit from GapFillBaseStrategy which holds the core state machine.

  GapFillLargeStrategy     — gap-down LONG, session_extreme stop, 1 trade/session
  GapFillSmallStrategy     — gap-up SHORT, gap_open_buffer stop, 1 trade/session
  GapFillSmallMultiStrategy — gap-up SHORT, gap_open_buffer re-entries, unlimited trades
  GapFillBigStrategy       — gap-up SHORT, gap_open_buffer stop, 1 trade/session
"""

from ._gap_fill_base import GapFillBaseStrategy


# =============================================================================
# gap_fill_large — long side, session_extreme stop, one trade per session
# =============================================================================

class GapFillLargeStrategy(GapFillBaseStrategy):
    """
    Gap-down LONG fill. Stop = session_low at trigger - buffer.
    No re-entry; session ends after first trade.
    """
    # All behaviour from base class. stop_type comes from params ("session_extreme").
    pass


# =============================================================================
# gap_fill_small — short side, gap_open_buffer stop, one trade per session
# =============================================================================

class GapFillSmallStrategy(GapFillBaseStrategy):
    """
    Gap-up SHORT fill. Stop = today_open + buffer (gap_open_buffer).
    One trade per session.
    """
    pass


# =============================================================================
# gap_fill_small_multi — short side, multi-trade, reentry_stop_type
# =============================================================================

class GapFillSmallMultiStrategy(GapFillBaseStrategy):
    """
    Gap-up SHORT fill. Allows multiple trades per session.
    Re-entries switch stop type to gap_open_buffer (session extreme expands too much).
    """

    def _max_trades_per_session(self) -> int:
        return self.params.get("max_trades_per_session", 0)  # 0 = unlimited

    def _effective_stop_type(self, session_trade_count: int) -> str:
        if session_trade_count == 0:
            return self.params.get("stop_type", "gap_open_buffer")
        # Re-entries: honour reentry_stop_type; default to gap_open_buffer
        rst = self.params.get("reentry_stop_type", "session_extreme")
        if rst == "inherit":
            return self.params.get("stop_type", "gap_open_buffer")
        return rst

    def on_exit(self, result_r: float, reason: str) -> None:
        """Re-arm state for the next potential entry."""
        self._in_trade            = False
        self._session_trade_count += 1
        max_t = self._max_trades_per_session()
        if max_t > 0 and self._session_trade_count >= max_t:
            self._session_active = False
            return
        # Reset intra-trade state, keep session-level gap qualification
        self._state              = 0   # _WAIT_ENTRY
        self._bars_scanned       = 0
        self._cooldown_remaining = self.params.get("reentry_cooldown_bars", 5)
        self._pending_entry      = None
        self._stop_raw           = None
        self._stop_fill          = None
        self._tp_fill            = None
        self._tp_raw_state       = None
        self._R                  = None
        self._entry_time_str     = None
        self._entry_bar_i        = None
        self._gap_fill_entry     = None
        self._extreme_entry      = None


# =============================================================================
# gap_fill_big — short side, gap_open_buffer stop, one trade per session
# Differentiated by gap_atr_ratio band (0.7–1.0) set in config params
# =============================================================================

class GapFillBigStrategy(GapFillBaseStrategy):
    """
    Gap-up SHORT fill. Stop = today_open + buffer.
    Targets the 0.7–1.0 ATR band (large-but-not-huge gaps).
    One trade per session.
    """
    pass

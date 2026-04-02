"""
risk/risk_manager.py — Risk layer.

Responsibilities:
  1. Size trades: given entry/stop and RISK_PER_TRADE_DOLLARS → share count
  2. Check max simultaneous positions
  3. Resolve same-symbol conflicts (first signal wins)
  4. Check daily loss limit
  5. Track open positions
"""

import logging
import uuid
from typing import Dict, List, Optional
from ..models import Signal, OpenPosition
from .. import config

log = logging.getLogger("risk")


class RiskManager:

    def __init__(self):
        self.open_positions: Dict[str, OpenPosition] = {}   # trade_id → OpenPosition
        self.daily_pnl_dollars: float = 0.0
        self.daily_r_total:     float = 0.0
        self.halted:            bool  = False
        self._session_date:     str   = ""

    # ── Session management ────────────────────────────────────────────────────

    def reset_day(self, session_date: str):
        self.daily_pnl_dollars = 0.0
        self.daily_r_total     = 0.0
        self.halted            = False
        self._session_date     = session_date
        # Note: open_positions are NOT cleared here — EOD close handles that.
        log.info(f"[{session_date}] Risk manager reset. Halt cleared.")

    # ── Main gate ─────────────────────────────────────────────────────────────

    def approve(self, signal: Signal) -> Optional[OpenPosition]:
        """
        Run all risk checks against a signal.
        Returns an OpenPosition ready to register, or None if rejected.
        Logs the rejection reason.
        """
        if self.halted:
            log.warning(f"REJECTED [{signal.strategy_id} {signal.symbol}] — daily loss limit HALT")
            return None

        # Max simultaneous positions
        if len(self.open_positions) >= config.MAX_SIMULTANEOUS_POSITIONS:
            log.warning(
                f"REJECTED [{signal.strategy_id} {signal.symbol}] — "
                f"max positions ({config.MAX_SIMULTANEOUS_POSITIONS}) reached"
            )
            return None

        # Same-symbol conflict
        conflict = self._same_symbol_conflict(signal.symbol)
        if conflict:
            log.warning(
                f"CONFLICT [{signal.strategy_id} {signal.symbol}] — "
                f"already held by {conflict.strategy_id} (first-signal wins)"
            )
            return None

        # Size trade
        risk_per_share = abs(signal.entry_price - signal.stop)
        if risk_per_share <= 0:
            log.error(f"REJECTED [{signal.strategy_id} {signal.symbol}] — zero R")
            return None

        shares = max(1, int(config.RISK_PER_TRADE_DOLLARS / risk_per_share))
        R_dollars = shares * risk_per_share

        trade_id = str(uuid.uuid4())[:8]
        pos = OpenPosition(
            trade_id     = trade_id,
            strategy_id  = signal.strategy_id,
            symbol       = signal.symbol,
            direction    = signal.direction,
            entry_price  = signal.entry_price,
            stop         = signal.stop,
            tp           = signal.tp,
            R_dollars    = R_dollars,
            shares       = shares,
            entry_time   = signal.bar_time,
            session_date = signal.session_date,
        )
        log.info(
            f"APPROVED [{signal.strategy_id} {signal.symbol}] "
            f"{signal.direction.upper()} {shares}sh @ {signal.entry_price:.2f} "
            f"stop={signal.stop:.2f}  tp={signal.tp:.2f}  "
            f"R=${R_dollars:.2f} (${config.RISK_PER_TRADE_DOLLARS:.0f} target)"
        )
        return pos

    def register_position(self, pos: OpenPosition):
        """Call after execution confirms the fill."""
        self.open_positions[pos.trade_id] = pos

    def close_position(self, trade_id: str, exit_price: float,
                        exit_reason: str) -> Optional[float]:
        """
        Close an open position. Returns result_r or None if not found.
        Updates daily P&L.
        """
        pos = self.open_positions.pop(trade_id, None)
        if pos is None:
            log.error(f"close_position: trade_id {trade_id} not found")
            return None

        dir_mult = 1 if pos.direction == "long" else -1
        result_r = (exit_price - pos.entry_price) * dir_mult / abs(pos.entry_price - pos.stop)
        pnl      = (exit_price - pos.entry_price) * dir_mult * pos.shares

        self.daily_pnl_dollars += pnl
        self.daily_r_total     += result_r

        log.info(
            f"CLOSED [{pos.strategy_id} {pos.symbol}] "
            f"exit={exit_price:.2f}  result={result_r:+.2f}R  "
            f"P&L=${pnl:+.2f}  daily_P&L=${self.daily_pnl_dollars:+.2f}"
        )

        # Check daily loss limit
        if self.daily_pnl_dollars <= -abs(config.DAILY_LOSS_LIMIT_DOLLARS):
            self.halted = True
            log.warning(
                f"DAILY LOSS LIMIT HIT — ${self.daily_pnl_dollars:.2f}. "
                f"No new signals for the session."
            )

        return result_r

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _same_symbol_conflict(self, symbol: str) -> Optional[OpenPosition]:
        for pos in self.open_positions.values():
            if pos.symbol == symbol:
                return pos
        return None

    def get_open_positions(self) -> List[OpenPosition]:
        return list(self.open_positions.values())

    def summary(self) -> dict:
        return {
            "open_count":          len(self.open_positions),
            "daily_pnl_dollars":   round(self.daily_pnl_dollars, 2),
            "daily_r_total":       round(self.daily_r_total, 4),
            "halted":              self.halted,
            "positions":           [
                {
                    "trade_id":    p.trade_id,
                    "strategy":    p.strategy_id,
                    "symbol":      p.symbol,
                    "direction":   p.direction,
                    "shares":      p.shares,
                    "entry":       p.entry_price,
                    "stop":        p.stop,
                    "tp":          p.tp,
                    "R_dollars":   p.R_dollars,
                }
                for p in self.open_positions.values()
            ]
        }

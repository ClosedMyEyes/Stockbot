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
from collections import defaultdict
from typing import Dict, List, Optional, Set
from ..models import Signal, OpenPosition
from .. import config

log = logging.getLogger("risk")


class RiskManager:

    def __init__(self):
        self.open_positions:    Dict[str, OpenPosition] = {}
        self.daily_pnl_dollars: float = 0.0
        self.daily_r_total:     float = 0.0
        self.halted:            bool  = False   # portfolio-level halt
        self._session_date:     str   = ""

        # Per-strategy tracking
        self._strategy_pnl:     Dict[str, float] = defaultdict(float)
        self._halted_strategies: Set[str]        = set()

    # ── Session management ────────────────────────────────────────────────────

    def reset_day(self, session_date: str):
        self.daily_pnl_dollars  = 0.0
        self.daily_r_total      = 0.0
        self.halted             = False
        self._session_date      = session_date
        self._strategy_pnl      = defaultdict(float)
        self._halted_strategies = set()
        # Note: open_positions are NOT cleared here — EOD close handles that.
        log.info(f"[{session_date}] Risk manager reset. All halts cleared.")

    # ── Main gate ─────────────────────────────────────────────────────────────

    def approve(self, signal: Signal) -> Optional[OpenPosition]:
        """
        Run all risk checks against a signal.
        Returns an OpenPosition ready to register, or None if rejected.
        """
        strat_id = signal.strategy_id

        # Portfolio-level halt
        if self.halted:
            log.warning(f"REJECTED [{strat_id} {signal.symbol}] — portfolio daily loss HALT")
            return None

        # Per-strategy halt
        if strat_id in self._halted_strategies:
            log.warning(f"REJECTED [{strat_id} {signal.symbol}] — strategy daily DD HALT")
            return None

        # Max simultaneous positions
        if len(self.open_positions) >= config.MAX_SIMULTANEOUS_POSITIONS:
            log.warning(
                f"REJECTED [{strat_id} {signal.symbol}] — "
                f"max positions ({config.MAX_SIMULTANEOUS_POSITIONS}) reached"
            )
            return None

        # Same-symbol conflict
        conflict = self._same_symbol_conflict(signal.symbol)
        if conflict:
            log.warning(
                f"CONFLICT [{strat_id} {signal.symbol}] — "
                f"already held by {conflict.strategy_id} (priority ordering)"
            )
            return None

        # Per-strategy risk sizing
        strat_risk = config.STRATEGY_RISK.get(strat_id, {})
        risk_per_trade = strat_risk.get("risk_per_trade", 100.0)

        risk_per_share = abs(signal.entry_price - signal.stop)
        if risk_per_share <= 0:
            log.error(f"REJECTED [{strat_id} {signal.symbol}] — zero R")
            return None

        shares    = max(1, int(risk_per_trade / risk_per_share))
        R_dollars = shares * risk_per_share

        trade_id = str(uuid.uuid4())[:8]
        pos = OpenPosition(
            trade_id     = trade_id,
            strategy_id  = strat_id,
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
            f"APPROVED [{strat_id} {signal.symbol}] "
            f"{signal.direction.upper()} {shares}sh @ {signal.entry_price:.2f} "
            f"stop={signal.stop:.2f}  tp={signal.tp:.2f}  "
            f"R=${R_dollars:.2f} (target ${risk_per_trade:.0f})"
        )
        return pos

    def register_position(self, pos: OpenPosition):
        """Call after execution confirms the fill."""
        self.open_positions[pos.trade_id] = pos

    def restore_position(self, pos: OpenPosition):
        """
        Re-register a position after crash recovery / reconnect reconciliation.
        Does NOT affect daily P&L stats (those are restored separately via
        restore_session_stats). Does NOT send any execution orders.
        """
        self.open_positions[pos.trade_id] = pos
        log.info(f"RESTORED position: {pos.strategy_id}({pos.symbol}) "
                 f"trade_id={pos.trade_id}")

    def restore_session_stats(self, daily_r: float, daily_pnl: float,
                               halted: bool) -> None:
        """
        Restore daily P&L stats from state.json on startup.
        Note: per-strategy P&L is not persisted in state.json (positions only),
        so we restore the portfolio totals and re-derive halts from STRATEGY_RISK.
        """
        self.daily_r_total     = daily_r
        self.daily_pnl_dollars = daily_pnl
        self.halted            = halted
        log.info(f"Restored session stats: R={daily_r:+.4f}  "
                 f"P&L=${daily_pnl:+.2f}  halted={halted}")

    def close_position(self, trade_id: str, exit_price: float,
                        exit_reason: str) -> Optional[float]:
        """
        Close an open position. Returns result_r or None if not found.
        Updates daily P&L and checks per-strategy / portfolio DD limits.
        """
        pos = self.open_positions.pop(trade_id, None)
        if pos is None:
            log.error(f"close_position: trade_id {trade_id} not found")
            return None

        dir_mult = 1 if pos.direction == "long" else -1
        result_r = (exit_price - pos.entry_price) * dir_mult / abs(pos.entry_price - pos.stop)
        pnl      = (exit_price - pos.entry_price) * dir_mult * pos.shares

        self.daily_pnl_dollars           += pnl
        self.daily_r_total               += result_r
        self._strategy_pnl[pos.strategy_id] += pnl

        log.info(
            f"CLOSED [{pos.strategy_id} {pos.symbol}] "
            f"exit={exit_price:.2f}  result={result_r:+.2f}R  "
            f"P&L=${pnl:+.2f}  strategy_P&L=${self._strategy_pnl[pos.strategy_id]:+.2f}  "
            f"daily_P&L=${self.daily_pnl_dollars:+.2f}"
        )

        # Per-strategy DD halt
        strat_max_dd = config.STRATEGY_RISK.get(pos.strategy_id, {}).get("max_dd", None)
        if strat_max_dd and self._strategy_pnl[pos.strategy_id] <= -abs(strat_max_dd):
            self._halted_strategies.add(pos.strategy_id)
            log.warning(
                f"STRATEGY HALT [{pos.strategy_id}] — "
                f"daily DD ${self._strategy_pnl[pos.strategy_id]:.2f} hit "
                f"limit -${strat_max_dd:.0f}. Strategy halted for session."
            )

        # Portfolio-level halt
        if self.daily_pnl_dollars <= -abs(config.DAILY_LOSS_LIMIT_DOLLARS):
            self.halted = True
            log.warning(
                f"PORTFOLIO HALT — daily P&L ${self.daily_pnl_dollars:.2f} "
                f"hit limit -${config.DAILY_LOSS_LIMIT_DOLLARS:.0f}. "
                f"All strategies halted for session."
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
            "halted_strategies":   sorted(self._halted_strategies),
            "strategy_pnl":        {k: round(v, 2) for k, v in self._strategy_pnl.items()},
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

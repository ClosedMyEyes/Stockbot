"""
orchestrator/main.py — Central orchestration loop.

Wires together:
  data feed → strategy modules → risk manager → execution → logging

Run:
  python -m orchestrator.main [--paper] [--warmup-days 5]

Architecture:
  - One StrategyInstance per (strategy_id, symbol) pair
  - Each bar is dispatched to all relevant strategies for that symbol
  - Signals are filtered through risk, then executed
  - Positions are tracked in-memory; exits detected bar-by-bar
  - EOD: force-close any open positions at 15:59 bar close

Fixes applied (v2):
  [FIX 1] os.makedirs("logs") now runs BEFORE logging.basicConfig so the
          FileHandler never tries to open a path that doesn't exist yet.
  [FIX 2] _positions_meta and _position_entry_bar are now cleared in
          _on_new_session so stale trade metadata can't bleed across days.
  [FIX 3] _session_open is now properly gated: starts False, opens when the
          first RTH bar (>= RTH_START) arrives, and closes again after the
          EOD bar is processed. Previously it was set True unconditionally in
          _on_new_session and never reset, so the `if not self._session_open`
          guard was dead code after day 1.
  [FIX 4] feed.on_bar is assigned BEFORE feed.subscribe_bars() so no bars
          can arrive on the event handlers while the callback is still None.
"""

import argparse
import logging
import os
import sys
import datetime
from collections import defaultdict
from typing import Dict, List, Optional, Set

# ── FIX 1: create the log directory BEFORE configuring the FileHandler ────────
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join("logs", "orchestrator.log"),
                            mode="a", encoding="utf-8"),
    ],
)

from . import config
from .models import Bar, SessionContext, OpenPosition, Signal
from .strategies import build_strategy, BaseStrategy
from .risk.risk_manager import RiskManager
from .execution import get_executor
from .data.feed import IBKRFeed, SessionContextBuilder
from . import logging_layer as ll

log = logging.getLogger("orchestrator")


# =============================================================================
# ORCHESTRATOR
# =============================================================================

class Orchestrator:

    def __init__(self, paper: bool = True):
        self.paper          = paper
        self.executor       = get_executor(paper)
        self.risk           = RiskManager()
        self.ctx_builder    = SessionContextBuilder()

        # Build all (strategy, symbol) instances
        self.strategies: Dict[str, List[BaseStrategy]] = defaultdict(list)
        for strat_id, symbols in config.STRATEGY_UNIVERSES.items():
            params = config.STRATEGY_PARAMS.get(strat_id, {})
            for sym in symbols:
                instance = build_strategy(strat_id, sym, params)
                self.strategies[sym].append(instance)
                log.info(f"Registered: {strat_id}({sym})")

        # All unique symbols
        self.all_symbols: Set[str] = set(self.strategies.keys())

        # Intra-session tracking
        self._session_date:    str = ""
        self._session_ctxs:    Dict[str, SessionContext] = {}
        # FIX 3: start False — only opens when first RTH bar arrives
        self._session_open:    bool = False

        # Daily stats
        self._signal_count:    int = 0
        self._signals_accepted:int = 0
        self._wins:            int = 0
        self._losses:          int = 0
        self._eod_closes:      int = 0
        self._max_sim:         int = 0
        self._positions_meta:  Dict[str, dict] = {}   # trade_id → signal meta
        self._position_entry_bar: Dict[str, int] = {}  # trade_id → bar_index
        self._bar_counters:    Dict[str, int] = defaultdict(int)  # symbol → bar count

    # ── Main bar callback ────────────────────────────────────────────────────

    def on_bar(self, bar: Bar):
        """Called by the data feed on every completed 1-min bar."""
        self.ctx_builder.on_bar_close(bar)

        # Session boundary detection
        if bar.date != self._session_date:
            self._on_new_session(bar.date)

        self._bar_counters[bar.symbol] += 1

        # Update session context for this symbol
        ctx = self._session_ctxs.get(bar.symbol)
        if ctx is None:
            ctx = self.ctx_builder.get_context(bar.symbol, bar.date)
            self._session_ctxs[bar.symbol] = ctx

        ctx.update_vwap(bar)
        ctx.update_extremes(bar)

        # Check open position exits for this symbol (always, even pre/post RTH)
        self._check_exits(bar, ctx)

        # EOD forced close — runs before the session_open gate below
        if bar.time == config.EOD_BAR:
            self._eod_close(bar, ctx)
            # FIX 3: close the signal window after EOD so no new entries can
            #         sneak in if a stray bar arrives after 15:59
            self._session_open = False
            return

        # FIX 3: open the signal window on the first RTH bar of each session.
        # String comparison is safe here because HH:MM sorts lexicographically.
        if not self._session_open and bar.time >= config.RTH_START:
            log.info(f"[{bar.date}] RTH open — signal window now OPEN ({bar.time})")
            self._session_open = True

        # Gate: don't dispatch to strategies outside the RTH window
        if not self._session_open:
            return

        for strat in self.strategies.get(bar.symbol, []):
            try:
                signal = strat.on_bar(bar, ctx)
            except Exception as e:
                log.error(f"Strategy error [{strat}]: {e}", exc_info=True)
                continue

            if signal is None:
                continue

            self._handle_signal(signal, strat, bar)

        # Track max simultaneous positions
        n = len(self.risk.open_positions)
        if n > self._max_sim:
            self._max_sim = n

    # ── Signal handling ──────────────────────────────────────────────────────

    def _handle_signal(self, signal: Signal, strat: BaseStrategy, bar: Bar):
        self._signal_count += 1
        log.info(
            f"SIGNAL [{signal.strategy_id} {signal.symbol}] "
            f"{signal.direction.upper()} @ {signal.entry_price:.4f}  "
            f"stop={signal.stop:.4f}  tp={signal.tp:.4f}"
        )

        # Risk check
        pos = self.risk.approve(signal)

        if pos is None:
            ll.log_signal(
                strategy_id=signal.strategy_id, symbol=signal.symbol,
                session=signal.session_date, bar_time=signal.bar_time,
                direction=signal.direction, entry=signal.entry_price,
                stop=signal.stop, tp=signal.tp, R=signal.R,
                status="REJECTED", meta=signal.meta,
            )
            return

        # Execute
        ok = self.executor.send_entry(pos)
        if not ok:
            log.error(f"Execution failed for {pos.symbol} — not registering position")
            return

        # Register
        self.risk.register_position(pos)
        strat.mark_in_trade()
        self._signals_accepted += 1
        self._positions_meta[pos.trade_id]     = signal.meta
        self._position_entry_bar[pos.trade_id] = self._bar_counters[signal.symbol]

        ll.log_signal(
            strategy_id=signal.strategy_id, symbol=signal.symbol,
            session=signal.session_date, bar_time=signal.bar_time,
            direction=signal.direction, entry=signal.entry_price,
            stop=signal.stop, tp=signal.tp, R=signal.R,
            status="ACCEPTED", shares=pos.shares, meta=signal.meta,
        )

    # ── Exit detection ───────────────────────────────────────────────────────

    def _check_exits(self, bar: Bar, ctx: SessionContext):
        """Check if any open positions on this symbol should exit this bar."""
        to_close = []
        for trade_id, pos in self.risk.open_positions.items():
            if pos.symbol != bar.symbol:
                continue
            exit_p, reason = self._detect_exit(bar, pos)
            if exit_p is not None:
                to_close.append((trade_id, pos, exit_p, reason))

        for trade_id, pos, exit_p, reason in to_close:
            self._close_position(trade_id, pos, exit_p, reason, bar)

    def _detect_exit(self, bar: Bar, pos: OpenPosition):
        """Returns (exit_price, reason) or (None, None)."""
        dir_mult = 1 if pos.direction == "long" else -1
        stop     = pos.stop
        tp       = pos.tp

        # Gap through stop
        if dir_mult == 1  and bar.open < stop:
            return bar.open, "stopped (gap through stop)"
        if dir_mult == -1 and bar.open > stop:
            return bar.open, "stopped (gap through stop)"

        # Both hit (ambiguous — use entry price as a conservative neutral exit)
        stop_hit = (dir_mult == 1 and bar.low <= stop) or (dir_mult == -1 and bar.high >= stop)
        tp_hit   = (dir_mult == 1 and bar.high >= tp)  or (dir_mult == -1 and bar.low <= tp)

        if stop_hit and tp_hit:
            return pos.entry_price, "ambiguous (SL+TP same bar)"
        if stop_hit:
            return stop, "stopped"
        if tp_hit:
            return tp, "TP hit"

        return None, None

    def _close_position(self, trade_id: str, pos: OpenPosition,
                        exit_price: float, reason: str, bar: Bar):
        result_r = self.risk.close_position(trade_id, exit_price, reason)
        if result_r is None:
            return

        # Notify strategy so it can reset state
        for strat in self.strategies.get(pos.symbol, []):
            if strat.strategy_id == pos.strategy_id and strat._in_trade:
                strat.on_exit(result_r, reason)
                break

        bars_held = (self._bar_counters.get(pos.symbol, 0)
                     - self._position_entry_bar.get(trade_id, 0))
        self.executor.send_exit(pos, exit_price, reason)

        ll.log_trade(
            pos=pos, exit_price=exit_price, exit_time=bar.time,
            exit_reason=reason, result_r=result_r, bars_to_exit=bars_held,
            meta=self._positions_meta.pop(trade_id, {}),
        )
        self._position_entry_bar.pop(trade_id, None)

        if result_r > 0:
            self._wins += 1
        else:
            self._losses += 1

    # ── EOD ──────────────────────────────────────────────────────────────────

    def _eod_close(self, bar: Bar, ctx: SessionContext):
        """Force-close any positions on this symbol at 15:59 bar close."""
        to_close = [
            (tid, pos) for tid, pos in self.risk.open_positions.items()
            if pos.symbol == bar.symbol
        ]
        for trade_id, pos in to_close:
            self._close_position(trade_id, pos, bar.close, "EOD close", bar)
            self._eod_closes += 1

    # ── Session management ────────────────────────────────────────────────────

    def _on_new_session(self, session_date: str):
        if self._session_date:
            self._post_market(self._session_date)

        log.info(f"{'='*60}")
        log.info(f"NEW SESSION: {session_date}")
        log.info(f"{'='*60}")

        self._session_date     = session_date
        self._session_ctxs     = {}
        # FIX 3: stays False until first RTH bar arrives (gated in on_bar)
        self._session_open     = False
        self._signal_count     = 0
        self._signals_accepted = 0
        self._wins             = 0
        self._losses           = 0
        self._eod_closes       = 0
        self._max_sim          = 0
        self._bar_counters     = defaultdict(int)
        # FIX 2: clear stale trade metadata from the previous session
        self._positions_meta.clear()
        self._position_entry_bar.clear()

        self.risk.reset_day(session_date)

        # Reset all strategy instances for the new session
        for sym, strats in self.strategies.items():
            ctx = self.ctx_builder.get_context(sym, session_date)
            for strat in strats:
                try:
                    strat.reset_session(ctx)
                except Exception as e:
                    log.error(f"reset_session error [{strat}]: {e}", exc_info=True)

    def _post_market(self, session_date: str):
        """Write daily summary."""
        log.info(f"Post-market routine for {session_date}")
        risk_sum = self.risk.summary()
        active_strats = list(config.STRATEGY_UNIVERSES.keys())
        ll.log_daily_summary(
            session       = session_date,
            risk_summary  = risk_sum,
            signal_count  = self._signal_count,
            accepted      = self._signals_accepted,
            wins          = self._wins,
            losses        = self._losses,
            eod_closes    = self._eod_closes,
            max_sim       = self._max_sim,
            halted        = risk_sum["halted"],
            strategies    = active_strats,
        )
        log.info(
            f"Day done: {self._signals_accepted} trades | "
            f"W={self._wins} L={self._losses} EOD={self._eod_closes} | "
            f"R={risk_sum['daily_r_total']:+.2f} | "
            f"$={risk_sum['daily_pnl_dollars']:+.2f}"
        )

    # ── Startup ───────────────────────────────────────────────────────────────

    def warm_up(self, feed: IBKRFeed, days: int = 20):
        """
        Pull historical bars to prime SessionContextBuilder with rolling stats.
        Call before going live.
        """
        log.info(f"Warming up with {days} days of history...")
        for sym in self.all_symbols:
            try:
                bars = feed.request_historical_bars(sym, duration=f"{days} D")
                for bar in bars:
                    self.ctx_builder.on_bar_close(bar)
                log.info(f"Warm-up done: {sym} ({len(bars)} bars)")
            except Exception as e:
                log.error(f"Warm-up failed for {sym}: {e}")

    def run(self, warmup_days: int = 20):
        """Full production run."""
        feed = IBKRFeed(symbols=list(self.all_symbols))
        if not feed.connect():
            log.error("Cannot connect to IBKR. Exiting.")
            return

        self.warm_up(feed, days=warmup_days)

        # FIX 4: assign the callback BEFORE subscribe_bars() so no bar can
        #         arrive on a handler while self.on_bar is still None
        feed.on_bar = self.on_bar
        feed.subscribe_bars()

        log.info("Orchestrator live. Listening for bars...")
        try:
            feed.start()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down.")
        finally:
            if self._session_date:
                self._post_market(self._session_date)
            feed.stop()
            log.info("Orchestrator stopped.")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Trading Bot Orchestrator")
    parser.add_argument("--paper",       action="store_true", default=True,
                        help="Paper trading mode (default: on)")
    parser.add_argument("--live",        action="store_false", dest="paper",
                        help="Live mode — fires real SignalStack webhooks")
    parser.add_argument("--warmup-days", type=int, default=20,
                        help="Historical days to pull for warm-up (default: 20)")
    args = parser.parse_args()

    if not args.paper:
        confirm = input(
            "⚠️  LIVE MODE — this will fire real orders via SignalStack. "
            "Type 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

    orch = Orchestrator(paper=args.paper)
    orch.run(warmup_days=args.warmup_days)


if __name__ == "__main__":
    main()

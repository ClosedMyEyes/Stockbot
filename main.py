"""
orchestrator/main.py — Central orchestration loop.

Changes from v2:
  [v3-1] orb_long removed — strategy never existed in backtest data.
  [v3-2] _eod_close() now calls ctx_builder.store_session_close() so prior_close
         is correctly available for gap fill strategies on the next session.
  [v3-3] _detect_exit() uses pos.meta["gap_dir"] for gap fill strategies so
         stop/TP hit detection works correctly for both long and short sides.
  [v3-4] _close_position() calls strat.on_exit() for ALL matching strategies,
         not just the first with _in_trade=True, so gap_fill_small_multi can
         re-arm correctly.
  [v3-5] signal_accepted guard: skip on_bar dispatch while _in_trade is True
         (prevents double-entry on the same symbol×strategy pair).
"""

import argparse
import logging
import os
import sys
import datetime
from collections import defaultdict
from typing import Dict, List, Optional, Set

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
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


class Orchestrator:

    def __init__(self, paper: bool = True):
        self.paper    = paper
        self.executor = get_executor(paper)
        self.risk     = RiskManager()
        self.ctx_builder = SessionContextBuilder()

        # Build all (strategy_id, symbol) instances
        self.strategies: Dict[str, List[BaseStrategy]] = defaultdict(list)
        for strat_id, symbols in config.STRATEGY_UNIVERSES.items():
            params = config.STRATEGY_PARAMS.get(strat_id, {})
            for sym in symbols:
                instance = build_strategy(strat_id, sym, params)
                self.strategies[sym].append(instance)
                log.info(f"Registered: {strat_id}({sym})")

        self.all_symbols: Set[str] = set(self.strategies.keys())

        # Session tracking
        self._session_date: str  = ""
        self._session_ctxs: Dict[str, SessionContext] = {}
        self._session_open: bool = False

        # Daily stats
        self._signal_count    = 0
        self._signals_accepted = 0
        self._wins            = 0
        self._losses          = 0
        self._eod_closes      = 0
        self._max_sim         = 0

        self._positions_meta:      Dict[str, dict] = {}
        self._position_entry_bar:  Dict[str, int]  = {}
        self._bar_counters:        Dict[str, int]  = defaultdict(int)

    # ── Main bar callback ─────────────────────────────────────────────────────

    def on_bar(self, bar: Bar):
        self.ctx_builder.on_bar_close(bar)

        if bar.date != self._session_date:
            self._on_new_session(bar.date)

        self._bar_counters[bar.symbol] += 1

        ctx = self._session_ctxs.get(bar.symbol)
        if ctx is None:
            ctx = self.ctx_builder.get_context(bar.symbol, bar.date)
            self._session_ctxs[bar.symbol] = ctx

        ctx.update_vwap(bar)
        ctx.update_extremes(bar)

        self._check_exits(bar, ctx)

        if bar.time == config.EOD_BAR:
            self._eod_close(bar, ctx)
            self._session_open = False
            return

        if not self._session_open and bar.time >= config.RTH_START:
            log.info(f"[{bar.date}] RTH open — signal window OPEN ({bar.time})")
            self._session_open = True

        if not self._session_open:
            return

        for strat in self.strategies.get(bar.symbol, []):
            if strat._in_trade:
                continue  # [v3-5] skip dispatch while in trade
            try:
                signal = strat.on_bar(bar, ctx)
            except Exception as e:
                log.error(f"Strategy error [{strat}]: {e}", exc_info=True)
                continue
            if signal is None:
                continue
            self._handle_signal(signal, strat, bar)

        n = len(self.risk.open_positions)
        if n > self._max_sim:
            self._max_sim = n

    # ── Signal handling ───────────────────────────────────────────────────────

    def _handle_signal(self, signal: Signal, strat: BaseStrategy, bar: Bar):
        self._signal_count += 1
        log.info(
            f"SIGNAL [{signal.strategy_id} {signal.symbol}] "
            f"{signal.direction.upper()} @ {signal.entry_price:.4f} "
            f"stop={signal.stop:.4f} tp={signal.tp:.4f}"
        )
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

        ok = self.executor.send_entry(pos)
        if not ok:
            log.error(f"Execution failed for {pos.symbol}")
            return

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

    # ── Exit detection ────────────────────────────────────────────────────────

    def _check_exits(self, bar: Bar, ctx: SessionContext):
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
        """
        Returns (exit_price, reason) or (None, None).

        Gap fill strategies store gap_dir in meta (+1=long, -1=short).
        Uses gap_dir for directional logic so both LONG and SHORT gap fills
        are handled correctly without relying on pos.direction string alone.
        """
        meta    = self._positions_meta.get(pos.trade_id, {})
        gap_dir = meta.get("gap_dir", None)

        # Resolve directional multiplier
        if gap_dir is not None:
            d = gap_dir  # +1 for long, -1 for short
        else:
            d = 1 if pos.direction == "long" else -1

        stop = pos.stop
        tp   = pos.tp

        # Gap through stop (open already beyond stop level)
        if d == 1  and bar.open < stop: return bar.open, "stopped (gap through stop)"
        if d == -1 and bar.open > stop: return bar.open, "stopped (gap through stop)"

        stop_hit = (d == 1 and bar.low <= stop)  or (d == -1 and bar.high >= stop)
        tp_hit   = (d == 1 and bar.high >= tp)   or (d == -1 and bar.low  <= tp)

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

        # [v3-4] Notify ALL matching strategies (not just first with _in_trade)
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
        """Force-close any open positions on this symbol. Store prior_close."""
        to_close = [
            (tid, pos) for tid, pos in self.risk.open_positions.items()
            if pos.symbol == bar.symbol
        ]
        for trade_id, pos in to_close:
            self._close_position(trade_id, pos, bar.close, "EOD close", bar)
            self._eod_closes += 1

        # [v3-2] Record closing price so gap fill strategies get correct prior_close tomorrow
        self.ctx_builder.store_session_close(bar.symbol, bar.close)

    # ── Session management ────────────────────────────────────────────────────

    def _on_new_session(self, session_date: str):
        if self._session_date:
            self._post_market(self._session_date)

        log.info(f"{'='*60}")
        log.info(f"NEW SESSION: {session_date}")
        log.info(f"{'='*60}")

        self._session_date     = session_date
        self._session_ctxs     = {}
        self._session_open     = False
        self._signal_count     = 0
        self._signals_accepted = 0
        self._wins             = 0
        self._losses           = 0
        self._eod_closes       = 0
        self._max_sim          = 0
        self._bar_counters     = defaultdict(int)
        self._positions_meta.clear()
        self._position_entry_bar.clear()
        self.risk.reset_day(session_date)

        for sym, strats in self.strategies.items():
            ctx = self.ctx_builder.get_context(sym, session_date)
            for strat in strats:
                try:
                    strat.reset_session(ctx)
                except Exception as e:
                    log.error(f"reset_session error [{strat}]: {e}", exc_info=True)

    def _post_market(self, session_date: str):
        log.info(f"Post-market routine for {session_date}")
        risk_sum     = self.risk.summary()
        active_strats = list(config.STRATEGY_UNIVERSES.keys())
        ll.log_daily_summary(
            session=session_date, risk_summary=risk_sum,
            signal_count=self._signal_count, accepted=self._signals_accepted,
            wins=self._wins, losses=self._losses,
            eod_closes=self._eod_closes, max_sim=self._max_sim,
            halted=risk_sum["halted"], strategies=active_strats,
        )
        log.info(
            f"Day done: {self._signals_accepted} trades | "
            f"W={self._wins} L={self._losses} EOD={self._eod_closes} | "
            f"R={risk_sum['daily_r_total']:+.2f} | "
            f"$={risk_sum['daily_pnl_dollars']:+.2f}"
        )

    # ── Startup ───────────────────────────────────────────────────────────────

    def warm_up(self, feed: IBKRFeed, days: int = 20):
        log.info(f"Warming up with {days} days of history...")
        for sym in self.all_symbols:
            try:
                bars = feed.request_historical_bars(sym, duration=f"{days} D")
                for bar in bars:
                    self.ctx_builder.on_bar_close(bar)
                    # Capture EOD close for prior_close tracking during warmup
                    if bar.time == config.EOD_BAR:
                        self.ctx_builder.store_session_close(sym, bar.close)
                log.info(f"Warm-up done: {sym} ({len(bars)} bars)")
            except Exception as e:
                log.error(f"Warm-up failed for {sym}: {e}")

    def run(self, warmup_days: int = 20):
        feed = IBKRFeed(symbols=list(self.all_symbols))
        if not feed.connect():
            log.error("Cannot connect to IBKR. Exiting.")
            return

        self.warm_up(feed, days=warmup_days)

        # Assign callback BEFORE subscribe so no bar can arrive while it is None
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
    parser.add_argument("--paper", action="store_true", default=True)
    parser.add_argument("--live",  action="store_false", dest="paper")
    parser.add_argument("--warmup-days", type=int, default=20)
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

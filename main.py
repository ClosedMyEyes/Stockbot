"""
orchestrator/main.py — Central orchestration loop (v4 — Robustness edition).

Robustness additions vs v3:
  [R1] State persistence  — state.json written atomically on every position
       change. Survives crash, kill -9, TWS restart, network blip.
  [R2] Reconnect loop     — exponential backoff (5s→10s→20s→...→5min) in a
       daemon thread. Gives up after 16:15 (market closed). On reconnect,
       reconciles state.json against actual IBKR positions.
  [R3] Fill verification  — 2 bars after entry, reqPositions() confirms actual
       shares. Adjusts sizing on partial fill; clears state on complete miss.
  [R4] Bar dedup          — (symbol, date, time) set drops duplicate bars that
       ib_insync occasionally re-emits.
  [R5] EOD safety timer   — threading.Timer fires at 16:00:30 regardless of
       whether the 15:59 bar arrived. Guarantees all positions are closed.
  [R6] State timeouts     — strategies stuck in a non-idle state for >90 bars
       since last transition are auto-reset (orphaned pending_entry etc.).
"""

import argparse
import datetime
import logging
import os
import sys
import threading
import time
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
from .state_manager import StateManager

log = logging.getLogger("orchestrator")

# Reconnect backoff schedule (seconds)
_BACKOFF = [5, 10, 20, 40, 80, 160, 300]   # then repeats 300s
_RECONNECT_GIVE_UP = datetime.time(16, 15)   # after this, don't bother


class Orchestrator:

    def __init__(self, paper: bool = True):
        self.paper    = paper
        self.executor = get_executor(paper)
        self.risk     = RiskManager()
        self.ctx_builder = SessionContextBuilder()
        self.state    = StateManager()

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
        self._signal_count     = 0
        self._signals_accepted = 0
        self._wins             = 0
        self._losses           = 0
        self._eod_closes       = 0
        self._max_sim          = 0

        self._positions_meta:     Dict[str, dict] = {}
        self._position_entry_bar: Dict[str, int]  = {}
        self._bar_counters:       Dict[str, int]  = defaultdict(int)

        # [R4] Bar dedup
        self._seen_bars: Set[tuple] = set()

        # [R3] Fill verification tracking: trade_id → (expected_shares, bar_count_at_entry)
        self._pending_verify: Dict[str, tuple] = {}

        # [R5] EOD safety timer
        self._eod_timer: Optional[threading.Timer] = None

        # [R2] Disconnect state
        self._connected    = False
        self._shutting_down = False
        self._feed: Optional[IBKRFeed] = None

        # [R6] Strategy state-transition bar tracking: (sym, strategy_id) → bar_count at last transition
        self._strat_transition_bar: Dict[tuple, int] = {}
        self._STATE_TIMEOUT_BARS = 90

        # Last known bar close per symbol — used by EOD safety timer for realistic exit price
        self._last_close: Dict[str, float] = {}
        # Guard against _post_market being called twice (timer + new session)
        self._post_market_done: bool = False

    # =========================================================================
    # MAIN BAR CALLBACK
    # =========================================================================

    def on_bar(self, bar: Bar):
        # [R4] Dedup
        bar_key = (bar.symbol, bar.date, bar.time)
        if bar_key in self._seen_bars:
            return
        self._seen_bars.add(bar_key)

        self.ctx_builder.on_bar_close(bar)

        if bar.date != self._session_date:
            self._on_new_session(bar.date)

        self._bar_counters[bar.symbol] += 1
        bar_idx = self._bar_counters[bar.symbol]

        ctx = self._session_ctxs.get(bar.symbol)
        if ctx is None:
            ctx = self.ctx_builder.get_context(bar.symbol, bar.date)
            self._session_ctxs[bar.symbol] = ctx

        ctx.update_vwap(bar)
        ctx.update_extremes(bar)

        # Track last known close price for every symbol (used by EOD safety timer)
        self._last_close[bar.symbol] = bar.close

        # [R3] Fill verification — check 2 bars after entry
        self._run_fill_verification(bar)

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
                continue

            # [R6] State timeout — reset strategies stuck mid-flow
            sk = (bar.symbol, strat.strategy_id)
            self._check_strat_timeout(strat, sk, bar_idx)

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

    # =========================================================================
    # SIGNAL HANDLING
    # =========================================================================

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
            log.error(f"Execution failed for {pos.symbol} — not registering position")
            return

        self.risk.register_position(pos)
        strat.mark_in_trade()
        self._signals_accepted += 1
        self._positions_meta[pos.trade_id]     = signal.meta
        self._position_entry_bar[pos.trade_id] = self._bar_counters[signal.symbol]

        # [R1] Persist state
        self.state.on_position_open(pos, self._session_date)

        # [R3] Queue fill verification in 2 bars
        self._pending_verify[pos.trade_id] = (
            pos.shares,
            self._bar_counters[signal.symbol],
        )

        ll.log_signal(
            strategy_id=signal.strategy_id, symbol=signal.symbol,
            session=signal.session_date, bar_time=signal.bar_time,
            direction=signal.direction, entry=signal.entry_price,
            stop=signal.stop, tp=signal.tp, R=signal.R,
            status="ACCEPTED", shares=pos.shares, meta=signal.meta,
        )

    # =========================================================================
    # FILL VERIFICATION [R3]
    # =========================================================================

    def _run_fill_verification(self, bar: Bar):
        """
        For any positions pending verification on this symbol, check if
        2 bars have elapsed. If so, query IBKR for actual shares held.
        """
        if not self._pending_verify or not self._connected:
            return

        bar_now = self._bar_counters.get(bar.symbol, 0)
        to_verify = [
            tid for tid, (_, entry_bar) in list(self._pending_verify.items())
            if self._positions_meta.get(tid, {}).get("symbol", "") == bar.symbol
            or self.risk.open_positions.get(tid, None) is not None and
               getattr(self.risk.open_positions[tid], "symbol", "") == bar.symbol
        ]

        for tid in to_verify:
            if tid not in self._pending_verify:
                continue
            expected_shares, entry_bar = self._pending_verify[tid]
            if bar_now - entry_bar < 2:
                continue

            pos = self.risk.open_positions.get(tid)
            if pos is None:
                del self._pending_verify[tid]
                continue

            if pos.symbol != bar.symbol:
                continue

            actual = self._query_actual_shares(pos.symbol, pos.direction)
            del self._pending_verify[tid]

            if actual is None:
                log.warning(f"FillVerify {tid}: could not query IBKR shares")
                continue

            if actual == 0:
                # Complete miss — no fill
                log.warning(
                    f"FillVerify {tid} ({pos.symbol}): COMPLETE MISS — "
                    f"expected {expected_shares} shares, IBKR shows 0. "
                    f"Clearing position state."
                )
                self._clear_failed_fill(tid, pos)

            elif actual < expected_shares * 0.80:
                # Partial fill — adjust sizing
                log.warning(
                    f"FillVerify {tid} ({pos.symbol}): PARTIAL FILL — "
                    f"expected {expected_shares}, actual {actual}. Adjusting."
                )
                pos.shares = actual
                self.state.on_shares_adjusted(tid, actual)

            else:
                log.info(f"FillVerify {tid} ({pos.symbol}): OK — {actual} shares confirmed")

    def _query_actual_shares(self, symbol: str, direction: str) -> Optional[int]:
        """Query IBKR for current position size in this symbol."""
        try:
            ib = self._feed._ib if self._feed else None
            if ib is None:
                return None
            positions = ib.positions()
            for p in positions:
                if p.contract.symbol == symbol:
                    qty = int(abs(p.position))
                    return qty
            return 0  # no position found = complete miss
        except Exception as e:
            log.warning(f"_query_actual_shares({symbol}): {e}")
            return None

    def _clear_failed_fill(self, trade_id: str, pos: OpenPosition):
        """Remove a position that completely failed to fill."""
        self.risk.open_positions.pop(trade_id, None)
        self.state._positions.pop(trade_id, None)
        self.state.save()
        self._positions_meta.pop(trade_id, None)
        self._position_entry_bar.pop(trade_id, None)

        for strat in self.strategies.get(pos.symbol, []):
            if strat.strategy_id == pos.strategy_id and strat._in_trade:
                strat._in_trade = False
                break

        log.info(f"Cleared failed fill for {pos.strategy_id}({pos.symbol}) trade_id={trade_id}")

    # =========================================================================
    # EXIT DETECTION
    # =========================================================================

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
        """Returns (exit_price, reason) or (None, None)."""
        meta    = self._positions_meta.get(pos.trade_id, {})
        gap_dir = meta.get("gap_dir", None)
        d       = gap_dir if gap_dir is not None else (1 if pos.direction == "long" else -1)

        stop = pos.stop
        tp   = pos.tp

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

        pnl_dollars = result_r * pos.R  # 1R = RISK_PER_TRADE_DOLLARS, so R * R_dollars = pnl

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
        self._pending_verify.pop(trade_id, None)

        # [R1] Persist updated state
        self.state.on_position_close(trade_id, result_r, pnl_dollars)

        if result_r > 0:
            self._wins += 1
        else:
            self._losses += 1

    def _force_close(self, trade_id: str, pos: OpenPosition,
                     exit_price: float, reason: str):
        """
        Full close path without a Bar object — used by EOD safety timer and
        any other caller that doesn't have a live bar reference.
        Fires executor.send_exit, writes ll.log_trade, updates state.json.
        """
        result_r = self.risk.close_position(trade_id, exit_price, reason)
        if result_r is None:
            return

        pnl_dollars = result_r * pos.R

        for strat in self.strategies.get(pos.symbol, []):
            if strat.strategy_id == pos.strategy_id and strat._in_trade:
                strat.on_exit(result_r, reason)
                break

        bars_held = (self._bar_counters.get(pos.symbol, 0)
                     - self._position_entry_bar.get(trade_id, 0))

        self.executor.send_exit(pos, exit_price, reason)
        ll.log_trade(
            pos=pos, exit_price=exit_price, exit_time="16:00",
            exit_reason=reason, result_r=result_r, bars_to_exit=bars_held,
            meta=self._positions_meta.pop(trade_id, {}),
        )
        self._position_entry_bar.pop(trade_id, None)
        self._pending_verify.pop(trade_id, None)
        self.state.on_position_close(trade_id, result_r, pnl_dollars)

        if result_r > 0:
            self._wins += 1
        else:
            self._losses += 1

    # =========================================================================
    # EOD [R5]
    # =========================================================================

    def _eod_close(self, bar: Bar, ctx: SessionContext):
        """Force-close open positions for this symbol. Store prior_close."""
        to_close = [
            (tid, pos) for tid, pos in self.risk.open_positions.items()
            if pos.symbol == bar.symbol
        ]
        for trade_id, pos in to_close:
            self._close_position(trade_id, pos, bar.close, "EOD close", bar)
            self._eod_closes += 1

        self.ctx_builder.store_session_close(bar.symbol, bar.close)

    def _schedule_eod_timer(self, session_date: str):
        """
        [R5] Schedule a safety timer for 16:00:30 that closes any positions
        still open even if the 15:59 bar never arrives for some symbols.
        """
        if self._eod_timer is not None:
            self._eod_timer.cancel()

        try:
            today = datetime.date.fromisoformat(session_date)
        except ValueError:
            return

        target = datetime.datetime.combine(
            today, datetime.time(16, 0, 30)
        )
        now    = datetime.datetime.now()
        delay  = (target - now).total_seconds()

        if delay <= 0:
            return  # already past EOD — don't schedule

        def _eod_safety():
            log.info("EOD SAFETY TIMER fired — forcing close of any remaining open positions")
            for trade_id, pos in list(self.risk.open_positions.items()):
                # Use last known bar close for this symbol; fall back to entry_price
                # (0R) only if we genuinely have no price data at all.
                exit_price = self._last_close.get(pos.symbol, pos.entry_price)
                log.warning(
                    f"EOD safety close: {pos.strategy_id}({pos.symbol}) "
                    f"trade_id={trade_id}  exit_price={exit_price:.4f}"
                )
                # Call the full close path — fires executor.send_exit (SignalStack
                # webhook), writes ll.log_trade (CSV), updates state.json.
                self._force_close(trade_id, pos, exit_price, "EOD safety close")

            if not self._post_market_done:
                self._post_market_done = True
                self._post_market(self._session_date)

        self._eod_timer = threading.Timer(delay, _eod_safety)
        self._eod_timer.daemon = True
        self._eod_timer.start()
        log.info(f"EOD safety timer scheduled for 16:00:30 ({delay:.0f}s from now)")

    # =========================================================================
    # STATE TIMEOUT [R6]
    # =========================================================================

    def _check_strat_timeout(self, strat: BaseStrategy, sk: tuple, bar_idx: int):
        """
        If a strategy has been in a non-idle state for > _STATE_TIMEOUT_BARS,
        force-reset it. Prevents strategies getting stuck after bar gaps or
        exceptions mid-transition.
        """
        # Only applies to strategies that expose a non-trivial state machine
        state = getattr(strat, "_state", None)
        if state is None or state == 0:  # 0 = WAIT_BREAK / WAIT_ENTRY — idle
            self._strat_transition_bar[sk] = bar_idx
            return

        last_transition = self._strat_transition_bar.get(sk, bar_idx)
        if bar_idx - last_transition > self._STATE_TIMEOUT_BARS:
            log.warning(
                f"State timeout: {strat} stuck in state={state} for "
                f"{bar_idx - last_transition} bars — forcing reset"
            )
            try:
                ctx = self._session_ctxs.get(strat.symbol)
                if ctx:
                    strat.reset_session(ctx)
                else:
                    strat._state = 0
            except Exception as e:
                log.error(f"Timeout reset failed for {strat}: {e}")
            self._strat_transition_bar[sk] = bar_idx

        # Update transition bar whenever state changes
        prev_state = getattr(strat, "_prev_state_for_timeout", state)
        if state != prev_state:
            self._strat_transition_bar[sk] = bar_idx
        strat._prev_state_for_timeout = state

    # =========================================================================
    # SESSION MANAGEMENT
    # =========================================================================

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
        self._seen_bars        = set()
        self._positions_meta.clear()
        self._position_entry_bar.clear()
        self._pending_verify.clear()
        self._strat_transition_bar.clear()

        self.risk.reset_day(session_date)
        self.state.clear_session()
        self._post_market_done = False  # reset guard for new session

        # [R5] Schedule EOD safety timer
        self._schedule_eod_timer(session_date)

        for sym, strats in self.strategies.items():
            ctx = self.ctx_builder.get_context(sym, session_date)
            for strat in strats:
                try:
                    strat.reset_session(ctx)
                except Exception as e:
                    log.error(f"reset_session error [{strat}]: {e}", exc_info=True)

    def _post_market(self, session_date: str):
        if self._post_market_done:
            return  # safety timer already ran this — don't double-log
        self._post_market_done = True

        if self._eod_timer is not None:
            self._eod_timer.cancel()
            self._eod_timer = None

        log.info(f"Post-market routine for {session_date}")
        risk_sum = self.risk.summary()
        ll.log_daily_summary(
            session=session_date, risk_summary=risk_sum,
            signal_count=self._signal_count, accepted=self._signals_accepted,
            wins=self._wins, losses=self._losses,
            eod_closes=self._eod_closes, max_sim=self._max_sim,
            halted=risk_sum["halted"],
            strategies=list(config.STRATEGY_UNIVERSES.keys()),
        )
        log.info(
            f"Day done: {self._signals_accepted} trades | "
            f"W={self._wins} L={self._losses} EOD={self._eod_closes} | "
            f"R={risk_sum['daily_r_total']:+.2f} | "
            f"$={risk_sum['daily_pnl_dollars']:+.2f}"
        )

    # =========================================================================
    # RECONNECT LOOP [R2]
    # =========================================================================

    def _reconnect_loop(self, feed: IBKRFeed):
        """
        Daemon thread. On disconnect, attempts reconnect with exponential
        backoff. Gives up at 16:15 (market is closed, nothing to protect).
        On successful reconnect, reconciles state and resubscribes bars.
        """
        backoff_idx = 0

        while not self._shutting_down:
            time.sleep(_BACKOFF[min(backoff_idx, len(_BACKOFF) - 1)])
            backoff_idx += 1

            now = datetime.datetime.now().time()
            if now >= _RECONNECT_GIVE_UP:
                log.warning("Reconnect loop: past 16:15 — giving up for today")
                return

            log.info(f"Reconnect attempt {backoff_idx}...")
            try:
                success = feed.connect(
                    host=config.IBKR_HOST,
                    port=config.IBKR_PORT,
                    client_id=config.IBKR_CLIENT_ID,
                )
            except Exception as e:
                log.warning(f"Reconnect failed: {e}")
                continue

            if not success:
                continue

            # Reconnected — reconcile state
            log.info("Reconnected. Reconciling state...")
            try:
                restored, ghosts, orphans = self.state.reconcile(
                    ib           = feed._ib,
                    risk_manager = self.risk,
                    strategy_map = self.strategies,
                    session_date = self._session_date,
                )
                log.info(
                    f"Reconciliation: restored={restored} "
                    f"ghost_closed={ghosts} orphans={orphans}"
                )
            except Exception as e:
                log.error(f"Reconciliation failed: {e}", exc_info=True)

            # Resubscribe bars
            try:
                feed.on_bar = self.on_bar
                feed.subscribe_bars()
                self._connected = True
                log.info("Resubscribed to bars — resuming normal operation")
                return  # exit reconnect loop; feed will call us if it disconnects again
            except Exception as e:
                log.error(f"subscribe_bars() after reconnect failed: {e}")
                self._connected = False

    def _on_disconnect(self, feed: IBKRFeed):
        """Called by the feed when the IBKR connection drops."""
        self._connected = False
        log.warning("IBKR connection lost — starting reconnect loop")
        t = threading.Thread(
            target=self._reconnect_loop, args=(feed,),
            daemon=True, name="reconnect-loop"
        )
        t.start()

    # =========================================================================
    # STARTUP
    # =========================================================================

    def warm_up(self, feed: IBKRFeed, days: int = 20):
        log.info(f"Warming up with {days} days of history...")
        for sym in self.all_symbols:
            try:
                bars = feed.request_historical_bars(sym, duration=f"{days} D")
                for bar in bars:
                    self.ctx_builder.on_bar_close(bar)
                    if bar.time == config.EOD_BAR:
                        self.ctx_builder.store_session_close(sym, bar.close)
                log.info(f"Warm-up done: {sym} ({len(bars)} bars)")
            except Exception as e:
                log.error(f"Warm-up failed for {sym}: {e}")

    def _startup_reconcile(self, feed: IBKRFeed, today: str) -> bool:
        """
        [R1] On every startup, check if there's a state file from today's
        session. If so, reconcile against IBKR before doing anything else.
        Returns True if any positions were restored.
        """
        saved = self.state.load()
        if saved is None:
            return False

        saved_date = saved.get("session_date", "")
        if saved_date != today:
            log.info(f"State file is from {saved_date}, today is {today} — discarding")
            return False

        log.info(f"Found today's state file — reconciling with IBKR")
        self.state.restore_from_dict(saved)
        restored, ghosts, orphans = self.state.reconcile(
            ib           = feed._ib,
            risk_manager = self.risk,
            strategy_map = self.strategies,
            session_date = today,
        )
        log.info(
            f"Startup reconciliation: restored={restored} "
            f"ghost_closed={ghosts} orphans={orphans}"
        )
        # Restore daily stats from state
        self.risk._daily_r_total    = saved.get("daily_r_total", 0.0)
        self.risk._daily_pnl_dollars = saved.get("daily_pnl_dollars", 0.0)
        self.risk._halted           = saved.get("halted", False)

        return restored > 0

    def run(self, warmup_days: int = 20):
        feed = IBKRFeed(symbols=list(self.all_symbols))
        self._feed = feed

        if not feed.connect(
            host=config.IBKR_HOST,
            port=config.IBKR_PORT,
            client_id=config.IBKR_CLIENT_ID,
        ):
            log.error("Cannot connect to IBKR. Exiting.")
            return

        self._connected = True

        # [R1] Startup reconciliation before warming up
        today = datetime.date.today().isoformat()
        self._startup_reconcile(feed, today)

        self.warm_up(feed, days=warmup_days)

        # Wire disconnect handler
        feed.on_disconnect = lambda: self._on_disconnect(feed)

        # Assign bar callback BEFORE subscribe
        feed.on_bar = self.on_bar
        feed.subscribe_bars()

        log.info("Orchestrator live. Listening for bars...")
        try:
            feed.start()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down.")
        finally:
            self._shutting_down = True
            if self._eod_timer:
                self._eod_timer.cancel()
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

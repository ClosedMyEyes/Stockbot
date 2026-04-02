"""
data/feed.py — Live data layer.

Provides:
  IBKRFeed    — subscribes to 1-min bars via ib_insync, calls on_bar callback
  SessionContextBuilder — pre-computes ATR, VWAP context, gap metrics pre-open

Requirements:
  pip install ib_insync
  TWS or IB Gateway running, paper trading enabled, port 7497.

Fixes applied (v2):
  [FIX 1] get_context: `if ctx.prior_close and ctx.today_open` replaced with
          `is not None` checks. The falsy check silently skipped gap
          calculations if prior_close or today_open happened to be 0.0.
  [FIX 2] _daily_atr: dates missing from _day_high/_day_low no longer
          default to 0-0=0. Missing dates are now skipped so they don't
          silently drag the ATR toward zero.
  [FIX 3] _vol_regime: same fix as above for the mean-range baseline.
          Missing-date zeroes were corrupting the regime ratio, especially
          during the first few warmup sessions.
  [FIX 4] _sessions now pruned to HISTORY_KEEP sessions per symbol on every
          new session arrival. Without pruning, the dict grew unboundedly
          across a full trading day. Corresponding day_* dicts are pruned
          in sync so memory is actually freed.
  [CLEAN] Removed unused `deque` import.
"""

import datetime
import logging
from collections import defaultdict
from typing import Callable, Dict, List, Optional

from ..models import Bar, SessionContext
from .. import config

log = logging.getLogger("data")


# =============================================================================
# SESSION CONTEXT BUILDER
# =============================================================================

class SessionContextBuilder:
    """
    Maintains rolling history to compute:
      - prior_close, today_open
      - daily ATR (14-day rolling average of daily range)
      - vol_regime_ratio (yesterday_range / 20-day mean range)
      - first_bar_vol_ratio (first bar vol / rolling 20-session median)
      - vol_median (rolling 20-session median total daily volume)

    Call:
      builder.on_bar_close(bar)         — for every RTH bar
      ctx = builder.get_context(symbol, today) — call pre-open or on first bar
    """

    ATR_PERIOD    = 14
    VOL_PERIOD    = 20
    REGIME_PERIOD = 20
    # FIX 4: keep enough history for the longest window + yesterday + small buffer
    HISTORY_KEEP  = REGIME_PERIOD + 5   # 25 sessions is plenty

    def __init__(self):
        # Per-symbol rolling history
        self._sessions:       Dict[str, List[str]]         = defaultdict(list)
        self._day_high:       Dict[str, Dict[str, float]]  = defaultdict(dict)
        self._day_low:        Dict[str, Dict[str, float]]  = defaultdict(dict)
        self._day_open:       Dict[str, Dict[str, float]]  = defaultdict(dict)
        self._day_close:      Dict[str, Dict[str, float]]  = defaultdict(dict)
        self._day_vol:        Dict[str, Dict[str, float]]  = defaultdict(dict)
        self._first_bar_vol:  Dict[str, Dict[str, float]]  = defaultdict(dict)
        self._first_bar_done: Dict[str, set]               = defaultdict(set)

    def on_bar_close(self, bar: Bar):
        sym = bar.symbol
        d   = bar.date

        # Track sessions in order
        if d not in self._day_high[sym]:
            self._sessions[sym].append(d)
            self._day_high[sym][d] = bar.high
            self._day_low[sym][d]  = bar.low
            self._day_open[sym][d] = bar.open
            self._day_vol[sym][d]  = 0.0

            # FIX 4: prune history once we exceed HISTORY_KEEP sessions
            self._prune(sym)
        else:
            self._day_high[sym][d] = max(self._day_high[sym][d], bar.high)
            self._day_low[sym][d]  = min(self._day_low[sym][d], bar.low)

        self._day_close[sym][d]  = bar.close
        self._day_vol[sym][d]   += bar.volume

        # First bar of session
        if d not in self._first_bar_done[sym]:
            self._first_bar_done[sym].add(d)
            self._first_bar_vol[sym][d] = bar.volume

    # FIX 4: drop the oldest sessions and all associated dict entries
    def _prune(self, sym: str):
        sessions = self._sessions[sym]
        while len(sessions) > self.HISTORY_KEEP:
            old = sessions.pop(0)
            for d in (self._day_high, self._day_low, self._day_open,
                      self._day_close, self._day_vol, self._first_bar_vol):
                d[sym].pop(old, None)
            self._first_bar_done[sym].discard(old)

    def get_context(self, symbol: str, today: str) -> SessionContext:
        ctx = SessionContext(symbol=symbol, session_date=today)
        sessions = self._sessions.get(symbol, [])

        # Prior sessions (excluding today)
        prior = [s for s in sessions if s < today]

        if not prior:
            return ctx

        yesterday       = prior[-1]
        ctx.prior_close = self._day_close[symbol].get(yesterday)
        ctx.today_open  = self._day_open[symbol].get(today)

        # FIX 1: use `is not None` — a close/open of 0.0 is falsy and
        #         would silently skip the gap calculation with the old check
        if ctx.prior_close is not None and ctx.today_open is not None:
            ctx.gap_pct  = (ctx.today_open - ctx.prior_close) / ctx.prior_close
            ctx.gap_size = abs(ctx.today_open - ctx.prior_close)

        ctx.daily_atr        = self._daily_atr(symbol, yesterday, prior)
        ctx.vol_regime_ratio = self._vol_regime(symbol, yesterday, prior)
        ctx.vol_median       = self._vol_median(symbol, prior)
        ctx.first_bar_vol_ratio = self._first_bar_ratio(symbol, today, prior)

        return ctx

    def _daily_atr(self, symbol: str, yesterday: str,
                   prior: List[str]) -> Optional[float]:
        window = prior[-self.ATR_PERIOD:]
        if len(window) < 3:
            return None

        # FIX 2: skip any date where high or low data is missing rather than
        #         letting a missing entry default to 0-0=0 and dragging ATR down
        ranges = [
            self._day_high[symbol][d] - self._day_low[symbol][d]
            for d in window
            if d in self._day_high[symbol] and d in self._day_low[symbol]
        ]
        if len(ranges) < 3:
            return None
        return sum(ranges) / len(ranges)

    def _vol_regime(self, symbol: str, yesterday: str,
                    prior: List[str]) -> Optional[float]:
        window = prior[-self.REGIME_PERIOD - 1: -1]   # 20 sessions before yesterday
        if len(window) < 5:
            return None

        # FIX 3: same fix as _daily_atr — skip missing dates instead of
        #         padding with zero and corrupting the mean range baseline
        ranges = [
            self._day_high[symbol][d] - self._day_low[symbol][d]
            for d in window
            if d in self._day_high[symbol] and d in self._day_low[symbol]
        ]
        if not ranges:
            return None
        mean = sum(ranges) / len(ranges)
        if mean == 0:
            return None

        if yesterday not in self._day_high[symbol] or yesterday not in self._day_low[symbol]:
            return None
        yesterday_range = (self._day_high[symbol][yesterday]
                           - self._day_low[symbol][yesterday])
        return yesterday_range / mean

    def _vol_median(self, symbol: str, prior: List[str]) -> Optional[float]:
        window = prior[-self.VOL_PERIOD:]
        if not window:
            return None
        vols = sorted(self._day_vol[symbol].get(d, 0) for d in window)
        n = len(vols)
        return (vols[n // 2 - 1] + vols[n // 2]) / 2 if n % 2 == 0 else vols[n // 2]

    def _first_bar_ratio(self, symbol: str, today: str,
                          prior: List[str]) -> Optional[float]:
        today_vol = self._first_bar_vol[symbol].get(today)
        if today_vol is None:
            return None
        window = prior[-self.VOL_PERIOD:]
        if len(window) < 5:
            return None
        fb_vols = sorted(
            self._first_bar_vol[symbol][d]
            for d in window
            if d in self._first_bar_vol[symbol]
        )
        if not fb_vols:
            return None
        n      = len(fb_vols)
        median = (fb_vols[n // 2 - 1] + fb_vols[n // 2]) / 2 if n % 2 == 0 else fb_vols[n // 2]
        return today_vol / median if median > 0 else None


# =============================================================================
# IBKR LIVE FEED
# =============================================================================

class IBKRFeed:
    """
    Connects to IBKR via ib_insync and streams 1-min bars.
    Calls on_bar(bar: Bar) for each completed 1-min bar.

    Usage:
        feed = IBKRFeed(symbols=["AAPL", "MSFT"])
        feed.on_bar = orchestrator.on_bar   # assign BEFORE subscribe_bars()
        feed.subscribe_bars()
        feed.start()                        # blocks until market close
    """

    def __init__(self, symbols: List[str]):
        self.symbols  = symbols
        self.on_bar: Optional[Callable[[Bar], None]] = None
        self._ib      = None
        self._bar_lists: Dict[str, object] = {}

    def connect(self) -> bool:
        try:
            from ib_insync import IB, Stock, util
            self._ib = IB()
            self._ib.connect(
                config.IBKR_HOST,
                config.IBKR_PORT,
                clientId=config.IBKR_CLIENT_ID,
            )
            log.info(f"Connected to IBKR at {config.IBKR_HOST}:{config.IBKR_PORT}")
            return True
        except ImportError:
            log.error("ib_insync not installed. Run: pip install ib_insync")
            return False
        except Exception as e:
            log.error(f"IBKR connection failed: {e}")
            return False

    def subscribe_bars(self):
        """Subscribe to historical+live 1-min bars for each symbol."""
        from ib_insync import Stock
        for sym in self.symbols:
            contract = Stock(sym, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            bars = self._ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                keepUpToDate=True,
            )
            bars.updateEvent += self._make_bar_handler(sym)
            self._bar_lists[sym] = bars
            log.info(f"Subscribed to 1-min bars: {sym}")

    def _make_bar_handler(self, symbol: str):
        """Returns a closure that fires on each new completed bar."""
        def handler(bars, has_new_bar: bool):
            if not has_new_bar or not bars:
                return
            last = bars[-1]
            dt   = last.date    # datetime for 1-min RTH bars from ib_insync
            bar  = Bar(
                symbol = symbol,
                time   = dt.strftime("%H:%M"),
                date   = dt.strftime("%Y-%m-%d"),
                open   = last.open,
                high   = last.high,
                low    = last.low,
                close  = last.close,
                volume = float(last.volume),
            )
            if self.on_bar:
                try:
                    self.on_bar(bar)
                except Exception as e:
                    log.error(f"on_bar callback error [{symbol}]: {e}",
                              exc_info=True)
        return handler

    def start(self):
        """Run the ib_insync event loop until stopped."""
        if self._ib is None:
            raise RuntimeError("Call connect() first.")
        log.info("IBKR event loop starting...")
        self._ib.run()

    def stop(self):
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            log.info("IBKR disconnected.")

    def request_historical_bars(self, symbol: str,
                                 duration: str = "5 D") -> List[Bar]:
        """Pull recent history for warm-up (builds SessionContextBuilder state)."""
        from ib_insync import Stock
        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)
        raw = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            keepUpToDate=False,
        )
        bars = []
        for r in raw:
            dt = r.date
            bars.append(Bar(
                symbol = symbol,
                time   = dt.strftime("%H:%M"),
                date   = dt.strftime("%Y-%m-%d"),
                open   = r.open,
                high   = r.high,
                low    = r.low,
                close  = r.close,
                volume = float(r.volume),
            ))
        return bars

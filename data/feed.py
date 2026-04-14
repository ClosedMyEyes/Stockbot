"""
data/feed.py — IBKR data feed + SessionContextBuilder.

SessionContext now carries all fields that gap fill strategies need at
reset_session() time, including prior_close (yesterday's last bar close).

Fields added vs original:
  prior_close        — yesterday's closing price (zero look-ahead)
  daily_atr          — 14-session rolling mean of daily range (shifted 1)
  vol_regime_ratio   — yesterday_range / 20-day mean_range (shifted 1)
  first_bar_vol_ratio — today first-bar vol / 20-session median first-bar vol
  vol_median_tod     — per-bar time-of-day rolling median volume (for impulse_short)
  atr                — intrabar rolling ATR (for impulse_short)
"""

import datetime
import logging
import math
from collections import defaultdict, deque
from typing import Dict, List, Optional

log = logging.getLogger("orchestrator.feed")

try:
    from ib_insync import IB, Stock, BarData
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    log.warning("ib_insync not installed — feed will not connect to IBKR")


# =============================================================================
# SESSION CONTEXT
# =============================================================================

class SessionContext:
    """
    Per-symbol, per-session context object.
    Populated incrementally by SessionContextBuilder on each bar.
    Strategies read from this in reset_session() and on_bar().
    """

    def __init__(self, symbol: str, session_date):
        self.symbol       = symbol
        self.session_date = session_date

        # ── Rolling session stats (updated bar-by-bar) ────────────────────────
        self.vwap         = 0.0
        self.session_high = 0.0
        self.session_low  = float("inf")
        self.atr          = 0.0          # intrabar 14-period ATR
        self.vol_median_tod: Optional[float] = None  # not yet implemented bar-level

        # ── Session-start context (set before any bars arrive) ────────────────
        self.prior_close:        Optional[float] = None
        self.daily_atr:          Optional[float] = None
        self.vol_regime_ratio:   Optional[float] = None
        self.first_bar_vol_ratio: Optional[float] = None
        self.median_session_vol: Optional[float] = None  # for orb_short

        # ── VWAP internals ────────────────────────────────────────────────────
        self._cum_pv = 0.0
        self._cum_v  = 0.0

        # ── ATR internals ─────────────────────────────────────────────────────
        self._prev_close:  Optional[float] = None
        self._atr_window:  deque           = deque(maxlen=14)

    def update_vwap(self, bar) -> None:
        tp             = (bar.high + bar.low + bar.close) / 3.0
        self._cum_pv  += tp * bar.volume
        self._cum_v   += bar.volume
        self.vwap      = self._cum_pv / self._cum_v if self._cum_v > 0 else bar.close

    def update_extremes(self, bar) -> None:
        self.session_high = max(self.session_high, bar.high)
        self.session_low  = min(self.session_low,  bar_low := bar.low)

    def update_atr(self, bar) -> None:
        """Intrabar session-local ATR (resets each session)."""
        prev = self._prev_close
        if prev is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev),
                abs(bar.low  - prev),
            )
        self._atr_window.append(tr)
        self._prev_close = bar.close
        self.atr = sum(self._atr_window) / len(self._atr_window)


# =============================================================================
# SESSION CONTEXT BUILDER
# =============================================================================

class SessionContextBuilder:
    """
    Maintains rolling daily statistics per symbol so that SessionContext
    objects can be pre-populated with historical context at session start.

    Statistics maintained (all zero look-ahead — shifted one session):
      prior_close         — last bar close of previous session
      daily_atr           — 14-session mean of daily range
      vol_regime_ratio    — yesterday range / 20-session mean range
      first_bar_vol_ratio — today's first bar vol / 20-session median first-bar vol
      median_session_vol  — 20-session median total daily volume (for orb_short)
    """

    _ATR_PERIOD      = 14
    _VOL_WINDOW      = 20
    _VOL_MIN_PERIODS = 5

    def __init__(self):
        # Per-symbol rolling data
        self._daily_close:     Dict[str, deque]  = defaultdict(lambda: deque(maxlen=max(self._ATR_PERIOD, self._VOL_WINDOW) + 2))
        self._daily_range:     Dict[str, deque]  = defaultdict(lambda: deque(maxlen=self._VOL_WINDOW + 2))
        self._daily_first_vol: Dict[str, deque]  = defaultdict(lambda: deque(maxlen=self._VOL_WINDOW + 2))
        self._daily_total_vol: Dict[str, deque]  = defaultdict(lambda: deque(maxlen=self._VOL_WINDOW + 2))
        self._session_high:    Dict[str, float]  = {}
        self._session_low:     Dict[str, float]  = {}
        self._session_date:    Dict[str, object] = {}
        self._is_first_bar:    Dict[str, bool]   = defaultdict(lambda: True)

        # Contexts for the current session
        self._contexts: Dict[str, SessionContext] = {}

    def on_bar_close(self, bar) -> None:
        """
        Called on every bar. Maintains rolling stats and updates the live
        SessionContext for this symbol.
        """
        sym = bar.symbol
        sd  = bar.date  # date string "YYYY-MM-DD" or date object

        # New session detection
        if self._session_date.get(sym) != sd:
            self._end_session(sym)
            self._session_date[sym]  = sd
            self._session_high[sym]  = bar.high
            self._session_low[sym]   = bar.low
            self._is_first_bar[sym]  = True

            # Build and store context for this new session
            ctx = SessionContext(sym, sd if hasattr(sd, "weekday") else
                                 datetime.date.fromisoformat(str(sd)))
            ctx.prior_close         = self._get_prior_close(sym)
            ctx.daily_atr           = self._get_daily_atr(sym)
            ctx.vol_regime_ratio    = self._get_vol_regime(sym)
            ctx.first_bar_vol_ratio = None  # filled on first bar below
            ctx.median_session_vol  = self._get_median_session_vol(sym)
            self._contexts[sym]     = ctx
        else:
            self._session_high[sym] = max(self._session_high.get(sym, bar.high), bar.high)
            self._session_low[sym]  = min(self._session_low.get(sym,  bar.low),  bar.low)

        ctx = self._contexts.get(sym)
        if ctx is None:
            return

        # First bar: capture first_bar_vol_ratio and today_open
        if self._is_first_bar[sym]:
            self._is_first_bar[sym] = False
            first_vol_med = self._get_first_bar_vol_median(sym)
            ctx.first_bar_vol_ratio = (
                bar.volume / first_vol_med
                if first_vol_med and first_vol_med > 0
                else None
            )
            # Store this session's first-bar vol for future sessions
            self._daily_first_vol[sym].append(bar.volume)

        ctx.update_vwap(bar)
        ctx.update_extremes(bar)
        ctx.update_atr(bar)

    def _end_session(self, sym: str) -> None:
        """Close out the day — push daily range and total vol to rolling windows."""
        if sym not in self._session_date:
            return
        h = self._session_high.get(sym)
        l = self._session_low.get(sym)
        if h is not None and l is not None:
            self._daily_range[sym].append(h - l)

    def get_context(self, symbol: str, session_date) -> SessionContext:
        """
        Return the SessionContext for a symbol on a given date.
        If not yet built (rare), return an empty one.
        """
        return self._contexts.get(symbol, SessionContext(symbol, session_date))

    # ── Rolling statistic helpers ─────────────────────────────────────────────

    def _get_prior_close(self, sym: str) -> Optional[float]:
        closes = self._daily_close[sym]
        return closes[-1] if closes else None

    def _get_daily_atr(self, sym: str) -> Optional[float]:
        ranges = list(self._daily_range[sym])
        if len(ranges) < self._VOL_MIN_PERIODS:
            return None
        window = ranges[-self._ATR_PERIOD:]
        return sum(window) / len(window)

    def _get_vol_regime(self, sym: str) -> Optional[float]:
        ranges = list(self._daily_range[sym])
        if len(ranges) < self._VOL_MIN_PERIODS + 1:
            return None
        yesterday_range = ranges[-1]
        window_20       = ranges[-min(20, len(ranges)) - 1 : -1]
        mean_range      = sum(window_20) / len(window_20) if window_20 else None
        if mean_range and mean_range > 0:
            return yesterday_range / mean_range
        return None

    def _get_first_bar_vol_median(self, sym: str) -> Optional[float]:
        vols = list(self._daily_first_vol[sym])
        if len(vols) < self._VOL_MIN_PERIODS:
            return None
        window = sorted(vols[-self._VOL_WINDOW:])
        mid    = len(window) // 2
        return (window[mid - 1] + window[mid]) / 2 if len(window) % 2 == 0 else window[mid]

    def _get_median_session_vol(self, sym: str) -> Optional[float]:
        vols = list(self._daily_total_vol[sym])
        if len(vols) < self._VOL_MIN_PERIODS:
            return None
        window = sorted(vols[-self._VOL_WINDOW:])
        mid    = len(window) // 2
        return (window[mid - 1] + window[mid]) / 2 if len(window) % 2 == 0 else window[mid]

    def store_session_total_vol(self, sym: str, total_vol: float) -> None:
        """Call at EOD to record total daily volume for the session just ended."""
        self._daily_total_vol[sym].append(total_vol)
        # Also record last close from context
        ctx = self._contexts.get(sym)
        if ctx and ctx.session_high > 0:
            pass  # prior_close is captured from the last bar.close — needs feed to call this
        # For prior_close: feed should call store_session_close at EOD
    
    def store_session_close(self, sym: str, close_price: float) -> None:
        """Call at EOD with the final bar's close price."""
        self._daily_close[sym].append(close_price)


# =============================================================================
# IBKR FEED
# =============================================================================

class IBKRFeed:
    """
    Wraps ib_insync to provide live 1-minute bars for a set of symbols.
    Calls self.on_bar(bar) on each completed bar.
    """

    def __init__(self, symbols: List[str]):
        self.symbols   = symbols
        self.on_bar    = None   # assigned by Orchestrator before subscribe_bars()
        self._ib       = IB() if _IB_AVAILABLE else None
        self._contracts = {}

    def connect(self, host: str = "127.0.0.1", port: int = 7497,
                client_id: int = 10) -> bool:
        if not _IB_AVAILABLE:
            log.error("ib_insync not available. Cannot connect.")
            return False
        try:
            self._ib.connect(host, port, clientId=client_id)
            log.info(f"Connected to IBKR {host}:{port} (clientId={client_id})")
            return True
        except Exception as e:
            log.error(f"IBKR connect failed: {e}")
            return False

    def request_historical_bars(self, symbol: str, duration: str = "20 D",
                                bar_size: str = "1 min") -> List:
        if not _IB_AVAILABLE or self._ib is None:
            return []
        contract = self._get_contract(symbol)
        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        return [self._adapt_bar(b, symbol) for b in bars]

    def subscribe_bars(self) -> None:
        if not _IB_AVAILABLE or self._ib is None:
            return
        for sym in self.symbols:
            contract = self._get_contract(sym)
            bars = self._ib.reqRealTimeBars(
                contract,
                barSize=5,        # 5-second bars; aggregated to 1-min in _on_rt_bar
                whatToShow="TRADES",
                useRTH=True,
            )
            bars.updateEvent += self._make_rt_handler(sym)
        log.info(f"Subscribed to real-time bars for {len(self.symbols)} symbols")

    def start(self) -> None:
        if _IB_AVAILABLE and self._ib:
            self._ib.run()

    def stop(self) -> None:
        if _IB_AVAILABLE and self._ib:
            try:
                self._ib.disconnect()
            except Exception:
                pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_contract(self, symbol: str):
        if symbol not in self._contracts:
            self._contracts[symbol] = Stock(symbol, "SMART", "USD")
        return self._contracts[symbol]

    def _adapt_bar(self, b, symbol: str):
        """Convert ib_insync BarData to our Bar model."""
        from models import Bar
        return Bar(
            symbol = symbol,
            date   = str(b.date.date()),
            time   = b.date.strftime("%H:%M"),
            open   = b.open,
            high   = b.high,
            low    = b.low,
            close  = b.close,
            volume = b.volume,
        )

    def _make_rt_handler(self, symbol: str):
        """Return a 5-sec-bar aggregator that emits 1-min bars."""
        _agg = {"bars": [], "minute": None}

        def _handler(bars, has_new_bar):
            if not has_new_bar:
                return
            b = bars[-1]
            minute = b.time.replace(second=0, microsecond=0)
            if _agg["minute"] is None:
                _agg["minute"] = minute
            if minute != _agg["minute"] and _agg["bars"]:
                _emit_minute(_agg["bars"], _agg["minute"], symbol)
                _agg["bars"]   = []
                _agg["minute"] = minute
            _agg["bars"].append(b)

        def _emit_minute(rt_bars, minute, sym):
            if self.on_bar is None:
                return
            from models import Bar
            bar = Bar(
                symbol = sym,
                date   = str(minute.date()),
                time   = minute.strftime("%H:%M"),
                open   = rt_bars[0].open,
                high   = max(b.high for b in rt_bars),
                low    = min(b.low  for b in rt_bars),
                close  = rt_bars[-1].close,
                volume = sum(b.volume for b in rt_bars),
            )
            self.on_bar(bar)

        return _handler

"""
strategies/impulse_short.py — Exhausted Impulse Retest (Short).
Extracted from short.py.

7-state machine:
  WAIT_BREAK → BUILD_PEAK → TRACK_PULLBACK → WAIT_RETEST
  → WAIT_FAILURE → WAIT_FILL → DONE (signal emitted)

The orchestrator handles IN_TRADE once the signal is accepted.
"""

import datetime
import numpy as np
from typing import Optional, List
from .base import BaseStrategy
from ..models import Signal, Bar, SessionContext


WAIT_BREAK     = "WAIT_BREAK"
BUILD_PEAK     = "BUILD_PEAK"
TRACK_PULLBACK = "TRACK_PULLBACK"
WAIT_RETEST    = "WAIT_RETEST"
WAIT_FAILURE   = "WAIT_FAILURE"
WAIT_FILL      = "WAIT_FILL"
DONE           = "DONE"

X_FULL           = 20
X_MIN            = 10
MAX_IMPULSE_BACK = 50
NO_NEW_LOW_STOP  = 5
PEAK_CUTOFF      = "15:45"


class ImpulseShort(BaseStrategy):

    def __init__(self, symbol: str, params: dict):
        super().__init__("impulse_short", symbol, params)
        p = params
        self.ATR_IMPULSE_MULT      = p["atr_impulse_mult"]
        self.IMPULSE_SIZE_PCT_MIN  = p["impulse_size_pct_min"] / 100.0
        self.IMPULSE_MIN_BARS      = p["impulse_min_bars"]
        self.DEEP_RETRACE_MIN      = p["deep_retrace_min"]
        self.DEEP_RETRACE_MAX      = p["deep_retrace_max"]
        self.RETEST_PCT            = p["retest_pct"] / 100.0
        self.RETEST_MAX_BARS       = p["retest_max_bars"]
        self.STOP_BUFFER_MULT      = p["stop_buffer_mult"]
        self.TP_MODE               = p["tp_mode"]
        self.TP_FIXED_MULT         = p["tp_fixed_mult"]
        self.MIN_R_PCT             = p["min_r_pct"] / 100.0
        self.SLIPPAGE_PCT          = p["slippage_pct"] / 100.0
        self.BREAKOUT_IBS_MIN      = p.get("breakout_ibs_min", 0.95)
        self.MIN_FAILURE_BODY_PCT  = p.get("min_failure_body_pct", 85) / 100.0
        self.MAX_PULLBACK_VOL      = p.get("max_pullback_vol_ratio", 1.8)
        self.BREAKOUT_MIN          = p.get("breakout_min", 0.75)
        self.ENTRY_TIME_START      = p.get("entry_time_start", "09:30")
        self.ENTRY_TIME_END        = p.get("entry_time_end", "15:55")
        self.EMA_SLOPE_BARS        = p.get("ema_slope_bars", 0)

        # session buffers (filled via reset_session)
        self._highs: List[float] = []
        self._lows:  List[float] = []
        self._closes: List[float] = []
        self._atrs:   List[float] = []
        self._vwaps:  List[float] = []
        self._emas:   List[float] = []
        self._vols:   List[float] = []
        self._times:  List[str]   = []
        self._vol_median_tod: List[float] = []
        self._reset_sv()

    def _reset_sv(self):
        self.sv = dict(
            impulse_low=None, impulse_high=None, impulse_size=None,
            peak_time=None, peak_bar_idx=None, breakout_bar_idx=None,
            pullback_low=None, deep_retrace_ratio=None, bars_in_pullback=None,
            retest_bar_idx=None, retest_wick_extension=None,
            failure_candle_body_pct=None, failure_candle_close_vs_vwap=None,
            stop=None, tp=None,
            ref_high=None, base_low=None,
            breakout_ibs=None, impulse_atr_ratio=None,
            impulse_consec_bars=None, breakout_bar_range_atr=None,
            _impulse_bar_vol=None, _pullback_vol_sum=0.0,
            pullback_vol_ratio=None,
        )

    def reset_session(self, ctx: SessionContext) -> None:
        self.state = WAIT_BREAK
        self._reset_sv()
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._atrs.clear()
        self._vwaps.clear()
        self._emas.clear()
        self._vols.clear()
        self._times.clear()
        self._vol_median_tod.clear()
        self._skipped = False

    def on_bar(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        if self._skipped or self.state == DONE or self._in_trade:
            return None

        # Accumulate bar arrays
        i = len(self._highs)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        self._vols.append(bar.volume)
        self._times.append(bar.time)
        # ATR from session context (we'll approximate using running intrabar TR)
        # The ctx doesn't carry bar-level ATR, so we compute it inline
        _atr = self._rolling_atr(i)
        self._atrs.append(_atr)
        self._vwaps.append(ctx.vwap)
        # EMA approximation (exponential smoothing on close)
        if len(self._emas) == 0:
            self._emas.append(bar.close)
        else:
            span = max(self.EMA_SLOPE_BARS if self.EMA_SLOPE_BARS > 0 else 20, 2)
            alpha = 2 / (span + 1)
            self._emas.append(alpha * bar.close + (1 - alpha) * self._emas[-1])
        # vol_median_tod approximation: use ctx.vol_median as a proxy
        self._vol_median_tod.append(ctx.vol_median or bar.volume)

        if i < X_MIN:
            return None

        return self._dispatch(i, bar, ctx)

    def _rolling_atr(self, i: int, period: int = 14) -> float:
        n = min(i + 1, period)
        if n == 0:
            return self._highs[i] - self._lows[i]
        trs = []
        for k in range(max(0, i - n + 1), i + 1):
            hl = self._highs[k] - self._lows[k]
            trs.append(hl)
        return sum(trs) / len(trs)

    def _dispatch(self, i: int, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        if self.state == WAIT_BREAK:
            return self._handle_wait_break(i, bar)
        elif self.state == BUILD_PEAK:
            return self._handle_build_peak(i, bar)
        elif self.state == TRACK_PULLBACK:
            return self._handle_track_pullback(i, bar)
        elif self.state == WAIT_RETEST:
            return self._handle_wait_retest(i, bar)
        elif self.state == WAIT_FAILURE:
            return self._handle_wait_failure(i, bar)
        elif self.state == WAIT_FILL:
            return self._handle_wait_fill(i, bar)
        return None

    def _handle_wait_break(self, i: int, bar: Bar) -> Optional[Signal]:
        lookback = min(i, X_FULL)
        prior_high = max(self._highs[i - lookback: i]) if lookback > 0 else bar.high
        prior_low  = min(self._lows[i - lookback: i])  if lookback > 0 else bar.low

        _thresh = prior_high * 0.0001
        floor_for_walk = self.sv["base_low"]
        if self.sv["ref_high"] is None or (prior_high - self.sv["ref_high"]) > _thresh:
            self.sv["ref_high"] = prior_high
            self.sv["base_low"] = bar.low
        else:
            self.sv["base_low"] = min(self.sv["base_low"] or bar.low, bar.low)

        if bar.close <= prior_high:
            return None

        # Structure break detected — walk left for impulse base
        imp_low = floor_for_walk or prior_low
        imp_low = self._backwalk(i, floor_low=imp_low)
        imp_high = bar.high
        imp_size = imp_high - imp_low

        bar_atr  = self._atrs[i - 1] if i > 0 else imp_size
        if self.ATR_IMPULSE_MULT > 0 and bar_atr > 0 and imp_size < self.ATR_IMPULSE_MULT * bar_atr:
            return None

        if self.IMPULSE_SIZE_PCT_MIN > 0:
            if (imp_size / imp_high) < self.IMPULSE_SIZE_PCT_MIN:
                return None

        bar_range = bar.high - bar.low
        if self.BREAKOUT_MIN > 0 and bar_atr > 0:
            if bar_range < self.BREAKOUT_MIN * bar_atr:
                return None

        if self.BREAKOUT_IBS_MIN > 0 and bar_range > 0:
            ibs = (bar.close - bar.low) / bar_range
            if ibs < self.BREAKOUT_IBS_MIN:
                return None

        consec = 1
        for k in range(i - 1, max(i - lookback - 1, 0), -1):
            if self._closes[k] < self._closes[k + 1]:
                consec += 1
            else:
                break
        if self.IMPULSE_MIN_BARS > 1 and consec < self.IMPULSE_MIN_BARS:
            return None

        self.sv["impulse_low"]           = imp_low
        self.sv["impulse_high"]          = imp_high
        self.sv["impulse_size"]          = imp_size
        self.sv["peak_time"]             = bar.time
        self.sv["peak_bar_idx"]          = i
        self.sv["breakout_bar_idx"]      = i
        _br_range = bar.high - bar.low
        self.sv["breakout_ibs"]          = (bar.close - bar.low) / _br_range if _br_range > 0 else None
        self.sv["impulse_atr_ratio"]     = imp_size / bar_atr if bar_atr > 0 else None
        self.sv["breakout_bar_range_atr"]= _br_range / bar_atr if bar_atr > 0 else None
        self.sv["impulse_consec_bars"]   = consec
        self.sv["_impulse_bar_vol"]      = bar.volume
        self.sv["_pullback_vol_sum"]     = 0.0
        self.state = BUILD_PEAK
        return None

    def _handle_build_peak(self, i: int, bar: Bar) -> Optional[Signal]:
        if bar.high > self.sv["impulse_high"]:
            self.sv["impulse_high"] = bar.high
            self.sv["impulse_size"] = bar.high - self.sv["impulse_low"]
            self.sv["peak_time"]    = bar.time
            self.sv["peak_bar_idx"] = i
        elif bar.close < bar.open:
            if bar.time >= PEAK_CUTOFF:
                self.state = WAIT_BREAK
                self._reset_sv()
                return None
            if self.EMA_SLOPE_BARS > 0 and self.sv["peak_bar_idx"] >= self.EMA_SLOPE_BARS:
                ema_now  = self._emas[self.sv["peak_bar_idx"]]
                ema_prev = self._emas[self.sv["peak_bar_idx"] - self.EMA_SLOPE_BARS]
                if ema_now >= ema_prev:
                    self.state = WAIT_BREAK
                    self._reset_sv()
                    return None
            self.sv["pullback_low"] = bar.low
            self.state = TRACK_PULLBACK
        return None

    def _handle_track_pullback(self, i: int, bar: Bar) -> Optional[Signal]:
        if bar.high > self.sv["impulse_high"]:
            self.sv["impulse_high"] = bar.high
            self.sv["impulse_size"] = bar.high - self.sv["impulse_low"]
            self.sv["peak_time"]    = bar.time
            self.sv["peak_bar_idx"] = i
            self.sv["pullback_low"] = None
            self.sv["_pullback_vol_sum"] = 0.0
            self.state = BUILD_PEAK
            return None

        self.sv["pullback_low"] = min(self.sv["pullback_low"] or bar.low, bar.low)
        self.sv["_pullback_vol_sum"] += bar.volume
        retrace = self.sv["impulse_high"] - self.sv["pullback_low"]
        ratio   = retrace / self.sv["impulse_size"] if self.sv["impulse_size"] > 0 else 0

        if ratio > self.DEEP_RETRACE_MAX:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        if ratio >= self.DEEP_RETRACE_MIN:
            self.sv["deep_retrace_ratio"] = ratio
            self.sv["bars_in_pullback"]   = i - self.sv["peak_bar_idx"]
            imp_vol = self.sv["_impulse_bar_vol"]
            pb_vol  = self.sv["_pullback_vol_sum"]
            self.sv["pullback_vol_ratio"] = (pb_vol / imp_vol) if imp_vol and imp_vol > 0 else None
            if self.MAX_PULLBACK_VOL > 0 and self.sv["pullback_vol_ratio"] is not None:
                if self.sv["pullback_vol_ratio"] >= self.MAX_PULLBACK_VOL:
                    self.state = WAIT_BREAK
                    self._reset_sv()
                    return None
            self.state = WAIT_RETEST
        return None

    def _handle_wait_retest(self, i: int, bar: Bar) -> Optional[Signal]:
        if self.RETEST_MAX_BARS > 0 and (i - self.sv["peak_bar_idx"]) > self.RETEST_MAX_BARS:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        if bar.close > self.sv["impulse_high"] + self.STOP_BUFFER_MULT * self.sv["impulse_size"]:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        if bar.low < self.sv["pullback_low"]:
            self.sv["pullback_low"] = bar.low

        imp_high = self.sv["impulse_high"]
        retest_threshold = imp_high * self.RETEST_PCT
        if bar.high < imp_high - retest_threshold:
            return None

        # Reject punch-through
        if bar.high > imp_high:
            return None

        self.sv["retest_bar_idx"]      = i
        self.sv["retest_wick_extension"] = max(bar.high - imp_high, 0.0) / imp_high
        self.state = WAIT_FAILURE
        return None

    def _handle_wait_failure(self, i: int, bar: Bar) -> Optional[Signal]:
        if bar.close > self.sv["impulse_high"] + self.STOP_BUFFER_MULT * self.sv["impulse_size"]:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        if self.RETEST_MAX_BARS > 0 and (i - self.sv["peak_bar_idx"]) > self.RETEST_MAX_BARS:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        if bar.close >= bar.open:
            if i - self.sv["retest_bar_idx"] > 2:
                self.state = WAIT_BREAK
                self._reset_sv()
            return None

        # Failure candle confirmed
        _fc_range = bar.high - bar.low
        body_pct  = (bar.open - bar.close) / _fc_range if _fc_range > 0 else None

        if self.MIN_FAILURE_BODY_PCT > 0 and body_pct is not None:
            if body_pct < self.MIN_FAILURE_BODY_PCT:
                self.state = WAIT_BREAK
                self._reset_sv()
                return None

        stop_price = self.sv["impulse_high"] + self.STOP_BUFFER_MULT * self.sv["impulse_size"]
        entry_vwap = self._vwaps[i]

        if self.TP_MODE == "vwap":
            tp_raw = None  # resolved at fill
        elif self.TP_MODE == "midpoint":
            tp_raw = self.sv["impulse_high"] - 0.5 * self.sv["impulse_size"]
        else:
            tp_raw = self.sv["impulse_high"] - self.TP_FIXED_MULT * self.sv["impulse_size"]

        self.sv["failure_candle_body_pct"] = body_pct
        self.sv["failure_candle_close_vs_vwap"] = (
            (bar.close - entry_vwap) / entry_vwap if entry_vwap > 0 else None
        )
        self.sv["stop"] = stop_price
        self.sv["tp"]   = tp_raw
        self.state = WAIT_FILL
        return None

    def _handle_wait_fill(self, i: int, bar: Bar) -> Optional[Signal]:
        if bar.time == "15:59":
            self.state = WAIT_BREAK
            self._reset_sv()
            return None
        if bar.time < self.ENTRY_TIME_START or bar.time > self.ENTRY_TIME_END:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        slip_entry = bar.open * self.SLIPPAGE_PCT
        entry_fill = bar.open - slip_entry   # short: worsened downward

        stop_raw   = self.sv["stop"]
        slip_stop  = stop_raw * self.SLIPPAGE_PCT
        stop_fill  = stop_raw + slip_stop    # stop exit: worsened upward

        if self.TP_MODE == "vwap":
            tp_raw = self._vwaps[i]
        else:
            tp_raw = self.sv["tp"]

        if tp_raw is None:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        slip_tp  = tp_raw * self.SLIPPAGE_PCT
        tp_fill  = tp_raw + slip_tp          # TP exit: worsened upward

        # Validity checks
        if entry_fill <= tp_fill:    return None
        if entry_fill >= stop_fill:  return None

        R_at_entry = stop_fill - entry_fill
        if R_at_entry < entry_fill * self.MIN_R_PCT:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        if tp_fill >= entry_fill:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        # Same-bar exits (gap market): skip — orchestrator cannot fill+exit same bar on live
        if bar.high >= stop_fill or bar.low <= tp_fill:
            self.state = WAIT_BREAK
            self._reset_sv()
            return None

        self.state = DONE
        return Signal(
            strategy_id  = self.strategy_id,
            symbol       = self.symbol,
            direction    = "short",
            entry_price  = round(entry_fill, 4),
            stop         = round(stop_fill, 4),
            tp           = round(tp_fill, 4),
            R            = round(R_at_entry, 6),
            bar_time     = bar.time,
            session_date = bar.date,
            meta={
                "impulse_high":          self.sv["impulse_high"],
                "impulse_low":           self.sv["impulse_low"],
                "deep_retrace_ratio":    self.sv["deep_retrace_ratio"],
                "failure_candle_body_pct": self.sv["failure_candle_body_pct"],
                "pullback_vol_ratio":    self.sv["pullback_vol_ratio"],
            }
        )

    def _backwalk(self, i: int, floor_low: float,
                  max_back: int = MAX_IMPULSE_BACK,
                  stop_no_new_low: int = NO_NEW_LOW_STOP) -> float:
        j = i - 1
        if j < 0:
            return floor_low
        imp_low   = floor_low
        no_new_low = 0
        steps      = 0
        while j - 1 >= 0 and steps < max_back and no_new_low < stop_no_new_low:
            j     -= 1
            steps += 1
            low_j  = self._lows[j]
            if low_j < imp_low:
                imp_low    = low_j
                no_new_low = 0
            else:
                no_new_low += 1
        return imp_low

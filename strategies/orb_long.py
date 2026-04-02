"""
strategies/orb_long.py — Opening Range Volume-Delta Fade (LONG side).
Extracted from orb_long.py. Mirror of orb_short with direction flipped.
"""

import datetime
from typing import Optional
from .base import BaseStrategy
from ..models import Signal, Bar, SessionContext

OBSERVING    = "OBSERVING"
WAIT_TRIGGER = "WAIT_TRIGGER"
DONE         = "DONE"


class ORBLong(BaseStrategy):

    def __init__(self, symbol: str, params: dict):
        super().__init__("orb_long", symbol, params)
        p = params
        self.OBSERVE_BARS       = p["observe_bars"]
        self.VOL_DELTA_MIN      = p["vol_delta_min"]
        self.VOL_DELTA_MAX      = p.get("vol_delta_max", 5.0)
        self.VOL_DOM_MIN_PCT    = p.get("vol_dom_min_pct", 3.5) / 100.0
        self.VWAP_ENTRY_PCT     = p["vwap_entry_pct"] / 100.0
        self.VWAP_DRIFT_MIN_PCT = p["vwap_drift_min_pct"] / 100.0
        self.VWAP_DRIFT_MAX_PCT = p["vwap_drift_max_pct"] / 100.0
        self.OBS_RANGE_MIN_PCT  = p["obs_range_min_pct"] / 100.0
        self.RETEST_TIMEOUT     = p["retest_timeout"]
        self.TP_MODE            = p["tp_mode"]
        self.TP_MULT            = p["tp_mult"]
        self.SL_BUFFER_PCT      = p["sl_buffer_pct"] / 100.0
        self.MIN_R_PCT          = p["min_r_pct"] / 100.0
        self.SLIPPAGE_PCT       = p["slippage_pct"] / 100.0
        self.ENTRY_GAP_MAX_PCT  = p["entry_gap_max_pct"] / 100.0
        self.ENTRY_TIME_MIN     = datetime.time.fromisoformat(p["entry_time_min"])
        self.ENTRY_TIME_MAX     = datetime.time.fromisoformat(p["entry_time_max"])
        self.SKIP_MONDAY        = p.get("skip_monday", False)
        self.SKIP_FRIDAY        = p.get("skip_friday", False)
        self.SKIP_MONTHS        = p.get("skip_months", set())
        self.VOL_REGIME_MIN     = p.get("vol_regime_min", 0)
        self.VOL_REGIME_MAX     = p.get("vol_regime_max", 1.6)
        self._reset_session_state()

    def _reset_session_state(self):
        self.state             = OBSERVING
        self.obs_bar_count     = 0
        self.obs_up_vol        = 0.0
        self.obs_down_vol      = 0.0
        self.signal_dir        = None
        self.vwap_at_signal    = None
        self.obs_tp_level      = None   # obs_high for longs
        self.vol_delta_ratio   = None
        self.retest_bars_count = 0
        self.pending_entry     = None
        self._skipped          = False
        self._obs_highs        = []
        self._obs_lows         = []

    def _session_allowed(self, ctx: SessionContext) -> bool:
        d = datetime.date.fromisoformat(ctx.session_date)
        if self.SKIP_MONDAY and d.weekday() == 0:   return False
        if self.SKIP_FRIDAY and d.weekday() == 4:   return False
        if self.SKIP_MONTHS and d.month in self.SKIP_MONTHS: return False
        if self.VOL_REGIME_MIN > 0 or self.VOL_REGIME_MAX > 0:
            r = ctx.vol_regime_ratio
            if r is None: return False
            if self.VOL_REGIME_MIN > 0 and r < self.VOL_REGIME_MIN: return False
            if self.VOL_REGIME_MAX > 0 and r > self.VOL_REGIME_MAX: return False
        return True

    def reset_session(self, ctx: SessionContext) -> None:
        self._reset_session_state()
        if not self._session_allowed(ctx):
            self._skipped = True
            self.state = DONE

    def on_bar(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        if self._skipped or self.state == DONE or self._in_trade:
            return None

        bar_time = datetime.time.fromisoformat(bar.time)

        # Pending fill from previous touch bar
        if self.pending_entry is not None and self.state == WAIT_TRIGGER:
            result = self._attempt_fill(bar, ctx)
            self.pending_entry = None
            if result is not None:
                self.state = DONE
            return result

        if self.state == OBSERVING:
            return self._handle_observing(bar, ctx)
        elif self.state == WAIT_TRIGGER:
            return self._handle_wait_trigger(bar, ctx, bar_time)
        return None

    def _handle_observing(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        if bar.close >= bar.open:
            self.obs_up_vol   += bar.volume
        else:
            self.obs_down_vol += bar.volume
        self.obs_bar_count += 1
        self._obs_highs.append(bar.high)
        self._obs_lows.append(bar.low)

        if self.obs_bar_count < self.OBSERVE_BARS:
            return None

        obs_high = max(self._obs_highs)
        _med_vol = ctx.vol_median
        _med_ok  = (self.VOL_DOM_MIN_PCT > 0 and _med_vol is not None
                    and not (_med_vol != _med_vol))
        _ratio_long = self.obs_up_vol / max(self.obs_down_vol, 1e-9)

        long_ok = (
            self.obs_up_vol > 0
            and _ratio_long >= self.VOL_DELTA_MIN
            and (self.VOL_DELTA_MAX <= 0 or _ratio_long <= self.VOL_DELTA_MAX)
            and bar.close > ctx.vwap
            and (not _med_ok or (self.obs_up_vol / _med_vol) >= self.VOL_DOM_MIN_PCT)
        )

        if long_ok and self.OBS_RANGE_MIN_PCT > 0:
            if ((obs_high - ctx.vwap) / ctx.vwap) < self.OBS_RANGE_MIN_PCT:
                long_ok = False

        if not long_ok:
            self.state = DONE
            return None

        self.signal_dir      = 1
        self.obs_tp_level    = obs_high
        self.vwap_at_signal  = ctx.vwap
        self.vol_delta_ratio = round(_ratio_long, 4)
        self.retest_bars_count = 0
        self.state           = WAIT_TRIGGER
        return None

    def _handle_wait_trigger(self, bar: Bar, ctx: SessionContext,
                              bar_time: datetime.time) -> Optional[Signal]:
        if bar_time < self.ENTRY_TIME_MIN:   return None
        if bar_time > self.ENTRY_TIME_MAX:
            self.state = DONE
            return None

        self.retest_bars_count += 1
        if self.RETEST_TIMEOUT > 0 and self.retest_bars_count > self.RETEST_TIMEOUT:
            self.state = DONE
            return None

        # Long: bar_low reached within VWAP_ENTRY_PCT of VWAP from above
        vwap_zone = ctx.vwap * self.VWAP_ENTRY_PCT
        if not (bar.low <= ctx.vwap + vwap_zone):
            return None

        _drift = abs(self.vwap_at_signal - ctx.vwap) / ctx.vwap
        if self.VWAP_DRIFT_MIN_PCT > 0 and _drift < self.VWAP_DRIFT_MIN_PCT:
            return None
        if self.VWAP_DRIFT_MAX_PCT > 0 and _drift > self.VWAP_DRIFT_MAX_PCT:
            self.state = DONE
            return None

        _buf = ctx.vwap * self.SL_BUFFER_PCT
        if bar.close < ctx.vwap - _buf:
            self.state = DONE
            return None

        self.pending_entry = {
            "vwap_at_signal":  self.vwap_at_signal,
            "vwap_at_trigger": ctx.vwap,
            "obs_tp_level":    self.obs_tp_level,
            "touch_close":     bar.close,
            "trigger_dir":     1,
        }
        return None

    def _attempt_fill(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        pe = self.pending_entry

        if self.ENTRY_GAP_MAX_PCT > 0:
            _gap = abs(bar.open - pe["touch_close"]) / pe["touch_close"]
            if _gap > self.ENTRY_GAP_MAX_PCT:
                self.state = DONE
                return None

        slip       = bar.open * self.SLIPPAGE_PCT
        entry_fill = bar.open - slip                    # long: worsened downward

        vwap_t     = pe["vwap_at_trigger"]
        buf        = vwap_t * self.SL_BUFFER_PCT
        stop_raw   = vwap_t - buf                       # long stop below VWAP
        stop_fill  = stop_raw - vwap_t * self.SLIPPAGE_PCT

        R_at_entry = abs(entry_fill - stop_fill)
        if R_at_entry < entry_fill * self.MIN_R_PCT:
            return None

        if self.TP_MODE == "fixed_r":
            tp_raw = entry_fill + self.TP_MULT * R_at_entry
        else:
            distance = abs(pe["obs_tp_level"] - entry_fill)
            tp_raw   = entry_fill + self.TP_MULT * distance
        tp_fill = tp_raw - (tp_raw * self.SLIPPAGE_PCT)

        if tp_fill <= entry_fill:      return None
        if bar.open < stop_raw:        return None
        if bar.open > tp_raw:          return None

        return Signal(
            strategy_id  = self.strategy_id,
            symbol       = self.symbol,
            direction    = "long",
            entry_price  = round(entry_fill, 4),
            stop         = round(stop_fill, 4),
            tp           = round(tp_fill, 4),
            R            = round(R_at_entry, 6),
            bar_time     = bar.time,
            session_date = bar.date,
            meta={
                "vwap_at_signal":  round(pe["vwap_at_signal"], 4),
                "vwap_at_trigger": round(vwap_t, 4),
                "obs_up_vol":      round(self.obs_up_vol, 2),
                "obs_down_vol":    round(self.obs_down_vol, 2),
                "vol_delta_ratio": self.vol_delta_ratio,
            }
        )

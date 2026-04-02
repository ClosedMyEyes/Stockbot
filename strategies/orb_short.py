"""
strategies/orb_short.py — Opening Range Volume-Delta Fade (SHORT side).

Extracted from orb.py. One instance per symbol.
State machine: OBSERVING → WAIT_TRIGGER → ARMED (pending fill) → done

The orchestrator handles IN_TRADE management via open_positions.
This module only signals entry — it does NOT track exits.
"""

import datetime
from typing import Optional
from .base import BaseStrategy
from ..models import Signal, Bar, SessionContext


# ── State constants ───────────────────────────────────────────────────────────
OBSERVING    = "OBSERVING"
WAIT_TRIGGER = "WAIT_TRIGGER"
ARMED        = "ARMED"        # pending fill queued, waiting for next bar
DONE         = "DONE"


class ORBShort(BaseStrategy):

    def __init__(self, symbol: str, params: dict):
        super().__init__("orb_short", symbol, params)
        # unpack params
        p = params
        self.OBSERVE_BARS       = p["observe_bars"]
        self.VOL_DELTA_MIN      = p["vol_delta_min"]
        self.VOL_DOWN_MIN_PCT   = p["vol_down_min_pct"] / 100.0
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
        self.SKIP_MONDAY        = p.get("skip_monday", True)
        self.SKIP_FRIDAY        = p.get("skip_friday", True)
        self.SKIP_MONTHS        = p.get("skip_months", {8, 9})
        self.VOL_REGIME_MIN     = p.get("vol_regime_min", 0.6)
        self.VOL_REGIME_MAX     = p.get("vol_regime_max", 1.6)

        # session state
        self._reset_session_state()

    def _reset_session_state(self):
        self.state             = OBSERVING
        self.obs_bar_count     = 0
        self.obs_up_vol        = 0.0
        self.obs_down_vol      = 0.0
        self.signal_dir        = None
        self.vwap_at_signal    = None
        self.obs_tp_level      = None   # obs_low
        self.vol_delta_ratio   = None
        self.retest_bars_count = 0
        self.pending_entry     = None   # dict when touch bar fires
        self._skipped          = False  # session-level skip flag
        self._obs_highs        = []
        self._obs_lows         = []

    # ── Session calendar filters ──────────────────────────────────────────────

    def _session_allowed(self, ctx: SessionContext) -> bool:
        d = datetime.date.fromisoformat(ctx.session_date)
        if self.SKIP_MONDAY and d.weekday() == 0:
            return False
        if self.SKIP_FRIDAY and d.weekday() == 4:
            return False
        if self.SKIP_MONTHS and d.month in self.SKIP_MONTHS:
            return False
        if self.VOL_REGIME_MIN > 0 or self.VOL_REGIME_MAX > 0:
            r = ctx.vol_regime_ratio
            if r is None:
                return False
            if self.VOL_REGIME_MIN > 0 and r < self.VOL_REGIME_MIN:
                return False
            if self.VOL_REGIME_MAX > 0 and r > self.VOL_REGIME_MAX:
                return False
        return True

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def reset_session(self, ctx: SessionContext) -> None:
        self._reset_session_state()
        if not self._session_allowed(ctx):
            self._skipped = True
            self.state = DONE

    def on_bar(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        if self._skipped or self.state == DONE or self._in_trade:
            return None

        bar_time = datetime.time.fromisoformat(bar.time)

        # ── PENDING ENTRY FILL ────────────────────────────────────────────────
        if self.pending_entry is not None and self.state == WAIT_TRIGGER:
            # state was set to WAIT_TRIGGER with pending_entry queued on the prev bar
            # Now on the next bar — attempt fill at this bar's open
            result = self._attempt_fill(bar, ctx)
            self.pending_entry = None
            if result is not None:
                self.state = DONE
            else:
                self.state = WAIT_TRIGGER  # keep watching (entry skipped)
            return result

        # ── OBSERVING ─────────────────────────────────────────────────────────
        if self.state == OBSERVING:
            return self._handle_observing(bar, ctx)

        # ── WAIT_TRIGGER ──────────────────────────────────────────────────────
        elif self.state == WAIT_TRIGGER:
            return self._handle_wait_trigger(bar, ctx, bar_time)

        return None

    # ── State handlers ────────────────────────────────────────────────────────

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

        # Observation window complete
        obs_high = max(self._obs_highs)
        obs_low  = min(self._obs_lows)

        # Volume delta check (short: down_vol dominates)
        _med_vol = ctx.vol_median
        _med_ok  = (self.VOL_DOWN_MIN_PCT > 0 and _med_vol is not None
                    and not (_med_vol != _med_vol))  # nan check

        short_ok = (
            self.obs_down_vol > 0
            and (self.obs_down_vol / max(self.obs_up_vol, 1e-9)) >= self.VOL_DELTA_MIN
            and bar.close < ctx.vwap
            and (not _med_ok or (self.obs_down_vol / _med_vol) >= self.VOL_DOWN_MIN_PCT)
        )

        # Minimum range filter
        if short_ok and self.OBS_RANGE_MIN_PCT > 0:
            range_ok = ((ctx.vwap - obs_low) / ctx.vwap) >= self.OBS_RANGE_MIN_PCT
            if not range_ok:
                short_ok = False

        if not short_ok:
            self.state = DONE
            return None

        self.signal_dir      = -1
        self.obs_tp_level    = obs_low
        self.vwap_at_signal  = ctx.vwap
        self.vol_delta_ratio = round(self.obs_down_vol / max(self.obs_up_vol, 1e-9), 4)
        self.retest_bars_count = 0
        self.state           = WAIT_TRIGGER
        return None

    def _handle_wait_trigger(self, bar: Bar, ctx: SessionContext,
                              bar_time: datetime.time) -> Optional[Signal]:
        if bar_time < self.ENTRY_TIME_MIN:
            return None
        if bar_time > self.ENTRY_TIME_MAX:
            self.state = DONE
            return None

        self.retest_bars_count += 1
        if self.RETEST_TIMEOUT > 0 and self.retest_bars_count > self.RETEST_TIMEOUT:
            self.state = DONE
            return None

        # Short: bar_high reached within VWAP_ENTRY_PCT of VWAP from below
        vwap_zone = ctx.vwap * self.VWAP_ENTRY_PCT
        near_vwap = bar.high >= ctx.vwap - vwap_zone
        if not near_vwap:
            return None

        # VWAP drift filter
        _drift = abs(self.vwap_at_signal - ctx.vwap) / ctx.vwap
        if self.VWAP_DRIFT_MIN_PCT > 0 and _drift < self.VWAP_DRIFT_MIN_PCT:
            return None   # keep watching
        if self.VWAP_DRIFT_MAX_PCT > 0 and _drift > self.VWAP_DRIFT_MAX_PCT:
            self.state = DONE
            return None

        # Close-through-stop guard
        _buf = ctx.vwap * self.SL_BUFFER_PCT
        if bar.close > ctx.vwap + _buf:
            self.state = DONE
            return None

        # Touch bar valid — queue pending fill for next bar
        self.pending_entry = {
            "vwap_at_signal":  self.vwap_at_signal,
            "vwap_at_trigger": ctx.vwap,
            "obs_tp_level":    self.obs_tp_level,
            "touch_close":     bar.close,
            "trigger_dir":     -1,
        }
        # state stays WAIT_TRIGGER; next bar open will attempt fill
        return None

    def _attempt_fill(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        """Called on the bar after the touch. Returns Signal or None."""
        pe = self.pending_entry

        # Gap filter: open too far from touch close
        if self.ENTRY_GAP_MAX_PCT > 0:
            _gap = abs(bar.open - pe["touch_close"]) / pe["touch_close"]
            if _gap > self.ENTRY_GAP_MAX_PCT:
                return None

        slip         = bar.open * self.SLIPPAGE_PCT
        entry_fill   = bar.open + pe["trigger_dir"] * slip  # short: worse (higher)

        vwap_t       = pe["vwap_at_trigger"]
        buf          = vwap_t * self.SL_BUFFER_PCT
        stop_raw     = vwap_t + buf                          # short stop above VWAP
        stop_fill    = stop_raw + vwap_t * self.SLIPPAGE_PCT

        R_at_entry = abs(entry_fill - stop_fill)
        if R_at_entry < entry_fill * self.MIN_R_PCT:
            return None

        # TP
        if self.TP_MODE == "fixed_r":
            tp_raw = entry_fill + pe["trigger_dir"] * self.TP_MULT * R_at_entry
        else:
            distance = abs(pe["obs_tp_level"] - entry_fill)
            tp_raw   = entry_fill + pe["trigger_dir"] * self.TP_MULT * distance
        tp_fill = tp_raw - pe["trigger_dir"] * (tp_raw * self.SLIPPAGE_PCT)

        # Sanity checks
        if tp_fill >= entry_fill:      return None   # TP on wrong side
        if bar.open > stop_raw:        return None   # open gapped through stop
        if bar.open < tp_raw:          return None   # open gapped through TP

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
                "vwap_at_signal":  round(pe["vwap_at_signal"], 4),
                "vwap_at_trigger": round(vwap_t, 4),
                "obs_up_vol":      round(self.obs_up_vol, 2),
                "obs_down_vol":    round(self.obs_down_vol, 2),
                "vol_delta_ratio": self.vol_delta_ratio,
            }
        )

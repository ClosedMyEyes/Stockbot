"""
strategies/orb_short.py — Opening Range Volume-Delta Fade (Short).

State machine: OBSERVING → WAIT_TRIGGER → IN_TRADE (pending)
Signal emitted on WAIT_TRIGGER touch; orchestrator manages exits.
"""

import datetime
import numpy as np
from .base import BaseStrategy

# Internal state constants
_OBSERVING     = 0
_WAIT_TRIGGER  = 1
_IN_TRADE      = 2  # pending entry queued; orchestrator takes over after Signal accepted


class OrbShortStrategy(BaseStrategy):

    def reset_session(self, ctx) -> None:
        p = self.params

        # ── Day-of-week / month filters (evaluated at session start) ──────────
        self._session_active = True
        sd = ctx.session_date
        if sd is not None:
            dow = sd.weekday()  # 0=Mon, 4=Fri
            if p.get("skip_monday", True)  and dow == 0: self._session_active = False
            if p.get("skip_friday", True)  and dow == 4: self._session_active = False
            skip_months = p.get("skip_months", {8, 9})
            if sd.month in skip_months: self._session_active = False

        # ── Vol-regime filter ─────────────────────────────────────────────────
        vr_min = p.get("vol_regime_min", 0.6)
        vr_max = p.get("vol_regime_max", 1.6)
        if (vr_min > 0 or vr_max > 0) and hasattr(ctx, "vol_regime_ratio"):
            ratio = ctx.vol_regime_ratio
            if ratio is None or np.isnan(ratio):
                self._session_active = False
            elif vr_min > 0 and ratio < vr_min:
                self._session_active = False
            elif vr_max > 0 and ratio > vr_max:
                self._session_active = False

        # ── State ─────────────────────────────────────────────────────────────
        self._state            = _OBSERVING
        self._obs_bar_count    = 0
        self._obs_up_vol       = 0.0
        self._obs_down_vol     = 0.0
        self._obs_high         = -np.inf
        self._obs_low          =  np.inf
        self._signal_dir       = None
        self._vwap_at_signal   = None
        self._obs_tp_level     = None   # obs_low (short) used as TP anchor
        self._vol_delta_ratio  = None
        self._range_atr_ratio  = None
        self._retest_bars      = 0
        self._pending_entry    = None   # set on touch bar; fill next bar open
        self._in_trade         = False

        # Resolved on pending fill:
        self._vwap_at_trigger  = None
        self._med_vol          = getattr(ctx, "median_session_vol", None)

    def on_bar(self, bar, ctx):
        if not self._session_active or self._in_trade:
            return None

        p             = self.params
        OBSERVE_BARS  = p.get("observe_bars", 15)
        VOL_DELTA_MIN = p.get("vol_delta_min", 2.0)
        VOL_DOWN_MIN  = p.get("vol_down_min_pct", 3.5) / 100.0
        OBS_RANGE_MIN = p.get("obs_range_min_pct", 0.75) / 100.0
        VWAP_ENTRY    = p.get("vwap_entry_pct", 0.20) / 100.0
        DRIFT_MIN     = p.get("vwap_drift_min_pct", 0.3) / 100.0
        DRIFT_MAX     = p.get("vwap_drift_max_pct", 1.5) / 100.0
        SL_BUF        = p.get("sl_buffer_pct", 0.8) / 100.0
        SLIP          = p.get("slippage_pct", 0.08) / 100.0
        MIN_R         = p.get("min_r_pct", 0.20) / 100.0
        ENTRY_GAP_MAX = p.get("entry_gap_max_pct", 0.15) / 100.0
        ENTRY_T_MIN   = datetime.time.fromisoformat(p.get("entry_time_min", "09:55"))
        ENTRY_T_MAX   = datetime.time.fromisoformat(p.get("entry_time_max", "11:00"))
        RETEST_TO     = p.get("retest_timeout", 0)
        TP_MODE       = p.get("tp_mode", "obs_level")
        TP_MULT       = p.get("tp_mult", 2.5)

        bar_time  = datetime.time.fromisoformat(bar.time)
        vwap_now  = ctx.vwap
        bar_high  = bar.high
        bar_low   = bar.low
        bar_close = bar.close
        bar_open  = bar.open
        bar_vol   = bar.volume

        # ── PENDING ENTRY FILL ────────────────────────────────────────────────
        if self._pending_entry is not None:
            pe = self._pending_entry
            self._pending_entry = None

            if ENTRY_GAP_MAX > 0:
                _gap = abs(bar_open - pe["touch_close"]) / pe["touch_close"]
                if _gap > ENTRY_GAP_MAX:
                    self._state = _WAIT_TRIGGER
                    return None

            _entry_slip  = bar_open * SLIP
            entry_fill   = bar_open + pe["trigger_dir"] * _entry_slip   # short: sell lower

            _buf         = pe["vwap_at_trigger"] * SL_BUF
            _slip_s      = pe["vwap_at_trigger"] * SLIP
            if pe["trigger_dir"] == -1:  # short
                stop_raw  = pe["vwap_at_trigger"] + _buf
                stop_fill = stop_raw + _slip_s
            else:
                stop_raw  = pe["vwap_at_trigger"] - _buf
                stop_fill = stop_raw - _slip_s

            R_at_entry = abs(entry_fill - stop_fill)
            if R_at_entry < entry_fill * MIN_R:
                self._state = _WAIT_TRIGGER
                return None

            # TP
            if TP_MODE == "obs_level":
                distance = abs(pe["obs_tp_level"] - entry_fill)
                tp_raw   = entry_fill + pe["trigger_dir"] * TP_MULT * distance
            else:  # fixed_r
                tp_raw   = entry_fill + pe["trigger_dir"] * TP_MULT * R_at_entry
            _tp_slip = tp_raw * SLIP
            tp_fill  = tp_raw - pe["trigger_dir"] * _tp_slip

            # Sanity checks
            tp_wrong = (pe["trigger_dir"] == -1 and tp_fill >= entry_fill) or \
                       (pe["trigger_dir"] ==  1 and tp_fill <= entry_fill)
            gap_stop = (pe["trigger_dir"] == -1 and bar_open > stop_raw) or \
                       (pe["trigger_dir"] ==  1 and bar_open < stop_raw)
            gap_tp   = (pe["trigger_dir"] == -1 and bar_open < tp_raw) or \
                       (pe["trigger_dir"] ==  1 and bar_open > tp_raw)
            if tp_wrong or gap_stop or gap_tp:
                self._state = _WAIT_TRIGGER
                return None

            self._vwap_at_trigger = pe["vwap_at_trigger"]
            direction = "long" if pe["trigger_dir"] == 1 else "short"

            from models import Signal
            return Signal(
                strategy_id   = self.strategy_id,
                symbol        = self.symbol,
                direction     = direction,
                entry_price   = entry_fill,
                stop          = stop_fill,
                tp            = tp_fill,
                R             = R_at_entry,
                session_date  = str(ctx.session_date),
                bar_time      = bar.time,
                meta          = {
                    "vwap_at_signal":  self._vwap_at_signal,
                    "vwap_at_trigger": pe["vwap_at_trigger"],
                    "obs_up_vol":      self._obs_up_vol,
                    "obs_down_vol":    self._obs_down_vol,
                    "vol_delta_ratio": self._vol_delta_ratio,
                    "range_atr_ratio": self._range_atr_ratio,
                    "is_ambiguous":    False,
                },
            )

        # ── STATE: OBSERVING ──────────────────────────────────────────────────
        if self._state == _OBSERVING:
            if bar_close >= bar_open:
                self._obs_up_vol   += bar_vol
            else:
                self._obs_down_vol += bar_vol
            self._obs_high = max(self._obs_high, bar_high)
            self._obs_low  = min(self._obs_low,  bar_low)
            self._obs_bar_count += 1

            if self._obs_bar_count < OBSERVE_BARS:
                return None

            # Observation window complete — evaluate signal
            obs_range = self._obs_high - self._obs_low
            d_atr     = getattr(ctx, "daily_atr", None)
            self._range_atr_ratio = round(obs_range / d_atr, 4) if d_atr else None

            med_valid  = VOL_DOWN_MIN > 0 and self._med_vol and not np.isnan(self._med_vol)
            short_ok   = (
                self._obs_down_vol > 0 and
                (self._obs_down_vol / max(self._obs_up_vol, 1e-9)) >= VOL_DELTA_MIN and
                bar_close < vwap_now and
                (not med_valid or (self._obs_down_vol / self._med_vol) >= VOL_DOWN_MIN)
            )

            if OBS_RANGE_MIN > 0 and short_ok:
                if (vwap_now - self._obs_low) / vwap_now < OBS_RANGE_MIN:
                    short_ok = False

            if not short_ok:
                self._session_active = False  # no signal possible today
                return None

            self._signal_dir      = -1  # short only per config
            self._obs_tp_level    = self._obs_low
            self._vwap_at_signal  = vwap_now
            self._vol_delta_ratio = round(
                self._obs_down_vol / max(self._obs_up_vol, 1e-9), 4
            )
            self._retest_bars = 0
            self._state       = _WAIT_TRIGGER
            return None

        # ── STATE: WAIT_TRIGGER ───────────────────────────────────────────────
        elif self._state == _WAIT_TRIGGER:

            if bar_time < ENTRY_T_MIN:
                return None
            if bar_time > ENTRY_T_MAX:
                self._session_active = False
                return None

            self._retest_bars += 1
            if RETEST_TO > 0 and self._retest_bars > RETEST_TO:
                self._session_active = False
                return None

            # Check VWAP touch
            vwap_zone = vwap_now * VWAP_ENTRY
            near_vwap = bar_high >= vwap_now - vwap_zone  # short: high reaches VWAP from below

            if not near_vwap:
                return None

            # VWAP drift filter
            drift = abs(self._vwap_at_signal - vwap_now) / vwap_now
            if DRIFT_MIN > 0 and drift < DRIFT_MIN:
                return None
            if DRIFT_MAX > 0 and drift > DRIFT_MAX:
                self._session_active = False
                return None

            # Reject if close already through stop boundary
            _buf_check = vwap_now * SL_BUF
            if bar_close > vwap_now + _buf_check:
                self._session_active = False
                return None

            # Queue fill at next bar open
            self._pending_entry = {
                "trigger_dir":     self._signal_dir,
                "vwap_at_signal":  self._vwap_at_signal,
                "vwap_at_trigger": vwap_now,
                "obs_tp_level":    self._obs_tp_level,
                "touch_close":     bar_close,
            }
            return None  # Signal emitted on next bar in pending fill block

        return None

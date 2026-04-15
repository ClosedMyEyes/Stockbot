"""
strategies/_gap_fill_base.py — Shared WAIT_ENTRY → IN_TRADE logic for all gap fill variants.

All four gap fill strategies (large, small, small_multi, big) share the same core
state machine. The differences are in parameters and multi-trade behaviour.
Subclasses override _get_effective_stop_type() and on_exit() as needed.
"""

import datetime
import numpy as np
from .base import BaseStrategy

_WAIT_ENTRY = 0
_IN_TRADE   = 1


class GapFillBaseStrategy(BaseStrategy):
    """
    Core gap fill logic. Parameters read from self.params dict.
    Subclasses may override:
      - _max_trades_per_session() → int (0=unlimited, 1=one-and-done)
      - _effective_stop_type(session_trade_count) → "session_extreme" | "gap_open_buffer"
      - on_exit() to re-arm for a second trade
    """

    # ── Subclass hooks ────────────────────────────────────────────────────────

    def _max_trades_per_session(self) -> int:
        return self.params.get("max_trades_per_session", 1)

    def _effective_stop_type(self, session_trade_count: int) -> str:
        return self.params.get("stop_type", "session_extreme")

    # ── Session reset ─────────────────────────────────────────────────────────

    def reset_session(self, ctx) -> None:
        p = self.params

        # ── Day / month filters ───────────────────────────────────────────────
        self._session_active = True
        sd = ctx.session_date
        if sd is not None:
            dow = sd.weekday()
            if p.get("skip_monday", False) and dow == 0: self._session_active = False
            if p.get("skip_friday", False) and dow == 4: self._session_active = False
            skip_months = p.get("skip_months", set())
            if sd.month in skip_months: self._session_active = False

        # ── Vol regime filter ─────────────────────────────────────────────────
        vr_min = p.get("vol_regime_min", 0.0)
        vr_max = p.get("vol_regime_max", 0.0)
        if (vr_min > 0 or vr_max > 0) and hasattr(ctx, "vol_regime_ratio"):
            ratio = ctx.vol_regime_ratio
            if ratio is None or (isinstance(ratio, float) and np.isnan(ratio)):
                self._session_active = False
            elif vr_min > 0 and ratio < vr_min:
                self._session_active = False
            elif vr_max > 0 and ratio > vr_max:
                self._session_active = False

        # ── Gap qualification state ───────────────────────────────────────────
        # Gap is computed on the FIRST bar of the session (today_open unknown until then)
        self._gap_computed    = False
        self._gap_qualified   = False
        self._gap_dir         = None   # +1=gap down (long), -1=gap up (short)
        self._gap_size        = None
        self._today_open      = None
        self._prior_close     = getattr(ctx, "prior_close", None)
        self._gap_pct         = None
        self._gap_atr_ratio   = None
        self._fb_vol_ratio    = getattr(ctx, "first_bar_vol_ratio", None)
        self._tp_raw          = None
        self._dir_label       = None

        # ── Intra-session trade tracking ──────────────────────────────────────
        self._state               = _WAIT_ENTRY
        self._session_high        = None
        self._session_low         = None
        self._bars_scanned        = 0
        self._pending_entry       = None
        self._session_trade_count = 0
        self._cooldown_remaining  = 0

        # ── Position state (reset on each trade) ──────────────────────────────
        self._entry_price    = None
        self._stop_raw       = None
        self._stop_fill      = None
        self._tp_fill        = None
        self._tp_raw_state   = None
        self._R              = None
        self._entry_time_str = None
        self._entry_bar_i    = None
        self._gap_fill_entry = None
        self._extreme_entry  = None
        self._in_trade       = False
        self._bar_i          = 0

    # ── Bar handler ───────────────────────────────────────────────────────────

    def on_bar(self, bar, ctx):
        if not self._session_active or self._in_trade:
            return None

        p = self.params

        SLIP          = p.get("slippage_pct", 0.08) / 100.0
        SL_BUF        = p.get("sl_buffer_pct", 0.10) / 100.0
        MIN_R         = p.get("min_r_pct", 0.10) / 100.0
        ENTRY_GAP_MAX = p.get("entry_gap_max_pct", 0.15)            # already a fraction
        ENTRY_T_MAX   = datetime.time.fromisoformat(p.get("entry_time_max", "11:00"))
        MAX_BARS      = p.get("max_bars_to_entry", 0)
        MIN_FILL      = p.get("min_gap_fill_at_entry", 0.2)
        GAP_VOL_MIN   = p.get("gap_vol_ratio_min", 0.0)
        GAP_VOL_MAX   = p.get("gap_vol_ratio_max", 0.0)   # 0 = off (same convention as scripts)
        GAP_ATR_MIN   = p.get("gap_atr_ratio_min", 0.0)
        GAP_ATR_MAX   = p.get("gap_atr_ratio_max", 0.0)
        DIRECTION     = p.get("direction", "short")

        bar_open  = bar.open
        bar_high  = bar.high
        bar_low   = bar.low
        bar_close = bar.close
        bar_label = bar.time
        bar_time  = datetime.time.fromisoformat(bar_label)

        # Track session extremes from very first bar
        if self._session_high is None:
            self._session_high = bar_open
            self._session_low  = bar_open
        self._session_high = max(self._session_high, bar_high)
        self._session_low  = min(self._session_low,  bar_low)

        # ── First bar: compute gap ────────────────────────────────────────────
        if not self._gap_computed:
            self._gap_computed = True
            self._today_open   = bar_open

            prior_close = self._prior_close
            if prior_close is None or (isinstance(prior_close, float) and np.isnan(prior_close)):
                self._session_active = False
                return None

            raw_gap_pct = (bar_open - prior_close) / prior_close
            abs_gap_pct = abs(raw_gap_pct)
            gap_min     = p.get("gap_min_pct", 1.5) / 100.0
            gap_max     = p.get("gap_max_pct", 5.0) / 100.0

            if abs_gap_pct < gap_min:
                self._session_active = False; return None
            if gap_max > 0 and abs_gap_pct > gap_max:
                self._session_active = False; return None

            gap_dir = 1 if raw_gap_pct < 0 else -1  # +1=gap down→long, -1=gap up→short
            if DIRECTION == "long"  and gap_dir != 1:  self._session_active = False; return None
            if DIRECTION == "short" and gap_dir != -1: self._session_active = False; return None

            # First-bar volume filter
            fb_ratio = self._fb_vol_ratio
            if GAP_VOL_MIN > 0:
                if fb_ratio is None or (isinstance(fb_ratio, float) and np.isnan(fb_ratio)):
                    self._session_active = False; return None
                if fb_ratio < GAP_VOL_MIN:
                    self._session_active = False; return None
            if GAP_VOL_MAX > 0 and fb_ratio is not None and not (isinstance(fb_ratio, float) and np.isnan(fb_ratio)):
                if fb_ratio > GAP_VOL_MAX:
                    self._session_active = False; return None

            # Gap ATR ratio filter
            gap_size  = abs(bar_open - prior_close)
            daily_atr = getattr(ctx, "daily_atr", None)
            gap_atr_r = round(gap_size / daily_atr, 4) if (daily_atr and daily_atr > 0) else None
            if GAP_ATR_MIN > 0:
                if gap_atr_r is None or gap_atr_r < GAP_ATR_MIN:
                    self._session_active = False; return None
            if GAP_ATR_MAX > 0:
                if gap_atr_r is None or gap_atr_r > GAP_ATR_MAX:
                    self._session_active = False; return None

            self._gap_dir       = gap_dir
            self._gap_size      = gap_size
            self._gap_pct       = abs_gap_pct
            self._gap_atr_ratio = gap_atr_r
            self._dir_label     = "long" if gap_dir == 1 else "short"

            fill_target_pct     = p.get("gap_fill_target_pct", 3.0)
            self._tp_raw        = bar_open + gap_dir * fill_target_pct * gap_size
            self._gap_qualified = True

        if not self._gap_qualified:
            return None

        gap_dir    = self._gap_dir
        gap_size   = self._gap_size
        today_open = self._today_open
        tp_raw     = self._tp_raw

        # ── Pending entry fill ────────────────────────────────────────────────
        if self._pending_entry is not None:
            pe            = self._pending_entry
            self._pending_entry = None
            max_t         = self._max_trades_per_session()

            if ENTRY_GAP_MAX > 0:
                _og = abs(bar_open - pe["trigger_close"]) / gap_size if gap_size > 0 else 0.0
                if _og > ENTRY_GAP_MAX:
                    if max_t == 1:
                        self._session_active = False
                    else:
                        self._state = _WAIT_ENTRY
                    return None

            _slip_amt  = bar_open * SLIP
            entry_fill = bar_open + gap_dir * _slip_amt

            _eff_stop  = self._effective_stop_type(self._session_trade_count)
            if _eff_stop == "session_extreme":
                _anchor  = pe["session_low_at_trigger"]  if gap_dir == 1 else pe["session_high_at_trigger"]
                stop_raw = _anchor - _anchor * SL_BUF    if gap_dir == 1 else _anchor + _anchor * SL_BUF
            else:  # gap_open_buffer
                _anchor  = today_open
                stop_raw = today_open - today_open * SL_BUF if gap_dir == 1 else today_open + today_open * SL_BUF

            _stop_slip = stop_raw * SLIP
            stop_fill  = stop_raw - gap_dir * _stop_slip

            R_at_entry = abs(entry_fill - stop_fill)
            if R_at_entry < entry_fill * MIN_R:
                self._session_active = False if max_t == 1 else self._session_active
                if max_t != 1: self._state = _WAIT_ENTRY
                return None

            _tp_slip      = tp_raw * SLIP
            tp_fill_price = tp_raw - gap_dir * _tp_slip

            if (gap_dir == 1 and tp_raw <= entry_fill) or (gap_dir == -1 and tp_raw >= entry_fill):
                self._session_active = False; return None

            _gapped_stop = (gap_dir == 1 and bar_open < stop_raw) or (gap_dir == -1 and bar_open > stop_raw)
            _gapped_tp   = (gap_dir == 1 and bar_open > tp_raw)  or (gap_dir == -1 and bar_open < tp_raw)
            if _gapped_stop or _gapped_tp:
                self._session_active = False; return None

            # Min gap fill at entry
            _eff_min_fill = (
                p.get("reentry_min_gap_fill", MIN_FILL)
                if self._session_trade_count > 0 else MIN_FILL
            )
            gap_fill_e = ((entry_fill - today_open) * gap_dir / gap_size) if gap_size > 0 else None
            if _eff_min_fill is not None and gap_fill_e is not None:
                if gap_fill_e < _eff_min_fill:
                    if max_t == 1:
                        self._session_active = False
                    else:
                        self._state = _WAIT_ENTRY
                        self._pending_entry = None
                    return None

            self._stop_raw       = stop_raw
            self._stop_fill      = stop_fill
            self._tp_fill        = tp_fill_price
            self._tp_raw_state   = tp_raw
            self._R              = R_at_entry
            self._entry_time_str = bar_label
            self._entry_bar_i    = self._bar_i
            self._extreme_entry  = _anchor
            self._gap_fill_entry = gap_fill_e
            self._state          = _IN_TRADE

            from models import Signal
            return Signal(
                strategy_id  = self.strategy_id,
                symbol       = self.symbol,
                direction    = self._dir_label,
                entry_price  = entry_fill,
                stop         = stop_fill,
                tp           = tp_fill_price,
                R            = R_at_entry,
                session_date = str(ctx.session_date),
                bar_time     = bar_label,
                meta = {
                    "gap_pct":                   round(self._gap_pct * 100, 4),
                    "prior_close":               round(self._prior_close, 4),
                    "today_open":                round(today_open, 4),
                    "gap_dir":                   gap_dir,
                    "gap_fill_pct_at_entry":     round(gap_fill_e, 4) if gap_fill_e is not None else None,
                    "session_extreme_at_entry":  round(_anchor, 4),
                    "bars_to_entry":             self._bars_scanned,
                    "first_bar_vol_ratio":       round(self._fb_vol_ratio, 4) if self._fb_vol_ratio else None,
                    "gap_atr_ratio":             self._gap_atr_ratio,
                    "trade_num_in_session":      self._session_trade_count + 1,
                    "stop_type":                 _eff_stop,
                },
            )

        # ── WAIT_ENTRY ────────────────────────────────────────────────────────
        if self._state == _WAIT_ENTRY:
            if bar_time > ENTRY_T_MAX:
                self._session_active = False; return None

            if self._cooldown_remaining > 0:
                self._cooldown_remaining -= 1
                self._bar_i += 1
                return None

            self._bars_scanned += 1
            if MAX_BARS > 0 and self._bars_scanned > MAX_BARS:
                self._session_active = False; return None

            is_reversal = (gap_dir == -1 and bar_close < bar_open) or \
                          (gap_dir ==  1 and bar_close > bar_open)
            if not is_reversal:
                self._bar_i += 1
                return None

            self._pending_entry = {
                "trigger_close":           bar_close,
                "session_high_at_trigger": self._session_high,
                "session_low_at_trigger":  self._session_low,
            }
            self._bar_i += 1
            return None

        self._bar_i += 1
        return None

    def on_exit(self, result_r: float, reason: str) -> None:
        """One-trade-per-session default: stay dormant."""
        self._in_trade     = False
        self._session_active = False

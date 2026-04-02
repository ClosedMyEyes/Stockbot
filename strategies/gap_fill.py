"""
strategies/gap_fill.py — Gap Fill (all three variants).

One class, three strategy IDs:
  "gap_fill_large"  — gap_fill.py defaults (both sides, 2.5–5%)
  "gap_fill_small"  — smallshort_fill.py defaults (short, 1.5–5%, small ATR ratio)
  "gap_fill_big"    — bigshort_fill.py defaults (short, 1.5–5%, large ATR ratio)

State machine: WAIT_ENTRY → ARMED (pending fill) → DONE
"""

import datetime
from typing import Optional
from .base import BaseStrategy
from ..models import Signal, Bar, SessionContext

WAIT_ENTRY = "WAIT_ENTRY"
ARMED      = "ARMED"
DONE       = "DONE"


class GapFill(BaseStrategy):

    def __init__(self, strategy_id: str, symbol: str, params: dict):
        super().__init__(strategy_id, symbol, params)
        p = params
        self.GAP_MIN_PCT          = p["gap_min_pct"] / 100.0
        self.GAP_MAX_PCT          = p["gap_max_pct"] / 100.0
        self.DIRECTION            = p["direction"]               # "long" | "short" | "both"
        self.FILL_TARGET_PCT      = p["gap_fill_target_pct"]    # kept as multiplier
        self.STOP_TYPE            = p["stop_type"]
        self.SL_BUFFER_PCT        = p["sl_buffer_pct"] / 100.0
        self.ENTRY_TIME_MAX       = datetime.time.fromisoformat(p["entry_time_max"])
        self.ENTRY_GAP_MAX_PCT    = p["entry_gap_max_pct"]      # fraction of gap_size
        self.MAX_BARS_TO_ENTRY    = p["max_bars_to_entry"]
        self.MIN_GAP_FILL_ENTRY   = p["min_gap_fill_at_entry"]
        self.MIN_R_PCT            = p["min_r_pct"] / 100.0
        self.SLIPPAGE_PCT         = p["slippage_pct"] / 100.0
        self.HOLD_CAP_BARS        = p["hold_cap_bars"]
        self.HOLD_CAP_EXIT_R      = p["hold_cap_exit_r"]
        self.SKIP_MONDAY          = p.get("skip_monday", False)
        self.SKIP_FRIDAY          = p.get("skip_friday", False)
        self.SKIP_MONTHS          = p.get("skip_months", set())
        self.VOL_REGIME_MIN       = p.get("vol_regime_min", 0)
        self.VOL_REGIME_MAX       = p.get("vol_regime_max", 3.0)
        self.GAP_VOL_RATIO_MIN    = p.get("gap_vol_ratio_min", 0)
        self.GAP_ATR_RATIO_MIN    = p.get("gap_atr_ratio_min", 0)
        self.GAP_ATR_RATIO_MAX    = p.get("gap_atr_ratio_max", 0)

        # session-level (set in reset_session)
        self._gap_dir        = None    # +1 = gap down (go long), -1 = gap up (go short)
        self._gap_size       = None
        self._gap_pct        = None
        self._today_open     = None
        self._prior_close    = None
        self._tp_raw         = None
        self._dir_label      = None
        self._gap_atr_ratio  = None
        self._fb_vol_ratio   = None
        self._skipped        = False

        self._session_high   = 0.0
        self._session_low    = 9e9
        self._bars_scanned   = 0
        self._pending_entry  = None

    def reset_session(self, ctx: SessionContext) -> None:
        self.state          = WAIT_ENTRY
        self._skipped       = False
        self._bars_scanned  = 0
        self._pending_entry = None
        self._session_high  = ctx.today_open or 0.0
        self._session_low   = ctx.today_open or 9e9

        # Calendar / vol regime filters
        if not self._session_allowed(ctx):
            self._skipped = True
            self.state = DONE
            return

        # Gap qualification
        pc = ctx.prior_close
        op = ctx.today_open
        if pc is None or op is None:
            self._skipped = True
            self.state = DONE
            return

        raw_gap = (op - pc) / pc
        abs_gap = abs(raw_gap)

        if abs_gap < self.GAP_MIN_PCT:
            self._skipped = True
            self.state = DONE
            return
        if self.GAP_MAX_PCT > 0 and abs_gap > self.GAP_MAX_PCT:
            self._skipped = True
            self.state = DONE
            return

        gap_dir = 1 if raw_gap < 0 else -1   # +1 = gap down → long; -1 = gap up → short

        if self.DIRECTION == "long"  and gap_dir != 1:
            self._skipped = True; self.state = DONE; return
        if self.DIRECTION == "short" and gap_dir != -1:
            self._skipped = True; self.state = DONE; return

        # Vol ratio filter
        if self.GAP_VOL_RATIO_MIN > 0:
            r = ctx.first_bar_vol_ratio
            if r is None or r < self.GAP_VOL_RATIO_MIN:
                self._skipped = True; self.state = DONE; return

        # ATR ratio filter
        gap_size = abs(op - pc)
        atr = ctx.daily_atr
        gap_atr_ratio = round(gap_size / atr, 4) if (atr and atr > 0) else None
        if self.GAP_ATR_RATIO_MIN > 0:
            if gap_atr_ratio is None or gap_atr_ratio < self.GAP_ATR_RATIO_MIN:
                self._skipped = True; self.state = DONE; return
        if self.GAP_ATR_RATIO_MAX > 0:
            if gap_atr_ratio is None or gap_atr_ratio > self.GAP_ATR_RATIO_MAX:
                self._skipped = True; self.state = DONE; return

        self._gap_dir       = gap_dir
        self._gap_size      = gap_size
        self._gap_pct       = abs_gap
        self._today_open    = op
        self._prior_close   = pc
        self._tp_raw        = op + gap_dir * self.FILL_TARGET_PCT * gap_size
        self._dir_label     = "long" if gap_dir == 1 else "short"
        self._gap_atr_ratio = gap_atr_ratio
        self._fb_vol_ratio  = ctx.first_bar_vol_ratio

    def _session_allowed(self, ctx: SessionContext) -> bool:
        d = datetime.date.fromisoformat(ctx.session_date)
        if self.SKIP_MONDAY and d.weekday() == 0: return False
        if self.SKIP_FRIDAY and d.weekday() == 4: return False
        if self.SKIP_MONTHS and d.month in self.SKIP_MONTHS: return False
        if self.VOL_REGIME_MIN > 0 or self.VOL_REGIME_MAX > 0:
            r = ctx.vol_regime_ratio
            if r is None: return False
            if self.VOL_REGIME_MIN > 0 and r < self.VOL_REGIME_MIN: return False
            if self.VOL_REGIME_MAX > 0 and r > self.VOL_REGIME_MAX: return False
        return True

    def on_bar(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        if self._skipped or self.state == DONE or self._in_trade:
            return None

        # Update session extremes
        self._session_high = max(self._session_high, bar.high)
        self._session_low  = min(self._session_low, bar.low)

        # Pending fill from previous trigger bar
        if self._pending_entry is not None and self.state == WAIT_ENTRY:
            result = self._attempt_fill(bar, ctx)
            self._pending_entry = None
            if result is not None:
                self.state = DONE
            return result

        if self.state == WAIT_ENTRY:
            return self._handle_wait_entry(bar, ctx)

        return None

    def _handle_wait_entry(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        bar_time = datetime.time.fromisoformat(bar.time)
        if bar_time > self.ENTRY_TIME_MAX:
            self.state = DONE
            return None

        self._bars_scanned += 1
        if self.MAX_BARS_TO_ENTRY > 0 and self._bars_scanned > self.MAX_BARS_TO_ENTRY:
            self.state = DONE
            return None

        # First reversal trigger
        is_reversal = (
            (self._gap_dir == -1 and bar.close < bar.open) or  # gap up → short: first red close
            (self._gap_dir ==  1 and bar.close > bar.open)     # gap dn → long:  first green close
        )
        if not is_reversal:
            return None

        self._pending_entry = {
            "trigger_close":           bar.close,
            "session_high_at_trigger": self._session_high,
            "session_low_at_trigger":  self._session_low,
        }
        return None

    def _attempt_fill(self, bar: Bar, ctx: SessionContext) -> Optional[Signal]:
        pe       = self._pending_entry
        gap_dir  = self._gap_dir
        gap_size = self._gap_size
        op       = self._today_open
        tp_raw   = self._tp_raw

        # Entry gap filter
        if self.ENTRY_GAP_MAX_PCT > 0 and gap_size > 0:
            _open_gap = abs(bar.open - pe["trigger_close"]) / gap_size
            if _open_gap > self.ENTRY_GAP_MAX_PCT:
                return None

        slip_amt   = bar.open * self.SLIPPAGE_PCT
        entry_fill = bar.open + gap_dir * slip_amt     # long: pay more; short: receive less

        # Stop calculation
        if self.STOP_TYPE == "session_extreme":
            if gap_dir == 1:
                _anchor  = pe["session_low_at_trigger"]
                stop_raw = _anchor - _anchor * self.SL_BUFFER_PCT
            else:
                _anchor  = pe["session_high_at_trigger"]
                stop_raw = _anchor + _anchor * self.SL_BUFFER_PCT
        else:  # gap_open_buffer
            _anchor  = op
            if gap_dir == 1:
                stop_raw = op - op * self.SL_BUFFER_PCT
            else:
                stop_raw = op + op * self.SL_BUFFER_PCT

        stop_slip = stop_raw * self.SLIPPAGE_PCT
        stop_fill = stop_raw - gap_dir * stop_slip

        R_at_entry = abs(entry_fill - stop_fill)
        if R_at_entry < entry_fill * self.MIN_R_PCT:
            return None

        tp_slip      = tp_raw * self.SLIPPAGE_PCT
        tp_fill_price = tp_raw - gap_dir * tp_slip

        if (gap_dir == 1 and tp_raw <= entry_fill) or (gap_dir == -1 and tp_raw >= entry_fill):
            return None

        _gapped_stop = (gap_dir == 1 and bar.open < stop_raw) or (gap_dir == -1 and bar.open > stop_raw)
        _gapped_tp   = (gap_dir == 1 and bar.open > tp_raw)   or (gap_dir == -1 and bar.open < tp_raw)
        if _gapped_stop or _gapped_tp:
            return None

        gap_fill_entry = ((entry_fill - op) * gap_dir / gap_size) if gap_size > 0 else None
        if self.MIN_GAP_FILL_ENTRY is not None and gap_fill_entry is not None:
            if gap_fill_entry < self.MIN_GAP_FILL_ENTRY:
                return None

        return Signal(
            strategy_id  = self.strategy_id,
            symbol       = self.symbol,
            direction    = self._dir_label,
            entry_price  = round(entry_fill, 4),
            stop         = round(stop_fill, 4),
            tp           = round(tp_fill_price, 4),
            R            = round(R_at_entry, 6),
            bar_time     = bar.time,
            session_date = bar.date,
            meta={
                "gap_pct":              round(self._gap_pct * 100, 4),
                "prior_close":          self._prior_close,
                "today_open":           op,
                "gap_atr_ratio":        self._gap_atr_ratio,
                "first_bar_vol_ratio":  self._fb_vol_ratio,
                "gap_fill_pct_at_entry": gap_fill_entry,
                "session_extreme":      _anchor,
                "bars_to_entry":        self._bars_scanned,
            }
        )

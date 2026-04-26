"""
strategies/impulse_short.py — Exhausted Impulse Retest (Short).

State machine: WAIT_BREAK → BUILD_PEAK → TRACK_PULLBACK → WAIT_RETEST
              → WAIT_FAILURE → WAIT_FILL → (Signal emitted) → orchestrator manages exit.

One trade per session (default). on_exit() keeps _in_trade=False, stays dormant.
"""

import datetime
import numpy as np
from .base import BaseStrategy

_WAIT_BREAK     = 0
_BUILD_PEAK     = 1
_TRACK_PULLBACK = 2
_WAIT_RETEST    = 3
_WAIT_FAILURE   = 4
_WAIT_FILL      = 5

X_FULL           = 20
X_MIN            = 10
MAX_IMPULSE_BACK = 50
NO_NEW_LOW_STOP  = 5
PEAK_CUTOFF      = "15:45"


def _backwalk_impulse_low(lows_arr, i, floor_low=None, max_back=50, stop_no_new_low=5):
    j = i - 1
    if j < 0:
        return None
    imp_low    = floor_low if floor_low is not None else lows_arr[j]
    no_new_low = 0
    steps      = 0
    while j - 1 >= 0 and steps < max_back and no_new_low < stop_no_new_low:
        j     -= 1
        steps += 1
        if lows_arr[j] < imp_low:
            imp_low    = lows_arr[j]
            no_new_low = 0
        else:
            no_new_low += 1
    return imp_low


def _reset_sv():
    return dict(
        impulse_low=None, impulse_high=None, impulse_size=None,
        impulse_atr_ratio=None, impulse_size_pct=None,
        breakout_bar_range_atr=None, impulse_consec_bars=None,
        peak_time=None, peak_bar_idx=None, pullback_low=None,
        deep_retrace_ratio=None, bars_in_pullback=None,
        retest_bar_idx=None, retest_wick_extension=None,
        failure_candle_body_pct=None, failure_candle_close_vs_vwap=None,
        entry_price=None, stop=None, tp=None, R=None,
        entry_time=None, entry_bar_i=None,
        vwap_at_entry=None, entry_vs_vwap_pct=None, session_atr=None,
        impulse_bar_vol_vs_median=None, pullback_vol_ratio=None,
        retest_bar_vol_vs_median=None,
        breakout_bar_idx=None, breakout_ibs=None,
        ref_high=None, base_low=None,
        _impulse_bar_vol=None, _pullback_vol_sum=0.0,
    )


class ImpulseShortStrategy(BaseStrategy):

    def reset_session(self, ctx) -> None:
        self._state    = _WAIT_BREAK
        self._sv       = _reset_sv()
        self._bars     = []          # accumulate (open,high,low,close,vol,time,vwap,atr,vol_med)
        self._bar_idx  = 0
        self._in_trade = False
        self._session_active = True
        self._prev_state_for_timeout = 0

    def on_bar(self, bar, ctx) -> None:
        if not self._session_active or self._in_trade:
            return None

        p = self.params
        ATR_MULT      = p.get("atr_impulse_mult", 0.9)
        SIZE_PCT_MIN  = p.get("impulse_size_pct_min", 0.7) / 100.0
        IMPULSE_MIN_B = p.get("impulse_min_bars", 0)
        DR_MIN        = p.get("deep_retrace_min", 0.60)
        DR_MAX        = p.get("deep_retrace_max", 0.95)
        RETEST_PCT    = p.get("retest_pct", 0.10) / 100.0
        RETEST_MAX_B  = p.get("retest_max_bars", 15)
        STOP_BUF      = p.get("stop_buffer_mult", 0.10)
        TP_MODE       = p.get("tp_mode", "fixed")
        TP_MULT       = p.get("tp_fixed_mult", 1.0)
        MIN_R         = p.get("min_r_pct", 0.08) / 100.0
        SLIP          = p.get("slippage_pct", 0.05) / 100.0
        IBS_MIN       = p.get("breakout_ibs_min", 0.95)
        FAIL_BODY_MIN = p.get("min_failure_body_pct", 85) / 100.0
        PB_VOL_MAX    = p.get("max_pullback_vol_ratio", 1.8)
        BK_MIN        = p.get("breakout_min", 0.75)
        ENTRY_T_START = p.get("entry_time_start", "09:30")
        ENTRY_T_END   = p.get("entry_time_end",   "15:55")
        EMA_SLOPE_B   = p.get("ema_slope_bars", 0)

        i         = self._bar_idx
        bar_high  = bar.high
        bar_low   = bar.low
        bar_close = bar.close
        bar_open  = bar.open
        bar_vol   = bar.volume
        bar_label = bar.time
        vwap_now  = ctx.vwap
        bar_atr   = ctx.atr if hasattr(ctx, "atr") else None
        vol_med   = getattr(ctx, "vol_median_tod", None)

        # Cache for backwalk
        self._bars.append((bar_open, bar_high, bar_low, bar_close, bar_vol, bar_label, vwap_now, bar_atr, vol_med))
        self._bar_idx += 1

        sv = self._sv

        if i < X_MIN:
            return None
        lookback = min(i, X_FULL)
        prior_highs = [self._bars[j][1] for j in range(i - lookback, i)]
        prior_lows  = [self._bars[j][2] for j in range(i - lookback, i)]

        # ── WAIT_BREAK ────────────────────────────────────────────────────────
        if self._state == _WAIT_BREAK:
            rolling_high = max(prior_highs)
            rolling_low  = min(prior_lows)

            _thresh = rolling_high * 0.0001
            floor_for_walk = sv["base_low"]
            if sv["ref_high"] is None or (rolling_high - sv["ref_high"]) > _thresh:
                sv["ref_high"] = rolling_high
                sv["base_low"] = bar_low
            else:
                sv["base_low"] = min(sv["base_low"], bar_low)

            if bar_close <= rolling_high:
                return None

            imp_low = floor_for_walk if floor_for_walk is not None else rolling_low
            lows_arr = [self._bars[j][2] for j in range(i)]
            imp_low = _backwalk_impulse_low(lows_arr, i, floor_low=imp_low,
                                            max_back=MAX_IMPULSE_BACK,
                                            stop_no_new_low=NO_NEW_LOW_STOP) or rolling_low

            imp_high = bar_high
            imp_size = imp_high - imp_low
            _atr = bar_atr or 0.0
            if _atr > 0 and imp_size < ATR_MULT * _atr:
                return None
            if SIZE_PCT_MIN > 0 and imp_high > 0:
                if imp_size / imp_high < SIZE_PCT_MIN:
                    return None
            bar_range = bar_high - bar_low
            if BK_MIN > 0 and _atr > 0 and bar_range < BK_MIN * _atr:
                return None
            if IBS_MIN > 0 and bar_range > 0:
                ibs = (bar_close - bar_low) / bar_range
                if ibs < IBS_MIN:
                    return None

            consec = 1
            for k in range(i - 1, max(i - lookback - 1, 0), -1):
                if self._bars[k][3] < self._bars[k + 1][3]:
                    consec += 1
                else:
                    break
            if IMPULSE_MIN_B > 1 and consec < IMPULSE_MIN_B:
                return None

            sv["impulse_low"]   = imp_low
            sv["impulse_high"]  = imp_high
            sv["impulse_size"]  = imp_size
            sv["peak_time"]     = bar_label
            sv["peak_bar_idx"]  = i
            sv["breakout_bar_idx"] = i
            sv["breakout_ibs"]  = (bar_close - bar_low) / bar_range if bar_range > 0 else None
            sv["impulse_atr_ratio"]      = imp_size / _atr if _atr > 0 else None
            sv["impulse_size_pct"]       = imp_size / imp_high if imp_high > 0 else None
            sv["breakout_bar_range_atr"] = bar_range / _atr if _atr > 0 else None
            sv["impulse_consec_bars"]    = consec
            _med = vol_med
            sv["impulse_bar_vol_vs_median"] = (bar_vol / _med
                                               if _med and not np.isnan(_med) and _med > 0 else None)
            sv["_impulse_bar_vol"]  = bar_vol
            sv["_pullback_vol_sum"] = 0.0
            self._state = _BUILD_PEAK

        # ── BUILD_PEAK ────────────────────────────────────────────────────────
        elif self._state == _BUILD_PEAK:
            if bar_high > sv["impulse_high"]:
                sv["impulse_high"] = bar_high
                sv["impulse_size"] = sv["impulse_high"] - sv["impulse_low"]
                sv["peak_time"]    = bar_label
                sv["peak_bar_idx"] = i
            elif bar_close < bar_open:
                if bar_label >= PEAK_CUTOFF:
                    self._state = _WAIT_BREAK
                    self._sv    = _reset_sv()
                    return None
                sv["pullback_low"] = bar_low
                self._state = _TRACK_PULLBACK

        # ── TRACK_PULLBACK ────────────────────────────────────────────────────
        elif self._state == _TRACK_PULLBACK:
            if bar_high > sv["impulse_high"]:
                sv["impulse_high"]  = bar_high
                sv["impulse_size"]  = sv["impulse_high"] - sv["impulse_low"]
                sv["peak_time"]     = bar_label
                sv["peak_bar_idx"]  = i
                sv["pullback_low"]  = None
                sv["_pullback_vol_sum"] = 0.0
                self._state = _BUILD_PEAK
                return None

            sv["pullback_low"] = min(sv["pullback_low"], bar_low)
            sv["_pullback_vol_sum"] += bar_vol
            retrace = sv["impulse_high"] - sv["pullback_low"]
            ratio   = retrace / sv["impulse_size"] if sv["impulse_size"] > 0 else 0

            if ratio > DR_MAX:
                self._state = _WAIT_BREAK
                self._sv    = _reset_sv()
                return None

            if ratio >= DR_MIN:
                sv["deep_retrace_ratio"] = ratio
                sv["bars_in_pullback"]   = i - sv["peak_bar_idx"]
                sv["pullback_vol_ratio"] = (sv["_pullback_vol_sum"] / sv["_impulse_bar_vol"]
                                            if sv["_impulse_bar_vol"] and sv["_impulse_bar_vol"] > 0 else None)
                if PB_VOL_MAX > 0 and sv["pullback_vol_ratio"] is not None:
                    if sv["pullback_vol_ratio"] >= PB_VOL_MAX:
                        self._state = _WAIT_BREAK
                        self._sv    = _reset_sv()
                        return None
                self._state = _WAIT_RETEST

        # ── WAIT_RETEST ───────────────────────────────────────────────────────
        elif self._state == _WAIT_RETEST:
            if RETEST_MAX_B > 0 and (i - sv["peak_bar_idx"]) > RETEST_MAX_B:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None
            if bar_close > sv["impulse_high"] + STOP_BUF * sv["impulse_size"]:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None
            if bar_low < sv["pullback_low"]:
                sv["pullback_low"] = bar_low

            thresh = sv["impulse_high"] * RETEST_PCT
            if bar_high >= sv["impulse_high"] - thresh:
                if bar_high > sv["impulse_high"]:
                    return None  # wick punch-through — skip, keep watching
                sv["retest_bar_idx"] = i
                sv["retest_wick_extension"] = max(bar_high - sv["impulse_high"], 0.0) / sv["impulse_high"]
                _med = vol_med
                sv["retest_bar_vol_vs_median"] = (bar_vol / _med
                                                  if _med and not np.isnan(_med) and _med > 0 else None)
                self._state = _WAIT_FAILURE

        # ── WAIT_FAILURE ──────────────────────────────────────────────────────
        elif self._state == _WAIT_FAILURE:
            if bar_close > sv["impulse_high"] + STOP_BUF * sv["impulse_size"]:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None
            if RETEST_MAX_B > 0 and (i - sv["peak_bar_idx"]) > RETEST_MAX_B:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None

            if bar_close < bar_open:
                stop_price = sv["impulse_high"] + STOP_BUF * sv["impulse_size"]
                _fc_range  = bar_high - bar_low
                sv["failure_candle_body_pct"] = (
                    (bar_open - bar_close) / _fc_range if _fc_range > 0 else None
                )
                if FAIL_BODY_MIN > 0 and sv["failure_candle_body_pct"] is not None:
                    if sv["failure_candle_body_pct"] < FAIL_BODY_MIN:
                        self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None

                sv["failure_candle_close_vs_vwap"] = (
                    (bar_close - vwap_now) / vwap_now if vwap_now > 0 else None
                )
                sv["stop"] = stop_price
                if TP_MODE != "vwap":
                    sv["tp"] = sv["impulse_high"] - TP_MULT * sv["impulse_size"]
                else:
                    sv["tp"] = None  # resolved on fill bar
                self._state = _WAIT_FILL
            else:
                if sv["retest_bar_idx"] is not None and i - sv["retest_bar_idx"] > 2:
                    self._state = _WAIT_BREAK; self._sv = _reset_sv()

        # ── WAIT_FILL ─────────────────────────────────────────────────────────
        elif self._state == _WAIT_FILL:
            if bar_label == "15:59":
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None
            if bar_label < ENTRY_T_START or bar_label > ENTRY_T_END:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None

            _slip_e  = bar_open * SLIP
            entry_fill = bar_open - _slip_e  # short: sell lower

            stop_raw  = sv["stop"]
            stop_fill = stop_raw + stop_raw * SLIP

            if TP_MODE == "vwap":
                tp_raw = vwap_now
            else:
                tp_raw = sv["tp"]
            tp_fill = tp_raw + tp_raw * SLIP  # short TP: buy back higher

            if entry_fill <= tp_fill:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None
            if entry_fill >= stop_fill:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None

            R_at_entry = stop_fill - entry_fill
            if R_at_entry < entry_fill * MIN_R:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None
            if tp_fill >= entry_fill:
                self._state = _WAIT_BREAK; self._sv = _reset_sv(); return None

            _vwap_fill = vwap_now
            _evwap_pct = (entry_fill - _vwap_fill) / _vwap_fill * 100 if _vwap_fill > 0 else None

            from ..models import Signal
            return Signal(
                strategy_id  = self.strategy_id,
                symbol       = self.symbol,
                direction    = "short",
                entry_price  = entry_fill,
                stop         = stop_fill,
                tp           = tp_fill,
                R            = R_at_entry,
                session_date = str(ctx.session_date),
                bar_time     = bar.time,
                meta = {
                    "impulse_low":                sv["impulse_low"],
                    "impulse_high":               sv["impulse_high"],
                    "impulse_size":               sv["impulse_size"],
                    "peak_time":                  sv["peak_time"],
                    "impulse_atr_ratio":          sv["impulse_atr_ratio"],
                    "impulse_size_pct":           round(sv["impulse_size_pct"] * 100, 4) if sv["impulse_size_pct"] else None,
                    "breakout_bar_range_atr":     sv["breakout_bar_range_atr"],
                    "impulse_consec_bars":        sv["impulse_consec_bars"],
                    "breakout_ibs":               sv["breakout_ibs"],
                    "deep_retrace_ratio":         sv["deep_retrace_ratio"],
                    "bars_in_pullback":           sv["bars_in_pullback"],
                    "retest_wick_extension":      round(sv["retest_wick_extension"] * 100, 4) if sv["retest_wick_extension"] is not None else None,
                    "failure_candle_body_pct":    round(sv["failure_candle_body_pct"] * 100, 4) if sv["failure_candle_body_pct"] is not None else None,
                    "failure_candle_close_vs_vwap": round(sv["failure_candle_close_vs_vwap"] * 100, 4) if sv["failure_candle_close_vs_vwap"] is not None else None,
                    "vwap_at_entry":              round(_vwap_fill, 4),
                    "entry_vs_vwap_pct":          round(_evwap_pct, 4) if _evwap_pct is not None else None,
                    "session_atr":                round(bar_atr, 4) if bar_atr else None,
                    "is_same_bar_fill":           False,
                    "impulse_bar_vol_vs_median":  sv["impulse_bar_vol_vs_median"],
                    "pullback_vol_ratio":         sv["pullback_vol_ratio"],
                    "retest_bar_vol_vs_median":   sv["retest_bar_vol_vs_median"],
                },
            )

        return None

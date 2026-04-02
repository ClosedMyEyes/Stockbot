"""
config.py — Central configuration for the orchestrator.
Edit this file to set your universe, risk params, and connections.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# =============================================================================
# IBKR CONNECTION
# =============================================================================
IBKR_HOST       = "127.0.0.1"
IBKR_PORT       = 7497          # 7497 = TWS paper trading; 7496 = live
IBKR_CLIENT_ID  = 10            # unique client ID for the orchestrator

# =============================================================================
# SIGNALSTACK
# =============================================================================
SIGNALSTACK_WEBHOOK_URL = "https://api.signalstack.com/webhook"
SIGNALSTACK_API_KEY     = "YOUR_SIGNALSTACK_API_KEY"   # set via env var in prod

# =============================================================================
# RISK PARAMETERS
# =============================================================================
RISK_PER_TRADE_DOLLARS  = 100.0    # fixed dollar risk per trade (1R)
MAX_SIMULTANEOUS_POSITIONS = 3     # max open trades across all strategies/symbols
DAILY_LOSS_LIMIT_DOLLARS   = 300.0 # halt new signals if daily P&L drops below this
MAX_POSITIONS_PER_SYMBOL   = 1     # if two strategies want the same symbol, first wins

# =============================================================================
# SESSION TIMING
# =============================================================================
RTH_START = "09:30"
RTH_END   = "15:59"
EOD_BAR   = "15:59"
PREMARKET_ROUTINE_TIME = "09:15"   # when to run ATR/gap pre-calc

# =============================================================================
# SYMBOL UNIVERSES PER STRATEGY
# Keys must match strategy IDs in strategies/__init__.py
# =============================================================================
STRATEGY_UNIVERSES: Dict[str, List[str]] = {
    "orb_short":        ["AMZN", "C", "FCX", "GE", "LVS", "ORCL", "QCOM"],   # ← fill in your symbols
    "orb_long":         ["BIDU", "CSCO", "F", "GOOG", "IBM", "JPM", "MSFT", "QCOM", "VZ", "WMT"],
    "impulse_short":    ["EMC", "F", "FB", "FCX", "GOOG", "GOOGL", "GS", "HD", "HPQ", "JNJ", "JPM", "KO", "LOW", "MCD", "MRK", "MSFT", "ORCL", "PFE", "PM", "PSE", "T", "VZ", "WFC"],
    "gap_fill_large":   ["AAPL", "MSFT", "WMT", "T", "PFE", "OXY", "ORCL", "NFLX", "MS", "LOW", "JPM", "HAL", "GE", "FCX", "F", "CSCO", "CRM", "CAT", "BIDU", "AMZN", "AMGN"],
    "gap_fill_small":   ["AMZN", "CSCO", "F", "GILD", "GS", "HPQ", "JNJ", "LOW", "LVS", "MCD", "MS"],
    "gap_fill_big":     ["MS", "T", "MCD", "LOW", "HPQ", "F", "CVX", "CSCO", "AMZN"],
}

# =============================================================================
# STRATEGY PARAMETERS
# These mirror the argparse defaults in the original scripts.
# Override per-strategy here; the strategy class reads from this dict.
# =============================================================================
STRATEGY_PARAMS: Dict[str, dict] = {
    "orb_short": {
        "observe_bars":         15,
        "vol_delta_min":        2.0,
        "vol_down_min_pct":     3.5,
        "vwap_entry_pct":       0.20,
        "vwap_drift_min_pct":   0.3,
        "vwap_drift_max_pct":   1.5,
        "obs_range_min_pct":    0.0,
        "retest_timeout":       0,
        "tp_mode":              "obs_level",
        "tp_mult":              2.5,
        "sl_buffer_pct":        0.8,
        "min_r_pct":            0.20,
        "slippage_pct":         0.03,
        "entry_gap_max_pct":    0.15,
        "entry_time_min":       "09:55",
        "entry_time_max":       "14:00",
        "skip_monday":          True,
        "skip_friday":          True,
        "skip_months":          {8, 9},
        "vol_regime_min":       0.6,
        "vol_regime_max":       1.6,
        "trail_activation_r":   0,
        "trail_lock_r":         0,
        "hold_cap_bars":        0,
        "hold_cap_exit_r":      -0.2,
    },
    "orb_long": {
        "observe_bars":         18,
        "vol_delta_min":        2.0,
        "vol_delta_max":        5.0,
        "vol_dom_min_pct":      3.5,
        "vwap_entry_pct":       0.30,
        "vwap_drift_min_pct":   0.5,
        "vwap_drift_max_pct":   2.0,
        "obs_range_min_pct":    0.0,
        "retest_timeout":       60,
        "tp_mode":              "obs_level",
        "tp_mult":              0.8,
        "sl_buffer_pct":        0.5,
        "min_r_pct":            0.10,
        "slippage_pct":         0.03,
        "entry_gap_max_pct":    0.15,
        "entry_time_min":       "10:00",
        "entry_time_max":       "14:00",
        "skip_monday":          False,
        "skip_friday":          False,
        "skip_months":          {7},
        "vol_regime_min":       0,
        "vol_regime_max":       1.6,
        "trail_activation_r":   0,
        "trail_lock_r":         0,
        "hold_cap_bars":        0,
        "hold_cap_exit_r":      0,
    },
    "impulse_short": {
        "atr_impulse_mult":         0.9,
        "impulse_size_pct_min":     0.7,
        "impulse_min_bars":         0,
        "deep_retrace_min":         0.60,
        "deep_retrace_max":         0.95,
        "retest_pct":               0.10,
        "retest_max_bars":          15,
        "stop_buffer_mult":         0.10,
        "tp_mode":                  "fixed",
        "tp_fixed_mult":            1.0,
        "min_r_pct":                0.08,
        "slippage_pct":             0.05,
        "breakout_ibs_min":         0.95,
        "min_failure_body_pct":     85,
        "max_pullback_vol_ratio":   1.8,
        "breakout_min":             0.75,
        "entry_time_start":         "09:30",
        "entry_time_end":           "15:55",
        "ema_period":               20,
        "ema_slope_bars":           0,
    },
    "gap_fill_large": {
        "gap_min_pct":              2.5,
        "gap_max_pct":              5.0,
        "direction":                "long",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "session_extreme",
        "sl_buffer_pct":            0.15,
        "entry_time_max":           "11:00",
        "entry_gap_max_pct":        0.10,
        "max_bars_to_entry":        2,
        "min_gap_fill_at_entry":    -0.05,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              set(),
        "vol_regime_min":           0.5,
        "vol_regime_max":           3.0,
        "gap_vol_ratio_min":        1.0,
        "gap_atr_ratio_min":        0.6,
        "gap_atr_ratio_max":        0,
    },
    "gap_fill_small": {
        "gap_min_pct":              1.5,
        "gap_max_pct":              5.0,
        "direction":                "short",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "gap_open_buffer",
        "sl_buffer_pct":            0.1,
        "entry_time_max":           "11:00",
        "entry_gap_max_pct":        0.15,
        "max_bars_to_entry":        0,
        "min_gap_fill_at_entry":    0.2,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              set(),
        "vol_regime_min":           0.2,
        "vol_regime_max":           3.0,
        "gap_vol_ratio_min":        0,
        "gap_atr_ratio_min":        0.2,
        "gap_atr_ratio_max":        0.6,
    },
    "gap_fill_big": {
        "gap_min_pct":              1.5,
        "gap_max_pct":              5.0,
        "direction":                "short",
        "gap_fill_target_pct":      3.0,
        "stop_type":                "gap_open_buffer",
        "sl_buffer_pct":            0.1,
        "entry_time_max":           "11:00",
        "entry_gap_max_pct":        0.15,
        "max_bars_to_entry":        0,
        "min_gap_fill_at_entry":    0.2,
        "min_r_pct":                0.10,
        "slippage_pct":             0.08,
        "hold_cap_bars":            0,
        "hold_cap_exit_r":          -0.3,
        "skip_monday":              False,
        "skip_friday":              False,
        "skip_months":              set(),
        "vol_regime_min":           0.7,
        "vol_regime_max":           3.0,
        "gap_vol_ratio_min":        0,
        "gap_atr_ratio_min":        0.7,
        "gap_atr_ratio_max":        1.0,
    },
}

# =============================================================================
# LOGGING
# =============================================================================
LOG_DIR              = "logs"
TRADE_LOG_CSV        = "logs/trade_log.csv"
SIGNAL_LOG_CSV       = "logs/signal_log.csv"
CONFLICT_LOG_CSV     = "logs/conflict_log.csv"
DAILY_SUMMARY_CSV    = "logs/daily_summary.csv"

# =============================================================================
# DASHBOARD
# =============================================================================
DASHBOARD_PORT   = 8050      # local Dash/Flask port
DASHBOARD_ENABLE = True

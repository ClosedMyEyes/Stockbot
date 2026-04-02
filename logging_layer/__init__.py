"""
logging_layer/__init__.py — Structured CSV logging for the orchestrator.

Writes three logs:
  trade_log.csv    — every completed trade (matches backtest CSV schema)
  signal_log.csv   — every signal fired, accepted or rejected
  conflict_log.csv — symbol conflicts
  daily_summary.csv — end-of-day summary
"""

import csv
import logging
import os
from datetime import datetime
from typing import Optional

from ..models import OpenPosition
from .. import config

log = logging.getLogger("logging_layer")


def _ensure_dir():
    os.makedirs(config.LOG_DIR, exist_ok=True)


def _write_row(path: str, row: dict, fieldnames: list):
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


# ── Trade log ─────────────────────────────────────────────────────────────────

TRADE_FIELDS = [
    "strategy_id", "symbol", "session", "entry_time", "exit_time",
    "direction", "entry_price", "exit_price", "stop", "tp",
    "result_R", "result_dollars", "shares", "exit_reason",
    "bars_to_exit", "R_dollars",
    # meta fields (strategy-specific, may be empty)
    "gap_pct", "prior_close", "today_open",
    "vwap_at_signal", "vwap_at_trigger",
    "obs_up_vol", "obs_down_vol", "vol_delta_ratio",
    "impulse_high", "impulse_low", "deep_retrace_ratio",
    "gap_atr_ratio", "first_bar_vol_ratio", "bars_to_entry",
]


def log_trade(pos: OpenPosition, exit_price: float, exit_time: str,
              exit_reason: str, result_r: float, bars_to_exit: int,
              meta: dict = None):
    _ensure_dir()
    meta = meta or {}
    dir_mult = 1 if pos.direction == "long" else -1
    pnl = (exit_price - pos.entry_price) * dir_mult * pos.shares
    row = {
        "strategy_id":   pos.strategy_id,
        "symbol":        pos.symbol,
        "session":       pos.session_date,
        "entry_time":    pos.entry_time,
        "exit_time":     exit_time,
        "direction":     pos.direction,
        "entry_price":   round(pos.entry_price, 4),
        "exit_price":    round(exit_price, 4),
        "stop":          round(pos.stop, 4),
        "tp":            round(pos.tp, 4),
        "result_R":      round(result_r, 4),
        "result_dollars": round(pnl, 2),
        "shares":        pos.shares,
        "exit_reason":   exit_reason,
        "bars_to_exit":  bars_to_exit,
        "R_dollars":     round(pos.R_dollars, 2),
        **{k: meta.get(k, "") for k in TRADE_FIELDS if k in meta},
    }
    _write_row(config.TRADE_LOG_CSV, row, TRADE_FIELDS)
    log.info(f"Trade logged: {pos.symbol} {result_r:+.3f}R")


# ── Signal log ────────────────────────────────────────────────────────────────

SIGNAL_FIELDS = [
    "timestamp", "strategy_id", "symbol", "session", "bar_time",
    "direction", "entry_price", "stop", "tp", "R",
    "status",   # ACCEPTED | REJECTED:<reason>
    "shares_approved",
    # meta
    "gap_pct", "vwap_at_signal", "impulse_high",
]


def log_signal(strategy_id: str, symbol: str, session: str, bar_time: str,
               direction: str, entry: float, stop: float, tp: float, R: float,
               status: str, shares: int = 0, meta: dict = None):
    _ensure_dir()
    meta = meta or {}
    row = {
        "timestamp":      datetime.now().strftime("%H:%M:%S"),
        "strategy_id":    strategy_id,
        "symbol":         symbol,
        "session":        session,
        "bar_time":       bar_time,
        "direction":      direction,
        "entry_price":    round(entry, 4),
        "stop":           round(stop, 4),
        "tp":             round(tp, 4),
        "R":              round(R, 6),
        "status":         status,
        "shares_approved": shares,
        **{k: meta.get(k, "") for k in SIGNAL_FIELDS if k in meta},
    }
    _write_row(config.SIGNAL_LOG_CSV, row, SIGNAL_FIELDS)


# ── Conflict log ──────────────────────────────────────────────────────────────

CONFLICT_FIELDS = [
    "timestamp", "session", "bar_time",
    "winner_strategy", "winner_symbol",
    "loser_strategy",  "loser_symbol",
    "conflict_type",
]


def log_conflict(session: str, bar_time: str,
                 winner_strategy: str, loser_strategy: str,
                 symbol: str, conflict_type: str):
    _ensure_dir()
    row = {
        "timestamp":       datetime.now().strftime("%H:%M:%S"),
        "session":         session,
        "bar_time":        bar_time,
        "winner_strategy": winner_strategy,
        "winner_symbol":   symbol,
        "loser_strategy":  loser_strategy,
        "loser_symbol":    symbol,
        "conflict_type":   conflict_type,
    }
    _write_row(config.CONFLICT_LOG_CSV, row, CONFLICT_FIELDS)


# ── Daily summary ─────────────────────────────────────────────────────────────

SUMMARY_FIELDS = [
    "session", "total_signals", "signals_accepted", "signals_rejected",
    "trades_won", "trades_lost", "trades_eod",
    "total_R", "total_dollars",
    "max_simultaneous", "daily_loss_halted",
    "strategies_active",
]


def log_daily_summary(session: str, risk_summary: dict,
                      signal_count: int, accepted: int,
                      wins: int, losses: int, eod_closes: int,
                      max_sim: int, halted: bool, strategies: list):
    _ensure_dir()
    row = {
        "session":           session,
        "total_signals":     signal_count,
        "signals_accepted":  accepted,
        "signals_rejected":  signal_count - accepted,
        "trades_won":        wins,
        "trades_lost":       losses,
        "trades_eod":        eod_closes,
        "total_R":           risk_summary.get("daily_r_total", 0),
        "total_dollars":     risk_summary.get("daily_pnl_dollars", 0),
        "max_simultaneous":  max_sim,
        "daily_loss_halted": halted,
        "strategies_active": ",".join(strategies),
    }
    _write_row(config.DAILY_SUMMARY_CSV, row, SUMMARY_FIELDS)
    log.info(f"Daily summary written for {session}: "
             f"{accepted} trades accepted, "
             f"R={risk_summary.get('daily_r_total', 0):+.2f}")

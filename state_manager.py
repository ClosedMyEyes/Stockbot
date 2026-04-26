"""
state_manager.py — Atomic state persistence and IBKR position reconciliation.

Writes state.json on every position change so the orchestrator can recover
cleanly from any crash, disconnect, or restart without double-entering or
orphaning positions.

STATE FILE SCHEMA
-----------------
{
  "session_date": "2026-04-13",
  "halted": false,
  "daily_r_total": -0.5,
  "daily_pnl_dollars": -50.0,
  "open_positions": {
    "<trade_id>": {
      "trade_id": "...",
      "symbol": "FCX",
      "strategy_id": "orb_short",
      "direction": "short",
      "entry_price": 45.23,
      "stop": 45.89,
      "tp": 43.10,
      "R_dollars": 0.66,
      "shares": 151,
      "entry_time": "10:43",
      "meta": { ... }
    }
  }
}

RECONCILIATION LOGIC (called on startup and reconnect)
-------------------------------------------------------
For each position in state.json:
  A. Also in IBKR reqPositions() → restore OpenPosition, resume exit monitoring.
  B. NOT in IBKR → was closed while we were disconnected.
       Query reqExecutions() for the fill. Log as "disconnected exit".
       Clear from state, notify strategy so _in_trade resets.

For each IBKR position NOT in state.json:
  C. Orphan — not ours or pre-existing manual trade.
       Log WARN. Offer to close via emergency_close_orphan().
"""

import json
import logging
import os
import tempfile
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger("orchestrator.state")

def _json_default(obj):
    """
    JSON serializer for types not handled by default.
    - set / frozenset → sorted list (restorable, readable)
    - anything else → str fallback (same as before, but sets are now caught first)
    """
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)

STATE_FILE = os.path.join("logs", "state.json")


# =============================================================================
# ATOMIC WRITE
# =============================================================================

def _atomic_write(path: str, data: dict) -> None:
    """
    Write JSON atomically via temp file + rename.
    Guarantees the state file is never left in a half-written state
    even if the process is killed mid-write.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".state_tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=_json_default)
        os.replace(tmp_path, path)  # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# STATE MANAGER
# =============================================================================

class StateManager:
    """
    Single source of truth for persistent orchestrator state.
    Call save() after every position change.
    Call load() on startup.
    Call reconcile() after any IBKR (re)connect.
    """

    def __init__(self, path: str = STATE_FILE):
        self._path    = path
        self._session = ""
        self._halted  = False
        self._daily_r = 0.0
        self._daily_pnl = 0.0
        # Serialisable position snapshots (not OpenPosition objects)
        self._positions: Dict[str, dict] = {}

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        """Write current state atomically. Call after every position change."""
        try:
            _atomic_write(self._path, {
                "session_date":    self._session,
                "halted":          self._halted,
                "daily_r_total":   round(self._daily_r,   4),
                "daily_pnl_dollars": round(self._daily_pnl, 4),
                "open_positions":  self._positions,
            })
        except Exception as e:
            log.error(f"StateManager.save() failed: {e}")

    def load(self) -> Optional[dict]:
        """
        Load state from disk. Returns the raw dict so the orchestrator
        can decide whether to restore it (same session_date) or discard it.
        Returns None if no state file exists.
        """
        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path) as f:
                data = json.load(f)
            log.info(f"Loaded state from {self._path}: "
                     f"session={data.get('session_date')} "
                     f"positions={len(data.get('open_positions', {}))}")
            return data
        except Exception as e:
            log.error(f"StateManager.load() failed: {e} — starting fresh")
            return None

    def restore_from_dict(self, data: dict) -> None:
        """Populate internal state from a previously loaded dict."""
        self._session    = data.get("session_date", "")
        self._halted     = data.get("halted", False)
        self._daily_r    = data.get("daily_r_total", 0.0)
        self._daily_pnl  = data.get("daily_pnl_dollars", 0.0)
        self._positions  = data.get("open_positions", {})

    def clear_session(self) -> None:
        """Reset all state at the start of a new trading day."""
        self._positions  = {}
        self._halted     = False
        self._daily_r    = 0.0
        self._daily_pnl  = 0.0
        self.save()

    # ── Position tracking (called by orchestrator) ────────────────────────────

    def on_position_open(self, pos, session_date: str) -> None:
        """Record a new position and persist."""
        self._session = session_date
        self._positions[pos.trade_id] = {
            "trade_id":    pos.trade_id,
            "symbol":      pos.symbol,
            "strategy_id": pos.strategy_id,
            "direction":   pos.direction,
            "entry_price": pos.entry_price,
            "stop":        pos.stop,
            "tp":          pos.tp,
            "R_dollars":   pos.R_dollars,
            "shares":      pos.shares,
            "entry_time":  pos.entry_time if hasattr(pos, "entry_time") else "",
            "meta":        getattr(pos, "meta", {}),
        }
        self.save()

    def on_position_close(self, trade_id: str, result_r: float,
                          pnl_dollars: float) -> None:
        """Remove a position and update daily stats, then persist."""
        self._positions.pop(trade_id, None)
        self._daily_r   += result_r
        self._daily_pnl += pnl_dollars
        self.save()

    def on_halt(self, halted: bool) -> None:
        self._halted = halted
        self.save()

    def on_shares_adjusted(self, trade_id: str, actual_shares: int) -> None:
        """Update shares after fill verification detects a partial fill."""
        if trade_id in self._positions:
            self._positions[trade_id]["shares"] = actual_shares
            self.save()

    @property
    def saved_positions(self) -> Dict[str, dict]:
        return dict(self._positions)

    # ── IBKR Reconciliation ───────────────────────────────────────────────────

    def reconcile(self, ib, risk_manager, strategy_map: dict,
                  session_date: str) -> Tuple[int, int, int]:
        """
        Compare state.json positions against actual IBKR positions.
        Returns (restored, ghost_closed, orphans_found).

        Parameters
        ----------
        ib             : ib_insync IB instance (connected)
        risk_manager   : RiskManager — to restore open_positions
        strategy_map   : dict mapping symbol → list[BaseStrategy]
                         used to reset _in_trade on ghost positions
        session_date   : today's date string
        """
        restored = ghost_closed = orphans = 0

        # ── Fetch actual IBKR positions ───────────────────────────────────────
        try:
            ib_positions = ib.positions()
            # Build {symbol → quantity} map (only equities, only non-zero)
            ib_sym_qty = {}
            for p in ib_positions:
                sym = p.contract.symbol
                qty = p.position
                if qty != 0:
                    ib_sym_qty[sym] = qty
        except Exception as e:
            log.error(f"reconcile: reqPositions() failed: {e} — skipping reconciliation")
            return 0, 0, 0

        saved = dict(self._positions)  # snapshot so we can modify during iteration

        # ── Case A / B: positions we think are open ───────────────────────────
        for trade_id, snap in saved.items():
            sym = snap["symbol"]

            if sym in ib_sym_qty:
                # Case A — position still open at IBKR: restore it
                from .models import OpenPosition
                pos = OpenPosition(
                    trade_id     = trade_id,
                    strategy_id  = snap["strategy_id"],
                    symbol       = sym,
                    direction    = snap["direction"],
                    entry_price  = snap["entry_price"],
                    stop         = snap["stop"],
                    tp           = snap["tp"],
                    R_dollars    = snap.get("R_dollars", snap.get("R", 0.0)),
                    shares       = snap["shares"],
                    entry_time   = snap.get("entry_time", ""),
                    session_date = session_date,
                )
                risk_manager.restore_position(pos)

                # Re-mark strategy as in_trade
                for strat in strategy_map.get(sym, []):
                    if strat.strategy_id == snap["strategy_id"]:
                        strat._in_trade = True
                        break

                log.info(f"RECONCILE restored: {snap['strategy_id']}({sym}) "
                         f"trade_id={trade_id}  shares={snap['shares']}")
                restored += 1

            else:
                # Case B — was in state.json but not in IBKR: closed while disconnected
                log.warning(f"RECONCILE ghost: {snap['strategy_id']}({sym}) "
                            f"trade_id={trade_id} — closed while disconnected")

                # Try to get fill price from execution history
                exit_price = self._query_exit_fill(ib, sym, snap["direction"],
                                                   snap["entry_price"])
                result_r   = self._estimate_result_r(snap, exit_price)

                from .logging_layer import log_trade as ll_log_trade
                class _FakePosForLog:
                    pass
                fpos = _FakePosForLog()
                fpos.trade_id    = trade_id
                fpos.strategy_id = snap["strategy_id"]
                fpos.symbol      = sym
                fpos.direction   = snap["direction"]
                fpos.entry_price = snap["entry_price"]
                fpos.stop        = snap["stop"]
                fpos.tp          = snap["tp"]
                fpos.R_dollars   = snap.get("R_dollars", snap.get("R", 0.0))
                fpos.shares      = snap["shares"]

                try:
                    ll_log_trade(
                        pos=fpos, exit_price=exit_price,
                        exit_time="unknown", exit_reason="disconnected exit",
                        result_r=result_r, bars_to_exit=None,
                        meta=snap.get("meta", {}),
                    )
                except Exception:
                    pass  # logging failure must never crash reconciliation

                # Reset strategy _in_trade
                for strat in strategy_map.get(sym, []):
                    if strat.strategy_id == snap["strategy_id"]:
                        strat._in_trade = False
                        break

                self._positions.pop(trade_id, None)
                ghost_closed += 1

        # ── Case C: positions at IBKR we don't know about ─────────────────────
        our_symbols = {snap["symbol"] for snap in saved.values()}
        for sym, qty in ib_sym_qty.items():
            if sym not in our_symbols:
                log.warning(
                    f"RECONCILE orphan: IBKR has {qty} shares of {sym} "
                    f"but no matching state record. "
                    f"This may be a manual trade or a pre-existing position. "
                    f"NOT touching it — review manually."
                )
                orphans += 1

        self.save()  # persist any changes from ghost resolution
        return restored, ghost_closed, orphans

    def _query_exit_fill(self, ib, symbol: str, direction: str,
                         entry_price: float) -> float:
        """
        Query recent executions to find the most likely exit fill for this symbol.
        Falls back to entry_price (0R) if nothing found.
        """
        try:
            execs = ib.executions()
            # Filter by symbol, opposite action (our exit would be buy-to-cover or sell)
            exit_action = "BOT" if direction == "short" else "SLD"
            candidates  = [
                e for e in execs
                if e.contract.symbol == symbol and e.execution.side == exit_action
            ]
            if candidates:
                # Most recent execution
                last = sorted(candidates, key=lambda x: x.execution.time)[-1]
                return last.execution.avgPrice
        except Exception as e:
            log.warning(f"_query_exit_fill({symbol}): {e}")
        return entry_price  # fallback: 0R — conservative unknown

    @staticmethod
    def _estimate_result_r(snap: dict, exit_price: float) -> float:
        try:
            ep  = snap["entry_price"]
            R   = snap["R_dollars"]
            d   = 1 if snap["direction"] == "long" else -1
            return round((exit_price - ep) * d / R, 4) if R > 0 else 0.0
        except Exception:
            return 0.0

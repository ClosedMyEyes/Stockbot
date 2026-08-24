"""
P1-5: a mid-session restart must not wipe what startup reconciliation just
restored. Before the fix, the first bar after a restart triggered
_on_new_session, which reset daily P&L / halts and cleared the restored
positions out of state.json (a second crash would then orphan them).
"""

import json
import os
from types import SimpleNamespace

from conftest import make_bar, pkg

TODAY = "2026-01-13"


def _write_state_file(daily_r=-2.5, daily_pnl=-167.5):
    os.makedirs("logs", exist_ok=True)
    with open(os.path.join("logs", "state.json"), "w") as f:
        json.dump({
            "session_date": TODAY,
            "halted": False,
            "daily_r_total": daily_r,
            "daily_pnl_dollars": daily_pnl,
            "open_positions": {
                "t1": {
                    "trade_id": "t1", "symbol": "GS",
                    "strategy_id": "gap_fill_small", "direction": "short",
                    "entry_price": 100.0, "stop": 101.0, "tp": 97.0,
                    "R_dollars": 67.0, "shares": 67, "entry_time": "09:41",
                    "stop_order_id": None, "meta": {"gap_dir": -1},
                },
            },
        }, f)


def _fake_feed_with_gs_short():
    ib = SimpleNamespace(positions=lambda: [
        SimpleNamespace(contract=SimpleNamespace(symbol="GS", secType="STK"),
                        position=-67),
    ])
    return SimpleNamespace(_ib=ib)


def test_restart_preserves_restored_session_state(orchestrator):
    orch = orchestrator
    _write_state_file()

    orch._startup_reconcile(_fake_feed_with_gs_short(), TODAY)
    assert orch.risk.daily_pnl_dollars == -167.5
    assert "t1" in orch.risk.open_positions

    # Worst case: the first live bar is for a DIFFERENT symbol — it triggers
    # the global session rollover.
    orch.on_bar(make_bar("AAPL", TODAY, "10:15", o=200.0))

    assert orch.risk.daily_pnl_dollars == -167.5          # not reset
    assert orch.risk.daily_r_total == -2.5
    assert "t1" in orch.risk.open_positions               # still tracked
    assert "t1" in orch.state.saved_positions             # still in state.json
    assert orch._positions_meta["t1"] == {"gap_dir": -1}  # meta kept

    # GS's own first bar lazily resets its strategies — the restored
    # position's strategy must come out of it still marked in-trade.
    orch.on_bar(make_bar("GS", TODAY, "10:16", o=100.2))
    strat = next(s for s in orch.strategies["GS"]
                 if s.strategy_id == "gap_fill_small")
    assert strat._in_trade is True
    assert "t1" in orch.risk.open_positions               # no accidental exit


def test_cold_start_still_resets_the_day(orchestrator):
    """No state file → normal path: the day resets as before."""
    orch = orchestrator
    orch.risk.daily_pnl_dollars = -500.0                  # leftover garbage
    orch.on_bar(make_bar("GS", TODAY, "09:31", o=100.0))
    assert orch.risk.daily_pnl_dollars == 0.0

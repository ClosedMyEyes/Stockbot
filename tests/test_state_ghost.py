"""
Ghost-exit ("disconnected exit") logging: fill lookup must use ib.fills()
(Fill objects carry .contract; ib.executions() returned bare Execution objects
and always fell back to entry price), and the R estimate must divide per-share
P&L by per-share risk, not by whole-trade R_dollars.
"""

from datetime import datetime
from types import SimpleNamespace

from conftest import pkg


def _fill(symbol, side, when, avg_price):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        execution=SimpleNamespace(side=side, time=when, avgPrice=avg_price),
    )


def _mgr(tmp_path):
    sm = pkg("state_manager")
    return sm.StateManager(path=str(tmp_path / "state.json"))


def test_query_exit_fill_finds_latest_matching_fill(tmp_path):
    mgr = _mgr(tmp_path)
    ib = SimpleNamespace(fills=lambda: [
        _fill("MS", "BOT", datetime(2026, 1, 5, 10, 0), 50.0),     # wrong symbol
        _fill("GS", "SLD", datetime(2026, 1, 5, 10, 1), 99.0),     # wrong side (short exit = BOT)
        _fill("GS", "BOT", datetime(2026, 1, 5, 10, 2), 101.5),
        _fill("GS", "BOT", datetime(2026, 1, 5, 10, 30), 102.25),  # latest → wins
    ])
    price = mgr._query_exit_fill(ib, "GS", direction="short", entry_price=100.0)
    assert price == 102.25


def test_query_exit_fill_falls_back_to_entry_price(tmp_path):
    mgr = _mgr(tmp_path)

    def _boom():
        raise ConnectionError("not connected")

    assert mgr._query_exit_fill(SimpleNamespace(fills=_boom), "GS",
                                direction="long", entry_price=100.0) == 100.0
    # no matching fills → same fallback
    assert mgr._query_exit_fill(SimpleNamespace(fills=lambda: []), "GS",
                                direction="long", entry_price=100.0) == 100.0


def test_estimate_result_r_uses_per_share_risk(tmp_path):
    sm = pkg("state_manager")
    est = sm.StateManager._estimate_result_r

    long_snap = {"entry_price": 100.0, "stop": 99.0,
                 "direction": "long", "R_dollars": 500.0}
    # +2.00 per share on 1.00 per-share risk = +2R (old code: 2.0/500 = 0.004R)
    assert est(long_snap, 102.0) == 2.0
    assert est(long_snap, 99.0) == -1.0

    short_snap = {"entry_price": 100.0, "stop": 101.0,
                  "direction": "short", "R_dollars": 250.0}
    assert est(short_snap, 99.0) == 1.0
    assert est(short_snap, 101.0) == -1.0

    # degenerate stop == entry → 0, never a ZeroDivisionError
    assert est({"entry_price": 100.0, "stop": 100.0,
                "direction": "long", "R_dollars": 100.0}, 105.0) == 0.0

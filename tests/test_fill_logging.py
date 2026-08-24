"""
P1-1: broker fill reporting. result_R stays trigger-based (comparable to the
backtests); actual fills land in new appended trade_log columns
(entry_fill_price, exit_fill_price, slippage_r) and in fill_log.csv.
"""

import pytest

from conftest import pkg
from test_ibkr_stops import FakeIB, _make_pos


def test_trade_fields_appended_not_renamed():
    llmod = pkg("logging_layer")
    original = [
        "strategy_id", "symbol", "session", "entry_time", "exit_time",
        "direction", "entry_price", "exit_price", "stop", "tp",
        "result_R", "result_dollars", "shares", "exit_reason",
        "bars_to_exit", "R_dollars",
    ]
    assert llmod.TRADE_FIELDS[:len(original)] == original
    assert llmod.TRADE_FIELDS[-3:] == ["entry_fill_price", "exit_fill_price",
                                       "slippage_r"]


def test_entry_and_exit_fill_events_routed():
    execution = pkg("execution")
    ib = FakeIB()
    ex = execution.IBKRExecution(ib)
    got = []
    ex.on_entry_filled = lambda tid, px: got.append(("entry", tid, px))
    ex.on_exit_filled  = lambda tid, px: got.append(("exit", tid, px))

    pos = _make_pos()
    ex.send_entry(pos)
    parent = ib.placed[0]
    parent.orderStatus.avgFillPrice = 99.97
    parent.filledEvent.emit(parent)

    ex.send_exit(pos, 99.0, "TP hit")
    exit_trade = ib.placed[-1]
    exit_trade.orderStatus.avgFillPrice = 99.02
    exit_trade.filledEvent.emit(exit_trade)

    assert got == [("entry", "t1", 99.97), ("exit", "t1", 99.02)]


def test_entry_fill_recorded_on_position(orchestrator, monkeypatch):
    llmod = pkg("logging_layer")
    fills = []
    monkeypatch.setattr(llmod, "log_fill",
                        lambda tid, kind, sym, px: fills.append((tid, kind, px)))
    pos = _make_pos()
    orchestrator.risk.open_positions["t1"] = pos

    orchestrator._on_entry_filled("t1", 100.03)

    assert pos.entry_fill_price == 100.03
    assert fills == [("t1", "entry", 100.03)]


def test_broker_stop_close_carries_fill_columns(orchestrator, monkeypatch):
    execution = pkg("execution")
    llmod = pkg("logging_layer")
    orch = orchestrator
    ib = FakeIB()
    ex = execution.IBKRExecution(ib)
    orch.executor = ex
    ex.on_stop_filled = orch._on_broker_stop_filled

    rows, fills = [], []
    monkeypatch.setattr(llmod, "log_trade", lambda **kw: rows.append(kw))
    monkeypatch.setattr(llmod, "log_fill",
                        lambda tid, kind, sym, px: fills.append((tid, kind, px)))

    pos = _make_pos()                       # short GS, entry 100, stop 101.004
    ex.send_entry(pos)
    orch.risk.open_positions["t1"] = pos

    stop_trade = ex._stop_trades["t1"]
    stop_trade.orderStatus.status = "Filled"
    stop_trade.orderStatus.avgFillPrice = 101.2
    stop_trade.filledEvent.emit(stop_trade)

    assert ("t1", "stop", 101.2) in fills
    assert len(rows) == 1
    row = rows[0]
    assert row["exit_fill_price"] == 101.2
    # short, per-share risk = |100 − 101.004|; fill 0.196 through the 101.004
    # trigger → negative slippage (worse than trigger)
    expected = (101.2 - pos.stop) * -1 / abs(pos.entry_price - pos.stop)
    assert row["slippage_r"] == pytest.approx(expected)
    assert row["exit_reason"] == "stopped (broker stop)"


def test_paper_mode_unchanged(orchestrator, monkeypatch):
    """Paper closes carry no fill columns — same rows as before."""
    llmod = pkg("logging_layer")
    rows = []
    monkeypatch.setattr(llmod, "log_trade", lambda **kw: rows.append(kw))
    pos = _make_pos()
    orchestrator.risk.open_positions["t1"] = pos

    orchestrator._do_close("t1", pos, 99.0, "TP hit", "10:30")

    assert rows[0]["exit_fill_price"] is None
    assert rows[0]["slippage_r"] is None

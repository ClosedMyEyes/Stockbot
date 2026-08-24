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
    assert llmod.TRADE_FIELDS[-4:] == ["entry_fill_price", "exit_fill_price",
                                       "slippage_r", "trade_id"]


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


def test_broker_tp_fill_closes_at_trigger(orchestrator, monkeypatch):
    """A TP limit fill closes the trade accounted at the TRIGGER (pos.tp) —
    result_R stays comparable to the backtest — with the actual (equal or
    better) fill in the slippage columns."""
    execution = pkg("execution")
    llmod = pkg("logging_layer")
    orch = orchestrator
    ib = FakeIB()
    ex = execution.IBKRExecution(ib)
    orch.executor = ex
    ex.on_tp_filled = orch._on_broker_tp_filled

    rows = []
    monkeypatch.setattr(llmod, "log_trade", lambda **kw: rows.append(kw))
    monkeypatch.setattr(llmod, "log_fill", lambda *a, **kw: None)

    pos = _make_pos()                       # short, entry 100, tp 97.0
    ex.send_entry(pos)
    orch.risk.open_positions["t1"] = pos

    tp_trade = ex._tp_trades["t1"]
    tp_trade.orderStatus.status = "Filled"
    tp_trade.orderStatus.avgFillPrice = 96.98   # limit filled a hair better
    n_before = len(ib.placed)

    tp_trade.filledEvent.emit(tp_trade)

    assert "t1" not in orch.risk.open_positions
    assert len(ib.placed) == n_before           # no duplicate close order
    row = rows[0]
    assert row["exit_price"] == pos.tp          # trigger-based accounting
    assert row["exit_reason"] == "TP hit (broker limit)"
    assert row["exit_fill_price"] == 96.98
    # short: filling below the 97.0 trigger is a small positive slippage
    expected = (96.98 - pos.tp) * -1 / abs(pos.entry_price - pos.stop)
    assert row["slippage_r"] == pytest.approx(expected)


def test_finalize_fills_backfills_software_exits(orchestrator):
    """Post-market: exit fills from fill_log.csv land in trade rows that were
    written before the market order's fill was known."""
    llmod = pkg("logging_layer")
    pos = _make_pos()                       # short, entry 100, stop 101.004

    # Trade row written at trigger time (no fill known yet)…
    llmod.log_trade(pos=pos, exit_price=97.0, exit_time="10:30",
                    exit_reason="TP hit", result_r=2.988, bars_to_exit=12)
    # …then the closing market order reports its fill.
    llmod.log_fill("t1", "exit", "GS", 97.05)

    assert llmod.finalize_fills() == 1

    import csv
    with open(llmod.config.TRADE_LOG_CSV, newline="") as f:
        row = list(csv.DictReader(f))[0]
    assert row["result_R"] == "2.988"                    # untouched
    assert row["exit_fill_price"] == "97.05"
    # short: fill 0.05 above the 97.0 trigger = worse → negative slippage
    expected = (97.05 - 97.0) * -1 / abs(pos.entry_price - pos.stop)
    assert float(row["slippage_r"]) == pytest.approx(expected, abs=1e-4)

    # Idempotent: nothing left to patch on a second run
    assert llmod.finalize_fills() == 0

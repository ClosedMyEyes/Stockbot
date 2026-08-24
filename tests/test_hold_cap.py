"""
P1-3: hold-time cap wired into live exit detection. All hold_cap_bars params
remain 0 in config (disabled) — behavior only changes when the owner turns
them on.
"""

from conftest import make_bar, pkg
from test_ibkr_stops import _make_pos


def _neutral_bar():
    # o=h=l=c=100 → neither the 101.004 stop nor the 97 TP is touched
    return make_bar("GS", "2026-01-13", "10:20", o=100.0)


def test_hold_cap_exits_after_n_bars(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    orch = orchestrator
    pos = _make_pos()
    orch.risk.open_positions["t1"] = pos
    orch._position_entry_bar["t1"] = 10

    orch._bar_counters["GS"] = 14                       # held 4 bars
    assert orch._detect_exit(_neutral_bar(), pos) == (None, None)

    orch._bar_counters["GS"] = 15                       # held 5 bars
    assert orch._detect_exit(_neutral_bar(), pos) == (100.0, "hold cap")


def test_hold_cap_disabled_by_default(orchestrator):
    """Config ships hold_cap_bars=0 everywhere — never fires."""
    orch = orchestrator
    pos = _make_pos()
    orch.risk.open_positions["t1"] = pos
    orch._position_entry_bar["t1"] = 10
    orch._bar_counters["GS"] = 510                      # held 500 bars
    assert orch._detect_exit(_neutral_bar(), pos) == (None, None)


def test_stop_takes_precedence_over_hold_cap(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    orch = orchestrator
    pos = _make_pos()                                   # short, stop 101.004
    orch.risk.open_positions["t1"] = pos
    orch._position_entry_bar["t1"] = 10
    orch._bar_counters["GS"] = 20                       # way past the cap

    bar = make_bar("GS", "2026-01-13", "10:20", o=100.0, h=101.5, l=100.0, c=101.4)
    exit_p, reason = orch._detect_exit(bar, pos)
    assert reason == "stopped"                          # not "hold cap"

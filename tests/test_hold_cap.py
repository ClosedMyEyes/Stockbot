"""
P1-3: hold-time cap wired into live exit detection, with owner-confirmed
semantics: once bars_held reaches hold_cap_bars, cut the trade only if its
unrealized R (at bar close) is at or below hold_cap_exit_r; winners past the
cap keep running. Without an exit_r threshold, the cap exits unconditionally.
All hold_cap_bars remain 0 in config (disabled).
"""

from conftest import make_bar, pkg
from test_ibkr_stops import _make_pos

# gap_fill_small config ships hold_cap_exit_r = -0.3; _make_pos gives a short
# with entry 100.0, stop 101.004 → per-share risk 1.004. Unrealized R at a
# flat close C is (C − 100) × −1 / 1.004.


def _flat_bar(px):
    return make_bar("GS", "2026-01-13", "10:20", o=px)


def _setup(orch, entry_bar=10, now_bar=15):
    pos = _make_pos()
    orch.risk.open_positions["t1"] = pos
    orch._position_entry_bar["t1"] = entry_bar
    orch._bar_counters["GS"] = now_bar
    return pos


def test_cap_cuts_laggard(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    pos = _setup(orchestrator)                       # held exactly 5 bars
    # close 100.5 → unrealized ≈ −0.498R ≤ −0.3 → cut
    assert orchestrator._detect_exit(_flat_bar(100.5), pos) == (100.5, "hold cap")


def test_cap_lets_winner_run(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    pos = _setup(orchestrator)
    # close 99.5 → unrealized ≈ +0.498R > −0.3 → keep running
    assert orchestrator._detect_exit(_flat_bar(99.5), pos) == (None, None)


def test_cap_before_n_bars_never_fires(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    pos = _setup(orchestrator, now_bar=14)           # held only 4 bars
    assert orchestrator._detect_exit(_flat_bar(100.5), pos) == (None, None)


def test_cap_unconditional_without_exit_r(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_exit_r", None)
    pos = _setup(orchestrator)
    # winner or not — no threshold means cut at the cap
    assert orchestrator._detect_exit(_flat_bar(99.5), pos) == (99.5, "hold cap")


def test_cap_disabled_by_default(orchestrator):
    """Config ships hold_cap_bars=0 everywhere — never fires."""
    pos = _setup(orchestrator, now_bar=510)          # held 500 bars
    assert orchestrator._detect_exit(_flat_bar(100.5), pos) == (None, None)


def test_stop_takes_precedence_over_hold_cap(orchestrator, monkeypatch):
    config = pkg("config")
    monkeypatch.setitem(config.STRATEGY_PARAMS["gap_fill_small"],
                        "hold_cap_bars", 5)
    pos = _setup(orchestrator, now_bar=20)           # way past the cap
    bar = make_bar("GS", "2026-01-13", "10:20", o=100.0,
                   h=101.5, l=100.0, c=101.4)        # bar tags the stop
    exit_p, reason = orchestrator._detect_exit(bar, pos)
    assert reason == "stopped"                       # not "hold cap"

"""
P2-6 leftovers: same-symbol conflicts reach conflict_log.csv, and the
pre-fetched regime payload is used instead of re-downloading at 09:30.
"""

from conftest import make_bar, pkg
from test_ibkr_stops import _make_pos
from test_threading import _make_signal


def test_same_symbol_conflict_written_to_conflict_log(orchestrator, monkeypatch):
    llmod = pkg("logging_layer")
    models = pkg("models")
    rows = []
    monkeypatch.setattr(llmod, "log_conflict", lambda **kw: rows.append(kw))

    orch = orchestrator
    orch.risk.open_positions["t1"] = _make_pos()        # gap_fill_small holds GS

    assert orch.risk.approve(_make_signal(models, "GS")) is None
    assert rows == [{
        "session": "2026-01-13", "bar_time": "10:00",
        "winner_strategy": "gap_fill_small", "loser_strategy": "gap_fill_small",
        "symbol": "GS", "conflict_type": "same_symbol",
    }]


def test_prefetched_regime_payload_used(orchestrator, monkeypatch):
    main = pkg("main")
    orch = orchestrator
    downloads = []
    monkeypatch.setattr(
        main, "load_for_session",
        lambda: downloads.append(1) or {"scales": {}, "date": "2026-01-14"})

    orch._regime_payload = {"scales": {"orb_short": 1.5}, "date": "2026-01-13"}
    orch.on_bar(make_bar("GS", "2026-01-13", "09:31", o=100.0))

    assert downloads == []                              # cache hit, no download
    assert orch.risk._regime_scales.get("orb_short") == 1.5

    # Next session: cached payload is stale → downloads once
    orch.on_bar(make_bar("GS", "2026-01-14", "09:31", o=100.0))
    assert downloads == [1]

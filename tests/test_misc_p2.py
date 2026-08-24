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


def test_position_meta_persisted_to_state(orchestrator):
    """P2-5: signal meta now actually reaches state.json (OpenPosition has no
    .meta attribute — it must be passed explicitly)."""
    orch = orchestrator
    pos = _make_pos()
    orch.state.on_position_open(pos, "2026-01-13", meta={"gap_dir": -1, "gap_pct": 3.1})
    snap = orch.state.saved_positions["t1"]
    assert snap["meta"] == {"gap_dir": -1, "gap_pct": 3.1}


def test_warmup_seeded_bars_deduped(orchestrator):
    """Intraday start: a live re-emit of a bar already fed during warm-up is
    dropped, and the seed survives the session rollover (same date)."""
    orch = orchestrator
    orch._seen_bars.add(("GS", "2026-01-13", "09:30"))   # as warm_up seeds it

    orch.on_bar(make_bar("GS", "2026-01-13", "09:30", o=100.0))   # duplicate
    assert orch._bar_counters["GS"] == 0                 # dropped entirely

    orch.on_bar(make_bar("GS", "2026-01-13", "09:31", o=100.0))   # fresh bar
    assert orch._bar_counters["GS"] == 1
    # rollover kept the same-session seed
    assert ("GS", "2026-01-13", "09:30") in orch._seen_bars

    orch.on_bar(make_bar("GS", "2026-01-13", "09:30", o=100.0))   # dup again
    assert orch._bar_counters["GS"] == 1                 # still dropped


def test_base_strategy_public_surface(orchestrator):
    """P2-3: orchestrator-facing surface on BaseStrategy."""
    strat = next(s for s in orchestrator.strategies["GS"]
                 if s.strategy_id == "gap_fill_small")
    assert strat.in_trade is False
    strat.mark_in_trade()
    assert strat.in_trade is True
    strat.clear_in_trade()
    assert strat.in_trade is False

    strat._state = 1
    assert strat.state_key == 1
    strat.force_reset(None)                              # no ctx → zero state
    assert strat.state_key == 0

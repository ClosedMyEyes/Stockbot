"""
EOD close timing (owner-approved 2026-08-23): the 15:59 bar never streams
live, so the LAST real bar (15:58, completing ~15:59:00 ET) triggers the
per-symbol EOD close, and the safety backstop fires at 15:59:30 ET — both
inside the session, when market orders still fill same-day. (The old design
closed at 16:01 ET; IBKR treats post-close market orders as next-day orders.)
"""

import datetime

from conftest import make_bar, pkg
from test_ibkr_stops import _make_pos

TODAY = "2026-01-13"


def test_eod_triggers_on_last_real_bar(orchestrator, monkeypatch):
    llmod = pkg("logging_layer")
    rows = []
    monkeypatch.setattr(llmod, "log_trade", lambda **kw: rows.append(kw))

    orch = orchestrator
    orch.on_bar(make_bar("GS", TODAY, "09:30", o=100.0))   # session opens
    pos = _make_pos()
    orch.risk.open_positions["t1"] = pos

    orch.on_bar(make_bar("GS", TODAY, "15:58", o=100.0))   # last real live bar

    assert "t1" not in orch.risk.open_positions
    assert rows[0]["exit_reason"] == "EOD close"
    assert rows[0]["exit_price"] == 100.0                  # the 15:58 close
    assert orch._session_open is False


def test_late_1559_bar_does_not_double_book(orchestrator):
    """If a 15:59 bar ever does arrive after the 15:58-triggered close
    (paper replays), the per-symbol EOD bookkeeping must not run twice —
    prior_close would otherwise be stored twice for one session."""
    orch = orchestrator
    orch.on_bar(make_bar("GS", TODAY, "09:30", o=100.0))
    orch.on_bar(make_bar("GS", TODAY, "15:58", o=100.5))
    assert len(orch.ctx_builder._daily_close["GS"]) == 1

    orch.on_bar(make_bar("GS", TODAY, "15:59", o=100.6))
    assert len(orch.ctx_builder._daily_close["GS"]) == 1   # still once


def test_timer_config_is_inside_the_session():
    config = pkg("config")
    safety = datetime.time.fromisoformat(config.EOD_SAFETY_AT)
    pexit  = datetime.time.fromisoformat(config.PROCESS_EXIT_AT)
    assert safety < datetime.time(15, 0)      # before the 15:00 CT close
    assert safety < pexit                     # backstop before process exit
    assert config.EOD_TRIGGER_BAR < config.EOD_BAR

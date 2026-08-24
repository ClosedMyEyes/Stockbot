"""
Group B (keepUpToDate) bar handler: on hasNewBar=True, ib_insync has just
appended the newly STARTED bar, so bars[-1] is partial. The handler must emit
bars[-2] — the bar that just completed — and must skip the first append after
an initial snapshot that ends on a previous session.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from conftest import pkg


def _ku_bar(dt, o, h, l, c, v):
    """Mimic an ib_insync BarData for 1-min historical bars."""
    return SimpleNamespace(date=dt, open=o, high=h, low=l, close=c, volume=v)


@pytest.fixture
def feed_and_captured(monkeypatch):
    feed_mod = pkg("data.feed")
    monkeypatch.setattr(feed_mod, "_IB_AVAILABLE", False)  # no IB() instance needed
    feed = feed_mod.IBKRFeed(symbols=["TEST"])
    captured = []
    feed.on_bar = captured.append
    return feed, captured


def test_emits_completed_bar_not_partial(feed_and_captured):
    feed, captured = feed_and_captured
    handler = feed._make_ku_handler("TEST")

    completed = _ku_bar(datetime(2026, 1, 6, 9, 30), o=100.0, h=101.5, l=99.5, c=101.0, v=50_000)
    partial   = _ku_bar(datetime(2026, 1, 6, 9, 31), o=101.0, h=101.1, l=101.0, c=101.05, v=800)

    handler([completed, partial], True)

    assert len(captured) == 1
    bar = captured[0]
    assert bar.time   == "09:30"          # the completed bar, not the new one
    assert bar.close  == 101.0
    assert bar.high   == 101.5
    assert bar.low    == 99.5
    assert bar.volume == 50_000


def test_no_emit_on_in_place_update(feed_and_captured):
    feed, captured = feed_and_captured
    handler = feed._make_ku_handler("TEST")
    bars = [
        _ku_bar(datetime(2026, 1, 6, 9, 30), 100, 101, 99, 100.5, 1000),
        _ku_bar(datetime(2026, 1, 6, 9, 31), 100.5, 100.6, 100.4, 100.5, 200),
    ]
    handler(bars, False)  # hasNewBar=False → current bar updated in place
    assert captured == []


def test_skips_first_append_after_prior_session_snapshot(feed_and_captured):
    """Pre-market start: snapshot ends on yesterday's last bar. The first live
    append must NOT re-emit yesterday's 15:59 bar as if it were live."""
    feed, captured = feed_and_captured
    handler = feed._make_ku_handler("TEST")
    yesterday_last = _ku_bar(datetime(2026, 1, 5, 15, 59), 100, 100.5, 99.5, 100.0, 30_000)
    today_partial  = _ku_bar(datetime(2026, 1, 6, 9, 30), 102, 102.1, 102.0, 102.05, 500)

    handler([yesterday_last, today_partial], True)
    assert captured == []


def test_no_emit_with_single_bar(feed_and_captured):
    feed, captured = feed_and_captured
    handler = feed._make_ku_handler("TEST")
    handler([_ku_bar(datetime(2026, 1, 6, 9, 30), 100, 100, 100, 100, 100)], True)
    assert captured == []

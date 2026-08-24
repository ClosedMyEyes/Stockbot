"""
P1-2 locking: concurrent signal handling, software closes, EOD force-closes,
and fill verification hammering the same position state must never produce a
double close, a lost position, a double clear, or a crashed thread.

Each trade_id must end in EXACTLY one terminal action: either one closing
exit (send_exit) or one failed-fill clear (cancel_order), never both.
"""

import threading
import time
import traceback

import pytest

from conftest import pkg

SYMS = ["GS", "MS", "GILD", "AMGN", "NFLX", "AMZN"]   # gap_fill_small universe


class RecordingExecutor:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []
        self.exits = []
        self.cancels = []

    def send_entry(self, pos):
        with self.lock:
            self.entries.append(pos.trade_id)
        return True

    def send_exit(self, pos, exit_price, reason):
        with self.lock:
            self.exits.append(pos.trade_id)
        return True

    def cancel_order(self, pos):
        with self.lock:
            self.cancels.append(pos.trade_id)
        return True


def _make_signal(models, sym):
    return models.Signal(
        strategy_id="gap_fill_small", symbol=sym, direction="short",
        entry_price=100.0, stop=101.0, tp=97.0, R=1.0,
        bar_time="10:00", session_date="2026-01-13", meta={"gap_dir": -1},
    )


def test_concurrent_close_paths_stress(orchestrator, monkeypatch):
    main   = pkg("main")
    models = pkg("models")
    orch   = orchestrator
    ex     = RecordingExecutor()
    orch.executor = ex

    # Fill-verification results: mostly partial fills (adjust), every 7th a
    # complete miss (clear). Only the verifier thread calls this.
    calls = {"n": 0}

    def fake_query(symbol, direction):
        calls["n"] += 1
        if calls["n"] % 7 == 0:
            return 0
        return 33 if calls["n"] % 2 else 50

    monkeypatch.setattr(orch, "_query_actual_shares", fake_query)

    strats = {s: next(x for x in orch.strategies[s]
                      if x.strategy_id == "gap_fill_small") for s in SYMS}

    errors = []
    stop = threading.Event()

    def guarded(fn):
        def wrapped():
            try:
                fn()
            except Exception:
                errors.append(traceback.format_exc())
                stop.set()
        return wrapped

    def snapshot():
        with main._position_lock:
            return list(orch.risk.open_positions.items())

    def producer():
        for i in range(1000):
            sym = SYMS[i % len(SYMS)]
            orch._handle_signal(_make_signal(models, sym), strats[sym], None)
        stop.set()

    # Exit prices at/above breakeven for these shorts — losing exits would
    # trip the per-strategy DD halt and (correctly) reject most signals,
    # which would starve the stress of concurrency.
    def closer():
        while not stop.is_set():
            for tid, pos in snapshot():
                orch._do_close(tid, pos, 100.0, "stopped", "10:01")
            time.sleep(0.001)

    def eod_timer():
        while not stop.is_set():
            for tid, pos in snapshot():
                orch._force_close(tid, pos, 99.9, "EOD safety close")
            time.sleep(0.0015)

    def verifier():
        while not stop.is_set():
            for tid, pos in snapshot():
                orch._verify_fill_async(tid, pos, pos.shares or 67)
            time.sleep(0.002)

    threads = [threading.Thread(target=guarded(fn), daemon=True)
               for fn in (producer, closer, eod_timer, verifier)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "stress thread failed to finish"

    # Drain anything still open after the producer stopped.
    for tid, pos in snapshot():
        orch._do_close(tid, pos, 100.0, "stopped", "10:02")

    assert errors == [], "thread raised:\n" + "\n".join(errors)

    with main._position_lock:
        assert orch.risk.open_positions == {}      # no lost/stuck positions
        assert orch._pending_verify == {}

    entries, exits, cancels = ex.entries, ex.exits, ex.cancels
    assert len(entries) > 100                      # the stress actually ran
    assert len(set(exits)) == len(exits)           # no double close
    assert len(set(cancels)) == len(cancels)       # no double clear
    assert set(exits) & set(cancels) == set()      # one terminal KIND per trade
    assert set(exits) | set(cancels) == set(entries)  # every entry terminated


def test_partial_fill_adjust_recomputes_r_dollars(orchestrator, monkeypatch):
    models = pkg("models")
    orch = orchestrator
    pos = models.OpenPosition(
        trade_id="t1", strategy_id="gap_fill_small", symbol="GS",
        direction="short", entry_price=100.0, stop=101.0, tp=97.0,
        R_dollars=67.0, shares=67, entry_time="09:35",
        session_date="2026-01-13",
    )
    orch.risk.open_positions["t1"] = pos
    orch.state.on_position_open(pos, "2026-01-13")
    monkeypatch.setattr(orch, "_query_actual_shares", lambda s, d: 50)

    orch._verify_fill_async("t1", pos, 67)

    assert pos.shares == 50
    assert pos.R_dollars == pytest.approx(50.0)    # 50 sh × $1.00/sh risk
    snap = orch.state.saved_positions["t1"]
    assert snap["shares"] == 50
    assert snap["R_dollars"] == pytest.approx(50.0)


def test_adjust_skipped_if_position_already_closed(orchestrator, monkeypatch):
    models = pkg("models")
    orch = orchestrator
    pos = models.OpenPosition(
        trade_id="t1", strategy_id="gap_fill_small", symbol="GS",
        direction="short", entry_price=100.0, stop=101.0, tp=97.0,
        R_dollars=67.0, shares=67, entry_time="09:35",
        session_date="2026-01-13",
    )
    # NOT registered in risk.open_positions — closed while verify was in flight
    monkeypatch.setattr(orch, "_query_actual_shares", lambda s, d: 50)

    orch._verify_fill_async("t1", pos, 67)

    assert pos.shares == 67                        # untouched
    assert pos.R_dollars == 67.0

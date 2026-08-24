"""
_do_close ordering: the closing order must reach the executor even if the
strategy's on_exit callback raises, and exactly once per trade_id.
"""

from conftest import pkg


class _RecordingExecutor:
    def __init__(self):
        self.exits = []

    def send_entry(self, pos):
        return True

    def send_exit(self, pos, exit_price, reason):
        self.exits.append((pos.trade_id, exit_price, reason))
        return True


def _open_position(orch, symbol="GS", strategy_id="gap_fill_small"):
    models = pkg("models")
    pos = models.OpenPosition(
        trade_id="t1", strategy_id=strategy_id, symbol=symbol,
        direction="short", entry_price=100.0, stop=101.0, tp=97.0,
        R_dollars=67.0, shares=67, entry_time="09:35",
        session_date="2026-01-13",
    )
    orch.risk.open_positions[pos.trade_id] = pos
    strat = next(s for s in orch.strategies[symbol]
                 if s.strategy_id == strategy_id)
    strat._in_trade = True
    return pos, strat


def test_exit_order_sent_even_if_on_exit_raises(orchestrator, monkeypatch):
    orch = orchestrator
    orch.executor = _RecordingExecutor()
    pos, strat = _open_position(orch)

    def _boom(result_r, reason):
        raise RuntimeError("strategy blew up")

    monkeypatch.setattr(strat, "on_exit", _boom)

    orch._do_close("t1", pos, exit_price=99.0, reason="TP hit",
                   exit_time_str="10:30")

    assert orch.executor.exits == [("t1", 99.0, "TP hit")]
    assert "t1" not in orch.risk.open_positions


def test_double_close_sends_only_one_exit(orchestrator):
    orch = orchestrator
    orch.executor = _RecordingExecutor()
    pos, _ = _open_position(orch)

    orch._do_close("t1", pos, 99.0, "TP hit", "10:30")
    orch._do_close("t1", pos, 99.0, "TP hit", "10:30")  # e.g. EOD timer race

    assert len(orch.executor.exits) == 1

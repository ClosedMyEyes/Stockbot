"""
P0-1 broker-side protective stops (IBKRExecution, mocked ib — no live TWS).

Acceptance (from HANDOFF):
  (a) entry places parent market order + attached GTC stop
  (b) software exit cancels the stop exactly once, then closes
  (c) EOD / force-close path cancels the stop too (same send_exit path,
      exercised through the orchestrator's _do_close)
  (d) reconcile re-associates or re-places a missing stop, and cancels
      orphaned stops for departed trades
  (e) failed fill cancels both the entry order and the child stop
  (f) a broker-side stop fill closes the position through the orchestrator
      without sending a duplicate closing order
"""

from types import SimpleNamespace

import pytest

from conftest import pkg


# ── fake ib_insync surface ────────────────────────────────────────────────────

class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def emit(self, *args):
        for h in list(self.handlers):
            h(*args)


class FakeTrade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        self.orderStatus = SimpleNamespace(status="Submitted", avgFillPrice=0.0)
        self.filledEvent = FakeEvent()


class FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)  # tests run without an asyncio loop → execute inline


class FakeIB:
    def __init__(self):
        self._next_id = 100
        self.placed = []      # FakeTrade, in placement order
        self.cancelled = []   # orders passed to cancelOrder
        self.open = []        # trades returned by openTrades()
        self.loop = FakeLoop()

    def placeOrder(self, contract, order):
        if not getattr(order, "orderId", 0):
            order.orderId = self._next_id
            self._next_id += 1
        trade = FakeTrade(contract, order)
        self.placed.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def openTrades(self):
        return list(self.open)


def _make_pos(trade_id="t1", symbol="GS", direction="short",
              entry=100.0, stop=101.004, tp=97.0, shares=67):
    models = pkg("models")
    return models.OpenPosition(
        trade_id=trade_id, strategy_id="gap_fill_small", symbol=symbol,
        direction=direction, entry_price=entry, stop=stop, tp=tp,
        R_dollars=67.0, shares=shares, entry_time="09:35",
        session_date="2026-01-13",
    )


@pytest.fixture
def ex():
    execution = pkg("execution")
    ib = FakeIB()
    return execution.IBKRExecution(ib), ib


# ── (a) entry ────────────────────────────────────────────────────────────────

def test_entry_places_parent_market_plus_gtc_stop(ex):
    executor, ib = ex
    pos = _make_pos()  # short: entry SELL, protection BUY

    assert executor.send_entry(pos) is True
    assert len(ib.placed) == 2
    parent, stop = ib.placed

    assert parent.order.orderType == "MKT"
    assert parent.order.action == "SELL"
    assert parent.order.transmit is False          # transmitted with the child
    assert parent.order.orderRef == "stockbot:t1"

    assert stop.order.orderType == "STP"
    assert stop.order.action == "BUY"              # reverse of entry
    assert stop.order.auxPrice == 101.0            # rounded to cents
    assert stop.order.tif == "GTC"
    assert stop.order.parentId == parent.order.orderId
    assert stop.order.transmit is True
    assert pos.stop_order_id == stop.order.orderId


def test_entry_long_uses_sell_stop(ex):
    executor, ib = ex
    pos = _make_pos(direction="long", stop=98.996)
    executor.send_entry(pos)
    parent, stop = ib.placed
    assert parent.order.action == "BUY"
    assert stop.order.action == "SELL"
    assert stop.order.auxPrice == 99.0


# ── (b) software exit ────────────────────────────────────────────────────────

def test_exit_cancels_stop_exactly_once_then_closes(ex):
    executor, ib = ex
    pos = _make_pos()
    executor.send_entry(pos)
    stop_order = ib.placed[1].order

    executor.send_exit(pos, 99.0, "TP hit")

    assert ib.cancelled == [stop_order]
    close = ib.placed[-1]
    assert close.order.orderType == "MKT"
    assert close.order.action == "BUY"             # closes the short

    executor.send_exit(pos, 99.0, "TP hit")        # e.g. redundant path
    assert ib.cancelled == [stop_order]            # still exactly one cancel


def test_exit_skips_close_if_stop_already_filled(ex):
    executor, ib = ex
    pos = _make_pos()
    executor.send_entry(pos)
    ib.placed[1].orderStatus.status = "Filled"     # stop beat us to it

    n_before = len(ib.placed)
    executor.send_exit(pos, 99.0, "stopped")

    assert len(ib.placed) == n_before              # no duplicate close order
    assert ib.cancelled == []                      # nothing to cancel


# ── (c) EOD / force-close path ───────────────────────────────────────────────

def test_force_close_path_cancels_stop(orchestrator, ex):
    executor, ib = ex
    orch = orchestrator
    orch.executor = executor
    pos = _make_pos()
    executor.send_entry(pos)
    orch.risk.open_positions[pos.trade_id] = pos
    stop_order = ib.placed[1].order

    orch._force_close(pos.trade_id, pos, 100.5, "EOD safety close")

    assert stop_order in ib.cancelled
    assert ib.placed[-1].order.orderType == "MKT"
    assert pos.trade_id not in orch.risk.open_positions


# ── (d) reconcile ────────────────────────────────────────────────────────────

def test_reconcile_replaces_missing_stop(ex):
    executor, ib = ex
    pos = _make_pos()                              # no stop at TWS, no id

    executor.reconcile_stops([pos])

    st = ib.placed[-1]
    assert st.order.orderType == "STP"
    assert st.order.tif == "GTC"
    assert st.order.action == "BUY"
    assert st.order.parentId == 0                  # position exists — no parent
    assert st.order.orderRef == "stockbot:t1"
    assert pos.stop_order_id == st.order.orderId
    assert executor._stop_trades["t1"] is st


def test_reconcile_reassociates_existing_stop(ex):
    executor, ib = ex
    resting = FakeTrade(
        SimpleNamespace(symbol="GS"),
        SimpleNamespace(orderId=555, orderType="STP",
                        orderRef="stockbot:t1", parentId=0),
    )
    ib.open = [resting]
    pos = _make_pos()
    pos.stop_order_id = 555

    executor.reconcile_stops([pos])

    assert ib.placed == []                         # nothing re-placed
    assert executor._stop_trades["t1"] is resting
    assert len(resting.filledEvent.handlers) == 1  # fill routing re-attached


def test_reconcile_cancels_orphaned_stop_of_departed_trade(ex):
    executor, ib = ex
    orphan = FakeTrade(
        SimpleNamespace(symbol="MS"),
        SimpleNamespace(orderId=777, orderType="STP",
                        orderRef="stockbot:gone", parentId=0),
    )
    ib.open = [orphan]

    executor.reconcile_stops([])                   # no open positions

    assert orphan.order in ib.cancelled


# ── (e) failed fill ──────────────────────────────────────────────────────────

def test_failed_fill_cancels_entry_and_child_stop(ex):
    executor, ib = ex
    pos = _make_pos()
    executor.send_entry(pos)
    parent_order, stop_order = ib.placed[0].order, ib.placed[1].order

    executor.cancel_order(pos)

    assert stop_order in ib.cancelled
    assert parent_order in ib.cancelled


def test_clear_failed_fill_calls_executor_cancel(orchestrator, monkeypatch):
    orch = orchestrator
    cancelled = []
    orch.executor = SimpleNamespace(cancel_order=lambda pos: cancelled.append(pos.trade_id))
    pos = _make_pos()
    orch.risk.open_positions[pos.trade_id] = pos

    orch._clear_failed_fill(pos.trade_id, pos)

    assert cancelled == ["t1"]
    assert "t1" not in orch.risk.open_positions


# ── (f) broker-side stop fill ────────────────────────────────────────────────

def test_broker_stop_fill_closes_position_without_duplicate_order(orchestrator, ex):
    executor, ib = ex
    orch = orchestrator
    orch.executor = executor
    executor.on_stop_filled = orch._on_broker_stop_filled

    pos = _make_pos()
    executor.send_entry(pos)
    orch.risk.open_positions[pos.trade_id] = pos
    strat = next(s for s in orch.strategies["GS"]
                 if s.strategy_id == "gap_fill_small")
    strat._in_trade = True

    stop_trade = executor._stop_trades["t1"]
    stop_trade.orderStatus.status = "Filled"
    stop_trade.orderStatus.avgFillPrice = 101.2
    n_before = len(ib.placed)

    stop_trade.filledEvent.emit(stop_trade)        # broker reports the fill

    assert "t1" not in orch.risk.open_positions    # exit recorded
    assert len(ib.placed) == n_before              # no duplicate close order
    assert ib.cancelled == []                      # stop is gone, not cancelled
    assert strat._in_trade is False

    # a late software exit for the same trade is a clean no-op
    orch._do_close("t1", pos, 101.0, "stopped", "10:31")
    assert len(ib.placed) == n_before

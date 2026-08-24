"""
Per-symbol lazy session reset: each symbol's strategies must be reset with
THAT symbol's fresh SessionContext when its own first bar of the session
arrives — not en masse with stale contexts when the global rollover fires.

Regression targets (pre-fix behavior):
  - gap fill strategies captured prior_close from the PREVIOUS session's
    context for every symbol except the rollover trigger
  - orb_short's weekday filters evaluated yesterday's session_date
    (skip_monday made it sit out Tuesdays and trade Fridays)
"""

from conftest import make_bar

# 2026-01-05 is a Monday; 2026-01-13 is a Tuesday.
HISTORY_DAYS = ["2026-01-05", "2026-01-06", "2026-01-07",
                "2026-01-08", "2026-01-09", "2026-01-12"]
TEST_DAY = "2026-01-13"   # Tuesday — orb_short must be ACTIVE (skips Mon/Fri)

TRIGGER = "AAPL"   # first bar of TEST_DAY → fires the global rollover
TARGET  = "AMZN"   # in orb_short AND the gap fill universes

AMZN_CLOSE = 333.0


def _feed_history(orch):
    """Six flat sessions so prior_close exists and vol_regime_ratio == 1.0."""
    for day in HISTORY_DAYS:
        for sym, px in ((TRIGGER, 200.0), (TARGET, AMZN_CLOSE)):
            # h/l straddle the close so every daily range is 2.0 (ratio = 1.0)
            orch.on_bar(make_bar(sym, day, "09:30", o=px, h=px + 1, l=px - 1, c=px))
            orch.on_bar(make_bar(sym, day, "15:59", o=px, h=px + 1, l=px - 1, c=px))


def _strat(orch, symbol, strategy_id):
    return next(s for s in orch.strategies[symbol] if s.strategy_id == strategy_id)


def test_reset_uses_own_symbols_fresh_context(orchestrator):
    orch = orchestrator
    _feed_history(orch)

    # TEST_DAY: AAPL's bar triggers the global rollover first…
    orch.on_bar(make_bar(TRIGGER, TEST_DAY, "09:30", o=200.0, h=201, l=199, c=200.0))
    # …then AMZN's own first bar arrives and must reset AMZN strategies
    # with AMZN's fresh context.
    orch.on_bar(make_bar(TARGET, TEST_DAY, "09:30", o=AMZN_CLOSE, h=AMZN_CLOSE + 1,
                         l=AMZN_CLOSE - 1, c=AMZN_CLOSE))

    gap = _strat(orch, TARGET, "gap_fill_small")
    # Fresh context → prior_close is yesterday's (Jan 12) close, not stale/None.
    assert gap._prior_close == AMZN_CLOSE

    orb = _strat(orch, TARGET, "orb_short")
    # Tuesday with vol_regime_ratio == 1.0 → active. Under the stale-context
    # bug, session_date evaluated as Monday and skip_monday knocked it out.
    assert orb._session_active is True


def test_reset_happens_once_per_symbol_per_session(orchestrator):
    orch = orchestrator
    _feed_history(orch)

    orch.on_bar(make_bar(TRIGGER, TEST_DAY, "09:30", o=200.0))
    orch.on_bar(make_bar(TARGET, TEST_DAY, "09:30", o=AMZN_CLOSE,
                         h=AMZN_CLOSE + 1, l=AMZN_CLOSE - 1, c=AMZN_CLOSE))

    gap = _strat(orch, TARGET, "gap_fill_small")
    gap._prior_close = "sentinel"   # must survive subsequent same-day bars
    orch.on_bar(make_bar(TARGET, TEST_DAY, "09:31", o=AMZN_CLOSE))
    assert gap._prior_close == "sentinel"


def test_rollover_trigger_symbol_also_fresh(orchestrator):
    """The symbol that triggers the rollover was always fine — keep it that way."""
    orch = orchestrator
    _feed_history(orch)

    orch.on_bar(make_bar(TRIGGER, TEST_DAY, "09:30", o=200.0, h=201, l=199, c=200.0))
    gap = _strat(orch, TRIGGER, "gap_fill_large")
    assert gap._prior_close == 200.0

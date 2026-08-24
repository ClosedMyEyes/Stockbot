# HANDOFF — Stockbot fix list for Claude Code

Repo: https://github.com/ClosedMyEyes/Stockbot (single `main` branch, ~5,500 LOC Python)

Context: intraday equities bot. 1-min bars from IBKR via ib_insync, 6 strategy
state machines, risk manager, three execution backends (paper / direct IBKR /
SignalStack webhook), atomic state.json persistence with IBKR reconciliation.
The code is more mature than the README suggested (README has been rewritten —
see README.md alongside this file; drop it into the repo root).

General constraints for all work:

- **Do not change strategy logic or risk parameters.** Anything in
  `strategies/`, `STRATEGY_PARAMS`, `STRATEGY_RISK`, `STRATEGY_UNIVERSES`,
  `SCALE_TABLE` is calibrated against backtests — treat as read-only unless a
  task explicitly says otherwise.
- ib_insync runs an asyncio event loop; bar callbacks arrive on that loop's
  thread. Several timers/workers run on other threads. Any new code that
  touches `ib` from a non-loop thread must use `call_soon_threadsafe` (see the
  existing pattern in `execution/__init__.py::IBKRExecution.send_exit`).
- There are currently **zero tests**. Every task below should add tests for the
  code it touches (pytest; mock the `ib` object — no live TWS in CI).
- Machine local time is assumed **Central Time**; bar times are **Eastern**
  (converted in `data/feed.py`). Don't "fix" this asymmetry by changing
  semantics — it's documented in config.py — but see P2-4.

---

## P0 — must fix before live money

### P0-1. Broker-side protective stops (the big one)

**Problem:** All three executors send a market order on entry only. Stops/TPs
are detected in software (`main.py::_detect_exit`) off 1-min bars and exited
with a market order. If the process dies or IBKR disconnects and reconnect
fails (loop gives up at 15:35 CT — `main.py` line ~61), open positions have no
exchange-level protection. Confirmed: no `StopOrder`, `bracketOrder`, `GTC`,
`parentId`, or `ocaGroup` anywhere in the repo.

**Fix (IBKRExecution first, it's the default mode):**

- In `execution/__init__.py::IBKRExecution.send_entry`, place a parent market
  order plus an attached GTC stop order (`StopOrder(reverse_action, shares,
  pos.stop, tif="GTC", parentId=parent.order.orderId)`), transmitted together.
  Optionally a full bracket with the TP as a limit, OCA-grouped, but the
  minimum viable fix is entry + protective stop.
- Track child order ids per trade_id in `self._trades` so `send_exit` /
  software exits **cancel the resting stop before or atomically with** the
  closing market order — otherwise the stop can double-fill after the software
  exit and flip the position.
- On EOD close paths (`_eod_close`, EOD safety timer, `_force_close`), same:
  cancel resting children, then close.
- StateManager: persist child order ids in the position snapshot
  (`state_manager.py::on_position_open`) so reconciliation after a restart can
  re-associate or re-place the protective stop. On reconcile, if a restored
  position has no live stop order at IBKR (`ib.openOrders()`), re-place it and
  log a WARNING.
- `_clear_failed_fill` (main.py): if the entry never filled, also cancel the
  orphaned child stop.
- SignalStackExecution can't express brackets (webhook is market-only) — leave
  it, but log a startup WARNING in that mode that positions are software-
  protected only.
- Keep the existing software exit detection as a redundant layer. When both
  fire, the cancel-before-close ordering above prevents double exits; add a
  guard in `_do_close` so a stop-order fill reported by IBKR while a software
  close is in flight doesn't produce a second `log_trade` row (dedupe on
  trade_id — `risk.close_position` already returns None for unknown ids, verify
  that path is airtight under the lock).

**Acceptance:** unit tests with a mocked `ib` proving (a) entry places parent +
GTC stop, (b) software exit cancels the stop exactly once, (c) EOD safety
timer path cancels stops, (d) reconcile re-places a missing stop, (e) failed
fill cancels the child. Manual test plan: run `--ibkr` against paper TWS, open
a position, `kill -9` the bot, confirm the stop is still resting in TWS.

### P0-2. Missing dependency: yfinance

**Problem:** `regime.py` does `import yfinance as yf` at module level and is
imported by `main.py` — fresh install with `requirements.txt` crashes at
startup. `requirements.txt` currently lists only `ib_insync`, `pandas`,
`numpy`.

**Fix:** add `yfinance` (pin a floor, e.g. `yfinance>=0.2.40`). Also make
`_on_new_session`'s `load_for_session()` call failure-tolerant: if the yfinance
download fails (rate limit, no internet), log an ERROR and fall back to scale
factor 1.0 for all strategies rather than crashing the session open. Check
whether `regime.py::load_for_session` already handles this; if it caches to
`regime.json`, fall back to a same-day cached file first, then 1.0.

**Acceptance:** `pip install -r requirements.txt && python -c "import orchestrator.main"`
succeeds in a clean venv; a test that mocks yfinance raising confirms the
session still opens with neutral scales.

### P0-3. Dashboard is dead code (broken filename, never imported)

**Problem:** the file is `dashboard/dashboard__init__.py` — not `__init__.py` —
so the package doesn't exist, and nothing in `main.py` imports or starts it.
The old README's claim of an auto-starting dashboard on :8050 is false.

**Fix:** rename to `dashboard/__init__.py` (use `git mv`), read what it
exposes, and wire its start into `Orchestrator.run()` (daemon thread, after
`subscribe_bars()`), guarded by a `config.DASHBOARD_ENABLED = True` flag and
try/except so a dashboard crash can never take down trading. If the module
turns out to be stale/incompatible with current models, prefer disabling it
cleanly (flag default False + log) over a large rewrite — flag that decision
in the PR description.

**Acceptance:** with the flag on, the process serves on :8050 and trading
continues if the dashboard thread raises; with the flag off, no import cost.

---

## P1 — correctness of records and recovery

### P1-1. Exit fills logged at trigger price, not actual fill

**Problem:** `main.py::_detect_exit` returns the stop/TP level as the exit
price and `_do_close` logs it. Real market orders fill ≥1 bar later with
slippage, so `trade_log.csv` `result_R` is systematically optimistic — which
poisons the live-vs-backtest comparison the project explicitly exists to do.
Related: "ambiguous (SL+TP same bar)" is scratched at entry price (0R), a
backtest convention that is fiction live.

**Fix:** in IBKRExecution, subscribe to fill events (ib_insync `Trade.fillEvent`
or `ib.execDetailsEvent`) and route actual avg fill price + time back to the
orchestrator. Log two prices per trade: `exit_trigger_price` (current value,
keep for backtest comparability) and `exit_fill_price` (new column; also
`slippage_r`). Compute `result_R` from the fill price when available, trigger
price as fallback (paper / SignalStack modes). Entry side too: `entry_fill_price`
column, and reconcile `pos.entry_price` from the entry fill event — this also
supersedes part of the 2-bar polling fill verification for the IBKR mode
(keep the polling as fallback).

**Acceptance:** columns added without renaming existing ones (backtest compare
scripts keep working); test with mocked fill events shows R computed from
fills; paper mode unchanged.

### P1-2. Threading: shared mutable state touched outside the lock

**Problem:** `_position_lock` wraps only `_do_close`. But
`risk.open_positions` is also mutated by `_handle_signal` → `register_position`
(event-loop thread), `_clear_failed_fill` (background verify thread), and
reconciliation (reconnect thread); `_verify_fill_async` mutates `pos.shares`
from a background thread; `_positions_meta` / `_pending_verify` /
`_position_entry_bar` are read/written from multiple threads. Python dict ops
are individually atomic under the GIL, but the compound sequences
(check-then-act in `_run_fill_verification`, iterate-then-close in
`_check_exits` vs. timer-driven `_force_close`) are not.

**Fix:** take `_position_lock` around every compound mutation of
`risk.open_positions` and the three side-dicts: `_handle_signal` registration
block, `_clear_failed_fill`, `_verify_fill_async`'s shares adjustment, the EOD
safety timer's iterate+close, and StateManager.reconcile's restore path. Keep
lock scope tight (no network calls under the lock — note `_do_close` currently
calls `executor.send_exit` under the lock; for SignalStack that's just a queue
put, for IBKR it's `placeOrder`/`call_soon_threadsafe` — move the executor call
outside the lock after state is settled). Verify no lock-order inversion is
introduced (single lock → fine).

**Acceptance:** a stress test that fires concurrent on_bar closes + EOD timer +
fill-verification threads against a mocked executor for 10k iterations without
double-close, lost position, or negative share counts.

### P1-3. Wire the hold-time cap into live exit detection

**Problem:** `config.STRATEGY_PARAMS` now carries `hold_cap_bars` /
`hold_cap_exit_r` for every strategy (all currently 0 = disabled), but
`main.py::_detect_exit` never reads them. Backtests support it; live doesn't.

**Fix:** in `_check_exits`/`_detect_exit`, compute `bars_held` (already done in
`_do_close`) and, when `hold_cap_bars > 0` for the position's strategy and
`bars_held >= hold_cap_bars`, exit at bar close with reason "hold cap". Do NOT
change the param values — leave them 0 so behavior is unchanged until the
owner turns them on.

**Acceptance:** unit test: with a param override of `hold_cap_bars=5`, a
position exits on the 5th bar with the right reason; with 0, never.

### P1-4. Stale docstring/comment sweep

Small but they bit us during review:

- `main.py::_reconnect_loop` docstring says "gives up at 16:15"; code uses
  `_RECONNECT_GIVE_UP = 15:35` (CT). Fix the docstring.
- Top-of-file docstring in `main.py` says EOD timer fires at 16:00:30; config
  is `EOD_SAFETY_AT = "15:01"` CT. Align.
- `state_manager.py` docstring mentions `emergency_close_orphan()` — confirm it
  exists; if not, remove the mention or implement a manual CLI helper.
- Remove the duplicated `group = parser.add_mutually_exclusive_group()` line in
  `main.py::main()`.

---

## P2 — hygiene

### P2-1. Repo hygiene
Add a `.gitignore` (`__pycache__/`, `logs/`, `regime.json`, `.env`, `*.pyc`)
and `git rm -r --cached __pycache__`. Confirm no API keys in history (the
config placeholder `YOUR_SIGNALSTACK_API_KEY` is fine).

### P2-2. Test scaffolding
Set up `pytest` + a `tests/` dir with a fake `ib` fixture and a bar-stream
builder helper. Priority coverage order: `_detect_exit` price logic (gap-
through-stop, ambiguous bar), RiskManager approve/halt/conflict logic,
StateManager save/load/reconcile (ghost, orphan, restore), rate limiter,
fill verification. These are pure-logic and easy wins; the P0/P1 tasks add
their own tests on top.

### P2-3. Encapsulation
Orchestrator reaches into strategy privates (`strat._in_trade`, `strat._state`,
`strat._prev_state_for_timeout`). Add small public methods/properties on
`BaseStrategy` (`in_trade`, `force_reset()`, `state_key`) and migrate call
sites. Mechanical change only — zero behavior change, verify with the new
tests.

### P2-4. Timezone robustness (optional, low risk appetite)
Timers use naive local-time (`datetime.datetime.now()`) with a documented
CT assumption. Safer: compute timer targets in `America/Chicago` (or better,
define config times in ET and convert), so deployment on a differently-zoned
box fails loudly instead of firing timers at wrong hours. If you do this, keep
the config values' meaning identical on the current CT box.

---

## Suggested order

1. P0-2 (5 min), P1-4 (15 min), P2-1 (10 min) — quick wins, separate commit(s)
2. P2-2 test scaffolding — needed by everything else
3. P0-1 broker-side stops — the headline change, its own PR
4. P1-2 locking — before or alongside P0-1 (they touch the same close paths)
5. P1-1 fill-price logging — builds on the fill events from P0-1
6. P0-3 dashboard, P1-3 hold cap, P2-3, P2-4

## Verification before calling it done

- Clean venv install → `python -m orchestrator.main --paper --warmup-days 2`
  runs against paper TWS through a full session without exceptions.
- `--ibkr` against paper TWS: enter a trade, confirm resting GTC stop in TWS,
  kill the process, confirm the stop survives, restart, confirm reconciliation
  restores the position and re-associates the stop.
- `trade_log.csv` gains `entry_fill_price` / `exit_fill_price` / `slippage_r`
  columns and existing columns are unchanged.
- Full pytest suite green.

---

# ADDENDUM — second review (Claude, 2026-08-23)

## STATUS as of 2026-08-23 (later the same day)

Environment: Python 3.13.15 installed (winget, user scope), venv at
`Stockbot\.venv` with all deps. `python -m pytest Stockbot/tests` from
`C:\Users\mark` — 12 tests, all green.

DONE (in working tree, not yet committed):
  - P0-2  requirements.txt: yfinance + pytest added; clean import verified
  - P0-4  Group B partial-bar fix (emit bars[-2], same-date guard) + tests
  - P0-5  lazy per-symbol session reset + tests (main test fails on old code)
  - P1-4  all four stale docstrings/comments + dup argparse line
  - P1-6  ghost-exit lookup (ib.fills()) + _estimate_result_r denominator + tests
  - P2-1  .gitignore added, committed __pycache__ untracked
  - P2-2  test scaffolding (tests/conftest.py: clone-name-agnostic imports,
          bar builder, paper orchestrator fixture with regime stubbed)
  - bonus _do_close hardening: executor.send_exit now fires BEFORE
          strategy on_exit, and on_exit is wrapped in try/except — a raising
          strategy callback can no longer block the closing order + tests
  - P0-1  broker-side GTC protective stops (--ibkr mode): parent MKT +
          attached GTC StopOrder transmitted together (stop price rounded to
          cents, orderRef "stockbot:<trade_id>" for restart recognition);
          send_exit cancels-before-close and skips the close if the stop
          already filled; stop fills route back via on_stop_filled →
          orchestrator records the exit ("stopped (broker stop)", actual avg
          fill price) with no duplicate order; reconcile_stops() after
          startup/reconnect re-associates by orderId/orderRef, re-places a
          missing stop with a WARNING, and cancels orphaned stops of departed
          trades; _clear_failed_fill cancels entry + child; stop_order_id
          persisted in state.json (OpenPosition field + snapshot);
          SignalStack mode logs a startup software-protection-only WARNING.
          11 tests in tests/test_ibkr_stops.py (mocked ib) cover acceptance
          (a)–(e) plus the stop-fill dedupe. NOT yet verified against live
          paper TWS (none installed on this machine) — manual test plan from
          P0-1 still pending. TP remains software-only (no OCA bracket, per
          the minimum-viable scope).
  - P1-2  locking: _position_lock now covers risk.approve + the registration
          block, the fill-verify select-and-remove, the partial-fill shares
          adjustment (re-fetches the live position; skips if closed),
          _clear_failed_fill (pop-under-lock is the ownership test — a
          double terminal on one trade_id is impossible), _do_close's
          side-dict pops, the EOD safety snapshot, _eod_close's snapshot,
          both reconcile call sites, and risk.summary() in _post_market.
          Executor/network calls all stay OUTSIDE the lock. StateManager got
          an internal RLock (its dict mutations + save() serialisation were
          racing across threads); CSV appends now serialise on a module lock
          so concurrent closes can't interleave rows. Stress test
          (tests/test_threading.py): producer + closer + EOD-timer +
          fill-verifier threads hammering the same positions — every entry
          ends in exactly one terminal action, no lost/stuck positions, no
          thread exceptions; stable across repeated runs.
  - P2-6 (partial): R_dollars is recomputed on partial-fill adjustment, so
          state.json / daily-stats P&L now agrees with the risk manager's
          shares-based P&L.

STILL OPEN: P0-3 (dashboard rename/wire), P1-1 (fill-price logging),
P1-3 (hold cap), P1-5 (restart wipe), P2-3/P2-4/P2-5, remaining P2-6 items
(conflict log, market-data lines check, regime pre-fetch, warm-up/live
dedup overlap). Plus: live paper-TWS verification of P0-1 once TWS is
installed.

NOTE: this repo copy of HANDOFF.md is now the living document; the copy in
C:\Users\mark\Downloads is stale and can be deleted.

---

Full read-through of every module confirmed the list above and found the
following additional issues. The two new P0s are data-integrity bugs that
silently make live behavior diverge from the backtests — they matter as much
as P0-1.

## NEW P0-4. Group B bar feed emits partial bars

`data/feed.py::_make_ku_handler` handles `keepUpToDate` subscriptions (symbols
91+ after sorting — 74 of the 164 unique symbols). On `hasNewBar=True`,
ib_insync has just **appended the newly started bar**, so `bars[-1]` is a
partial bar containing only the first seconds of the new minute (open correct;
high/low/close/volume wrong). The completed previous bar is `bars[-2]` and is
never delivered. Consequences: VWAP/ATR built from partial bars, reversal
detection (close vs open) is noise, and stop/TP breaches inside a minute are
invisible — exits trigger late. **Fix:** on `hasNewBar`, emit `bars[-2]`
(guard `len(bars) >= 2`; skip the first emission after the initial snapshot so
historical bars aren't double-fed). Verify one live session against a TWS
chart. Group A (reqRealTimeBars 5s→1min aggregator) is correct.

## NEW P0-5. Stale SessionContext at session rollover

`main.py::_on_new_session` resets all ~300 strategy instances immediately,
using `ctx_builder.get_context(sym, date)` — but `get_context` ignores the
date and returns whatever context is cached, which is the **previous
session's** context for every symbol except the single symbol whose bar
triggered the rollover. Because strategies capture values at reset:

- Gap fills capture `self._prior_close` → gap measured against the
  day-before-yesterday's close → wrong qualification (a stock that rose 3%
  yesterday looks like a 3% gap-up today and gets shorted).
- orb_short's `skip_monday`/`skip_friday`/`skip_months` evaluate
  **yesterday's** `ctx.session_date` → it currently skips Tuesdays and trades
  Fridays.
- `vol_regime_ratio`, `daily_atr`, `first_bar_vol_ratio`,
  `median_session_vol` filters all evaluate one day stale.
- impulse_short is unaffected (reads ctx live, captures nothing at reset).

**Fix:** reset each symbol's strategies lazily — in `on_bar`, when
`bar.date` differs from that strategy's last-reset session, call
`reset_session` with the fresh context (ctx_builder has already built it at
that point in `on_bar`). Do not reset strategies inside `_on_new_session`.
This must land before trusting any live gap-fill or orb results.

## NEW P1-5. `_on_new_session` wipes restored state after a mid-day restart

`_startup_reconcile` restores positions, meta, daily R/P&L and halt status —
then the first live bar triggers `_on_new_session`, which calls
`risk.reset_day()` (zeroes restored P&L, clears halts),
`self._positions_meta.clear()`, and `state.clear_session()` (drops restored
positions from state.json — a second crash would orphan them, and with P0-1
unfixed they'd have no protection at all). Strategy `reset_session` also
clears the `_in_trade` flag reconcile just set (the same-symbol conflict check
is the only thing preventing a duplicate entry). **Fix:** make
`_on_new_session` restart-aware — if state was restored for the same
`session_date`, skip `reset_day`/`clear_session`/meta clear (or re-apply the
restored values after reset). Also: `RiskManager` never calls
`state.on_halt()`, so a halt tripped mid-day is never persisted even before
the wipe.

## NEW P1-6. Ghost-exit ("disconnected exit") logging is doubly broken

`state_manager.py::_query_exit_fill` calls `ib.executions()`, which returns
`Execution` objects that have no `.contract` attribute → AttributeError →
caught → always falls back to entry price (0R). Use `ib.fills()` (Fill has
`.contract` and `.execution`). Separately `_estimate_result_r` divides
per-share P&L by `R_dollars` (whole-trade dollar risk) instead of per-share
risk (`abs(entry - stop)`), understating R by roughly the share count.
Logging-only, but these rows poison the live-vs-backtest comparison.

## NEW P2-5. Position meta never persisted

`state_manager.on_position_open` does `getattr(pos, "meta", {})` but
`OpenPosition` has no `meta` field — state.json always stores `meta: {}`, so
the "restore meta for exit detection" path restores nothing. Currently benign
(`gap_dir` equals the direction-sign fallback in `_detect_exit`), but fix by
passing `signal.meta` into `on_position_open` — P0-1's stop-order-id
persistence will need this plumbing anyway.

## NEW P2-6. Smaller items

- `logging_layer.log_conflict` has zero call sites — conflict_log.csv is never
  written. Either call it from `RiskManager.approve`'s conflict branch or drop it.
- 164 unique symbols vs IBKR's default 100 concurrent market-data lines —
  verify the account's entitlement or part of the universe will error out.
  Also `warm_up` fires 164 historical requests of 20 D × 1-min bars back to
  back; IBKR pacing limits (~60 hist requests / 10 min) will throttle this.
  Measure actual warm-up duration and make sure the bot is started early enough.
- `regime.load_for_session()` runs a synchronous yfinance download on the
  event-loop thread when the 09:30 bar arrives — bars queue behind it.
  Pre-fetch before the open (e.g. at connect time if before 09:30).
- After partial-fill adjustment, `pos.R_dollars` is not recomputed, so
  `_do_close`'s `pnl_dollars = result_r * pos.R_dollars` (fed to state.json /
  daily stats) disagrees with `RiskManager.close_position`'s shares-based P&L.
  Recompute `R_dollars = shares * risk_per_share` on adjustment.
- Live bars that duplicate warm-up bars aren't deduped against the warm-up
  (dedup set only covers `on_bar`) — a bar can be fed to `ctx_builder` twice
  at startup, double-counting one minute of VWAP volume. Minor.

## Corrections to the list above

- **P0-2:** `regime.load_for_session()` is already failure-tolerant — it
  catches every exception and returns 1.0x scales (verified). Only the
  `requirements.txt` addition is needed; no code change.
- **P0-3:** the config flag is `DASHBOARD_ENABLE` (no D). The dashboard module
  itself looks compatible with the current orchestrator — `_build_status()`
  reads `risk.summary()` keys that all exist — so rename + wire-up is likely
  sufficient, no rewrite.
- **P1-3:** impulse_short has no `hold_cap_bars` param — "every strategy"
  overstates it; guard with `.get()`.
- **P1-4:** confirmed all four items, including `emergency_close_orphan()`
  being mentioned in the state_manager docstring but not existing. The
  reconnect log message at main.py:683 also says "past 16:15" — fix along
  with the docstring.
- The README rewrite that shipped alongside this handoff has been corrected
  and installed at the repo root (run commands now use the actual package name
  `Stockbot`, and the state-restore/meta/ghost-exit claims were fixed to match
  the code).

## Revised suggested order

1. P0-2 requirements, P1-4 docstrings, P2-1 gitignore — quick wins
2. P2-2 test scaffolding
3. **P0-4 partial bars** + **P0-5 stale context** — small diffs, huge
   correctness impact, and testable without live TWS (bar-stream fixtures)
4. P0-1 broker-side stops (+ P1-2 locking alongside)
5. P1-5 restart wipe, P1-1 fill-price logging, P1-6 ghost logging
6. P0-3 dashboard, P1-3 hold cap, P2-3/P2-4/P2-5/P2-6

# Trading Bot Orchestrator

Central hub for 6 intraday trading strategy modules. Feeds live 1-min bars from
IBKR into each strategy, routes signals through per-strategy and portfolio-level
risk management with daily regime-based sizing, fires orders via direct IBKR
`placeOrder` (default) or SignalStack webhooks, and persists state so it
survives crashes, disconnects, and restarts.

---

## Architecture

```
IBKR (ib_insync)
    │
    ▼
IBKRFeed.on_bar(bar)         ← bar dedup (symbol+date+time) drops IBKR re-emits
    │                          RealTimeBar timestamps converted UTC → ET (zoneinfo)
    │
    ├── SessionContextBuilder
    │     Rolling per-symbol stats: VWAP, ATR, prior_close, vol_regime,
    │     first_bar_vol_ratio, daily_ATR, median session volume.
    │     Fed to strategies at session start.
    │
    ├── Strategy instances (one per strategy × symbol pair)
    │
    │   SHORT STRATEGIES
    │     orb_short (31 syms)        3-state: OBSERVING → WAIT_TRIGGER → IN_TRADE
    │     impulse_short (102 syms)   7-state: WAIT_BREAK → BUILD_PEAK →
    │                                TRACK_PULLBACK → WAIT_RETEST →
    │                                WAIT_FAILURE → WAIT_FILL → signal
    │
    │   GAP FILL STRATEGIES  (all share same WAIT_ENTRY → IN_TRADE core)
    │     gap_fill_large (39)        gap-down LONG, session_extreme stop
    │     gap_fill_small (39)        gap-up SHORT, gap_open_buffer stop, 1 trade/day
    │     gap_fill_small_multi (62)  gap-up SHORT, unlimited re-entries/day
    │     gap_fill_big (39)          gap-up SHORT, gap_atr_ratio band
    │          │
    │          └── Signal { entry, stop, tp, direction, R, meta }
    │
    │     (164 unique symbols across all universes — see "IBKR subscription
    │      limits" under Known limitations)
    │
    ├── Regime classifier (regime.py)
    │     Once per session: downloads 2y of daily SPY + VIX via yfinance,
    │     computes a 9-cell regime label (BULL/SIDEWAYS/BEAR × LOW/MED/HIGH VIX),
    │     and applies per-strategy sizing scale factors (1.5x BOOST cells from
    │     1998–2026 bootstrap CI analysis). Writes regime.json. Falls back to
    │     1.0x scales on any download/computation failure.
    │
    ├── RiskManager
    │     • per-strategy risk_per_trade (1R) and max_dd — hitting a strategy's
    │       drawdown limit halts THAT strategy for the session, others continue
    │     • portfolio daily loss limit halt ($1,500)
    │     • max simultaneous positions cap (6)
    │     • MAX_POSITIONS_PER_SYMBOL = 1 — same-symbol conflicts resolved by
    │       STRATEGY_PRIORITY order; existing positions never preempted
    │     • regime scales applied to both sizing and max_dd, so the circuit
    │       breaker trips after the same R-count at any size
    │
    ├── StateManager
    │     • atomic state.json (temp file + os.replace) on every position change
    │     • startup/reconnect reconciliation against IBKR positions
    │     • ghost detection (closed while disconnected → ib.fills() lookup
    │       for the actual exit price), orphan alerting (never touched
    │       automatically)
    │
    ├── Executor  (execution/__init__.py — three modes)
    │     • PaperExecution        logs only, instant simulated fills
    │     • IBKRExecution         parent MarketOrder + attached GTC StopOrder
    │                             (broker-side protection, transmitted together);
    │                             software exits cancel the resting stop before
    │                             closing; broker stop fills are routed back via
    │                             on_stop_filled so no duplicate close is sent;
    │                             reconcile_stops() re-places/re-associates stops
    │                             after restarts; thread-safe via
    │                             call_soon_threadsafe off the event loop
    │     • SignalStackExecution  queued webhook worker thread with a shared
    │                             2-calls-per-60s rate limiter (prop-firm cap);
    │                             never blocks the bar callback
    │
    ├── Logger (logging_layer/)
    │     trade_log.csv    signal_log.csv    conflict_log.csv
    │     fill_log.csv (broker fills, join on trade_id)
    │     daily_summary.csv    state.json    regime.json
    │
    └── Dashboard (dashboard/)
          http://localhost:8050 — LIVE / TRADES / HISTORY views, started as a
          daemon thread when config.DASHBOARD_ENABLE is True; binds loopback
          only; a dashboard crash never affects trading
```

---

## Execution modes

| Flag             | Executor             | What it does                                                     |
| ---------------- | -------------------- | ---------------------------------------------------------------- |
| `--paper`        | PaperExecution       | Internal simulation. No orders sent anywhere.                    |
| `--ibkr` (default) | IBKRExecution      | Direct `placeOrder` market orders to whichever TWS/Gateway you're connected to (paper account on 7497, live on 7496). |
| `--live`         | SignalStackExecution | HTTP webhook → SignalStack → broker. Prompts for confirmation. Rate-limited to 2 actions/60s. |

> ⚠️ **Exit management:** in `--ibkr` mode every entry is a parent market
> order plus an attached **GTC protective stop resting at the broker**, so a
> dead process no longer leaves positions unprotected. Software exit detection
> stays as a redundant layer: it cancels the resting stop before sending a
> closing order, and a broker-side stop fill is routed back so no duplicate
> close is ever sent. Targets (TP) are still software-monitored only.
> `--live` (SignalStack) is market-only — those positions remain
> **software-protected only** (a startup WARNING says so). The `--ibkr` stop
> flow is unit-tested against a mocked IBKR but still needs one live
> verification session against paper TWS (see Known limitations).

---

## Robustness guarantees

| Failure mode                       | How it's handled                                                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Crash / kill -9                    | `logs/state.json` written atomically on every position change. On restart, reconcile vs IBKR positions before any new signals; in `--ibkr` mode the GTC protective stop keeps resting at the broker while the process is down, and reconciliation re-associates it (or re-places it with a WARNING if it's gone). (Caveat: restored daily P&L/halt status is wiped again on the first bar — see Known limitations.) |
| IBKR disconnect mid-trade          | Reconnect loop with exponential backoff (5s → 10s → 20s → … → 300s) in a daemon thread. On reconnect, state is reconciled before resubscribing bars. Gives up at 15:35 CT (16:35 ET). |
| Position closed while disconnected | Detected at reconcile: `ib.fills()` queried for the actual exit fill, logged as "disconnected exit" (falls back to entry price / 0R if no fill is found), strategy state cleared. |
| Orphan position (not in our state) | Logged as WARNING. Never touched automatically — requires manual review.                                                  |
| Partial fill                       | Fill verification 2 bars after entry via the ib_insync position cache (thread-safe, non-blocking). If actual < 80% of expected, `pos.shares` is adjusted so R math stays correct. If 0 shares, position state is cleared and the strategy re-arms. |
| Duplicate bar (ib_insync re-emit)  | Deduplicated by `(symbol, date, time)` key — silently dropped.                                                            |
| 15:59 bar never arrives            | EOD safety timer fires at `EOD_SAFETY_AT` (15:01 CT / 16:01 ET) on the local clock and force-closes all remaining positions at the last known bar close, through the full close path (executor + CSV + state.json). |
| Strategy stuck mid-state-machine   | Any strategy in a non-idle state for >90 bars is auto-reset to its idle state.                                            |
| Unattended overnight operation     | Process self-exit timer at `PROCESS_EXIT_AT` (15:45 CT / 16:45 ET) shuts down cleanly after IBC has closed TWS.           |

### Clock conventions

Bar times are **Eastern Time** — `data/feed.py` converts IBKR's UTC-aware
RealTimeBar timestamps to `America/New_York` via `zoneinfo`, so all strategy
time thresholds (e.g. `RTH_START = "09:30"`, `EOD_BAR = "15:59"`) are ET.
The EOD safety timer, reconnect give-up, and process-exit timer run on the
**local system clock**, which is assumed to be **Central Time** (see comments
in `config.py`). If you deploy on a machine in a different timezone, adjust
`EOD_SAFETY_AT`, `PROCESS_EXIT_AT`, and `_RECONNECT_GIVE_UP` accordingly.

---

## File structure

The **repo root is itself the Python package** (it has an `__init__.py` and all
modules use relative imports), so run it with `python -m <clone-dir-name>.main`
from the clone's **parent** directory. Commands below assume the clone is named
`Stockbot`.

```
Stockbot/
├── config.py                  ← All settings: universes, risk, timing, connections
├── models.py                  ← Bar, Signal, OpenPosition, SessionContext
├── main.py                    ← Orchestrator class + entry point
├── state_manager.py           ← Atomic state persistence + IBKR reconciliation
├── regime.py                  ← Daily SPY/VIX regime classifier + scale table
├── requirements.txt
│
├── strategies/
│   ├── base.py                ← BaseStrategy ABC
│   ├── orb_short.py           ← Volume-delta VWAP fade (short)
│   ├── impulse_short.py       ← Exhausted impulse retest (short)
│   ├── _gap_fill_base.py      ← Shared gap fill state machine
│   ├── gap_fill_variants.py   ← 4 concrete gap fill subclasses
│   └── __init__.py            ← Strategy factory (build_strategy)
│
├── risk/
│   └── risk_manager.py        ← Sizing, per-strategy + portfolio halts,
│                                 conflict resolution, regime scaling
├── execution/
│   └── __init__.py            ← PaperExecution, IBKRExecution,
│                                 SignalStackExecution + rate limiter
├── data/
│   └── feed.py                ← IBKRFeed + SessionContextBuilder (ET-normalized)
│
├── logging_layer/
│   └── __init__.py            ← CSV loggers
│
└── dashboard/
    └── __init__.py            ← localhost:8050 monitor (LIVE/TRADES/HISTORY)
```

---

## Quick start

### 1. Install dependencies

```
pip install -r requirements.txt
```

Requires: `ib_insync`, `pandas`, `numpy`, `yfinance` (used by `regime.py`
every morning — the bot will not start without it), plus `pytest` for the
test suite. A ready-made venv lives at `.venv/` (gitignored); use
`.venv\Scripts\python.exe` to run the bot and tests.

### 2. Configure

Edit `config.py` — all settings live there:

```python
MAX_SIMULTANEOUS_POSITIONS = 6
DAILY_LOSS_LIMIT_DOLLARS   = 1500.0   # portfolio hard stop
MAX_POSITIONS_PER_SYMBOL   = 1

STRATEGY_RISK = {
    "gap_fill_small_multi": {"risk_per_trade": 125.0, "max_dd":  750.0},
    "gap_fill_big":         {"risk_per_trade": 245.0, "max_dd": 1000.0},
    # ... per-strategy 1R sizing and per-strategy drawdown halts
}

STRATEGY_UNIVERSES = { "orb_short": [...31 syms], "impulse_short": [...102], ... }
STRATEGY_PRIORITY  = [ ... ]   # same-symbol conflict order
```

### 3. Start TWS or IB Gateway

- TWS → Configure → API → Enable ActiveX and Socket Clients
- Port: 7497 (paper), 7496 (live)

### 4. Run — paper simulation (no orders anywhere)

From the directory **containing** the clone:

```
python -m Stockbot.main --paper --warmup-days 20
```

### 5. Run — direct IBKR (default)

```
python -m Stockbot.main --warmup-days 20
```

Sends real `placeOrder` calls to whichever account TWS is logged into.
Use the paper TWS login (port 7497) for paper trading with real order flow.

### 6. Run — SignalStack live

```
set SIGNALSTACK_API_KEY=your_key_here
python -m Stockbot.main --live
```

You will be prompted to confirm. This fires real SignalStack webhooks.

---

## State file

`logs/state.json` is written atomically on every position change. Do not edit
it manually while the orchestrator is running. To clear it, stop the
orchestrator first, then delete it — the next startup starts fresh.

On restart, the orchestrator reads state.json, connects to IBKR, and
reconciles. A position that matches IBKR is restored and exit monitoring
resumes. A position IBKR doesn't show was closed while we were down — its
actual fill is looked up via `ib.fills()` and logged as a "disconnected exit"
(entry price / 0R if no fill is found). A position IBKR shows that we don't
know about is logged as an orphan and left alone.

Restored daily R / P&L / halt status, open positions, and their meta now
survive the session's first bar: `_on_new_session()` detects a restart into
the same session (via the restored state's date) and keeps everything instead
of resetting the day. Restored positions' strategies are re-marked in-trade
when their symbol's first bar arrives.

**Remaining caveat (on the fix list):** position `meta` is not actually
persisted — `OpenPosition` has no `meta` attribute, so state.json always
stores `meta: {}`. Benign for exit detection today (`gap_dir` equals the
direction sign it falls back to), but don't rely on restored meta.

---

## Comparing live vs backtest

`trade_log.csv` uses the same field names as the backtest CSVs: `session`,
`entry_time`, `exit_time`, `entry_price`, `stop`, `tp`, `exit_price`,
`result_R`, `exit_reason`, `direction`, plus strategy-specific meta fields.

**Fill reporting (IBKR mode):** `result_R` is deliberately still computed
from **trigger prices** so it stays directly comparable to the backtest CSVs
(and so the drawdown halts stay calibrated to backtest R). The actual broker
fills are recorded alongside: `entry_fill_price` in the trade row (the entry
fill arrives seconds after entry), `exit_fill_price` + `slippage_r` inline
for broker-stop exits (fill known at close time), and every fill in
`logs/fill_log.csv` (join on `trade_id`) for software exits whose market
order fills after the row is written. Real-money slippage analysis:

```python
fills = pd.read_csv("logs/fill_log.csv")
exit_fills = fills[fills["kind"] == "exit"].set_index("trade_id")["avg_price"]
```

```python
import pandas as pd
bt   = pd.read_csv("all_trades_gap_fill_large.csv")
live = pd.read_csv("logs/trade_log.csv")
live_gfl = live[live["strategy_id"] == "gap_fill_large"]

print(bt["result_R"].describe())
print(live_gfl["result_R"].describe())
```

---

## Adding a strategy

1. Create `strategies/my_strategy.py` inheriting `BaseStrategy`
2. Implement `reset_session(ctx)` and `on_bar(bar, ctx) → Signal | None`
3. Implement `on_exit(result_r, reason)` if the strategy allows re-entry
4. Add to `strategies/__init__.py` factory
5. Add to `config.STRATEGY_UNIVERSES`, `config.STRATEGY_PARAMS`,
   `config.STRATEGY_RISK`, and `config.STRATEGY_PRIORITY`

---

## Known limitations / next steps

| Item                        | Status                                                                                                                    |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| ~~Broker-side stop orders~~ | **Fixed 2026-08-23** (`--ibkr` mode). Entries now transmit a parent market order + attached GTC stop; software exits cancel-before-close; broker stop fills route back through `on_stop_filled` (logged as "stopped (broker stop)" with the actual fill price); reconciliation re-associates or re-places stops and cancels orphaned ones. Covered by `tests/test_ibkr_stops.py` (mocked IBKR). **Remaining:** one live paper-TWS session to verify end-to-end (enter trade → kill bot → stop still resting in TWS → restart → stop re-associated); TP is still software-only (no OCA bracket); SignalStack mode remains software-protected only. |
| ~~Group B feed emits partial bars~~ | **Fixed 2026-08-23.** Symbols 91+ (fed via `reqHistoricalData(keepUpToDate=True)`) now emit `bars[-2]` — the completed bar — on `hasNewBar`, instead of the newly started partial bar, with a same-date guard so a pre-market snapshot's last bar isn't re-fed. Covered by `tests/test_feed_ku.py`. Still worth eyeballing one live session against a TWS chart. |
| ~~Stale SessionContext at session rollover~~ | **Fixed 2026-08-23.** Strategies are now reset lazily on their own symbol's first bar of the session (with that symbol's fresh context), not en masse at global rollover with stale contexts. Gap `prior_close` and orb_short's weekday filters now see the correct session. Covered by `tests/test_session_reset.py` (the main test fails against the old code). |
| ~~Restart wipes restored session state~~ | **Fixed 2026-08-23.** `_on_new_session()` is restart-aware: same-session restarts keep restored daily P&L/halts/positions/meta, and restored positions' strategies are re-marked in-trade on their symbol's first bar. Covered by `tests/test_restart_session.py`. |
| ~~Exit fill prices in logs~~ | **Fixed 2026-08-23.** Broker fills now recorded: `entry_fill_price` / `exit_fill_price` / `slippage_r` columns appended to trade_log.csv plus a full `fill_log.csv`; `result_R` intentionally stays trigger-based for backtest comparability (see "Comparing live vs backtest"). |
| ~~Disconnected-exit fill lookup broken~~ | **Fixed 2026-08-23.** `_query_exit_fill` now uses `ib.fills()` (Fill objects carry `.contract`), and `_estimate_result_r` divides by per-share risk instead of `R_dollars`. Covered by `tests/test_state_ghost.py`. |
| Position meta not persisted | `state_manager.on_position_open` reads `getattr(pos, "meta", {})` but `OpenPosition` has no such attribute — always `{}`. Pass `signal.meta` through instead. |
| Hold-cap `hold_cap_exit_r` semantics | The bars-based cap is wired (`hold_cap_bars`); the companion `hold_cap_exit_r` param's backtest meaning was lost with the old PC's data — currently unused. Owner to clarify before enabling caps. |
| ~~Dashboard~~               | **Fixed 2026-08-23.** Renamed to `dashboard/__init__.py`, started by `run()` behind `config.DASHBOARD_ENABLE` on a daemon thread (crash-isolated from trading), binds `127.0.0.1` only, and takes the position lock around `risk.summary()`. Covered by `tests/test_dashboard.py`. |
| IBKR subscription limits    | 164 unique symbols: 90 via `reqRealTimeBars` + 74 via `keepUpToDate` historical. Default live market-data entitlement is 100 concurrent lines — verify your account's line count or expect "max tickers reached" errors on part of the universe. |
| ~~Regime download blocks bar thread~~ | **Fixed 2026-08-23.** `run()` pre-fetches the regime payload pre-market; `_on_new_session` uses the cached payload when it's from today and downloads only as a fallback. |
| ~~Hold-time cap~~           | **Fixed 2026-08-23.** `_detect_exit()` now honors `hold_cap_bars` (exit at bar close, reason "hold cap"; stop/TP take precedence within a bar). All params remain 0 = disabled, so behavior is unchanged until enabled. Same-symbol conflicts also now reach conflict_log.csv. Covered by `tests/test_hold_cap.py` / `tests/test_misc_p2.py`. |
| ~~requirements.txt~~        | **Fixed 2026-08-23.** `yfinance` (and `pytest`) added.                                                                     |
| ~~Repo hygiene~~            | **Fixed 2026-08-23.** `.gitignore` added; committed `__pycache__/*.pyc` untracked.                                         |
| Pre-market gap calculator   | Gap is computed from prior close vs first RTH bar open. A true pre-market feed would give earlier visibility. (`PREMARKET_ROUTINE_TIME` in config is currently unused.) |
| Tests                       | `tests/` (39 tests) covers the Group B feed handler, per-symbol session resets, ghost-exit logging, `_do_close` ordering, broker-side stops (mocked IBKR), the locking stress test, fill logging, restart preservation, hold cap, dashboard, and conflict/regime plumbing. Run `python -m pytest Stockbot/tests` from the parent directory. Still uncovered: RiskManager halt thresholds, reconcile edge cases, rate limiter, EOD timer scheduling. |

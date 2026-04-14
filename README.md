# Trading Bot Orchestrator

Central hub for all 6 trading strategy modules. Feeds live 1-min bars from IBKR
into each strategy, routes signals through risk management, fires orders via
SignalStack webhooks, and persists state so it survives disconnects and restarts.

---

## Architecture

```
IBKR (ib_insync)
    │
    ▼
IBKRFeed.on_bar(bar)         ← bar dedup (symbol+date+time) drops IBKR re-emits
    │
    ├── SessionContextBuilder
    │     Rolling per-symbol stats: VWAP, ATR, prior_close, vol_regime,
    │     first_bar_vol_ratio, daily_ATR. Fed to strategies at session start.
    │
    ├── Strategy instances (one per strategy × symbol pair)
    │
    │   SHORT STRATEGIES
    │     orb_short(FCX)          3-state: OBSERVING → WAIT_TRIGGER → IN_TRADE
    │     impulse_short(AMZN)     7-state: WAIT_BREAK → BUILD_PEAK →
    │                                      TRACK_PULLBACK → WAIT_RETEST →
    │                                      WAIT_FAILURE → WAIT_FILL → signal
    │
    │   GAP FILL STRATEGIES  (all share same WAIT_ENTRY → IN_TRADE core)
    │     gap_fill_large(MSFT)    gap-down LONG, session_extreme stop
    │     gap_fill_small(GS)      gap-up SHORT, gap_open_buffer stop, 1 trade/day
    │     gap_fill_small_multi(AMZN)  gap-up SHORT, unlimited re-entries/day
    │     gap_fill_big(T)         gap-up SHORT, gap_atr_ratio 0.7–1.0 band
    │          │
    │          └── Signal { entry, stop, tp, direction, R, meta }
    │
    ├── RiskManager
    │     • size trade: shares = RISK_PER_TRADE_DOLLARS / risk_per_share
    │     • max simultaneous positions cap
    │     • per-strategy position limit
    │     • same-symbol conflict (first signal wins)
    │     • daily loss limit halt
    │
    ├── StateManager                        ← NEW
    │     • atomic state.json written on every position change
    │     • startup/reconnect reconciliation against IBKR reqPositions()
    │     • ghost detection (closed while disconnected), orphan alerting
    │
    ├── Executor (PaperExecution or SignalStackExecution)
    │     • paper: logs only
    │     • live: POST webhook → SignalStack → IBKR paper/live
    │     • fill verification 2 bars post-entry (adjusts partial fills)
    │
    └── Logger
          trade_log.csv       signal_log.csv
          conflict_log.csv    daily_summary.csv    state.json
```

---

## Robustness guarantees

| Failure mode | How it's handled |
|---|---|
| Crash / kill -9 | `state.json` written atomically on every position change. On restart, reconcile vs IBKR `reqPositions()` before any new signals. |
| IBKR disconnect mid-trade | Reconnect loop with exponential backoff (5s → 10s → 20s → … → 5min). On reconnect, position state is reconciled before resubscribing bars. Gives up at 16:15. |
| Position closed while disconnected | Detected at reconnile: query `reqExecutions()` for fill price, log as "disconnected exit", clear strategy state. |
| Orphan position (not in our state) | Logged as WARNING. Never touched automatically — requires manual review. |
| Partial fill | Fill verification runs 2 bars after entry. If actual shares < 80% of expected, adjusts `OpenPosition.shares` so R calculations remain correct. If 0 shares found, clears position state and re-arms strategy. |
| Duplicate bar (ib_insync re-emit) | Deduplicated by `(symbol, date, time)` key — silently dropped. |
| 15:59 bar never arrives | `threading.Timer` fires at 16:00:30 regardless of bar arrival and force-closes all open positions. |
| Strategy stuck mid-state-machine | State timeout: any strategy in a non-idle state for >90 bars is auto-reset to WAIT_BREAK / WAIT_ENTRY. |

---

## File structure

```
orchestrator/
├── config.py                  ← All settings: universes, risk params, connections
├── models.py                  ← Bar, Signal, OpenPosition, SessionContext
├── main.py                    ← Orchestrator class + entry point
├── state_manager.py           ← Atomic state persistence + IBKR reconciliation
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
│   └── risk_manager.py        ← Sizing, limits, conflict resolution, halt logic
│
├── execution/
│   └── __init__.py            ← PaperExecution + SignalStackExecution
│
├── data/
│   └── feed.py                ← IBKRFeed + SessionContextBuilder
│                                 SessionContext carries: vwap, atr, prior_close,
│                                 daily_atr, vol_regime_ratio, first_bar_vol_ratio
│
├── logging_layer/
│   └── __init__.py            ← CSV loggers
│
└── dashboard/
    └── __init__.py            ← Live HTML dashboard (port 8050)
```

---

## Quick start

### 1. Install dependencies

```bash
pip install ib_insync pandas numpy
```

### 2. Configure

Edit `config.py` — all settings live there:

```python
RISK_PER_TRADE_DOLLARS     = 100.0   # 1R = $100
MAX_SIMULTANEOUS_POSITIONS = 3
DAILY_LOSS_LIMIT_DOLLARS   = 300.0   # halt after -$300

STRATEGY_UNIVERSES = {
    "orb_short":            ["FCX", "LVS", "QCOM", ...],
    "impulse_short":        ["AMZN", "ORCL", ...],
    "gap_fill_large":       ["MSFT", "ORCL", ...],
    "gap_fill_small":       ["GS", "MS", ...],
    "gap_fill_small_multi": ["GS", "MS", ...],
    "gap_fill_big":         ["T", "F", "LOW", ...],
}
```

### 3. Set your SignalStack key

```bash
export SIGNALSTACK_API_KEY="your_key_here"
```

### 4. Start TWS or IB Gateway

- TWS → Configure → API → Enable ActiveX and Socket Clients
- Port: 7497 (paper), 7496 (live)

### 5. Run — paper mode

```bash
python -m orchestrator.main --paper --warmup-days 20
```

### 6. Run — live

```bash
python -m orchestrator.main --live
```

You will be prompted to confirm. This fires real SignalStack webhooks.

### 7. Dashboard

Auto-starts at `http://localhost:8050`. Shows open positions, daily P&L in R
and dollars, per-strategy signal counts, halt status, fill verification alerts.

---

## State file

`logs/state.json` is written atomically on every position change. Do not edit
it manually while the orchestrator is running. If you need to clear it, stop
the orchestrator first, then delete it. The next startup will start fresh.

On restart, the orchestrator reads state.json, connects to IBKR, and
reconciles. If IBKR shows a position that matches — it's restored. If IBKR
doesn't show it — it was closed while we were down and gets logged as a
"disconnected exit". If IBKR shows something we don't know about — it's
logged as an orphan and left alone.

---

## Comparing live vs backtest

`trade_log.csv` uses the same field names as the backtest CSVs:
`session`, `entry_time`, `exit_time`, `entry_price`, `stop`, `tp`,
`exit_price`, `result_R`, `exit_reason`, `direction`, plus strategy-specific
meta fields (`gap_pct`, `vwap_at_signal`, `impulse_high`, etc.).

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
5. Add to `config.STRATEGY_UNIVERSES` and `config.STRATEGY_PARAMS`

---

## Known limitations / next steps

| Item | Status |
|---|---|
| Bracket orders via IB API | Not yet. Exits are managed in software. SignalStack webhook fires on entry only. For production, send GTC stop/limit orders directly via IB API so IBKR manages exits even if the orchestrator is down. |
| Partial fill resolution | Handled via 2-bar fill verification and reqPositions(). Not as precise as a direct order fill callback. |
| Hold-time cap | Implemented in backtest scripts. Not yet wired into `_detect_exit()` in main.py — add when risk tuning is finalised. |
| Pre-market gap calculator | Gap is computed from prior close vs first RTH bar open. A true pre-market feed would give earlier visibility. |
| Dashboard | Clean EOD dashboard in progress. |

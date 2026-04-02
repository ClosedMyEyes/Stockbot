# Trading Bot Orchestrator

Central hub for all trading strategy modules. Feeds live 1-min bars from IBKR
into each strategy, routes signals through risk management, and fires orders
via SignalStack webhooks.

---

## Architecture

```
IBKR (ib_insync)
    │
    ▼
IBKRFeed.on_bar(bar)
    │
    ├── SessionContextBuilder  (rolling ATR, VWAP, vol stats per symbol)
    │
    ├── Strategy instances (one per strategy×symbol pair)
    │       orb_short(GOOG)  orb_long(MSFT)  impulse_short(PM)
    │       gap_fill_large(AAPL)  gap_fill_small(MSFT)  gap_fill_big(MS)
    │            │
    │            └── Signal { entry, stop, tp, direction, meta }
    │
    ├── RiskManager
    │       • size trade: shares = RISK_PER_TRADE_DOLLARS / risk_per_share
    │       • max simultaneous positions cap
    │       • same-symbol conflict (first signal wins)
    │       • daily loss limit halt
    │
    ├── Executor (PaperExecution or SignalStackExecution)
    │       • paper: logs only
    │       • live: POST webhook → SignalStack → IBKR paper/live
    │
    └── Logger
            trade_log.csv     signal_log.csv
            conflict_log.csv  daily_summary.csv
```

---

## File Structure

```
orchestrator/
├── config.py              ← ALL your settings live here
├── models.py              ← Bar, Signal, OpenPosition, SessionContext
├── main.py                ← Orchestrator class + entry point
├── requirements.txt
├── strategies/
│   ├── base.py            ← BaseStrategy ABC
│   ├── orb_short.py       ← from orb.py
│   ├── orb_long.py        ← from orb_long.py
│   ├── impulse_short.py   ← from short.py
│   └── gap_fill.py        ← from gap_fill.py / smallshort_fill.py / bigshort_fill.py
├── risk/
│   └── risk_manager.py    ← sizing, limits, conflict resolution
├── execution/
│   └── __init__.py        ← PaperExecution + SignalStackExecution
├── data/
│   └── feed.py            ← IBKRFeed + SessionContextBuilder
├── logging_layer/
│   └── __init__.py        ← CSV loggers
└── dashboard/
    └── __init__.py        ← Live HTML dashboard (port 8050)
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install ib_insync pandas numpy
```

### 2. Configure your universe

Edit `config.py`:

```python
STRATEGY_UNIVERSES = {
    "orb_short":     ["GOOG", "MSFT"],
    "orb_long":      ["MSFT"],
    "impulse_short": ["PM"],
    "gap_fill_small": ["MSFT", "JPM"],
}

RISK_PER_TRADE_DOLLARS     = 100.0   # 1R = $100
MAX_SIMULTANEOUS_POSITIONS = 3
DAILY_LOSS_LIMIT_DOLLARS   = 300.0   # halt after -$300
```

### 3. Set your SignalStack key

```bash
export SIGNALSTACK_API_KEY="your_key_here"
```

### 4. Start TWS or IB Gateway (paper trading)

- Open TWS → Configure → API → Enable ActiveX and Socket Clients
- Port: 7497 (paper), 7496 (live)

### 5. Run (paper mode)

```bash
# From the parent directory of orchestrator/
python -m orchestrator.main --paper --warmup-days 20
```

### 6. Run (live — fires real SignalStack webhooks)

```bash
python -m orchestrator.main --live
```
You will be prompted to confirm.

### 7. Dashboard

Starts automatically at `http://localhost:8050` — auto-refreshes every 5 seconds.
Shows open positions, daily P&L in R and dollars, signal counts, halt status.

---

## Tuning Parameters

All strategy parameters are in `config.py` under `STRATEGY_PARAMS`.
They mirror the `argparse` defaults from the original backtest scripts exactly.

To change a parameter for a specific strategy:
```python
STRATEGY_PARAMS["orb_short"]["sl_buffer_pct"] = 1.0   # was 0.8
STRATEGY_PARAMS["gap_fill_small"]["gap_min_pct"] = 1.0
```

No code changes needed — only config.py edits.

---

## Output Files (logs/)

| File | Contents |
|------|----------|
| `trade_log.csv` | Every completed trade. Matches backtest CSV schema for direct comparison. |
| `signal_log.csv` | Every signal fired — accepted or rejected with reason. |
| `conflict_log.csv` | Symbol conflicts (two strategies wanted the same stock). |
| `daily_summary.csv` | End-of-day: R, dollars, trades, halts. |
| `orchestrator.log` | Full run log with timestamps. |

---

## Comparing Live vs Backtest

The `trade_log.csv` uses the same field names as your backtest CSVs:
`session`, `entry_time`, `exit_time`, `entry_price`, `stop`, `tp`,
`exit_price`, `result_R`, `exit_reason`, `direction`, plus strategy-specific
meta fields (gap_pct, vwap_at_signal, impulse_high, etc.).

Load both into pandas and compare:

```python
import pandas as pd
bt = pd.read_csv("all_trades.csv")        # your backtest log
live = pd.read_csv("logs/trade_log.csv")  # orchestrator live log

# Filter same strategy
bt_gap   = bt  # (your backtest already is gap fills)
live_gap = live[live["strategy_id"] == "gap_fill_large"]

print(bt_gap["result_R"].describe())
print(live_gap["result_R"].describe())
```

---

## Adding a New Strategy

1. Create `strategies/my_new_strategy.py` inheriting `BaseStrategy`
2. Implement `reset_session(ctx)` and `on_bar(bar, ctx) → Signal | None`
3. Add to `strategies/__init__.py` factory
4. Add entry to `config.STRATEGY_UNIVERSES` and `config.STRATEGY_PARAMS`

That's it — the orchestrator automatically picks it up.

---

## SignalStack → IBKR Paper Setup

1. Create a SignalStack account at signalstack.com
2. Connect your IBKR paper trading account
3. Get your webhook URL and API key
4. Set `SIGNALSTACK_WEBHOOK_URL` and `SIGNALSTACK_API_KEY` in config.py
5. Run with `--paper` first to verify signals log correctly
6. Switch to `--live` when ready to fire real paper orders

The webhook payload (in `execution/__init__.py`) uses:
```json
{ "ticker": "AAPL", "action": "buy", "orderType": "market", "contracts": 10 }
```
Adjust the `send_entry()` and `send_exit()` methods in `execution/__init__.py`
if your SignalStack configuration expects different field names.

---

## Known Limitations / Next Steps

- **Exit management**: stops and TPs are checked on each bar but are NOT
  sent as bracket orders to SignalStack. The orchestrator manages them in
  software. For production, consider sending GTC stop/limit orders.
- **Partial fills**: not modeled — all fills assumed complete at entry price.
- **Multi-symbol bars**: bars arrive one at a time; no cross-symbol logic.
- **Hold-time cap & trailing stop**: these are implemented in the backtest
  scripts but not yet wired into the orchestrator's exit detector. Add to
  `_detect_exit()` in `main.py` when ready.
- **Pre-market gap calculator**: currently computed from prior day close and
  today's first bar open. A true pre-market feed would improve accuracy.

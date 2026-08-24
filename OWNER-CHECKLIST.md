# OWNER CHECKLIST — what the bot needs from you

Status as of 2026-08-23: every item on the HANDOFF fix list is implemented and
covered by the test suite (53 tests — run them any time with the command at
the bottom), and the EOD-timing decision (§3) is resolved and implemented.
What remains needs **you**: a TWS install and one account check. Nothing here
is optional before trading real money.

---

## 1. Install TWS and run the live bracket verification  (~20 min, one market day)

This is the last gate. The broker-side stop/TP flow is fully unit-tested
against a mocked IBKR, but it has never touched a real TWS.

1. Install **TWS** (or IB Gateway) on this machine and log into the **paper**
   account (port 7497).
2. TWS → File → Global Configuration → API → Settings:
   - ✅ Enable ActiveX and Socket Clients
   - ❌ Read-Only API (must be OFF or orders are rejected)
   - Socket port = 7497
3. On a market day, start the bot **pre-market** (any time before 09:30 ET;
   warm-up for 164 symbols takes a while — start early and note how long it
   actually takes, we may want to slim it down):

   ```bash
   C:/Users/mark/Stockbot/.venv/Scripts/python -m Stockbot.main --warmup-days 20
   ```

   (run from `C:\Users\mark`, i.e. the folder *containing* the clone)
4. When a trade opens, check TWS: you should see the position **plus two
   resting GTC orders** — a stop (STP) and a take-profit limit (LMT) — both
   tagged `stockbot:<trade_id>` in the order ref column.
5. **Kill the bot** (close the console / Task Manager). Confirm both orders
   are still resting in TWS. That's the whole point of the brackets.
6. Restart the bot. The log should show `re-associated protective stop` /
   `re-associated tp` and `Session matches restored state — keeping daily
   P&L...`. Daily P&L must not reset to 0.
7. Let an exit happen (stop, TP, or EOD). Confirm: exactly one closing
   action at the broker, one row in `logs/trade_log.csv`, fills in
   `logs/fill_log.csv`, and after the session the trade row's
   `exit_fill_price` / `slippage_r` columns are populated.
   For an EOD exit specifically: the closing order must appear in TWS at
   ~15:59:00 ET (or 15:59:30 for the backstop) and FILL before 16:00 — if
   you ever see an EOD order sitting unfilled after the close, tell me.
8. Bonus check: pick one symbol from the second feed group (alphabetically
   after the first 90 — e.g. TGT or WFC) and compare a few of its logged
   bars against the TWS chart, verifying the partial-bar fix live.

## 2. Check your IBKR market-data line entitlement

The bot subscribes **164 symbols**; a default account gets **100 concurrent
market data lines**. Check: IBKR Client Portal → Settings → User Settings →
Market Data Subscriptions (or ask IBKR support what your line count is).

- If you have ≥164 lines (boosters scale with equity/commissions): nothing to do.
- If you have ~100: **you need to decide which universes to trim** — that's a
  strategy decision, so it's yours. Universe sizes: orb_short 31,
  impulse_short 102, gap_fill_large 39, gap_fill_small 39,
  gap_fill_small_multi 62, gap_fill_big 39 (164 unique).
- Either way, watch the startup log: it warns when >100 subscriptions are
  requested, and IBKR error 101 ("max tickers reached") means symbols are
  silently not streaming.

## 3. ~~DECISION NEEDED: end-of-day close timing~~ — RESOLVED 2026-08-23

You approved the recommendation and it's implemented + tested: each symbol's
EOD close fires when its **last real bar completes (~15:59:00 ET)**, with the
safety backstop at **15:59:30 ET** — both inside the session, so EOD market
orders always fill same-day. (Background, for the record: the 15:59 bar never
streams live, and IBKR treats post-close market orders as next-day orders —
the old 16:01 ET timer would have produced silent overnight holds.) EOD exits
now happen ~1 minute earlier than the backtests' 16:00 close; when comparing
live vs backtest EOD rows, expect that small systematic difference.

## 4. Only if you return to SignalStack / prop-firm mode

- SignalStack webhooks can't express brackets — those positions are
  software-protected only (the bot warns at startup).
- Exits share the 2-per-60-seconds webhook budget with entries, so a queued
  exit can wait behind entries. If that mode comes back, ask me to add
  exit-priority to the webhook queue first.

## 5. Notes — no action needed

- `impulse_short`'s volume-vs-median meta columns (`impulse_bar_vol_vs_median`
  etc.) are always blank live: `vol_median_tod` was never implemented in the
  feed. Trading decisions are unaffected; only those comparison columns are
  missing. Say the word if you want it built.
- `ib_insync` is no longer maintained (its author passed away in 2024). The
  pinned version works fine; if it ever breaks on a new Python, the
  community continuation is `ib_async` (near drop-in).
- The backtest/Monte-Carlo data died with the old PC — live logs are the new
  dataset. Resist retuning any strategy parameter until enough live data
  accumulates to justify it.

---

## Running things (recap)

```bash
C:/Users/mark/Stockbot/.venv/Scripts/python -m pytest Stockbot/tests -q
```

```bash
C:/Users/mark/Stockbot/.venv/Scripts/python -m Stockbot.main --paper --warmup-days 20
```

Both from `C:\Users\mark` (the folder containing the clone). Dashboard:
http://localhost:8050 while the bot runs. Fix-list history: `HANDOFF.md`.

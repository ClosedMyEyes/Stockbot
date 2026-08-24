# OWNER CHECKLIST — what the bot needs from you

Status as of 2026-08-23: every item on the HANDOFF fix list is implemented and
covered by the test suite (50 tests — run them any time with the command at
the bottom). What remains needs **you**: a TWS install, one account check, and
one decision. Nothing here is optional before trading real money.

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

## 3. DECISION NEEDED: end-of-day close timing

Found in the 2026-08-23 deep pass — this one is genuinely broken as designed
and needs your call because it changes *when* EOD exits happen:

**The problem:** no symbol reliably delivers its 15:59 bar (both feed paths
only emit a completed bar when the *next* bar starts, and RTH data ends at
16:00 — there is no next bar). So EOD closes always fall to the safety timer
at **16:01 ET** — but IBKR treats a market order submitted after the close as
a **next-day order**. Positions would hold overnight and close at tomorrow's
open. The GTC stops still protect you overnight, but it's unintended exposure
and the logs would claim a 16:00 close that actually happened the next morning.

**My recommendation:** close EOD positions when the day's last real bar
completes (~15:59:00 ET, on the 15:58 bar's close) and move the backstop
timer to 15:59:30 ET (14:59:30 CT). That's ~1 minute earlier than the
backtests' 16:00 close — a small, honest divergence in exchange for
guaranteed same-day fills.

Alternatives if you prefer: (a) exit at 15:55 ET for more margin, or
(b) submit real Market-on-Close orders before IBKR's 15:50 ET MOC cutoff —
closest to the backtest's closing price, but more moving parts.

**Tell me which and I'll implement it the same day.** Until then, avoid
holding into the close on live money.

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

"""
execution/__init__.py — Execution layer.

Two modes:
  1. PaperExecution  — simulated fills, no network calls
  2. SignalStackExecution — fires webhooks to SignalStack

The orchestrator decides which to use via config.
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from typing import Optional

from ..models import OpenPosition, Signal
from .. import config

log = logging.getLogger("execution")


# =============================================================================
# RATE LIMITER
# Prop firms cap inbound webhook actions. This enforces max 2 per 60 seconds
# across ALL send_entry and send_exit calls, regardless of strategy or symbol.
# If the limit is hit it sleeps until a slot opens — it never drops a signal.
# =============================================================================

class _RateLimiter:
    """Token-bucket style rate limiter over a rolling time window."""

    def __init__(self, max_calls: int = 2, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._timestamps: deque = deque()

    def acquire(self):
        now = time.monotonic()
        # Evict timestamps that have fallen outside the rolling window
        while self._timestamps and now - self._timestamps[0] >= self.period:
            self._timestamps.popleft()
        # If already at the cap, sleep until the oldest slot expires
        if len(self._timestamps) >= self.max_calls:
            sleep_for = self.period - (now - self._timestamps[0])
            if sleep_for > 0:
                log.info(f"[RateLimiter] cap reached — sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)
        self._timestamps.append(time.monotonic())


# One shared limiter instance — both entry and exit calls share the same budget
_rate_limiter = _RateLimiter(max_calls=2, period=60.0)


def _start_webhook_worker(q: queue.Queue) -> threading.Thread:
    """
    Background thread that drains the webhook queue with rate limiting.
    Sleeps happen here — never in the bar callback thread.
    """
    def _worker():
        while True:
            item = q.get()
            if item is None:
                break
            payload, label = item
            _rate_limiter.acquire()
            try:
                body = json.dumps(payload).encode("utf-8")
                req  = urllib.request.Request(
                    config.SIGNALSTACK_WEBHOOK_URL,
                    data=body,
                    headers={
                        "Content-Type":  "application/json",
                        "Authorization": f"Bearer {os.environ.get('SIGNALSTACK_API_KEY', config.SIGNALSTACK_API_KEY)}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status not in (200, 201, 204):
                        log.error(f"SignalStack HTTP {resp.status}  [{label}] payload={payload}")
                    else:
                        log.info(f"SignalStack → {resp.status}  [{label}]")
            except Exception as e:
                log.error(f"SignalStack webhook failed [{label}]: {e}")
            finally:
                q.task_done()

    t = threading.Thread(target=_worker, daemon=True, name="signalstack-worker")
    t.start()
    return t


# =============================================================================
# PAPER TRADING (SIMULATION)
# =============================================================================

class PaperExecution:
    """
    Simulates fills instantly at the signal's entry_price.
    Slippage is already baked in by the strategy module.
    Exit fills happen in the orchestrator's bar loop.
    Rate limiter is intentionally skipped in paper mode.
    """

    def send_entry(self, pos: OpenPosition) -> bool:
        """Returns True on success (always True for paper)."""
        log.info(
            f"[PAPER] ENTRY {pos.direction.upper()} {pos.symbol} "
            f"{pos.shares}sh @ {pos.entry_price:.4f}  "
            f"stop={pos.stop:.4f}  tp={pos.tp:.4f}"
        )
        return True

    def send_exit(self, pos: OpenPosition, exit_price: float, reason: str) -> bool:
        log.info(
            f"[PAPER] EXIT {pos.symbol} @ {exit_price:.4f}  ({reason})"
        )
        return True

    def cancel_order(self, pos: OpenPosition) -> bool:
        log.info(f"[PAPER] CANCEL {pos.symbol}")
        return True


# =============================================================================
# IBKR DIRECT EXECUTION
# =============================================================================

class IBKRExecution:
    """
    Submits orders directly to IBKR via ib_insync placeOrder.
    Works with both paper and live IBKR accounts — the account type is
    determined by which TWS/Gateway the IB instance is connected to.

    Every entry is a parent market order plus an attached GTC protective stop,
    transmitted together, so open positions keep exchange-level protection
    even if this process dies. The orchestrator's software exit detection
    stays as a redundant layer:
      - send_exit cancels the resting stop before placing the closing market
        order (and skips the close entirely if the stop already filled)
      - a broker-side stop fill is reported through `on_stop_filled(trade_id,
        avg_price)` so the orchestrator records the exit without sending a
        duplicate order
      - reconcile_stops() re-associates or re-places stops after a restart,
        and cancels stops whose position is gone (a forgotten GTC stop would
        otherwise fire later and OPEN a position)
    """

    # Stamped on every order we place; lets reconciliation recognise our
    # orders at TWS across process restarts.
    ORDER_REF_PREFIX = "stockbot:"

    def __init__(self, ib):
        self._ib = ib
        self._trades: dict = {}        # trade_id -> parent (entry) ib Trade
        self._stop_trades: dict = {}   # trade_id -> protective stop ib Trade
        self._exited_by_stop: set = set()  # trade_ids already closed by their stop
        self._contracts: dict = {}
        # Set by the orchestrator: callbacks (trade_id, avg_fill_price)
        # invoked on the ib event-loop thread when the respective order fills.
        self.on_stop_filled  = None   # protective stop filled → position closed
        self.on_entry_filled = None   # entry market order filled
        self.on_exit_filled  = None   # closing market order filled

    def _get_contract(self, symbol: str):
        if symbol not in self._contracts:
            from ib_insync import Stock
            self._contracts[symbol] = Stock(symbol, "SMART", "USD")
        return self._contracts[symbol]

    def _run_on_loop(self, fn) -> None:
        """Run fn on ib's event-loop thread (directly if already on it)."""
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._ib.loop.call_soon_threadsafe(fn)
        else:
            fn()

    def _ref(self, trade_id: str) -> str:
        return f"{self.ORDER_REF_PREFIX}{trade_id}"

    def _make_fill_reporter(self, trade_id: str, which: str):
        """Report an entry/exit fill's avg price back to the orchestrator."""
        def _on_filled(trade):
            price = getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0
            cb = self.on_entry_filled if which == "entry" else self.on_exit_filled
            if cb is not None:
                try:
                    cb(trade_id, price)
                except Exception as e:
                    log.error(f"[IBKR] on_{which}_filled callback failed for {trade_id}: {e}",
                              exc_info=True)
        return _on_filled

    def _make_stop_fill_handler(self, trade_id: str):
        def _on_filled(trade):
            self._exited_by_stop.add(trade_id)
            self._stop_trades.pop(trade_id, None)
            price = getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0
            log.warning(f"[IBKR] protective stop FILLED  trade_id={trade_id}  avg={price}")
            cb = self.on_stop_filled
            if cb is not None:
                try:
                    cb(trade_id, price)
                except Exception as e:
                    log.error(f"[IBKR] on_stop_filled callback failed for {trade_id}: {e}",
                              exc_info=True)
        return _on_filled

    def send_entry(self, pos: OpenPosition) -> bool:
        from ib_insync import MarketOrder, StopOrder
        action  = "BUY"  if pos.direction == "long" else "SELL"
        reverse = "SELL" if pos.direction == "long" else "BUY"
        # Stops are computed with fractional slippage buffers; IBKR rejects
        # sub-penny prices on stocks >= $1, so round to cents.
        stop_price = round(pos.stop, 2)
        try:
            contract = self._get_contract(pos.symbol)
            parent = MarketOrder(action, pos.shares, tif='DAY',
                                 orderRef=self._ref(pos.trade_id), transmit=False)
            parent_trade = self._ib.placeOrder(contract, parent)
            try:
                stop = StopOrder(reverse, pos.shares, stop_price, tif='GTC',
                                 parentId=parent_trade.order.orderId,
                                 orderRef=self._ref(pos.trade_id), transmit=True)
                stop_trade = self._ib.placeOrder(contract, stop)
            except Exception:
                # Child failed after parent was placed (untransmitted) — don't
                # leave the inert parent sitting at TWS.
                try:
                    self._ib.cancelOrder(parent_trade.order)
                except Exception:
                    pass
                raise

            self._trades[pos.trade_id]      = parent_trade
            self._stop_trades[pos.trade_id] = stop_trade
            stop_trade.filledEvent   += self._make_stop_fill_handler(pos.trade_id)
            parent_trade.filledEvent += self._make_fill_reporter(pos.trade_id, "entry")
            pos.stop_order_id = stop_trade.order.orderId

            log.info(
                f"[IBKR] ENTRY {action} {pos.symbol} {pos.shares}sh @ market  "
                f"+ GTC stop {reverse} @ {stop_price:.2f}  tp={pos.tp:.4f}  "
                f"orderId={parent_trade.order.orderId} stopId={stop_trade.order.orderId}"
            )
            return True
        except Exception as e:
            log.error(f"[IBKR] placeOrder entry failed for {pos.symbol}: {e}")
            return False

    def _cancel_protective_stop(self, trade_id: str):
        """Cancel the resting GTC stop for this trade, if any.
        Returns the stop Trade when it turns out to have already FILLED
        (the position is gone — the caller must not send a closing order)."""
        stop_trade = self._stop_trades.pop(trade_id, None)
        if stop_trade is None:
            return None
        if getattr(stop_trade.orderStatus, "status", "") == "Filled":
            self._exited_by_stop.add(trade_id)
            return stop_trade
        try:
            self._ib.cancelOrder(stop_trade.order)
            log.info(f"[IBKR] cancelled protective stop  trade_id={trade_id}")
        except Exception as e:
            log.error(f"[IBKR] cancel protective stop failed for {trade_id}: {e}")
        return None

    def send_exit(self, pos: OpenPosition, exit_price: float, reason: str) -> bool:
        from ib_insync import MarketOrder

        if pos.trade_id in self._exited_by_stop:
            # Position was already closed by the broker-side stop — the
            # orchestrator is just recording the exit. No order to send.
            self._exited_by_stop.discard(pos.trade_id)
            self._trades.pop(pos.trade_id, None)
            log.info(f"[IBKR] EXIT already done by broker stop: {pos.symbol}  ({reason})")
            return True

        action   = "SELL" if pos.direction == "long" else "BUY"
        contract = self._get_contract(pos.symbol)
        order    = MarketOrder(action, pos.shares, tif='DAY',
                               orderRef=self._ref(pos.trade_id))
        label    = f"[IBKR] EXIT {action} {pos.symbol} {pos.shares}sh @ market  ({reason})"

        def _place():
            try:
                # Cancel the resting stop FIRST — otherwise it can fill after
                # our market close and flip the position.
                already_filled = self._cancel_protective_stop(pos.trade_id)
                if already_filled is not None:
                    log.warning(
                        f"[IBKR] EXIT skipped for {pos.symbol}: protective stop "
                        f"already filled — no closing order sent  ({reason})"
                    )
                    return
                exit_trade = self._ib.placeOrder(contract, order)
                exit_trade.filledEvent += self._make_fill_reporter(pos.trade_id, "exit")
                log.info(label)
            except Exception as e:
                log.error(f"[IBKR] placeOrder exit failed for {pos.symbol}: {e}")

        self._run_on_loop(_place)
        self._trades.pop(pos.trade_id, None)
        return True

    def cancel_order(self, pos: OpenPosition) -> bool:
        """Cancel the tracked entry order AND its protective stop.
        Used when fill verification finds the entry never filled."""
        def _cancel():
            self._cancel_protective_stop(pos.trade_id)
            trade = self._trades.pop(pos.trade_id, None)
            if trade is None:
                log.warning(f"[IBKR] cancel_order: no tracked entry order for {pos.symbol}")
                return
            try:
                self._ib.cancelOrder(trade.order)
                log.info(f"[IBKR] CANCEL {pos.symbol}")
            except Exception as e:
                log.error(f"[IBKR] cancelOrder failed for {pos.symbol}: {e}")

        self._run_on_loop(_cancel)
        return True

    def reconcile_stops(self, positions) -> None:
        """
        Call after every (re)connect + state reconciliation, with the list of
        restored open positions:
          - each position is re-associated with its resting GTC stop at TWS
            (matched by stop_order_id, else by our orderRef); if none is
            found, a fresh GTC stop is placed and a WARNING logged
          - any of our stops whose trade_id has no open position is cancelled
        Runs on the ib event-loop thread; safe to call from any thread.
        """
        positions = list(positions)

        def _reconcile():
            from ib_insync import StopOrder
            try:
                open_trades = list(self._ib.openTrades())
            except Exception as e:
                log.error(f"[IBKR] reconcile_stops: openTrades() failed: {e}")
                return

            by_id = {t.order.orderId: t for t in open_trades}
            ours  = {}
            for t in open_trades:
                ref = getattr(t.order, "orderRef", "") or ""
                if ref.startswith(self.ORDER_REF_PREFIX) and t.order.orderType == "STP":
                    ours[ref[len(self.ORDER_REF_PREFIX):]] = t

            live_ids = set()
            for pos in positions:
                tid = pos.trade_id
                live_ids.add(tid)
                stop_trade = by_id.get(getattr(pos, "stop_order_id", None)) \
                             or ours.get(tid)
                if stop_trade is not None:
                    self._stop_trades[tid] = stop_trade
                    stop_trade.filledEvent += self._make_stop_fill_handler(tid)
                    pos.stop_order_id = stop_trade.order.orderId
                    log.info(f"[IBKR] re-associated protective stop  trade_id={tid}  "
                             f"stopId={stop_trade.order.orderId}")
                    continue
                # No live stop at IBKR — re-place it (no parent: position exists)
                reverse = "SELL" if pos.direction == "long" else "BUY"
                try:
                    st = StopOrder(reverse, pos.shares, round(pos.stop, 2),
                                   tif='GTC', orderRef=self._ref(tid))
                    t = self._ib.placeOrder(self._get_contract(pos.symbol), st)
                    self._stop_trades[tid] = t
                    t.filledEvent += self._make_stop_fill_handler(tid)
                    pos.stop_order_id = t.order.orderId
                    log.warning(
                        f"[IBKR] restored position {pos.symbol} trade_id={tid} had "
                        f"NO live protective stop — re-placed GTC stop @ {round(pos.stop, 2):.2f}"
                    )
                except Exception as e:
                    log.error(f"[IBKR] re-placing protective stop failed for {tid}: {e}")

            for tid, t in ours.items():
                if tid not in live_ids and tid not in self._stop_trades:
                    try:
                        self._ib.cancelOrder(t.order)
                        log.warning(
                            f"[IBKR] cancelled orphaned protective stop for departed "
                            f"trade {tid} ({t.contract.symbol}) — position no longer open"
                        )
                    except Exception as e:
                        log.error(f"[IBKR] cancelling orphaned stop {tid} failed: {e}")

        self._run_on_loop(_reconcile)


# =============================================================================
# SIGNALSTACK WEBHOOK
# =============================================================================

class SignalStackExecution:
    """
    Fires signals to SignalStack via HTTP POST webhooks.
    All HTTP calls happen in a dedicated background thread so the bar callback
    (ib_insync event loop) never blocks on network I/O or rate-limiter sleeps.

    send_entry / send_exit return True immediately after enqueueing — delivery
    is best-effort async. Failures are logged but do not crash the orchestrator.
    """

    def __init__(self):
        api_key = os.environ.get("SIGNALSTACK_API_KEY", config.SIGNALSTACK_API_KEY)
        if not api_key or api_key == "YOUR_SIGNALSTACK_API_KEY":
            raise RuntimeError(
                "SignalStack API key not set. "
                "Export SIGNALSTACK_API_KEY as an environment variable."
            )
        self._q      = queue.Queue()
        self._worker = _start_webhook_worker(self._q)

    def send_entry(self, pos: OpenPosition) -> bool:
        action = "buy" if pos.direction == "long" else "sell"
        payload = {
            "ticker":    pos.symbol,
            "action":    action,
            "orderType": "market",
            "contracts": pos.shares,
            "comment":   f"{pos.strategy_id}|{pos.trade_id}",
        }
        self._q.put((payload, f"ENTRY {pos.symbol}"))
        return True

    def send_exit(self, pos: OpenPosition, exit_price: float, reason: str) -> bool:
        action = "sell" if pos.direction == "long" else "buy"
        payload = {
            "ticker":    pos.symbol,
            "action":    action,
            "orderType": "market",
            "contracts": pos.shares,
            "comment":   f"EXIT|{pos.trade_id}|{reason}",
        }
        self._q.put((payload, f"EXIT {pos.symbol}"))
        return True

    def cancel_order(self, pos: OpenPosition) -> bool:
        log.warning(f"[SignalStack] cancel_order called for {pos.symbol} — "
                    f"no-op (market orders fill immediately).")
        return True


# =============================================================================
# FACTORY
# =============================================================================

def get_executor(mode: str = "paper", ib=None):
    """
    mode: "paper"       — internal simulation, no orders sent anywhere
          "ibkr"        — direct ib_insync placeOrder (requires ib= kwarg)
          "signalstack" — HTTP webhook to SignalStack
    """
    if mode == "paper":
        log.info("Execution mode: PAPER (simulated, no orders sent)")
        return PaperExecution()
    if mode == "ibkr":
        if ib is None:
            raise RuntimeError("IBKRExecution requires an IB instance — pass ib=feed._ib")
        log.info("Execution mode: IBKR (direct ib_insync placeOrder)")
        return IBKRExecution(ib)
    if mode == "signalstack":
        log.info("Execution mode: SIGNALSTACK (live webhook)")
        log.warning(
            "SignalStack webhooks are market-only — no broker-side protective "
            "stops. Open positions are SOFTWARE-protected only: if this process "
            "dies mid-trade, they have no exchange-level stop."
        )
        return SignalStackExecution()
    raise ValueError(f"Unknown execution mode {mode!r} — choose paper, ibkr, or signalstack")

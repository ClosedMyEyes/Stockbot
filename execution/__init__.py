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
    """

    def __init__(self, ib):
        self._ib = ib
        self._trades: Dict[str, object] = {}   # trade_id -> ib Trade
        self._contracts: Dict[str, object] = {}

    def _get_contract(self, symbol: str):
        if symbol not in self._contracts:
            from ib_insync import Stock
            self._contracts[symbol] = Stock(symbol, "SMART", "USD")
        return self._contracts[symbol]

    def send_entry(self, pos: OpenPosition) -> bool:
        from ib_insync import MarketOrder
        action = "BUY" if pos.direction == "long" else "SELL"
        try:
            trade = self._ib.placeOrder(
                self._get_contract(pos.symbol),
                MarketOrder(action, pos.shares),
            )
            self._trades[pos.trade_id] = trade
            log.info(
                f"[IBKR] ENTRY {action} {pos.symbol} {pos.shares}sh @ market  "
                f"stop={pos.stop:.4f}  tp={pos.tp:.4f}  orderId={trade.order.orderId}"
            )
            return True
        except Exception as e:
            log.error(f"[IBKR] placeOrder entry failed for {pos.symbol}: {e}")
            return False

    def send_exit(self, pos: OpenPosition, exit_price: float, reason: str) -> bool:
        from ib_insync import MarketOrder
        action = "SELL" if pos.direction == "long" else "BUY"
        try:
            self._ib.placeOrder(
                self._get_contract(pos.symbol),
                MarketOrder(action, pos.shares),
            )
            log.info(f"[IBKR] EXIT {action} {pos.symbol} {pos.shares}sh @ market  ({reason})")
            return True
        except Exception as e:
            log.error(f"[IBKR] placeOrder exit failed for {pos.symbol}: {e}")
            return False

    def cancel_order(self, pos: OpenPosition) -> bool:
        trade = self._trades.pop(pos.trade_id, None)
        if trade is None:
            log.warning(f"[IBKR] cancel_order: no tracked entry order for {pos.symbol}")
            return False
        try:
            self._ib.cancelOrder(trade.order)
            log.info(f"[IBKR] CANCEL {pos.symbol}")
            return True
        except Exception as e:
            log.error(f"[IBKR] cancelOrder failed for {pos.symbol}: {e}")
            return False


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
        return SignalStackExecution()
    raise ValueError(f"Unknown execution mode {mode!r} — choose paper, ibkr, or signalstack")

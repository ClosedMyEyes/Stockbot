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
# SIGNALSTACK WEBHOOK
# =============================================================================

class SignalStackExecution:
    """
    Fires signals to SignalStack via HTTP POST webhooks.
    SignalStack then routes to your broker (IBKR paper or live).

    Webhook format follows the SignalStack spec:
      POST https://api.signalstack.com/webhook
      Headers: Content-Type: application/json
      Body: { "ticker": "AAPL", "action": "buy", "orderType": "market",
              "contracts": 10 }

    Adjust the payload builder below to match your SignalStack setup.
    """

    def __init__(self):
        self.webhook_url = config.SIGNALSTACK_WEBHOOK_URL
        self.api_key     = os.environ.get("SIGNALSTACK_API_KEY",
                                          config.SIGNALSTACK_API_KEY)
        if not self.api_key or self.api_key == "YOUR_SIGNALSTACK_API_KEY":
            raise RuntimeError(
                "SignalStack API key not set. "
                "Export SIGNALSTACK_API_KEY as an environment variable."
            )

    def _post(self, payload: dict) -> bool:
        body = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                if status in (200, 201, 204):
                    log.info(f"SignalStack → {status}  payload={payload}")
                    return True
                else:
                    log.error(f"SignalStack HTTP {status}  payload={payload}")
                    return False
        except Exception as e:
            log.error(f"SignalStack request failed: {e}  payload={payload}")
            return False

    def send_entry(self, pos: OpenPosition) -> bool:
        _rate_limiter.acquire()   # enforce 2-per-minute cap before firing
        action = "buy" if pos.direction == "long" else "sell"
        payload = {
            "ticker":    pos.symbol,
            "action":    action,
            "orderType": "market",
            "contracts": pos.shares,
            # Custom fields SignalStack passes through to IBKR:
            "comment":   f"{pos.strategy_id}|{pos.trade_id}",
        }
        return self._post(payload)

    def send_exit(self, pos: OpenPosition, exit_price: float, reason: str) -> bool:
        _rate_limiter.acquire()   # enforce 2-per-minute cap before firing
        # Close the position: opposite side
        action = "sell" if pos.direction == "long" else "buy"
        payload = {
            "ticker":    pos.symbol,
            "action":    action,
            "orderType": "market",
            "contracts": pos.shares,
            "comment":   f"EXIT|{pos.trade_id}|{reason}",
        }
        return self._post(payload)

    def cancel_order(self, pos: OpenPosition) -> bool:
        # SignalStack doesn't have a cancel concept for market orders that already filled.
        # If needed, send an offsetting market order.
        log.warning(f"[SignalStack] cancel_order called for {pos.symbol} — "
                    f"no-op (market orders fill immediately in paper mode).")
        return True


# =============================================================================
# FACTORY
# =============================================================================

def get_executor(paper: bool = True):
    if paper:
        log.info("Execution mode: PAPER (simulated)")
        return PaperExecution()
    else:
        log.info("Execution mode: SIGNALSTACK (live webhook)")
        return SignalStackExecution()

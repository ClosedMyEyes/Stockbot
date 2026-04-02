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
from typing import Optional

from ..models import OpenPosition, Signal
from .. import config

log = logging.getLogger("execution")


# =============================================================================
# PAPER TRADING (SIMULATION)
# =============================================================================

class PaperExecution:
    """
    Simulates fills instantly at the signal's entry_price.
    Slippage is already baked in by the strategy module.
    Exit fills happen in the orchestrator's bar loop.
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
        if self.api_key == "YOUR_SIGNALSTACK_API_KEY":
            log.warning("SignalStack API key not set — set SIGNALSTACK_API_KEY env var.")

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

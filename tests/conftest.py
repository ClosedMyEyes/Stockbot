"""
Shared test fixtures.

The repo root is itself the Python package (relative imports throughout), so
tests import it by whatever the clone directory is named. Run with:

    python -m pytest <clone-dir>/tests

from the clone's parent directory, or from anywhere — sys.path is patched here.
No live TWS: anything that would touch the network is stubbed.
"""

import importlib
import sys
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[1]
PKG = _PKG_DIR.name
if str(_PKG_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR.parent))


def pkg(module: str = ""):
    """Import a module from the bot package regardless of the clone's name."""
    name = f"{PKG}.{module}" if module else PKG
    return importlib.import_module(name)


def make_bar(symbol, date, time, o=100.0, h=None, l=None, c=None, v=10_000.0):
    """Build a models.Bar with sane OHLC defaults (flat bar unless told otherwise)."""
    models = pkg("models")
    c = o if c is None else c
    h = max(o, c) if h is None else h
    l = min(o, c) if l is None else l
    return models.Bar(symbol=symbol, date=date, time=time,
                      open=o, high=h, low=l, close=c, volume=v)


@pytest.fixture
def orchestrator(monkeypatch, tmp_path):
    """
    Paper-mode Orchestrator with no network:
      - cwd moved to tmp_path so logs/, state.json, CSVs land in the sandbox
      - regime.load_for_session stubbed (no yfinance download)
    Session dates used in tests must be in the past so the EOD safety /
    process-exit timers compute a negative delay and are never scheduled.
    """
    monkeypatch.chdir(tmp_path)
    main = pkg("main")
    monkeypatch.setattr(main, "load_for_session",
                        lambda: {"scales": {}, "regime": "TEST"})
    return main.Orchestrator(mode="paper")

"""
regime.py — Morning regime classifier for the orchestrator.

Downloads last 2y of daily SPY + VIX, computes a 9-regime label
(BULL/SIDEWAYS/BEAR x LOW/MED/HIGH), and returns per-strategy scale
factors derived from the 1998-2026 bootstrap CI analysis (BOOST=1.5x).

Called once per trading session from _on_new_session() in main.py.
No local cache — fresh yfinance download takes ~1 second.

Indicator parameters must match regime_analysis.py exactly.
"""

import json
import logging
from datetime import date

import pandas as pd
import yfinance as yf

log = logging.getLogger("regime")

# ── Indicator parameters (must match regime_analysis.py) ──────────────────────
MA_FAST    = 50
MA_SLOW    = 200
SLOPE_DAYS = 20
VIX_LOW    = 15
VIX_HIGH   = 25

# ── Hardcoded scale table (1998-2026 bootstrap, BOOST=1.5x) ──────────────────
# Only cells where the lower 95% CI of avg_R > +0.05 are BOOST.
# Source: regime_optimizer.py Part 1 output, run 2026-05-11.
# Update this table after accumulating 2-3 more years of live data.
_BOOST = 1.5
SCALE_TABLE: dict[tuple, float] = {
    ("orb_short",            "SIDEWAYS_MED"):  _BOOST,
    ("impulse_short",        "BEAR_HIGH"):     _BOOST,
    ("gap_fill_big",         "BULL_MED"):      _BOOST,
    ("gap_fill_big",         "SIDEWAYS_HIGH"): _BOOST,
    ("gap_fill_large",       "BULL_MED"):      _BOOST,
    ("gap_fill_large",       "SIDEWAYS_MED"):  _BOOST,
    ("gap_fill_large",       "SIDEWAYS_HIGH"): _BOOST,
    ("gap_fill_large",       "BEAR_MED"):      _BOOST,
    ("gap_fill_small",       "BEAR_HIGH"):     _BOOST,
    ("gap_fill_small_multi", "BULL_MED"):      _BOOST,
    ("gap_fill_small_multi", "BEAR_MED"):      _BOOST,
    ("gap_fill_small_multi", "BEAR_HIGH"):     _BOOST,
}

_ALL_STRATEGIES = [
    "orb_short", "impulse_short", "gap_fill_big",
    "gap_fill_large", "gap_fill_small", "gap_fill_small_multi",
]

REGIME_JSON_PATH = "regime.json"


# ── Core computation ──────────────────────────────────────────────────────────

def _download_daily() -> pd.DataFrame:
    spy = yf.download("SPY",  period="2y", interval="1d",
                      auto_adjust=True, progress=False)
    vix = yf.download("^VIX", period="2y", interval="1d",
                      auto_adjust=True, progress=False)
    for df in (spy, vix):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    closes = pd.DataFrame({
        "spy": spy["Close"],
        "vix": vix["Close"],
    }).dropna()
    return closes


def compute_regime(closes: pd.DataFrame | None = None) -> dict:
    """
    Compute today's regime from daily closes.
    If closes is None, downloads fresh data from yfinance.
    Returns a dict with keys: regime, trend, vol, spy, vix.
    Raises ValueError on bad/insufficient data so load_for_session()
    can catch it and fall back to 1.0x scales safely.
    """
    import math

    if closes is None:
        closes = _download_daily()

    min_bars = MA_SLOW + SLOPE_DAYS + 5
    if len(closes) < min_bars:
        raise ValueError(
            f"Only {len(closes)} bars returned — need {min_bars} for {MA_SLOW}-day MA"
        )

    ma_fast = closes["spy"].rolling(MA_FAST).mean()
    ma_slow = closes["spy"].rolling(MA_SLOW).mean()
    slope   = (ma_slow - ma_slow.shift(SLOPE_DAYS)) / ma_slow.shift(SLOPE_DAYS)

    price = float(closes["spy"].iloc[-1])
    fast  = float(ma_fast.iloc[-1])
    slow  = float(ma_slow.iloc[-1])
    sl    = float(slope.iloc[-1]) if not pd.isna(slope.iloc[-1]) else 0.0
    vix_v = float(closes["vix"].iloc[-1])

    # NaN comparisons silently return False, producing a garbage regime label.
    # Raise explicitly so the caller falls back to 1.0x rather than boosting on bad data.
    if any(math.isnan(v) for v in (price, fast, slow, vix_v)):
        raise ValueError(
            f"NaN in regime indicators — price={price} fast={fast} "
            f"slow={slow} vix={vix_v}"
        )

    if price > slow and fast > slow and sl > 0.0005:
        trend = "BULL"
    elif price < slow and fast < slow and sl <= 0.0005:
        trend = "BEAR"
    else:
        trend = "SIDEWAYS"

    if vix_v < VIX_LOW:
        vol = "LOW"
    elif vix_v <= VIX_HIGH:
        vol = "MED"
    else:
        vol = "HIGH"

    return {
        "regime": f"{trend}_{vol}",
        "trend":  trend,
        "vol":    vol,
        "spy":    round(price, 2),
        "vix":    round(vix_v, 2),
    }


def get_scales(regime: str) -> dict[str, float]:
    """Return {strategy_id: scale_factor} for the given regime label."""
    return {
        strat: SCALE_TABLE.get((strat, regime), 1.0)
        for strat in _ALL_STRATEGIES
    }


# ── Session entry point ───────────────────────────────────────────────────────

def load_for_session(json_path: str = REGIME_JSON_PATH) -> dict:
    """
    Called once per session from _on_new_session().
    Downloads daily data, computes regime, writes regime.json, returns payload.
    On any failure, returns all scales = 1.0 (safe default).
    """
    try:
        info   = compute_regime()
        regime = info["regime"]
        scales = get_scales(regime)
    except Exception as exc:
        log.error(f"Regime computation failed ({exc}) — defaulting all scales to 1.0")
        info   = {"regime": "UNKNOWN", "trend": "?", "vol": "?", "spy": 0.0, "vix": 0.0}
        regime = "UNKNOWN"
        scales = {s: 1.0 for s in _ALL_STRATEGIES}

    payload = {
        "date":   date.today().isoformat(),
        "scales": scales,
        **info,
    }

    try:
        with open(json_path, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        log.error(f"Could not write {json_path}: {exc}")

    boosted = [s for s, v in scales.items() if v > 1.0]
    log.info(
        f"Regime {regime}  SPY={info['spy']:.2f}  VIX={info['vix']:.2f}  "
        f"boosted={boosted if boosted else 'none'}"
    )
    return payload

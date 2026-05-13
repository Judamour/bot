"""Centralized tunable constants for shadow v2 (concentrated high-conviction).

Imported by BOTH:
  - shadow/runner.py (prod live cycle on Alpaca paper)
  - backtest/run_shadow.py (3y historical replay)

This single source of truth guarantees backtest ↔ prod parity.
"""
from __future__ import annotations

# ── Quality gate ─────────────────────────────────────────────────────────────
SCORE_FLOOR = 65              # G1: signal must score ≥ this
COOLDOWN_DAYS = 5             # G4: forbid re-entry on a symbol N days after a stop

# Active detectors (subset of shadow.strategies.ALL_DETECTORS).
# v2 iter-1: dropping supertrend + mean_reversion which bleed on 4h
# (300 trades 27% win -$38, 128 trades 24% win -$21). Keep trend_multi_asset
# (89 trades 34% win +$68), donchian, momentum.
ACTIVE_DETECTORS = ("trend_multi_asset", "donchian", "momentum")

# ── Concentration / sizing ───────────────────────────────────────────────────
TOP_N_SIGNALS = 2             # number of candidates considered per cycle (top by score)
MAX_OPEN_POSITIONS = 10       # hard cap on concurrent positions across cycles
WEIGHT_BY_RANK = [0.60, 0.30]  # % of available cash by rank in the cycle's top-2
                                # total 90%, leaves 10% cash buffer
RISK_PARITY_PCT_FALLBACK = 0.01      # fallback if score-weighted sizing not applicable

# ── Trailing stop adaptatif ──────────────────────────────────────────────────
ATR_MULT_STOP_INIT = 4.0      # initial stop = entry - 4.0 × ATR(14) (wider, trend trades need room)
ATR_MULT_TRAIL = 5.0          # trailing widens to 5.0 × ATR once position is up > +5%
PROFIT_LOOSEN_PCT = 0.05      # threshold to switch from tight → loose trailing

# ── Régime SHIELD ────────────────────────────────────────────────────────────
VIX_SHIELD_THRESHOLD = 30.0   # VIX > this → SHIELD active (no new entries)

# ── Defensive rotation (iter-4) ──────────────────────────────────────────────
# When broad equity is in bear (SPY < SMA200 or QQQ < SMA200), instead of
# going fully dormant we restrict scanning to assets that historically
# perform in bear/crisis regimes (gold, healthcare, defensive consumer, energy)
# and reduce sizing as a prudent measure.
DEFENSIVE_SYMBOLS = ("GLD", "KO", "PG", "LLY", "ABBV", "XOM", "CVX")
EQUITY_BEAR_SIZE_FACTOR = 0.5  # half-position when in equity-bear scan mode

# ── Risk guard (MaxDD halt) ──────────────────────────────────────────────────
HALT_DD_PCT = -0.15           # rolling DD ≤ this → halt new entries
HALT_DURATION_DAYS = 7        # halt lasts this many days after triggering

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

# ── Concentration / sizing ───────────────────────────────────────────────────
TOP_N_SIGNALS = 3             # number of candidates considered per cycle (top by score)
MAX_OPEN_POSITIONS = 10       # hard cap on concurrent positions across cycles
WEIGHT_BY_RANK = [0.30, 0.20, 0.15]  # % of available cash by rank in the cycle's top-3
                                      # remainder ≥35% stays as cash buffer
RISK_PARITY_PCT_FALLBACK = 0.01      # fallback if score-weighted sizing not applicable

# ── Trailing stop adaptatif ──────────────────────────────────────────────────
ATR_MULT_STOP_INIT = 1.5      # initial stop = entry - 1.5 × ATR(14)
ATR_MULT_TRAIL = 3.0          # trailing widens to 3.0 × ATR once position is up > +5%
PROFIT_LOOSEN_PCT = 0.05      # threshold to switch from tight → loose trailing

# ── Régime SHIELD ────────────────────────────────────────────────────────────
VIX_SHIELD_THRESHOLD = 30.0   # VIX > this → SHIELD active (no new entries)

# ── Risk guard (MaxDD halt) ──────────────────────────────────────────────────
HALT_DD_PCT = -0.15           # rolling DD ≤ this → halt new entries
HALT_DURATION_DAYS = 7        # halt lasts this many days after triggering

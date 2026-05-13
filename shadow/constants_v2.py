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
# v2 iter-1: dropped supertrend + mean_reversion (4h bleeders, -$38 / -$21).
# v2 iter-5: dropped momentum (bear bleeder: 6 trades, win 17%, -$41 PnL).
# Kept: trend_multi_asset (workhorse, +$1,211 in bull / -$46 in bear) and
# donchian (small but positive +$11 in bear, decorrelated breakout).
ACTIVE_DETECTORS = ("trend_multi_asset", "donchian", "inverse_bear")

# ── Concentration / sizing ───────────────────────────────────────────────────
TOP_N_SIGNALS = 2             # number of candidates considered per cycle (top by score)
MAX_OPEN_POSITIONS = 10       # hard cap on concurrent positions across cycles
WEIGHT_BY_RANK = [0.60, 0.30]  # % of available cash by rank in the cycle's top-2
                                # total 90%, leaves 10% cash buffer
RISK_PARITY_PCT_FALLBACK = 0.01      # fallback if score-weighted sizing not applicable

# Vol-adjusted sizing (iter-5): scale position size by inverse of asset vol.
# weight_adjusted = weight × min(TARGET_DAILY_VOL / asset_vol_pct, 1.0)
# Caps at 1.0 → no leverage on low-vol. Penalizes high-vol (BTC, AVAX) which
# get smaller positions for same risk budget. Pass-through if atr unknown.
TARGET_DAILY_VOL = 0.015      # 1.5% daily vol target (matches mid-cap stocks)

# ── Trailing stop adaptatif ──────────────────────────────────────────────────
ATR_MULT_STOP_INIT = 4.0      # initial stop = entry - 4.0 × ATR(14) (wider, trend trades need room)
ATR_MULT_TRAIL = 5.0          # trailing widens to 5.0 × ATR once position is up > +5%
PROFIT_LOOSEN_PCT = 0.05      # threshold to switch from tight → loose trailing

# ── Régime SHIELD ────────────────────────────────────────────────────────────
# Raised 30 → 35 in iter-5: with real VIX in backtest, 30 fires too often on
# regular pullbacks (44 cycles in 3y bull, dampening CAGR by ~15 pts).
# 35+ is "real crisis" territory (2020 covid, 2022 Q4, 2024 carry unwind).
VIX_SHIELD_THRESHOLD = 35.0   # VIX > this → SHIELD active (no new entries)

# ── Defensive rotation (iter-4) ──────────────────────────────────────────────
# When broad equity is in bear (SPY < SMA200 or QQQ < SMA200), instead of
# going fully dormant we restrict scanning to assets that historically
# perform in bear/crisis regimes (gold, healthcare, defensive consumer, energy)
# plus INVERSE_ETFS (SQQQ = -3× QQQ, SH = -1× SPY) which profit from declines.
DEFENSIVE_SYMBOLS = ("GLD", "KO", "PG", "LLY", "ABBV", "XOM", "CVX")
# Inverse ETFs (iter-6 #4): only tradeable in equity_bear (handled by detector).
# SQQQ = -3× QQQ daily, SH = -1× SPY daily. KNOWN RISK: volatility decay on
# long holds. Trailing stops + cooldowns limit exposure window.
INVERSE_ETFS = ("SQQQ", "SH")
DEFENSIVE_AND_INVERSE = DEFENSIVE_SYMBOLS + INVERSE_ETFS  # equity_bear scan universe
EQUITY_BEAR_SIZE_FACTOR = 0.5  # half-position when in equity-bear scan mode

# ── Diversification (iter-6 #2) ──────────────────────────────────────────────
# Constrain top-N to come from different sectors. Avoids concentrating
# capital on highly correlated assets (e.g. BTC + ETH both rallying together,
# or NVDA + GOOGL both on tech earnings beat). Hard cap = 1 position per
# sector per cycle.
SECTOR_MAP = {
    # Crypto (highly correlated, treat as single sector)
    "BTC/USD": "crypto", "ETH/USD": "crypto", "SOL/USD": "crypto",
    "AVAX/USD": "crypto", "LINK/USD": "crypto",
    # Tech mega-cap
    "NVDA": "tech", "GOOGL": "tech", "META": "tech",
    # AI/Cloud (correlated with tech but distinct)
    "PLTR": "ai", "CRWD": "ai",
    # Healthcare
    "LLY": "healthcare", "ABBV": "healthcare",
    # Energy
    "XOM": "energy", "CVX": "energy",
    # Financials
    "JPM": "financials", "BAC": "financials",
    # Defensive consumer
    "KO": "consumer", "PG": "consumer",
    # Broad index
    "SPY": "index", "QQQ": "index",
    # Gold (true diversifier)
    "GLD": "gold",
    # Inverse ETFs (iter-6 #4): own sector to allow 1 inverse position alongside defensives
    "SQQQ": "inverse",
    "SH": "inverse",
}
MAX_PER_SECTOR = 1  # max 1 position per sector per cycle

# ── Macro-aware exits (iter-6 #3) ────────────────────────────────────────────
# When SHIELD or HALT activates with a position already at +N%, lock in
# the gain (force exit at market). +5% was too tight (cut bull rallies short),
# +15% protects only "strong winners" while letting normal trends run.
MACRO_EXIT_PROFIT_PCT = 0.15   # take profit if SHIELD/HALT + pnl ≥ +15%

# ── Risk guard (MaxDD halt) ──────────────────────────────────────────────────
HALT_DD_PCT = -0.15           # rolling DD ≤ this → halt new entries
HALT_DURATION_DAYS = 7        # halt lasts this many days after triggering

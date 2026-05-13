"""Regime detection for shadow v2 — hard SHIELD cutoff.

shield_active(macro) returns True when the market is too risky for new entries.
This is a HARD GATE at cycle level: when True, the runner skips the scan/entry
phase and only manages existing positions.
"""
from __future__ import annotations
from shadow.constants_v2 import VIX_SHIELD_THRESHOLD


def shield_active(macro: dict) -> bool:
    """Return True if SHIELD should suppress new entries this cycle.

    Conditions (OR-combined):
      1. VIX strictly above VIX_SHIELD_THRESHOLD (default 30)
      2. BTC trend is bear AND QQQ is below its 200-day SMA

    Missing keys default to neutral values (vix=18, btc=bull, qqq_ok=True) so
    incomplete macro snapshots do NOT trigger SHIELD by mistake.
    """
    vix = macro.get("vix", 18.0)
    btc_trend = macro.get("btc_trend", "bull")
    qqq_ok = macro.get("qqq_regime_ok", True)

    if vix > VIX_SHIELD_THRESHOLD:
        return True
    if btc_trend == "bear" and not qqq_ok:
        return True
    return False


def equity_bear_active(macro: dict) -> bool:
    """Return True if broad equity is in bear regime → rotate to defensives.

    Lighter trigger than SHIELD: fires on SPY/QQQ below its 200-day SMA alone.
    Used to switch from full-universe scan to DEFENSIVE_SYMBOLS-only scan,
    not to halt entries entirely.

    Disjoint from shield_active in practice: if both are true, SHIELD wins
    (no new entries). If only equity_bear, we scan the defensive subset.
    """
    return not macro.get("qqq_regime_ok", True)

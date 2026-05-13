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

    Asymmetric (hysteresis) to avoid whipsaw on dead-cat bounces:
      - Enter equity_bear: SPY closes below its 200-day SMA (qqq_regime_ok=False)
      - Exit equity_bear: SPY > SMA50 > SMA200 (full uptrend reconfirmed)

    Without hysteresis, a 3-day SPY > SMA200 blip during a bear (April,
    August, November 2022 each had such blips) would flip the system back
    to full-universe scan, where it then caught faux dawn breakouts and
    bled (-$132 on AVAX/GOOGL/PLTR in iter-4 backtest).

    Disjoint from shield_active: if both true, SHIELD wins (no entries).
    If only equity_bear, we scan the defensive subset with 0.5× sizing.

    Missing keys: default to bull (qqq_regime_ok=True, qqq_full_uptrend=True)
    → returns False (no false-positive rotation).
    """
    qqq_ok = macro.get("qqq_regime_ok", True)
    if not qqq_ok:
        return True                              # below SMA200: enter bear
    # Above SMA200 but not yet full uptrend → stay in bear (sticky exit)
    full_uptrend = macro.get("qqq_full_uptrend", True)
    return not full_uptrend

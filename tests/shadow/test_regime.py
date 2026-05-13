"""Tests for shadow/regime.py — SHIELD + equity_bear truth tables."""
from shadow.regime import shield_active, equity_bear_active


def test_normal_market_no_shield():
    """VIX=18, BTC bull, QQQ ok → no SHIELD."""
    macro = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": True}
    assert shield_active(macro) is False


def test_high_vix_triggers_shield():
    """VIX > 30 → SHIELD regardless of other signals."""
    macro = {"vix": 31.0, "btc_trend": "bull", "qqq_regime_ok": True}
    assert shield_active(macro) is True


def test_vix_at_threshold_no_shield():
    """VIX = 30 exactly → no SHIELD (strict >)."""
    macro = {"vix": 30.0, "btc_trend": "bull", "qqq_regime_ok": True}
    assert shield_active(macro) is False


def test_btc_bear_and_qqq_bad_triggers_shield():
    """BTC bear AND QQQ < SMA200 → SHIELD."""
    macro = {"vix": 18.0, "btc_trend": "bear", "qqq_regime_ok": False}
    assert shield_active(macro) is True


def test_btc_bear_alone_no_shield():
    """BTC bear but QQQ ok → no SHIELD (both required)."""
    macro = {"vix": 18.0, "btc_trend": "bear", "qqq_regime_ok": True}
    assert shield_active(macro) is False


def test_qqq_bad_alone_no_shield():
    """QQQ bad but BTC bull → no SHIELD (both required)."""
    macro = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": False}
    assert shield_active(macro) is False


def test_missing_keys_safe_defaults():
    """Missing macro keys → assume neutre (no SHIELD)."""
    assert shield_active({}) is False
    assert shield_active({"vix": 18}) is False


# ── equity_bear_active (asymmetric hysteresis) ──────────────────────────────
def test_equity_bear_fires_when_qqq_below_sma200():
    """SPY below SMA200 → enter equity bear (rotate to defensives)."""
    assert equity_bear_active({"qqq_regime_ok": False}) is True


def test_equity_bear_clears_only_on_full_uptrend():
    """SPY above SMA200 AND SPY > SMA50 > SMA200 → exit equity bear."""
    macro = {"qqq_regime_ok": True, "qqq_full_uptrend": True}
    assert equity_bear_active(macro) is False


def test_equity_bear_sticks_on_partial_recovery():
    """SPY above SMA200 but SMA50 still below SMA200 → stay in bear (hysteresis).

    This is the key anti-whipsaw: in 2022, SPY briefly crossed back above
    SMA200 in April / August / November but the SMA50 stayed below SMA200
    (downtrend not yet broken). Hysteresis keeps the rotation active.
    """
    macro = {"qqq_regime_ok": True, "qqq_full_uptrend": False}
    assert equity_bear_active(macro) is True


def test_equity_bear_default_no_trigger():
    """Missing keys → assume bull, prevents false-positive rotation."""
    assert equity_bear_active({}) is False
    assert equity_bear_active({"vix": 22, "btc_trend": "bull"}) is False

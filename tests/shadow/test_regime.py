"""Tests for shadow/regime.py — SHIELD truth table."""
from shadow.regime import shield_active


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

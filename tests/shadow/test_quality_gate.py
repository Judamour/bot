"""Tests for shadow/quality_gate.py — 4 hard gates."""
from datetime import datetime, timezone

import pytest

from shadow.scorer import Signal
from shadow.quality_gate import passes, reject_reason
from shadow.risk_guard import RiskGuard


@pytest.fixture
def t0():
    return datetime(2026, 5, 12, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def rg(tmp_path, t0):
    return RiskGuard.load(str(tmp_path / "rs.json"), initial_equity=100_000.0, now=t0)


def _sig(score=80, mtf=True, vol=1.5, symbol="NVDA"):
    return Signal(
        symbol=symbol,
        strategy="supertrend",
        side="long",
        entry_price=100.0,
        atr=2.0,
        stop_price=97.0,
        rationale={"mtf_aligned": mtf, "volume_ratio": vol, "adx": 30, "rsi": 55},
        score=score,
    )


def test_strong_signal_passes(rg, t0):
    """High score + MTF + volume + no cooldown → pass."""
    assert passes(_sig(), rg, now=t0) is True


def test_score_below_floor_fails(rg, t0):
    """Score < SCORE_FLOOR (65) fails G1."""
    assert passes(_sig(score=60), rg, now=t0) is False


def test_score_exactly_at_floor_passes(rg, t0):
    """Score == SCORE_FLOOR passes (≥, not strict)."""
    assert passes(_sig(score=65), rg, now=t0) is True


def test_mtf_not_aligned_fails(rg, t0):
    """mtf_aligned False fails G2."""
    assert passes(_sig(mtf=False), rg, now=t0) is False


def test_low_volume_fails(rg, t0):
    """volume_ratio < 1.0 fails G3."""
    assert passes(_sig(vol=0.8), rg, now=t0) is False


def test_volume_exactly_at_floor_passes(rg, t0):
    """volume_ratio == 1.0 passes (≥)."""
    assert passes(_sig(vol=1.0), rg, now=t0) is True


def test_symbol_in_cooldown_fails(rg, t0):
    """Cooldown on the symbol → fail G4."""
    rg.register_stop("NVDA", pnl=-200, now=t0)
    assert passes(_sig(symbol="NVDA"), rg, now=t0) is False
    # Different symbol still passes
    assert passes(_sig(symbol="GOOGL"), rg, now=t0) is True


def test_missing_rationale_keys_fail_safe(rg, t0):
    """If a detector omits mtf_aligned or volume_ratio, gate must NOT silently pass."""
    sig = Signal(
        symbol="X", strategy="momentum", side="long",
        entry_price=100, atr=2, stop_price=98,
        rationale={},  # missing both keys
        score=80,
    )
    assert passes(sig, rg, now=t0) is False


# ── reject_reason (iter-8 audit) ────────────────────────────────────────────
def test_reject_reason_returns_none_on_pass(rg, t0):
    """Passing signal → None (no reason to reject)."""
    assert reject_reason(_sig(), rg, now=t0) is None


def test_reject_reason_g1_score(rg, t0):
    assert reject_reason(_sig(score=60), rg, now=t0) == "G1_score"


def test_reject_reason_g2_mtf(rg, t0):
    assert reject_reason(_sig(mtf=False), rg, now=t0) == "G2_mtf"


def test_reject_reason_g3_volume(rg, t0):
    assert reject_reason(_sig(vol=0.5), rg, now=t0) == "G3_volume"


def test_reject_reason_g4_cooldown(rg, t0):
    rg.register_stop("NVDA", pnl=-200, now=t0)
    assert reject_reason(_sig(symbol="NVDA"), rg, now=t0) == "G4_cooldown"


def test_reject_reason_g1_priority(rg, t0):
    """Multiple failures → G1 (score) reported first."""
    rg.register_stop("NVDA", pnl=-200, now=t0)
    sig = _sig(score=50, mtf=False, vol=0.5, symbol="NVDA")
    assert reject_reason(sig, rg, now=t0) == "G1_score"

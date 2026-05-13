"""Tests for shadow/sizing.py — score-weighted position sizing.

Tests are parameterized on WEIGHT_BY_RANK so they survive tuning changes.
"""
import pytest
from shadow.sizing import compute_size, SizeResult
from shadow.constants_v2 import WEIGHT_BY_RANK, TARGET_DAILY_VOL


@pytest.mark.parametrize("rank", range(len(WEIGHT_BY_RANK)))
def test_each_rank_uses_its_weight(rank: int):
    expected_notional = 100_000.0 * WEIGHT_BY_RANK[rank]
    res = compute_size(rank=rank, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(expected_notional)
    assert res.qty == pytest.approx(expected_notional / 100.0)


def test_rank_out_of_range_returns_zero():
    """rank >= len(WEIGHT_BY_RANK) → no position."""
    res = compute_size(rank=len(WEIGHT_BY_RANK), cash=100_000.0, entry_price=100.0)
    assert res.qty == 0.0


def test_zero_cash_returns_zero():
    res = compute_size(rank=0, cash=0.0, entry_price=100.0)
    assert res.qty == 0.0


def test_zero_entry_price_returns_zero():
    """Defensive: bad price should not crash with div-by-zero."""
    res = compute_size(rank=0, cash=100_000.0, entry_price=0.0)
    assert res.qty == 0.0


def test_total_top_n_leaves_cash_buffer():
    """Sum of top-N weights should be < 1.0 (cash buffer for fees + slippage)."""
    total_pct = sum(WEIGHT_BY_RANK)
    total_notional = sum(compute_size(r, 100_000.0, 100.0).notional for r in range(len(WEIGHT_BY_RANK)))
    assert total_notional == pytest.approx(100_000.0 * total_pct)
    assert total_pct < 1.0, f"WEIGHT_BY_RANK total {total_pct:.2f} leaves no cash buffer"


# ── Vol-adjusted sizing (iter-5) ─────────────────────────────────────────────
def test_vol_adjust_high_vol_asset_gets_smaller_position():
    """ATR/price = 3% (BTC-like) → scale by 1.5/3.0 = 0.5 → half the position."""
    base = compute_size(rank=0, cash=100_000.0, entry_price=100.0)
    # ATR = 3.0 on $100 price → asset_vol_pct = 3.0% (high)
    scaled = compute_size(rank=0, cash=100_000.0, entry_price=100.0, atr=3.0)
    assert scaled.notional < base.notional
    expected_factor = TARGET_DAILY_VOL / 0.03  # 1.5% / 3.0% = 0.5
    assert scaled.notional == pytest.approx(base.notional * expected_factor)


def test_vol_adjust_low_vol_asset_capped_at_1():
    """ATR/price = 0.5% (GLD-like) → would scale to 3x, but capped at 1.0 (no leverage)."""
    base = compute_size(rank=0, cash=100_000.0, entry_price=100.0)
    # ATR = 0.5 on $100 price → asset_vol_pct = 0.5% (low)
    scaled = compute_size(rank=0, cash=100_000.0, entry_price=100.0, atr=0.5)
    assert scaled.notional == pytest.approx(base.notional)  # no upscale beyond 1.0


def test_vol_adjust_pass_through_when_atr_none():
    """atr=None → behaves like the original score-weighted sizing."""
    base = compute_size(rank=0, cash=100_000.0, entry_price=100.0)
    with_none = compute_size(rank=0, cash=100_000.0, entry_price=100.0, atr=None)
    assert with_none.notional == base.notional

"""Tests for shadow/sizing.py — score-weighted position sizing."""
import pytest
from shadow.sizing import compute_size, SizeResult


def test_top_1_gets_30_pct():
    res = compute_size(rank=0, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(30_000.0)
    assert res.qty == pytest.approx(300.0)


def test_top_2_gets_20_pct():
    res = compute_size(rank=1, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(20_000.0)


def test_top_3_gets_15_pct():
    res = compute_size(rank=2, cash=100_000.0, entry_price=100.0)
    assert res.notional == pytest.approx(15_000.0)


def test_rank_out_of_range_returns_zero():
    """rank >= 3 → no position."""
    res = compute_size(rank=3, cash=100_000.0, entry_price=100.0)
    assert res.qty == 0.0


def test_zero_cash_returns_zero():
    res = compute_size(rank=0, cash=0.0, entry_price=100.0)
    assert res.qty == 0.0


def test_zero_entry_price_returns_zero():
    """Defensive: bad price should not crash with div-by-zero."""
    res = compute_size(rank=0, cash=100_000.0, entry_price=0.0)
    assert res.qty == 0.0


def test_total_top_3_capped_at_65_pct():
    """Sum of top-3 weights = 30 + 20 + 15 = 65, leaves 35% cash buffer."""
    total = sum(compute_size(r, 100_000.0, 100.0).notional for r in range(3))
    assert total == pytest.approx(65_000.0)

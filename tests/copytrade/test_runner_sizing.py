"""Pure-function tests of compute_paper_size — no IO."""
from live.copytrade.runner import compute_paper_size


def test_basic_proportional():
    """Target uses 5% of AUM → bot uses 5% of paper capital."""
    out = compute_paper_size(trade_size_usd=5_000, target_aum=100_000,
                             capital_per_wallet=333.33)
    assert abs(out - 333.33 * 0.05) < 1e-6  # 16.67


def test_clamp_at_50_percent():
    """Target uses 90% of AUM → bot clamped to 50%."""
    out = compute_paper_size(trade_size_usd=90_000, target_aum=100_000,
                             capital_per_wallet=333.33)
    assert abs(out - 333.33 * 0.5) < 1e-6


def test_zero_aum_returns_zero():
    """AUM unknown → skip (size 0)."""
    out = compute_paper_size(trade_size_usd=100, target_aum=0,
                             capital_per_wallet=333.33)
    assert out == 0.0


def test_negative_inputs_return_zero():
    assert compute_paper_size(-1, 100, 333) == 0.0
    assert compute_paper_size(1, -1, 333) == 0.0

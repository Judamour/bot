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


from unittest.mock import patch

import pytest

from live.copytrade import runner as runner_mod
from live.copytrade.paper_portfolio import PaperPortfolio


def _trade(ts, side, size, price, condition="0xC", asset="42", outcome_index=0):
    return {
        "timestamp": ts, "side": side, "size": size, "price": price,
        "conditionId": condition, "asset": asset, "outcomeIndex": outcome_index,
        "outcome": "Yes", "title": "Test market",
        "transactionHash": f"0x{ts:x}",
    }


def test_process_new_buy_creates_position(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    last_seen = 0
    trades = [_trade(ts=100, side="BUY", size=500, price=0.5)]

    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=10_000.0):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen, capital_per_wallet=1000.0,
        )

    assert new_last_seen == 100
    assert len(decisions) == 1
    assert decisions[0]["action"] == "executed"
    # trade_pct = (500 * 0.5) / 10_000 = 0.025 → paper_size = 25
    assert pf.cash_usd == pytest.approx(1000.0 - 25.0)
    assert len(pf.positions) == 1


def test_skip_already_seen(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    trades = [_trade(ts=50, side="BUY", size=500, price=0.5)]

    with patch("live.copytrade.runner.data_api.trades", return_value=[]):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=100, capital_per_wallet=1000.0,
        )

    assert new_last_seen == 100
    assert decisions == []


def test_sell_reduces_position(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    # Seed a position the target also has (10 shares at 0.5)
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=5.0, target_hash="0xseed",
           market_title="m", opened_ts=50)

    # Target sells 50% of its on-chain position. Mock target_position_size_at
    # to return its full pre-sell size = 100 shares. Trade sells 50 → frac = 0.5.
    trades = [_trade(ts=200, side="SELL", size=50, price=0.7)]

    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.data_api.target_position_size_at",
               return_value=100.0), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=10_000.0):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=0, capital_per_wallet=1000.0,
        )

    # Paper position size was 10 (5 USD / 0.5), half sold → 5 left
    assert pf.positions[0]["size"] == pytest.approx(5.0)
    assert new_last_seen == 200
    assert decisions[0]["action"] == "executed"

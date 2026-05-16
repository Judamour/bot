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
            target, pf, last_seen,
        )

    assert new_last_seen == 100
    assert len(decisions) == 1
    assert decisions[0]["action"] == "executed"
    # trade_pct = (500 * 0.5) / 10_000 = 0.025 → paper_size = 25 (sur $1000 equity)
    assert pf.cash_usd == pytest.approx(1000.0 - 25.0)
    assert len(pf.positions) == 1


def test_skip_already_seen(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)

    with patch("live.copytrade.runner.data_api.trades", return_value=[]):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=100,
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
            target, pf, last_seen_ts=0,
        )

    # Paper position size was 10 (5 USD / 0.5), half sold → 5 left
    assert pf.positions[0]["size"] == pytest.approx(5.0)
    assert new_last_seen == 200
    assert decisions[0]["action"] == "executed"


def test_bootstrap_cutoff_skips_old_trades(tmp_path):
    """When last_seen_ts == 0 (first boot), only trades within BOOTSTRAP_CUTOFF_S
    must be considered — avoids replaying historical PnL retroactively."""
    import time as _time
    now = int(_time.time())
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    captured = {}

    def fake_trades(wallet, limit=50, since_ts=None):
        captured["since_ts"] = since_ts
        return []

    with patch("live.copytrade.runner.data_api.trades", side_effect=fake_trades):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=0,
        )

    # since_ts must be ~now - BOOTSTRAP_CUTOFF_S (within 5s for clock slack)
    expected_floor = now - runner_mod.BOOTSTRAP_CUTOFF_S
    assert captured["since_ts"] is not None
    assert abs(captured["since_ts"] - expected_floor) < 5
    # And on empty response, last_seen_ts must advance to the cutoff so we
    # don't keep re-applying it forever.
    assert new_last_seen >= expected_floor - 1


def test_bootstrap_cutoff_does_not_apply_when_resuming(tmp_path):
    """When last_seen_ts > 0, the cutoff must NOT override it (continuity)."""
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    captured = {}

    def fake_trades(wallet, limit=50, since_ts=None):
        captured["since_ts"] = since_ts
        return []

    with patch("live.copytrade.runner.data_api.trades", side_effect=fake_trades):
        runner_mod.process_wallet(
            target, pf, last_seen_ts=12345,
        )

    assert captured["since_ts"] == 12345  # untouched


def test_insufficient_cash_skips_buy(tmp_path):
    """A BUY whose paper_size exceeds current cash is skipped, not executed."""
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=10.0)  # only $10 left, equity_at_cost = $10
    # Trade that would compute to a big paper_size: target trades 5000 @ $0.5
    # with AUM 1000 → trade_pct = 2500/1000 = 2.5, clamped to 0.5
    # paper_size = equity_at_cost * 0.5 = $5 → fits in $10 cash, but let's
    # construct a wallet where cost-based equity > cash to trigger insufficient_cash.
    pf.buy(condition_id="0xSeed", asset="seed", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=90.0, target_hash="0xseed",
           market_title="m", opened_ts=10)
    # Now: cash=10 - 90 = WAIT, cash would go negative. Let me redo.
    # Reset: cash=10, then no seed buy. Use a giant trade so cap of 50% of
    # equity_at_cost ($10) → $5. Still fits. Need to make cost-equity > cash.
    # Easiest: seed cash=100, then BUY seed for $90 → cash=10, cost=90, equity=100.
    # Then attempt copy whose paper_size = 50%*$100 = $50 > $10 cash → skipped.
    pf2 = PaperPortfolio(wallet="T1", cash_usd=100.0)
    pf2.buy(condition_id="0xSeed", asset="seed", outcome="Yes", outcome_index=0,
            price=0.5, usd_size=90.0, target_hash="0xseed",
            market_title="m", opened_ts=10)
    assert pf2.cash_usd == pytest.approx(10.0)
    assert pf2.equity_at_cost() == pytest.approx(100.0)

    trades = [_trade(ts=100, side="BUY", size=5000, price=0.5)]
    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=1000.0):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf2, last_seen_ts=50,
        )

    assert new_last_seen == 100
    # Last decision is the copy attempt (skipped for insufficient_cash)
    copy_decisions = [d for d in decisions if d.get("ts") == 100]
    assert len(copy_decisions) == 1
    assert copy_decisions[0]["action"] == "skipped"
    assert "insufficient_cash" in copy_decisions[0]["rationale"]
    # Cash untouched (still $10 after the seed buy)
    assert pf2.cash_usd == pytest.approx(10.0)
    # Position count unchanged (only the seed)
    assert len(pf2.positions) == 1


def test_sufficient_cash_allows_buy(tmp_path):
    """Sanity counterpart: when cash >= paper_size, BUY proceeds normally."""
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    trades = [_trade(ts=100, side="BUY", size=500, price=0.5)]  # 250 USD trade

    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=10_000.0):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=50,
        )

    # trade_pct = 250/10000 = 0.025 → paper_size = $25 (sur $1000 equity)
    assert decisions[0]["action"] == "executed"
    assert pf.cash_usd == pytest.approx(975.0)


def test_sizing_uses_live_equity_not_initial(tmp_path):
    """KEY: après réalisation de PnL, sizing scale avec l'equity courante,
    pas avec un capital initial figé. C'est le coeur du fix."""
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    # Wallet a perdu 50% sur des positions résolues : cash 500, pas de position
    pf = PaperPortfolio(wallet="T1", cash_usd=500.0)
    pf.realized_pnl_usd = -500.0
    assert pf.equity_at_cost() == pytest.approx(500.0)

    # Target met 10% de son AUM → on doit mettre 10% de $500 = $50 (pas 10% de $1000)
    trades = [_trade(ts=100, side="BUY", size=200, price=0.5)]  # 100 USD trade

    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=1000.0):
        _, decisions = runner_mod.process_wallet(target, pf, last_seen_ts=0)

    # trade_pct = 100/1000 = 0.10 → paper_size = 500 * 0.10 = $50
    assert decisions[0]["paper_size_usd"] == pytest.approx(50.0)
    assert pf.cash_usd == pytest.approx(450.0)

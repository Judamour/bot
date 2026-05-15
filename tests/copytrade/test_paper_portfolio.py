"""Paper portfolio mechanics — buy adds, sell reduces, MTM uses current prices."""
import pytest

from live.copytrade.paper_portfolio import PaperPortfolio


@pytest.fixture
def pf():
    return PaperPortfolio(wallet="RN1", cash_usd=333.33)


def test_initial_state(pf):
    assert pf.cash_usd == 333.33
    assert pf.positions == []
    assert pf.realized_pnl_usd == 0.0


def test_buy_creates_position(pf):
    pf.buy(
        condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
        price=0.5, usd_size=10.0, target_hash="0xtxA",
        market_title="Test market", opened_ts=1000,
    )
    assert pf.cash_usd == pytest.approx(323.33)
    assert len(pf.positions) == 1
    p = pf.positions[0]
    assert p["size"] == pytest.approx(20.0)        # 10 USD / 0.5 per share
    assert p["avg_price"] == 0.5
    assert p["cost_usd"] == 10.0


def test_buy_adds_to_existing_position(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.6, usd_size=12.0, target_hash="0xtxB",
           market_title="m", opened_ts=2000)
    assert len(pf.positions) == 1
    p = pf.positions[0]
    assert p["size"] == pytest.approx(40.0)        # 20 + 20
    assert p["cost_usd"] == pytest.approx(22.0)
    assert p["avg_price"] == pytest.approx(22.0 / 40.0)


def test_sell_fraction_reduces_position(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    # sell half at 0.7
    pf.sell(condition_id="0xC", outcome_index=0, fraction=0.5, price=0.7,
            target_hash="0xtxB", ts=2000)
    p = pf.positions[0]
    assert p["size"] == pytest.approx(10.0)
    # Proceeds = 10 * 0.7 = 7.0, cost basis sold = 5.0, realized PnL = +2.0
    assert pf.cash_usd == pytest.approx(323.33 + 7.0)
    assert pf.realized_pnl_usd == pytest.approx(2.0)


def test_sell_full_removes_position(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    pf.sell(condition_id="0xC", outcome_index=0, fraction=1.0, price=0.6,
            target_hash="0xtxB", ts=2000)
    assert pf.positions == []
    assert pf.realized_pnl_usd == pytest.approx(2.0)  # 20 * 0.6 - 10 = 2


def test_sell_more_than_owned_clamps(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    pf.sell(condition_id="0xC", outcome_index=0, fraction=2.0, price=0.7,
            target_hash="0xtxB", ts=2000)
    assert pf.positions == []  # fully closed
    assert pf.realized_pnl_usd == pytest.approx(20 * 0.7 - 10)


def test_sell_unknown_position_noop(pf):
    pf.sell(condition_id="0xC", outcome_index=0, fraction=1.0, price=0.7,
            target_hash="0xtxB", ts=1000)
    assert pf.positions == []
    assert pf.cash_usd == 333.33
    assert pf.realized_pnl_usd == 0.0


def test_mtm_equity_with_current_prices(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    eq = pf.equity({"42": 0.7})
    # cash 323.33 + 20 shares * 0.7 = 323.33 + 14 = 337.33
    assert eq == pytest.approx(337.33)


def test_mtm_uses_avg_price_when_no_quote(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    eq = pf.equity({})
    # no current price → fall back to avg_price 0.5 → 323.33 + 10 = 333.33
    assert eq == pytest.approx(333.33)


def test_to_dict_roundtrip(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    d = pf.to_dict()
    pf2 = PaperPortfolio.from_dict(d)
    assert pf2.cash_usd == pf.cash_usd
    assert pf2.positions == pf.positions
    assert pf2.realized_pnl_usd == pf.realized_pnl_usd

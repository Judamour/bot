"""polymarket_executor — paper/live routing + double opt-in safety."""
import os
from unittest.mock import patch, MagicMock

import pytest

from live.copytrade import polymarket_executor as pe


@pytest.fixture(autouse=True)
def _reset_client():
    pe.reset_client_for_test()
    yield
    pe.reset_client_for_test()


@pytest.fixture
def env_paper():
    """Mode paper trading explicit."""
    with patch.dict(os.environ, {"PAPER_TRADING": "true",
                                  "LIVE_POLYMARKET": "false"}, clear=False):
        yield


@pytest.fixture
def env_live():
    """Mode live activé (PAPER_TRADING=false + LIVE_POLYMARKET=true)."""
    with patch.dict(os.environ, {
        "PAPER_TRADING": "false",
        "LIVE_POLYMARKET": "true",
        "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
        "POLYMARKET_FUNDER_ADDRESS": "0x" + "cd" * 20,
        "POLYMARKET_SIGNATURE_TYPE": "1",
    }, clear=False):
        yield


@pytest.fixture
def env_half_opt_in():
    """PAPER_TRADING=false MAIS LIVE_POLYMARKET=false → reste safe (no-op)."""
    with patch.dict(os.environ, {"PAPER_TRADING": "false",
                                  "LIVE_POLYMARKET": "false"}, clear=False):
        yield


# ── Mode detection ───────────────────────────────────────────────────────────


def test_is_live_requires_double_opt_in(env_half_opt_in):
    """PAPER_TRADING=false seul ne suffit PAS à activer le live (sécurité)."""
    assert pe.is_live() is False


def test_is_live_paper_mode(env_paper):
    assert pe.is_live() is False


def test_is_live_full_opt_in(env_live):
    assert pe.is_live() is True


# ── Paper mode = no-op ───────────────────────────────────────────────────────


def test_paper_execute_buy_is_noop_no_client_call(env_paper):
    """Mode paper : pas d'init de client, retourne success cosmétique."""
    with patch("live.copytrade.polymarket_executor._build_client") as bc:
        r = pe.execute_buy(token_id="42", usd_size=100.0, target_price=0.5)
    assert r.success
    assert r.order_id == "paper"
    bc.assert_not_called()


def test_paper_execute_sell_is_noop(env_paper):
    with patch("live.copytrade.polymarket_executor._build_client") as bc:
        r = pe.execute_sell(token_id="42", shares_size=200.0, target_price=0.6)
    assert r.success
    assert r.filled_shares == 200.0
    assert r.cost_usd == pytest.approx(120.0)
    bc.assert_not_called()


def test_paper_check_balance_returns_zero(env_paper):
    assert pe.check_balance() == 0.0


def test_paper_startup_check_passes_without_creds(env_paper):
    """Aucune clé Polymarket nécessaire en paper."""
    assert pe.startup_check() is True


def test_half_opt_in_stays_safe(env_half_opt_in):
    """PAPER_TRADING=false sans LIVE_POLYMARKET=true → toujours no-op."""
    with patch("live.copytrade.polymarket_executor._build_client") as bc:
        r = pe.execute_buy(token_id="42", usd_size=100.0, target_price=0.5)
    assert r.success and r.order_id == "paper"
    bc.assert_not_called()


# ── Live mode : init client + place order ────────────────────────────────────


def test_live_missing_credentials_raises(env_live, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYMARKET_PRIVATE_KEY"):
        pe._build_client()


def test_live_execute_buy_signs_and_posts(env_live):
    fake_client = MagicMock()
    fake_client.get_price.return_value = {"price": "0.50"}
    fake_client.create_order.return_value = "SIGNED_ORDER_OBJ"
    fake_client.post_order.return_value = {
        "success": True, "orderID": "0xORDER123",
        "makingAmount": str(int(200 * 1e6)),   # 200 shares
        "takingAmount": str(int(102 * 1e6)),   # $102 cost
    }
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        r = pe.execute_buy(token_id="42", usd_size=100.0, target_price=0.50)

    assert r.success
    assert r.order_id == "0xORDER123"
    assert r.filled_shares == pytest.approx(200.0)
    assert r.cost_usd == pytest.approx(102.0)
    assert r.filled_price == pytest.approx(0.51)
    # Verify create_order called with limit price including slippage
    args, kwargs = fake_client.create_order.call_args
    order_args = args[0]
    assert order_args.side == "BUY"
    assert order_args.token_id == "42"
    # base=max(0.50, 0.50)=0.50, *(1+0.02)=0.51
    assert order_args.price == pytest.approx(0.51)


def test_live_execute_buy_rejected_returns_failure(env_live):
    fake_client = MagicMock()
    fake_client.get_price.return_value = {"price": "0.50"}
    fake_client.create_order.return_value = "SIGNED_ORDER_OBJ"
    fake_client.post_order.return_value = {"success": False,
                                            "errorMsg": "insufficient balance"}
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        r = pe.execute_buy(token_id="42", usd_size=100.0, target_price=0.50)

    assert not r.success
    assert "insufficient balance" in r.error


def test_live_execute_buy_invalid_inputs_returns_failure(env_live):
    r = pe.execute_buy(token_id="42", usd_size=0, target_price=0.5)
    assert not r.success
    assert "invalid" in r.error.lower()


def test_live_execute_buy_too_small_skips(env_live):
    """USD trop petit pour faire ≥ 1 share au limit price."""
    fake_client = MagicMock()
    fake_client.get_price.return_value = {"price": "0.95"}
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        # 0.50 / 0.97 ≈ 0.51 shares < 1
        r = pe.execute_buy(token_id="42", usd_size=0.50, target_price=0.95)
    assert not r.success
    assert "too small" in r.error


def test_live_execute_sell_signs_and_posts(env_live):
    fake_client = MagicMock()
    fake_client.get_price.return_value = {"price": "0.60"}
    fake_client.create_order.return_value = "SIGNED_ORDER_OBJ"
    fake_client.post_order.return_value = {
        "success": True, "orderID": "0xS456",
        "makingAmount": str(int(100 * 1e6)),
        "takingAmount": str(int(58.80 * 1e6)),
    }
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        r = pe.execute_sell(token_id="42", shares_size=100.0, target_price=0.60)

    assert r.success
    assert r.order_id == "0xS456"
    assert r.filled_shares == pytest.approx(100.0)
    assert r.cost_usd == pytest.approx(58.80)
    args, _ = fake_client.create_order.call_args
    assert args[0].side == "SELL"
    # base=max(0.60, 0.60)=0.60, *(1-0.02)=0.588
    assert args[0].price == pytest.approx(0.588)


def test_live_check_balance_parses_usdc(env_live):
    fake_client = MagicMock()
    fake_client.get_balance_allowance.return_value = {
        "balance": str(int(1234.56 * 1e6)),
        "allowance": str(int(10000 * 1e6)),
    }
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        bal = pe.check_balance()
    assert bal == pytest.approx(1234.56)


def test_live_startup_check_initializes_client(env_live):
    fake_client = MagicMock()
    fake_client.get_balance_allowance.return_value = {"balance": str(int(500 * 1e6))}
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        assert pe.startup_check() is True


def test_live_startup_check_fails_gracefully(env_live):
    with patch("live.copytrade.polymarket_executor._build_client",
               side_effect=RuntimeError("missing creds")):
        assert pe.startup_check() is False


def test_live_cancel_order(env_live):
    fake_client = MagicMock()
    fake_client.cancel.return_value = {"canceled": True}
    with patch("live.copytrade.polymarket_executor._build_client",
               return_value=fake_client):
        assert pe.cancel_order("0xORDER123") is True
    fake_client.cancel.assert_called_once_with("0xORDER123")


def test_paper_cancel_order_noop():
    """Paper : cancel always succeeds cosmétiquement."""
    with patch.dict(os.environ, {"PAPER_TRADING": "true"}, clear=False):
        assert pe.cancel_order("foo") is True

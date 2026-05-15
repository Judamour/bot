"""Tests for live/copytrade/data_api.py — mocked-HTTP behaviour."""
import json
from unittest.mock import patch, MagicMock

import pytest

from live.copytrade import data_api


def _mock_resp(body, status=200):
    m = MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = status
    m.read.return_value = json.dumps(body).encode()
    return m


def test_get_returns_parsed_json():
    payload = [{"k": "v"}]
    with patch("urllib.request.urlopen", return_value=_mock_resp(payload)):
        out = data_api._get("https://example.com/x")
    assert out == payload


def test_get_sends_required_headers():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return _mock_resp([])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        data_api._get("https://example.com/x")
    # urllib title-cases header names
    assert captured["headers"].get("Origin") == "https://polymarket.com"
    assert captured["headers"].get("Referer") == "https://polymarket.com/"
    assert "Mozilla" in captured["headers"].get("User-agent", "")


def test_trades_url_format():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _mock_resp([{"timestamp": 1, "side": "BUY"}])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = data_api.trades("0xABC", limit=50)
    assert "user=0xabc" in captured["url"].lower()
    assert "limit=50" in captured["url"]
    assert isinstance(out, list)


def test_trades_since_filters_older():
    body = [
        {"timestamp": 100, "side": "BUY", "transactionHash": "0xa"},
        {"timestamp": 200, "side": "SELL", "transactionHash": "0xb"},
        {"timestamp": 50,  "side": "BUY",  "transactionHash": "0xc"},
    ]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        out = data_api.trades("0xABC", since_ts=100)
    # since_ts is strictly greater-than → 50 and 100 are excluded
    hashes = [t["transactionHash"] for t in out]
    assert hashes == ["0xb"]


def test_positions_returns_list():
    body = [{"conditionId": "0xC", "size": 10, "curPrice": 0.5, "currentValue": 5}]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        out = data_api.positions("0xABC")
    assert out == body


def test_value_returns_scalar():
    body = [{"user": "0xabc", "value": 1234.56}]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        v = data_api.value("0xABC")
    assert v == pytest.approx(1234.56)


def test_value_handles_empty():
    with patch("urllib.request.urlopen", return_value=_mock_resp([])):
        v = data_api.value("0xABC")
    assert v == 0.0


def test_price_url_and_response():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _mock_resp({"price": "0.62"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        p = data_api.price(token_id="42", side="BUY")
    assert "token_id=42" in captured["url"]
    assert "side=BUY" in captured["url"]
    assert p == pytest.approx(0.62)


def test_price_handles_missing():
    with patch("urllib.request.urlopen", return_value=_mock_resp({})):
        p = data_api.price(token_id="42", side="BUY")
    assert p is None


def test_target_position_size_at_basic():
    """Sum signed sizes up to ts."""
    body = [
        # BUYs add, SELLs subtract; sorted desc by API but our func sorts asc internally
        {"timestamp": 100, "side": "BUY",  "size": 10, "conditionId": "0xC", "outcomeIndex": 0},
        {"timestamp": 200, "side": "BUY",  "size": 5,  "conditionId": "0xC", "outcomeIndex": 0},
        {"timestamp": 300, "side": "SELL", "size": 3,  "conditionId": "0xC", "outcomeIndex": 0},
        {"timestamp": 150, "side": "BUY",  "size": 7,  "conditionId": "0xOTHER", "outcomeIndex": 0},
    ]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        size = data_api.target_position_size_at("0xW", "0xC", outcome_index=0, ts=250)
    assert size == pytest.approx(15)  # 10 + 5, SELL at 300 not included


def test_target_position_size_at_zero_when_none_before():
    with patch("urllib.request.urlopen", return_value=_mock_resp([])):
        size = data_api.target_position_size_at("0xW", "0xC", outcome_index=0, ts=999)
    assert size == 0.0

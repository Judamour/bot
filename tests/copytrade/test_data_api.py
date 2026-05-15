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

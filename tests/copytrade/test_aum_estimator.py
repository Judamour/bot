"""AUM estimator — value + positions composition + cache TTL."""
import time
from unittest.mock import patch

from live.copytrade import aum_estimator


def test_aum_uses_value_endpoint_when_positive():
    """If /value returns a positive number, trust it directly."""
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=5000.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 5000.0


def test_aum_falls_back_to_positions_sum_when_value_zero():
    """If /value returns 0 but positions show value, sum them."""
    positions = [
        {"size": 100, "curPrice": 0.3, "currentValue": 30.0},
        {"size": 50,  "curPrice": 0.8, "currentValue": 40.0},
    ]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=positions):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 70.0


def test_aum_zero_when_no_data():
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 0.0


def test_cache_hit_skips_api_calls():
    """Two calls within TTL — second uses cache (fallback path with positions)."""
    aum_estimator.clear_cache()
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0) as mv, \
         patch("live.copytrade.aum_estimator.data_api.positions",
               return_value=[{"currentValue": 1000.0}]) as mp:
        a1 = aum_estimator.aum("0xW", _cache_ttl=60)
        a2 = aum_estimator.aum("0xW", _cache_ttl=60)
    assert a1 == a2 == 1000.0
    assert mv.call_count == 1
    assert mp.call_count == 1


def test_cache_expires():
    aum_estimator.clear_cache()
    t = [1000.0]

    def fake_value(_):
        t[0] += 100
        return t[0]

    with patch("live.copytrade.aum_estimator.data_api.value", side_effect=fake_value), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]), \
         patch("live.copytrade.aum_estimator.time.time", side_effect=[0, 70]):
        a1 = aum_estimator.aum("0xW", _cache_ttl=60)
        a2 = aum_estimator.aum("0xW", _cache_ttl=60)
    assert a1 == 1100.0
    assert a2 == 1200.0  # cache expired between calls

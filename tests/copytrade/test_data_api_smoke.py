"""Integration smoke test — hits real Polymarket Data API. Skipped by default."""
import pytest

from live.copytrade import data_api
from live.copytrade.targets import TARGETS


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS, ids=[t["pseudonym"] for t in TARGETS])
def test_trades_returns_list_for_target(target):
    out = data_api.trades(target["wallet"], limit=5)
    assert isinstance(out, list)


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS, ids=[t["pseudonym"] for t in TARGETS])
def test_positions_returns_list_for_target(target):
    out = data_api.positions(target["wallet"])
    assert isinstance(out, list)


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS, ids=[t["pseudonym"] for t in TARGETS])
def test_value_returns_scalar_for_target(target):
    v = data_api.value(target["wallet"])
    assert isinstance(v, float)
    assert v >= 0

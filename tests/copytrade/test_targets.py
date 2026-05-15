"""Constants module — verify shape and capital math."""
from live.copytrade.targets import TARGETS, PAPER_CAPITAL_USD, CAPITAL_PER_WALLET


def test_three_targets():
    assert len(TARGETS) == 3


def test_each_target_has_required_fields():
    for t in TARGETS:
        assert set(t.keys()) >= {"pseudonym", "wallet", "allocation_pct"}
        assert t["wallet"].startswith("0x") and len(t["wallet"]) == 42
        assert 0 < t["allocation_pct"] <= 1.0


def test_allocations_sum_to_one():
    total = sum(t["allocation_pct"] for t in TARGETS)
    assert abs(total - 1.0) < 1e-9


def test_paper_capital_positive():
    assert PAPER_CAPITAL_USD > 0


def test_capital_per_wallet_consistent():
    assert abs(CAPITAL_PER_WALLET - PAPER_CAPITAL_USD / len(TARGETS)) < 1e-9


def test_targets_are_the_expected_three():
    pseudos = {t["pseudonym"] for t in TARGETS}
    assert pseudos == {"RN1", "bossoskil1", "surfandturf"}

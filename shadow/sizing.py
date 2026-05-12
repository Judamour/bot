"""Score-weighted sizing helper for shadow v2.

compute_size(rank, cash, entry_price) returns the qty + notional value for
position at given rank (0-indexed) in the cycle's top-3.

Pure function — no I/O, no broker calls. Suitable for backtest and live.
"""
from __future__ import annotations
from dataclasses import dataclass
from shadow.constants_v2 import WEIGHT_BY_RANK


@dataclass
class SizeResult:
    qty: float        # number of units / shares
    notional: float   # qty × entry_price (USD)


def compute_size(rank: int, cash: float, entry_price: float) -> SizeResult:
    """Return the size for the rank-th candidate of the cycle's top-N.

    Args:
        rank: 0-indexed position in the sorted top-N (0 = best score).
        cash: available cash (USD) at the moment of sizing decision.
        entry_price: signal's entry price (USD).

    Returns:
        SizeResult with qty=0.0 if rank is out of range, cash≤0, or price≤0.
    """
    if rank < 0 or rank >= len(WEIGHT_BY_RANK):
        return SizeResult(qty=0.0, notional=0.0)
    if cash <= 0 or entry_price <= 0:
        return SizeResult(qty=0.0, notional=0.0)
    weight = WEIGHT_BY_RANK[rank]
    notional = cash * weight
    qty = notional / entry_price
    return SizeResult(qty=qty, notional=notional)

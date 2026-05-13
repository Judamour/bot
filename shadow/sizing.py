"""Score-weighted sizing helper for shadow v2.

compute_size(rank, cash, entry_price, atr=None) returns the qty + notional
value for position at given rank (0-indexed) in the cycle's top-N.

Pure function — no I/O, no broker calls. Suitable for backtest and live.
"""
from __future__ import annotations
from dataclasses import dataclass
from shadow.constants_v2 import WEIGHT_BY_RANK, TARGET_DAILY_VOL


@dataclass
class SizeResult:
    qty: float        # number of units / shares
    notional: float   # qty × entry_price (USD)


def compute_size(rank: int, cash: float, entry_price: float,
                 atr: float | None = None) -> SizeResult:
    """Return the size for the rank-th candidate of the cycle's top-N.

    Args:
        rank: 0-indexed position in the sorted top-N (0 = best score).
        cash: available cash (USD) at the moment of sizing decision.
        entry_price: signal's entry price (USD).
        atr: optional ATR(14) for vol-adjusted scaling. When provided,
             scales the weight by min(TARGET_DAILY_VOL / asset_vol, 1.0)
             so that high-vol assets (BTC, AVAX) get smaller positions
             for the same risk budget. Capped at 1.0 → no leverage.

    Returns:
        SizeResult with qty=0.0 if rank is out of range, cash≤0, or price≤0.
    """
    if rank < 0 or rank >= len(WEIGHT_BY_RANK):
        return SizeResult(qty=0.0, notional=0.0)
    if cash <= 0 or entry_price <= 0:
        return SizeResult(qty=0.0, notional=0.0)
    weight = WEIGHT_BY_RANK[rank]
    # Vol-adjusted scaling: if atr provided, scale weight by inverse vol
    if atr is not None and atr > 0:
        asset_vol_pct = atr / entry_price
        if asset_vol_pct > 0:
            vol_scale = min(TARGET_DAILY_VOL / asset_vol_pct, 1.0)
            weight *= vol_scale
    notional = cash * weight
    qty = notional / entry_price
    return SizeResult(qty=qty, notional=notional)

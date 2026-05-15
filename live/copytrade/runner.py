"""bot-cp main runner — polling loop + orchestration."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_TRADE_PCT = 0.50
MIN_PAPER_SIZE_USD = 1.0


def compute_paper_size(
    trade_size_usd: float,
    target_aum: float,
    capital_per_wallet: float,
) -> float:
    """Return the paper-trade size (USD) to mirror a target's trade.

    Logic: trade_pct = trade_size_usd / target_aum, clamped to [0, MAX_TRADE_PCT].
    Returns 0 on invalid inputs (negative or zero AUM).
    """
    if trade_size_usd <= 0 or target_aum <= 0 or capital_per_wallet <= 0:
        return 0.0
    trade_pct = min(trade_size_usd / target_aum, MAX_TRADE_PCT)
    return capital_per_wallet * trade_pct

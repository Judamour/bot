"""Hardcoded target wallets for paper copytrading and capital constants.

2026-05-20: switched to RN1-exclusive (was tracking 3 wallets — surfandturf
and bossoskil1 removed). Decision driven by Option B paper outperformance
(+9.7pp vs Option A on 25 trades) and the need to dedicate scanner CPU/API
quota to one target. Historical paper portfolios for the other two wallets
remain in portfolio.json (frozen, no longer updated).
"""
import os


TARGETS = [
    {
        "pseudonym": "RN1",
        "wallet": "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
        "allocation_pct": 1.0,
    },
]


PAPER_CAPITAL_USD = float(os.getenv("BOT_CP_CAPITAL_USD", "1000.0"))
CAPITAL_PER_WALLET = PAPER_CAPITAL_USD / len(TARGETS)

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
        "allocation_pct": 0.34,
    },
    {
        "pseudonym": "surfandturf",
        "wallet": "0x9f2fe025f84839ca81dd8e0338892605702d2ca8",
        "allocation_pct": 0.33,
    },
    # newdogbeginning: #1 daily winner 2026-05-21 (+$370K), equity $4.2M,
    # 40 BUYs/day arbing "quasi-certainties" at 0.99 (Bitcoin/Roland Garros).
    # Added to copy-track while RN1 is in revenge-trading drawdown.
    {
        "pseudonym": "newdog",
        "wallet": "0xfea31bc088000ff909be1dfd8d0e3f2c7ef2d227",
        "allocation_pct": 0.33,
    },
]


PAPER_CAPITAL_USD = float(os.getenv("BOT_CP_CAPITAL_USD", "1000.0"))
CAPITAL_PER_WALLET = PAPER_CAPITAL_USD / len(TARGETS)

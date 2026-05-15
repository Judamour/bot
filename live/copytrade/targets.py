"""Hardcoded target wallets for paper copytrading and capital constants.

The three wallets were selected on 2026-05-15 by querying
https://lb-api.polymarket.com/profit on All-time, 30d, 7d windows and
keeping only wallets present in ≥2 windows (sustained edge, not luck).
"""
import os


TARGETS = [
    {
        "pseudonym": "RN1",
        "wallet": "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
        "allocation_pct": 1 / 3,
    },
    {
        "pseudonym": "bossoskil1",
        "wallet": "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a",
        "allocation_pct": 1 / 3,
    },
    {
        "pseudonym": "surfandturf",
        "wallet": "0x9f2fe025f84839ca81dd8e0338892605702d2ca8",
        "allocation_pct": 1 / 3,
    },
]


PAPER_CAPITAL_USD = float(os.getenv("BOT_CP_CAPITAL_USD", "1000.0"))
CAPITAL_PER_WALLET = PAPER_CAPITAL_USD / len(TARGETS)

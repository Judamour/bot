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
        "allocation_pct": 0.20,
    },
    {
        "pseudonym": "surfandturf",
        "wallet": "0x9f2fe025f84839ca81dd8e0338892605702d2ca8",
        "allocation_pct": 0.20,
    },
    # newdogbeginning: #1 daily winner 2026-05-21 (+$370K). Quasi-cert arb
    # at 0.99 — backtest 56d showed -0.72% ROI (Bayern catastrophe killed it).
    {
        "pseudonym": "newdog",
        "wallet": "0xfea31bc088000ff909be1dfd8d0e3f2c7ef2d227",
        "allocation_pct": 0.20,
    },
    # Mosley1: #2 daily winner 2026-05-21 (+$268K), equity $456K. Whale
    # conviction NBA/foot, median chunk $4347. Backtest 61d showed +2.62%
    # ROI / +65%/yr — best candidate among the alternatives.
    {
        "pseudonym": "Mosley1",
        "wallet": "0x5bec79df9add70a3892041ab1a5516b60f53b215",
        "allocation_pct": 0.20,
    },
    # kch123: #3 all-time ($12.5M lifetime), still active. Buy-and-hold
    # style — almost never sells (3498 BUYs / 2 SELLs / 117d). Backtest
    # inconclusive without Gamma resolutions, but auto-unwind 99% in our
    # paper_bot.py should capture his winners automatically.
    {
        "pseudonym": "kch123",
        "wallet": "0x6a72f61820b2cce1cce9b30797b41e7a13265ea2",
        "allocation_pct": 0.20,
    },
]


PAPER_CAPITAL_USD = float(os.getenv("BOT_CP_CAPITAL_USD", "1000.0"))
CAPITAL_PER_WALLET = PAPER_CAPITAL_USD / len(TARGETS)

"""Option B filters — RN1-specific exclusions derived from 6010-trade analysis.

Replays of paper trades show Option B (Option A absband + these 3 exclusions)
outperforms vanilla Option A by ~+9.7pp over 25 trades.

Edge zones to AVOID when copying RN1:
- Hour 18-24 UTC: -14.16% ROI (NBA/EPL live-betting where US sharps dominate)
- Market types draw/spread/winner_yes_no/other: -6.07% ROI on winner_yes_no alone
- Whale trades >$10K: -27.13% ROI (his desperate DCA / manipulation buckets)
"""
from datetime import datetime, timezone


def classify_market_type(title: str, outcome: str) -> str:
    """Identify Polymarket market type from title pattern.

    Mirrors analysis/rn1/analyze_deep.py:classify_market_type — keep in sync.
    """
    t = (title or "").lower()
    if "o/u" in t or "over/under" in t:
        return "over_under"
    if t.startswith("spread:"):
        return "spread"
    if "both teams to score" in t or "btts" in t:
        return "btts"
    if "end in a draw" in t or "draw?" in t:
        return "draw"
    if "will " in t and " win " in t:
        return "winner_yes_no"
    if " vs. " in t or " vs " in t:
        return "match_winner"
    return "other"


# Market types where RN1 loses money — skip these in Option B
BAD_MTYPES = frozenset({"draw", "spread", "winner_yes_no", "other"})

# Hours (UTC) where RN1 loses money — skip BUYs that originated in this window
BAD_HOURS_UTC = frozenset(range(18, 24))

# Whale threshold — his trades above this size lose money on average
WHALE_USD = 10000.0


def optionb_passes(decision: dict) -> tuple[bool, str]:
    """Return (passes: bool, skip_reason: str).

    Apply Option B's 3 exclusion filters. Caller skips the BUY if passes=False.
    """
    # 1. Hour-of-day filter
    ts = int(decision.get("ts") or 0)
    if ts > 0:
        hour_utc = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        if hour_utc in BAD_HOURS_UTC:
            return False, f"optb_bad_hour({hour_utc}h)"

    # 2. Market-type filter
    title = decision.get("market") or ""
    outcome = decision.get("outcome") or ""
    mtype = classify_market_type(title, outcome)
    if mtype in BAD_MTYPES:
        return False, f"optb_bad_mtype({mtype})"

    # 3. Whale filter
    target_size = float(decision.get("target_size_usd") or 0)
    if target_size > WHALE_USD:
        return False, f"optb_whale(${target_size:.0f})"

    return True, "ok"

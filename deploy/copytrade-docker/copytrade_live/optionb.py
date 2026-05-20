"""Option B filters — RN1-specific exclusions derived from 6010-trade analysis.

Replays of paper trades show Option B (Option A absband + these 3 exclusions)
outperforms vanilla Option A by ~+9.7pp over 25 trades.

Edge zones to AVOID when copying RN1:
- Hour 18-24 UTC: -14.16% ROI (NBA/EPL live-betting where US sharps dominate)
- Market types draw/spread/winner_yes_no/other: -6.07% ROI on winner_yes_no alone
- Whale trades >$10K: -27.13% ROI (his desperate DCA / manipulation buckets)

Adds one further behavior (2026-05-20):
- Conviction filter (whale + net-buy aggregation): only fire when RN1 has
  committed >=$50 to a (market, outcome) — either in one chunk or cumulated
  over a 10min window. Filters out his exploratory hedge dust ($1-5).

Note on RN1's "reverse conviction": he never SELLs losing positions; when the
market moves against him he ADDs to the winning side instead, holding both
sides until resolution. We mirror that pattern by capping per (cond_id,
outcome) rather than per cond_id — see poller.py's outcome_saturated check.
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

# Title substrings (lowercased) that indicate RN1's lottery/exploration zone.
# Observed 2026-05-20: tennis qualification markets (Roland Garros Qualif,
# Hamburg European Open Qualif, etc.) → his open positions in these markets
# are -95% to -99.8% across the board (~-$1500 unrealized). Meanwhile his
# foot/baseball convictions ("Will X win on", "X vs Y" market_winner) were
# the source of his +$20K redemptions in the same 4h window. Skip the noise.
BAD_TITLE_KEYWORDS = ("qualification",)


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

    # 4. Title-keyword filter — skip RN1's lottery zones (tennis qualifications)
    title_low = title.lower()
    for kw in BAD_TITLE_KEYWORDS:
        if kw in title_low:
            return False, f"optb_bad_category({kw})"

    return True, "ok"


# --- Conviction filter (whale + net-buy aggregation over rolling window) ---
# In-memory store, per-process. Lost on container restart — that's fine because
# the window is short (10min) and bot-cp re-replays only recent decisions on
# restart via positions_polling. Format: {(cid, oi): [(ts, target_size), ...]}.
_recent_buys: dict[tuple[str, int], list[tuple[int, float]]] = {}

# Tuned to capture his real convictions while skipping exploratory hedge dust.
# Env-driven so we can re-tune without redeploying code. $200 default matches
# yesterday's MIN_TARGET when the $40 wallet ran the strictest filter.
import os
CONVICTION_THRESHOLD_USD = float(os.getenv("COPYTRADE_CONVICTION_THRESHOLD_USD", "200"))
CONVICTION_WINDOW_S = int(os.getenv("COPYTRADE_CONVICTION_WINDOW_S", "600"))


def record_observation(decision: dict) -> None:
    """Log this BUY in the rolling window so future chunks can build cumulative."""
    cid = decision.get("conditionId") or ""
    if not cid:
        return
    oi = int(decision.get("outcomeIndex") or 0)
    ts = int(decision.get("ts") or 0)
    size = float(decision.get("target_size_usd") or 0)
    if ts <= 0 or size <= 0:
        return
    key = (cid, oi)
    arr = _recent_buys.setdefault(key, [])
    arr.append((ts, size))
    # Prune entries older than window so memory stays bounded
    cutoff = ts - CONVICTION_WINDOW_S
    if arr[0][0] < cutoff:
        _recent_buys[key] = [(t, s) for (t, s) in arr if t >= cutoff]


def conviction_passes(
    decision: dict,
    *,
    threshold_usd: float = CONVICTION_THRESHOLD_USD,
    window_s: int = CONVICTION_WINDOW_S,
) -> tuple[bool, str]:
    """Whale-or-cumulative filter. Pass if THIS chunk OR window-cumulative >= threshold.

    Note: caller should record_observation(decision) BEFORE this check so the
    current chunk is counted in the cumulative.
    """
    size = float(decision.get("target_size_usd") or 0)
    cid = decision.get("conditionId") or ""
    oi = int(decision.get("outcomeIndex") or 0)
    ts = int(decision.get("ts") or 0)
    if size >= threshold_usd:
        return True, f"single_chunk_${size:.0f}"
    if not cid or ts <= 0:
        return False, f"below_conviction_${size:.0f}"
    cutoff = ts - window_s
    cum = sum(s for (t, s) in _recent_buys.get((cid, oi), []) if t >= cutoff)
    if cum >= threshold_usd:
        return True, f"cumulative_${cum:.0f}_in_{window_s}s"
    return False, f"below_conviction_${cum:.0f}<{threshold_usd:.0f}"



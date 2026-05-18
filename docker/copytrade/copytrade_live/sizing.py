"""Tiered sizing strategy for copytrade live.

Sizing modes (selectable via COPYTRADE_SIZING_MODE env var):
  - 'fixed': returns config.FIXED_SIZE_USD always (legacy behavior)
  - 'tiered': scales USD allocated by entry price band + his conviction (trade_pct)

The tiered grid is tuned for surfandturf on a small ($40) wallet:
  - Penny [0.06-0.20):  fixed $1 -> place_buy floors to 5 shares ($0.30-$1.00 cost),
                        leverage 5-20x via min-order constraint
  - Mid [0.20-0.65):    linear scale by conviction (5%->50% AUM mapped to $1.5->$5)
  - Fav [0.65-MAX):     fixed $4.50, only if conviction >= TIER_FAV_MIN_CONVICTION
  - Below MIN_ENTRY or above MAX_ENTRY: caller-side filters still apply upstream

Returns None => skip the trade (logged separately by caller).
"""
from . import config


def compute_size_usd(price: float, trade_pct: float) -> float | None:
    """Return USD size to allocate, or None to skip.

    place_buy will floor to 5 shares (min Polymarket order); effective cost is
    max(returned_usd, 5 * price).
    """
    if config.SIZING_MODE == "fixed":
        return config.FIXED_SIZE_USD

    # ---- ABSOLUTE_BAND MODE ----
    # Penny band: fixed penny size, leverage via min-5-shares constraint.
    # SKIP zone (optional): for traders like RN1, the [TIER_PENNY_MAX, TIER_SKIP_HIGH]
    #   band is the loser bucket — skip it. Default TIER_SKIP_HIGH = TIER_PENNY_MAX
    #   so no skip zone (matches surfandturf-tuned behavior).
    # Normal band: flat normal size. Conviction ignored; robustness against DCA /
    #   partial-fill noise comes from MAX_USD_PER_MARKET cap upstream.
    if config.SIZING_MODE == "absolute_band":
        if price < config.TIER_PENNY_MAX:
            return config.TIER_PENNY_SIZE
        if price < config.TIER_SKIP_HIGH:
            return None  # losing zone (RN1 mid_low) — skip
        return config.TIER_NORMAL_SIZE

    # ---- TIERED MODE ----

    # Penny pocket: leverage via min-5-shares constraint
    if price < config.TIER_PENNY_MAX:
        if trade_pct < config.TIER_PENNY_MIN_CONVICTION:
            return None
        return config.TIER_PENNY_SIZE

    # Mid + tossup: linear scale by his conviction (his trade_pct)
    if price < config.TIER_MID_MAX:
        if trade_pct < config.TIER_MID_MIN_CONVICTION:
            return None
        denom = config.TIER_MID_MAX_CONVICTION - config.TIER_MID_MIN_CONVICTION
        if denom <= 0:
            return config.TIER_MID_MIN_SIZE
        slope = (config.TIER_MID_MAX_SIZE - config.TIER_MID_MIN_SIZE) / denom
        size = config.TIER_MID_MIN_SIZE + (trade_pct - config.TIER_MID_MIN_CONVICTION) * slope
        return max(config.TIER_MID_MIN_SIZE, min(config.TIER_MID_MAX_SIZE, size))

    # Favorite zone: take only on real conviction
    if trade_pct < config.TIER_FAV_MIN_CONVICTION:
        return None
    return config.TIER_FAV_SIZE


def describe_tier(price: float, trade_pct: float) -> str:
    """Return short label for logging which tier applied."""
    if config.SIZING_MODE == "fixed":
        return "fixed"
    if config.SIZING_MODE == "absolute_band":
        if price < config.TIER_PENNY_MAX:
            return "penny"
        if price < config.TIER_SKIP_HIGH:
            return "skip_zone"
        return "normal"
    if price < config.TIER_PENNY_MAX:
        return "penny"
    if price < config.TIER_MID_MAX:
        return "mid"
    return "fav"

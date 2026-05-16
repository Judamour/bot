"""Target AUM estimator: stable cash + cost basis composition.

The previous implementation used /value directly, which includes MTM on open
positions. That meant when a position MTM-ed from $0.04 → $0.99 (favori qui
gagne), the estimator's AUM jumped 20-30x — and the next BUY copied from
the same wallet sized off a phantom AUM, distorting trade_pct.

New formula (stable across MTM swings):

    open_pos    = positions where NOT redeemable (market still unresolved)
    mtm_open    = Σ currentValue across open_pos
    cost_open   = Σ initialValue across open_pos
    cash_estim  = max(0, /value − mtm_open)   # cash + resolved/redeemable value
    AUM         = cash_estim + cost_open

Why filter `not redeemable`? Polymarket /positions keeps resolved positions
in the list until they're redeemed (one wallet had 98/100 stale loser stubs
inflating cost basis to $10.5M vs $210K real value). Filtering out redeemable
ones isolates true open exposure.

Why subtract mtm_open from /value? `/value` is cash + ALL MTM. Subtracting
just open-position MTM leaves us with (cash + resolved-but-unredeemed value),
which is stable — only realized PnL or new redemptions shift it.

Symmetric with PaperPortfolio.equity_at_cost() for our own wallet.

Fallback hierarchy:
    - /value fails       → AUM = Σ initialValue (cost only, no cash estimate)
    - /positions fails   → AUM = /value (least bad, may include MTM)
    - both fail          → 0

Cached per wallet for `_cache_ttl` seconds (default 60s).
"""
from __future__ import annotations

import logging
import time

from live.copytrade import data_api

log = logging.getLogger(__name__)

# (wallet → (ts, aum))
_cache: dict[str, tuple[float, float]] = {}


def clear_cache() -> None:
    _cache.clear()


def aum(wallet: str, _cache_ttl: float = 60.0) -> float:
    """Return stable AUM estimate in USD. Uses cache if fresh."""
    now = time.time()
    cached = _cache.get(wallet)
    if cached and (now - cached[0]) < _cache_ttl:
        return cached[1]

    try:
        total_value = data_api.value(wallet)
        value_ok = True
    except data_api.DataAPIError as e:
        log.warning("value endpoint failed for %s: %s", wallet, e)
        total_value = 0.0
        value_ok = False

    try:
        poss = data_api.positions(wallet)
        positions_ok = True
    except data_api.DataAPIError as e:
        log.warning("positions endpoint failed for %s: %s", wallet, e)
        poss = []
        positions_ok = False

    if not positions_ok:
        # Without positions we can't separate cash from MTM. Fall back to /value
        # (least bad) — accepts MTM-induced instability for this snapshot.
        v = total_value
    else:
        open_pos = [p for p in poss if not p.get("redeemable", False)]
        cost_open = sum(float(p.get("initialValue", 0.0)) for p in open_pos)
        mtm_open = sum(float(p.get("currentValue", 0.0)) for p in open_pos)
        if not value_ok:
            v = cost_open  # no /value → use cost basis of open positions only
        else:
            cash_estim = max(0.0, total_value - mtm_open)
            v = cash_estim + cost_open

    _cache[wallet] = (now, v)
    return v

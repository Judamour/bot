"""Target AUM estimator: cash (USDC) + positions market value.

We approximate by using the public /value endpoint first (Polymarket's own
sum). If that returns 0 (sometimes lagging), fall back to summing positions'
`currentValue`. Both are snapshots — for paper sizing across our 3 wallets
($100K-$10M AUM range) this snapshot drift is <1% of the ratio used.

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
    """Return estimated AUM in USD. Uses cache if fresh."""
    now = time.time()
    cached = _cache.get(wallet)
    if cached and (now - cached[0]) < _cache_ttl:
        return cached[1]

    try:
        v = data_api.value(wallet)
    except data_api.DataAPIError as e:
        log.warning("value endpoint failed for %s: %s", wallet, e)
        v = 0.0

    if v <= 0:
        try:
            poss = data_api.positions(wallet)
            v = sum(float(p.get("currentValue", 0.0)) for p in poss)
        except data_api.DataAPIError as e:
            log.warning("positions endpoint failed for %s: %s", wallet, e)
            v = 0.0

    _cache[wallet] = (now, v)
    return v

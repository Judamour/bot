"""Polymarket public API client (read-only).

Endpoints used:
  - data-api.polymarket.com/trades?user={wallet}&limit=N
  - data-api.polymarket.com/positions?user={wallet}
  - data-api.polymarket.com/value?user={wallet}
  - clob.polymarket.com/price?token_id={asset}&side={BUY|SELL}

Required headers (else 403):
  Origin: https://polymarket.com
  Referer: https://polymarket.com/
  User-Agent: Mozilla/5.0

Backoff: 1s, 2s, 4s on 429/5xx (max 3 retries); raise after.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_DATA_API = "https://data-api.polymarket.com"
_CLOB_API = "https://clob.polymarket.com"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}


class DataAPIError(Exception):
    """Non-retryable API failure (4xx other than 429)."""


def _get(url: str, retries: int = 3, timeout: float = 10.0) -> Any:
    """GET url with required headers and exponential backoff on 429/5xx."""
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                log.warning("data_api %s -> HTTP %d, retry in %.1fs", url, e.code, delay)
                time.sleep(delay)
                delay *= 2
                last_exc = e
                continue
            raise DataAPIError(f"HTTP {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                last_exc = e
                continue
            raise DataAPIError(f"network error for {url}: {e}") from e
    raise DataAPIError(f"exhausted retries for {url}") from last_exc


def trades(wallet: str, limit: int = 50, since_ts: int | None = None) -> list[dict]:
    """Recent trades for `wallet`. If `since_ts` is set, only return trades with
    timestamp strictly > since_ts (used for incremental polling)."""
    url = f"{_DATA_API}/trades?user={wallet.lower()}&limit={limit}"
    out = _get(url) or []
    if since_ts is not None:
        out = [t for t in out if int(t.get("timestamp", 0)) > since_ts]
    return out


def positions(wallet: str) -> list[dict]:
    """Open positions for `wallet`, each with size/curPrice/currentValue."""
    url = f"{_DATA_API}/positions?user={wallet.lower()}"
    return _get(url) or []


def value(wallet: str) -> float:
    """Total portfolio value for `wallet`, in USD."""
    url = f"{_DATA_API}/value?user={wallet.lower()}"
    data = _get(url) or []
    if not data:
        return 0.0
    return float(data[0].get("value", 0.0))


def price(token_id: str, side: str = "BUY") -> float | None:
    """Current orderbook mid for an outcome token. Returns None if endpoint
    returns no price (e.g. resolved market)."""
    url = f"{_CLOB_API}/price?token_id={token_id}&side={side}"
    data = _get(url) or {}
    p = data.get("price")
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def target_position_size_at(
    wallet: str,
    condition_id: str,
    outcome_index: int,
    ts: int,
    fetch_limit: int = 500,
) -> float:
    """Return target's outcome-token size on (condition_id, outcome_index)
    at-or-just-before `ts`, by summing signed trades with timestamp ≤ ts.

    BUY adds size, SELL subtracts. We fetch up to `fetch_limit` recent trades
    of the wallet, which is enough for our 3 chosen targets (high-frequency
    but most positions opened in last 1-2 weeks).
    """
    all_trades = trades(wallet, limit=fetch_limit)
    relevant = [
        t for t in all_trades
        if t.get("conditionId") == condition_id
        and int(t.get("outcomeIndex", -1)) == outcome_index
        and int(t.get("timestamp", 0)) <= ts
    ]
    size = 0.0
    for t in sorted(relevant, key=lambda x: int(x["timestamp"])):
        delta = float(t.get("size", 0.0))
        size += delta if t.get("side") == "BUY" else -delta
    return max(size, 0.0)

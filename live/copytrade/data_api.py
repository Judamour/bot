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

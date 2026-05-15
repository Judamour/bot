# Polymarket CopyTrade Paper Bot — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone systemd service that paper-trades a mirror of three Polymarket top wallets via the public Data API, persisting decisions and equity to JSONL for a 30-day evaluation before any capital decision.

**Architecture:** Single Python process polling `data-api.polymarket.com` every 60s. For each new target trade detected, estimate target AUM, compute the proportional paper size, and update a per-wallet simulated portfolio. State persisted as JSON+JSONL. Pattern mirrors the existing Shadow bot (`shadow/runner.py`) for consistency.

**Tech Stack:** Python 3.12, stdlib only (`urllib`, `json`, `time`, `logging`), pytest for tests, systemd for service lifecycle. Reuses `live/notifier.py` for Telegram and `dashboard/app.py` Flask for the read-only API surface.

**Spec:** `docs/superpowers/specs/2026-05-15-polymarket-copytrade-bot-design.md`

---

## File map

| File | Purpose |
|---|---|
| `live/copytrade/__init__.py` | Package marker |
| `live/copytrade/targets.py` | Hardcoded wallets + `PAPER_CAPITAL_USD` |
| `live/copytrade/data_api.py` | Polymarket public API client (trades, positions, value, price, target_position_size_at) |
| `live/copytrade/aum_estimator.py` | Target AUM = cash + positions market value, with 60s cache |
| `live/copytrade/paper_portfolio.py` | Per-wallet buy/sell/MTM/equity operations |
| `live/copytrade/state.py` | Atomic JSON persistence for state.json + portfolio.json |
| `live/copytrade/runner.py` | Polling loop, orchestration |
| `live/copytrade/README.md` | Operator docs |
| `tests/copytrade/__init__.py` | Test package marker |
| `tests/copytrade/conftest.py` | Shared pytest fixtures |
| `tests/copytrade/test_data_api.py` | Mocked-HTTP tests for the API client |
| `tests/copytrade/test_aum_estimator.py` | AUM math, cache TTL |
| `tests/copytrade/test_paper_portfolio.py` | Buy/sell ops + clamps |
| `tests/copytrade/test_state.py` | Atomic write, reload, schema |
| `tests/copytrade/test_runner_sizing.py` | Trade → paper_size end-to-end |
| `tests/copytrade/test_data_api_smoke.py` | Integration: hit real API on 3 wallets |
| `scripts/replay_30d.py` | Retroactive equity curve from last 30d of target trades |
| `deploy/bot-cp.service` | Systemd unit |
| `dashboard/app.py` | + `/api/copytrade` route |
| `dashboard/templates/index.html` | + CopyTrade tab |

---

## Chunk 1: Foundation (setup, targets, data_api)

### Task 1: Package scaffolding

**Files:**
- Create: `live/copytrade/__init__.py`
- Create: `tests/copytrade/__init__.py`
- Create: `logs/copytrade/.gitkeep`

- [ ] **Step 1: Create directories and empty package files**

```bash
mkdir -p live/copytrade tests/copytrade logs/copytrade
touch live/copytrade/__init__.py tests/copytrade/__init__.py logs/copytrade/.gitkeep
```

- [ ] **Step 2: Verify pytest discovers the new test dir**

Run: `pytest tests/copytrade/ -v --collect-only`
Expected: `no tests collected` (no test files yet, dir is discovered)

- [ ] **Step 3: Commit**

```bash
git add live/copytrade/__init__.py tests/copytrade/__init__.py logs/copytrade/.gitkeep
git commit -m "chore(copytrade): scaffold package and log dir"
```

---

### Task 2: targets.py — constants

**Files:**
- Create: `live/copytrade/targets.py`
- Test: `tests/copytrade/test_targets.py`

- [ ] **Step 1: Write the failing test**

Create `tests/copytrade/test_targets.py`:

```python
"""Constants module — verify shape and capital math."""
from live.copytrade.targets import TARGETS, PAPER_CAPITAL_USD, CAPITAL_PER_WALLET


def test_three_targets():
    assert len(TARGETS) == 3


def test_each_target_has_required_fields():
    for t in TARGETS:
        assert set(t.keys()) >= {"pseudonym", "wallet", "allocation_pct"}
        assert t["wallet"].startswith("0x") and len(t["wallet"]) == 42
        assert 0 < t["allocation_pct"] <= 1.0


def test_allocations_sum_to_one():
    total = sum(t["allocation_pct"] for t in TARGETS)
    assert abs(total - 1.0) < 1e-9


def test_paper_capital_positive():
    assert PAPER_CAPITAL_USD > 0


def test_capital_per_wallet_consistent():
    assert abs(CAPITAL_PER_WALLET - PAPER_CAPITAL_USD / len(TARGETS)) < 1e-9


def test_targets_are_the_expected_three():
    pseudos = {t["pseudonym"] for t in TARGETS}
    assert pseudos == {"RN1", "bossoskil1", "surfandturf"}
```

- [ ] **Step 2: Run test — must fail**

Run: `pytest tests/copytrade/test_targets.py -v`
Expected: `ModuleNotFoundError: No module named 'live.copytrade.targets'`

- [ ] **Step 3: Implement targets.py**

Create `live/copytrade/targets.py`:

```python
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
```

- [ ] **Step 4: Run test — must pass**

Run: `pytest tests/copytrade/test_targets.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/targets.py tests/copytrade/test_targets.py
git commit -m "feat(copytrade): targets and capital constants"
```

---

### Task 3: data_api.py — HTTP helper foundation

**Files:**
- Create: `live/copytrade/data_api.py`
- Test: `tests/copytrade/test_data_api.py`

- [ ] **Step 1: Write the failing test for the basic GET helper**

Create `tests/copytrade/test_data_api.py`:

```python
"""Tests for live/copytrade/data_api.py — mocked-HTTP behaviour."""
import json
from unittest.mock import patch, MagicMock

import pytest

from live.copytrade import data_api


def _mock_resp(body, status=200):
    m = MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = status
    m.read.return_value = json.dumps(body).encode()
    return m


def test_get_returns_parsed_json():
    payload = [{"k": "v"}]
    with patch("urllib.request.urlopen", return_value=_mock_resp(payload)):
        out = data_api._get("https://example.com/x")
    assert out == payload


def test_get_sends_required_headers():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return _mock_resp([])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        data_api._get("https://example.com/x")
    # urllib title-cases header names
    assert captured["headers"].get("Origin") == "https://polymarket.com"
    assert captured["headers"].get("Referer") == "https://polymarket.com/"
    assert "Mozilla" in captured["headers"].get("User-agent", "")
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: `ImportError`

- [ ] **Step 3: Implement the GET helper**

Create `live/copytrade/data_api.py`:

```python
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
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/data_api.py tests/copytrade/test_data_api.py
git commit -m "feat(copytrade): http helper with retries and headers"
```

---

### Task 4: data_api.py — trades, positions, value

**Files:**
- Modify: `live/copytrade/data_api.py`
- Modify: `tests/copytrade/test_data_api.py`

- [ ] **Step 1: Append failing tests for endpoint wrappers**

Append to `tests/copytrade/test_data_api.py`:

```python
def test_trades_url_format():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _mock_resp([{"timestamp": 1, "side": "BUY"}])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = data_api.trades("0xABC", limit=50)
    assert "user=0xabc" in captured["url"].lower()
    assert "limit=50" in captured["url"]
    assert isinstance(out, list)


def test_trades_since_filters_older():
    body = [
        {"timestamp": 100, "side": "BUY", "transactionHash": "0xa"},
        {"timestamp": 200, "side": "SELL", "transactionHash": "0xb"},
        {"timestamp": 50,  "side": "BUY",  "transactionHash": "0xc"},
    ]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        out = data_api.trades("0xABC", since_ts=100)
    # since_ts is strictly greater-than → 50 and 100 are excluded
    hashes = [t["transactionHash"] for t in out]
    assert hashes == ["0xb"]


def test_positions_returns_list():
    body = [{"conditionId": "0xC", "size": 10, "curPrice": 0.5, "currentValue": 5}]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        out = data_api.positions("0xABC")
    assert out == body


def test_value_returns_scalar():
    body = [{"user": "0xabc", "value": 1234.56}]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        v = data_api.value("0xABC")
    assert v == pytest.approx(1234.56)


def test_value_handles_empty():
    with patch("urllib.request.urlopen", return_value=_mock_resp([])):
        v = data_api.value("0xABC")
    assert v == 0.0
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: 5 new failures (`AttributeError: module ... has no attribute 'trades'`)

- [ ] **Step 3: Implement the endpoints**

Append to `live/copytrade/data_api.py`:

```python
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
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/data_api.py tests/copytrade/test_data_api.py
git commit -m "feat(copytrade): trades/positions/value endpoints"
```

---

### Task 5: data_api.py — price endpoint + target_position_size_at

**Files:**
- Modify: `live/copytrade/data_api.py`
- Modify: `tests/copytrade/test_data_api.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/copytrade/test_data_api.py`:

```python
def test_price_url_and_response():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _mock_resp({"price": "0.62"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        p = data_api.price(token_id="42", side="BUY")
    assert "token_id=42" in captured["url"]
    assert "side=BUY" in captured["url"]
    assert p == pytest.approx(0.62)


def test_price_handles_missing():
    with patch("urllib.request.urlopen", return_value=_mock_resp({})):
        p = data_api.price(token_id="42", side="BUY")
    assert p is None


def test_target_position_size_at_basic():
    """Sum signed sizes up to ts."""
    body = [
        # BUYs add, SELLs subtract; sorted desc by API but our func sorts asc internally
        {"timestamp": 100, "side": "BUY",  "size": 10, "conditionId": "0xC", "outcomeIndex": 0},
        {"timestamp": 200, "side": "BUY",  "size": 5,  "conditionId": "0xC", "outcomeIndex": 0},
        {"timestamp": 300, "side": "SELL", "size": 3,  "conditionId": "0xC", "outcomeIndex": 0},
        {"timestamp": 150, "side": "BUY",  "size": 7,  "conditionId": "0xOTHER", "outcomeIndex": 0},
    ]
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        size = data_api.target_position_size_at("0xW", "0xC", outcome_index=0, ts=250)
    assert size == pytest.approx(15)  # 10 + 5, SELL at 300 not included


def test_target_position_size_at_zero_when_none_before():
    with patch("urllib.request.urlopen", return_value=_mock_resp([])):
        size = data_api.target_position_size_at("0xW", "0xC", outcome_index=0, ts=999)
    assert size == 0.0
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: 4 new failures

- [ ] **Step 3: Implement price + position-size helper**

Append to `live/copytrade/data_api.py`:

```python
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
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: 11 passed total

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/data_api.py tests/copytrade/test_data_api.py
git commit -m "feat(copytrade): price endpoint and target position size helper"
```

---

### Task 6: data_api.py — error paths (4xx, retry exhaustion)

**Files:**
- Modify: `tests/copytrade/test_data_api.py`

- [ ] **Step 1: Append failing-path tests**

Append to `tests/copytrade/test_data_api.py`:

```python
def test_403_raises_immediately():
    err = urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(data_api.DataAPIError):
            data_api._get("https://example.com/x", retries=2)


def test_429_retries_then_succeeds(monkeypatch):
    """First two calls return 429, third returns 200 — final result is the 200."""
    monkeypatch.setattr(data_api.time, "sleep", lambda *_: None)
    err = urllib.error.HTTPError("u", 429, "rate-limited", {}, None)
    seq = [err, err, _mock_resp({"ok": True})]

    def fake(req, timeout=None):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    with patch("urllib.request.urlopen", side_effect=fake):
        out = data_api._get("https://example.com/x", retries=3)
    assert out == {"ok": True}


def test_500_retries_exhausted(monkeypatch):
    monkeypatch.setattr(data_api.time, "sleep", lambda *_: None)
    err = urllib.error.HTTPError("u", 500, "ise", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(data_api.DataAPIError):
            data_api._get("https://example.com/x", retries=2)
```

Add at the top of the file (after the existing imports):

```python
import urllib.error
```

- [ ] **Step 2: Run — must pass without further code changes**

Run: `pytest tests/copytrade/test_data_api.py -v`
Expected: 14 passed total

- [ ] **Step 3: Commit**

```bash
git add tests/copytrade/test_data_api.py
git commit -m "test(copytrade): error path coverage for data_api"
```

---

## Chunk 1 review checkpoint

Run the entire chunk 1 test suite once:

```bash
pytest tests/copytrade/ -v
```

Expected: 20 passed (6 targets + 14 data_api).

Then dispatch the plan-document-reviewer subagent on Chunk 1 before proceeding to Chunk 2.

---

## Chunk 2: Core logic (aum_estimator, paper_portfolio, state)

### Task 7: aum_estimator.py — AUM math with caching

**Files:**
- Create: `live/copytrade/aum_estimator.py`
- Test: `tests/copytrade/test_aum_estimator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/copytrade/test_aum_estimator.py`:

```python
"""AUM estimator — value + positions composition + cache TTL."""
import time
from unittest.mock import patch

from live.copytrade import aum_estimator


def test_aum_uses_value_endpoint_when_positive():
    """If /value returns a positive number, trust it directly."""
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=5000.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 5000.0


def test_aum_falls_back_to_positions_sum_when_value_zero():
    """If /value returns 0 but positions show value, sum them."""
    positions = [
        {"size": 100, "curPrice": 0.3, "currentValue": 30.0},
        {"size": 50,  "curPrice": 0.8, "currentValue": 40.0},
    ]
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=positions):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 70.0


def test_aum_zero_when_no_data():
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=0.0), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]):
        aum = aum_estimator.aum("0xW", _cache_ttl=0)
    assert aum == 0.0


def test_cache_hit_skips_api_calls():
    """Two calls within TTL — second uses cache."""
    aum_estimator.clear_cache()
    with patch("live.copytrade.aum_estimator.data_api.value", return_value=1000.0) as mv, \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]) as mp:
        a1 = aum_estimator.aum("0xW", _cache_ttl=60)
        a2 = aum_estimator.aum("0xW", _cache_ttl=60)
    assert a1 == a2 == 1000.0
    assert mv.call_count == 1
    assert mp.call_count == 1


def test_cache_expires():
    aum_estimator.clear_cache()
    t = [1000.0]

    def fake_value(_):
        t[0] += 100
        return t[0]

    with patch("live.copytrade.aum_estimator.data_api.value", side_effect=fake_value), \
         patch("live.copytrade.aum_estimator.data_api.positions", return_value=[]), \
         patch("live.copytrade.aum_estimator.time.time", side_effect=[0, 0, 70, 70]):
        a1 = aum_estimator.aum("0xW", _cache_ttl=60)
        a2 = aum_estimator.aum("0xW", _cache_ttl=60)
    assert a1 == 1100.0
    assert a2 == 1200.0  # cache expired between calls
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_aum_estimator.py -v`
Expected: 5 failures (`ImportError`)

- [ ] **Step 3: Implement aum_estimator.py**

Create `live/copytrade/aum_estimator.py`:

```python
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
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_aum_estimator.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/aum_estimator.py tests/copytrade/test_aum_estimator.py
git commit -m "feat(copytrade): aum_estimator with 60s cache"
```

---

### Task 8: paper_portfolio.py — buy/sell/MTM

**Files:**
- Create: `live/copytrade/paper_portfolio.py`
- Test: `tests/copytrade/test_paper_portfolio.py`

- [ ] **Step 1: Write failing tests**

Create `tests/copytrade/test_paper_portfolio.py`:

```python
"""Paper portfolio mechanics — buy adds, sell reduces, MTM uses current prices."""
import pytest

from live.copytrade.paper_portfolio import PaperPortfolio


@pytest.fixture
def pf():
    return PaperPortfolio(wallet="RN1", cash_usd=333.33)


def test_initial_state(pf):
    assert pf.cash_usd == 333.33
    assert pf.positions == []
    assert pf.realized_pnl_usd == 0.0


def test_buy_creates_position(pf):
    pf.buy(
        condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
        price=0.5, usd_size=10.0, target_hash="0xtxA",
        market_title="Test market", opened_ts=1000,
    )
    assert pf.cash_usd == pytest.approx(323.33)
    assert len(pf.positions) == 1
    p = pf.positions[0]
    assert p["size"] == pytest.approx(20.0)        # 10 USD / 0.5 per share
    assert p["avg_price"] == 0.5
    assert p["cost_usd"] == 10.0


def test_buy_adds_to_existing_position(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.6, usd_size=12.0, target_hash="0xtxB",
           market_title="m", opened_ts=2000)
    assert len(pf.positions) == 1
    p = pf.positions[0]
    assert p["size"] == pytest.approx(40.0)        # 20 + 20
    assert p["cost_usd"] == pytest.approx(22.0)
    assert p["avg_price"] == pytest.approx(22.0 / 40.0)


def test_sell_fraction_reduces_position(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    # sell half at 0.7
    pf.sell(condition_id="0xC", outcome_index=0, fraction=0.5, price=0.7,
            target_hash="0xtxB", ts=2000)
    p = pf.positions[0]
    assert p["size"] == pytest.approx(10.0)
    # Proceeds = 10 * 0.7 = 7.0, cost basis sold = 5.0, realized PnL = +2.0
    assert pf.cash_usd == pytest.approx(323.33 + 7.0)
    assert pf.realized_pnl_usd == pytest.approx(2.0)


def test_sell_full_removes_position(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    pf.sell(condition_id="0xC", outcome_index=0, fraction=1.0, price=0.6,
            target_hash="0xtxB", ts=2000)
    assert pf.positions == []
    assert pf.realized_pnl_usd == pytest.approx(2.0)  # 20 * 0.6 - 10 = 2


def test_sell_more_than_owned_clamps(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    pf.sell(condition_id="0xC", outcome_index=0, fraction=2.0, price=0.7,
            target_hash="0xtxB", ts=2000)
    assert pf.positions == []  # fully closed
    assert pf.realized_pnl_usd == pytest.approx(20 * 0.7 - 10)


def test_sell_unknown_position_noop(pf):
    pf.sell(condition_id="0xC", outcome_index=0, fraction=1.0, price=0.7,
            target_hash="0xtxB", ts=1000)
    assert pf.positions == []
    assert pf.cash_usd == 333.33
    assert pf.realized_pnl_usd == 0.0


def test_mtm_equity_with_current_prices(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    eq = pf.equity({"42": 0.7})
    # cash 323.33 + 20 shares * 0.7 = 323.33 + 14 = 337.33
    assert eq == pytest.approx(337.33)


def test_mtm_uses_avg_price_when_no_quote(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    eq = pf.equity({})
    # no current price → fall back to avg_price 0.5 → 323.33 + 10 = 333.33
    assert eq == pytest.approx(333.33)


def test_to_dict_roundtrip(pf):
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=10.0, target_hash="0xtxA",
           market_title="m", opened_ts=1000)
    d = pf.to_dict()
    pf2 = PaperPortfolio.from_dict(d)
    assert pf2.cash_usd == pf.cash_usd
    assert pf2.positions == pf.positions
    assert pf2.realized_pnl_usd == pf.realized_pnl_usd
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_paper_portfolio.py -v`
Expected: 10 failures

- [ ] **Step 3: Implement paper_portfolio.py**

Create `live/copytrade/paper_portfolio.py`:

```python
"""Per-wallet paper portfolio: cash + open positions + realized PnL.

A position is keyed by (condition_id, outcome_index). Adding to an existing
position averages the price. Selling reduces proportionally and realizes
the difference vs avg_price.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PaperPortfolio:
    wallet: str
    cash_usd: float
    positions: list[dict] = field(default_factory=list)
    realized_pnl_usd: float = 0.0

    def _find(self, condition_id: str, outcome_index: int) -> dict | None:
        for p in self.positions:
            if p["condition_id"] == condition_id and p["outcome_index"] == outcome_index:
                return p
        return None

    def buy(
        self,
        *,
        condition_id: str,
        asset: str,
        outcome: str,
        outcome_index: int,
        price: float,
        usd_size: float,
        target_hash: str,
        market_title: str,
        opened_ts: int,
    ) -> None:
        """Buy `usd_size` USD worth at `price` per outcome token."""
        if price <= 0 or usd_size <= 0:
            return
        shares = usd_size / price
        self.cash_usd -= usd_size
        existing = self._find(condition_id, outcome_index)
        if existing:
            new_size = existing["size"] + shares
            new_cost = existing["cost_usd"] + usd_size
            existing["size"] = new_size
            existing["cost_usd"] = new_cost
            existing["avg_price"] = new_cost / new_size if new_size else 0.0
            existing["target_hashes"].append(target_hash)
        else:
            self.positions.append({
                "condition_id": condition_id,
                "asset": asset,
                "outcome": outcome,
                "outcome_index": outcome_index,
                "market_title": market_title,
                "size": shares,
                "avg_price": price,
                "cost_usd": usd_size,
                "opened_ts": opened_ts,
                "target_hashes": [target_hash],
            })

    def sell(
        self,
        *,
        condition_id: str,
        outcome_index: int,
        fraction: float,
        price: float,
        target_hash: str,
        ts: int,
    ) -> None:
        """Sell `fraction` (clamped to [0,1]) of the position at `price`."""
        existing = self._find(condition_id, outcome_index)
        if not existing:
            return
        frac = max(0.0, min(1.0, fraction))
        shares_sold = existing["size"] * frac
        cost_sold = existing["cost_usd"] * frac
        proceeds = shares_sold * price
        self.cash_usd += proceeds
        self.realized_pnl_usd += proceeds - cost_sold
        existing["size"] -= shares_sold
        existing["cost_usd"] -= cost_sold
        existing["target_hashes"].append(target_hash)
        existing["last_sell_ts"] = ts
        # Remove if fully closed (size effectively zero)
        if existing["size"] < 1e-9:
            self.positions.remove(existing)

    def equity(self, current_prices: dict[str, float]) -> float:
        """MTM equity = cash + Σ size × (current price OR avg_price fallback)."""
        eq = self.cash_usd
        for p in self.positions:
            px = current_prices.get(p["asset"])
            if px is None:
                px = p["avg_price"]
            eq += p["size"] * px
        return eq

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaperPortfolio":
        return cls(
            wallet=d["wallet"],
            cash_usd=float(d["cash_usd"]),
            positions=list(d.get("positions", [])),
            realized_pnl_usd=float(d.get("realized_pnl_usd", 0.0)),
        )
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_paper_portfolio.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/paper_portfolio.py tests/copytrade/test_paper_portfolio.py
git commit -m "feat(copytrade): per-wallet paper portfolio buy/sell/MTM"
```

---

### Task 9: state.py — atomic JSON persistence

**Files:**
- Create: `live/copytrade/state.py`
- Test: `tests/copytrade/test_state.py`

- [ ] **Step 1: Write failing tests**

Create `tests/copytrade/test_state.py`:

```python
"""State persistence — atomic write, schema, reload."""
import json
import os

import pytest

from live.copytrade import state as state_mod


def test_load_state_missing_returns_default(tmp_path):
    s = state_mod.load_state(str(tmp_path / "state.json"))
    assert s == {"last_seen_ts": {}}


def test_save_state_then_load(tmp_path):
    p = str(tmp_path / "state.json")
    state_mod.save_state(p, {"last_seen_ts": {"0xW": 12345}})
    out = state_mod.load_state(p)
    assert out == {"last_seen_ts": {"0xW": 12345}}


def test_save_state_atomic(tmp_path):
    """save_state writes via tmp + rename — a kill mid-write must not corrupt."""
    p = str(tmp_path / "state.json")
    state_mod.save_state(p, {"last_seen_ts": {"0xA": 1}})
    # No .tmp leftover after success
    assert not os.path.exists(p + ".tmp")
    with open(p) as f:
        assert json.load(f)["last_seen_ts"] == {"0xA": 1}


def test_load_portfolio_missing_returns_empty(tmp_path):
    out = state_mod.load_portfolio(str(tmp_path / "portfolio.json"))
    assert out == {}


def test_save_load_portfolio(tmp_path):
    p = str(tmp_path / "portfolio.json")
    body = {
        "RN1": {"wallet": "RN1", "cash_usd": 333.33, "positions": [], "realized_pnl_usd": 0.0}
    }
    state_mod.save_portfolio(p, body)
    out = state_mod.load_portfolio(p)
    assert out == body


def test_corrupt_state_falls_back_to_default(tmp_path):
    p = str(tmp_path / "state.json")
    with open(p, "w") as f:
        f.write("{not valid json")
    s = state_mod.load_state(p)
    assert s == {"last_seen_ts": {}}


def test_append_decision_creates_jsonl(tmp_path):
    p = str(tmp_path / "decisions.jsonl")
    state_mod.append_decision(p, {"ts": 1, "wallet": "RN1", "action": "executed"})
    state_mod.append_decision(p, {"ts": 2, "wallet": "RN1", "action": "skipped"})
    with open(p) as f:
        lines = f.readlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["ts"] == 1
    assert json.loads(lines[1])["action"] == "skipped"


def test_append_equity_creates_jsonl(tmp_path):
    p = str(tmp_path / "equity.jsonl")
    state_mod.append_equity(p, {"ts": 1, "total_eq": 1000.0})
    with open(p) as f:
        line = f.readline()
    assert json.loads(line)["total_eq"] == 1000.0
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_state.py -v`
Expected: 8 failures

- [ ] **Step 3: Implement state.py**

Create `live/copytrade/state.py`:

```python
"""Atomic JSON state persistence + JSONL append helpers."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)


def _atomic_write_json(path: str, body: Any) -> None:
    """Write JSON atomically: tmp file in same dir + os.replace."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_seen_ts": {}}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "last_seen_ts" not in data:
            return {"last_seen_ts": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("corrupt state at %s, defaulting: %s", path, e)
        return {"last_seen_ts": {}}


def save_state(path: str, body: dict) -> None:
    _atomic_write_json(path, body)


def load_portfolio(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("corrupt portfolio at %s, defaulting: %s", path, e)
        return {}


def save_portfolio(path: str, body: dict) -> None:
    _atomic_write_json(path, body)


def append_decision(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def append_equity(path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_state.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/state.py tests/copytrade/test_state.py
git commit -m "feat(copytrade): atomic state persistence + jsonl helpers"
```

---

## Chunk 2 review checkpoint

Run the full copytrade test suite:

```bash
pytest tests/copytrade/ -v
```

Expected: 43 passed (20 from chunk 1 + 23 from chunk 2).

Dispatch plan-document-reviewer on Chunk 2 before proceeding.

---

## Chunk 3: Runner + service + smoke tests

### Task 10: runner.py — pure sizing function (testable in isolation)

**Files:**
- Create: `live/copytrade/runner.py` (partial — just the sizing helper)
- Test: `tests/copytrade/test_runner_sizing.py`

- [ ] **Step 1: Write failing test for the sizing function**

Create `tests/copytrade/test_runner_sizing.py`:

```python
"""Pure-function tests of compute_paper_size — no IO."""
from live.copytrade.runner import compute_paper_size


def test_basic_proportional():
    """Target uses 5% of AUM → bot uses 5% of paper capital."""
    out = compute_paper_size(trade_size_usd=5_000, target_aum=100_000,
                             capital_per_wallet=333.33)
    assert abs(out - 333.33 * 0.05) < 1e-6  # 16.67


def test_clamp_at_50_percent():
    """Target uses 90% of AUM → bot clamped to 50%."""
    out = compute_paper_size(trade_size_usd=90_000, target_aum=100_000,
                             capital_per_wallet=333.33)
    assert abs(out - 333.33 * 0.5) < 1e-6


def test_zero_aum_returns_zero():
    """AUM unknown → skip (size 0)."""
    out = compute_paper_size(trade_size_usd=100, target_aum=0,
                             capital_per_wallet=333.33)
    assert out == 0.0


def test_negative_inputs_return_zero():
    assert compute_paper_size(-1, 100, 333) == 0.0
    assert compute_paper_size(1, -1, 333) == 0.0
```

- [ ] **Step 2: Run — must fail**

Run: `pytest tests/copytrade/test_runner_sizing.py -v`
Expected: 4 failures

- [ ] **Step 3: Implement compute_paper_size**

Create `live/copytrade/runner.py`:

```python
"""bot-cp main runner — polling loop + orchestration."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_TRADE_PCT = 0.50
MIN_PAPER_SIZE_USD = 1.0


def compute_paper_size(
    trade_size_usd: float,
    target_aum: float,
    capital_per_wallet: float,
) -> float:
    """Return the paper-trade size (USD) to mirror a target's trade.

    Logic: trade_pct = trade_size_usd / target_aum, clamped to [0, MAX_TRADE_PCT].
    Returns 0 on invalid inputs (negative or zero AUM).
    """
    if trade_size_usd <= 0 or target_aum <= 0 or capital_per_wallet <= 0:
        return 0.0
    trade_pct = min(trade_size_usd / target_aum, MAX_TRADE_PCT)
    return capital_per_wallet * trade_pct
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_runner_sizing.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/runner.py tests/copytrade/test_runner_sizing.py
git commit -m "feat(copytrade): compute_paper_size pure function"
```

---

### Task 11: runner.py — trade processing (one-cycle integration test)

**Files:**
- Modify: `live/copytrade/runner.py`
- Modify: `tests/copytrade/test_runner_sizing.py`

- [ ] **Step 1: Append integration test using mocked Data API + tmp paths**

Append to `tests/copytrade/test_runner_sizing.py`:

```python
from unittest.mock import patch

from live.copytrade import runner as runner_mod
from live.copytrade.paper_portfolio import PaperPortfolio


def _trade(ts, side, size, price, condition="0xC", asset="42", outcome_index=0):
    return {
        "timestamp": ts, "side": side, "size": size, "price": price,
        "conditionId": condition, "asset": asset, "outcomeIndex": outcome_index,
        "outcome": "Yes", "title": "Test market",
        "transactionHash": f"0x{ts:x}",
    }


def test_process_new_buy_creates_position(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    last_seen = 0
    trades = [_trade(ts=100, side="BUY", size=500, price=0.5)]

    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=10_000.0):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen, capital_per_wallet=1000.0,
        )

    assert new_last_seen == 100
    assert len(decisions) == 1
    assert decisions[0]["action"] == "executed"
    # trade_pct = (500 * 0.5) / 10_000 = 0.025 → paper_size = 25
    assert pf.cash_usd == pytest.approx(1000.0 - 25.0)
    assert len(pf.positions) == 1


def test_skip_already_seen(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    trades = [_trade(ts=50, side="BUY", size=500, price=0.5)]

    with patch("live.copytrade.runner.data_api.trades", return_value=[]):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=100, capital_per_wallet=1000.0,
        )

    assert new_last_seen == 100
    assert decisions == []


def test_sell_reduces_position(tmp_path):
    target = {"pseudonym": "T1", "wallet": "0xW", "allocation_pct": 1.0}
    pf = PaperPortfolio(wallet="T1", cash_usd=1000.0)
    # Seed a position the target also has (10 shares at 0.5)
    pf.buy(condition_id="0xC", asset="42", outcome="Yes", outcome_index=0,
           price=0.5, usd_size=5.0, target_hash="0xseed",
           market_title="m", opened_ts=50)

    # Target sells 50% of its on-chain position. Mock target_position_size_at
    # to return its full pre-sell size = 100 shares. Trade sells 50 → frac = 0.5.
    trades = [_trade(ts=200, side="SELL", size=50, price=0.7)]

    with patch("live.copytrade.runner.data_api.trades", return_value=trades), \
         patch("live.copytrade.runner.data_api.target_position_size_at",
               return_value=100.0), \
         patch("live.copytrade.runner.aum_estimator.aum", return_value=10_000.0):
        new_last_seen, decisions = runner_mod.process_wallet(
            target, pf, last_seen_ts=0, capital_per_wallet=1000.0,
        )

    # Paper position size was 10 (5 USD / 0.5), half sold → 5 left
    assert pf.positions[0]["size"] == pytest.approx(5.0)
    assert new_last_seen == 200
    assert decisions[0]["action"] == "executed"
```

You need `import pytest` near the top of the test file — add it if missing.

- [ ] **Step 2: Run — must fail (process_wallet undefined)**

Run: `pytest tests/copytrade/test_runner_sizing.py -v`
Expected: 3 new failures.

- [ ] **Step 3: Implement process_wallet**

Append to `live/copytrade/runner.py`:

```python
from live.copytrade import aum_estimator, data_api
from live.copytrade.paper_portfolio import PaperPortfolio


def process_wallet(
    target: dict,
    portfolio: PaperPortfolio,
    last_seen_ts: int,
    capital_per_wallet: float,
) -> tuple[int, list[dict]]:
    """Fetch new trades for target.wallet, mirror each into `portfolio`.

    Returns:
        (new_last_seen_ts, decisions) where decisions is a list of structured
        records (one per detected trade, including skipped).
    """
    wallet = target["wallet"]
    new_trades = data_api.trades(wallet, limit=50, since_ts=last_seen_ts)
    decisions: list[dict] = []
    if not new_trades:
        return last_seen_ts, decisions

    # process oldest first so dedup / position state evolves correctly
    for t in sorted(new_trades, key=lambda x: int(x["timestamp"])):
        ts = int(t["timestamp"])
        side = t.get("side")
        price = float(t.get("price", 0.0))
        size_shares = float(t.get("size", 0.0))
        # Polymarket's `size` is in outcome tokens (shares). USD = shares * price.
        trade_usd = size_shares * price
        condition_id = t.get("conditionId")
        outcome_index = int(t.get("outcomeIndex", 0))
        asset = t.get("asset", "")
        outcome = t.get("outcome", "")
        market_title = t.get("title", "")
        target_hash = t.get("transactionHash", "")

        target_aum = aum_estimator.aum(wallet)
        paper_size = compute_paper_size(trade_usd, target_aum, capital_per_wallet)

        decision = {
            "ts": ts, "wallet": target["pseudonym"], "target_hash": target_hash,
            "side": side, "market": market_title, "outcome": outcome,
            "target_size_usd": trade_usd, "target_aum_estimate": target_aum,
            "trade_pct": (trade_usd / target_aum) if target_aum else 0,
            "paper_size_usd": paper_size, "price": price,
        }

        if paper_size < MIN_PAPER_SIZE_USD:
            decision["action"] = "skipped"
            decision["rationale"] = (
                "paper_size_below_threshold" if paper_size > 0 else "zero_aum_or_zero_trade"
            )
            decisions.append(decision)
            last_seen_ts = max(last_seen_ts, ts)
            continue

        if side == "BUY":
            portfolio.buy(
                condition_id=condition_id, asset=asset, outcome=outcome,
                outcome_index=outcome_index, price=price, usd_size=paper_size,
                target_hash=target_hash, market_title=market_title, opened_ts=ts,
            )
            decision["action"] = "executed"
            decision["rationale"] = "buy_mirrored"
        elif side == "SELL":
            target_size_before = data_api.target_position_size_at(
                wallet, condition_id, outcome_index, ts=ts,
            )
            if target_size_before <= 0:
                fraction = 1.0
            else:
                fraction = min(size_shares / target_size_before, 1.0)
            portfolio.sell(
                condition_id=condition_id, outcome_index=outcome_index,
                fraction=fraction, price=price, target_hash=target_hash, ts=ts,
            )
            decision["action"] = "executed"
            decision["rationale"] = f"sell_mirrored_fraction={fraction:.4f}"
        else:
            decision["action"] = "skipped"
            decision["rationale"] = f"unknown_side={side}"

        decisions.append(decision)
        last_seen_ts = max(last_seen_ts, ts)

    return last_seen_ts, decisions
```

- [ ] **Step 4: Run — must pass**

Run: `pytest tests/copytrade/test_runner_sizing.py -v`
Expected: 7 passed total

- [ ] **Step 5: Commit**

```bash
git add live/copytrade/runner.py tests/copytrade/test_runner_sizing.py
git commit -m "feat(copytrade): process_wallet — trade detection and mirror"
```

---

### Task 12: runner.py — main loop with state persistence

**Files:**
- Modify: `live/copytrade/runner.py`

- [ ] **Step 1: Append the main loop**

Append to `live/copytrade/runner.py`:

```python
import os
import signal
import sys
import time
from pathlib import Path

from live.copytrade import state as state_mod
from live.copytrade.targets import (
    CAPITAL_PER_WALLET,
    PAPER_CAPITAL_USD,
    TARGETS,
)

LOG_DIR = Path(os.getenv("BOT_CP_LOG_DIR", "logs/copytrade"))
POLL_INTERVAL_S = int(os.getenv("BOT_CP_POLL_S", "60"))

_stop = False


def _signal_handler(signum, _frame):
    global _stop
    log.info("received signal %d, stopping after current cycle", signum)
    _stop = True


def _load_portfolios() -> dict[str, PaperPortfolio]:
    portfolio_path = LOG_DIR / "portfolio.json"
    raw = state_mod.load_portfolio(str(portfolio_path))
    out: dict[str, PaperPortfolio] = {}
    for t in TARGETS:
        pseudo = t["pseudonym"]
        if pseudo in raw:
            out[pseudo] = PaperPortfolio.from_dict(raw[pseudo])
        else:
            out[pseudo] = PaperPortfolio(wallet=pseudo, cash_usd=CAPITAL_PER_WALLET)
    return out


def _save_portfolios(portfolios: dict[str, PaperPortfolio]) -> None:
    portfolio_path = LOG_DIR / "portfolio.json"
    body = {pseudo: pf.to_dict() for pseudo, pf in portfolios.items()}
    state_mod.save_portfolio(str(portfolio_path), body)


# Track last-snapshotted UTC date so we append at most once per day
_last_equity_date: str | None = None


def _maybe_snapshot_equity(portfolios: dict[str, PaperPortfolio]) -> None:
    """Append a daily MTM snapshot to equity.jsonl when the UTC date changes.

    For MTM we use the position avg_price as fallback (no live price fetch
    each cycle to stay light). The dashboard can compute richer MTM on demand.
    """
    from datetime import datetime, timezone

    global _last_equity_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_equity_date == today:
        return

    per_wallet = {}
    total = 0.0
    for pseudo, pf in portfolios.items():
        eq = pf.equity({})  # avg_price fallback
        per_wallet[pseudo] = eq
        total += eq

    state_mod.append_equity(str(LOG_DIR / "equity.jsonl"), {
        "ts": int(time.time()),
        "date": today,
        "per_wallet_eq": per_wallet,
        "total_eq": total,
    })
    _last_equity_date = today
    log.info("equity snapshot for %s: total=$%.2f", today, total)


def _smoke_test() -> None:
    """Hit Data API once to fail-fast on geoblock / network at boot."""
    test_wallet = TARGETS[0]["wallet"]
    try:
        data_api.trades(test_wallet, limit=1)
        log.info("smoke test ok: Data API reachable")
    except data_api.DataAPIError as e:
        log.error("smoke test failed: %s — aborting", e)
        sys.exit(2)


def run() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    log.info("bot-cp starting: capital=$%.2f, %d wallets, poll=%ds",
             PAPER_CAPITAL_USD, len(TARGETS), POLL_INTERVAL_S)
    _smoke_test()

    state = state_mod.load_state(str(LOG_DIR / "state.json"))
    last_seen = state.get("last_seen_ts", {})
    portfolios = _load_portfolios()

    decisions_path = str(LOG_DIR / "decisions.jsonl")

    while not _stop:
        cycle_start = time.time()
        for t in TARGETS:
            pseudo = t["pseudonym"]
            wallet = t["wallet"]
            try:
                new_ts, decisions = process_wallet(
                    t, portfolios[pseudo],
                    last_seen_ts=int(last_seen.get(wallet, 0)),
                    capital_per_wallet=CAPITAL_PER_WALLET,
                )
                for d in decisions:
                    state_mod.append_decision(decisions_path, d)
                if new_ts > int(last_seen.get(wallet, 0)):
                    last_seen[wallet] = new_ts
                if decisions:
                    log.info("%s: %d new trade(s) processed", pseudo, len(decisions))
            except Exception:
                log.exception("error processing %s, continuing", pseudo)

        # Persist after each cycle
        state_mod.save_state(str(LOG_DIR / "state.json"), {"last_seen_ts": last_seen})
        _save_portfolios(portfolios)

        # Daily equity snapshot (UTC date change triggers append)
        _maybe_snapshot_equity(portfolios)

        # Sleep, but check stop flag every second
        elapsed = time.time() - cycle_start
        remaining = max(0.0, POLL_INTERVAL_S - elapsed)
        slept = 0.0
        while slept < remaining and not _stop:
            time.sleep(1.0)
            slept += 1.0

    log.info("bot-cp stopped cleanly")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Smoke-test the import (no functional test of `run()` — that's covered by deployment smoke)**

```bash
python -c "from live.copytrade import runner; print('runner import ok'); print('compute_paper_size:', runner.compute_paper_size(100, 1000, 333.33))"
```

Expected output: `runner import ok` and a non-zero number.

- [ ] **Step 3: Re-run the full unit suite**

```bash
pytest tests/copytrade/ -v
```

Expected: 50 passed (all previous + the 7 runner tests). No new tests added in this step.

- [ ] **Step 4: Commit**

```bash
git add live/copytrade/runner.py
git commit -m "feat(copytrade): main polling loop and graceful shutdown"
```

---

### Task 13: Telegram integration

**Files:**
- Modify: `live/copytrade/runner.py`

- [ ] **Step 1: Add Telegram alerts in run()**

Edit `live/copytrade/runner.py`:

At the top with other `from live...` imports add:
```python
from live import notifier
```

Replace the existing `log.info("bot-cp starting: ...")` line in `run()` with the same line followed by:
```python
    try:
        notifier.notify(
            f"🟢 bot-cp démarré — capital ${PAPER_CAPITAL_USD:.0f}, "
            f"{len(TARGETS)} wallets ({', '.join(t['pseudonym'] for t in TARGETS)})"
        )
    except Exception:
        log.exception("telegram start notify failed (non-fatal)")
```

Modify the existing `for d in decisions:` persistence loop inside the cycle (single pass — do not add a second loop). Replace the existing loop body with:

```python
                for d in decisions:
                    state_mod.append_decision(decisions_path, d)
                    if d.get("action") != "executed":
                        continue
                    try:
                        side_emoji = "🟩" if d["side"] == "BUY" else "🟥"
                        notifier.notify(
                            f"{side_emoji} CP {pseudo} {d['side']} "
                            f"{d.get('market', '?')[:50]} / {d.get('outcome', '?')}\n"
                            f"size ${d['paper_size_usd']:.2f} @ {d['price']:.3f} "
                            f"(target ${d['target_size_usd']:.0f} / "
                            f"{d['trade_pct']*100:.1f}% AUM)"
                        )
                    except Exception:
                        log.exception("telegram trade notify failed (non-fatal)")
```

At the end of `run()` (just before `log.info("bot-cp stopped cleanly")`), add:
```python
    try:
        notifier.notify("🛑 bot-cp arrêté")
    except Exception:
        log.exception("telegram stop notify failed (non-fatal)")
```

- [ ] **Step 2: Verify it still imports**

```bash
python -c "from live.copytrade import runner; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Run full suite — no regression**

```bash
pytest tests/copytrade/ -v
```

Expected: 50 passed

- [ ] **Step 4: Commit**

```bash
git add live/copytrade/runner.py
git commit -m "feat(copytrade): telegram alerts on start, stop, and each executed trade"
```

---

### Task 14: Dashboard route

**Files:**
- Modify: `dashboard/app.py`

- [ ] **Step 1: Locate the existing route registration**

```bash
grep -n "@app.route\|def api_" dashboard/app.py | head -20
```

Note the file structure to know where to add the new route.

- [ ] **Step 2: Add the /api/copytrade route**

Edit `dashboard/app.py`. After the last `@app.route(...)` block, add:

```python
@app.route("/api/copytrade")
def api_copytrade():
    """Read-only snapshot of the bot-cp paper portfolio + recent decisions."""
    import json
    import os
    from pathlib import Path

    log_dir = Path(os.getenv("BOT_CP_LOG_DIR", "logs/copytrade"))
    portfolio_path = log_dir / "portfolio.json"
    decisions_path = log_dir / "decisions.jsonl"
    equity_path = log_dir / "equity.jsonl"

    portfolio: dict = {}
    if portfolio_path.exists():
        try:
            with open(portfolio_path) as f:
                portfolio = json.load(f)
        except Exception:
            pass

    recent: list = []
    if decisions_path.exists():
        try:
            with open(decisions_path) as f:
                lines = f.readlines()[-50:]
            recent = [json.loads(line) for line in lines if line.strip()]
        except Exception:
            pass

    equity_curve: list = []
    if equity_path.exists():
        try:
            with open(equity_path) as f:
                equity_curve = [json.loads(line) for line in f if line.strip()]
        except Exception:
            pass

    return {
        "portfolio": portfolio,
        "recent_decisions": recent,
        "equity_curve": equity_curve,
    }
```

- [ ] **Step 3: Smoke test the route**

Boot the dashboard locally (skip if Flask app needs heavy setup):
```bash
python -c "
from dashboard.app import app
client = app.test_client()
resp = client.get('/api/copytrade')
print('status:', resp.status_code)
print('keys:', list(resp.json.keys()))
"
```

Expected: `status: 200`, `keys: ['portfolio', 'recent_decisions', 'equity_curve']`

- [ ] **Step 4: Commit**

```bash
git add dashboard/app.py
git commit -m "feat(dashboard): /api/copytrade read-only endpoint"
```

---

### Task 15: Systemd unit + README

**Files:**
- Create: `deploy/bot-cp.service`
- Create: `live/copytrade/README.md`

- [ ] **Step 1: Create the systemd unit**

Create `deploy/bot-cp.service`:

```ini
[Unit]
Description=Bot CopyTrade Paper (Polymarket)
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/bot-trading
EnvironmentFile=-/home/botuser/bot-trading/.env
ExecStart=/home/botuser/bot-trading/venv/bin/python -m live.copytrade.runner
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/botuser/bot-trading/logs/copytrade/copytrade.log
StandardError=append:/home/botuser/bot-trading/logs/copytrade/copytrade.log

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create the operator README**

Create `live/copytrade/README.md`:

````markdown
# Bot CopyTrade Paper (bot-cp)

Paper-trading mirror of 3 Polymarket top wallets. See spec at
`docs/superpowers/specs/2026-05-15-polymarket-copytrade-bot-design.md`.

## Local run

```bash
python -m live.copytrade.runner
```

Env vars:
- `BOT_CP_CAPITAL_USD` — total paper capital (default 1000)
- `BOT_CP_POLL_S` — polling interval seconds (default 60)
- `BOT_CP_LOG_DIR` — output dir (default `logs/copytrade`)

## VPS deployment

```bash
sudo cp deploy/bot-cp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bot-cp
sudo systemctl status bot-cp
sudo tail -f /home/botuser/bot-trading/logs/copytrade/copytrade.log
```

## State files

```
logs/copytrade/
├── state.json          last_seen_ts per wallet
├── portfolio.json      cash + positions per wallet
├── decisions.jsonl     each detected trade + copy outcome
├── equity.jsonl        daily MTM snapshot
└── copytrade.log       stdout + stderr
```

## Reset

```bash
sudo systemctl stop bot-cp
sudo rm -f /home/botuser/bot-trading/logs/copytrade/{state,portfolio}.json \
           /home/botuser/bot-trading/logs/copytrade/{decisions,equity}.jsonl
sudo systemctl start bot-cp
```

## Tests

```bash
pytest tests/copytrade/ -v
```

Integration smoke test (hits real Polymarket API):
```bash
pytest tests/copytrade/test_data_api_smoke.py -v --run-integration
```
````

- [ ] **Step 3: Commit**

```bash
git add deploy/bot-cp.service live/copytrade/README.md
git commit -m "feat(copytrade): systemd unit + operator README"
```

---

### Task 16: Integration smoke test (real Data API)

**Files:**
- Create: `tests/copytrade/test_data_api_smoke.py`
- Modify: `tests/copytrade/conftest.py` (create)

- [ ] **Step 1: Create conftest with --run-integration flag**

Create `tests/copytrade/conftest.py`:

```python
"""Shared fixtures + integration-marker plumbing."""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit external APIs",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: mark test as hitting external services",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="needs --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
```

- [ ] **Step 2: Create the smoke test**

Create `tests/copytrade/test_data_api_smoke.py`:

```python
"""Integration smoke test — hits real Polymarket Data API. Skipped by default."""
import pytest

from live.copytrade import data_api
from live.copytrade.targets import TARGETS


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS, ids=[t["pseudonym"] for t in TARGETS])
def test_trades_returns_list_for_target(target):
    out = data_api.trades(target["wallet"], limit=5)
    assert isinstance(out, list)


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS, ids=[t["pseudonym"] for t in TARGETS])
def test_positions_returns_list_for_target(target):
    out = data_api.positions(target["wallet"])
    assert isinstance(out, list)


@pytest.mark.integration
@pytest.mark.parametrize("target", TARGETS, ids=[t["pseudonym"] for t in TARGETS])
def test_value_returns_scalar_for_target(target):
    v = data_api.value(target["wallet"])
    assert isinstance(v, float)
    assert v >= 0
```

- [ ] **Step 3: Verify it is skipped by default**

```bash
pytest tests/copytrade/test_data_api_smoke.py -v
```

Expected: 9 skipped (3 tests × 3 wallets, but all marked integration so all skip)

- [ ] **Step 4: Run with --run-integration once to verify the API works**

```bash
pytest tests/copytrade/test_data_api_smoke.py -v --run-integration
```

Expected: 9 passed (3 wallets × 3 endpoints). If any fail, investigate before deploying.

- [ ] **Step 5: Commit**

```bash
git add tests/copytrade/conftest.py tests/copytrade/test_data_api_smoke.py
git commit -m "test(copytrade): integration smoke test (opt-in via --run-integration)"
```

---

## Chunk 3 review checkpoint (mid)

Run unit + integration:

```bash
pytest tests/copytrade/ -v
pytest tests/copytrade/test_data_api_smoke.py -v --run-integration
```

Expected: 50 unit passed + 9 integration passed.

Dispatch plan-document-reviewer on Chunk 3 before proceeding to Chunk 4.

---

## Chunk 4: Replay tool + UI + deploy

### Task 17: scripts/replay_30d.py — historical equity check

**Files:**
- Create: `scripts/replay_30d.py`

- [ ] **Step 1: Create the script**

Create `scripts/replay_30d.py`:

```python
#!/usr/bin/env python3
"""Replay last 30 days of target trades to compute a retroactive paper equity
curve. Used as a sanity check BEFORE running the bot live-forward.

Usage:
    python scripts/replay_30d.py

Writes a `replay_equity.csv` and `replay_decisions.jsonl` next to the script
output dir (default: `backtest/results/copytrade/`).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from live.copytrade import data_api, runner
from live.copytrade.paper_portfolio import PaperPortfolio
from live.copytrade.targets import CAPITAL_PER_WALLET, TARGETS

log = logging.getLogger("replay")

OUT_DIR = Path("backtest/results/copytrade")
WINDOW_S = 30 * 24 * 3600


def replay() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = int(time.time()) - WINDOW_S
    portfolios = {t["pseudonym"]: PaperPortfolio(wallet=t["pseudonym"],
                                                  cash_usd=CAPITAL_PER_WALLET)
                  for t in TARGETS}

    decisions_log = OUT_DIR / "replay_decisions.jsonl"
    if decisions_log.exists():
        decisions_log.unlink()

    for t in TARGETS:
        all_trades = data_api.trades(t["wallet"], limit=500)
        recent = [tr for tr in all_trades if int(tr["timestamp"]) >= cutoff]
        log.info("%s: %d trades in last 30d", t["pseudonym"], len(recent))
        for tr in sorted(recent, key=lambda x: int(x["timestamp"])):
            ts = int(tr["timestamp"])
            target_aum = data_api.value(t["wallet"]) or 1.0
            paper_size = runner.compute_paper_size(
                float(tr.get("size", 0)) * float(tr.get("price", 0)),
                target_aum,
                CAPITAL_PER_WALLET,
            )
            if paper_size < runner.MIN_PAPER_SIZE_USD:
                continue
            pf = portfolios[t["pseudonym"]]
            if tr["side"] == "BUY":
                pf.buy(
                    condition_id=tr["conditionId"], asset=tr.get("asset", ""),
                    outcome=tr.get("outcome", ""), outcome_index=int(tr.get("outcomeIndex", 0)),
                    price=float(tr["price"]), usd_size=paper_size,
                    target_hash=tr.get("transactionHash", ""),
                    market_title=tr.get("title", ""), opened_ts=ts,
                )
            elif tr["side"] == "SELL":
                size_before = data_api.target_position_size_at(
                    t["wallet"], tr["conditionId"],
                    int(tr.get("outcomeIndex", 0)), ts,
                )
                frac = (float(tr["size"]) / size_before) if size_before else 1.0
                pf.sell(condition_id=tr["conditionId"],
                        outcome_index=int(tr.get("outcomeIndex", 0)),
                        fraction=min(frac, 1.0), price=float(tr["price"]),
                        target_hash=tr.get("transactionHash", ""), ts=ts)
            with open(decisions_log, "a") as f:
                f.write(json.dumps({
                    "ts": ts, "wallet": t["pseudonym"], "side": tr["side"],
                    "paper_size_usd": paper_size,
                    "cash_usd_after": pf.cash_usd,
                    "n_positions_after": len(pf.positions),
                }) + "\n")

    # Final equity (uses avg_price fallback for unknown current prices to keep
    # the script self-contained; the live runner uses real CLOB prices).
    total = 0.0
    print(f"\n{'Wallet':<15} {'Cash':>10} {'Positions':>10} {'Equity':>10}")
    for pseudo, pf in portfolios.items():
        eq = pf.equity({})
        total += eq
        print(f"{pseudo:<15} {pf.cash_usd:>10.2f} {len(pf.positions):>10} {eq:>10.2f}")
    initial = CAPITAL_PER_WALLET * len(TARGETS)
    print(f"\nTOTAL equity:   ${total:.2f}  (initial ${initial:.2f}, "
          f"PnL {(total - initial) / initial * 100:+.2f}%)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    replay()
```

- [ ] **Step 2: Run it (live API call, will take ~30s)**

```bash
python scripts/replay_30d.py
```

Expected: Prints per-wallet PnL + total. Output also written to `backtest/results/copytrade/replay_decisions.jsonl`.

If output looks pathological (e.g., total equity = 0, no trades found), investigate the API responses before claiming victory.

- [ ] **Step 3: Commit**

```bash
git add scripts/replay_30d.py
git commit -m "feat(copytrade): replay_30d.py retroactive equity curve script"
```

---

### Task 18: Dashboard tab (UI)

**Files:**
- Modify: `dashboard/templates/index.html`

The existing template (verified 2026-05-15) uses three coordinated blocks:
1. **Top nav** : `<nav class="nav">…<button class="nav-btn" onclick="switchTab('xxx')">Label</button>…</nav>` (around line 397)
2. **Bottom nav (mobile)** : `<nav class="bottom-nav">…<button class="bnav-btn" onclick="switchTab('xxx')" id="bnav-xxx">…</button>…</nav>` (around line 410)
3. **Tab content** : `<div id="tab-xxx" class="tab-content">…</div>` (sequential blocks after `<div class="main">`)

`switchTab('xxx')` is a global JS function that hides all `.tab-content`, shows `#tab-xxx`, and updates `.nav-btn` + `.bnav-btn` active class. We do not need to touch it.

- [ ] **Step 1: Add the top-nav button**

In `dashboard/templates/index.html`, locate the `<nav class="nav">` block (≈ line 397). After the last `<button class="nav-btn" ...>Documentation</button>` line, insert:

```html
  <button class="nav-btn" onclick="switchTab('copytrade')">CopyTrade <span class="nav-badge" id="nb-cp">—</span></button>
```

- [ ] **Step 2: Add the bottom-nav button (mobile)**

Locate the `<nav class="bottom-nav">` block. After the last `<button class="bnav-btn"…>Logs</button>` block, insert:

```html
  <button class="bnav-btn" onclick="switchTab('copytrade')" id="bnav-copytrade">
    <span class="bnav-icon">📋</span>
    <span class="bnav-lbl">CopyTrade</span>
  </button>
```

- [ ] **Step 3: Add the tab content block**

After the last existing `<div id="tab-…" class="tab-content">…</div>` block (the docs tab around line 718, end of its closing `</div>`), insert:

```html
<div id="tab-copytrade" class="tab-content">
  <h2 style="margin:0 0 12px 0">CopyTrade (paper)</h2>
  <div id="cp-wallets" style="display:grid;gap:8px;margin-bottom:16px">Loading…</div>
  <h3 style="margin:16px 0 8px 0">Recent decisions (last 20)</h3>
  <table id="cp-decisions" style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="border-bottom:1px solid var(--border)">
      <th style="text-align:left;padding:6px">UTC</th>
      <th style="text-align:left">Wallet</th>
      <th>Side</th>
      <th style="text-align:left">Market</th>
      <th style="text-align:right">Size</th>
      <th>Action</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>
```

- [ ] **Step 4: Add the loader script + auto-refresh**

Find the closing `</body>` (last lines). Just before it, insert:

```html
<script>
async function loadCopytrade() {
  try {
    const r = await fetch('/api/copytrade');
    if (!r.ok) return;
    const data = await r.json();
    const w = document.getElementById('cp-wallets');
    if (!w) return;
    w.innerHTML = '';
    let totalEq = 0;
    for (const [pseudo, pf] of Object.entries(data.portfolio || {})) {
      const eq = (pf.cash_usd || 0) + (pf.positions || [])
        .reduce((s, p) => s + (p.size || 0) * (p.avg_price || 0), 0);
      totalEq += eq;
      w.insertAdjacentHTML('beforeend',
        `<div style="padding:8px;border:1px solid var(--border);border-radius:4px">` +
        `<b>${pseudo}</b>: cash $${(pf.cash_usd||0).toFixed(2)} · ` +
        `${(pf.positions||[]).length} pos · equity $${eq.toFixed(2)} ` +
        `· realized $${(pf.realized_pnl_usd||0).toFixed(2)}</div>`);
    }
    const badge = document.getElementById('nb-cp');
    if (badge) badge.textContent = `$${totalEq.toFixed(0)}`;
    const tb = document.querySelector('#cp-decisions tbody');
    if (tb) {
      tb.innerHTML = '';
      const recent = (data.recent_decisions || []).slice(-20).reverse();
      for (const d of recent) {
        const ts = new Date((d.ts||0)*1000).toISOString().slice(0,16).replace('T',' ');
        tb.insertAdjacentHTML('beforeend',
          `<tr style="border-bottom:1px solid var(--border)">` +
          `<td style="padding:4px">${ts}</td>` +
          `<td>${d.wallet||''}</td>` +
          `<td style="text-align:center">${d.side||''}</td>` +
          `<td>${(d.market||'').slice(0,40)}</td>` +
          `<td style="text-align:right">$${(d.paper_size_usd||0).toFixed(2)}</td>` +
          `<td style="text-align:center">${d.action||''}</td></tr>`);
      }
    }
  } catch (e) { console.warn('loadCopytrade failed', e); }
}
loadCopytrade();
setInterval(loadCopytrade, 60000);
</script>
```

The badge and table refresh every 60s. No tab-activation hook needed since the initial load + setInterval keeps the data fresh whether the tab is visible or not.

- [ ] **Step 5: Manual visual check**

Start the dashboard locally:
```bash
python dashboard/app.py &
curl http://localhost:5000/api/copytrade
```

Then open `http://localhost:5000` in a browser and click the CopyTrade tab. Empty data is fine (bot hasn't run yet).

- [ ] **Step 6: Commit**

```bash
git add dashboard/templates/index.html
git commit -m "feat(dashboard): CopyTrade tab"
```

---

### Task 19: Deploy to VPS

**Files:** `CLAUDE.md`

**Pre-flight rollback plan** (read before starting): if any step from 2 onwards fails, the safe undo is:
```bash
ssh ubuntu@51.210.13.248 'sudo systemctl disable --now bot-cp 2>/dev/null; sudo rm -f /etc/systemd/system/bot-cp.service; sudo systemctl daemon-reload'
git revert HEAD && git push   # revert the last code commit if push already happened
```
`bot.service` and `shadow.service` are not touched by any step below — they keep running. The only shared component is `dashboard/app.py` and the `/api/copytrade` route is purely additive (read-only file probes); if the dashboard fails to start, revert the dashboard commit only.

- [ ] **Step 1: Push to main**

```bash
git status
git log --oneline -20
git push origin main
```

CI/CD on the VPS pulls main and restarts `bot.service` + `dashboard.service` (≈30s). `bot-cp.service` is NOT yet on the VPS so it stays absent — no harm from this push.

After ~45s, sanity check that the dashboard still works:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://vps-957c8713.vps.ovh.net/
```
Expected: `200`. If 500/502, revert the dashboard commit (`git revert <sha> && git push`) and stop here.

- [ ] **Step 2: Install the new service on the VPS**

```bash
ssh ubuntu@51.210.13.248 'sudo cp /home/botuser/bot-trading/deploy/bot-cp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now bot-cp && sudo systemctl status bot-cp --no-pager'
```

Expected: `active (running)`.
If `failed`: run the rollback block above before debugging.

- [ ] **Step 3: Inspect the first cycle (no `-f`)**

```bash
sleep 90
ssh ubuntu@51.210.13.248 'sudo tail -n 80 /home/botuser/bot-trading/logs/copytrade/copytrade.log'
```

Expected lines:
- `bot-cp starting: capital=$1000.00, 3 wallets, poll=60s`
- `smoke test ok: Data API reachable`
- For each wallet : `0 new trade(s)` or `N new trade(s) processed`
- Telegram message received : `🟢 bot-cp démarré`

If the smoke test failed (`aborting` in the log) → Data API is unreachable from the VPS (rare but possible if Polymarket adds geo on read endpoints). Disable the service and investigate before re-enabling.

- [ ] **Step 4: Verify the dashboard endpoint**

```bash
curl -s https://vps-957c8713.vps.ovh.net/api/copytrade | python -m json.tool | head -40
```

Expected: JSON with `portfolio`, `recent_decisions`, `equity_curve` keys (may be empty initially — that's fine).

- [ ] **Step 5: Watch for 30 minutes**

Open Telegram. Within 30 minutes there should either be at least one copy-trade alert (`🟩 CP ... BUY ...` or `🟥 CP ... SELL ...`) or nothing if no target traded — both are acceptable. If you see error-level log spam on the VPS:
```bash
ssh ubuntu@51.210.13.248 'sudo grep -i error /home/botuser/bot-trading/logs/copytrade/copytrade.log | tail -20'
```
Error rate > 5/cycle → rollback.

- [ ] **Step 6: Update CLAUDE.md and MEMORY.md**

Edit `CLAUDE.md`. Locate the `## Shadow Bot — Moteur unifie en parallele` section (the one ending just before `## Revue 2026-04-30`). After its `### Reset complet` block, insert a new top-level section before `## Revue 2026-04-30`:

```markdown
## Bot CopyTrade — Paper mirror Polymarket (2026-05-15)

Bot paper qui mirror 3 wallets profitables Polymarket (RN1, bossoskil1, surfandturf)
via la Data API publique. Lecture seule, aucun ordre réel.
Capital simulé : 1000 USDC (333.33 par wallet).

### Service

```bash
sudo systemctl status bot-cp
sudo systemctl restart bot-cp
sudo tail -f /home/botuser/bot-trading/logs/copytrade/copytrade.log
```

### State

```
/home/botuser/bot-trading/logs/copytrade/
├── state.json          ← last_seen_ts par wallet
├── portfolio.json      ← cash + positions par wallet
├── decisions.jsonl     ← chaque trade détecté + copie
├── equity.jsonl        ← snapshot quotidien MTM
└── copytrade.log       ← stdout
```

### Reset

```bash
sudo systemctl stop bot-cp
sudo rm /home/botuser/bot-trading/logs/copytrade/{state,portfolio}.json \
        /home/botuser/bot-trading/logs/copytrade/{decisions,equity}.jsonl
sudo systemctl start bot-cp
```

### Critères 30j → décision capitalisation

Capital final > $1000, Sharpe > 1.0, ≥20 trades, MaxDD < 20%, ≥50% trades résolus.
Si tous validés → projet "go live" (geoblock + USDC funding) séparé.
```

Also append to `MEMORY.md` (one-line entry under `## Sessions`):
```markdown
- [2026-05-15 bot-cp](session_2026-05-15_botcp.md) : **Bot CopyTrade paper déployé** — 3 wallets Polymarket (RN1, bossoskil1, surfandturf), 1000 USDC simulé, service bot-cp.service, dashboard /api/copytrade. 30j d'observation avant décision capitalisation.
```

- [ ] **Step 7: Commit docs and push**

```bash
git add CLAUDE.md
git commit -m "docs: bot-cp operator section in CLAUDE.md"
git push
```
The CI/CD will pull but `CLAUDE.md` is not loaded by any service → no restart impact.

---

## Chunk 4 review checkpoint

End-to-end check:

```bash
pytest tests/copytrade/ -v
pytest tests/copytrade/test_data_api_smoke.py -v --run-integration
python scripts/replay_30d.py
```

Plus on the VPS after deploy:
```bash
ssh ubuntu@51.210.13.248 'sudo systemctl status bot-cp --no-pager && curl -s http://localhost:5000/api/copytrade | python -m json.tool | head -30'
```

Dispatch plan-document-reviewer on Chunk 4.

---

## Done criteria

- [ ] `pytest tests/copytrade/ -v` → all pass
- [ ] `pytest tests/copytrade/ --run-integration -v` → all pass against real Polymarket API
- [ ] `python scripts/replay_30d.py` → produces a realistic equity curve (per-wallet PnL printed, no crashes)
- [ ] `bot-cp.service` running on VPS for ≥1 hour with non-empty `decisions.jsonl` OR clear log of "0 trades" cycles
- [ ] `/api/copytrade` returns valid JSON with 3 wallets in the portfolio
- [ ] Telegram start alert received

After 30 days of running, evaluate against the success criteria in the spec (final capital > $1000, Sharpe > 1.0, ≥ 20 trades, MaxDD < 20%, ≥ 50% of trades on resolved markets). If all five pass, plan v2 (live trading + geoblock).

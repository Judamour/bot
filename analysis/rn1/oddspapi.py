"""OddsPapi v5 client with strict 250-call budget control.

Free tier has 250 requests TOTAL (no monthly reset documented).
The client enforces a hard ceiling + 3h memory cache + persistent counter.

Environment :
  oddspapi_api : API key (lowercase as set in .env by user 2026-05-21)

Public endpoints (base https://v5.oddspapi.io/en) — all auth via ?apiKey=K :
  /fixtures/today                : Fixture[] (meta only, no odds)
  /fixtures-odds/main            : Fixture[] with main odds (h2h preferably)
  /sports                        : sport ID lookup (call once, cache to disk)

Hard guards :
  - Per-key memory cache 3h (re-call only after expiry)
  - Persistent call counter in data_bookarb/oddspapi_state.json
  - WARN at 200/250 (via log), HARD STOP at 240/250 (raise BudgetExceeded)
  - Min interval between paid calls = 10s (anti-burst)
"""
from __future__ import annotations
import json
import logging
import os
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

log = logging.getLogger("oddspapi")

BASE_URL = "https://v5.oddspapi.io/en"
DEFAULT_CACHE_TTL_S = 3 * 3600  # 3 hours
MIN_INTERVAL_S = 10
BUDGET_CAP = 250
BUDGET_WARN = 200
BUDGET_HARD_STOP = 240


class BudgetExceeded(RuntimeError):
    """Raised when API budget is exhausted."""


class OddsPapiClient:
    def __init__(self, state_dir: Path, cache_ttl_s: int = DEFAULT_CACHE_TTL_S):
        self.api_key = (
            os.environ.get("oddspapi_api")
            or os.environ.get("ODDSPAPI_API")
            or os.environ.get("ODDSPAPI_API_KEY")
            or ""
        )
        if not self.api_key:
            log.warning("OddsPapi API key not found in env (oddspapi_api). Client will refuse calls.")
        self.cache_ttl_s = cache_ttl_s
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "oddspapi_state.json"
        self._cache: dict[str, tuple[float, Any]] = {}
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except Exception:
                pass
        return {"calls_made": 0, "last_call_ts": 0, "calls_log": []}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        os.replace(tmp, self.state_path)

    @property
    def calls_made(self) -> int:
        return int(self._state.get("calls_made", 0))

    @property
    def remaining(self) -> int:
        return BUDGET_CAP - self.calls_made

    def _check_budget(self) -> None:
        if self.calls_made >= BUDGET_HARD_STOP:
            raise BudgetExceeded(
                f"OddsPapi budget exhausted: {self.calls_made}/{BUDGET_CAP} "
                f"(hard stop at {BUDGET_HARD_STOP}). Upgrade plan or wait."
            )
        if self.calls_made >= BUDGET_WARN:
            log.warning(f"OddsPapi budget warning: {self.calls_made}/{BUDGET_CAP} "
                        f"calls used ({self.remaining} remaining)")

    def _throttle(self) -> None:
        since_last = time.time() - float(self._state.get("last_call_ts", 0))
        if since_last < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - since_last)

    def _cache_key(self, path: str, params: dict) -> str:
        if not params:
            return path
        ordered = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{path}?{ordered}"

    def _get(self, path: str, params: dict | None = None, cache_ttl_s: int | None = None) -> Any:
        """Authenticated GET with cache + budget + throttle."""
        if not self.api_key:
            raise RuntimeError("OddsPapi API key missing — refusing to call")

        params = dict(params or {})
        cache_key = self._cache_key(path, params)
        ttl = cache_ttl_s if cache_ttl_s is not None else self.cache_ttl_s

        # Cache check
        if cache_key in self._cache:
            cached_at, payload = self._cache[cache_key]
            if (time.time() - cached_at) < ttl:
                log.debug(f"cache HIT {cache_key} (age {time.time()-cached_at:.0f}s/{ttl}s)")
                return payload
            else:
                log.debug(f"cache EXPIRED {cache_key} (age {time.time()-cached_at:.0f}s)")

        # Budget check before paid call
        self._check_budget()
        self._throttle()

        # Build URL (API key as query param per docs)
        params["apiKey"] = self.api_key
        qs = urllib.parse.urlencode(params)
        url = f"{BASE_URL}{path}?{qs}"
        log.info(f"oddspapi GET {path} (call {self.calls_made+1}/{BUDGET_CAP})")

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.loads(r.read())
            self._cache[cache_key] = (time.time(), payload)
            # Persist budget update
            self._state["calls_made"] = self.calls_made + 1
            self._state["last_call_ts"] = time.time()
            self._state.setdefault("calls_log", []).append({
                "ts": int(time.time()),
                "path": path,
                "params": {k: v for k, v in params.items() if k != "apiKey"},
                "calls_total": self.calls_made,
            })
            # Keep only last 100 calls in log
            self._state["calls_log"] = self._state["calls_log"][-100:]
            self._save_state()
            return payload
        except urllib.error.HTTPError as e:
            # 429 = rate limit, 402 = budget exhausted, 401 = bad auth, 404 = wrong path
            body = ""
            try: body = e.read().decode()[:300]
            except: pass
            log.error(f"oddspapi {e.code} on {path}: {body}")
            # Still count failed-due-to-budget calls
            if e.code in (402, 429):
                self._state["calls_made"] = self.calls_made + 1
                self._save_state()
            raise

    # ── Public methods ──────────────────────────────────────────────────────

    def get_sports(self) -> list[dict]:
        """List of sports (sportId, sportName). Cached to disk forever after first fetch."""
        sports_cache = self.state_dir / "oddspapi_sports.json"
        if sports_cache.exists():
            try:
                return json.loads(sports_cache.read_text())
            except Exception:
                pass
        # Fetch + persist
        data = self._get("/sports", cache_ttl_s=86400 * 30)  # 30-day mem cache too
        sports_cache.write_text(json.dumps(data, indent=2))
        return data

    def get_fixtures_today(self, sport_id: int | None = None,
                            bookmakers: str = "pinnacle") -> list[dict]:
        """List today's fixtures (sport meta only, no odds yet)."""
        params = {"bookmakers": bookmakers}
        if sport_id:
            params["sportId"] = sport_id
        return self._get("/fixtures/today", params)

    def get_fixtures_odds_main(self, sport_id: int | None = None,
                                bookmakers: str = "pinnacle") -> list[dict]:
        """List today's fixtures WITH main odds (h2h / moneyline)."""
        params = {"bookmakers": bookmakers}
        if sport_id:
            params["sportId"] = sport_id
        # This is the value-call: 1 call → all odds for the day for that sport
        return self._get("/fixtures-odds/main", params)


# ── Helper for matching Polymarket questions ↔ OddsPapi fixtures ───────────

def normalize_team_name(s: str) -> str:
    """Normalize for fuzzy matching: lowercase + strip common suffixes."""
    if not s: return ""
    s = s.lower().strip()
    for suffix in (" fc", " sc", " cf", " club", " united", " city"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    return s


def match_polymarket_to_fixture(poly_title: str, fixtures: list[dict]) -> dict | None:
    """Find OddsPapi fixture matching a Polymarket question.

    Polymarket titles look like 'PSG vs. Arsenal: O/U 1.5' or 'Will Arsenal win?'
    OddsPapi fixtures have home/away or participants.
    Returns the matching fixture dict, or None.
    """
    import re
    poly_low = (poly_title or "").lower()
    # Extract team names from "X vs. Y" pattern
    m = re.search(r"([a-zà-üA-Z][\w\-\.' &]+?)\s+vs\.?\s+([a-zà-üA-Z][\w\-\.' &]+?)(?:\s*[:?(]|\s+-\s+|$)", poly_low)
    if not m:
        return None
    poly_a, poly_b = normalize_team_name(m.group(1)), normalize_team_name(m.group(2))
    if not poly_a or not poly_b:
        return None

    best_match = None
    best_score = 0
    for fx in fixtures:
        parts = fx.get("participants", [])
        if not parts and "home" in fx:
            parts = [fx.get("home", {}), fx.get("away", {})]
        names = [normalize_team_name(p.get("name", "") if isinstance(p, dict) else str(p)) for p in parts]
        names = [n for n in names if n]
        if len(names) < 2: continue
        # Both team names must appear as substring in either direction
        a_in = any(poly_a in n or n in poly_a for n in names if n)
        b_in = any(poly_b in n or n in poly_b for n in names if n)
        if a_in and b_in:
            score = sum(len(set(poly_a.split()) & set(n.split())) +
                        len(set(poly_b.split()) & set(n.split())) for n in names)
            if score > best_score:
                best_score = score
                best_match = fx
    return best_match

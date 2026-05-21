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

BASE_URL = "https://api.oddspapi.io/v4"
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

    def get_tournaments(self, sport_id: int) -> list[dict]:
        """List tournaments for a sport. Cached 24h (refresh daily for new tournaments)."""
        cache_path = self.state_dir / f"tournaments_sport{sport_id}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                age = time.time() - cached.get("_cached_at", 0)
                if age < 86400:  # 24h disk cache
                    return cached["data"]
            except Exception:
                pass
        data = self._get("/tournaments", {"sportId": sport_id}, cache_ttl_s=86400)
        cache_path.write_text(json.dumps({"_cached_at": time.time(), "data": data}, indent=2))
        return data

    def get_participants(self, sport_id: int) -> list[dict]:
        """List participants (teams/players) for a sport. Cached to disk forever."""
        cache_path = self.state_dir / f"participants_sport{sport_id}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if cached.get("data"):
                    return cached["data"]
            except Exception:
                pass
        data = self._get("/participants", {"sportId": sport_id}, cache_ttl_s=86400 * 30)
        cache_path.write_text(json.dumps({"_cached_at": time.time(), "data": data}, indent=2))
        return data

    def get_odds_by_tournaments(self, tournament_ids: list[int],
                                 bookmaker: str = "pinnacle") -> list[dict]:
        """Main value-call: all fixtures + odds for these tournaments in 1 call.

        Response format (v4):
          [{fixtureId, participant1Id, participant2Id, sportId, tournamentId,
            startTime, hasOdds, bookmakerOdds: {pinnacle: {markets: {101: {outcomes: {...}}}}}}]
        """
        if not tournament_ids:
            return []
        ids_str = ",".join(str(i) for i in tournament_ids)
        return self._get("/odds-by-tournaments",
                         {"bookmaker": bookmaker, "tournamentIds": ids_str})

    # Aliases for backward compatibility (paper_bookarb.py imports)
    def get_fixtures_today(self, sport_id: int | None = None,
                            bookmakers: str = "pinnacle") -> list[dict]:
        """Legacy v5 endpoint — replaced by get_odds_by_tournaments in v4."""
        raise NotImplementedError("v4 has no /fixtures/today — use get_odds_by_tournaments")

    def get_fixtures_odds_main(self, sport_id: int | None = None,
                                bookmakers: str = "pinnacle") -> list[dict]:
        """Legacy v5 endpoint shim — picks top tournaments for sport + fetches odds."""
        if not sport_id:
            raise ValueError("sport_id required in v4 mode (no all-sports batch endpoint)")
        # Get tournaments with upcoming/future fixtures
        tournaments = self.get_tournaments(sport_id)
        active = [t for t in tournaments
                  if (t.get("futureFixtures", 0) or 0) > 0
                  or (t.get("upcomingFixtures", 0) or 0) > 0
                  or (t.get("liveFixtures", 0) or 0) > 0]
        # Top 5 tournaments by future+upcoming fixture count
        active.sort(key=lambda t: -((t.get("futureFixtures", 0) or 0)
                                     + (t.get("upcomingFixtures", 0) or 0)))
        tids = [t["tournamentId"] for t in active[:5]]
        if not tids:
            log.info(f"No active tournaments for sportId={sport_id}")
            return []
        return self.get_odds_by_tournaments(tids, bookmakers.split(",")[0])


# ── Helper for matching Polymarket questions ↔ OddsPapi fixtures ───────────

def normalize_team_name(s: str) -> str:
    """Normalize for fuzzy matching: lowercase + strip common suffixes."""
    if not s: return ""
    s = s.lower().strip()
    for suffix in (" fc", " sc", " cf", " club", " united", " city"):
        if s.endswith(suffix):
            s = s[:-len(suffix)].strip()
    return s


def match_polymarket_to_fixture(poly_title: str, fixtures: list[dict],
                                  participants_map: dict[int, str] | None = None) -> dict | None:
    """Find OddsPapi v4 fixture matching a Polymarket question.

    v4 fixture has `participant1Id` and `participant2Id` (numeric).
    Use `participants_map` (id -> name) to resolve names for matching.
    """
    import re
    poly_low = (poly_title or "").lower()
    m = re.search(r"([a-zà-üA-Z][\w\-\.' &]+?)\s+vs\.?\s+([a-zà-üA-Z][\w\-\.' &]+?)(?:\s*[:?(]|\s+-\s+|$)", poly_low)
    if not m:
        return None
    poly_a, poly_b = normalize_team_name(m.group(1)), normalize_team_name(m.group(2))
    if not poly_a or not poly_b or not participants_map:
        return None

    best_match = None
    best_score = 0
    for fx in fixtures:
        p1 = participants_map.get(fx.get("participant1Id"), "")
        p2 = participants_map.get(fx.get("participant2Id"), "")
        n1, n2 = normalize_team_name(p1), normalize_team_name(p2)
        if not n1 or not n2:
            continue
        a_in = poly_a in n1 or n1 in poly_a or poly_a in n2 or n2 in poly_a
        b_in = poly_b in n1 or n1 in poly_b or poly_b in n2 or n2 in poly_b
        if a_in and b_in:
            score = (len(set(poly_a.split()) & set(n1.split())) +
                     len(set(poly_a.split()) & set(n2.split())) +
                     len(set(poly_b.split()) & set(n1.split())) +
                     len(set(poly_b.split()) & set(n2.split())))
            if score > best_score:
                best_score = score
                best_match = fx
                best_match["_p1_name"] = p1
                best_match["_p2_name"] = p2
    return best_match


def sharp_implied_from_v4_fixture(fixture: dict, target_team: str,
                                    bookmaker: str = "pinnacle") -> float | None:
    """Extract Pinnacle implied probability for `target_team` from a v4 fixture.

    v4 structure :
      fixture.bookmakerOdds.{bookmaker}.markets.{marketId}.outcomes.{outcomeId}.players.0.price
      Market 101 = moneyline (3-way for football, 2-way for basketball/tennis)
      Outcome IDs : bookmakerOutcomeId field tells "home"/"draw"/"away"
    """
    target_norm = (target_team or "").lower().strip()
    p1_name = (fixture.get("_p1_name") or "").lower()
    p2_name = (fixture.get("_p2_name") or "").lower()

    odds = (fixture.get("bookmakerOdds") or {}).get(bookmaker)
    if not odds:
        return None
    moneyline = (odds.get("markets") or {}).get("101")
    if not moneyline:
        return None
    outcomes = moneyline.get("outcomes") or {}

    # Compute implied probabilities for all outcomes
    implied = {}
    for out_id, out_data in outcomes.items():
        players = out_data.get("players") or {}
        p0 = players.get("0") or {}
        outcome_label = (p0.get("bookmakerOutcomeId") or "").lower()  # home/draw/away
        try:
            price = float(p0.get("price"))
            if price <= 1.0:
                continue
        except (TypeError, ValueError):
            continue
        implied[outcome_label] = 1.0 / price

    total = sum(implied.values())
    if total <= 0:
        return None

    # Map target team -> home/draw/away label
    if target_norm in ("draw", "tie"):
        target_label = "draw"
    elif p1_name and (target_norm in p1_name or p1_name in target_norm):
        target_label = "home"
    elif p2_name and (target_norm in p2_name or p2_name in target_norm):
        target_label = "away"
    else:
        return None

    if target_label not in implied:
        return None
    # Remove vig
    return implied[target_label] / total


def build_participants_map(participants: list[dict]) -> dict[int, str]:
    """Build {participant_id: name} map from /v4/participants response."""
    out = {}
    for p in participants:
        pid = p.get("participantId") or p.get("id")
        name = p.get("participantName") or p.get("name") or ""
        if pid is not None and name:
            out[pid] = name
    return out

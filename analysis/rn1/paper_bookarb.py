"""Bot Ultime Moteur B — Bookmaker Arbitrage Directionnel.

Compare Polymarket sports market prices against Pinnacle sharp odds (via
OddsPapi free tier, 250-call budget). When Polymarket undervalues the
sharp consensus by ≥ 7%, enter a paper BUY.

Cycle 3h (= 8 calls/day on OddsPapi = 31 days of free-tier runway).

Cycle logic :
  1. Single OddsPapi call : /fixtures-odds/main?sportId=X&bookmakers=pinnacle
     → batch all today's fixtures + odds for that sport
     → cached 3h in memory
  2. Fetch Polymarket sports markets via Gamma (free, unlimited)
  3. For each Polymarket market : fuzzy match to OddsPapi fixture
  4. Compute edge = sharp_implied_prob - polymarket_best_ask
  5. If edge ≥ ENTRY_EDGE (default 0.07) AND volume_24h ≥ MIN_VOLUME
     → paper BUY at best_ask with sizing $10 / Kelly
  6. Exit (resolve_positions) : auto-unwind at NEAR_RESOLVE (0.99)
     or stop-loss -50% or 2h past event_start

Risk overlay (mirrors Bot Ultime Moteur A) :
  - MAX_POSITIONS = 10 (smaller than A since sample is rarer)
  - MAX_SAME_SPORT = 5
  - KILL_SWITCH = -15% equity

Sport rotation (priority) :
  - Tennis (Roland Garros active May-June 2026)
  - Soccer (FIFA WC June-July 2026 USA-MEX-CAN)
  - 1 sport per cycle (saves OddsPapi budget vs all-sports fetch)

Storage : data_bookarb/
"""
from __future__ import annotations
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .oddspapi import (
    OddsPapiClient, match_polymarket_to_fixture, BudgetExceeded,
)

DATA_DIR = Path(__file__).resolve().parent / "data_bookarb"
POSITIONS_PATH = DATA_DIR / "positions.json"
TRADES_LOG_PATH = DATA_DIR / "trades.jsonl"
EQUITY_PATH = DATA_DIR / "equity.jsonl"
STATE_PATH = DATA_DIR / "state.json"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"

INITIAL_CAPITAL_USD = float(os.environ.get("BOOKARB_INITIAL", "600"))
MOTEUR_B_CAPITAL = float(os.environ.get("BOOKARB_MOTEUR_B_CAP", str(INITIAL_CAPITAL_USD * 0.5)))
NORMAL_SIZE = float(os.environ.get("BOOKARB_NORMAL_SIZE", "10"))
MAX_POSITIONS = int(os.environ.get("BOOKARB_MAX_POSITIONS", "10"))
MAX_SAME_SPORT = int(os.environ.get("BOOKARB_MAX_SAME_SPORT", "5"))
KILL_SWITCH_PCT = float(os.environ.get("BOOKARB_KILL_SWITCH_PCT", "-0.15"))
ENTRY_EDGE = float(os.environ.get("BOOKARB_ENTRY_EDGE", "0.07"))
MIN_VOLUME = float(os.environ.get("BOOKARB_MIN_VOLUME", "5000"))
MIN_LIQUIDITY = float(os.environ.get("BOOKARB_MIN_LIQ", "500"))
MIN_HOURS_TO_EVENT = float(os.environ.get("BOOKARB_MIN_HOURS_TO_EVENT", "0.5"))
MAX_HOURS_TO_EVENT = float(os.environ.get("BOOKARB_MAX_HOURS_TO_EVENT", "4"))
NEAR_RESOLVE = float(os.environ.get("BOOKARB_NEAR_RESOLVE", "0.99"))
STOP_LOSS_PCT = float(os.environ.get("BOOKARB_STOP_LOSS_PCT", "-0.50"))
TAKER_FEE = 0.02
POLL_INTERVAL_S = int(os.environ.get("BOOKARB_POLL_S", "10800"))  # 3 hours

# Sport rotation : we cycle through these, 1 per OddsPapi call
SPORT_ROTATION = [
    ("Tennis", 2),       # Tennis sport_id (typical; will be reconciled at boot)
    ("Soccer", 1),       # Soccer
    ("Basketball", 11),  # Basketball (NBA fits here per docs example)
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rn1-bookarb")

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False
    log.info("SIGTERM, exit at next loop iteration")


def _load_json(path: Path, default):
    if not path.exists(): return default
    try: return json.loads(path.read_text())
    except: return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Polymarket data (free) ─────────────────────────────────────────────────

def fetch_polymarket_sports_markets(category_keywords: list[str] | None = None) -> list[dict]:
    """Fetch active Polymarket sports markets within trading window."""
    out = []
    for offset in range(0, 2000, 500):
        try:
            r = httpx.get(GAMMA_API, params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": 500,
                "offset": offset,
            }, timeout=15)
            r.raise_for_status()
            page = r.json()
        except Exception as e:
            log.warning(f"gamma fetch offset={offset}: {e}")
            break
        if not page: break
        out.extend(page)
        if len(page) < 500: break
    # Filter to sports-tagged or sports-keyword markets
    keywords = category_keywords or ["soccer", "tennis", "basketball", "baseball", "hockey", "mma", "football"]
    sports = []
    for m in out:
        tags = [(t or "").lower() for t in (m.get("tags") or []) if isinstance(t, str)]
        slug = (m.get("slug") or "").lower()
        q = (m.get("question") or "").lower()
        if any(k in tags for k in keywords) or any(k in slug for k in keywords) or any(k in q for k in keywords):
            sports.append(m)
    return sports


def fetch_market_resolution(cid: str) -> dict | None:
    try:
        r = httpx.get(f"{CLOB_API}/markets/{cid}", timeout=10)
        if r.status_code == 200: return r.json()
    except Exception as e:
        log.debug(f"clob fetch failed {cid[:14]}: {e}")
    return None


# ── Signal computation ─────────────────────────────────────────────────────

def parse_iso_ts(s: str) -> int:
    if not s: return 0
    s = s.strip().replace(" ", "T")
    if s.endswith("+00"): s = s + "00"
    try: return int(datetime.fromisoformat(s).timestamp())
    except: return 0


def sharp_implied_from_fixture(fixture: dict, target_team: str) -> float | None:
    """Extract Pinnacle implied probability for `target_team` from an OddsPapi
    fixture-odds-main response. Returns None if structure not as expected.

    OddsPapi structure is typically :
      fixture.odds : [
        {market: "h2h" or "moneyline", bookmaker: "pinnacle",
         outcomes: [{name: "Home Team", price: 1.95}, {name: "Away Team", price: 2.05}]}
      ]
    Prices are decimal odds. implied = 1/price (then adjusted for vig if needed).
    """
    if not fixture or not target_team: return None
    target_norm = target_team.lower().strip()
    odds_blocks = fixture.get("odds") or []
    for block in odds_blocks:
        if (block.get("bookmaker", "") or "").lower() != "pinnacle":
            continue
        market = (block.get("market", "") or "").lower()
        if market not in ("h2h", "moneyline", "match_winner"):
            continue
        outcomes = block.get("outcomes") or block.get("selections") or []
        total_implied = 0.0
        target_implied = None
        for out in outcomes:
            name = (out.get("name") or out.get("participant") or "").lower()
            price = out.get("price") or out.get("odds") or out.get("decimal")
            if not price: continue
            try: imp = 1.0 / float(price)
            except: continue
            total_implied += imp
            if target_norm in name or name in target_norm:
                target_implied = imp
        # Remove vig
        if target_implied is not None and total_implied > 0:
            return target_implied / total_implied
    return None


# ── Paper accounting ───────────────────────────────────────────────────────

def kelly_size_usd(edge: float, price: float, capital: float) -> float:
    """Kelly for asymmetric payoff: f* = edge / (1 - price). Fractional 1/4."""
    if price <= 0 or price >= 1 or edge <= 0: return NORMAL_SIZE
    f_star = edge / (1.0 - price)
    kelly_usd = f_star * 0.25 * capital
    return max(4.0, min(28.0, kelly_usd))


def count_positions_by_sport(positions: dict, sport: str) -> int:
    return sum(1 for p in positions.values() if (p.get("sport") or "").lower() == sport.lower())


def open_position(state: dict, positions: dict, opp: dict) -> bool:
    price = opp["poly_price"]
    eff_price = price * (1 + TAKER_FEE)
    size_usd = kelly_size_usd(opp["edge"], price, MOTEUR_B_CAPITAL)
    shares = size_usd / eff_price if eff_price > 0 else 0
    cost = shares * eff_price

    if state["cash_usd"] < cost: return False
    if len(positions) >= MAX_POSITIONS: return False
    if count_positions_by_sport(positions, opp["sport"]) >= MAX_SAME_SPORT: return False

    cid = opp["condition_id"]
    state["cash_usd"] -= cost
    positions[cid] = {
        "condition_id": cid,
        "title": opp["title"],
        "outcome": opp["outcome"],
        "token_id": opp["token_id"],
        "shares": shares,
        "avg_price": eff_price,
        "cost_basis": cost,
        "sport": opp["sport"],
        "sharp_implied": opp["sharp_implied"],
        "edge_at_entry": opp["edge"],
        "event_start_ts": opp.get("event_start_ts", 0),
        "opened_ts": int(time.time()),
    }
    _append_jsonl(TRADES_LOG_PATH, {
        "ts": int(time.time()),
        "action": "buy",
        "condition_id": cid,
        "title": opp["title"],
        "outcome": opp["outcome"],
        "poly_price": price,
        "sharp_implied": opp["sharp_implied"],
        "edge": opp["edge"],
        "shares": shares,
        "cost_basis": cost,
        "sport": opp["sport"],
    })
    log.info(f"BUY [{opp['sport']:<10}] {opp['title'][:50]} edge={opp['edge']*100:.1f}pp "
             f"poly={price:.3f} sharp={opp['sharp_implied']:.3f} → ${cost:.2f}")
    return True


def resolve_positions(state: dict, positions: dict) -> None:
    """Realize PnL on closed markets, auto-unwind 99%, stop-loss, time exit."""
    now = int(time.time())
    for cid in list(positions.keys()):
        pos = positions[cid]
        market = fetch_market_resolution(cid)
        if not market: continue
        closed = market.get("closed", False)
        # Find token's current price
        cur_price = None
        win_tok = None
        for tok in market.get("tokens", []):
            if tok.get("winner"):
                win_tok = str(tok.get("token_id"))
            if str(tok.get("token_id")) == str(pos.get("token_id")):
                try: cur_price = float(tok.get("price"))
                except: pass
        # Decide exit
        exit_reason = None
        payout_per_share = None
        if closed and win_tok is not None:
            payout_per_share = 1.0 if str(pos["token_id"]) == win_tok else 0.0
            exit_reason = "closed"
        elif cur_price is not None and cur_price >= NEAR_RESOLVE:
            payout_per_share = cur_price; exit_reason = "near_resolve_99"
        elif cur_price is not None and cur_price <= (1 - NEAR_RESOLVE):
            payout_per_share = cur_price; exit_reason = "near_resolve_0"
        elif cur_price is not None:
            cur_value = pos["shares"] * cur_price
            if (cur_value - pos["cost_basis"]) / pos["cost_basis"] < STOP_LOSS_PCT:
                payout_per_share = cur_price; exit_reason = "stop_loss"
        # Time exit (2h past event start)
        if payout_per_share is None and pos.get("event_start_ts", 0) > 0:
            if now > pos["event_start_ts"] + 7200 and cur_price is not None:
                payout_per_share = cur_price; exit_reason = "time_exit_post_event"
        if payout_per_share is None:
            continue

        payout = pos["shares"] * payout_per_share
        pnl = payout - pos["cost_basis"]
        state["cash_usd"] += payout
        state["realized_pnl"] += pnl
        state["n_resolved"] += 1
        outcome = "won" if pnl > 0 else "lost"
        if outcome == "won": state["n_won"] += 1
        _append_jsonl(TRADES_LOG_PATH, {
            "ts": now,
            "action": "resolve",
            "condition_id": cid,
            "title": pos.get("title"),
            "outcome": outcome,
            "exit_reason": exit_reason,
            "shares": pos["shares"],
            "cost_basis": pos["cost_basis"],
            "payout": payout,
            "pnl": pnl,
            "sport": pos.get("sport"),
        })
        log.info(f"RESOLVE {outcome:<5} [{exit_reason}] pnl=${pnl:+.2f} "
                 f"{(pos.get('title') or '?')[:50]}")
        del positions[cid]


# ── Cycle ──────────────────────────────────────────────────────────────────

def cycle(state: dict, positions: dict, client: OddsPapiClient) -> None:
    """One cycle : resolve, scan, match, enter."""
    # 1. Resolve
    resolve_positions(state, positions)

    # 2. Kill switch
    equity = state["cash_usd"] + sum(
        p["shares"] * p.get("avg_price", 0) for p in positions.values()
    )
    dd = (equity - INITIAL_CAPITAL_USD) / INITIAL_CAPITAL_USD
    if dd < KILL_SWITCH_PCT:
        log.warning(f"KILL_SWITCH eq=${equity:.2f} dd={dd*100:.1f}% — no entries")
        return

    # 3. Pick sport for this cycle (rotation)
    state.setdefault("sport_rotation_idx", 0)
    sport_name, sport_id = SPORT_ROTATION[state["sport_rotation_idx"] % len(SPORT_ROTATION)]
    state["sport_rotation_idx"] = (state["sport_rotation_idx"] + 1) % len(SPORT_ROTATION)

    # 4. Fetch OddsPapi fixtures-odds-main for this sport (1 call, cached 3h)
    try:
        fixtures = client.get_fixtures_odds_main(sport_id=sport_id, bookmakers="pinnacle")
    except BudgetExceeded as e:
        log.error(f"OddsPapi budget exhausted: {e}")
        return
    except Exception as e:
        log.error(f"OddsPapi fetch failed for sport={sport_name}: {e}")
        return
    log.info(f"OddsPapi fixtures fetched: sport={sport_name}, n={len(fixtures)}, "
             f"budget {client.calls_made}/250 ({client.remaining} remaining)")

    # 5. Fetch Polymarket sports markets (free)
    poly_sports_keywords = [sport_name.lower()]
    poly_markets = fetch_polymarket_sports_markets(poly_sports_keywords)
    log.info(f"Polymarket markets for {sport_name}: {len(poly_markets)}")

    # 6. Match + signal
    candidates = []
    skip_counts: dict[str, int] = {}
    now = int(time.time())
    for m in poly_markets:
        title = m.get("question") or ""
        slug = m.get("slug") or ""
        if m.get("closed"): skip_counts["closed"] += 1 or 0; continue
        # Skip if already held
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid or cid in positions:
            skip_counts["dup"] = skip_counts.get("dup", 0) + 1
            continue
        # Liquidity / volume gates
        try:
            liq = float(m.get("liquidity") or 0)
            vol = float(m.get("volume") or 0)
        except: continue
        if liq < MIN_LIQUIDITY:
            skip_counts["low_liq"] = skip_counts.get("low_liq", 0) + 1; continue
        if vol < MIN_VOLUME:
            skip_counts["low_vol"] = skip_counts.get("low_vol", 0) + 1; continue
        # Time window to event
        end_ts = parse_iso_ts(m.get("endDate") or m.get("gameStartTime") or "")
        if end_ts > 0:
            hours_to_event = (end_ts - now) / 3600
            if not (MIN_HOURS_TO_EVENT <= hours_to_event <= MAX_HOURS_TO_EVENT):
                skip_counts["bad_time"] = skip_counts.get("bad_time", 0) + 1; continue
        # Fuzzy-match to OddsPapi fixture
        fx = match_polymarket_to_fixture(title, fixtures)
        if not fx:
            skip_counts["no_match"] = skip_counts.get("no_match", 0) + 1; continue
        # Parse outcomes + prices
        try:
            outcomes = m.get("outcomes")
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            prices_raw = m.get("outcomePrices")
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            ctids_raw = m.get("clobTokenIds")
            ctids = json.loads(ctids_raw) if isinstance(ctids_raw, str) else ctids_raw
        except: continue
        if not outcomes or not prices or len(outcomes) != 2: continue
        # For each outcome, compute edge
        for idx, (out_name, p) in enumerate(zip(outcomes, prices)):
            try: poly_price = float(p)
            except: continue
            sharp = sharp_implied_from_fixture(fx, out_name)
            if sharp is None: continue
            edge = sharp - poly_price
            if edge < ENTRY_EDGE: continue
            candidates.append({
                "condition_id": cid,
                "title": title,
                "outcome": out_name,
                "token_id": str(ctids[idx]) if ctids and len(ctids) > idx else None,
                "poly_price": poly_price,
                "sharp_implied": sharp,
                "edge": edge,
                "sport": sport_name,
                "event_start_ts": end_ts,
            })

    # 7. Rank by edge desc + open
    candidates.sort(key=lambda c: -c["edge"])
    n_opened = 0
    for opp in candidates:
        if len(positions) >= MAX_POSITIONS: break
        if open_position(state, positions, opp):
            n_opened += 1

    log.info(f"cycle: sport={sport_name} sharp_n={len(fixtures)} poly_n={len(poly_markets)} "
             f"matched={len(candidates)} opened={n_opened} held={len(positions)} "
             f"cash=${state['cash_usd']:.2f} eq=${equity:.2f} dd={dd*100:+.1f}% "
             f"skips={dict(skip_counts)}")


def equity_snapshot(state: dict, positions: dict) -> dict:
    mtm = 0.0
    for cid, pos in positions.items():
        # Use avg_price as fallback (no real-time CLOB price needed each snapshot)
        mtm += pos["shares"] * pos.get("avg_price", 0)
    equity = state["cash_usd"] + mtm
    return {
        "ts": int(time.time()),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "cash_usd": round(state["cash_usd"], 2),
        "open_positions_mtm": round(mtm, 2),
        "n_open": len(positions),
        "equity_usd": round(equity, 2),
        "realized_pnl": round(state["realized_pnl"], 2),
        "perf_pct": round((equity - INITIAL_CAPITAL_USD) / INITIAL_CAPITAL_USD * 100, 3),
        "n_resolved": state["n_resolved"],
        "n_won": state["n_won"],
        "win_rate": round(state["n_won"] / state["n_resolved"], 4) if state["n_resolved"] else 0,
    }


def maybe_snapshot(state: dict, positions: dict) -> None:
    snap = equity_snapshot(state, positions)
    today = snap["date"]
    last_date = None
    if EQUITY_PATH.exists():
        try:
            lines = EQUITY_PATH.read_text().strip().splitlines()
            if lines: last_date = json.loads(lines[-1]).get("date")
        except: pass
    if last_date == today:
        lines = EQUITY_PATH.read_text().splitlines()
        if lines: lines[-1] = json.dumps(snap)
        EQUITY_PATH.write_text("\n".join(lines) + "\n")
    else:
        _append_jsonl(EQUITY_PATH, snap)


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    state = _load_json(STATE_PATH, {
        "cash_usd": INITIAL_CAPITAL_USD,
        "realized_pnl": 0.0,
        "n_resolved": 0,
        "n_won": 0,
        "sport_rotation_idx": 0,
        "boot_ts": int(time.time()),
    })
    positions = _load_json(POSITIONS_PATH, {})
    client = OddsPapiClient(state_dir=DATA_DIR)

    log.info(f"Boot — Bot Ultime Moteur B (Bookmaker arb), capital=${INITIAL_CAPITAL_USD} "
             f"(MoteurB_cap=${MOTEUR_B_CAPITAL}), OddsPapi budget {client.calls_made}/250")
    log.info(f"Entry edge >= {ENTRY_EDGE*100:.0f}pp, normal_size=${NORMAL_SIZE}, "
             f"max_pos={MAX_POSITIONS}, max_same_sport={MAX_SAME_SPORT}, "
             f"kill_switch={KILL_SWITCH_PCT*100:.0f}%")
    log.info(f"Cycle = {POLL_INTERVAL_S}s ({POLL_INTERVAL_S/3600:.1f}h) → "
             f"{86400/POLL_INTERVAL_S:.0f} calls/day → {250 // max(1, 86400//POLL_INTERVAL_S)} days runway")

    while _running:
        try:
            cycle(state, positions, client)
            _save_json(STATE_PATH, state)
            _save_json(POSITIONS_PATH, positions)
            maybe_snapshot(state, positions)
        except Exception as e:
            log.error(f"cycle exception {type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL_S)

    log.info("Stopped")


if __name__ == "__main__":
    main()

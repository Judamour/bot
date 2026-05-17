"""RN1-autobot — autonomous Polymarket scanner mimicking RN1's STYLE.

Different from rn1-live-paper.service :
  - rn1-live-paper : COPIES RN1's specific picks (filtered)
  - rn1-autobot    : PICKS INDEPENDENTLY using RN1-style criteria

Strategy :
  1. Every 60s, fetch all active Polymarket markets via Gamma API
  2. Filter to RN1-like style :
     - Sport tag (Soccer / Tennis / Hockey / MLB / NBA / etc.)
     - end_date_iso in next 0-48h (event upcoming, not too far)
     - One outcome in favorite bucket (0.85-0.95)
     - Liquidity + volume gates
     - Skip markets already in our positions (anti-DCA)
  3. Paper BUY the favorite outcome at $10 fixed size + 2% taker fee
  4. On market resolution, realize PnL
  5. Snapshot equity, repeat

If our autobot ~= RN1's paper bot stats → his edge is in the criteria
(replicable from style alone).
If our autobot << RN1's paper bot stats → he has additional filter we don't
have access to (sharp odds, volume signals, news, etc.) → next phase needed.

Storage isolated from other paper bots :
  data/autobot_positions.json
  data/autobot_trades.jsonl
  data/autobot_equity.jsonl
  data/autobot_state.json
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

DATA_DIR = Path(__file__).resolve().parent / "data"
POSITIONS_PATH = DATA_DIR / "autobot_positions.json"
TRADES_LOG_PATH = DATA_DIR / "autobot_trades.jsonl"
EQUITY_PATH = DATA_DIR / "autobot_equity.jsonl"
STATE_PATH = DATA_DIR / "autobot_state.json"

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"

# ── Config ──────────────────────────────────────────────────────────────────
# $100K paper to match RN1's AUM ~$100-500K
# $500/trade = 0.5% AUM (his observed average 0.3-0.8%)
# Max 200 positions = $100K full deployment capacity
INITIAL_CAPITAL_USD = float(os.environ.get("AUTOBOT_INITIAL", "100000"))
FIXED_SIZE_USD = float(os.environ.get("AUTOBOT_SIZE", "500"))
TAKER_FEE = 0.02
# Gamma full scan ~100 calls (10K markets pagination). At 60s cycle = 6000 calls/hour.
# At 300s (5min) cycle = 1200 calls/hour, much politer. Markets relevant for us
# (sport ending in 0-48h) don't change fast enough to warrant sub-5min scan.
POLL_INTERVAL_S = int(os.environ.get("AUTOBOT_POLL_S", "300"))

# RN1-style criteria
SPORT_TAGS = {"Soccer", "Basketball", "Baseball", "Tennis", "Hockey", "MMA", "Football",
              "Sports"}
# Polymarket sport markets often have end_date = day of game @ 00:00 UTC.
# So a market for tonight's game shows end_date as already past by 10-20h.
# Window [-24h, +72h] catches today's games + tomorrow + day after.
END_DATE_MIN_HOURS = -24
END_DATE_MAX_HOURS = 72
WIN_PRICE_MIN = 0.85
WIN_PRICE_MAX = 0.95
MIN_LIQUIDITY = 100
MIN_VOLUME = 500
MAX_POSITIONS = int(os.environ.get("AUTOBOT_MAX_POSITIONS", "200"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rn1-autobot")

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False
    log.info("SIGTERM, exit at next loop iteration")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _parse_list(val) -> list | None:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return None
    return val if isinstance(val, list) else None


# ── Polymarket Gamma scanner ────────────────────────────────────────────────

def fetch_active_markets() -> list[dict]:
    """Paginate Gamma API for all active markets. Cap at ~12K for safety."""
    out: list[dict] = []
    offset = 0
    page_size = 100  # Gamma API hard cap
    while offset < 15000:
        try:
            r = httpx.get(
                f"{GAMMA_API}?active=true&closed=false&limit={page_size}&offset={offset}",
                timeout=30,
            )
            r.raise_for_status()
            page = r.json()
        except Exception as e:
            log.warning(f"gamma offset={offset} failed: {e}")
            break
        if not isinstance(page, list) or not page:
            break
        out.extend(m for m in page if isinstance(m, dict))
        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(0.05)
    return out


def fetch_market_resolution(condition_id: str) -> dict | None:
    """Get CLOB market details (resolved status + winner)."""
    try:
        r = httpx.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def is_sport_market(market: dict) -> bool:
    """Reliable detection via Polymarket's feeType field (sports_fees_v2 etc.).

    Polymarket categorizes markets via feeType :
    - sports_fees_v2 / sports_fees → sport (what we want)
    - politics_fees → politics
    - crypto_fees → crypto
    - etc.
    Returns True if feeType starts with 'sports'.
    """
    fee_type = (market.get("feeType") or "").lower()
    return fee_type.startswith("sport")


def is_candidate(market: dict, now: datetime) -> tuple[bool, str, dict | None]:
    """Return (accept, reason_if_reject, opportunity_dict_if_accept)."""
    if not is_sport_market(market):
        return False, "not_sport", None

    end_iso = market.get("endDate") or market.get("endDateIso")
    if not end_iso:
        return False, "no_end_date", None
    try:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except Exception:
        return False, "bad_end_date", None
    hours_to_end = (end_dt - now).total_seconds() / 3600
    if not (END_DATE_MIN_HOURS <= hours_to_end <= END_DATE_MAX_HOURS):
        return False, "end_date_out_of_window", None

    prices = _parse_list(market.get("outcomePrices"))
    outcomes = _parse_list(market.get("outcomes"))
    token_ids = _parse_list(market.get("clobTokenIds"))
    if not prices or not outcomes or len(prices) != 2 or len(outcomes) != 2:
        return False, "non_binary", None
    try:
        prices = [float(p) for p in prices]
    except Exception:
        return False, "bad_prices", None

    # Identify favorite outcome
    if WIN_PRICE_MIN <= prices[0] <= WIN_PRICE_MAX:
        winner_idx = 0
    elif WIN_PRICE_MIN <= prices[1] <= WIN_PRICE_MAX:
        winner_idx = 1
    else:
        return False, "no_favorite_in_range", None

    try:
        liq = float(market.get("liquidity") or 0)
        vol = float(market.get("volume") or 0)
    except Exception:
        liq = vol = 0
    if liq < MIN_LIQUIDITY:
        return False, "low_liquidity", None
    if vol < MIN_VOLUME:
        return False, "low_volume", None

    cid = market.get("conditionId") or market.get("condition_id")
    if not cid:
        return False, "no_cid", None

    return True, "ok", {
        "condition_id": cid,
        "title": market.get("question") or market.get("title") or "",
        "slug": market.get("slug"),
        "winner_outcome": outcomes[winner_idx],
        "winner_price": prices[winner_idx],
        "winner_token_id": token_ids[winner_idx] if token_ids else None,
        "hours_to_end": round(hours_to_end, 2),
        "liquidity": round(liq, 2),
        "volume": round(vol, 2),
        "tags": [t for t in market.get("tags", []) if isinstance(t, str)],
    }


# ── Paper accounting ────────────────────────────────────────────────────────

def open_position(state: dict, positions: dict, opp: dict) -> bool:
    price = opp["winner_price"]
    effective_price = price * (1 + TAKER_FEE)
    shares = FIXED_SIZE_USD / effective_price if effective_price > 0 else 0
    cost = shares * effective_price

    if state["cash_usd"] < cost:
        return False
    if len(positions) >= MAX_POSITIONS:
        return False

    cid = opp["condition_id"]
    state["cash_usd"] -= cost
    positions[cid] = {
        "condition_id": cid,
        "title": opp["title"],
        "outcome": opp["winner_outcome"],
        "token_id": opp["winner_token_id"],
        "shares": shares,
        "avg_price": effective_price,
        "cost_basis": cost,
        "opened_ts": int(time.time()),
    }
    _append_jsonl(TRADES_LOG_PATH, {
        "ts": int(time.time()),
        "action": "buy",
        "condition_id": cid,
        "title": opp["title"],
        "outcome": opp["winner_outcome"],
        "winner_price": price,
        "effective_price": effective_price,
        "shares": shares,
        "cost_basis": cost,
        "tags": opp.get("tags", []),
        "hours_to_end_at_buy": opp.get("hours_to_end"),
    })
    log.info(f"BUY {opp['title'][:55]} / {opp['winner_outcome']:<12} "
             f"@ {effective_price:.3f} → {shares:.2f} shares (${cost:.2f}) "
             f"cash=${state['cash_usd']:.2f}")
    return True


def resolve_positions(state: dict, positions: dict) -> list[dict]:
    """For each position, check if its market has resolved. Realize PnL if yes."""
    resolved = []
    for cid in list(positions.keys()):
        pos = positions[cid]
        market = fetch_market_resolution(cid)
        if not market or not market.get("closed"):
            continue
        # Find winner token
        win_tok = None
        for tok in market.get("tokens", []):
            if tok.get("winner"):
                win_tok = str(tok.get("token_id"))
                break
        if not win_tok:
            continue
        if str(pos.get("token_id")) == win_tok:
            payout = pos["shares"] * 1.0
            outcome = "won"
        else:
            payout = 0.0
            outcome = "lost"
        pnl = payout - pos["cost_basis"]
        state["cash_usd"] += payout
        state["realized_pnl"] += pnl
        state["n_resolved"] += 1
        if outcome == "won":
            state["n_won"] += 1
        resolved.append({
            "ts": int(time.time()),
            "action": "resolve",
            "condition_id": cid,
            "title": pos.get("title"),
            "outcome": outcome,
            "shares": pos["shares"],
            "cost_basis": pos["cost_basis"],
            "payout": payout,
            "pnl": pnl,
        })
        _append_jsonl(TRADES_LOG_PATH, resolved[-1])
        del positions[cid]
        log.info(f"RESOLVE {outcome:<5} {pos.get('title', '?')[:50]} pnl=${pnl:+.2f}")
    return resolved


def equity_snapshot(state: dict, positions: dict) -> dict:
    mtm = sum(p["shares"] * p["avg_price"] for p in positions.values())
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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = equity_snapshot(state, positions)
    last_date = None
    if EQUITY_PATH.exists():
        with open(EQUITY_PATH) as f:
            lines = f.readlines()
        if lines:
            try:
                last_date = json.loads(lines[-1]).get("date")
            except Exception:
                pass
    if last_date == today:
        with open(EQUITY_PATH) as f:
            lines = f.readlines()
        if lines:
            lines[-1] = json.dumps(snap) + "\n"
        with open(EQUITY_PATH, "w") as f:
            f.writelines(lines)
    else:
        _append_jsonl(EQUITY_PATH, snap)


# ── Main loop ───────────────────────────────────────────────────────────────

def cycle(state: dict, positions: dict) -> None:
    # 1. Resolve open positions whose markets closed
    resolve_positions(state, positions)

    # 2. Scan for new opportunities
    now = datetime.now(timezone.utc)
    log.debug("fetching active markets...")
    markets = fetch_active_markets()

    candidates = []
    skip_counts: dict[str, int] = {}
    for m in markets:
        accept, reason, opp = is_candidate(m, now)
        if accept and opp:
            candidates.append(opp)
        else:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1

    # 3. Filter out markets we already hold
    already_held = set(positions.keys())
    new_candidates = [c for c in candidates if c["condition_id"] not in already_held]

    # 4. Sort by hours_to_end ASC (prefer markets closest to resolution)
    new_candidates.sort(key=lambda c: c["hours_to_end"])

    # 5. Open positions until budget cap
    n_opened = 0
    for opp in new_candidates:
        if not open_position(state, positions, opp):
            break
        n_opened += 1

    log.info(f"cycle: scanned={len(markets)} candidates={len(candidates)} "
             f"new={len(new_candidates)} opened={n_opened} "
             f"held={len(positions)} cash=${state['cash_usd']:.2f}")


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    state = _load_json(STATE_PATH, {
        "cash_usd": INITIAL_CAPITAL_USD,
        "realized_pnl": 0.0,
        "n_resolved": 0,
        "n_won": 0,
        "boot_ts": int(time.time()),
    })
    positions = _load_json(POSITIONS_PATH, {})

    log.info(f"Boot — capital=${state['cash_usd']:.2f}, {len(positions)} open, "
             f"{state['n_resolved']} resolved (won={state['n_won']})")
    log.info(f"Filters: sport tags={sorted(SPORT_TAGS)}, "
             f"end_date∈[{END_DATE_MIN_HOURS}h, {END_DATE_MAX_HOURS}h], "
             f"price∈[{WIN_PRICE_MIN}, {WIN_PRICE_MAX}], "
             f"liq≥${MIN_LIQUIDITY}, vol≥${MIN_VOLUME}, max_pos={MAX_POSITIONS}")
    log.info(f"Poll interval: {POLL_INTERVAL_S}s")

    while _running:
        cycle_start = time.time()
        try:
            cycle(state, positions)
            _save_json(STATE_PATH, state)
            _save_json(POSITIONS_PATH, positions)
            maybe_snapshot(state, positions)
        except Exception as e:
            log.exception(f"cycle error (non-fatal): {e}")
        elapsed = time.time() - cycle_start
        remaining = max(0.0, POLL_INTERVAL_S - elapsed)
        slept = 0.0
        while slept < remaining and _running:
            time.sleep(1.0)
            slept += 1.0

    log.info("Stopped cleanly")


if __name__ == "__main__":
    main()

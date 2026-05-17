"""Tennis Elo-based paper trader for Polymarket WTA markets.

Long-running service that :
  1. Every 60s, fetch active Polymarket tennis markets via Gamma API
  2. For each market with parseable "X vs Y" title :
     - Lookup pid_a, pid_b in our Elo DB
     - Compute predicted prob from blended (surface + overall) Elo
     - Compare to current market price for each outcome
     - If our_prob - market_price >= EDGE_THRESHOLD → paper BUY
  3. Check resolution on open positions
  4. Snapshot equity, repeat

Output :
    data/tennis_bot_state.json
    data/tennis_bot_positions.json
    data/tennis_bot_trades.jsonl
    data/tennis_bot_equity.jsonl
"""
from __future__ import annotations
import json
import logging
import os
import re
import signal
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR / "data"
RATINGS_PATH = DATA_DIR / "elo_ratings.json"
POSITIONS_PATH = DATA_DIR / "tennis_bot_positions.json"
TRADES_LOG_PATH = DATA_DIR / "tennis_bot_trades.jsonl"
EQUITY_PATH = DATA_DIR / "tennis_bot_equity.jsonl"
STATE_PATH = DATA_DIR / "tennis_bot_state.json"

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"

# ── Config ──────────────────────────────────────────────────────────────────
INITIAL_CAPITAL_USD = float(os.environ.get("TENNIS_BOT_INITIAL", "1000"))
FIXED_SIZE_USD = float(os.environ.get("TENNIS_BOT_SIZE", "10"))
TAKER_FEE = 0.02
POLL_INTERVAL_S = int(os.environ.get("TENNIS_BOT_POLL_S", "60"))
EDGE_THRESHOLD = float(os.environ.get("TENNIS_BOT_EDGE", "0.05"))  # 5% min edge net of fee
MIN_PRICE = 0.05   # skip extreme penny shares
MAX_PRICE = 0.95   # skip lock bets (limited upside)
MAX_POSITIONS = int(os.environ.get("TENNIS_BOT_MAX_POSITIONS", "50"))
MIN_LIQUIDITY = 50
MIN_VOLUME = 200

# Tennis tournament keywords for slug/event matching
TENNIS_KEYWORDS = [
    "internazionali", "wta", "atp",
    "ostrava", "monterrey", "miami", "indian-wells", "stuttgart", "madrid",
    "rome", "roland-garros", "wimbledon", "us-open", "australian-open",
    "tournament", "doha", "dubai", "abu-dhabi", "berlin", "guangzhou",
    "tokyo", "moscow", "ningbo", "tashkent", "luxembourg", "tampico",
    "rabat", "strasbourg", "nottingham", "birmingham", "eastbourne",
    "bad-homburg", "wta-finals", "linz", "vienna", "merida",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tennis-bot")

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False
    log.info("SIGTERM, exit at next cycle")


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


def normalize_name(name: str) -> str:
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    no_acc = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-zA-Z\s]", "", no_acc)
    return re.sub(r"\s+", " ", cleaned.lower()).strip()


def build_name_index(player_names: dict) -> dict[str, str]:
    idx = {}
    for pid, name in player_names.items():
        norm = normalize_name(name)
        if norm:
            idx[norm] = pid
            parts = norm.split()
            if len(parts) >= 2:
                last = parts[-1]
                if last not in idx:
                    idx[last] = pid
    return idx


def find_player_id(name: str, name_idx: dict) -> str | None:
    if not name:
        return None
    norm = normalize_name(name)
    if norm in name_idx:
        return name_idx[norm]
    parts = norm.split()
    if parts:
        last = parts[-1]
        if last in name_idx:
            return name_idx[last]
    return None


def parse_match_title(title: str) -> tuple[str, str] | None:
    if not title:
        return None
    t = title.split(":", 1)[-1].strip() if ":" in title else title
    m = re.search(r"^(.+?)\s+vs\.?\s+(.+)$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _parse_list(val):
    if val is None:
        return None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return None
    return val if isinstance(val, list) else None


# ── Polymarket fetching ─────────────────────────────────────────────────────

def fetch_tennis_markets() -> list[dict]:
    """Fetch all active sport markets via Gamma, then filter for tennis."""
    out = []
    offset = 0
    page_size = 100
    while offset < 15000:
        try:
            r = httpx.get(
                f"{GAMMA_API}?active=true&closed=false&limit={page_size}&offset={offset}",
                timeout=20,
            )
            r.raise_for_status()
            page = r.json()
        except Exception as e:
            log.warning(f"gamma offset={offset} failed: {e}")
            break
        if not isinstance(page, list) or not page:
            break
        for m in page:
            if not isinstance(m, dict):
                continue
            fee_type = (m.get("feeType") or "").lower()
            if not fee_type.startswith("sport"):
                continue
            slug = (m.get("slug") or "").lower()
            title = (m.get("question") or "").lower()
            if not any(kw in slug or kw in title for kw in TENNIS_KEYWORDS):
                continue
            if " vs" not in title:
                continue
            out.append(m)
        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(0.05)
    return out


def fetch_market_resolution(condition_id: str) -> dict | None:
    try:
        r = httpx.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ── Trading logic ───────────────────────────────────────────────────────────

def find_opportunities(market: dict, ratings: dict, name_idx: dict) -> list[dict]:
    """Return list of {token_id, side_name, market_price, our_prob, edge}
    for each outcome where our_prob - price >= EDGE_THRESHOLD.
    """
    title = market.get("question") or ""
    parsed = parse_match_title(title)
    if not parsed:
        return []
    name_a, name_b = parsed
    pid_a = find_player_id(name_a, name_idx)
    pid_b = find_player_id(name_b, name_idx)
    if not pid_a or not pid_b:
        return []

    r_a = ratings.get(pid_a, 1500)
    r_b = ratings.get(pid_b, 1500)
    prob_a = expected_score(r_a, r_b)
    prob_b = 1.0 - prob_a

    prices = _parse_list(market.get("outcomePrices"))
    outcomes = _parse_list(market.get("outcomes"))
    token_ids = _parse_list(market.get("clobTokenIds"))
    if not prices or not outcomes or len(prices) != 2 or len(outcomes) != 2:
        return []
    try:
        prices = [float(p) for p in prices]
    except Exception:
        return []

    # Match outcome name → player A or B
    norm_a = normalize_name(name_a)
    norm_b = normalize_name(name_b)
    opps = []
    for i, outcome in enumerate(outcomes):
        n_out = normalize_name(outcome)
        if n_out in norm_a or norm_a in n_out:
            our_prob = prob_a
            side_pid = pid_a
        elif n_out in norm_b or norm_b in n_out:
            our_prob = prob_b
            side_pid = pid_b
        else:
            continue
        market_price = prices[i]
        if market_price < MIN_PRICE or market_price > MAX_PRICE:
            continue
        # Edge net of taker fee
        effective_price = market_price * (1 + TAKER_FEE)
        edge = our_prob - effective_price
        if edge >= EDGE_THRESHOLD:
            opps.append({
                "condition_id": market.get("conditionId") or market.get("condition_id"),
                "title": title,
                "outcome": outcome,
                "side_pid": side_pid,
                "token_id": token_ids[i] if token_ids else None,
                "market_price": market_price,
                "effective_price": effective_price,
                "our_prob": our_prob,
                "edge": edge,
                "elo_a": r_a, "elo_b": r_b,
                "name_a": name_a, "name_b": name_b,
                "liquidity": float(market.get("liquidity") or 0),
                "volume": float(market.get("volume") or 0),
            })
    return opps


def open_paper_position(state: dict, positions: dict, opp: dict) -> bool:
    cid = opp["condition_id"]
    token_id = opp["token_id"]
    if not cid or not token_id:
        return False
    pos_key = f"{cid}::{token_id}"
    if pos_key in positions:
        return False  # already held
    if len(positions) >= MAX_POSITIONS:
        return False
    cost = FIXED_SIZE_USD
    if state["cash_usd"] < cost:
        return False
    shares = cost / opp["effective_price"]
    state["cash_usd"] -= cost
    positions[pos_key] = {
        "condition_id": cid,
        "token_id": token_id,
        "title": opp["title"][:80],
        "outcome": opp["outcome"],
        "shares": shares,
        "avg_price": opp["effective_price"],
        "cost_basis": cost,
        "our_prob_at_buy": opp["our_prob"],
        "market_price_at_buy": opp["market_price"],
        "edge_at_buy": opp["edge"],
        "opened_ts": int(time.time()),
    }
    _append_jsonl(TRADES_LOG_PATH, {
        "ts": int(time.time()),
        "action": "buy",
        **{k: opp[k] for k in
           ["condition_id", "title", "outcome", "market_price", "our_prob",
            "edge", "name_a", "name_b", "elo_a", "elo_b"]},
        "shares": shares,
        "cost_basis": cost,
    })
    log.info(f"BUY {opp['title'][:55]} / {opp['outcome']:<15} "
             f"@ {opp['effective_price']:.3f} (our={opp['our_prob']:.3f} "
             f"edge=+{opp['edge']:.3f}) cash=${state['cash_usd']:.2f}")
    return True


def resolve_positions(state: dict, positions: dict) -> int:
    resolved_count = 0
    for pkey in list(positions.keys()):
        pos = positions[pkey]
        cid = pos["condition_id"]
        market = fetch_market_resolution(cid)
        if not market or not market.get("closed"):
            continue
        win_tok = None
        for tok in market.get("tokens", []):
            if tok.get("winner"):
                win_tok = str(tok.get("token_id"))
                break
        if not win_tok:
            continue
        if str(pos["token_id"]) == win_tok:
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
        _append_jsonl(TRADES_LOG_PATH, {
            "ts": int(time.time()), "action": "resolve",
            "condition_id": cid, "title": pos["title"],
            "outcome": outcome, "shares": pos["shares"],
            "cost_basis": pos["cost_basis"], "payout": payout, "pnl": pnl,
            "our_prob_at_buy": pos.get("our_prob_at_buy"),
            "market_price_at_buy": pos.get("market_price_at_buy"),
        })
        del positions[pkey]
        resolved_count += 1
        log.info(f"RESOLVE {outcome:<5} {pos['title'][:50]} pnl=${pnl:+.2f}")
    return resolved_count


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

def cycle(state: dict, positions: dict, ratings: dict, name_idx: dict) -> None:
    resolved = resolve_positions(state, positions)

    markets = fetch_tennis_markets()
    all_opps = []
    for m in markets:
        opps = find_opportunities(m, ratings, name_idx)
        all_opps.extend(opps)
    all_opps.sort(key=lambda o: -o["edge"])

    n_opened = 0
    for opp in all_opps:
        if open_paper_position(state, positions, opp):
            n_opened += 1

    log.info(f"cycle: tennis_markets={len(markets)} opps={len(all_opps)} "
             f"opened={n_opened} resolved={resolved} "
             f"held={len(positions)} cash=${state['cash_usd']:.2f}")


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not RATINGS_PATH.exists():
        sys.exit(f"[error] {RATINGS_PATH} missing. Run elo.py first.")

    ratings_data = _load_json(RATINGS_PATH, {})
    overall_ratings = ratings_data.get("overall_ratings", {})
    player_names = ratings_data.get("player_names", {})
    name_idx = build_name_index(player_names)

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
    log.info(f"Elo DB: {len(overall_ratings)} players, {len(name_idx)} lookup keys")
    log.info(f"Filters: edge≥{EDGE_THRESHOLD}, price∈[{MIN_PRICE}, {MAX_PRICE}], "
             f"size=${FIXED_SIZE_USD}, max_pos={MAX_POSITIONS}")
    log.info(f"Poll interval: {POLL_INTERVAL_S}s")

    while _running:
        cycle_start = time.time()
        try:
            cycle(state, positions, overall_ratings, name_idx)
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

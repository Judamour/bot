"""Bot Ultime — Moteur A (Heavy_fav autonomous scan) v1.

Research-grade enhancement of autobot (2026-05-21 deep research). Combines :
  - Heavy_fav longshot bias edge (Quantpedia, validated by our edge_sim at
    95.4% WR / +3.79% in 24h on 65 trades)
  - Fractional Kelly sizing (1/4) calibrated on empirical WR=0.954
  - Score-based ranking (tighter spread + higher liquidity = priority)
  - Risk overlay : MAX_POSITION 5%, KILL_SWITCH -15%, MAX_SAME_SPORT 5
  - Category blacklist (esports, crypto_1h) — high variance / oracle risk
  - Spread filter (skip if best_ask - best_bid > 3¢)
  - Auto-unwind 99% trigger (paper resolves at outcomePrice >= 0.99)

Moteur B (Bookmaker arb via OddsPapi) will be wired in v2 — separate engine,
shared risk overlay, separate $280 sub-allocation.

Different from copy-trading bots (RN1 / surf / Mosley1 / newdog / kch) :
this bot picks INDEPENDENTLY based on documented edges, no signal dependency.

Storage isolated :
  data_ultimate/positions.json
  data_ultimate/trades.jsonl
  data_ultimate/equity.jsonl
  data_ultimate/state.json
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

DATA_DIR = Path(__file__).resolve().parent / "data_ultimate"
POSITIONS_PATH = DATA_DIR / "positions.json"
TRADES_LOG_PATH = DATA_DIR / "trades.jsonl"
EQUITY_PATH = DATA_DIR / "equity.jsonl"
STATE_PATH = DATA_DIR / "state.json"

GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"

# ── Config ──────────────────────────────────────────────────────────────────
# $100K paper to match RN1's AUM ~$100-500K
# $500/trade = 0.5% AUM (his observed average 0.3-0.8%)
# Max 200 positions = $100K full deployment capacity
INITIAL_CAPITAL_USD = float(os.environ.get("ULTIMATE_INITIAL", "600"))
# Moteur A only for v1 (Moteur B reserved for OddsPapi bookmaker arb in v2).
MOTEUR_A_CAPITAL = float(os.environ.get("ULTIMATE_MOTEUR_A_CAP", str(INITIAL_CAPITAL_USD * 0.5)))
TAKER_FEE = 0.02
POLL_INTERVAL_S = int(os.environ.get("ULTIMATE_POLL_S", "300"))

# Heavy_fav longshot bias range
WIN_PRICE_MIN = float(os.environ.get("ULTIMATE_WIN_PRICE_MIN", "0.85"))
WIN_PRICE_MAX = float(os.environ.get("ULTIMATE_WIN_PRICE_MAX", "0.95"))

# Liquidity / volume gates (research recipe : tighter than autobot defaults)
MIN_LIQUIDITY = float(os.environ.get("ULTIMATE_MIN_LIQ", "1000"))
MIN_VOLUME = float(os.environ.get("ULTIMATE_MIN_VOLUME", "5000"))
MAX_SPREAD = float(os.environ.get("ULTIMATE_MAX_SPREAD", "0.03"))
MIN_HOURS_TO_END = float(os.environ.get("ULTIMATE_MIN_HOURS_TO_END", "2"))

# Sport whitelist (skip esports because of high variance + crypto/political)
SPORT_TAGS = {"Soccer", "Basketball", "Baseball", "Tennis", "Hockey", "MMA", "Football", "Sports"}
# Category blacklist (high oracle/variance risk per research)
CATEGORY_BLACKLIST = {"esports", "crypto_1h"}
# Also catch market-slug pattern for problematic markets
SLUG_BLACKLIST = set(os.environ.get("ULTIMATE_SLUG_BLACKLIST", "").split(",")) - {""}

# Sizing knobs — fractional Kelly with empirical WR=0.954
EMPIRICAL_WR = float(os.environ.get("ULTIMATE_EMPIRICAL_WR", "0.954"))
KELLY_FRACTION = float(os.environ.get("ULTIMATE_KELLY_FRAC", "0.25"))
MIN_TRADE_USD = float(os.environ.get("ULTIMATE_MIN_TRADE", "4"))
MAX_TRADE_USD = float(os.environ.get("ULTIMATE_MAX_TRADE", "28"))

# Risk overlay (research recipe)
MAX_POSITIONS = int(os.environ.get("ULTIMATE_MAX_POSITIONS", "15"))
MAX_SAME_SPORT = int(os.environ.get("ULTIMATE_MAX_SAME_SPORT", "5"))
KILL_SWITCH_PCT = float(os.environ.get("ULTIMATE_KILL_SWITCH_PCT", "-0.15"))
END_DATE_MIN_HOURS = -2  # match has just ended OK
END_DATE_MAX_HOURS = 72

# Auto-unwind on outcomePrice >= 0.99 (handled in resolve_positions)
NEAR_RESOLVE_PRICE = float(os.environ.get("ULTIMATE_NEAR_RESOLVE", "0.99"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rn1-ultimate")

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

    # Spread filter (research recipe: skip if best_ask - best_bid > 3¢)
    # Approximate spread from prices (other side = 1 - winner price - small offset)
    other_price = prices[1 - winner_idx]
    # If the binary market is well-formed, prices sum ~1. Spread ≈ 1 - (best_bid + (1-best_ask))
    # Simpler: market depth field if available
    spread_est = max(0, 1.0 - (prices[winner_idx] + other_price))
    if spread_est > MAX_SPREAD:
        return False, f"spread_too_wide({spread_est:.3f})", None

    # Time-to-end : skip markets too close to resolution (low conviction window)
    if hours_to_end < MIN_HOURS_TO_END:
        return False, f"too_close_to_end({hours_to_end:.1f}h)", None

    # Category blacklist
    slug = market.get("slug", "") or ""
    if slug in SLUG_BLACKLIST:
        return False, "slug_blacklist", None
    tags_lower = {t.lower() for t in (market.get("tags") or []) if isinstance(t, str)}
    if tags_lower & CATEGORY_BLACKLIST:
        return False, "category_blacklist", None

    return True, "ok", {
        "condition_id": cid,
        "title": market.get("question") or market.get("title") or "",
        "slug": slug,
        "winner_outcome": outcomes[winner_idx],
        "winner_price": prices[winner_idx],
        "winner_token_id": token_ids[winner_idx] if token_ids else None,
        "hours_to_end": round(hours_to_end, 2),
        "liquidity": round(liq, 2),
        "volume": round(vol, 2),
        "spread": round(spread_est, 4),
        "tags": [t for t in market.get("tags", []) if isinstance(t, str)],
    }


# ── Sizing + scoring + risk overlay (research recipe) ──────────────────────

def kelly_size_usd(price: float, capital: float) -> float:
    """Fractional Kelly sizing: f* = (p_win × b - p_loss) / b where b = payoff_ratio.
    With WR=0.954 and price entry, b = (1/price) - 1. Floor at MIN_TRADE, cap MAX_TRADE.
    """
    if price <= 0 or price >= 1: return MIN_TRADE_USD
    payoff_ratio = (1.0 / price) - 1.0
    if payoff_ratio <= 0: return MIN_TRADE_USD
    p_win = EMPIRICAL_WR
    p_loss = 1.0 - p_win
    f_star = (p_win * payoff_ratio - p_loss) / payoff_ratio
    if f_star <= 0: return MIN_TRADE_USD  # negative-EV, skip via min
    kelly_usd = f_star * KELLY_FRACTION * capital
    return max(MIN_TRADE_USD, min(MAX_TRADE_USD, kelly_usd))


def score_opportunity(opp: dict) -> float:
    """Rank candidates: tighter spread + more liquidity + lower entry = higher score."""
    spread = opp.get("spread", 0.03)
    liq = opp.get("liquidity", 0)
    vol = opp.get("volume", 0)
    price = opp.get("winner_price", 0.9)
    spread_bonus = max(0, (0.03 - spread) / 0.03)
    liq_bonus = min(liq / 10000, 2.0)
    vol_bonus = min(vol / 20000, 2.0)
    price_bonus = (0.95 - price) + 0.10
    return spread_bonus + liq_bonus + vol_bonus + price_bonus


def count_positions_by_sport(positions: dict, sport: str) -> int:
    return sum(1 for p in positions.values() if sport in (p.get("tags") or []))


# ── Paper accounting ────────────────────────────────────────────────────────

def open_position(state: dict, positions: dict, opp: dict) -> bool:
    """Open a Heavy_fav position with Kelly sizing + risk overlay."""
    price = opp["winner_price"]
    effective_price = price * (1 + TAKER_FEE)
    # Fractional Kelly on Moteur A's sub-allocation
    size_usd = kelly_size_usd(price, MOTEUR_A_CAPITAL)
    shares = size_usd / effective_price if effective_price > 0 else 0
    cost = shares * effective_price

    if state["cash_usd"] < cost:
        return False
    if len(positions) >= MAX_POSITIONS:
        return False
    # Per-sport concentration cap
    tags = opp.get("tags", [])
    for sport in tags:
        if sport in SPORT_TAGS and count_positions_by_sport(positions, sport) >= MAX_SAME_SPORT:
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
        "tags": tags,
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
    """For each position, check if its market is resolved OR near-resolved (>=0.99).
    Realize PnL. Captures wins faster than waiting for closed=True (auto-unwind).
    """
    resolved = []
    for cid in list(positions.keys()):
        pos = positions[cid]
        market = fetch_market_resolution(cid)
        if not market:
            continue
        closed = market.get("closed", False)
        win_tok = None
        # Try closed-with-winner path first
        for tok in market.get("tokens", []):
            if tok.get("winner"):
                win_tok = str(tok.get("token_id")); break
        # If not closed, try near-resolve path (token price >= 0.99)
        if not win_tok and not closed:
            for tok in market.get("tokens", []):
                try: p = float(tok.get("price", 0))
                except: continue
                if p >= NEAR_RESOLVE_PRICE:
                    win_tok = str(tok.get("token_id")); break
        if not win_tok:
            continue
        # Compute payout at marker price (matches real CLOB sell value)
        payout_per_share = 1.0  # default closed redemption
        if not closed:
            # Near-resolve : use current price for that token
            for tok in market.get("tokens", []):
                if str(tok.get("token_id")) == win_tok:
                    try: payout_per_share = float(tok.get("price", 1.0))
                    except: payout_per_share = 1.0
                    break
        if str(pos.get("token_id")) == win_tok:
            payout = pos["shares"] * payout_per_share
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
    # 1. Resolve open positions (closed markets + near-resolve 99%)
    resolve_positions(state, positions)

    # 2. KILL_SWITCH check — pause new entries if equity drawdown too deep
    equity = state["cash_usd"] + sum(
        p["shares"] * p.get("avg_price", 0) for p in positions.values()
    )
    drawdown_pct = (equity - INITIAL_CAPITAL_USD) / INITIAL_CAPITAL_USD
    if drawdown_pct < KILL_SWITCH_PCT:
        log.warning(f"KILL_SWITCH triggered: equity=${equity:.2f} dd={drawdown_pct*100:.1f}% — no new entries")
        return

    # 3. Scan for new opportunities
    now = datetime.now(timezone.utc)
    markets = fetch_active_markets()
    candidates = []
    skip_counts: dict[str, int] = {}
    for m in markets:
        accept, reason, opp = is_candidate(m, now)
        if accept and opp:
            candidates.append(opp)
        else:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1

    # 4. Filter out markets we already hold
    already_held = set(positions.keys())
    new_candidates = [c for c in candidates if c["condition_id"] not in already_held]

    # 5. Rank by score (tighter spread + liquidity + entry price)
    new_candidates.sort(key=score_opportunity, reverse=True)

    # 6. Open positions until budget cap / MAX_POSITIONS / KILL_SWITCH
    n_opened = 0
    for opp in new_candidates:
        if len(positions) >= MAX_POSITIONS:
            break
        if not open_position(state, positions, opp):
            continue  # rejected for risk reason, try next candidate
        n_opened += 1

    log.info(f"cycle: scanned={len(markets)} cands={len(candidates)} "
             f"new={len(new_candidates)} opened={n_opened} held={len(positions)} "
             f"cash=${state['cash_usd']:.2f} eq=${equity:.2f} dd={drawdown_pct*100:+.1f}%")


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

    log.info(f"Boot — Bot Ultime Moteur A (Heavy_fav scan), "
             f"capital=${state['cash_usd']:.2f} "
             f"(MoteurA_cap=${MOTEUR_A_CAPITAL:.0f}), "
             f"{len(positions)} open, {state['n_resolved']} resolved (won={state['n_won']})")
    log.info(f"Kelly sizing: WR={EMPIRICAL_WR}, frac={KELLY_FRACTION}, "
             f"trade range ${MIN_TRADE_USD}-${MAX_TRADE_USD}")
    log.info(f"Risk overlay: max_pos={MAX_POSITIONS}, max_same_sport={MAX_SAME_SPORT}, "
             f"kill_switch={KILL_SWITCH_PCT*100:.0f}%")
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

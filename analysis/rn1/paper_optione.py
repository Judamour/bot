"""RN1 paper bot — Option E: absband + cross-side / cross-event defense.

Hypothesis (2026-05-20 PM after live disconnect): RN1 injects deliberately
losing trades to dilute copytraders — symmetric BOTH-SIDE entries on the
same condition (Counter-Strike Legacy 64¢ + TYLOO 42¢ sum=1.06) AND
correlated trap entries on different markets of the same match (Arsenal
Yes-draw 9¢ + Arsenal O/U 1.5 Over 44¢, both lost when game ended 1-0).

Option E filters (vs absband baseline):
  1. cross-side same cid : skip BUY if we already hold an OPEN position on
     a different outcome of the same conditionId. Catches CS Legacy+TYLOO.
  2. cross-event same match : skip BUY if we already hold an OPEN position
     on a different market whose title shares the same "TEAM_A vs TEAM_B"
     fragment. Catches "Arsenal draw + Arsenal O/U" correlation trap.

Sizing identical to absband: penny $1, normal $4.5, MIN_TARGET=$200,
$4.5/market, max 8 positions.

Run :
    python -m analysis.rn1.paper_optione

systemd : deploy/rn1-paper-optione.service
"""
from __future__ import annotations
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .paper_bot import (
    _classify_market_type,
    _market_sport,
    _winning_token,
    _load_json,
    _save_json,
    _append_jsonl,
    _resolve_positions,
)

# --- Paths (separate from siblings) ---
DATA_DIR = Path(__file__).resolve().parent / "data_optione"
POSITIONS_PATH = DATA_DIR / "positions.json"
TRADES_LOG_PATH = DATA_DIR / "trades.jsonl"
EQUITY_PATH = DATA_DIR / "equity.jsonl"
STATE_PATH = DATA_DIR / "state.json"
MARKETS_PATH = Path(__file__).resolve().parent / "data" / "markets.jsonl"

# --- Source: bot-cp.service decisions ---
DECISIONS_PATH = Path(os.environ.get(
    "BOT_CP_DECISIONS",
    "/home/botuser/bot-trading/logs/copytrade/decisions.jsonl",
))

CLOB_API = "https://clob.polymarket.com"
POLL_INTERVAL_S = int(os.environ.get("RN1_OPTIONE_POLL_S", "60"))
DEDUP_WINDOW_S = int(os.environ.get("RN1_OPTIONE_DEDUP_WINDOW_S", "300"))

# --- Capital + position caps ---
INITIAL_CAPITAL_USD = float(os.environ.get("RN1_OPTIONE_INITIAL", "600"))
MAX_POSITIONS = int(os.environ.get("RN1_OPTIONE_MAX_POSITIONS", "8"))
MAX_USD_PER_MARKET = float(os.environ.get("RN1_OPTIONE_MAX_USD_PER_MARKET", "4.5"))
MIN_TARGET_SIZE_USD = float(os.environ.get("RN1_OPTIONE_MIN_TARGET", "200"))
TAKER_FEE = float(os.environ.get("RN1_OPTIONE_TAKER_FEE", "0.02"))

# --- Tiered bands (env reproduces absband layout) ---
TIER_PENNY_MIN = float(os.environ.get("RN1_OPTIONE_PENNY_MIN", "0.06"))
TIER_PENNY_MAX = float(os.environ.get("RN1_OPTIONE_PENNY_MAX", "0.20"))
TIER_PENNY_SIZE = float(os.environ.get("RN1_OPTIONE_PENNY_SIZE", "1.0"))
TIER_SKIP_HIGH = float(os.environ.get("RN1_OPTIONE_SKIP_HIGH", "0.20"))
TIER_NORMAL_MAX = float(os.environ.get("RN1_OPTIONE_NORMAL_MAX", "0.95"))
TIER_NORMAL_SIZE = float(os.environ.get("RN1_OPTIONE_NORMAL_SIZE", "4.5"))

# Polymarket min-order floor (5 shares)
MIN_SHARES = 5.0

# Extract "TEAM_A vs TEAM_B" from common Polymarket titles, e.g.
#   "Arsenal FC vs. Burnley FC: O/U 1.5"
#   "Will Arsenal FC vs. Burnley FC end in a draw?"
#   "Counter-Strike: Legacy vs TYLOO - Map 1 Winner"
#   "Geneva Open: Stefanos Tsitsipas vs Learner Tien"
_LEAD_WILL_RE = re.compile(r"^will\s+", re.IGNORECASE)
_MATCH_RE = re.compile(
    r"([A-Za-z][\w\-\.'/ &]+?)\s+vs\.?\s+([A-Za-z][\w\-\.'/ &]+?)"
    r"(?:\s*[:?(]|\s+-\s+|\s+(?:end|win|on|Map|BO\d)\b|$)",
    re.IGNORECASE,
)
_TEAM_NOISE_SUFFIXES = (" fc", " sc", " cf")


def _match_key(title: str) -> str | None:
    """Return a normalized 'team_a|team_b' key (sorted) if title carries a
    'A vs B' fragment, else None. Sorting makes the key order-invariant.
    Imperfect on Spread/ML one-sided titles (e.g. 'Spread: Liaoning (-2.5)')
    which lack 'vs' — those slip through and are not cross-blocked."""
    if not title:
        return None
    t = title.strip()
    # Drop "Will " prefix so it doesn't leak into team A.
    t = _LEAD_WILL_RE.sub("", t)
    # Drop category prefix like "Counter-Strike: " or "Geneva Open: ",
    # but only if there's no "vs" before the colon (else we'd cut the match).
    if ":" in t:
        head, _, tail = t.partition(":")
        if "vs" not in head.lower():
            t = tail.strip()
    m = _MATCH_RE.match(t)
    if not m:
        return None
    a, b = m.group(1).strip().lower(), m.group(2).strip().lower()
    for suf in _TEAM_NOISE_SUFFIXES:
        if a.endswith(suf): a = a[:-len(suf)].strip()
        if b.endswith(suf): b = b[:-len(suf)].strip()
    if not a or not b:
        return None
    return "|".join(sorted([a, b]))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rn1-optione")

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False
    log.info("SIGTERM, exit at next loop iteration")


def _load_markets() -> dict:
    out: dict = {}
    if not MARKETS_PATH.exists():
        return out
    with open(MARKETS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                cid = m.get("condition_id") or m.get("conditionId")
                if cid and not m.get("_missing"):
                    out[cid] = m
            except Exception:
                pass
    return out


def _fetch_market_ondemand(cid: str) -> dict | None:
    try:
        r = httpx.get(f"{CLOB_API}/markets/{cid}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"fetch market {cid[:10]}... failed: {e}")
    return None


def _append_market_to_cache(market: dict) -> None:
    try:
        with open(MARKETS_PATH, "a") as f:
            f.write(json.dumps(market) + "\n")
    except Exception:
        pass


def _refresh_open_markets(positions: dict, markets: dict) -> int:
    """Re-fetch markets that have open positions and aren't yet known as closed.

    Without this, the in-memory `markets` cache only updates at boot (from
    markets.jsonl) or when a brand-new market is fetched on-demand. Resolutions
    of already-open positions go undetected until the next reboot — this loop
    closes that gap. Returns the number of markets that flipped to closed during
    this refresh.
    """
    n_newly_closed = 0
    for pos in list(positions.values()):
        cid = pos.get("condition_id")
        if not cid:
            continue
        cached = markets.get(cid, {})
        if cached.get("closed"):
            continue  # already resolved, no need to re-fetch
        fresh = _fetch_market_ondemand(cid)
        if not fresh:
            continue
        markets[cid] = fresh
        if fresh.get("closed"):
            n_newly_closed += 1
            _append_market_to_cache(fresh)
    return n_newly_closed


def _compute_size_usd(price: float) -> float | None:
    """Return USD to allocate, or None to skip. RN1-tuned absolute_band.

    The TIER_SKIP_HIGH branch is dead code under the current service env
    (SKIP_HIGH=PENNY_MAX=0.20); kept so a deploy can re-enable the mid_low
    skip by raising SKIP_HIGH without code changes.
    """
    if price < TIER_PENNY_MIN:
        return None  # lottery
    if price < TIER_PENNY_MAX:
        return TIER_PENNY_SIZE  # forced to 5+ shares via MIN_SHARES floor
    if price < TIER_SKIP_HIGH:
        return None  # mid_low — disabled when env SKIP_HIGH<=PENNY_MAX
    if price <= TIER_NORMAL_MAX:
        return TIER_NORMAL_SIZE
    return None  # > NORMAL_MAX, edge too thin


def _describe_tier(price: float) -> str:
    if price < TIER_PENNY_MIN:
        return "lottery"
    if price < TIER_PENNY_MAX:
        return "penny"
    if price < TIER_SKIP_HIGH:
        return "losing_zone"
    if price <= TIER_NORMAL_MAX:
        return "normal"
    return "thin_edge"


def _trade_passes(
    trade: dict,
    market: dict | None,
    positions: dict,
) -> tuple[bool, str]:
    if trade.get("side") != "BUY":
        return False, "not_buy"
    cid = trade.get("conditionId")
    asset = trade.get("asset")
    if not cid or cid in (None, "None", "") or not asset or asset in (None, "None", ""):
        return False, "missing_ids"
    price = float(trade.get("price", 0))
    target_size = float(trade.get("target_size_usd") or 0)
    if target_size and target_size < MIN_TARGET_SIZE_USD:
        return False, f"target_too_small ({target_size:.0f}<{MIN_TARGET_SIZE_USD:.0f})"
    tier = _describe_tier(price)
    if tier in ("lottery", "losing_zone", "thin_edge"):
        return False, f"tier_{tier}({price:.3f})"

    # --- Option E cross-* filters — 2026-05-20 PM ---
    # Defense against RN1 anti-copytrade injection.

    # 1. cross-side same cid : opposite outcome already held.
    #    Catches Counter-Strike Legacy 64¢ + TYLOO 42¢ (sum 1.06 = guaranteed
    #    loss on the pair). When RN1 buys both sides we'd otherwise eat both.
    for held_asset, pos in positions.items():
        if pos.get("condition_id") == cid and str(held_asset) != str(asset):
            return False, f"opte_cross_side_cid"

    # 2. cross-event same match : different market, same TEAM_A vs TEAM_B.
    #    Catches "Arsenal vs Burnley draw" + "Arsenal vs Burnley O/U 1.5"
    #    correlation trap (both legs lost together when game ended 1-0).
    new_title = trade.get("title") or trade.get("market") or ""
    new_match = _match_key(new_title)
    if new_match:
        for pos in positions.values():
            if pos.get("condition_id") == cid:
                continue  # same market handled by cross-side rule above
            held_match = _match_key(pos.get("title") or "")
            if held_match and held_match == new_match:
                return False, f"opte_cross_event_match"
    # --- end Option E filters ---

    return True, "ok"


def _dedup_recent_buy(state: dict, cid: str | None, outcome: str | None, ts: int) -> bool:
    key = f"{cid or 'nocid'}|{outcome or ''}"
    recent: dict = state.setdefault("recent_buys", {})
    cutoff = ts - DEDUP_WINDOW_S
    for k in [k for k, v in recent.items() if v < cutoff]:
        del recent[k]
    if recent.get(key, 0) >= cutoff:
        return True
    recent[key] = ts
    return False


def _tail_decisions(since_ts: int) -> list[dict]:
    if not DECISIONS_PATH.exists():
        return []
    out = []
    try:
        with open(DECISIONS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = int(d.get("ts") or d.get("timestamp") or 0)
                if ts <= since_ts:
                    continue
                out.append(d)
    except Exception as e:
        log.warning(f"read decisions failed: {e}")
    return out


def _to_trade_format(d: dict) -> dict:
    return {
        "timestamp": int(d.get("ts") or d.get("timestamp") or 0),
        "side": d.get("side"),
        "asset": d.get("asset"),
        "conditionId": d.get("conditionId") or d.get("condition_id"),
        "size": d.get("size") or 0,
        "price": float(d.get("price") or 0),
        "title": d.get("market") or d.get("title") or "",
        "outcome": d.get("outcome") or "",
        "transactionHash": d.get("target_hash") or d.get("transactionHash") or "",
        "target_size_usd": float(d.get("target_size_usd") or 0),
    }


def _market_exposure(positions: dict, condition_id: str) -> float:
    return sum(
        p.get("cost_basis", 0)
        for p in positions.values()
        if p.get("condition_id") == condition_id
    )


def _open_position_absband(
    state: dict, positions: dict, trade: dict, market: dict | None
) -> dict:
    price = float(trade.get("price", 0))
    size_usd = _compute_size_usd(price)
    if size_usd is None:
        return {"skipped": "tier_decision_none"}

    cid = trade.get("conditionId") or ""
    # Saturation cap per market (handles his DCA/partial fills)
    cur_exposure = _market_exposure(positions, cid)
    eff_min_cost = MIN_SHARES * price * (1 + TAKER_FEE)
    target_cost = max(size_usd, eff_min_cost)
    if cur_exposure + target_cost > MAX_USD_PER_MARKET:
        return {"skipped": "market_saturated", "current": cur_exposure}

    # Max positions cap (treats a NEW market entry as a new slot)
    asset = str(trade.get("asset"))
    if asset not in positions and len(positions) >= MAX_POSITIONS:
        return {"skipped": "max_positions"}

    # Buy: shares = USD / (price × (1+fee)), floored to 5 shares
    effective_price = price * (1 + TAKER_FEE)
    shares = size_usd / effective_price if effective_price > 0 else 0
    if shares < MIN_SHARES:
        shares = MIN_SHARES
    cost = shares * effective_price

    if state["cash_usd"] < cost:
        return {"skipped": "insufficient_cash", "needed": cost, "have": state["cash_usd"]}

    state["cash_usd"] -= cost
    pos = positions.get(asset)
    if pos:
        total_shares = pos["shares"] + shares
        pos["shares"] = total_shares
        pos["cost_basis"] = pos["cost_basis"] + cost
        pos["avg_price"] = pos["cost_basis"] / total_shares if total_shares else 0
        pos["n_buys"] = pos.get("n_buys", 1) + 1
    else:
        positions[asset] = {
            "asset": asset,
            "condition_id": cid,
            "title": trade.get("title"),
            "outcome": trade.get("outcome"),
            "shares": shares,
            "avg_price": effective_price,
            "cost_basis": cost,
            "opened_ts": int(trade.get("timestamp", time.time())),
            "n_buys": 1,
            "tier": _describe_tier(price),
        }
    return {
        "opened": True, "asset": asset, "shares": shares,
        "price": effective_price, "cost": cost, "tier": _describe_tier(price),
    }


def _equity_snapshot(state: dict, positions: dict, markets: dict) -> dict:
    mtm = 0.0
    for asset, pos in positions.items():
        m = markets.get(pos.get("condition_id")) or {}
        price = None
        for tok in m.get("tokens", []):
            if str(tok.get("token_id")) == asset:
                p = tok.get("price")
                if p is not None:
                    try:
                        price = float(p)
                    except Exception:
                        pass
                break
        mtm += pos["shares"] * (price if price is not None else pos["avg_price"])
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


def _cycle(state: dict, positions: dict, markets: dict) -> tuple[int, int, int]:
    last_seen = int(state.get("last_seen_ts", 0))
    decisions = _tail_decisions(last_seen)

    # Refresh markets with open positions BEFORE resolving — catches closures
    # that happened between cycles (matches finishing mid-day).
    n_newly_closed = _refresh_open_markets(positions, markets)
    if n_newly_closed:
        log.info(f"refresh: {n_newly_closed} open market(s) flipped closed, resolving")

    resolved_before = _resolve_positions(state, positions, markets)
    for r in resolved_before:
        _append_jsonl(TRADES_LOG_PATH, r)
        log.info(f"RESOLVE {r['result']:<5} {r['title'][:50]} pnl=${r['pnl']:+.2f}")

    n_examined = n_opened = 0
    skip_counts: dict[str, int] = {}

    for d in decisions:
        if d.get("wallet") != "RN1":
            continue
        if d.get("action") != "executed":
            continue
        n_examined += 1

        trade = _to_trade_format(d)
        cid = trade.get("conditionId")
        market = markets.get(cid)
        if not market:
            market = _fetch_market_ondemand(cid)
            if market:
                markets[cid] = market
                _append_market_to_cache(market)

        accept, reason = _trade_passes(trade, market, positions)
        if not accept:
            key = reason.split("(")[0].strip()
            skip_counts[key] = skip_counts.get(key, 0) + 1
            state["last_seen_ts"] = max(state["last_seen_ts"], trade["timestamp"])
            continue

        if _dedup_recent_buy(state, cid, trade.get("outcome"), trade["timestamp"]):
            skip_counts["dedup_recent_buy"] = skip_counts.get("dedup_recent_buy", 0) + 1
            state["last_seen_ts"] = max(state["last_seen_ts"], trade["timestamp"])
            continue

        result = _open_position_absband(state, positions, trade, market)
        if result.get("opened"):
            n_opened += 1
            _append_jsonl(TRADES_LOG_PATH, {
                "ts": trade["timestamp"],
                "action": "buy",
                "tier": result.get("tier"),
                "asset": result["asset"],
                "title": trade.get("title"),
                "outcome": trade.get("outcome"),
                "shares": result["shares"],
                "price_with_fee": result["price"],
                "cost": result["cost"],
            })
            log.info(f"BUY [{result.get('tier')}] {trade.get('title','?')[:50]} @ "
                     f"{result['price']:.3f} -> {result['shares']:.2f} sh "
                     f"(${result['cost']:.2f}), cash=${state['cash_usd']:.2f}")
        else:
            key = result.get("skipped", "unknown")
            skip_counts[key] = skip_counts.get(key, 0) + 1

        state["last_seen_ts"] = max(state["last_seen_ts"], trade["timestamp"])

    resolved_after = _resolve_positions(state, positions, markets)
    for r in resolved_after:
        _append_jsonl(TRADES_LOG_PATH, r)
        log.info(f"RESOLVE {r['result']:<5} (just-opened) {r['title'][:40]} pnl=${r['pnl']:+.2f}")

    n_resolved_total = len(resolved_before) + len(resolved_after)

    if n_examined or n_opened or n_resolved_total or skip_counts:
        skip_str = " ".join(f"{k}={v}" for k, v in skip_counts.items())
        log.info(f"cycle: OPTE examined={n_examined} opened={n_opened} "
                 f"resolved={n_resolved_total} skips:[{skip_str}] "
                 f"open_total={len(positions)} cash=${state['cash_usd']:.2f}")

    return n_examined, n_opened, n_resolved_total


def _maybe_snapshot_equity(state: dict, positions: dict, markets: dict, last_snap_date: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = _equity_snapshot(state, positions, markets)
    last_date_in_file = None
    if EQUITY_PATH.exists():
        with open(EQUITY_PATH) as f:
            lines = f.readlines()
        if lines:
            try:
                last_date_in_file = json.loads(lines[-1]).get("date")
            except Exception:
                pass
    if last_date_in_file == today:
        with open(EQUITY_PATH) as f:
            lines = f.readlines()
        if lines:
            lines[-1] = json.dumps(snap) + "\n"
        with open(EQUITY_PATH, "w") as f:
            f.writelines(lines)
    else:
        _append_jsonl(EQUITY_PATH, snap)
    return today


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Fresh boot (no state.json) → start tracking from NOW, no replay of history.
    _now = int(time.time())
    state = _load_json(STATE_PATH, {
        "cash_usd": INITIAL_CAPITAL_USD,
        "realized_pnl": 0.0,
        "n_resolved": 0,
        "n_won": 0,
        "last_seen_ts": _now,
        "boot_ts": _now,
        "recent_buys": {},
    })
    positions = _load_json(POSITIONS_PATH, {})
    markets = _load_markets()

    log.info(f"Boot — RN1 paper Option E (cross-side+cross-event), "
             f"capital=${INITIAL_CAPITAL_USD}, max_pos={MAX_POSITIONS}, "
             f"max_per_market=${MAX_USD_PER_MARKET}, min_target=${MIN_TARGET_SIZE_USD}")
    log.info(f"Tier grid — penny[{TIER_PENNY_MIN}-{TIER_PENNY_MAX}): ${TIER_PENNY_SIZE} | "
             f"SKIP[{TIER_PENNY_MAX}-{TIER_SKIP_HIGH}) | "
             f"normal[{TIER_SKIP_HIGH}-{TIER_NORMAL_MAX}]: ${TIER_NORMAL_SIZE} | "
             f"SKIP >{TIER_NORMAL_MAX}")
    log.info(f"Source: {DECISIONS_PATH}")
    log.info(f"State: last_seen_ts={state['last_seen_ts']}, "
             f"open={len(positions)}, cash=${state['cash_usd']:.2f}")

    last_snap_date = ""
    while _running:
        try:
            _cycle(state, positions, markets)
            _save_json(STATE_PATH, state)
            _save_json(POSITIONS_PATH, positions)
            last_snap_date = _maybe_snapshot_equity(state, positions, markets, last_snap_date)
        except Exception as e:
            log.error(f"Cycle exception: {type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL_S)

    log.info("Stopped")


if __name__ == "__main__":
    main()

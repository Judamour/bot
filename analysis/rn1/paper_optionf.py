"""RN1 paper bot — Option F: Option C + pre-event + Qualif skip.

Rationale (2026-05-20 PM): Option C leads B/absband on calm-flow days, but
would have lost the same ~$66 as Option A this morning because it lacks
the two filters that block RN1's most toxic flow:
  - Tennis Qualification (6 RG Qualif positions all to $0 on 2026-05-20)
  - Live-game arbitrage (Phemex Apr 2026: RN1's 45s info edge, unbeatable
    via REST polling — copying his live trades = structural loss)

Option F = C base filters + 2 protections from E v2.1. The cross-side and
cross-event filters from E are NOT added (their cost-benefit is unclear on
this sample; will be added if F bleeds on the both-side trap).

Filter set:
  C inheritance:
    - skip hour 19 UTC only (the one truly bad hour, NOT 18-23)
    - skip Monday (64.7% WR per session 2026-05-20 deep analysis)
    - skip lottery <$0.06 entry price
    - skip mtype="other" (catch-all, low signal)
    - skip whale trades >$10K target_size
  F additions:
    - skip Qualification markets (env-toggleable: all|heavy_fav_ok|off)
    - pre-event only via Gamma gameStartTime (skip if match started
      OR starts within RN1_OPTIONF_PRE_EVENT_MIN_S seconds, default 1800)

Effective bands with service env (SKIP_HIGH=0.20, NORMAL_MAX=0.95):
  - <0.06              -> SKIP (lottery)
  - [0.06-0.20)        -> $1.00 (penny)
  - [0.20-0.95]        -> $4.50 (normal)
  - >0.95              -> SKIP (thin edge)

Run :
    python -m analysis.rn1.paper_optionf

systemd : deploy/rn1-paper-optionf.service
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

from .analyze_deep import classify_market_type
from .paper_bot import (
    _classify_market_type,
    _market_sport,
    _winning_token,
    _load_json,
    _save_json,
    _append_jsonl,
    _resolve_positions,
)

# --- Paths (separate from rn1-live-paper / paper_bot) ---
DATA_DIR = Path(__file__).resolve().parent / "data_optionf"
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
POLL_INTERVAL_S = int(os.environ.get("RN1_OPTIONF_POLL_S", "60"))
# RN1 splits big orders into many small ones in seconds; coalesce them.
DEDUP_WINDOW_S = int(os.environ.get("RN1_OPTIONF_DEDUP_WINDOW_S", "300"))

# --- Capital + position caps (mirror live $40 surfandturf constraints) ---
INITIAL_CAPITAL_USD = float(os.environ.get("RN1_OPTIONF_INITIAL", "600"))
MAX_POSITIONS = int(os.environ.get("RN1_OPTIONF_MAX_POSITIONS", "8"))
MAX_USD_PER_MARKET = float(os.environ.get("RN1_OPTIONF_MAX_USD_PER_MARKET", "5.0"))
MIN_TARGET_SIZE_USD = float(os.environ.get("RN1_OPTIONF_MIN_TARGET", "200"))
TAKER_FEE = float(os.environ.get("RN1_OPTIONF_TAKER_FEE", "0.02"))

# --- Tiered bands (RN1-tuned) ---
TIER_PENNY_MIN = float(os.environ.get("RN1_OPTIONF_PENNY_MIN", "0.06"))
TIER_PENNY_MAX = float(os.environ.get("RN1_OPTIONF_PENNY_MAX", "0.20"))
TIER_PENNY_SIZE = float(os.environ.get("RN1_OPTIONF_PENNY_SIZE", "1.0"))
TIER_SKIP_HIGH = float(os.environ.get("RN1_OPTIONF_SKIP_HIGH", "0.45"))  # below this AND above PENNY_MAX = skip
TIER_NORMAL_MAX = float(os.environ.get("RN1_OPTIONF_NORMAL_MAX", "0.95"))
TIER_NORMAL_SIZE = float(os.environ.get("RN1_OPTIONF_NORMAL_SIZE", "4.5"))

# Polymarket min-order floor (5 shares)
MIN_SHARES = 5.0

# --- Bot F v1 knobs (pre-event + Qualif, ported from Bot E v2.1) ---
# Skip BUY if match has already started or starts within PRE_EVENT_MIN_S.
# -1 disables. Phemex Apr 2026: RN1's edge is ~45s live-score arbitrage,
# uncopyable via REST polling.
PRE_EVENT_MIN_S = int(os.environ.get("RN1_OPTIONF_PRE_EVENT_MIN_S", "1800"))
# Tennis Qualif policy: "off" (no skip) | "all" (default, conservative) |
# "heavy_fav_ok" (allow only entries >= 0.80, RN1's confirmed edge zone).
# Empirical 2026-05-20: user lost ~$31 on 6 RG Qualif mid-price positions.
QUALIF_MODE = os.environ.get("RN1_OPTIONF_SKIP_QUALIF", "all").lower()

GAMMA_API = "https://gamma-api.polymarket.com"
_GAMMA_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# Module-level cache: condition_id -> game_start_ts (epoch seconds; 0=unknown)
_gamma_start_ts_cache: dict[str, int] = {}


def _parse_gamma_ts(s: str) -> int:
    """Parse Gamma's gameStartTime ('YYYY-MM-DD HH:MM:SS+00') to epoch sec."""
    if not s:
        return 0
    s = s.strip().replace(" ", "T")
    if s.endswith("+00"):
        s = s + "00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


def _fetch_gamma_start_ts(cid: str) -> int:
    """Return game_start_ts for a condition_id, cached. 0 if unknown."""
    if cid in _gamma_start_ts_cache:
        return _gamma_start_ts_cache[cid]
    try:
        req = httpx.get(
            f"{GAMMA_API}/markets",
            params={"condition_ids": cid},
            headers=_GAMMA_UA,
            timeout=10,
        )
        if req.status_code == 200:
            data = req.json()
            if isinstance(data, list) and data:
                ts = _parse_gamma_ts(data[0].get("gameStartTime") or "")
                _gamma_start_ts_cache[cid] = ts
                return ts
    except Exception as e:
        log.warning(f"gamma fetch {cid[:10]}... failed: {e}")
    _gamma_start_ts_cache[cid] = 0
    return 0


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rn1-optionf")

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


def _trade_passes(trade: dict, market: dict | None) -> tuple[bool, str]:
    if trade.get("side") != "BUY":
        return False, "not_buy"
    cid = trade.get("conditionId")
    asset = trade.get("asset")
    if not cid or cid in (None, "None", "") or not asset or asset in (None, "None", ""):
        # Can't resolve this position without IDs; avoid creating untracked dust.
        return False, "missing_ids"
    price = float(trade.get("price", 0))
    target_size = float(trade.get("target_size_usd") or 0)
    if target_size and target_size < MIN_TARGET_SIZE_USD:
        return False, f"target_too_small ({target_size:.0f}<{MIN_TARGET_SIZE_USD:.0f})"
    tier = _describe_tier(price)
    if tier in ("lottery", "losing_zone", "thin_edge"):
        return False, f"tier_{tier}({price:.3f})"

    # --- Option C filters — 2026-05-20 corrected pattern analysis ---
    # Data: cross-ref of BUYs (decisions.jsonl) + REDEEMs + current /positions
    # with WIN/LOST/OPEN labeling. UNKNOWN inferred as WIN (winner already
    # redeemed). Patterns identified across 913 resolved markets.
    #
    # Option B v2 had 2 wrong filters (Qualification 95%WR not 51%, and
    # hour 18-24 had hours 20-23 at 100%WR). Option C corrects these.

    # 1. Skip ONLY hour 19 UTC (53.3% WR; hours 20-23 are 100% WR)
    ts = int(trade.get("timestamp") or trade.get("ts") or 0)
    if ts > 0:
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt_utc.hour == 19:
            return False, "optc_hour_19"
        # 2. Skip Mondays (64.7% WR vs 98-100% on Sun/Tue/Wed)
        if dt_utc.weekday() == 0:
            return False, "optc_monday"

    # 3. Skip lottery tickets (entry price < $0.06) — 36.4% WR confirmed loser
    #    across all market types. RN1's penny stretch lower bound was the
    #    actual losing zone, not the 0.20-0.45 mid-low band.
    if price < 0.06:
        return False, f"optc_lottery({price:.3f})"

    # 4. Skip 'other' mtype (catch-all, low signal by definition).
    title = trade.get("title") or trade.get("market") or ""
    outcome = trade.get("outcome") or ""
    mtype = classify_market_type(title, outcome)
    if mtype == "other":
        return False, f"optc_bad_mtype({mtype})"

    # 5. Skip whale trades (>$10K = -27% ROI manipulation/desperate DCA)
    if target_size and target_size > 10000:
        return False, f"optc_whale({target_size:.0f})"
    # --- end inherited Option C filters ---

    # --- Bot F additions (2026-05-20 PM) ---

    # 6. Tennis Qualification skip — empirical 2026-05-20 PM evidence: user's
    #    wallet lost ~$31 on 6 Roland Garros Qualif positions at mid-price
    #    45-65¢ (Akugue, Clarke, Pigato, Bail, Dimitrov, Zidansek). The
    #    earlier REDEEM analysis claiming 95% WR was biased toward heavy_fav
    #    fast resolutions and missed mid-price slow coin flips.
    if QUALIF_MODE != "off" and "qualification" in title.lower():
        if QUALIF_MODE == "heavy_fav_ok" and price >= 0.80:
            pass
        else:
            return False, f"optf_qualif({QUALIF_MODE})"

    # 7. Pre-event only — Phemex Apr 2026 identified RN1's edge as ~45s info
    #    advantage on live-score markets. Uncopyable via data-api REST. Skip
    #    BUYs placed after kickoff (or within PRE_EVENT_MIN_S of it).
    if PRE_EVENT_MIN_S >= 0 and ts > 0:
        start_ts = _fetch_gamma_start_ts(cid)
        if start_ts <= 0:
            return False, "optf_no_gametime"
        seconds_until_start = start_ts - ts
        if seconds_until_start < PRE_EVENT_MIN_S:
            return False, f"optf_live_or_close({seconds_until_start}s)"
    # --- end Bot F additions ---

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

        accept, reason = _trade_passes(trade, market)
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
        log.info(f"cycle: OPTF examined={n_examined} opened={n_opened} "
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

    pre_evt = (
        f"pre_event>={PRE_EVENT_MIN_S}s" if PRE_EVENT_MIN_S >= 0 else "pre_event=off"
    )
    log.info(f"Boot — RN1 paper Option F (C filters + qualif={QUALIF_MODE} + {pre_evt}), "
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

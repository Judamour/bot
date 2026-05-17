"""RN1 live paper bot — long-running version of paper_bot.py.

Isolated from bot-cp.service : reads decisions.jsonl (written by bot-cp every
60s) but never touches bot-cp itself. Crash here = no impact on live $40
surfandturf bot.

Loop every 60s :
1. Tail /home/botuser/bot-trading/logs/copytrade/decisions.jsonl
2. Filter wallet=RN1 + action=executed + ts > last_seen_ts
3. Apply edge filters (favorite bucket, sport whitelist, market_type)
4. Open paper position (fetch market metadata on-demand if missing)
5. Check open positions for resolution
6. Persist state, sleep 60s

Run :
    python -m analysis.rn1.live_paper

systemd : deploy/rn1-live-paper.service
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

from .paper_bot import (
    DATA_DIR, POSITIONS_PATH, TRADES_LOG_PATH, EQUITY_PATH, STATE_PATH,
    INITIAL_CAPITAL_USD, FIXED_SIZE_USD, TAKER_FEE,
    MIN_PRICE, MAX_PRICE, SPORT_WHITELIST, MARKET_TYPE_WHITELIST,
    _classify_market_type, _market_sport, _winning_token,
    _load_json, _save_json, _append_jsonl,
    _trade_passes_filters, _open_paper_position, _resolve_positions,
    _equity_snapshot,
)

# Source de signaux = decisions.jsonl écrit par bot-cp.service
DECISIONS_PATH = Path(os.environ.get(
    "BOT_CP_DECISIONS",
    "/home/botuser/bot-trading/logs/copytrade/decisions.jsonl",
))
# Fallback pour dev local
if not DECISIONS_PATH.exists():
    DECISIONS_PATH = Path(__file__).resolve().parent / "data" / "trades.jsonl"

MARKETS_PATH = DATA_DIR / "markets.jsonl"
POLL_INTERVAL_S = int(os.environ.get("RN1_PAPER_POLL_S", "60"))
CLOB_API = "https://clob.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rn1-paper")

_running = True


def _sigterm(_sig, _frame):
    global _running
    _running = False
    log.info("SIGTERM, exit at next loop iteration")


def _load_markets() -> dict:
    out = {}
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
    """Fetch a market we don't have cached. Adds 1 API call only for truly new markets."""
    try:
        r = httpx.get(f"{CLOB_API}/markets/{cid}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"fetch market {cid[:10]}... failed: {e}")
    return None


def _append_market_to_cache(market: dict) -> None:
    """Persist newly-fetched market into markets.jsonl so daily run + analyze see it."""
    try:
        with open(MARKETS_PATH, "a") as f:
            f.write(json.dumps(market) + "\n")
    except Exception:
        pass


def _tail_decisions(since_ts: int) -> list[dict]:
    """Read decisions.jsonl entries strictly newer than since_ts."""
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
    """Adapt bot-cp decision format to fetch_trades trade format expected by paper_bot."""
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
    }


def _cycle(state: dict, positions: dict, markets: dict) -> tuple[int, int, int]:
    """One iteration of the loop. Returns (n_examined, n_opened, n_resolved)."""
    last_seen = int(state.get("last_seen_ts", 0))
    decisions = _tail_decisions(last_seen)

    # First pass : resolve any open positions that may have closed since last cycle
    resolved_before = _resolve_positions(state, positions, markets)
    for r in resolved_before:
        _append_jsonl(TRADES_LOG_PATH, r)
        log.info(f"RESOLVE {r['result']:<5} {r['title'][:50]} pnl=${r['pnl']:+.2f}")

    n_examined = 0
    n_opened = 0
    skip_counts: dict[str, int] = {}

    # bot-cp emits decisions for all 3 wallets; we filter to RN1 only
    for d in decisions:
        wallet = d.get("wallet")
        if wallet != "RN1":
            continue
        if d.get("action") != "executed":
            continue
        n_examined += 1

        trade = _to_trade_format(d)
        cid = trade.get("conditionId")
        market = markets.get(cid)
        if not market:
            # On-demand fetch — only fires for markets daily enrichment hasn't seen yet
            market = _fetch_market_ondemand(cid)
            if market:
                markets[cid] = market
                _append_market_to_cache(market)

        accept, reason = _trade_passes_filters(trade, market)
        if not accept:
            skip_counts[reason.split("(")[0].strip()] = skip_counts.get(
                reason.split("(")[0].strip(), 0) + 1
            state["last_seen_ts"] = max(state["last_seen_ts"], trade["timestamp"])
            continue

        result = _open_paper_position(state, positions, trade, market)
        if result.get("opened"):
            n_opened += 1
            _append_jsonl(TRADES_LOG_PATH, {
                "ts": trade["timestamp"],
                "action": "buy",
                "asset": result["asset"],
                "title": trade.get("title"),
                "outcome": trade.get("outcome"),
                "shares": result["shares"],
                "price_with_fee": result["price"],
                "cost": result["cost"],
            })
            log.info(f"BUY {trade.get('title', '?')[:50]} @ {result['price']:.3f} → "
                     f"{result['shares']:.2f} shares ($"
                     f"{result['cost']:.2f}), cash=${state['cash_usd']:.2f}")
        else:
            skip_counts[result.get("skipped", "unknown")] = skip_counts.get(
                result.get("skipped", "unknown"), 0) + 1

        state["last_seen_ts"] = max(state["last_seen_ts"], trade["timestamp"])

    # Second resolution pass for any just-opened positions on already-closed markets
    resolved_after = _resolve_positions(state, positions, markets)
    for r in resolved_after:
        _append_jsonl(TRADES_LOG_PATH, r)
        log.info(f"RESOLVE {r['result']:<5} (just-opened) {r['title'][:40]} pnl=${r['pnl']:+.2f}")

    n_resolved_total = len(resolved_before) + len(resolved_after)

    if n_examined or n_opened or n_resolved_total or skip_counts:
        skip_str = " ".join(f"{k}={v}" for k, v in skip_counts.items())
        log.info(f"cycle: RN1 examined={n_examined} opened={n_opened} "
                 f"resolved={n_resolved_total} skips:[{skip_str}] "
                 f"open_total={len(positions)}")

    return n_examined, n_opened, n_resolved_total


def _maybe_snapshot_equity(state: dict, positions: dict, markets: dict, last_snap_date: str) -> str:
    """Append/replace today's equity snapshot. Returns the date written."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = _equity_snapshot(state, positions, markets)
    # Dedup: if today's snap already at tail, replace it; else append
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

    state = _load_json(STATE_PATH, {
        "cash_usd": INITIAL_CAPITAL_USD,
        "realized_pnl": 0.0,
        "n_resolved": 0,
        "n_won": 0,
        "last_seen_ts": 0,
        "boot_ts": int(time.time()),
    })
    positions = _load_json(POSITIONS_PATH, {})
    markets = _load_markets()

    log.info(f"Boot — capital=${state['cash_usd']:.2f}, {len(positions)} open, "
             f"{state['n_resolved']} resolved (won={state['n_won']}), "
             f"last_seen_ts={state['last_seen_ts']}")
    log.info(f"Source: {DECISIONS_PATH}")
    log.info(f"Filters: price∈[{MIN_PRICE}, {MAX_PRICE}], "
             f"sports={sorted(SPORT_WHITELIST)}, "
             f"market_types={sorted(MARKET_TYPE_WHITELIST)}")
    log.info(f"Poll interval: {POLL_INTERVAL_S}s")

    last_snap_date = ""
    while _running:
        cycle_start = time.time()
        try:
            _cycle(state, positions, markets)
            _save_json(STATE_PATH, state)
            _save_json(POSITIONS_PATH, positions)
            last_snap_date = _maybe_snapshot_equity(state, positions, markets, last_snap_date)
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

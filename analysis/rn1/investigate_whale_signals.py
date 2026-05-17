"""Test hypothèse : RN1 follows whale orders.

For each RN1 trade, fetch ALL trades on same market via /trades?market=<cid>,
then look at activity in the 5min window BEFORE his trade :
  - Was there a whale order ($1000+ USD cost) ?
  - Was there volume surge (sum > 5x his trade) ?
  - Was he isolated (no significant activity) ?

If most of his trades follow whales → his edge = smart money detection
                                       → replicable by monitoring same endpoint
If most are isolated                  → he has external info source

Usage : python -m analysis.rn1.investigate_whale_signals
"""
from __future__ import annotations
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADES_PATH = DATA_DIR / "trades.jsonl"
OUT_PATH = DATA_DIR / "whale_analysis.json"

TRADES_API = "https://data-api.polymarket.com/trades"
HOURS_BACK = 24                # only analyze his last 24h of trades
WHALE_USD_THRESHOLD = 1000     # trades $1000+ are "whale" orders
WINDOW_BEFORE_SEC = 300        # look 5 min before his entry
PARALLEL_WORKERS = 8
HTTP_TIMEOUT = 5


def fetch_market_trades(condition_id: str, limit: int = 500) -> list[dict]:
    """Fetch up to N most recent trades on a market."""
    try:
        r = httpx.get(f"{TRADES_API}?market={condition_id}&limit={limit}",
                      timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def analyze_rn1_trade(rn1_trade: dict, market_trades: list[dict]) -> dict:
    """For one RN1 trade, classify the activity in the 5min before it."""
    rn1_ts = int(rn1_trade.get("timestamp", 0))
    rn1_hash = rn1_trade.get("transactionHash", "")
    rn1_usd = float(rn1_trade.get("size", 0)) * float(rn1_trade.get("price", 0))
    window_start = rn1_ts - WINDOW_BEFORE_SEC

    preceding = []
    for t in market_trades:
        t_ts = int(t.get("timestamp", 0))
        t_hash = t.get("transactionHash", "")
        # Exclude RN1's own trade, only count strictly BEFORE
        if t_hash == rn1_hash:
            continue
        if t_ts >= rn1_ts or t_ts < window_start:
            continue
        t_usd = float(t.get("size", 0)) * float(t.get("price", 0))
        preceding.append({
            "delay_sec": rn1_ts - t_ts,
            "usd": t_usd,
            "side": t.get("side"),
            "wallet_name": t.get("name") or t.get("pseudonym", ""),
            "outcome": t.get("outcome"),
        })

    n_preceding = len(preceding)
    total_volume_preceding = sum(p["usd"] for p in preceding)
    largest_preceding = max((p["usd"] for p in preceding), default=0)
    whale_preceding = [p for p in preceding if p["usd"] >= WHALE_USD_THRESHOLD]
    n_whales = len(whale_preceding)
    same_side_whales = [p for p in whale_preceding if p["side"] == rn1_trade.get("side")]

    # Classify
    if n_whales >= 1 and len(same_side_whales) >= 1:
        klass = "follows_same_side_whale"
    elif n_whales >= 1:
        klass = "whales_present_diff_side"
    elif total_volume_preceding >= 5 * rn1_usd and n_preceding >= 3:
        klass = "volume_surge"
    elif n_preceding == 0:
        klass = "isolated"
    elif n_preceding <= 2:
        klass = "quiet"
    else:
        klass = "minor_activity"

    return {
        "ts": rn1_ts,
        "usd": rn1_usd,
        "side": rn1_trade.get("side"),
        "n_preceding": n_preceding,
        "total_volume_preceding_usd": round(total_volume_preceding, 2),
        "largest_preceding_usd": round(largest_preceding, 2),
        "n_whales_preceding": n_whales,
        "n_same_side_whales": len(same_side_whales),
        "classification": klass,
        "title": (rn1_trade.get("title") or "")[:60],
    }


def main() -> None:
    cutoff_ts = int(time.time()) - HOURS_BACK * 3600

    # Load RN1 trades in window
    rn1_trades = []
    with open(TRADES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if t.get("name") != "RN1":
                    continue
                if int(t.get("timestamp", 0)) < cutoff_ts:
                    continue
                rn1_trades.append(t)
            except Exception:
                pass
    print(f"[load] {len(rn1_trades)} RN1 trades in past {HOURS_BACK}h", flush=True)

    # Unique conditionIds
    cid_set = set()
    for t in rn1_trades:
        cid = t.get("conditionId")
        if cid:
            cid_set.add(cid)
    cid_list = sorted(cid_set)
    print(f"[unique] {len(cid_list)} unique markets to fetch", flush=True)

    # Parallel fetch all market trades
    print(f"[fetch] querying market-level /trades (parallel {PARALLEL_WORKERS} workers)...",
          flush=True)
    cid_to_trades: dict[str, list[dict]] = {}

    def _fetch(c):
        return c, fetch_market_trades(c, limit=500)

    started = time.time()
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        futures = {pool.submit(_fetch, c): c for c in cid_list}
        done = 0
        for fut in as_completed(futures, timeout=180):
            try:
                cid_done, trades = fut.result(timeout=10)
                cid_to_trades[cid_done] = trades
            except Exception:
                cid_to_trades[futures[fut]] = []
            done += 1
            if done % 25 == 0:
                rate = done / (time.time() - started)
                eta = (len(cid_list) - done) / rate if rate else 0
                print(f"  {done}/{len(cid_list)} fetched ({rate:.1f}/s, eta {eta:.0f}s)",
                      flush=True)
    print(f"[fetch done] {sum(1 for v in cid_to_trades.values() if v)}/{len(cid_list)} "
          f"markets with data in {time.time()-started:.0f}s", flush=True)

    # Analyze each RN1 trade
    results = []
    for t in rn1_trades:
        cid = t.get("conditionId")
        market_trades = cid_to_trades.get(cid, [])
        results.append(analyze_rn1_trade(t, market_trades))

    # Aggregate
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_class[r["classification"]].append(r)

    summary = []
    for klass, recs in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        avg_n_prec = sum(r["n_preceding"] for r in recs) / len(recs)
        avg_vol = sum(r["total_volume_preceding_usd"] for r in recs) / len(recs)
        avg_largest = sum(r["largest_preceding_usd"] for r in recs) / len(recs)
        summary.append({
            "classification": klass,
            "count": len(recs),
            "pct": round(100 * len(recs) / len(results), 1),
            "avg_n_preceding": round(avg_n_prec, 1),
            "avg_total_vol_preceding_usd": round(avg_vol, 0),
            "avg_largest_preceding_usd": round(avg_largest, 0),
        })

    OUT_PATH.write_text(json.dumps({
        "ts": int(time.time()),
        "hours_back": HOURS_BACK,
        "n_trades": len(rn1_trades),
        "n_unique_markets": len(cid_list),
        "whale_threshold_usd": WHALE_USD_THRESHOLD,
        "window_before_sec": WINDOW_BEFORE_SEC,
        "summary": summary,
        "sample_per_class": {
            klass: [
                {"side": r["side"], "usd": r["usd"],
                 "n_prec": r["n_preceding"], "vol_prec": r["total_volume_preceding_usd"],
                 "largest_prec": r["largest_preceding_usd"], "title": r["title"]}
                for r in recs[:5]
            ]
            for klass, recs in by_class.items()
        },
    }, indent=2))

    # Report
    print(f"\n=== WHALE SIGNAL ANALYSIS — {len(results)} RN1 trades over past "
          f"{HOURS_BACK}h ===\n")
    print(f"Whale threshold: ${WHALE_USD_THRESHOLD}+ trade in {WINDOW_BEFORE_SEC}s "
          f"before RN1 trade\n")
    print(f"{'classification':<28} {'count':>6} {'pct':>6} {'n_prec':>7} "
          f"{'vol_prec':>10} {'largest':>10}")
    print("-" * 75)
    for s in summary:
        print(f"{s['classification']:<28} {s['count']:>6} {s['pct']:>5.1f}% "
              f"{s['avg_n_preceding']:>7.1f} "
              f"${s['avg_total_vol_preceding_usd']:>8,.0f} "
              f"${s['avg_largest_preceding_usd']:>8,.0f}")

    total = len(results)
    follows = sum(s["count"] for s in summary
                  if s["classification"] in {"follows_same_side_whale",
                                              "whales_present_diff_side"})
    print(f"\n=== VERDICT ===")
    print(f"  Trades with whale preceding (any side)  : "
          f"{follows} ({100*follows/total:.1f}%)")
    follow_same = sum(s["count"] for s in summary
                      if s["classification"] == "follows_same_side_whale")
    print(f"  Trades following SAME-SIDE whale (60s+) : "
          f"{follow_same} ({100*follow_same/total:.1f}%)")
    isolated = sum(s["count"] for s in summary
                   if s["classification"] in {"isolated", "quiet"})
    print(f"  Trades isolated / quiet                 : "
          f"{isolated} ({100*isolated/total:.1f}%)")

    if follow_same / total >= 0.5:
        print(f"\n✅ HYPOTHESIS CONFIRMED : RN1 follows whale orders systematically")
        print(f"   → His edge = smart money detection on Polymarket")
        print(f"   → Replicable by monitoring /trades endpoint for sport markets")
    elif isolated / total >= 0.5:
        print(f"\n❌ HYPOTHESIS REJECTED : RN1 trades are mostly isolated")
        print(f"   → His edge comes from external info (Pinnacle / Betfair / news)")
        print(f"   → Need to test those sources or build own model")
    else:
        print(f"\n🟡 MIXED SIGNAL : need deeper investigation")

    print(f"\nOutput : {OUT_PATH}")


if __name__ == "__main__":
    main()

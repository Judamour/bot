"""Investigation : how do prices move between RN1's entry and now?

For each of his recent trades, compare :
  - His entry price (what he paid)
  - Current price (what the market shows now)
  - Resolution status (if closed)

Goal : classify his entry timing into 4 categories :
  - CASH SETTLE : he buys at 0.85+, market resolves to 1.00 (slow UMA arb)
  - CAUGHT TRANSITION : price moved >10% in his favor since entry (he's early)
  - LATE CONVERGENCE : price barely moved since entry (he's near settlement)
  - WRONG : price moved against him

Tells us if his edge is timing-based or info-based.

Usage : python -m analysis.rn1.investigate_timing
"""
from __future__ import annotations
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADES_PATH = DATA_DIR / "trades.jsonl"
OUT_PATH = DATA_DIR / "timing_analysis.json"

GAMMA_API = "https://gamma-api.polymarket.com/markets"
HOURS_BACK = int(24)  # only analyze his last 24h of trades (faster, more recent)


def fetch_market_current(condition_id: str) -> dict | None:
    """Return current market state from Gamma (None if not found, timeout 5s)."""
    try:
        r = httpx.get(f"{GAMMA_API}?condition_ids={condition_id}", timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except Exception:
        pass
    return None


def parse_prices(m: dict) -> list[float] | None:
    raw = m.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    try:
        return [float(p) for p in raw]
    except Exception:
        return None


def parse_outcomes(m: dict) -> list[str] | None:
    raw = m.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, list) else None


def classify_timing(entry_price: float, current_price: float | None,
                    closed: bool, entry_outcome_won: bool | None) -> str:
    """Classify how RN1's trade played out."""
    if closed and entry_outcome_won is True:
        if entry_price >= 0.85:
            return "cash_settle_win"  # entered favorite at 0.85+, won → cash arb
        if entry_price < 0.50:
            return "underdog_win"     # bet underdog, lucky win
        return "mid_win"
    if closed and entry_outcome_won is False:
        return "lost"
    if current_price is None:
        return "no_data"

    delta = current_price - entry_price
    if delta >= 0.10:
        return "caught_transition"   # price moved 10%+ in his favor
    if delta >= 0.03:
        return "rising"              # modest move in his favor
    if delta >= -0.03:
        return "flat"                # near where he entered
    if delta >= -0.10:
        return "declining"           # slight move against
    return "wrong_direction"         # bigger move against


def find_outcome_winner(market: dict, entry_outcome_name: str) -> bool | None:
    """Did the outcome RN1 bought win? Returns None if not yet resolved."""
    if not market.get("closed"):
        return None
    tokens = market.get("tokens", [])
    for tok in tokens:
        # gamma sometimes returns tokens differently — also check outcomes/outcomePrices
        if tok.get("outcome") == entry_outcome_name:
            winner = tok.get("winner")
            if winner is not None:
                return bool(winner)
    # Fallback : check outcomePrices — closed market has [1.0, 0.0] or [0.0, 1.0]
    prices = parse_prices(market)
    outcomes = parse_outcomes(market)
    if prices and outcomes and len(prices) == len(outcomes):
        for i, name in enumerate(outcomes):
            if name == entry_outcome_name and prices[i] >= 0.999:
                return True
            if name == entry_outcome_name and prices[i] <= 0.001:
                return False
    return None


def main() -> None:
    if not TRADES_PATH.exists():
        sys.exit(f"[error] {TRADES_PATH} missing")

    cutoff_ts = int(time.time()) - HOURS_BACK * 3600

    # Load RN1 trades in window
    trades = []
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
                trades.append(t)
            except Exception:
                pass
    print(f"[load] {len(trades)} RN1 trades in past {HOURS_BACK}h")

    # Group by conditionId to avoid duplicate market fetches
    cid_to_trades = defaultdict(list)
    for t in trades:
        cid = t.get("conditionId")
        if cid:
            cid_to_trades[cid].append(t)
    print(f"[unique] {len(cid_to_trades)} unique markets")

    # Fetch current state for each — parallelized via ThreadPoolExecutor for speed
    print(f"[fetch] querying current market state via Gamma (parallel)...")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cid_to_market: dict[str, dict | None] = {}
    cid_list = list(cid_to_trades.keys())

    def _fetch(c):
        return c, fetch_market_current(c)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch, c): c for c in cid_list}
        done_count = 0
        for fut in as_completed(futures, timeout=120):
            try:
                cid_done, market = fut.result(timeout=8)
                cid_to_market[cid_done] = market
            except Exception as e:
                cid_to_market[futures[fut]] = None
            done_count += 1
            if done_count % 25 == 0:
                print(f"  {done_count}/{len(cid_list)} fetched", flush=True)

    print(f"[fetch done] {sum(1 for m in cid_to_market.values() if m)}/{len(cid_list)} markets retrieved")

    results = []
    for cid, ts in cid_to_trades.items():
        market = cid_to_market.get(cid)
        if not market:
            for t in ts:
                results.append({
                    "ts": t["timestamp"], "cid": cid, "entry_price": t["price"],
                    "entry_outcome": t.get("outcome"), "current_price": None,
                    "closed": None, "won": None, "classification": "no_data",
                    "delta": None, "title": t.get("title"),
                })
            continue

        prices = parse_prices(market) or []
        outcomes = parse_outcomes(market) or []
        closed = bool(market.get("closed"))

        for t in ts:
            entry_price = float(t["price"])
            entry_outcome = t.get("outcome")
            current_price = None
            if outcomes and entry_outcome in outcomes:
                idx = outcomes.index(entry_outcome)
                if idx < len(prices):
                    current_price = prices[idx]
            entry_won = find_outcome_winner(market, entry_outcome) if closed else None
            klass = classify_timing(entry_price, current_price, closed, entry_won)
            results.append({
                "ts": t["timestamp"],
                "cid": cid,
                "entry_price": entry_price,
                "entry_outcome": entry_outcome,
                "current_price": current_price,
                "closed": closed,
                "won": entry_won,
                "classification": klass,
                "delta": (current_price - entry_price) if current_price else None,
                "title": t.get("title"),
            })

    print(f"[done] {len(results)} trade records analyzed")

    # Aggregate stats
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_class[r["classification"]].append(r)

    summary = []
    for klass, recs in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        avg_entry = sum(r["entry_price"] for r in recs) / len(recs)
        avg_delta = None
        deltas = [r["delta"] for r in recs if r["delta"] is not None]
        if deltas:
            avg_delta = sum(deltas) / len(deltas)
        summary.append({
            "classification": klass,
            "count": len(recs),
            "pct": round(100 * len(recs) / len(results), 1),
            "avg_entry_price": round(avg_entry, 3),
            "avg_price_delta": round(avg_delta, 3) if avg_delta else None,
        })

    OUT_PATH.write_text(json.dumps({
        "ts": int(time.time()),
        "hours_back": HOURS_BACK,
        "n_trades": len(trades),
        "n_unique_markets": len(cid_to_trades),
        "summary": summary,
        "sample_per_class": {
            klass: [
                {"entry": r["entry_price"], "current": r["current_price"],
                 "delta": r["delta"], "title": (r["title"] or "")[:60],
                 "outcome": r["entry_outcome"], "closed": r["closed"], "won": r["won"]}
                for r in recs[:5]
            ]
            for klass, recs in by_class.items()
        },
    }, indent=2))

    # Console report
    print(f"\n=== TIMING ANALYSIS — {len(results)} RN1 trades over past {HOURS_BACK}h ===\n")
    print(f"{'classification':<20} {'count':>6} {'pct':>6} {'avg_entry':>10} {'avg_delta':>10}")
    print("-" * 60)
    for s in summary:
        delta_str = f"{s['avg_price_delta']:+.3f}" if s['avg_price_delta'] is not None else "—"
        print(f"{s['classification']:<20} {s['count']:>6} {s['pct']:>5.1f}% "
              f"{s['avg_entry_price']:>10.3f} {delta_str:>10}")

    # Interpretation
    print("\n=== INTERPRETATION ===")
    total = len(results)
    cs = sum(s["count"] for s in summary if s["classification"] == "cash_settle_win")
    caught = sum(s["count"] for s in summary if s["classification"] == "caught_transition")
    lost = sum(s["count"] for s in summary if s["classification"] == "lost")
    no_data = sum(s["count"] for s in summary if s["classification"] == "no_data")
    underdog = sum(s["count"] for s in summary if s["classification"] == "underdog_win")
    print(f"  Cash settle wins (UMA arb)         : {cs} ({100*cs/total:.1f}%)")
    print(f"  Caught transitions (+10% delta)    : {caught} ({100*caught/total:.1f}%)")
    print(f"  Underdog wins (lucky)              : {underdog} ({100*underdog/total:.1f}%)")
    print(f"  Lost                               : {lost} ({100*lost/total:.1f}%)")
    print(f"  No data (market gone from Gamma)   : {no_data} ({100*no_data/total:.1f}%)")
    print(f"\nOutput : {OUT_PATH}")


if __name__ == "__main__":
    main()

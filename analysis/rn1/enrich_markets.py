"""For each unique conditionId in trades.jsonl, fetch market details.

Resolution status, outcomes, final prices (won token = $1.00, lost = $0.00).
Used to compute PnL on RN1's closed trades.

Usage:
    python -m analysis.rn1.enrich_markets

Output: analysis/rn1/data/markets.jsonl (one market per line)
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADES_PATH = DATA_DIR / "trades.jsonl"
OUT_PATH = DATA_DIR / "markets.jsonl"

CLOB_API = "https://clob.polymarket.com"


def fetch_market(condition_id: str, retries: int = 3) -> dict | None:
    url = f"{CLOB_API}/markets/{condition_id}"
    delay = 0.5
    for attempt in range(retries):
        try:
            r = httpx.get(url, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [warn] {condition_id[:10]}... fetch failed: {e}", file=sys.stderr)
                return None
            time.sleep(delay)
            delay *= 2
    return None


def collect_unique_condition_ids() -> set[str]:
    if not TRADES_PATH.exists():
        print(f"[error] {TRADES_PATH} missing. Run fetch_trades first.", file=sys.stderr)
        sys.exit(1)
    cids: set[str] = set()
    with open(TRADES_PATH) as f:
        for line in f:
            if line.strip():
                try:
                    cids.add(json.loads(line)["conditionId"])
                except Exception:
                    pass
    return cids


def load_already_fetched() -> set[str]:
    if not OUT_PATH.exists():
        return set()
    done: set[str] = set()
    with open(OUT_PATH) as f:
        for line in f:
            if line.strip():
                try:
                    m = json.loads(line)
                    cid = m.get("condition_id") or m.get("conditionId")
                    if cid:
                        done.add(cid)
                except Exception:
                    pass
    return done


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_cids = collect_unique_condition_ids()
    done = load_already_fetched()
    todo = sorted(all_cids - done)
    print(f"[start] {len(all_cids)} unique markets total, {len(done)} already fetched, "
          f"{len(todo)} to fetch")

    started = time.time()
    n_ok = n_missing = 0
    with open(OUT_PATH, "a") as f:
        for i, cid in enumerate(todo, 1):
            m = fetch_market(cid)
            if m:
                f.write(json.dumps(m) + "\n")
                f.flush()
                n_ok += 1
            else:
                # Write a marker so we don't refetch
                f.write(json.dumps({"condition_id": cid, "_missing": True}) + "\n")
                f.flush()
                n_missing += 1
            if i % 50 == 0:
                rate = i / (time.time() - started)
                eta = (len(todo) - i) / rate
                print(f"  {i:>5}/{len(todo)} | ok={n_ok} missing={n_missing} | "
                      f"{rate:.1f}/s | eta {eta:.0f}s")
            # Polite throttle
            time.sleep(0.05)

    elapsed = time.time() - started
    print(f"\n[done] fetched {n_ok} markets ({n_missing} missing) in {elapsed:.0f}s")
    print(f"[done] output: {OUT_PATH}")


if __name__ == "__main__":
    main()

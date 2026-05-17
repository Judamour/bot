"""Paginate all RN1 trades via Polymarket data-api.

Usage:
    python -m analysis.rn1.fetch_trades

Output: analysis/rn1/data/trades.jsonl (one trade per line)

API:
    GET data-api.polymarket.com/trades?user=<wallet>&limit=500&offset=N
    - max limit 500, offset works
    - returned newest-first
    - paginated stops when empty array returned
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import httpx

WALLET = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
API = "https://data-api.polymarket.com/trades"
PAGE_SIZE = 500
DATA_DIR = Path(__file__).resolve().parent / "data"
OUT_PATH = DATA_DIR / "trades.jsonl"


def fetch_page(offset: int, retries: int = 3) -> list[dict]:
    url = f"{API}?user={WALLET}&limit={PAGE_SIZE}&offset={offset}"
    delay = 1.0
    for attempt in range(retries):
        try:
            r = httpx.get(url, timeout=30)
            r.raise_for_status()
            return r.json() or []
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt+1}/{retries} after error: {e}", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    return []


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen_hashes: set[str] = set()
    n_total = 0
    offset = 0
    started = time.time()

    with open(OUT_PATH, "w") as f:
        while True:
            page = fetch_page(offset)
            if not page:
                print(f"\n[done] empty page at offset {offset} — stop")
                break

            new = 0
            for trade in page:
                h = trade.get("transactionHash")
                if h and h not in seen_hashes:
                    seen_hashes.add(h)
                    f.write(json.dumps(trade) + "\n")
                    new += 1
                    n_total += 1

            oldest_ts = page[-1].get("timestamp", 0)
            oldest_date = time.strftime("%Y-%m-%d %H:%M", time.gmtime(oldest_ts))
            print(f"  offset={offset:>6} | got {len(page):>3} ({new:>3} new) | "
                  f"total={n_total:>6} | oldest={oldest_date}")

            if new == 0:
                # API loop (returning same data) — defensive
                print("\n[warn] no new trades on this page — possible duplicate page, stop")
                break

            offset += PAGE_SIZE
            # Be polite to the API
            time.sleep(0.15)

    elapsed = time.time() - started
    print(f"\n[result] {n_total} unique trades saved to {OUT_PATH}")
    print(f"[result] elapsed {elapsed:.1f}s ({n_total/elapsed:.0f} trades/s)")


if __name__ == "__main__":
    main()

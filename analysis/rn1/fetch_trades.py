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


def _load_existing_hashes() -> set[str]:
    """Read existing trades.jsonl and return set of transactionHashes already saved."""
    if not OUT_PATH.exists():
        return set()
    seen: set[str] = set()
    with open(OUT_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    h = json.loads(line).get("transactionHash")
                    if h:
                        seen.add(h)
                except Exception:
                    pass
    return seen


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen_hashes = _load_existing_hashes()
    n_known = len(seen_hashes)
    n_new = 0
    offset = 0
    started = time.time()
    consecutive_empty_pages = 0

    print(f"[start] {n_known} trades already in {OUT_PATH.name}, "
          f"appending new ones")

    with open(OUT_PATH, "a") as f:
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
                    n_new += 1

            oldest_ts = page[-1].get("timestamp", 0)
            oldest_date = time.strftime("%Y-%m-%d %H:%M", time.gmtime(oldest_ts))
            print(f"  offset={offset:>6} | got {len(page):>3} ({new:>3} new) | "
                  f"total={len(seen_hashes):>6} | oldest={oldest_date}")

            if new == 0:
                # Daily mode : if 2 pages straight are all duplicates, we're
                # reading old data we already have — stop. (1-page duplicate
                # can happen at exact pagination boundary.)
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 2:
                    print("\n[done] 2 consecutive pages without new trades — stop")
                    break
            else:
                consecutive_empty_pages = 0

            offset += PAGE_SIZE
            time.sleep(0.15)

    elapsed = time.time() - started
    print(f"\n[result] {n_new} new trades appended (total {len(seen_hashes)})")
    print(f"[result] elapsed {elapsed:.1f}s")


if __name__ == "__main__":
    main()

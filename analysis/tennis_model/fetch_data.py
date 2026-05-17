"""Download Sackmann WTA match data (free, public CSVs).

Source : https://github.com/JeffSackmann/tennis_wta
File pattern : wta_matches_YYYY.csv

Usage : python -m analysis.tennis_model.fetch_data
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent / "data"
BASE_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"

# 5 years rolling — enough for current player ratings
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def fetch_year(year: int) -> bool:
    """Download wta_matches_YYYY.csv. Returns True if success."""
    url = f"{BASE_URL}/wta_matches_{year}.csv"
    out_path = DATA_DIR / f"wta_matches_{year}.csv"
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            print(f"  [skip] {year}: HTTP {r.status_code}")
            return False
        out_path.write_bytes(r.content)
        # Count rows
        n_lines = sum(1 for _ in open(out_path)) - 1  # minus header
        print(f"  [ok] {year}: {n_lines} matches, {len(r.content)/1024:.0f} KB")
        return True
    except Exception as e:
        print(f"  [error] {year}: {e}")
        return False


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[fetch] Downloading {len(YEARS)} years of WTA match data")
    ok = 0
    for year in YEARS:
        if fetch_year(year):
            ok += 1
        time.sleep(0.3)  # polite
    print(f"\n[done] {ok}/{len(YEARS)} years downloaded to {DATA_DIR}")


if __name__ == "__main__":
    main()

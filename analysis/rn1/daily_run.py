"""Daily orchestrator — fetch new trades + enrich new markets.

Designed to run via systemd timer or cron daily.
- Idempotent : ne refait pas le travail
- Court (~30-90s) : juste l'incrémental
- Sortie minimaliste pour logs systemd

Usage:
    python -m analysis.rn1.daily_run

Hebdomadaire (lundi) : refait analyze.py + envoie un digest Telegram
si TELEGRAM_BOT_TOKEN/CHAT_ID dispos.
"""
from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import fetch_trades, enrich_markets, analyze, analyze_deep, paper_bot

DATA_DIR = Path(__file__).resolve().parent / "data"


def _send_telegram_digest() -> None:
    """Send weekly summary (Monday only) via Telegram."""
    try:
        import httpx
    except ImportError:
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return

    summary_path = DATA_DIR / "pattern_summary.csv"
    trades_path = DATA_DIR / "trades.jsonl"
    if not summary_path.exists() or not trades_path.exists():
        return

    # Count trades + bucket-level edge summary
    import json
    n_trades = 0
    oldest_ts = float("inf")
    newest_ts = 0
    with open(trades_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                    ts = t.get("timestamp", 0)
                    if ts:
                        oldest_ts = min(oldest_ts, ts)
                        newest_ts = max(newest_ts, ts)
                        n_trades += 1
                except Exception:
                    pass
    days = (newest_ts - oldest_ts) / 86400 if newest_ts and oldest_ts != float("inf") else 0

    # Parse bucket summary for key buckets
    bucket_lines = []
    with open(summary_path) as f:
        header = f.readline().strip().split(",")
        idx_cat = header.index("category")
        idx_key = header.index("key")
        idx_n = header.index("n_closed_buy")
        idx_wr = header.index("win_rate")
        idx_roi = header.index("raw_roi")
        idx_edge = header.index("our_edge_per_share_with_fee")
        for line in f:
            cols = line.strip().split(",")
            if cols[idx_cat] == "bucket" and int(cols[idx_n]) >= 20:
                bucket_lines.append(
                    f"{cols[idx_key]:<9} | n={cols[idx_n]:>4} | "
                    f"wr={float(cols[idx_wr])*100:>5.1f}% | "
                    f"ROI={float(cols[idx_roi])*100:+.1f}% | "
                    f"edge=${cols[idx_edge]}"
                )

    msg = (
        f"📊 <b>RN1 weekly digest</b>\n"
        f"Dataset: {n_trades} trades sur {days:.1f}j\n\n"
        f"<pre>"
        + "\n".join(bucket_lines) +
        "</pre>"
    )
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def main() -> None:
    started = time.time()
    print(f"=== RN1 daily run @ {datetime.now(timezone.utc).isoformat()} ===")

    print("\n[step 1/3] fetch trades (incremental)")
    fetch_trades.main()

    print("\n[step 2/3] enrich markets (incremental)")
    enrich_markets.main()

    # Re-analyze every day so latest report is fresh
    print("\n[step 3/4] analyze + report")
    analyze.main()

    print("\n[step 4/5] deep analysis (8 dimensions)")
    analyze_deep.main()

    print("\n[step 5/5] paper bot — simulate edge on new RN1 signals")
    paper_bot.main()

    # Weekly digest on Monday
    if datetime.now(timezone.utc).weekday() == 0:
        print("\n[bonus] Monday — sending Telegram digest")
        _send_telegram_digest()

    elapsed = time.time() - started
    print(f"\n=== done in {elapsed:.0f}s ===")


if __name__ == "__main__":
    main()

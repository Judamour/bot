#!/usr/bin/env python3
"""Audit copy-trade fidelity vs target wallets.

What this checks:
  1. Per-wallet coverage: how many of the target's currently-open
     positions does our paper portfolio mirror?
  2. Sizing drift: for mirrored positions, is paper.size / target.size
     close to the expected ratio (capital_per_wallet / target_AUM_at_open)?
  3. Stale paper positions: positions we still hold in paper but the
     target has already closed (we may have missed a SELL).
  4. Decisions log breakdown over a lookback window (default 24h):
     executed vs skipped (by rationale).
  5. Recent target trades not reflected in our decisions log
     (truly missing copies, not just skipped).

Usage:
    python scripts/audit_copytrade.py
    python scripts/audit_copytrade.py --hours 24
    python scripts/audit_copytrade.py --telegram

Exit code:
    0 always (this is observational, not a CI gate).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from live.copytrade import data_api
from live.copytrade.targets import CAPITAL_PER_WALLET, TARGETS

LOG_DIR = Path(os.getenv("BOT_CP_LOG_DIR", REPO / "logs" / "copytrade"))
PORTFOLIO_PATH = LOG_DIR / "portfolio.json"
DECISIONS_PATH = LOG_DIR / "decisions.jsonl"

# Sizing tolerance: paper ratio within ±SIZING_TOL of expected is "OK"
SIZING_TOL = 0.30  # 30%


def _load_portfolio() -> dict:
    if not PORTFOLIO_PATH.exists():
        return {}
    try:
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: cannot read {PORTFOLIO_PATH}: {e}", file=sys.stderr)
        return {}


def _load_decisions(since_ts: int) -> list[dict]:
    """Load decisions written after `since_ts`."""
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
                if int(d.get("ts", 0)) >= since_ts:
                    out.append(d)
    except Exception as e:
        print(f"ERROR: cannot read {DECISIONS_PATH}: {e}", file=sys.stderr)
    return out


def _audit_wallet(target: dict, paper: dict) -> dict:
    """Compare a single target wallet against its paper portfolio entry."""
    wallet = target["wallet"]
    pseudo = target["pseudonym"]

    target_aum = 0.0
    target_positions: list[dict] = []
    try:
        target_aum = data_api.value(wallet)
        target_positions = data_api.positions(wallet) or []
    except Exception as e:
        return {
            "pseudonym": pseudo,
            "error": str(e),
            "target_aum": 0,
            "target_open": 0,
            "paper_open": 0,
        }

    paper_positions = paper.get("positions", []) if paper else []
    paper_keys = {(p["condition_id"], p["outcome_index"]): p for p in paper_positions}
    target_keys = {
        (p["conditionId"], int(p.get("outcomeIndex", 0))): p
        for p in target_positions
    }

    mirrored = paper_keys.keys() & target_keys.keys()
    stale_paper = paper_keys.keys() - target_keys.keys()
    # NOT all "target only" positions are missed — many are smaller than $1 paper
    # because target uses a tiny pct of AUM. We can't tell which are "missable"
    # without trade-history reconstruction. So we just count them as "target only".
    target_only = target_keys.keys() - paper_keys.keys()

    # Sizing drift for mirrored
    expected_ratio = CAPITAL_PER_WALLET / target_aum if target_aum > 0 else None
    drift_rows = []
    drift_ok = 0
    for k in mirrored:
        pp = paper_keys[k]
        tp = target_keys[k]
        ts = float(tp.get("size", 0) or 0)
        ps = float(pp.get("size", 0) or 0)
        if ts <= 0 or expected_ratio is None:
            continue
        actual_ratio = ps / ts
        rel = actual_ratio / expected_ratio if expected_ratio else 0
        is_ok = abs(rel - 1.0) < SIZING_TOL
        drift_rows.append({
            "title": tp.get("title", "?")[:40],
            "outcome": tp.get("outcome", "?"),
            "target_size": ts,
            "paper_size": ps,
            "actual_ratio": actual_ratio,
            "expected_ratio": expected_ratio,
            "rel": rel,
            "ok": is_ok,
        })
        if is_ok:
            drift_ok += 1

    return {
        "pseudonym": pseudo,
        "wallet": wallet,
        "target_aum": target_aum,
        "target_open": len(target_positions),
        "paper_open": len(paper_positions),
        "mirrored": len(mirrored),
        "target_only": len(target_only),
        "stale_paper": len(stale_paper),
        "stale_paper_titles": [paper_keys[k].get("market_title", "?")[:50] for k in list(stale_paper)[:5]],
        "expected_ratio": expected_ratio,
        "drift_ok": drift_ok,
        "drift_total": len(mirrored),
        "drift_rows": drift_rows[:5],
        "cash_usd": float(paper.get("cash_usd", 0)) if paper else 0,
        "realized": float(paper.get("realized_pnl_usd", 0)) if paper else 0,
    }


def _audit_decisions(hours: int) -> dict:
    since = int(time.time()) - hours * 3600
    decisions = _load_decisions(since)
    if not decisions:
        return {"total": 0, "executed": 0, "skipped": 0, "by_rationale": {}}

    executed = sum(1 for d in decisions if d.get("action") == "executed")
    skipped = sum(1 for d in decisions if d.get("action") == "skipped")
    by_rat = Counter()
    for d in decisions:
        if d.get("action") == "skipped":
            r = (d.get("rationale") or "?").split(" ")[0]  # strip details
            by_rat[r] += 1

    # Trades by wallet
    by_wallet = Counter(d.get("wallet", "?") for d in decisions)

    return {
        "total": len(decisions),
        "executed": executed,
        "skipped": skipped,
        "by_rationale": dict(by_rat.most_common()),
        "by_wallet": dict(by_wallet),
    }


def _print_text_report(audits: list[dict], decisions_summary: dict, hours: int) -> str:
    """Build a human-readable text report."""
    lines = []
    p = lines.append
    width = 72
    bar = "═" * width

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    p(bar)
    p(f"COPY TRADE AUDIT — {now}")
    p(bar)
    p("")
    p("PORTFOLIO COMPARISON (paper vs target, current open positions)")
    p("")

    overall_mirrored = 0
    overall_target_open = 0
    overall_stale = 0

    for a in audits:
        if "error" in a:
            p(f"  {a['pseudonym']}: ERROR — {a['error']}")
            continue
        wallet_short = a["wallet"][:10] + "…" + a["wallet"][-4:]
        cov_pct = (a["mirrored"] / a["target_open"] * 100) if a["target_open"] else 0
        overall_mirrored += a["mirrored"]
        overall_target_open += a["target_open"]
        overall_stale += a["stale_paper"]
        p(f"  {a['pseudonym']} ({wallet_short})")
        p(f"    Target AUM:        ${a['target_aum']:>12,.0f}")
        p(f"    Target open pos:   {a['target_open']:>6}")
        p(f"    Paper open pos:    {a['paper_open']:>6}")
        p(f"    Mirrored:          {a['mirrored']:>6} ({cov_pct:.1f}%)")
        p(f"    Stale paper:       {a['stale_paper']:>6}   (target closed, we still hold)")
        if a["stale_paper_titles"]:
            for t in a["stale_paper_titles"]:
                p(f"        • {t}")
        if a["drift_total"]:
            drift_pct = a["drift_ok"] / a["drift_total"] * 100
            p(f"    Sizing in tolerance: {a['drift_ok']}/{a['drift_total']} ({drift_pct:.0f}%)")
            for r in a["drift_rows"]:
                tag = "✓" if r["ok"] else "✗"
                p(f"        {tag} {r['title']:<42} target={r['target_size']:.1f}sh  paper={r['paper_size']:.2f}sh  rel={r['rel']:.2f}x")
        else:
            p(f"    Sizing:            no mirrored positions to check")
        p(f"    Paper cash:        ${a['cash_usd']:>12,.2f}    realized=${a['realized']:>+8.2f}")
        p("")

    p(f"  TOTAL:  mirrored {overall_mirrored}/{overall_target_open}"
      f"  ({(overall_mirrored/overall_target_open*100 if overall_target_open else 0):.1f}%)"
      f"  ·  stale paper {overall_stale}")
    p("")
    p(bar)
    p(f"DECISIONS LOG — last {hours}h")
    p("")
    p(f"  Total recorded:    {decisions_summary['total']}")
    p(f"  Executed:          {decisions_summary['executed']}")
    p(f"  Skipped:           {decisions_summary['skipped']}")
    if decisions_summary["by_rationale"]:
        p("  Skipped breakdown:")
        for k, v in decisions_summary["by_rationale"].items():
            p(f"    {k:<35} {v:>4}")
    if decisions_summary.get("by_wallet"):
        p("  By wallet:")
        for k, v in decisions_summary["by_wallet"].items():
            p(f"    {k:<12} {v:>4}")
    p("")
    p(bar)
    p("VERDICT")
    p("")
    if overall_target_open == 0:
        p("  → No target positions to evaluate. Wallets may have cashed out.")
    else:
        cov = overall_mirrored / overall_target_open * 100
        if cov < 5:
            p("  ⚠  Coverage very low (<5%). Expected when target trade sizes are")
            p("      small relative to our capital_per_wallet ($333). The bot will")
            p("      only mirror trades where target uses ≳0.3% of their AUM.")
            p("      Increase BOT_CP_CAPITAL_USD to mirror more.")
        elif cov < 30:
            p("  ●  Moderate coverage. Bot is selective on high-conviction signals.")
        else:
            p(f"  ✓  Coverage healthy: {cov:.0f}% of target positions mirrored.")
    if overall_stale > 0:
        p(f"  ⚠  {overall_stale} stale paper position(s) — target closed but our")
        p("      bot still shows them as open. May indicate a missed SELL.")
    if decisions_summary["skipped"] > 0 and decisions_summary["total"] > 0:
        skip_rate = decisions_summary["skipped"] / decisions_summary["total"] * 100
        p(f"  ●  Skip rate: {skip_rate:.0f}% (mostly paper_size_below_threshold expected)")
    p("")
    p(bar)
    return "\n".join(lines)


def _telegram_digest(audits: list[dict], decisions_summary: dict, hours: int) -> str:
    """Compact Telegram-friendly digest."""
    lines = []
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines.append(f"📊 *CopyTrade audit* — {now} (last {hours}h)")
    lines.append("")
    overall_target = sum(a.get("target_open", 0) for a in audits if "error" not in a)
    overall_mirrored = sum(a.get("mirrored", 0) for a in audits if "error" not in a)
    overall_stale = sum(a.get("stale_paper", 0) for a in audits if "error" not in a)
    cov = (overall_mirrored / overall_target * 100) if overall_target else 0

    lines.append(f"Coverage: *{overall_mirrored}/{overall_target}* ({cov:.1f}%)")
    if overall_stale > 0:
        lines.append(f"⚠ Stale paper positions: *{overall_stale}*")
    lines.append(f"Decisions {hours}h: {decisions_summary['executed']} executed · {decisions_summary['skipped']} skipped")
    lines.append("")
    for a in audits:
        if "error" in a:
            lines.append(f"  {a['pseudonym']}: ERROR")
            continue
        cov_pct = (a["mirrored"] / a["target_open"] * 100) if a["target_open"] else 0
        lines.append(f"  *{a['pseudonym']}* — mirror {a['mirrored']}/{a['target_open']} ({cov_pct:.0f}%) · stale {a['stale_paper']} · cash ${a['cash_usd']:.0f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit copy-trade fidelity vs target wallets.")
    parser.add_argument("--hours", type=int, default=24, help="Decisions lookback window (default 24)")
    parser.add_argument("--telegram", action="store_true", help="Also send a Telegram digest")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of text")
    args = parser.parse_args()

    paper_portfolio = _load_portfolio()
    audits = [_audit_wallet(t, paper_portfolio.get(t["pseudonym"], {})) for t in TARGETS]
    decisions_summary = _audit_decisions(args.hours)

    if args.json:
        print(json.dumps({"audits": audits, "decisions": decisions_summary}, indent=2, default=str))
        return 0

    text = _print_text_report(audits, decisions_summary, args.hours)
    print(text)

    if args.telegram:
        try:
            from live import notifier
            digest = _telegram_digest(audits, decisions_summary, args.hours)
            notifier.notify(digest)
            print("\n[telegram digest sent]")
        except Exception as e:
            print(f"\n[telegram failed: {e}]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

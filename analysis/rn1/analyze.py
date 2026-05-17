"""Classify RN1 trades by pattern and compute edge/win-rate.

Reads:
    analysis/rn1/data/trades.jsonl    (3500 trades from fetch_trades.py)
    analysis/rn1/data/markets.jsonl   (market metadata from enrich_markets.py)

Outputs:
    analysis/rn1/data/trades_enriched.csv   (one row per trade with resolution + PnL)
    analysis/rn1/data/pattern_summary.csv   (aggregated stats per pattern)
    docs/rn1_strategy_reverse_engineered.md (human-readable verdict)

Classification rules:
    Price bucket:
        - penny       : price < 0.10
        - mid_low     : 0.10 <= price < 0.50
        - mid_high    : 0.50 <= price < 0.85
        - favorite    : 0.85 <= price < 0.95
        - lock        : 0.95 <= price

    Direction:
        - BUY = bet for the outcome at entry price
        - SELL = exit existing position (closing trade)

    Resolved outcome (if market is closed):
        - won  : token he bought has winner=True
        - lost : token has winner=False (won by other side)

PnL per share:
    BUY won  → +1.00 - price
    BUY lost → -price
    SELL: only see proceeds at exit, not original entry → skip for now

Edge = average PnL/share across all closed trades in pattern.
Apply 2% taker fee to model OUR exposure (RN1 is likely a maker, gets rebates).
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADES_PATH = DATA_DIR / "trades.jsonl"
MARKETS_PATH = DATA_DIR / "markets.jsonl"
OUT_ENRICHED = DATA_DIR / "trades_enriched.csv"
OUT_SUMMARY = DATA_DIR / "pattern_summary.csv"
OUT_JSON = DATA_DIR / "summary.json"  # consumed by dashboard /api/rn1
OUT_REPORT = Path(__file__).resolve().parent.parent.parent / "docs" / "rn1_strategy_reverse_engineered.md"

TAKER_FEE = 0.02  # 2% — what WE would pay


# ─── load & index markets ────────────────────────────────────────────────────

def load_markets() -> dict[str, dict]:
    """Return {condition_id: market_dict}."""
    if not MARKETS_PATH.exists():
        sys.exit(f"[error] {MARKETS_PATH} missing. Run enrich_markets first.")
    out: dict[str, dict] = {}
    with open(MARKETS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                cid = m.get("condition_id") or m.get("conditionId")
                if cid:
                    out[cid] = m
            except Exception:
                pass
    return out


def winning_token(market: dict) -> str | None:
    """Return token_id of the winning outcome, or None if market not resolved."""
    if market.get("_missing"):
        return None
    if not market.get("closed"):
        return None
    for tok in market.get("tokens", []):
        if tok.get("winner"):
            return str(tok.get("token_id"))
    return None


def market_tags(market: dict) -> list[str]:
    return [t for t in (market.get("tags") or []) if isinstance(t, str)]


def sport_category(tags: list[str]) -> str:
    """Pick the highest-priority sport tag, or 'Other'."""
    priority = ["Soccer", "Basketball", "Baseball", "MMA", "Tennis", "Hockey", "Football"]
    for p in priority:
        if p in tags:
            return p
    if "Sports" in tags:
        return "Sports-other"
    return "Non-sport"


# ─── classification ──────────────────────────────────────────────────────────

def price_bucket(price: float) -> str:
    if price < 0.10:
        return "penny"
    if price < 0.50:
        return "mid_low"
    if price < 0.85:
        return "mid_high"
    if price < 0.95:
        return "favorite"
    return "lock"


def trade_pnl_per_share(trade: dict, market: dict | None) -> tuple[str, float | None]:
    """Return (status, pnl_per_share).

    Status:
        - 'open'    : market not resolved
        - 'no_data' : market missing
        - 'won'     : he bought, his token won
        - 'lost'    : he bought, his token lost
        - 'sell'    : SELL trade, can't compute realized without entry side

    PnL per share (in USD):
        - won  : 1.0 - entry_price
        - lost : -entry_price
        - other: None
    """
    if not market or market.get("_missing"):
        return "no_data", None
    if not market.get("closed"):
        return "open", None
    if trade.get("side") != "BUY":
        return "sell", None

    win_tok = winning_token(market)
    if not win_tok:
        return "no_data", None

    asset = str(trade.get("asset"))
    price = float(trade.get("price", 0))
    if asset == win_tok:
        return "won", 1.0 - price
    return "lost", -price


# ─── correlation: multi-market on same event ─────────────────────────────────

def detect_correlated_trades(trades: list[dict]) -> set[str]:
    """A trade is 'correlated' if RN1 placed >= 3 distinct markets on the same
    event_slug within 60 minutes. Returns set of transactionHash strings.
    """
    by_event: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        ev = t.get("eventSlug")
        if ev:
            by_event[ev].append(t)

    correlated: set[str] = set()
    for ev, ts in by_event.items():
        ts.sort(key=lambda x: x.get("timestamp", 0))
        # Sliding window: ≥3 unique conditionIds in 60min
        for i, t in enumerate(ts):
            window = [tt for tt in ts if abs(tt["timestamp"] - t["timestamp"]) <= 3600]
            unique_markets = len({tt["conditionId"] for tt in window})
            if unique_markets >= 3:
                for tt in window:
                    correlated.add(tt.get("transactionHash", ""))
    correlated.discard("")
    return correlated


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TRADES_PATH.exists():
        sys.exit(f"[error] {TRADES_PATH} missing. Run fetch_trades first.")

    trades: list[dict] = []
    with open(TRADES_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass

    markets = load_markets()
    print(f"[load] {len(trades)} trades, {len(markets)} markets")

    correlated_hashes = detect_correlated_trades(trades)
    print(f"[corr] {len(correlated_hashes)} trades part of correlated multi-market plays")

    # Build enriched table + summary stats
    enriched: list[dict] = []
    for t in trades:
        cid = t.get("conditionId")
        m = markets.get(cid)
        status, pnl_share = trade_pnl_per_share(t, m)
        bucket = price_bucket(float(t.get("price", 0)))
        sport = sport_category(market_tags(m or {}))
        h = t.get("transactionHash", "")
        is_corr = h in correlated_hashes
        enriched.append({
            "timestamp": t.get("timestamp"),
            "side": t.get("side"),
            "title": (t.get("title") or "")[:60],
            "outcome": t.get("outcome"),
            "price": float(t.get("price", 0)),
            "size": float(t.get("size", 0)),
            "usd_cost": float(t.get("price", 0)) * float(t.get("size", 0)),
            "bucket": bucket,
            "sport": sport,
            "correlated": is_corr,
            "status": status,
            "pnl_per_share": pnl_share if pnl_share is not None else "",
            "pnl_total": (pnl_share * float(t.get("size", 0))) if pnl_share is not None else "",
        })

    # Write enriched CSV
    if enriched:
        cols = list(enriched[0].keys())
        with open(OUT_ENRICHED, "w") as f:
            f.write(",".join(cols) + "\n")
            for row in enriched:
                f.write(",".join(
                    str(row[c]).replace(",", " ") if row[c] is not None else ""
                    for c in cols
                ) + "\n")
        print(f"[write] {OUT_ENRICHED} ({len(enriched)} rows)")

    # Aggregate per (bucket, sport)
    summaries = compute_summaries(enriched)
    write_summary_csv(summaries)
    write_report(trades, enriched, summaries, correlated_hashes)
    write_summary_json(trades, enriched, summaries, correlated_hashes)


def write_summary_json(trades: list[dict], enriched: list[dict],
                       summaries: list[dict], correlated_hashes: set[str]) -> None:
    """Structured snapshot for dashboard /api/rn1 consumption."""
    import time
    n_trades = len(trades)
    n_resolved = sum(1 for e in enriched if e["status"] in ("won", "lost"))
    n_open = sum(1 for e in enriched if e["status"] == "open")

    oldest_ts = min((t["timestamp"] for t in trades if t.get("timestamp")), default=0)
    newest_ts = max((t["timestamp"] for t in trades if t.get("timestamp")), default=0)

    bucket_rows = [s for s in summaries if s["category"] == "bucket"]
    bucket_rows.sort(key=lambda s: ["penny", "mid_low", "mid_high", "favorite", "lock"].index(s["key"])
                     if s["key"] in ["penny", "mid_low", "mid_high", "favorite", "lock"] else 99)
    sport_rows = sorted([s for s in summaries if s["category"] == "sport"],
                        key=lambda s: -s["n_closed_buy"])
    corr_rows = [s for s in summaries if s["category"] == "correlated_buys"]

    # Verdict: pick best bucket by NET ROI (raw_roi - our 2% fee).
    # ROI reflects $ we'd actually make, unlike per-share edge which is unweighted.
    def _net_roi(s):
        return (s.get("raw_roi") or 0) - 0.02

    most_profitable_bucket = max(
        (s for s in bucket_rows if s["n_closed_buy"] >= 20),
        key=_net_roi,
        default=None,
    )
    best_net_roi = _net_roi(most_profitable_bucket) if most_profitable_bucket else -1
    if best_net_roi > 0.05:
        verdict_status = "go"
    elif best_net_roi > 0:
        verdict_status = "marginal"
    else:
        verdict_status = "stop"

    snapshot = {
        "ts": int(time.time()),
        "dataset": {
            "n_trades": n_trades,
            "n_resolved": n_resolved,
            "n_open": n_open,
            "n_buy": sum(1 for e in enriched if e["side"] == "BUY"),
            "n_sell": sum(1 for e in enriched if e["side"] == "SELL"),
            "n_correlated": len(correlated_hashes),
            "oldest_ts": oldest_ts,
            "newest_ts": newest_ts,
            "days_span": round((newest_ts - oldest_ts) / 86400, 2) if oldest_ts else 0,
        },
        "verdict": {
            "status": verdict_status,
            "best_bucket": most_profitable_bucket["key"] if most_profitable_bucket else None,
            "best_bucket_roi_net_pct": round(
                ((most_profitable_bucket.get("raw_roi") or 0) - 0.02) * 100, 2
            ) if most_profitable_bucket else None,
            "best_bucket_win_rate_pct": round(
                most_profitable_bucket["win_rate"] * 100, 2
            ) if most_profitable_bucket else None,
        },
        "buckets": bucket_rows,
        "sports": sport_rows,
        "correlation": corr_rows,
    }

    OUT_JSON.write_text(json.dumps(snapshot, indent=2))
    print(f"[write] {OUT_JSON}")


def compute_summaries(enriched: list[dict]) -> list[dict]:
    """Aggregate stats grouped by price_bucket only (clearest pattern signal)."""
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    by_sport: dict[str, list[dict]] = defaultdict(list)
    by_corr: dict[bool, list[dict]] = defaultdict(list)

    for e in enriched:
        by_bucket[e["bucket"]].append(e)
        by_sport[e["sport"]].append(e)
        if e["side"] == "BUY":
            by_corr[e["correlated"]].append(e)

    rows: list[dict] = []
    for label, groups in (("bucket", by_bucket), ("sport", by_sport),
                          ("correlated_buys", by_corr)):
        for k, group in groups.items():
            row = aggregate(label, str(k), group)
            rows.append(row)
    return rows


def aggregate(category: str, key: str, group: list[dict]) -> dict:
    n_total = len(group)
    n_buy = sum(1 for e in group if e["side"] == "BUY")
    n_sell = n_total - n_buy
    # Only count closed BUYs for win rate / edge
    closed_buys = [e for e in group if e["side"] == "BUY" and e["status"] in ("won", "lost")]
    n_won = sum(1 for e in closed_buys if e["status"] == "won")
    n_lost = sum(1 for e in closed_buys if e["status"] == "lost")
    win_rate = (n_won / len(closed_buys)) if closed_buys else 0.0

    pnl_per_share = [float(e["pnl_per_share"]) for e in closed_buys
                     if e["pnl_per_share"] != ""]
    avg_edge_per_share = (sum(pnl_per_share) / len(pnl_per_share)) if pnl_per_share else 0.0

    pnl_total_raw = sum(float(e["pnl_total"]) for e in closed_buys
                        if e["pnl_total"] != "")
    usd_cost_total = sum(e["usd_cost"] for e in closed_buys)
    # ROI based on capital deployed (cost basis)
    raw_roi = (pnl_total_raw / usd_cost_total) if usd_cost_total else 0.0

    # OUR scenario: every BUY costs 2% extra in taker fee
    our_pnl_per_share = avg_edge_per_share - (
        sum(float(e["price"]) for e in closed_buys) / len(closed_buys) * TAKER_FEE
        if closed_buys else 0
    )

    return {
        "category": category,
        "key": key,
        "n_total": n_total,
        "n_buy": n_buy,
        "n_sell": n_sell,
        "n_closed_buy": len(closed_buys),
        "n_won": n_won,
        "n_lost": n_lost,
        "win_rate": round(win_rate, 4),
        "avg_edge_per_share_raw": round(avg_edge_per_share, 5),
        "pnl_total_raw_usd": round(pnl_total_raw, 2),
        "usd_cost_total": round(usd_cost_total, 2),
        "raw_roi": round(raw_roi, 4),
        "our_edge_per_share_with_fee": round(our_pnl_per_share, 5),
    }


def write_summary_csv(rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    with open(OUT_SUMMARY, "w") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r[c]) for c in cols) + "\n")
    print(f"[write] {OUT_SUMMARY} ({len(rows)} rows)")


def write_report(trades: list[dict], enriched: list[dict],
                 summaries: list[dict], correlated_hashes: set[str]) -> None:
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)

    n_trades = len(trades)
    n_resolved = sum(1 for e in enriched if e["status"] in ("won", "lost"))
    n_open = sum(1 for e in enriched if e["status"] == "open")
    n_buy = sum(1 for e in enriched if e["side"] == "BUY")
    n_sell = sum(1 for e in enriched if e["side"] == "SELL")

    if trades:
        from datetime import datetime, timezone
        oldest = datetime.fromtimestamp(min(t["timestamp"] for t in trades), tz=timezone.utc)
        newest = datetime.fromtimestamp(max(t["timestamp"] for t in trades), tz=timezone.utc)
        days = (newest - oldest).total_seconds() / 86400
    else:
        oldest = newest = None
        days = 0

    bucket_rows = [s for s in summaries if s["category"] == "bucket"]
    sport_rows = [s for s in summaries if s["category"] == "sport"]
    corr_rows = [s for s in summaries if s["category"] == "correlated_buys"]

    bucket_rows.sort(key=lambda s: ["penny", "mid_low", "mid_high", "favorite", "lock"].index(s["key"])
                     if s["key"] in ["penny", "mid_low", "mid_high", "favorite", "lock"] else 99)

    # Verdict
    most_profitable_bucket = max(
        (s for s in bucket_rows if s["n_closed_buy"] >= 20),
        key=lambda s: s["our_edge_per_share_with_fee"],
        default=None,
    )

    md = []
    md.append("# RN1 strategy — reverse engineering\n")
    md.append(f"_Auto-generated by `analysis/rn1/analyze.py`._\n")
    md.append(f"Source: {n_trades} trades de `0x2005d16a…875ea` via Polymarket data-api.\n")
    md.append(f"Période: **{oldest.strftime('%Y-%m-%d') if oldest else '?'}** → "
              f"**{newest.strftime('%Y-%m-%d') if newest else '?'}** "
              f"({days:.1f} jours)\n\n")

    md.append("## Volume\n")
    md.append(f"- Total trades: **{n_trades}**\n")
    md.append(f"- BUY: {n_buy} ({n_buy/n_trades*100:.1f}%)\n")
    md.append(f"- SELL: {n_sell} ({n_sell/n_trades*100:.1f}%)\n")
    md.append(f"- Marchés résolus dans la période: **{n_resolved}** (peut calc PnL)\n")
    md.append(f"- Marchés encore ouverts: {n_open}\n")
    md.append(f"- Trades part of correlated multi-market plays: **{len(correlated_hashes)}** "
              f"({len(correlated_hashes)/n_trades*100:.1f}%)\n\n")

    md.append("## Edge par bucket de prix d'entry (BUYs résolus uniquement)\n\n")
    md.append("| Bucket | N closed | Win rate | Edge/share brut | Edge/share avec notre fee 2% | Cost basis | ROI brut |\n")
    md.append("|---|---|---|---|---|---|---|\n")
    for s in bucket_rows:
        if s["n_closed_buy"] > 0:
            edge_emoji = "✅" if s["our_edge_per_share_with_fee"] > 0 else "❌"
            md.append(
                f"| {s['key']:<10} | {s['n_closed_buy']:>4} | "
                f"{s['win_rate']*100:>5.1f}% | "
                f"${s['avg_edge_per_share_raw']:+.4f} | "
                f"${s['our_edge_per_share_with_fee']:+.4f} {edge_emoji} | "
                f"${s['usd_cost_total']:>9,.0f} | "
                f"{s['raw_roi']*100:+.1f}% |\n"
            )

    md.append("\n## Edge par sport (BUYs résolus)\n\n")
    md.append("| Sport | N closed | Win rate | Edge/share brut | Edge avec fee | ROI |\n")
    md.append("|---|---|---|---|---|---|\n")
    sport_rows.sort(key=lambda s: -s["n_closed_buy"])
    for s in sport_rows:
        if s["n_closed_buy"] > 0:
            edge_emoji = "✅" if s["our_edge_per_share_with_fee"] > 0 else "❌"
            md.append(
                f"| {s['key']:<15} | {s['n_closed_buy']:>4} | "
                f"{s['win_rate']*100:>5.1f}% | "
                f"${s['avg_edge_per_share_raw']:+.4f} | "
                f"${s['our_edge_per_share_with_fee']:+.4f} {edge_emoji} | "
                f"{s['raw_roi']*100:+.1f}% |\n"
            )

    md.append("\n## Effet de la stratégie corrélée multi-marchés\n\n")
    md.append("| Setup | N closed | Win rate | Edge/share brut | Edge avec fee | ROI |\n")
    md.append("|---|---|---|---|---|---|\n")
    for s in corr_rows:
        if s["n_closed_buy"] > 0:
            label = "Multi-marché corrélé" if s["key"] == "True" else "Standalone"
            edge_emoji = "✅" if s["our_edge_per_share_with_fee"] > 0 else "❌"
            md.append(
                f"| {label:<23} | {s['n_closed_buy']:>4} | "
                f"{s['win_rate']*100:>5.1f}% | "
                f"${s['avg_edge_per_share_raw']:+.4f} | "
                f"${s['our_edge_per_share_with_fee']:+.4f} {edge_emoji} | "
                f"{s['raw_roi']*100:+.1f}% |\n"
            )

    md.append("\n## Verdict\n\n")
    if most_profitable_bucket and most_profitable_bucket["our_edge_per_share_with_fee"] > 0:
        b = most_profitable_bucket
        md.append(
            f"✅ **Sous-pattern réplicable identifié** : bucket `{b['key']}`\n"
            f"- {b['n_closed_buy']} trades résolus, win rate {b['win_rate']*100:.1f}%\n"
            f"- Edge net après 2% taker fee: **${b['our_edge_per_share_with_fee']:+.4f}/share**\n"
            f"- Sur {b['n_closed_buy']} trades × ~$1 cost moyen, profit estimé: "
            f"${b['our_edge_per_share_with_fee'] * b['n_closed_buy']:.2f}\n\n"
            f"**Recommandation** : explorer Phase 2 (build scanner autonome) "
            f"focalisé sur ce bucket.\n"
        )
    else:
        md.append(
            "❌ **Aucun sous-pattern n'est +EV après application de notre 2% taker fee.**\n\n"
            "L'edge de RN1 dépend probablement de :\n"
            "- Fee rebate (maker side) — il est payé pour fournir liquidité\n"
            "- Latence sub-seconde (firme MM partenaire)\n"
            "- Volume (>10K trades/mois pour amortir frictions)\n\n"
            "**Recommandation** : projet RN1-bot abandonné. Garder copytrade surfandturf.\n"
        )

    md.append("\n## Notes méthodologie\n")
    md.append("- Données limitées aux 3500 trades les plus récents (Polymarket data-api cap).\n")
    md.append("- Win rate calculé uniquement sur BUYs où le marché est résolu (≠ open positions).\n")
    md.append("- Edge brut = (1.00 − price) pour wins, (−price) pour losses.\n")
    md.append("- Notre edge net = edge brut − 2% × prix d'entry moyen (notre taker fee).\n")
    md.append("- Stratégie corrélée = ≥3 marchés distincts sur même eventSlug en <60min.\n")

    OUT_REPORT.write_text("".join(md))
    print(f"[write] {OUT_REPORT}")


if __name__ == "__main__":
    main()

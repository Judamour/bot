"""Deep dive analyzer — 8 dimensions au-delà de analyze.py.

Lit trades_enriched.csv (généré par analyze.py) et extrait :
1. Market type (Winner / O/U / Spread / BTTS / Draw)
2. Timing entry (hours before resolution)
3. Sizing pattern (cost bucket vs win rate)
4. Multi-leg pack outcome (correlated plays : all win, all lose, mixed)
5. Outcome side (Yes vs No vs named outcome)
6. Streak effect (post-win vs post-loss)
7. League granularity (J-League vs Premier League vs MLB...)
8. Hour of day

Output:
    analysis/rn1/data/deep_analysis.json (consumed by dashboard)
    appended sections in docs/rn1_strategy_reverse_engineered.md
"""
from __future__ import annotations
import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADES_PATH = DATA_DIR / "trades.jsonl"
MARKETS_PATH = DATA_DIR / "markets.jsonl"
ENRICHED_PATH = DATA_DIR / "trades_enriched.csv"
OUT_JSON = DATA_DIR / "deep_analysis.json"
OUT_REPORT = Path(__file__).resolve().parent.parent.parent / "docs" / "rn1_strategy_reverse_engineered.md"

TAKER_FEE = 0.02


# ─── classification helpers ──────────────────────────────────────────────────

def classify_market_type(title: str, outcome: str) -> str:
    """Identify Polymarket market type from title pattern."""
    t = (title or "").lower()
    o = (outcome or "").lower()
    if "o/u" in t or "over/under" in t:
        return "over_under"
    if t.startswith("spread:"):
        return "spread"
    if "both teams to score" in t or "btts" in t:
        return "btts"
    if "end in a draw" in t or "draw?" in t.lower():
        return "draw"
    if "will " in t and " win " in t:
        return "winner_yes_no"
    if " vs. " in t or " vs " in t:
        return "match_winner"
    return "other"


def parse_league(market_slug: str | None, title: str | None, tags_str: str | None) -> str:
    """Pick a league/competition label from slug + tags."""
    s = (market_slug or "").lower()
    t = (title or "").lower()
    tag_set = set((tags_str or "").lower().split("|"))

    league_patterns = [
        ("J1", ["j1100", " j1 ", "j-league div 1"]),
        ("J2", ["j2200", " j2 ", "j-league div 2"]),
        ("J3", ["j3300", " j3 ", "j-league div 3"]),
        ("MLB", ["mlb", "yankees", "mets", "dodgers", "rangers", "astros"]),
        ("NBA", ["nba", "pistons", "celtics", "lakers", "warriors"]),
        ("Premier League", ["epl", "premier league", "arsenal", "liverpool"]),
        ("La Liga", ["laliga", "barcelona", "real madrid"]),
        ("Serie A", ["seriea", "juventus", "inter"]),
        ("Tennis ATP", ["atp", "masters"]),
        ("Tennis WTA", ["wta", "internazionali"]),
        ("Saudi Pro", ["saudi", "fateh", "najmah"]),
        ("Brasil", ["palmeiras", "cruzeiro", "flamengo"]),
        ("Eliteserien", ["valerenga", "sarpsborg"]),
        ("Norway", ["norway", "norge"]),
    ]
    for label, patterns in league_patterns:
        for p in patterns:
            if p in s or p in t:
                return label
    return "Other"


def classify_size_bucket(usd_cost: float) -> str:
    if usd_cost < 1:
        return "micro_<1"
    if usd_cost < 10:
        return "small_1-10"
    if usd_cost < 100:
        return "mid_10-100"
    if usd_cost < 1000:
        return "large_100-1k"
    if usd_cost < 10000:
        return "huge_1k-10k"
    return "whale_>10k"


def hour_of_day(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    h = dt.hour
    if 0 <= h < 6:
        return "00-06_UTC"
    if 6 <= h < 12:
        return "06-12_UTC"
    if 12 <= h < 18:
        return "12-18_UTC"
    return "18-24_UTC"


# ─── pattern aggregator ──────────────────────────────────────────────────────

def aggregate_dimension(rows: list[dict], dim_fn) -> list[dict]:
    """Group rows by dim_fn(row) → key, compute stats per group."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        try:
            k = dim_fn(r)
        except Exception:
            continue
        groups[k].append(r)

    out = []
    for k, group in groups.items():
        closed = [r for r in group if r["status"] in ("won", "lost")]
        n_closed = len(closed)
        if n_closed == 0:
            continue
        n_won = sum(1 for r in closed if r["status"] == "won")
        pnl_share_list = [float(r["pnl_per_share"]) for r in closed
                          if r["pnl_per_share"] != ""]
        avg_edge = sum(pnl_share_list) / len(pnl_share_list) if pnl_share_list else 0
        pnl_total = sum(float(r["pnl_total"]) for r in closed
                        if r["pnl_total"] != "")
        usd_cost = sum(float(r["usd_cost"]) for r in closed)
        raw_roi = pnl_total / usd_cost if usd_cost else 0
        avg_price = sum(float(r["price"]) for r in closed) / n_closed if closed else 0
        net_edge = avg_edge - TAKER_FEE * avg_price
        net_roi = raw_roi - TAKER_FEE

        out.append({
            "key": k,
            "n_closed": n_closed,
            "win_rate": round(n_won / n_closed, 4),
            "avg_edge_per_share": round(avg_edge, 5),
            "net_edge_per_share": round(net_edge, 5),
            "raw_roi": round(raw_roi, 4),
            "net_roi": round(net_roi, 4),
            "usd_cost_total": round(usd_cost, 2),
            "pnl_total": round(pnl_total, 2),
        })
    out.sort(key=lambda r: -r["n_closed"])
    return out


# ─── multi-leg pack analysis ─────────────────────────────────────────────────

def analyze_multi_leg_packs(enriched_rows: list[dict], trades: list[dict]) -> dict:
    """For trades flagged correlated=True, group by eventSlug and 60min window.
    Determine if the WHOLE PACK won, lost, or was mixed.
    """
    # Map enriched row → trade (need eventSlug)
    by_hash: dict[str, dict] = {}
    for t in trades:
        h = t.get("transactionHash")
        if h:
            by_hash[h] = t

    correlated_rows = [r for r in enriched_rows if r.get("correlated") == "True"]
    # Re-attach eventSlug
    for r in correlated_rows:
        # row index not preserved; use timestamp + title as fuzzy key
        # Actually we need transactionHash — wasn't in enriched CSV. Re-build via trades.
        pass

    # Rebuild from trades directly using same correlation logic
    by_event: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t.get("side") != "BUY":
            continue
        ev = t.get("eventSlug")
        if ev:
            by_event[ev].append(t)

    pack_outcomes = {"all_win": 0, "all_lose": 0, "mixed": 0, "open_or_partial": 0}
    pack_count = 0
    pack_pnl = 0.0
    pack_cost = 0.0

    # Build hash → (status, pnl_total) map from enriched
    h_to_status: dict[str, tuple[str, float, float]] = {}
    # enriched doesn't have hash — use timestamp+title as proxy key
    proxy_to_status: dict[tuple, tuple[str, float, float]] = {}
    for r in enriched_rows:
        try:
            ts = int(r["timestamp"])
            title = r["title"]
            status = r["status"]
            pnl = float(r["pnl_total"]) if r["pnl_total"] != "" else 0
            cost = float(r["usd_cost"])
            proxy_to_status[(ts, title)] = (status, pnl, cost)
        except Exception:
            continue

    seen_packs = set()
    for ev, ts in by_event.items():
        ts.sort(key=lambda x: x.get("timestamp", 0))
        for i, t in enumerate(ts):
            window = [tt for tt in ts if abs(tt["timestamp"] - t["timestamp"]) <= 3600]
            unique_mkts = {tt["conditionId"] for tt in window}
            if len(unique_mkts) < 3:
                continue
            # Identify pack by window timestamps tuple
            pack_id = (ev, tuple(sorted(tt.get("transactionHash", "") for tt in window)))
            if pack_id in seen_packs:
                continue
            seen_packs.add(pack_id)
            pack_count += 1

            statuses = []
            pnl_pack = 0.0
            cost_pack = 0.0
            for tt in window:
                ts_t = tt.get("timestamp", 0)
                title_t = (tt.get("title") or "")[:60]
                key = (ts_t, title_t)
                if key in proxy_to_status:
                    s, p, c = proxy_to_status[key]
                    statuses.append(s)
                    pnl_pack += p
                    cost_pack += c
                else:
                    statuses.append("unknown")

            won = sum(1 for s in statuses if s == "won")
            lost = sum(1 for s in statuses if s == "lost")
            total_closed = won + lost
            if total_closed == 0:
                pack_outcomes["open_or_partial"] += 1
                continue
            if won == total_closed:
                pack_outcomes["all_win"] += 1
            elif lost == total_closed:
                pack_outcomes["all_lose"] += 1
            else:
                pack_outcomes["mixed"] += 1
            pack_pnl += pnl_pack
            pack_cost += cost_pack

    return {
        "n_packs_detected": pack_count,
        "outcomes": pack_outcomes,
        "total_pnl": round(pack_pnl, 2),
        "total_cost": round(pack_cost, 2),
        "pack_roi": round(pack_pnl / pack_cost, 4) if pack_cost else 0,
    }


# ─── streak effect ───────────────────────────────────────────────────────────

def analyze_streaks(enriched_rows: list[dict]) -> dict:
    """For each closed BUY in chronological order, what's the win rate
    of trades following a win vs following a loss?
    """
    closed = [r for r in enriched_rows
              if r["side"] == "BUY" and r["status"] in ("won", "lost")]
    closed.sort(key=lambda r: int(r["timestamp"]))

    post_win_won = post_win_total = 0
    post_loss_won = post_loss_total = 0

    for i in range(1, len(closed)):
        prev = closed[i - 1]
        curr = closed[i]
        if prev["status"] == "won":
            post_win_total += 1
            if curr["status"] == "won":
                post_win_won += 1
        else:
            post_loss_total += 1
            if curr["status"] == "won":
                post_loss_won += 1

    return {
        "post_win_win_rate": round(post_win_won / post_win_total, 4) if post_win_total else 0,
        "post_win_n": post_win_total,
        "post_loss_win_rate": round(post_loss_won / post_loss_total, 4) if post_loss_total else 0,
        "post_loss_n": post_loss_total,
        "baseline_win_rate": round(sum(1 for r in closed if r["status"] == "won") / len(closed), 4)
                             if closed else 0,
    }


# ─── timing entry ────────────────────────────────────────────────────────────

def analyze_timing(enriched_rows: list[dict], markets: dict[str, dict]) -> list[dict]:
    """Hours between trade ts and market end_date_iso. Bucket the result."""
    # Need to load trades to get conditionId → end_date_iso mapping
    # Use markets dict
    cid_to_end: dict[str, int] = {}
    for cid, m in markets.items():
        end_iso = m.get("end_date_iso")
        if end_iso:
            try:
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                cid_to_end[cid] = int(end_dt.timestamp())
            except Exception:
                pass

    # But enriched rows don't have conditionId — we need a different strategy.
    # Load trades.jsonl to get hash → conditionId → end_ts
    trade_to_end: dict[tuple[int, str], int] = {}  # (ts, title[:60]) -> end_ts
    if TRADES_PATH.exists():
        with open(TRADES_PATH) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    t = json.loads(line)
                    end_ts = cid_to_end.get(t.get("conditionId"))
                    if end_ts:
                        key = (int(t["timestamp"]), (t.get("title") or "")[:60])
                        trade_to_end[key] = end_ts
                except Exception:
                    pass

    def hours_before(row: dict) -> str | None:
        try:
            ts = int(row["timestamp"])
            title = row["title"]
            end_ts = trade_to_end.get((ts, title))
            if not end_ts:
                return None
            hours = (end_ts - ts) / 3600
            if hours < 0:
                return "after_event"
            if hours < 2:
                return "0-2h"
            if hours < 6:
                return "2-6h"
            if hours < 24:
                return "6-24h"
            if hours < 72:
                return "1-3d"
            return "3d+"
        except Exception:
            return None

    return aggregate_dimension([r for r in enriched_rows if r["side"] == "BUY"], hours_before)


# ─── load enriched + markets ─────────────────────────────────────────────────

def load_enriched() -> list[dict]:
    if not ENRICHED_PATH.exists():
        sys.exit(f"[error] {ENRICHED_PATH} missing. Run analyze first.")
    out = []
    with open(ENRICHED_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            out.append(row)
    return out


def load_markets() -> dict[str, dict]:
    if not MARKETS_PATH.exists():
        return {}
    out = {}
    with open(MARKETS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    m = json.loads(line)
                    cid = m.get("condition_id") or m.get("conditionId")
                    if cid:
                        out[cid] = m
                except Exception:
                    pass
    return out


def load_trades() -> list[dict]:
    out = []
    with open(TRADES_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


# ─── market-type / league via trades + markets ───────────────────────────────

def build_trade_metadata(enriched_rows: list[dict], trades: list[dict],
                         markets: dict[str, dict]) -> dict[tuple, dict]:
    """Map (ts, title[:60]) → enriched metadata (market_type, league, slug)."""
    proxy_map: dict[tuple, dict] = {}
    for t in trades:
        ts = int(t.get("timestamp", 0))
        title = (t.get("title") or "")[:60]
        outcome = t.get("outcome", "")
        slug = t.get("slug", "")
        cid = t.get("conditionId")
        market = markets.get(cid, {})
        tags_str = "|".join(market.get("tags") or [])
        proxy_map[(ts, title)] = {
            "market_type": classify_market_type(t.get("title", ""), outcome),
            "league": parse_league(slug, t.get("title", ""), tags_str),
            "outcome": outcome,
        }
    return proxy_map


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("[load] enriched + trades + markets")
    enriched = load_enriched()
    trades = load_trades()
    markets = load_markets()
    proxy_meta = build_trade_metadata(enriched, trades, markets)

    # Attach extra columns to enriched rows
    for r in enriched:
        try:
            key = (int(r["timestamp"]), r["title"])
            extra = proxy_meta.get(key, {})
            r["market_type"] = extra.get("market_type", "unknown")
            r["league"] = extra.get("league", "Other")
        except Exception:
            r["market_type"] = "unknown"
            r["league"] = "Other"

    print(f"[load] {len(enriched)} enriched rows, {len(trades)} trades, {len(markets)} markets")

    # Run all dimensions (BUYs only for aggregations that need it)
    buys = [r for r in enriched if r["side"] == "BUY"]

    deep = {
        "ts": int(time.time()),
        "n_buys_analyzed": len(buys),
        "n_closed_buys": sum(1 for r in buys if r["status"] in ("won", "lost")),
        "dimensions": {
            "market_type": aggregate_dimension(buys, lambda r: r["market_type"]),
            "size_bucket": aggregate_dimension(buys, lambda r: classify_size_bucket(float(r["usd_cost"]))),
            "outcome_side": aggregate_dimension(buys, lambda r: r.get("outcome", "unknown")[:20]),
            "hour_of_day": aggregate_dimension(buys, lambda r: hour_of_day(int(r["timestamp"]))),
            "league": aggregate_dimension(buys, lambda r: r["league"]),
            "timing_to_event": analyze_timing(enriched, markets),
        },
        "multi_leg_packs": analyze_multi_leg_packs(enriched, trades),
        "streaks": analyze_streaks(enriched),
    }

    OUT_JSON.write_text(json.dumps(deep, indent=2))
    print(f"[write] {OUT_JSON}")

    # Append deep findings to existing markdown report
    append_to_report(deep)


def append_to_report(deep: dict) -> None:
    md_parts = []
    md_parts.append("\n\n---\n\n## Deep dive (analyze_deep.py)\n\n")
    md_parts.append(f"_{deep['n_buys_analyzed']} BUYs analysés, "
                    f"{deep['n_closed_buys']} closed._\n\n")

    for dim_name, label in [
        ("market_type", "Type de marché"),
        ("league", "Ligue / compétition"),
        ("size_bucket", "Taille du bet ($)"),
        ("hour_of_day", "Heure (UTC)"),
        ("timing_to_event", "Délai entry → résolution"),
    ]:
        rows = deep["dimensions"].get(dim_name, [])
        if not rows:
            continue
        md_parts.append(f"\n### {label}\n\n")
        md_parts.append("| Key | N closed | Win rate | Raw ROI | Net ROI (−2% fee) | Cost basis |\n")
        md_parts.append("|---|---|---|---|---|---|\n")
        for r in rows[:12]:  # top 12 by n_closed
            emoji = "✅" if r["net_roi"] > 0.05 else ("🟡" if r["net_roi"] > 0 else "❌")
            md_parts.append(
                f"| `{r['key']}` | {r['n_closed']:>4} | "
                f"{r['win_rate']*100:>5.1f}% | "
                f"{r['raw_roi']*100:+.1f}% | "
                f"{r['net_roi']*100:+.1f}% {emoji} | "
                f"${r['usd_cost_total']:>9,.0f} |\n"
            )

    # Multi-leg
    mlp = deep["multi_leg_packs"]
    md_parts.append("\n### Multi-leg packs (≥3 marchés corrélés)\n\n")
    md_parts.append(f"- Packs détectés: **{mlp['n_packs_detected']}**\n")
    md_parts.append(f"- All-win: {mlp['outcomes']['all_win']}\n")
    md_parts.append(f"- All-lose: {mlp['outcomes']['all_lose']}\n")
    md_parts.append(f"- Mixed: {mlp['outcomes']['mixed']}\n")
    md_parts.append(f"- Open/partial: {mlp['outcomes']['open_or_partial']}\n")
    md_parts.append(f"- Pack ROI brut: {mlp['pack_roi']*100:+.1f}% sur ${mlp['total_cost']:,.0f} déployés\n")

    # Streaks
    sk = deep["streaks"]
    md_parts.append("\n### Effet de streak (post-trade conditional win rate)\n\n")
    md_parts.append(f"- Baseline win rate: {sk['baseline_win_rate']*100:.1f}%\n")
    md_parts.append(f"- Après un WIN: {sk['post_win_win_rate']*100:.1f}% sur {sk['post_win_n']} trades\n")
    md_parts.append(f"- Après une LOSS: {sk['post_loss_win_rate']*100:.1f}% sur {sk['post_loss_n']} trades\n")

    # Append to report (don't overwrite the main analyze.py report)
    existing = OUT_REPORT.read_text() if OUT_REPORT.exists() else ""
    # Strip any previous deep section to avoid duplication
    if "## Deep dive (analyze_deep.py)" in existing:
        existing = existing.split("\n\n---\n\n## Deep dive")[0]
    OUT_REPORT.write_text(existing + "".join(md_parts))
    print(f"[write] {OUT_REPORT} (appended deep section)")


if __name__ == "__main__":
    main()

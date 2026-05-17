"""Test ultime : nos predictions Elo matchent-elles les picks tennis WTA de RN1 ?

Pour chaque trade tennis WTA de RN1 :
  1. Parse le marché pour extraire les 2 joueuses
  2. Look up nos Elo ratings (au moment du match)
  3. Compute notre prob predicted
  4. Compare à son entry price + outcome qu'il a choisi
  5. Classify :
     - AGREE : on aurait pris le même side avec une edge similar
     - DISAGREE : on aurait pris l'autre side
     - UNDERSIZED_EDGE : on est d'accord mais edge faible (< 5%)
     - PLAYER_NOT_FOUND : impossible d'identifier les joueuses

Si AGREE rate > 50% → notre modèle réplique son edge tennis WTA.

Usage : python -m analysis.tennis_model.compare_to_rn1
"""
from __future__ import annotations
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
RATINGS_PATH = DATA_DIR / "elo_ratings.json"
RN1_TRADES_PATH = Path(__file__).resolve().parent.parent / "rn1" / "data" / "trades.jsonl"
OUT_PATH = DATA_DIR / "rn1_comparison.json"


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def normalize_name(name: str) -> str:
    """Normalize player name for matching across data sources."""
    if not name:
        return ""
    # Remove accents/diacritics + lowercase
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    no_acc = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Strip non-letter chars
    cleaned = re.sub(r"[^a-zA-Z\s]", "", no_acc)
    # Collapse whitespace
    return re.sub(r"\s+", " ", cleaned.lower()).strip()


def build_name_index(player_names: dict) -> dict[str, str]:
    """Build normalized_name → player_id index. Handle name variants."""
    idx = {}
    for pid, name in player_names.items():
        norm = normalize_name(name)
        if norm:
            idx[norm] = pid
            # Also index last name only (common in market titles)
            parts = norm.split()
            if len(parts) >= 2:
                last = parts[-1]
                if last not in idx:  # don't overwrite full match
                    idx[last] = pid
    return idx


def find_player_id(player_name: str, name_idx: dict) -> str | None:
    """Try to find player_id from name string (possibly partial)."""
    if not player_name:
        return None
    norm = normalize_name(player_name)
    if norm in name_idx:
        return name_idx[norm]
    # Try last name only
    parts = norm.split()
    if parts:
        last = parts[-1]
        if last in name_idx:
            return name_idx[last]
    # Try fuzzy : any key that contains all parts
    for key in name_idx:
        if all(p in key for p in parts):
            return name_idx[key]
    return None


def parse_match_title(title: str) -> tuple[str, str] | None:
    """Extract (player_a, player_b) from Polymarket market title.

    Patterns observed :
      "X vs. Y"
      "X vs Y"
      "Will X win on YYYY-MM-DD?"  (only one player — skip for now)
      "Tournament: X vs Y"
    """
    if not title:
        return None
    # Strip leading tournament prefix
    t = title.split(":", 1)[-1].strip() if ":" in title else title
    # Pattern : X vs. Y or X vs Y
    m = re.search(r"^(.+?)\s+vs\.?\s+(.+)$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def main() -> None:
    if not RATINGS_PATH.exists():
        sys.exit(f"[error] {RATINGS_PATH} missing — run elo.py first")
    if not RN1_TRADES_PATH.exists():
        sys.exit(f"[error] {RN1_TRADES_PATH} missing")

    data = json.loads(RATINGS_PATH.read_text())
    overall_ratings = data["overall_ratings"]
    player_names = data["player_names"]
    name_idx = build_name_index(player_names)
    print(f"[load] {len(player_names)} player names indexed "
          f"({len(name_idx)} lookup keys)")

    # Load RN1 tennis trades (filter via title heuristic — names like "Sabalenka")
    rn1_tennis_trades = []
    with open(RN1_TRADES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if t.get("name") != "RN1":
                    continue
                title = (t.get("title") or "").lower()
                # Heuristic : tennis WTA markets have player names + "vs"
                if " vs" not in title and "vs." not in title:
                    continue
                # Skip obvious non-tennis ("X vs Y" exists in many sports)
                # Check for tennis-related keywords
                slug = (t.get("slug") or "").lower()
                event_slug = (t.get("eventSlug") or "").lower()
                if any(kw in slug + event_slug for kw in
                       ["wta", "tennis", "atp", "internazionali", "ostrava",
                        "monterrey", "rome", "madrid", "roland", "wimbledon",
                        "us-open", "australian"]):
                    rn1_tennis_trades.append(t)
            except Exception:
                pass

    print(f"[rn1] {len(rn1_tennis_trades)} probable tennis trades from RN1")

    # Analyze
    results = []
    counters = defaultdict(int)

    for t in rn1_tennis_trades:
        title = t.get("title") or ""
        outcome = t.get("outcome") or ""
        entry_price = float(t.get("price", 0))
        side = t.get("side")

        if side != "BUY":
            counters["sell_skip"] += 1
            continue

        # Parse match title for the two players
        parsed = parse_match_title(title)
        if not parsed:
            counters["title_unparseable"] += 1
            continue
        name_a, name_b = parsed

        pid_a = find_player_id(name_a, name_idx)
        pid_b = find_player_id(name_b, name_idx)
        if not pid_a or not pid_b:
            counters["player_not_found"] += 1
            results.append({
                "title": title[:60], "outcome": outcome, "entry": entry_price,
                "name_a": name_a, "name_b": name_b,
                "found_a": bool(pid_a), "found_b": bool(pid_b),
                "classification": "player_not_found",
            })
            continue

        r_a = overall_ratings.get(pid_a, 1500)
        r_b = overall_ratings.get(pid_b, 1500)
        # Our prob that A wins
        prob_a = expected_score(r_a, r_b)
        prob_b = 1.0 - prob_a

        # Which side did RN1 bet on ? Match outcome to player name
        norm_outcome = normalize_name(outcome)
        if norm_outcome in normalize_name(name_a) or normalize_name(name_a) in norm_outcome:
            his_side = "a"
            our_prob_his_side = prob_a
        elif norm_outcome in normalize_name(name_b) or normalize_name(name_b) in norm_outcome:
            his_side = "b"
            our_prob_his_side = prob_b
        else:
            counters["outcome_unmatched"] += 1
            continue

        # Compare : did we predict same side ? edge magnitude ?
        our_predicted_side = "a" if prob_a > 0.5 else "b"
        agree = (his_side == our_predicted_side)
        # Edge = our_prob - market_price (positive means we think it's underpriced)
        our_edge = our_prob_his_side - entry_price

        if agree and our_edge >= 0.05:
            klass = "AGREE_STRONG"  # we'd also bet, with significant edge
        elif agree and our_edge >= 0:
            klass = "AGREE_WEAK"    # we agree but tiny edge
        elif agree:
            klass = "AGREE_BUT_OVERPRICED"  # we'd skip (market overpriced our pick)
        else:
            klass = "DISAGREE"      # we'd bet the other side

        counters[klass] += 1
        results.append({
            "title": title[:60], "outcome": outcome, "entry": entry_price,
            "name_a": name_a, "name_b": name_b,
            "elo_a": r_a, "elo_b": r_b,
            "our_prob_a": round(prob_a, 4),
            "his_side": his_side,
            "our_prob_his_side": round(our_prob_his_side, 4),
            "our_edge": round(our_edge, 4),
            "classification": klass,
        })

    # Save
    OUT_PATH.write_text(json.dumps({
        "n_analyzed": len(rn1_tennis_trades),
        "counters": dict(counters),
        "sample_results": results[:50],
    }, indent=2))

    # Report
    total_analyzable = sum(counters[k] for k in
                           ["AGREE_STRONG", "AGREE_WEAK", "AGREE_BUT_OVERPRICED",
                            "DISAGREE"])
    print(f"\n=== COMPARISON RESULTS ===\n")
    print(f"Total RN1 tennis-like trades  : {len(rn1_tennis_trades)}")
    print(f"  Title unparseable           : {counters['title_unparseable']}")
    print(f"  Player not found in our DB  : {counters['player_not_found']}")
    print(f"  Outcome unmatched           : {counters['outcome_unmatched']}")
    print(f"  Sell (skipped)              : {counters['sell_skip']}")
    print(f"  Analyzable BUYs             : {total_analyzable}")
    print()
    if total_analyzable > 0:
        print(f"{'Classification':<24} {'count':>6} {'pct':>7}")
        print("-" * 40)
        for k in ["AGREE_STRONG", "AGREE_WEAK", "AGREE_BUT_OVERPRICED", "DISAGREE"]:
            c = counters[k]
            pct = 100 * c / total_analyzable
            emoji = "✅" if "AGREE" in k else "❌"
            print(f"  {emoji} {k:<20} {c:>6} {pct:>6.1f}%")

        agree_total = counters["AGREE_STRONG"] + counters["AGREE_WEAK"] + counters["AGREE_BUT_OVERPRICED"]
        agree_pct = 100 * agree_total / total_analyzable
        strong_pct = 100 * counters["AGREE_STRONG"] / total_analyzable

        print(f"\n=== VERDICT ===")
        print(f"  Agreement rate (any)    : {agree_pct:.1f}%")
        print(f"  Strong agreement (>5%   : {strong_pct:.1f}%")
        print(f"    edge)")
        if strong_pct >= 50:
            print(f"\n✅ HYPOTHESIS CONFIRMED : our Elo model agrees with RN1 on >50% of picks")
            print(f"   → We've replicated his tennis edge with a free statistical model")
            print(f"   → Phase B3 (build the scanner) is justified")
        elif agree_pct >= 50:
            print(f"\n🟡 PARTIAL : we agree on side but not on edge magnitude")
            print(f"   → Our model is in the right zone but RN1 has more conviction")
            print(f"   → He likely uses additional signals (form, injuries, recent)")
        else:
            print(f"\n❌ HYPOTHESIS WEAK : we disagree with him often")
            print(f"   → His edge isn't pure Elo — needs more features")

    # Show sample of strong agreements
    strong = [r for r in results if r.get("classification") == "AGREE_STRONG"]
    if strong:
        print(f"\nSample STRONG AGREEMENTS (top 10) :")
        for r in sorted(strong, key=lambda x: -x.get("our_edge", 0))[:10]:
            print(f"  [{r['our_edge']:+.3f} edge] {r['name_a']} vs {r['name_b']} "
                  f"→ RN1 bet {r['outcome']} @ {r['entry']:.3f} "
                  f"(our prob {r['our_prob_his_side']:.3f})")


if __name__ == "__main__":
    main()

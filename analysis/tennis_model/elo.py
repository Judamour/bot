"""Elo rating system for WTA matches.

Two Elos per player :
  - Overall (all surfaces)
  - Surface-specific (clay / hard / grass)

K-factor adjusted by tournament tier :
  - Grand Slam : 40
  - WTA 1000   : 32
  - WTA 500    : 28
  - WTA 250    : 24
  - Other      : 20

Prediction : P(A wins) = 1 / (1 + 10^((R_B - R_A) / 400))
  Blended : 0.6 * surface_elo + 0.4 * overall_elo

Usage : python -m analysis.tennis_model.elo
"""
from __future__ import annotations
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
RATINGS_PATH = DATA_DIR / "elo_ratings.json"
MATCHES_ENRICHED_PATH = DATA_DIR / "matches_enriched.jsonl"

INITIAL_ELO = 1500.0
ELO_SCALE = 400.0
SURFACES = {"Hard", "Clay", "Grass", "Carpet"}

K_BY_LEVEL = {
    "G": 40,    # Grand Slam
    "PM": 32,   # Premier Mandatory / WTA 1000
    "P": 28,    # Premier / WTA 500
    "I": 24,    # International / WTA 250
}


def k_factor(tourney_level: str) -> float:
    return K_BY_LEVEL.get(tourney_level, 20)


def expected_score(rating_a: float, rating_b: float) -> float:
    """P(A wins) given Elo ratings."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / ELO_SCALE))


def update_elo(rating_winner: float, rating_loser: float, k: float
               ) -> tuple[float, float]:
    """Return (new_winner_rating, new_loser_rating)."""
    expected_w = expected_score(rating_winner, rating_loser)
    new_w = rating_winner + k * (1.0 - expected_w)
    new_l = rating_loser + k * (0.0 - (1.0 - expected_w))
    return new_w, new_l


def parse_tourney_date(s: str) -> datetime:
    """YYYYMMDD format."""
    s = str(s)
    return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))


def load_all_matches() -> list[dict]:
    """Load and merge all wta_matches_*.csv files."""
    matches = []
    for csv_path in sorted(DATA_DIR.glob("wta_matches_*.csv")):
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                matches.append(row)
    return matches


def process_matches() -> dict:
    """Process all matches chronologically, build final ratings + backtest stats.

    Returns dict with :
      - overall_ratings : {player_id: rating}
      - surface_ratings : {surface: {player_id: rating}}
      - player_names    : {player_id: name}
      - n_matches_processed
      - backtest stats (predicted vs actual on later years)
    """
    matches = load_all_matches()
    print(f"[load] {len(matches)} total matches")

    # Sort chronologically
    matches.sort(key=lambda m: (m.get("tourney_date", "0"),
                                 int(m.get("match_num", 0))))

    overall_ratings: dict[str, float] = defaultdict(lambda: INITIAL_ELO)
    surface_ratings: dict[str, dict[str, float]] = {
        s: defaultdict(lambda: INITIAL_ELO) for s in SURFACES
    }
    player_names: dict[str, str] = {}
    n_matches: dict[str, int] = defaultdict(int)  # match count per player

    # Backtest : predict matches in 2025-2026 using ratings BEFORE the match
    backtest_correct = 0
    backtest_total = 0
    backtest_buckets = defaultdict(lambda: {"correct": 0, "total": 0})

    out_records = []

    for m in matches:
        winner_id = m.get("winner_id")
        loser_id = m.get("loser_id")
        if not winner_id or not loser_id:
            continue
        try:
            tdate = parse_tourney_date(m.get("tourney_date"))
        except Exception:
            continue

        surface = m.get("surface", "")
        if surface not in SURFACES:
            surface = "Hard"  # default

        winner_name = m.get("winner_name") or ""
        loser_name = m.get("loser_name") or ""
        player_names[winner_id] = winner_name
        player_names[loser_id] = loser_name

        k = k_factor(m.get("tourney_level", ""))

        # Get ratings BEFORE this match
        w_overall_pre = overall_ratings[winner_id]
        l_overall_pre = overall_ratings[loser_id]
        w_surface_pre = surface_ratings[surface][winner_id]
        l_surface_pre = surface_ratings[surface][loser_id]

        # Blended pre-match ratings (0.6 surface + 0.4 overall)
        w_blended = 0.6 * w_surface_pre + 0.4 * w_overall_pre
        l_blended = 0.6 * l_surface_pre + 0.4 * l_overall_pre
        prob_winner = expected_score(w_blended, l_blended)

        # Backtest : count matches from 2025+ where both players have ≥ 10 prior matches.
        # Bucket by ABSOLUTE prediction confidence : max(prob_winner, 1-prob_winner).
        # Predicted correct if prob_winner >= 0.5 (we said winner was favored).
        if tdate.year >= 2025 and n_matches[winner_id] >= 10 and n_matches[loser_id] >= 10:
            backtest_total += 1
            predicted_correct = prob_winner >= 0.5
            if predicted_correct:
                backtest_correct += 1
            # Confidence = how strongly we predicted (away from 0.5)
            confidence = max(prob_winner, 1.0 - prob_winner)
            bucket = ("0.50-0.55" if confidence < 0.55 else
                      "0.55-0.60" if confidence < 0.60 else
                      "0.60-0.70" if confidence < 0.70 else
                      "0.70-0.80" if confidence < 0.80 else
                      "0.80-0.90" if confidence < 0.90 else
                      "0.90+")
            b = backtest_buckets[bucket]
            b["total"] += 1
            if predicted_correct:
                b["correct"] += 1

        # Update ratings
        new_w_o, new_l_o = update_elo(w_overall_pre, l_overall_pre, k)
        new_w_s, new_l_s = update_elo(w_surface_pre, l_surface_pre, k)
        overall_ratings[winner_id] = new_w_o
        overall_ratings[loser_id] = new_l_o
        surface_ratings[surface][winner_id] = new_w_s
        surface_ratings[surface][loser_id] = new_l_s

        n_matches[winner_id] += 1
        n_matches[loser_id] += 1

        # Save enriched match record (with pre-match ratings)
        out_records.append({
            "date": tdate.strftime("%Y-%m-%d"),
            "year": tdate.year,
            "tourney_name": m.get("tourney_name"),
            "tourney_level": m.get("tourney_level"),
            "surface": surface,
            "winner_id": winner_id,
            "winner_name": winner_name,
            "loser_id": loser_id,
            "loser_name": loser_name,
            "w_overall_pre": round(w_overall_pre, 1),
            "l_overall_pre": round(l_overall_pre, 1),
            "w_surface_pre": round(w_surface_pre, 1),
            "l_surface_pre": round(l_surface_pre, 1),
            "prob_winner": round(prob_winner, 4),
            "k": k,
        })

    print(f"[elo] processed {len(out_records)} matches with valid Elo updates")

    # Write enriched matches (for further analysis)
    with open(MATCHES_ENRICHED_PATH, "w") as f:
        for r in out_records:
            f.write(json.dumps(r) + "\n")
    print(f"[write] {MATCHES_ENRICHED_PATH} ({len(out_records)} records)")

    # Write final ratings
    final_data = {
        "n_matches_processed": len(out_records),
        "n_players": len(overall_ratings),
        "overall_ratings": {pid: round(r, 1) for pid, r in overall_ratings.items()},
        "surface_ratings": {
            s: {pid: round(r, 1) for pid, r in ratings.items()}
            for s, ratings in surface_ratings.items()
        },
        "player_names": dict(player_names),
        "n_matches_per_player": dict(n_matches),
    }
    RATINGS_PATH.write_text(json.dumps(final_data))
    print(f"[write] {RATINGS_PATH}")

    # Report backtest
    acc = (backtest_correct / backtest_total * 100) if backtest_total else 0
    print(f"\n=== BACKTEST 2025-2026 (matches w/ both players ≥10 prior matches) ===")
    print(f"Predictions made : {backtest_total}")
    print(f"Correct          : {backtest_correct} ({acc:.1f}%)")
    print()
    print(f"By prediction confidence bucket (calibration check) :")
    print(f"{'confidence':<14} {'count':>6} {'correct':>8} {'accuracy':>10} {'expected':>10}")
    print("-" * 60)
    for bucket in ["0.50-0.55", "0.55-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90+"]:
        b = backtest_buckets[bucket]
        if b["total"] > 0:
            acc_b = b["correct"] / b["total"] * 100
            # Expected = midpoint of bucket
            exp = {"0.50-0.55": 52.5, "0.55-0.60": 57.5, "0.60-0.70": 65,
                   "0.70-0.80": 75, "0.80-0.90": 85, "0.90+": 95}[bucket]
            print(f"{bucket:<14} {b['total']:>6} {b['correct']:>8} {acc_b:>9.1f}% "
                  f"{exp:>9.1f}%")

    return final_data


def get_top_n(ratings: dict, n: int = 20) -> list[tuple]:
    """Return top N players by rating."""
    return sorted(ratings.items(), key=lambda x: -x[1])[:n]


def main() -> None:
    data = process_matches()
    print(f"\n=== TOP 20 OVERALL ELO ===")
    names = data["player_names"]
    n_matches = data["n_matches_per_player"]
    # Filter to active (>=20 matches)
    active = {pid: r for pid, r in data["overall_ratings"].items()
              if n_matches.get(pid, 0) >= 20}
    for pid, rating in get_top_n(active, 20):
        name = names.get(pid, pid)
        nm = n_matches.get(pid, 0)
        print(f"  {rating:>6.0f}  {name:<30} ({nm} matches)")


if __name__ == "__main__":
    main()

"""Cash settlement opportunity scanner — test hypothesis #1.

Hypothesis : RN1 trade des markets où :
  - end_date_iso est passé (event terminé)
  - une outcome est encore tradable à 0.85-0.99 (winner pas encore résolu UMA)
  - autre outcome à 0.01-0.15
→ Buy le winner à 0.95 → attend 1-3j UMA resolution → +5-10% garanti

ON N'A PAS BESOIN D'ESPN/sports scores — Polymarket lui-même nous dit
implicitement qui a gagné via les prix (le marché a déjà convergé).

Cross-référence : pour chaque opportunité détectée, on check si RN1 l'a
tradée (dans decisions.jsonl récent). Match high = hypothèse confirmée.

Usage :
    python -m analysis.rn1.discover_cashsettle

Output :
    analysis/rn1/data/cashsettle_opportunities.json
    rapport console
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

GAMMA_API = "https://gamma-api.polymarket.com/markets"
DATA_DIR = Path(__file__).resolve().parent / "data"
OUT_PATH = DATA_DIR / "cashsettle_opportunities.json"

# bot-cp's decisions.jsonl — RN1 recent trades for cross-reference
DECISIONS_PATH = Path("/home/botuser/bot-trading/logs/copytrade/decisions.jsonl")
if not DECISIONS_PATH.exists():
    DECISIONS_PATH = DATA_DIR / "trades.jsonl"

# Cash settlement filter
LOOKBACK_HOURS = 72        # event must be in [-72h, +0h]
LOOKAHEAD_HOURS = 0        # don't include future events
WIN_PRICE_MIN = 0.85       # likely-winner side trades at 0.85+
WIN_PRICE_MAX = 0.99       # but not 1.00 (not yet resolved)
LOSS_PRICE_MAX = 0.15      # other side trades < 0.15 (clear loser)
MIN_LIQUIDITY = 50         # at least $50 liquidity in book
MIN_VOLUME = 100           # at least $100 lifetime volume


def fetch_all_markets() -> list[dict]:
    """Paginate Gamma API to get all active markets.

    Hard cap : Gamma API returns max 100 markets per request, regardless
    of `limit` param. Pagination via `offset`. Total active markets is
    ~10-15K, we cap at 20K iterations for safety.
    """
    out = []
    offset = 0
    page_size = 100  # Gamma API hard cap
    consecutive_empty = 0
    while True:
        url = f"{GAMMA_API}?active=true&closed=false&limit={page_size}&offset={offset}"
        try:
            r = httpx.get(url, timeout=30)
            r.raise_for_status()
            page = r.json()
        except Exception as e:
            print(f"  [warn] page offset={offset} failed: {e}", file=sys.stderr)
            break
        if not page:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            offset += page_size
            continue
        consecutive_empty = 0
        out.extend(page)
        if offset % 1000 == 0:
            print(f"  fetched offset={offset:>5} ({len(page)}), total={len(out)}")
        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(0.05)
        if offset > 20000:
            print(f"  [warn] safety cap reached at offset {offset}")
            break
    return out


def parse_outcome_prices(m: dict) -> list[float] | None:
    """Parse outcomePrices field which is sometimes a list, sometimes a JSON string."""
    raw = m.get("outcomePrices")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    try:
        return [float(p) for p in raw]
    except Exception:
        return None


def parse_outcomes(m: dict) -> list[str] | None:
    raw = m.get("outcomes")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    return [str(o) for o in raw]


def parse_token_ids(m: dict) -> list[str] | None:
    raw = m.get("clobTokenIds")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    return [str(t) for t in raw]


def is_cashsettle_opportunity(m: dict, now: datetime) -> dict | None:
    """Return enriched opportunity dict if market matches cash-settle criteria, else None."""
    end_iso = m.get("endDate") or m.get("endDateIso")
    if not end_iso:
        return None
    try:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except Exception:
        return None

    delta_hours = (now - end_dt).total_seconds() / 3600
    # End date in past 0-72h (event happened, market still trading)
    if not (0 <= delta_hours <= LOOKBACK_HOURS):
        return None

    prices = parse_outcome_prices(m)
    outcomes = parse_outcomes(m)
    if not prices or not outcomes or len(prices) != 2 or len(outcomes) != 2:
        return None

    # Identify which outcome is the "likely winner" by price
    if WIN_PRICE_MIN <= prices[0] <= WIN_PRICE_MAX and prices[1] <= LOSS_PRICE_MAX:
        winner_idx = 0
    elif WIN_PRICE_MIN <= prices[1] <= WIN_PRICE_MAX and prices[0] <= LOSS_PRICE_MAX:
        winner_idx = 1
    else:
        return None

    # Liquidity / volume gates
    try:
        liq = float(m.get("liquidity") or 0)
        vol = float(m.get("volume") or 0)
    except Exception:
        liq = vol = 0
    if liq < MIN_LIQUIDITY or vol < MIN_VOLUME:
        return None

    token_ids = parse_token_ids(m) or [None, None]

    return {
        "condition_id": m.get("conditionId") or m.get("condition_id"),
        "question": m.get("question"),
        "slug": m.get("slug"),
        "end_date": end_iso,
        "hours_since_end": round(delta_hours, 2),
        "winner_outcome": outcomes[winner_idx],
        "winner_price": prices[winner_idx],
        "loser_outcome": outcomes[1 - winner_idx],
        "loser_price": prices[1 - winner_idx],
        "winner_token_id": token_ids[winner_idx],
        "loser_token_id": token_ids[1 - winner_idx],
        "expected_roi_pct": round((1.0 / prices[winner_idx] - 1) * 100, 2),
        "liquidity_usd": round(liq, 2),
        "volume_usd": round(vol, 2),
        "category": m.get("category"),
        "tags": m.get("tags") if isinstance(m.get("tags"), list) else [],
    }


def load_rn1_recent_trades(hours_back: int = 72) -> set[str]:
    """Return set of conditionIds RN1 traded in the past N hours.

    Works with both formats :
    - bot-cp decisions.jsonl : {wallet: 'RN1', ts: ..., conditionId: ...}
    - Polymarket data-api trades.jsonl : {name: 'RN1', timestamp: ..., conditionId: ...}
    """
    if not DECISIONS_PATH.exists():
        return set()
    cutoff_ts = int(time.time()) - hours_back * 3600
    rn1_cids: set[str] = set()
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
                # Try multiple wallet field names (bot-cp vs data-api format)
                wallet_id = (d.get("wallet") or d.get("name")
                             or d.get("pseudonym") or "")
                ts = int(d.get("ts") or d.get("timestamp") or 0)
                is_rn1 = wallet_id == "RN1" or wallet_id == "Scary-Edible"
                if is_rn1 and ts >= cutoff_ts:
                    cid = d.get("conditionId") or d.get("condition_id")
                    if cid:
                        rn1_cids.add(cid)
    except Exception as e:
        print(f"  [warn] failed reading {DECISIONS_PATH}: {e}", file=sys.stderr)
    return rn1_cids


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    print(f"=== Cash settlement scanner @ {now.isoformat()} ===")

    print("\n[1/3] Fetching all active Polymarket markets via Gamma API")
    markets = fetch_all_markets()
    print(f"  → {len(markets)} active markets total")

    print(f"\n[2/3] Filtering for cash-settle opportunities (end_date ∈ [-{LOOKBACK_HOURS}h, 0h], "
          f"winner ∈ [{WIN_PRICE_MIN}, {WIN_PRICE_MAX}], loser ≤ {LOSS_PRICE_MAX})")
    opportunities = []
    for m in markets:
        opp = is_cashsettle_opportunity(m, now)
        if opp:
            opportunities.append(opp)
    print(f"  → {len(opportunities)} opportunities matching cash-settle pattern")

    print(f"\n[3/3] Cross-reference with RN1 trades (past 72h)")
    rn1_recent_cids = load_rn1_recent_trades(hours_back=72)
    print(f"  → {len(rn1_recent_cids)} unique markets RN1 traded in past 72h")

    matched = 0
    for opp in opportunities:
        cid = opp["condition_id"]
        if cid and cid in rn1_recent_cids:
            opp["rn1_traded"] = True
            matched += 1
        else:
            opp["rn1_traded"] = False

    overlap_pct = (matched / len(opportunities) * 100) if opportunities else 0
    reverse_pct = (matched / len(rn1_recent_cids) * 100) if rn1_recent_cids else 0

    # Sort by RN1 traded first, then by hours_since_end desc
    opportunities.sort(key=lambda o: (-o["rn1_traded"], -o["hours_since_end"]))

    OUT_PATH.write_text(json.dumps({
        "ts": int(time.time()),
        "scan_window_hours": LOOKBACK_HOURS,
        "total_active_markets": len(markets),
        "n_opportunities": len(opportunities),
        "n_rn1_recent_trades": len(rn1_recent_cids),
        "n_overlap": matched,
        "overlap_pct_of_opportunities": round(overlap_pct, 2),
        "rn1_coverage_pct": round(reverse_pct, 2),
        "opportunities": opportunities,
    }, indent=2))

    print(f"\n=== VERDICT ===")
    print(f"Opportunities détectées par notre scanner : {len(opportunities)}")
    print(f"Trades RN1 dans la même fenêtre (72h)    : {len(rn1_recent_cids)}")
    print(f"Match (les deux) : {matched}")
    print(f"  → {overlap_pct:.1f}% de nos opportunités ont été tradées par RN1")
    print(f"  → {reverse_pct:.1f}% des trades RN1 récents matchent nos opportunités")
    print(f"\nOutput : {OUT_PATH}")

    if opportunities:
        print(f"\nTop 10 opportunités triées (RN1 picked first) :")
        print(f"{'RN1?':<5} {'h_end':<6} {'win px':<7} {'ROI%':<6} {'liq':<8} {'question'}")
        print("-" * 110)
        for opp in opportunities[:10]:
            flag = "✅" if opp["rn1_traded"] else "  "
            print(f"{flag:<5} {opp['hours_since_end']:>5.1f} "
                  f"{opp['winner_price']:>6.3f} "
                  f"{opp['expected_roi_pct']:>5.1f} "
                  f"${opp['liquidity_usd']:>6.0f} "
                  f"{(opp['question'] or '')[:60]}")


if __name__ == "__main__":
    main()

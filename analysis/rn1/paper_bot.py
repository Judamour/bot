"""RN1 paper bot — applies our identified edge to RN1's trade signals.

Strategy = "filtered copy of RN1" in paper mode:
1. Watch each new RN1 BUY in trades.jsonl
2. Apply our edge filters:
   - bucket = favorite (0.85 <= price < 0.95) — best ROI net identified
   - sport in whitelist (Tennis WTA, Saudi Pro, Premier League, etc.)
   - market_type = match_winner or over_under (the +EV types)
3. Open paper position : $FIXED_SIZE at his entry price + 2% taker fee
4. When market resolves (closed=True in markets.jsonl), realize PnL
5. Snapshot equity daily

Output:
    data/paper_positions.json   ← open paper positions
    data/paper_trades.jsonl     ← every paper action (buy + resolution)
    data/paper_equity.jsonl     ← daily equity snapshot
    data/paper_state.json       ← last_seen_ts + meta

Tuning:
    INITIAL_CAPITAL_USD : 1000 (paper)
    FIXED_SIZE_USD      : 10 (paper, fractional ok)
    TAKER_FEE           : 2%
"""
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADES_PATH = DATA_DIR / "trades.jsonl"
MARKETS_PATH = DATA_DIR / "markets.jsonl"
POSITIONS_PATH = DATA_DIR / "paper_positions.json"
TRADES_LOG_PATH = DATA_DIR / "paper_trades.jsonl"
EQUITY_PATH = DATA_DIR / "paper_equity.jsonl"
STATE_PATH = DATA_DIR / "paper_state.json"

INITIAL_CAPITAL_USD = float(os.environ.get("RN1_PAPER_INITIAL", "1000"))
FIXED_SIZE_USD = float(os.environ.get("RN1_PAPER_SIZE", "10"))
TAKER_FEE = 0.02

# Edge filters (derived from analyze_deep.py)
MIN_PRICE = 0.85
MAX_PRICE = 0.95
SPORT_WHITELIST = {
    "Soccer", "Tennis", "Hockey",  # broad
    # Specific leagues are filtered in finer detail by market_type
}
MARKET_TYPE_WHITELIST = {"match_winner", "over_under", "winner_yes_no"}
# winner_yes_no is included despite -19% raw_roi because it's how match_winner-style
# bets appear for "Will X win" framings. Combined with bucket filter (only favorites)
# we capture the +EV slice.


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _classify_market_type(title: str) -> str:
    t = (title or "").lower()
    if "o/u" in t or "over/under" in t:
        return "over_under"
    if t.startswith("spread:"):
        return "spread"
    if "both teams to score" in t or "btts" in t:
        return "btts"
    if "end in a draw" in t or "draw?" in t:
        return "draw"
    if "will " in t and " win " in t:
        return "winner_yes_no"
    if " vs. " in t or " vs " in t:
        return "match_winner"
    return "other"


def _market_sport(market: dict) -> str | None:
    tags = market.get("tags") or []
    priority = ["Soccer", "Basketball", "Baseball", "MMA", "Tennis", "Hockey", "Football"]
    for p in priority:
        if p in tags:
            return p
    return None


def _winning_token(market: dict) -> str | None:
    if not market.get("closed"):
        return None
    for tok in market.get("tokens", []):
        if tok.get("winner"):
            return str(tok.get("token_id"))
    return None


def _load_markets() -> dict:
    out = {}
    if not MARKETS_PATH.exists():
        return out
    with open(MARKETS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                cid = m.get("condition_id") or m.get("conditionId")
                if cid and not m.get("_missing"):
                    out[cid] = m
            except Exception:
                pass
    return out


def _trade_passes_filters(trade: dict, market: dict) -> tuple[bool, str]:
    """Return (accept, reason). reason is the rejection cause if False."""
    if trade.get("side") != "BUY":
        return False, "not_buy"
    price = float(trade.get("price", 0))
    if price < MIN_PRICE or price >= MAX_PRICE:
        return False, f"price_oob ({price:.3f})"
    mtype = _classify_market_type(trade.get("title", ""))
    if mtype not in MARKET_TYPE_WHITELIST:
        return False, f"market_type {mtype}"
    sport = _market_sport(market) if market else None
    if sport and sport not in SPORT_WHITELIST:
        return False, f"sport {sport}"
    return True, "ok"


def _open_paper_position(state: dict, positions: dict, trade: dict, market: dict) -> dict:
    price = float(trade.get("price", 0))
    # Buy shares at his entry + 2% taker fee → effective cost = FIXED_SIZE_USD
    # Shares acquired = FIXED_SIZE_USD / (price * (1 + fee))
    effective_price = price * (1 + TAKER_FEE)
    shares = FIXED_SIZE_USD / effective_price if effective_price > 0 else 0
    cost = shares * effective_price

    if state["cash_usd"] < cost:
        return {"skipped": "insufficient_cash", "needed": cost, "have": state["cash_usd"]}

    state["cash_usd"] -= cost
    asset = str(trade.get("asset"))
    pos = positions.get(asset)
    if pos:
        # average in
        total_shares = pos["shares"] + shares
        pos["shares"] = total_shares
        pos["cost_basis"] = pos["cost_basis"] + cost
        pos["avg_price"] = pos["cost_basis"] / total_shares if total_shares else 0
        pos["n_buys"] = pos.get("n_buys", 1) + 1
    else:
        positions[asset] = {
            "asset": asset,
            "condition_id": trade.get("conditionId"),
            "title": trade.get("title"),
            "outcome": trade.get("outcome"),
            "shares": shares,
            "avg_price": effective_price,
            "cost_basis": cost,
            "opened_ts": int(trade.get("timestamp", time.time())),
            "n_buys": 1,
        }
    return {
        "opened": True, "asset": asset, "shares": shares,
        "price": effective_price, "cost": cost,
    }


def _near_resolved_payout(
    asset: str, market: dict, threshold: float
) -> tuple[float, str] | None:
    """If the market is effectively decided (one token's price >= threshold,
    the other <= 1 - threshold), return (payout_per_share, outcome) for the
    held asset. None if the market is still genuinely live.

    payout uses current marker price → realistic exit value (matches what
    we'd get by selling on the book, à la RN1's Fulham 99.9¢ unwind).
    """
    tokens = market.get("tokens") or []
    if len(tokens) < 2:
        return None
    prices: dict[str, float] = {}
    for tok in tokens:
        tid = str(tok.get("token_id"))
        try:
            prices[tid] = float(tok.get("price"))
        except (TypeError, ValueError):
            return None
    if not prices:
        return None
    near_win = [(tid, p) for tid, p in prices.items() if p >= threshold]
    near_lose = [(tid, p) for tid, p in prices.items() if p <= (1 - threshold)]
    # Need exactly one winner AND one loser to call it decided
    if len(near_win) != 1 or len(near_lose) != 1:
        return None
    held_price = prices.get(asset)
    if held_price is None:
        return None
    if held_price >= threshold:
        return held_price, "won"
    if held_price <= (1 - threshold):
        return held_price, "lost"
    return None


# Threshold for near-resolve. 0.99 means a price of 0.9995/0.0005 fires it
# (typical Polymarket "game over, settling" state). Override via env.
_NEAR_RESOLVE_THRESHOLD = float(os.environ.get("PAPER_NEAR_RESOLVE_THRESHOLD", "0.99"))


def _resolve_positions(state: dict, positions: dict, markets: dict) -> list[dict]:
    """Realize PnL for closed markets AND for markets effectively decided
    (winner side priced >= threshold). The near-resolve path frees capital
    hours earlier than Polymarket's official close — wins/losses at 99.95¢
    are committed at current market price (slight haircut vs $1 redeem)."""
    realized = []
    for asset, pos in list(positions.items()):
        cid = pos.get("condition_id")
        market = markets.get(cid)
        if not market:
            continue

        payout_per_share: float | None = None
        outcome: str | None = None
        resolve_path = ""

        if market.get("closed"):
            win_tok = _winning_token(market)
            if win_tok is None:
                continue  # closed but no winner flag yet — wait next cycle
            payout_per_share = 1.0 if asset == win_tok else 0.0
            outcome = "won" if asset == win_tok else "lost"
            resolve_path = "closed"
        else:
            near = _near_resolved_payout(asset, market, _NEAR_RESOLVE_THRESHOLD)
            if near is None:
                continue
            payout_per_share, outcome = near
            resolve_path = "near"

        payout = pos["shares"] * payout_per_share
        pnl = payout - pos["cost_basis"]
        state["cash_usd"] += payout
        state["realized_pnl"] += pnl
        state["n_resolved"] += 1
        if outcome == "won":
            state["n_won"] += 1
        realized.append({
            "ts": int(time.time()),
            "action": "resolve",
            "asset": asset,
            "title": pos.get("title"),
            "outcome_bought": pos.get("outcome"),
            "shares": pos["shares"],
            "cost_basis": pos["cost_basis"],
            "payout": payout,
            "pnl": pnl,
            "result": outcome,
            "resolve_path": resolve_path,  # "closed" or "near"
        })
        del positions[asset]
    return realized


def _equity_snapshot(state: dict, positions: dict, markets: dict) -> dict:
    # MTM open positions at current best ask (approx via market token price if available)
    mtm = 0.0
    for asset, pos in positions.items():
        m = markets.get(pos.get("condition_id")) or {}
        price = None
        for tok in m.get("tokens", []):
            if str(tok.get("token_id")) == asset:
                p = tok.get("price")
                if p is not None:
                    try:
                        price = float(p)
                    except Exception:
                        pass
                break
        mtm += pos["shares"] * (price if price is not None else pos["avg_price"])
    equity = state["cash_usd"] + mtm
    return {
        "ts": int(time.time()),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "cash_usd": round(state["cash_usd"], 2),
        "open_positions_mtm": round(mtm, 2),
        "n_open": len(positions),
        "equity_usd": round(equity, 2),
        "realized_pnl": round(state["realized_pnl"], 2),
        "perf_pct": round((equity - INITIAL_CAPITAL_USD) / INITIAL_CAPITAL_USD * 100, 3),
        "n_resolved": state["n_resolved"],
        "n_won": state["n_won"],
        "win_rate": round(state["n_won"] / state["n_resolved"], 4) if state["n_resolved"] else 0,
    }


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRADES_PATH.exists():
        sys.exit(f"[error] {TRADES_PATH} missing. Run fetch_trades first.")

    state = _load_json(STATE_PATH, {
        "cash_usd": INITIAL_CAPITAL_USD,
        "realized_pnl": 0.0,
        "n_resolved": 0,
        "n_won": 0,
        "last_seen_ts": 0,
        "boot_ts": int(time.time()),
    })
    positions = _load_json(POSITIONS_PATH, {})
    markets = _load_markets()
    last_seen = int(state.get("last_seen_ts", 0))

    print(f"[paper] state: cash=${state['cash_usd']:.2f}, "
          f"{len(positions)} open positions, "
          f"{state['n_resolved']} resolved (won={state['n_won']}), "
          f"last_seen={last_seen}")
    print(f"[paper] filters: price∈[{MIN_PRICE}, {MAX_PRICE}], "
          f"sports={SPORT_WHITELIST}, market_types={MARKET_TYPE_WHITELIST}")

    # First : resolve any pending positions
    resolutions = _resolve_positions(state, positions, markets)
    for r in resolutions:
        _append_jsonl(TRADES_LOG_PATH, r)
        print(f"  RESOLVE {r['result']:<5} {r['title'][:40]:<40} "
              f"pnl=${r['pnl']:+.2f}")

    # Then : process new RN1 trades
    n_examined = 0
    n_opened = 0
    n_skipped = 0
    skip_reasons: dict[str, int] = {}

    with open(TRADES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
            except Exception:
                continue
            ts = int(trade.get("timestamp", 0))
            if ts <= last_seen:
                continue
            n_examined += 1
            market = markets.get(trade.get("conditionId"))
            accept, reason = _trade_passes_filters(trade, market)
            if not accept:
                n_skipped += 1
                skip_reasons[reason.split("(")[0].strip()] = skip_reasons.get(reason.split("(")[0].strip(), 0) + 1
                continue

            result = _open_paper_position(state, positions, trade, market)
            if result.get("opened"):
                n_opened += 1
                _append_jsonl(TRADES_LOG_PATH, {
                    "ts": ts,
                    "action": "buy",
                    "asset": result["asset"],
                    "title": trade.get("title"),
                    "outcome": trade.get("outcome"),
                    "shares": result["shares"],
                    "price_with_fee": result["price"],
                    "cost": result["cost"],
                })
            else:
                n_skipped += 1
                skip_reasons[result.get("skipped", "unknown")] = skip_reasons.get(result.get("skipped", "unknown"), 0) + 1

            state["last_seen_ts"] = max(state["last_seen_ts"], ts)

    print(f"[paper] examined={n_examined}, opened={n_opened}, skipped={n_skipped}")
    if skip_reasons:
        for reason, cnt in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            print(f"  skip {reason}: {cnt}")

    # 2nd resolution pass : positions we just opened may already be on closed
    # markets (we process trades from past 5 days, many already resolved).
    resolutions_2 = _resolve_positions(state, positions, markets)
    for r in resolutions_2:
        _append_jsonl(TRADES_LOG_PATH, r)
    if resolutions_2:
        n_won_2nd = sum(1 for r in resolutions_2 if r["result"] == "won")
        print(f"[paper] 2nd-pass resolved: {len(resolutions_2)} positions "
              f"({n_won_2nd} won, {len(resolutions_2)-n_won_2nd} lost)")

    # Save state + positions
    _save_json(STATE_PATH, state)
    _save_json(POSITIONS_PATH, positions)

    # Daily equity snapshot (1x/day, append-only)
    snap = _equity_snapshot(state, positions, markets)
    # Dedup : if today's already there, replace last line; else append
    today = snap["date"]
    last_date = None
    if EQUITY_PATH.exists():
        with open(EQUITY_PATH) as f:
            for line in f:
                if line.strip():
                    try:
                        last_date = json.loads(line).get("date")
                    except Exception:
                        pass
    if last_date == today:
        # rewrite the file replacing the last entry
        lines = []
        with open(EQUITY_PATH) as f:
            for line in f:
                if line.strip():
                    lines.append(line)
        if lines:
            lines[-1] = json.dumps(snap) + "\n"
        with open(EQUITY_PATH, "w") as f:
            f.writelines(lines)
    else:
        _append_jsonl(EQUITY_PATH, snap)

    print(f"[paper] equity snapshot: ${snap['equity_usd']} ({snap['perf_pct']:+.2f}%) "
          f"| {snap['n_open']} open | {snap['n_resolved']} resolved "
          f"({snap['win_rate']*100:.1f}% wr)")


if __name__ == "__main__":
    main()

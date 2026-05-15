"""bot-cp main runner — polling loop + orchestration."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_TRADE_PCT = 0.50
MIN_PAPER_SIZE_USD = 1.0


def compute_paper_size(
    trade_size_usd: float,
    target_aum: float,
    capital_per_wallet: float,
) -> float:
    """Return the paper-trade size (USD) to mirror a target's trade.

    Logic: trade_pct = trade_size_usd / target_aum, clamped to [0, MAX_TRADE_PCT].
    Returns 0 on invalid inputs (negative or zero AUM).
    """
    if trade_size_usd <= 0 or target_aum <= 0 or capital_per_wallet <= 0:
        return 0.0
    trade_pct = min(trade_size_usd / target_aum, MAX_TRADE_PCT)
    return capital_per_wallet * trade_pct

from live.copytrade import aum_estimator, data_api
from live.copytrade.paper_portfolio import PaperPortfolio


def process_wallet(
    target: dict,
    portfolio: PaperPortfolio,
    last_seen_ts: int,
    capital_per_wallet: float,
) -> tuple[int, list[dict]]:
    """Fetch new trades for target.wallet, mirror each into `portfolio`.

    Returns:
        (new_last_seen_ts, decisions) where decisions is a list of structured
        records (one per detected trade, including skipped).
    """
    wallet = target["wallet"]
    new_trades = data_api.trades(wallet, limit=50, since_ts=last_seen_ts)
    decisions: list[dict] = []
    if not new_trades:
        return last_seen_ts, decisions

    # process oldest first so dedup / position state evolves correctly
    for t in sorted(new_trades, key=lambda x: int(x["timestamp"])):
        ts = int(t["timestamp"])
        side = t.get("side")
        price = float(t.get("price", 0.0))
        size_shares = float(t.get("size", 0.0))
        # Polymarket's `size` is in outcome tokens (shares). USD = shares * price.
        trade_usd = size_shares * price
        condition_id = t.get("conditionId")
        outcome_index = int(t.get("outcomeIndex", 0))
        asset = t.get("asset", "")
        outcome = t.get("outcome", "")
        market_title = t.get("title", "")
        target_hash = t.get("transactionHash", "")

        target_aum = aum_estimator.aum(wallet)
        paper_size = compute_paper_size(trade_usd, target_aum, capital_per_wallet)

        decision = {
            "ts": ts, "wallet": target["pseudonym"], "target_hash": target_hash,
            "side": side, "market": market_title, "outcome": outcome,
            "target_size_usd": trade_usd, "target_aum_estimate": target_aum,
            "trade_pct": (trade_usd / target_aum) if target_aum else 0,
            "paper_size_usd": paper_size, "price": price,
        }

        if paper_size < MIN_PAPER_SIZE_USD:
            decision["action"] = "skipped"
            decision["rationale"] = (
                "paper_size_below_threshold" if paper_size > 0 else "zero_aum_or_zero_trade"
            )
            decisions.append(decision)
            last_seen_ts = max(last_seen_ts, ts)
            continue

        if side == "BUY":
            portfolio.buy(
                condition_id=condition_id, asset=asset, outcome=outcome,
                outcome_index=outcome_index, price=price, usd_size=paper_size,
                target_hash=target_hash, market_title=market_title, opened_ts=ts,
            )
            decision["action"] = "executed"
            decision["rationale"] = "buy_mirrored"
        elif side == "SELL":
            target_size_before = data_api.target_position_size_at(
                wallet, condition_id, outcome_index, ts=ts,
            )
            if target_size_before <= 0:
                fraction = 1.0
            else:
                fraction = min(size_shares / target_size_before, 1.0)
            portfolio.sell(
                condition_id=condition_id, outcome_index=outcome_index,
                fraction=fraction, price=price, target_hash=target_hash, ts=ts,
            )
            decision["action"] = "executed"
            decision["rationale"] = f"sell_mirrored_fraction={fraction:.4f}"
        else:
            decision["action"] = "skipped"
            decision["rationale"] = f"unknown_side={side}"

        decisions.append(decision)
        last_seen_ts = max(last_seen_ts, ts)

    return last_seen_ts, decisions

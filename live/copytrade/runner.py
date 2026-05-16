"""bot-cp main runner — polling loop + orchestration."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MAX_TRADE_PCT = 0.50
MIN_PAPER_SIZE_USD = 1.0
# Hard cap on how old a trade can be when the bot first boots a wallet (last_seen_ts == 0).
# Without this the first cycle replays days of historical trades, which inflates PnL
# with a retroactive mini-backtest the bot was never present for.
BOOTSTRAP_CUTOFF_S = 3600  # 1 hour


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
    import time as _time
    wallet = target["wallet"]
    # First-boot guard: if we've never seen this wallet, only consider trades
    # within the last BOOTSTRAP_CUTOFF_S seconds. Avoids replaying ancient
    # history as if it were happening now.
    effective_since = last_seen_ts
    if last_seen_ts == 0:
        effective_since = int(_time.time()) - BOOTSTRAP_CUTOFF_S
    new_trades = data_api.trades(wallet, limit=50, since_ts=effective_since)
    decisions: list[dict] = []
    if not new_trades:
        # Advance last_seen_ts to the cutoff so subsequent cycles use real time
        return max(last_seen_ts, effective_since), decisions

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
            # Cash check: refuse to overdraw. In live this is what Polymarket would do.
            if portfolio.cash_usd < paper_size:
                decision["action"] = "skipped"
                decision["rationale"] = (
                    f"insufficient_cash (have ${portfolio.cash_usd:.2f}, need ${paper_size:.2f})"
                )
                decisions.append(decision)
                last_seen_ts = max(last_seen_ts, ts)
                continue
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


import os
import signal
import sys
import time
from pathlib import Path

from live import notifier
from live.copytrade import state as state_mod
from live.copytrade.targets import (
    CAPITAL_PER_WALLET,
    PAPER_CAPITAL_USD,
    TARGETS,
)

LOG_DIR = Path(os.getenv("BOT_CP_LOG_DIR", "logs/copytrade"))
POLL_INTERVAL_S = int(os.getenv("BOT_CP_POLL_S", "60"))

_stop = False


def _signal_handler(signum, _frame):
    global _stop
    log.info("received signal %d, stopping after current cycle", signum)
    _stop = True


def _load_portfolios() -> dict[str, PaperPortfolio]:
    portfolio_path = LOG_DIR / "portfolio.json"
    raw = state_mod.load_portfolio(str(portfolio_path))
    out: dict[str, PaperPortfolio] = {}
    for t in TARGETS:
        pseudo = t["pseudonym"]
        if pseudo in raw:
            out[pseudo] = PaperPortfolio.from_dict(raw[pseudo])
        else:
            out[pseudo] = PaperPortfolio(wallet=pseudo, cash_usd=CAPITAL_PER_WALLET)
    return out


def _save_portfolios(portfolios: dict[str, PaperPortfolio]) -> None:
    portfolio_path = LOG_DIR / "portfolio.json"
    body = {pseudo: pf.to_dict() for pseudo, pf in portfolios.items()}
    state_mod.save_portfolio(str(portfolio_path), body)


# Track last-snapshotted UTC date so we append at most once per day
_last_equity_date: str | None = None
_last_reconcile_date: str | None = None


def _reconcile_resolved_once(portfolios: dict[str, PaperPortfolio]) -> int:
    """Close positions on resolved markets: credit/debit cash to reflect the
    final payoff (1.0 for winners, 0.0 for losers), record realized PnL,
    drop the position. Runs at most once per UTC day. Returns count closed.
    """
    from datetime import datetime, timezone

    global _last_reconcile_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_reconcile_date == today:
        return 0

    cond_ids = {
        p.get("condition_id")
        for pf in portfolios.values()
        for p in pf.positions
        if p.get("condition_id")
    }

    # Only accept strict resolution (price ∈ {0, 1}) — skip disputed/pending
    payoffs: dict[str, float] = {}
    for cid in cond_ids:
        try:
            m = data_api.market(cid)
            if not m or not m.get("closed"):
                continue
            for tok in m.get("tokens", []):
                tid = tok.get("token_id")
                px = tok.get("price")
                if tid is None or px is None:
                    continue
                fpx = float(px)
                if fpx == 0.0 or fpx == 1.0:
                    payoffs[tid] = fpx
        except Exception as e:
            log.warning("reconcile: market fetch failed for %s: %s", cid, e)

    if not payoffs:
        _last_reconcile_date = today
        return 0

    closed = 0
    now_ts = int(time.time())
    for pseudo, pf in portfolios.items():
        # snapshot list because pf.sell() mutates pf.positions
        for p in list(pf.positions):
            asset = p["asset"]
            if asset not in payoffs:
                continue
            cid = p.get("condition_id")
            oi = p.get("outcome_index")
            if cid is None or oi is None:
                continue
            payoff = payoffs[asset]
            pf.sell(
                condition_id=cid,
                outcome_index=oi,
                fraction=1.0,
                price=payoff,
                target_hash=f"resolved:{cid[:10]}",
                ts=now_ts,
            )
            closed += 1
            log.info(
                "reconcile %s: %s / %s payoff=%.2f cost=$%.2f",
                pseudo, str(p.get("market_title", "?"))[:50],
                p.get("outcome", "?"), payoff, p.get("cost_usd", 0.0),
            )

    _last_reconcile_date = today
    if closed:
        log.info("reconcile: closed %d resolved position(s)", closed)
    return closed


def _maybe_snapshot_equity(portfolios: dict[str, PaperPortfolio]) -> None:
    """Append a daily MTM snapshot to equity.jsonl when the UTC date changes.

    Pricing waterfall per asset:
      1. CLOB live bid (SELL side) — for open markets
      2. CLOB market resolution payoff (0.0 / 1.0) — for closed markets
      3. avg_price — fallback when both endpoints fail
    """
    from datetime import datetime, timezone

    global _last_equity_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_equity_date == today:
        return

    assets = {p["asset"] for pf in portfolios.values() for p in pf.positions}
    current_prices: dict[str, float] = {}

    n_live = 0
    for asset in assets:
        try:
            px = data_api.price(asset, side="SELL")
            if px is not None:
                current_prices[asset] = px
                n_live += 1
        except Exception as e:
            log.debug("live price miss for asset %s (likely resolved): %s", asset, e)

    n_resolved = 0
    missing = assets - current_prices.keys()
    if missing:
        asset_to_cond = {
            p["asset"]: p.get("condition_id")
            for pf in portfolios.values()
            for p in pf.positions
            if p["asset"] in missing and p.get("condition_id")
        }
        for cond_id in set(asset_to_cond.values()):
            try:
                m = data_api.market(cond_id)
                if not m or not m.get("closed"):
                    continue
                for tok in m.get("tokens", []):
                    tid = tok.get("token_id")
                    px = tok.get("price")
                    if tid in missing and px is not None:
                        current_prices[tid] = float(px)
                        n_resolved += 1
            except Exception as e:
                log.warning("market fetch failed for cond %s: %s", cond_id, e)

    per_wallet = {}
    total = 0.0
    for pseudo, pf in portfolios.items():
        eq = pf.equity(current_prices)
        per_wallet[pseudo] = eq
        total += eq

    state_mod.append_equity(str(LOG_DIR / "equity.jsonl"), {
        "ts": int(time.time()),
        "date": today,
        "per_wallet_eq": per_wallet,
        "total_eq": total,
    })
    _last_equity_date = today
    n_fallback = len(assets) - n_live - n_resolved
    log.info(
        "equity snapshot for %s: total=$%.2f (live %d, resolved %d, fallback %d / %d)",
        today, total, n_live, n_resolved, n_fallback, len(assets),
    )


def _smoke_test() -> None:
    """Hit Data API once to fail-fast on geoblock / network at boot."""
    test_wallet = TARGETS[0]["wallet"]
    try:
        data_api.trades(test_wallet, limit=1)
        log.info("smoke test ok: Data API reachable")
    except data_api.DataAPIError as e:
        log.error("smoke test failed: %s — aborting", e)
        sys.exit(2)


def run() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    log.info("bot-cp starting: capital=$%.2f, %d wallets, poll=%ds",
             PAPER_CAPITAL_USD, len(TARGETS), POLL_INTERVAL_S)
    try:
        notifier.notify(
            f"🟢 bot-cp démarré — capital ${PAPER_CAPITAL_USD:.0f}, "
            f"{len(TARGETS)} wallets ({', '.join(t['pseudonym'] for t in TARGETS)})"
        )
    except Exception:
        log.exception("telegram start notify failed (non-fatal)")
    _smoke_test()

    state = state_mod.load_state(str(LOG_DIR / "state.json"))
    last_seen = state.get("last_seen_ts", {})
    portfolios = _load_portfolios()

    decisions_path = str(LOG_DIR / "decisions.jsonl")

    while not _stop:
        cycle_start = time.time()
        for t in TARGETS:
            pseudo = t["pseudonym"]
            wallet = t["wallet"]
            try:
                new_ts, decisions = process_wallet(
                    t, portfolios[pseudo],
                    last_seen_ts=int(last_seen.get(wallet, 0)),
                    capital_per_wallet=CAPITAL_PER_WALLET,
                )
                for d in decisions:
                    state_mod.append_decision(decisions_path, d)
                    if d.get("action") != "executed":
                        continue
                    try:
                        side_emoji = "🟩" if d["side"] == "BUY" else "🟥"
                        notifier.notify(
                            f"{side_emoji} CP {pseudo} {d['side']} "
                            f"{d.get('market', '?')[:50]} / {d.get('outcome', '?')}\n"
                            f"size ${d['paper_size_usd']:.2f} @ {d['price']:.3f} "
                            f"(target ${d['target_size_usd']:.0f} / "
                            f"{d['trade_pct']*100:.1f}% AUM)"
                        )
                    except Exception:
                        log.exception("telegram trade notify failed (non-fatal)")
                if new_ts > int(last_seen.get(wallet, 0)):
                    last_seen[wallet] = new_ts
                if decisions:
                    log.info("%s: %d new trade(s) processed", pseudo, len(decisions))
            except Exception:
                log.exception("error processing %s, continuing", pseudo)

        # Persist after each cycle
        state_mod.save_state(str(LOG_DIR / "state.json"), {"last_seen_ts": last_seen})
        _save_portfolios(portfolios)

        # Daily: close resolved positions (realize PnL), then snapshot equity
        if _reconcile_resolved_once(portfolios) > 0:
            _save_portfolios(portfolios)
        _maybe_snapshot_equity(portfolios)

        # Sleep, but check stop flag every second
        elapsed = time.time() - cycle_start
        remaining = max(0.0, POLL_INTERVAL_S - elapsed)
        slept = 0.0
        while slept < remaining and not _stop:
            time.sleep(1.0)
            slept += 1.0

    try:
        notifier.notify("🛑 bot-cp arrêté")
    except Exception:
        log.exception("telegram stop notify failed (non-fatal)")

    log.info("bot-cp stopped cleanly")


if __name__ == "__main__":
    run()

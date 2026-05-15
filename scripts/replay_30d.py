#!/usr/bin/env python3
"""Replay last 30 days of target trades to compute a retroactive paper equity
curve. Used as a sanity check BEFORE running the bot live-forward.

Usage:
    python scripts/replay_30d.py

Writes a `replay_equity.csv` and `replay_decisions.jsonl` next to the script
output dir (default: `backtest/results/copytrade/`).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from live.copytrade import data_api, runner
from live.copytrade.paper_portfolio import PaperPortfolio
from live.copytrade.targets import CAPITAL_PER_WALLET, TARGETS

log = logging.getLogger("replay")

OUT_DIR = Path("backtest/results/copytrade")
WINDOW_S = 30 * 24 * 3600


def replay() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = int(time.time()) - WINDOW_S
    portfolios = {t["pseudonym"]: PaperPortfolio(wallet=t["pseudonym"],
                                                  cash_usd=CAPITAL_PER_WALLET)
                  for t in TARGETS}

    decisions_log = OUT_DIR / "replay_decisions.jsonl"
    if decisions_log.exists():
        decisions_log.unlink()

    for t in TARGETS:
        all_trades = data_api.trades(t["wallet"], limit=500)
        recent = [tr for tr in all_trades if int(tr["timestamp"]) >= cutoff]
        log.info("%s: %d trades in last 30d", t["pseudonym"], len(recent))
        for tr in sorted(recent, key=lambda x: int(x["timestamp"])):
            ts = int(tr["timestamp"])
            target_aum = data_api.value(t["wallet"]) or 1.0
            paper_size = runner.compute_paper_size(
                float(tr.get("size", 0)) * float(tr.get("price", 0)),
                target_aum,
                CAPITAL_PER_WALLET,
            )
            if paper_size < runner.MIN_PAPER_SIZE_USD:
                continue
            pf = portfolios[t["pseudonym"]]
            if tr["side"] == "BUY":
                pf.buy(
                    condition_id=tr["conditionId"], asset=tr.get("asset", ""),
                    outcome=tr.get("outcome", ""), outcome_index=int(tr.get("outcomeIndex", 0)),
                    price=float(tr["price"]), usd_size=paper_size,
                    target_hash=tr.get("transactionHash", ""),
                    market_title=tr.get("title", ""), opened_ts=ts,
                )
            elif tr["side"] == "SELL":
                size_before = data_api.target_position_size_at(
                    t["wallet"], tr["conditionId"],
                    int(tr.get("outcomeIndex", 0)), ts,
                )
                frac = (float(tr["size"]) / size_before) if size_before else 1.0
                pf.sell(condition_id=tr["conditionId"],
                        outcome_index=int(tr.get("outcomeIndex", 0)),
                        fraction=min(frac, 1.0), price=float(tr["price"]),
                        target_hash=tr.get("transactionHash", ""), ts=ts)
            with open(decisions_log, "a") as f:
                f.write(json.dumps({
                    "ts": ts, "wallet": t["pseudonym"], "side": tr["side"],
                    "paper_size_usd": paper_size,
                    "cash_usd_after": pf.cash_usd,
                    "n_positions_after": len(pf.positions),
                }) + "\n")

    # Final equity (uses avg_price fallback for unknown current prices to keep
    # the script self-contained; the live runner uses real CLOB prices).
    total = 0.0
    print(f"\n{'Wallet':<15} {'Cash':>10} {'Positions':>10} {'Equity':>10}")
    for pseudo, pf in portfolios.items():
        eq = pf.equity({})
        total += eq
        print(f"{pseudo:<15} {pf.cash_usd:>10.2f} {len(pf.positions):>10} {eq:>10.2f}")
    initial = CAPITAL_PER_WALLET * len(TARGETS)
    print(f"\nTOTAL equity:   ${total:.2f}  (initial ${initial:.2f}, "
          f"PnL {(total - initial) / initial * 100:+.2f}%)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    replay()

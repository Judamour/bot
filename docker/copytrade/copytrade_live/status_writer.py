"""Snapshot writer for the dashboard.

Writes a single JSON file with the full live state at each cycle, so the
host dashboard (running as botuser, no docker access) can read it.

Path: /decisions/copytrade_live_status.json (host bind mount, world-readable).
"""
import json
import os
import time
from pathlib import Path

from . import config

STATUS_PATH = Path(os.getenv(
    "COPYTRADE_STATUS_PATH",
    "/decisions/copytrade_live_status.json",
))


def write_status(
    meta: dict,
    positions: dict,
    *,
    clob_balance_usd: float | None = None,
    cycle_count: int = 0,
    last_cycle_decisions: int = 0,
    last_cycle_executed: int = 0,
) -> None:
    cost_basis = sum(p.get("cost_usd", 0) for p in positions.values())
    snapshot = {
        "ts": int(time.time()),
        "wallet_address": config.FUNDER,
        "target_wallet": config.TARGET_WALLET,
        "mode": "live" if not config.DRY_RUN else "dry_run",
        "clob_balance_usd": clob_balance_usd,
        "n_positions": len(positions),
        "positions_cost_basis_usd": cost_basis,
        "equity_estimate_usd": (clob_balance_usd or 0) + cost_basis,
        "last_seen_ts": meta.get("last_seen_ts", 0),
        "cycle_count": cycle_count,
        "last_cycle_decisions": last_cycle_decisions,
        "last_cycle_executed": last_cycle_executed,
        "filters": {
            "fixed_size_usd": config.FIXED_SIZE_USD,
            "max_positions": config.MAX_POSITIONS,
            "min_target_size_usd": config.MIN_TARGET_SIZE_USD,
            "min_entry_price": config.MIN_ENTRY_PRICE,
            "max_entry_price": config.MAX_ENTRY_PRICE,
            "max_price_drift": config.MAX_PRICE_DRIFT,
            "max_usd_per_market": config.MAX_USD_PER_MARKET,
            "kill_equity_usd": config.KILL_EQUITY_USD,
        },
        "positions": [
            {
                "token_id": tid,
                "market": p.get("market"),
                "outcome": p.get("outcome"),
                "size_shares": p.get("size_shares"),
                "avg_price": p.get("avg_price"),
                "cost_usd": p.get("cost_usd"),
                "opened_ts": p.get("opened_ts"),
            }
            for tid, p in positions.items()
        ],
    }
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    os.replace(tmp, STATUS_PATH)

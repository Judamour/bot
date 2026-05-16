"""Atomic state persistence.

state.json    — { last_seen_ts: int }
positions.json — { token_id: PositionRecord }

Atomic writes: write to .tmp + os.replace (atomic on POSIX).
At boot: reconcile positions.json against Polymarket /positions (source of truth).
"""
import json
import os
import time
from pathlib import Path
from . import config


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def load_meta() -> dict:
    if not config.STATE_PATH.exists():
        return {"last_seen_ts": 0}
    try:
        return json.loads(config.STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"last_seen_ts": 0}


def save_meta(meta: dict) -> None:
    _atomic_write(config.STATE_PATH, meta)


def load_positions() -> dict:
    if not config.POSITIONS_PATH.exists():
        return {}
    try:
        return json.loads(config.POSITIONS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_positions(positions: dict) -> None:
    _atomic_write(config.POSITIONS_PATH, positions)


def record_buy(positions: dict, token_id: str, *, market: str, outcome: str,
               size_shares: float, avg_price: float, cost_usd: float,
               target_hash: str, condition_id: str = "") -> dict:
    existing = positions.get(token_id)
    if existing:
        new_size = existing["size_shares"] + size_shares
        new_cost = existing["cost_usd"] + cost_usd
        existing.update({
            "size_shares": new_size,
            "cost_usd": new_cost,
            "avg_price": new_cost / new_size if new_size else avg_price,
            "last_buy_ts": int(time.time()),
            "target_hashes": existing.get("target_hashes", []) + [target_hash],
        })
        positions[token_id] = existing
    else:
        positions[token_id] = {
            "token_id": token_id,
            "market": market,
            "outcome": outcome,
            "condition_id": condition_id,
            "size_shares": size_shares,
            "avg_price": avg_price,
            "cost_usd": cost_usd,
            "opened_ts": int(time.time()),
            "last_buy_ts": int(time.time()),
            "target_hashes": [target_hash],
        }
    return positions[token_id]


def record_sell(positions: dict, token_id: str, *, size_shares: float,
                exit_price: float, exit_ts: int | None = None) -> tuple[dict | None, float]:
    """Returns (updated_position_or_None, realized_pnl_usd).

    If size_shares >= existing, position is removed and full PnL realized.
    Otherwise position is partially reduced proportionally.
    """
    pos = positions.get(token_id)
    if not pos:
        return None, 0.0
    ts = exit_ts or int(time.time())
    sell_proceeds = size_shares * exit_price
    if size_shares >= pos["size_shares"] - 1e-9:
        cost_basis = pos["cost_usd"]
        realized = sell_proceeds - cost_basis
        del positions[token_id]
        return None, realized
    fraction_sold = size_shares / pos["size_shares"]
    cost_basis = pos["cost_usd"] * fraction_sold
    realized = sell_proceeds - cost_basis
    pos["size_shares"] -= size_shares
    pos["cost_usd"] -= cost_basis
    pos["last_sell_ts"] = ts
    positions[token_id] = pos
    return pos, realized


def equity_snapshot(positions: dict, cash_usd: float, mtm_prices: dict[str, float]) -> dict:
    mtm_value = sum(
        pos["size_shares"] * mtm_prices.get(token_id, pos["avg_price"])
        for token_id, pos in positions.items()
    )
    return {
        "ts": int(time.time()),
        "cash_usd": cash_usd,
        "positions_mtm_usd": mtm_value,
        "equity_usd": cash_usd + mtm_value,
        "n_positions": len(positions),
    }

"""Live view of the target wallets we're copytrading.

Pulls fresh data from data-api.polymarket.com (read-only, public) for each
target. Per-target view: AUM, open positions (with current MTM PnL), recent
trades. Aggregated activity feed across all targets sorted by timestamp.

Cached 45s per target to avoid hammering the API on each dashboard refresh.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from live.copytrade import data_api
from live.copytrade.targets import TARGETS

log = logging.getLogger(__name__)

_CACHE_TTL_S = 45.0
_cache: dict[str, tuple[float, dict]] = {}


def _fetch_one(target: dict) -> dict:
    wallet = target["wallet"]
    pseudo = target["pseudonym"]

    out: dict[str, Any] = {
        "pseudonym": pseudo,
        "wallet": wallet,
        "profile_url": f"https://polymarket.com/profile/{wallet}",
        "value_usd": 0.0,
        "open_positions": [],
        "open_count": 0,
        "recent_trades": [],
        "trades_24h": 0,
        "volume_24h_usd": 0.0,
        "errors": [],
    }

    try:
        out["value_usd"] = data_api.value(wallet)
    except Exception as e:
        out["errors"].append(f"value: {e}")

    try:
        positions = data_api.positions(wallet) or []
        # Sort by currentValue desc and keep the meaningful entries
        positions.sort(key=lambda p: float(p.get("currentValue", 0) or 0), reverse=True)
        slim = []
        for p in positions[:20]:
            slim.append({
                "title": p.get("title", "?"),
                "outcome": p.get("outcome", "?"),
                "size": float(p.get("size", 0) or 0),
                "avg_price": float(p.get("avgPrice", 0) or 0),
                "cur_price": float(p.get("curPrice", 0) or 0),
                "current_value": float(p.get("currentValue", 0) or 0),
                "cash_pnl": float(p.get("cashPnl", 0) or 0),
                "percent_pnl": float(p.get("percentPnl", 0) or 0),
                "end_date": p.get("endDate"),
                "slug": p.get("slug"),
            })
        out["open_positions"] = slim
        out["open_count"] = len(positions)
    except Exception as e:
        out["errors"].append(f"positions: {e}")

    try:
        now = int(time.time())
        cutoff_24h = now - 24 * 3600
        trades = data_api.trades(wallet, limit=50) or []
        trades.sort(key=lambda t: int(t.get("timestamp", 0)), reverse=True)
        slim_trades = []
        v24 = 0.0
        c24 = 0
        for t in trades[:20]:
            ts = int(t.get("timestamp", 0))
            sz = float(t.get("size", 0) or 0)
            px = float(t.get("price", 0) or 0)
            usd = sz * px
            slim_trades.append({
                "ts": ts,
                "side": t.get("side", "?"),
                "title": t.get("title", "?"),
                "outcome": t.get("outcome", "?"),
                "size_shares": sz,
                "price": px,
                "usd": usd,
                "tx_hash": t.get("transactionHash"),
                "slug": t.get("slug"),
            })
        for t in trades:
            if int(t.get("timestamp", 0)) >= cutoff_24h:
                c24 += 1
                v24 += float(t.get("size", 0) or 0) * float(t.get("price", 0) or 0)
        out["recent_trades"] = slim_trades
        out["trades_24h"] = c24
        out["volume_24h_usd"] = v24
    except Exception as e:
        out["errors"].append(f"trades: {e}")

    return out


def _get_target(target: dict) -> dict:
    pseudo = target["pseudonym"]
    cached = _cache.get(pseudo)
    now = time.time()
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1]
    data = _fetch_one(target)
    _cache[pseudo] = (now, data)
    return data


def build_targets() -> dict:
    """Aggregate view for the Targets tab."""
    targets = [_get_target(t) for t in TARGETS]

    # Combined activity feed: merge all recent_trades, sort by ts desc
    feed: list[dict] = []
    for t in targets:
        for tr in t["recent_trades"]:
            feed.append({**tr, "pseudonym": t["pseudonym"]})
    feed.sort(key=lambda x: x["ts"], reverse=True)
    feed = feed[:30]

    totals = {
        "aum_total_usd": sum(t["value_usd"] for t in targets),
        "open_total": sum(t["open_count"] for t in targets),
        "trades_24h_total": sum(t["trades_24h"] for t in targets),
        "volume_24h_total_usd": sum(t["volume_24h_usd"] for t in targets),
    }

    return {"targets": targets, "feed": feed, "totals": totals}

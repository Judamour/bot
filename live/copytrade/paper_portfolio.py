"""Per-wallet paper portfolio: cash + open positions + realized PnL.

A position is keyed by (condition_id, outcome_index). Adding to an existing
position averages the price. Selling reduces proportionally and realizes
the difference vs avg_price.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PaperPortfolio:
    wallet: str
    cash_usd: float
    positions: list[dict] = field(default_factory=list)
    realized_pnl_usd: float = 0.0

    def _find(self, condition_id: str, outcome_index: int) -> dict | None:
        for p in self.positions:
            if p["condition_id"] == condition_id and p["outcome_index"] == outcome_index:
                return p
        return None

    def buy(
        self,
        *,
        condition_id: str,
        asset: str,
        outcome: str,
        outcome_index: int,
        price: float,
        usd_size: float,
        target_hash: str,
        market_title: str,
        opened_ts: int,
    ) -> None:
        """Buy `usd_size` USD worth at `price` per outcome token."""
        if price <= 0 or usd_size <= 0:
            return
        shares = usd_size / price
        self.cash_usd -= usd_size
        existing = self._find(condition_id, outcome_index)
        if existing:
            new_size = existing["size"] + shares
            new_cost = existing["cost_usd"] + usd_size
            existing["size"] = new_size
            existing["cost_usd"] = new_cost
            existing["avg_price"] = new_cost / new_size if new_size else 0.0
            existing["target_hashes"].append(target_hash)
        else:
            self.positions.append({
                "condition_id": condition_id,
                "asset": asset,
                "outcome": outcome,
                "outcome_index": outcome_index,
                "market_title": market_title,
                "size": shares,
                "avg_price": price,
                "cost_usd": usd_size,
                "opened_ts": opened_ts,
                "target_hashes": [target_hash],
            })

    def sell(
        self,
        *,
        condition_id: str,
        outcome_index: int,
        fraction: float,
        price: float,
        target_hash: str,
        ts: int,
    ) -> None:
        """Sell `fraction` (clamped to [0,1]) of the position at `price`."""
        existing = self._find(condition_id, outcome_index)
        if not existing:
            return
        frac = max(0.0, min(1.0, fraction))
        shares_sold = existing["size"] * frac
        cost_sold = existing["cost_usd"] * frac
        proceeds = shares_sold * price
        self.cash_usd += proceeds
        self.realized_pnl_usd += proceeds - cost_sold
        existing["size"] -= shares_sold
        existing["cost_usd"] -= cost_sold
        existing["target_hashes"].append(target_hash)
        existing["last_sell_ts"] = ts
        # Remove if fully closed (size effectively zero)
        if existing["size"] < 1e-9:
            self.positions.remove(existing)

    def equity(self, current_prices: dict[str, float]) -> float:
        """MTM equity = cash + Σ size × (current price OR avg_price fallback)."""
        eq = self.cash_usd
        for p in self.positions:
            px = current_prices.get(p["asset"])
            if px is None:
                px = p["avg_price"]
            eq += p["size"] * px
        return eq

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PaperPortfolio":
        return cls(
            wallet=d["wallet"],
            cash_usd=float(d["cash_usd"]),
            positions=list(d.get("positions", [])),
            realized_pnl_usd=float(d.get("realized_pnl_usd", 0.0)),
        )

"""Simulation de fills broker pour le shadow bot.

Pas d'API broker. Les fills utilisent le prix Alpaca/Binance courant +
slippage simulé + frais simulés. Track les positions, équity, trailing stops.
"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass, field
import json
import os

INITIAL_CAPITAL = 100_000.0
SLIPPAGE = 0.001       # 0.1%
FEE = 0.0026           # 0.26% taker
RISK_PER_TRADE = 0.01  # 1% du capital risqué par trade
MAX_POSITION_PCT = 0.10  # max 10% capital par position
MAX_OPEN_POSITIONS = 10


@dataclass
class Position:
    symbol: str
    strategy: str
    entry_price: float
    size: float
    stop: float
    initial_stop: float
    entry_date: str
    atr_at_entry: float
    rationale: dict = field(default_factory=dict)


def load_state(state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "initial_capital": INITIAL_CAPITAL,
        "positions": {},
        "trades": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(state: dict, state_path: str):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def open_position(state: dict, signal, current_price: float) -> dict | None:
    """Tente d'ouvrir une position. Retourne dict trade ou None si refus."""
    if signal.symbol in state["positions"]:
        return None
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return None

    # Risk parity sizing : 1% du capital risqué
    risk_eur = state["capital"] * RISK_PER_TRADE
    stop_dist = abs(signal.entry_price - signal.stop_price)
    if stop_dist <= 0:
        return None
    size = risk_eur / stop_dist
    # Cap par capital max
    max_size = (state["capital"] * MAX_POSITION_PCT) / signal.entry_price
    size = min(size, max_size)
    if size <= 0:
        return None

    fill_price = current_price * (1 + SLIPPAGE)
    cost = fill_price * size
    fee = cost * FEE
    total = cost + fee
    if total > state["capital"]:
        return None

    state["capital"] -= total
    state["positions"][signal.symbol] = {
        "strategy": signal.strategy,
        "entry_price": fill_price,
        "size": size,
        "stop": signal.stop_price,
        "initial_stop": signal.stop_price,
        "entry_date": datetime.now(timezone.utc).isoformat(),
        "atr_at_entry": signal.atr,
        "rationale": signal.rationale,
        "score": signal.score,
    }
    return {
        "action": "buy",
        "symbol": signal.symbol,
        "strategy": signal.strategy,
        "score": round(signal.score, 1),
        "size": round(size, 6),
        "price": round(fill_price, 4),
        "stop": round(signal.stop_price, 4),
        "risk_eur": round(risk_eur, 2),
        "fee": round(fee, 4),
        "capital_after": round(state["capital"], 2),
    }


def close_position(state: dict, symbol: str, current_price: float, reason: str) -> dict | None:
    """Ferme une position. Retourne dict trade ou None si symbol absent."""
    pos = state["positions"].pop(symbol, None)
    if not pos:
        return None
    fill_price = current_price * (1 - SLIPPAGE)
    proceeds = fill_price * pos["size"]
    fee = proceeds * FEE
    net = proceeds - fee
    state["capital"] += net
    pnl = net - (pos["entry_price"] * pos["size"])
    pnl_pct = (fill_price - pos["entry_price"]) / pos["entry_price"]
    state["trades"].append({
        "symbol": symbol, "strategy": pos["strategy"],
        "entry": pos["entry_price"], "exit": fill_price,
        "size": pos["size"],
        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct * 100, 2),
        "entry_date": pos["entry_date"],
        "exit_date": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "score": pos.get("score", 0),
    })
    return {
        "action": "sell",
        "symbol": symbol,
        "strategy": pos["strategy"],
        "price": round(fill_price, 4),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct * 100, 2),
        "reason": reason,
        "capital_after": round(state["capital"], 2),
    }


def update_trailing(state: dict, symbol: str, current_price: float, atr: float) -> bool:
    """Met à jour le trailing stop si nouveau high. Retourne True si modifié."""
    pos = state["positions"].get(symbol)
    if not pos:
        return False
    new_stop = round(current_price - 4 * atr, 4)
    if new_stop > pos["stop"]:
        pos["stop"] = new_stop
        return True
    return False


def check_stop(state: dict, symbol: str, low_price: float) -> bool:
    """True si le stop a été touché (low <= stop)."""
    pos = state["positions"].get(symbol)
    if not pos:
        return False
    return low_price <= pos["stop"]


def equity(state: dict, prices: dict) -> float:
    """Equity total : cash + valeur de marché des positions."""
    eq = state["capital"]
    for sym, pos in state["positions"].items():
        price = prices.get(sym, pos["entry_price"])
        eq += price * pos["size"]
    return eq

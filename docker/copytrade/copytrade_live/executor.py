"""Polymarket CLOB executor — signe et soumet les ordres BUY/SELL.

En mode DRY_RUN, log uniquement sans soumettre.
"""
import logging
import time
from typing import Optional

import httpx

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    AssetType,
    OrderArgs,
)
from py_clob_client.order_builder.constants import BUY, SELL

from . import config

log = logging.getLogger(__name__)


_client: Optional[ClobClient] = None


def get_client() -> ClobClient:
    """Lazy-init un singleton ClobClient avec creds dérivés ou fournis."""
    global _client
    if _client is not None:
        return _client
    config.validate()
    creds = ApiCreds(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        api_passphrase=config.API_PASSPHRASE,
    )
    _client = ClobClient(
        host=config.POLYMARKET_HOST,
        chain_id=config.POLYMARKET_CHAIN_ID,
        key=config.PRIVATE_KEY,
        creds=creds,
        signature_type=config.POLYMARKET_SIG_TYPE,
        funder=config.FUNDER,
    )
    return _client


def get_clob_balance_usd() -> float:
    """Solde collateral USDC visible côté CLOB (en USD)."""
    client = get_client()
    bal = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=config.POLYMARKET_SIG_TYPE,
        )
    )
    return int(bal.get("balance", "0")) / 1e6


def resolve_outcome_to_token_id(market_title: str, outcome: str) -> Optional[dict]:
    """Résout (market_title, outcome) → {token_id, condition_id, price} via Gamma API.

    Renvoie None si non trouvé ou ambigu.
    """
    try:
        r = httpx.get(
            f"{config.GAMMA_API}/markets",
            params={"limit": 5, "search": market_title[:80]},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Gamma search {r.status_code} pour '{market_title[:50]}'")
            return None
        markets = r.json()
    except Exception as e:
        log.warning(f"Gamma search erreur: {e}")
        return None

    norm_title = market_title.strip().lower()
    norm_outcome = outcome.strip().lower()

    for m in markets:
        if m.get("question", "").strip().lower() != norm_title:
            continue
        outcomes = m.get("outcomes")
        token_ids = m.get("clobTokenIds")
        if isinstance(outcomes, str):
            import json as _j
            outcomes = _j.loads(outcomes)
        if isinstance(token_ids, str):
            import json as _j
            token_ids = _j.loads(token_ids)
        if not (outcomes and token_ids):
            continue
        for idx, outc in enumerate(outcomes):
            if outc.strip().lower() == norm_outcome and idx < len(token_ids):
                return {
                    "token_id": token_ids[idx],
                    "condition_id": m.get("conditionId", ""),
                    "outcome_index": idx,
                    "market_slug": m.get("slug", ""),
                }
    return None


def get_market_price(token_id: str, side: str) -> Optional[float]:
    """Récupère le best price actuel pour un side (BUY = best ask, SELL = best bid)."""
    client = get_client()
    try:
        return float(client.get_price(token_id=token_id, side=side))
    except Exception as e:
        log.warning(f"get_price échec token={token_id[:10]}...: {e}")
        return None


def place_buy(token_id: str, size_usd: float, max_price: float = 0.99) -> dict:
    """Place un BUY market-ish (limite au max_price). Renvoie le résultat brut."""
    client = get_client()
    ask = get_market_price(token_id, "buy")
    if ask is None:
        return {"status": "no_price", "token_id": token_id}
    if ask > max_price:
        return {"status": "price_too_high", "ask": ask, "max": max_price}
    size_shares = round(size_usd / ask, 2)
    if size_shares < 5:
        size_shares = 5  # min order size Polymarket
    if config.DRY_RUN:
        log.info(f"[DRY] BUY {size_shares:.2f} @ {ask:.4f} (${size_shares*ask:.2f}) tok={token_id[:10]}...")
        return {"status": "dry_run", "side": "BUY", "size_shares": size_shares,
                "price": ask, "cost_usd": size_shares * ask, "token_id": token_id}
    try:
        order_args = OrderArgs(price=ask, size=size_shares, side=BUY, token_id=token_id)
        signed = client.create_order(order_args)
        resp = client.post_order(signed)
        log.info(f"BUY submit: {resp}")
        return {"status": "submitted", "side": "BUY", "size_shares": size_shares,
                "price": ask, "cost_usd": size_shares * ask, "token_id": token_id, "resp": resp}
    except Exception as e:
        log.error(f"BUY échec: {type(e).__name__}: {e}")
        return {"status": "error", "error": str(e), "token_id": token_id}


def place_sell(token_id: str, size_shares: float, min_price: float = 0.01) -> dict:
    """Place un SELL au best bid actuel."""
    client = get_client()
    bid = get_market_price(token_id, "sell")
    if bid is None:
        return {"status": "no_price", "token_id": token_id}
    if bid < min_price:
        return {"status": "price_too_low", "bid": bid, "min": min_price}
    size_shares = round(size_shares, 2)
    if config.DRY_RUN:
        log.info(f"[DRY] SELL {size_shares:.2f} @ {bid:.4f} (${size_shares*bid:.2f}) tok={token_id[:10]}...")
        return {"status": "dry_run", "side": "SELL", "size_shares": size_shares,
                "price": bid, "proceeds_usd": size_shares * bid, "token_id": token_id}
    try:
        order_args = OrderArgs(price=bid, size=size_shares, side=SELL, token_id=token_id)
        signed = client.create_order(order_args)
        resp = client.post_order(signed)
        log.info(f"SELL submit: {resp}")
        return {"status": "submitted", "side": "SELL", "size_shares": size_shares,
                "price": bid, "proceeds_usd": size_shares * bid, "token_id": token_id, "resp": resp}
    except Exception as e:
        log.error(f"SELL échec: {type(e).__name__}: {e}")
        return {"status": "error", "error": str(e), "token_id": token_id}

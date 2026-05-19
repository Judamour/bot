"""Polymarket CLOB v2 executor — signs and submits BUY/SELL orders.

In DRY_RUN mode, logs only without submitting.

Migrated 2026-05-18 to py_clob_client_v2 + sig_type=3 (POLY_1271, EIP-1271 smart
contract signature) after Polymarket CLOB v2 migration end of April 2026 broke
the legacy v1 flow (order_version_mismatch + maker address not allowed).
"""
import logging
import time
from typing import Optional

import httpx

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    AssetType,
    OrderArgs,
    PartialCreateOrderOptions,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

from . import config

log = logging.getLogger(__name__)


_client: Optional[ClobClient] = None
# Cache of {token_id: {neg_risk: bool, tick_size: str}} so we don't re-fetch
# per-order; CLOB metadata doesn't change during a market's lifetime.
_market_meta_cache: dict = {}


def get_client() -> ClobClient:
    """Lazy-init a singleton ClobClient with provided or derived creds."""
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
    """Solde collateral USDC visible côté CLOB (en USD).

    NOTE v2: Polymarket v2 uses a shared deposit vault (~0x4cd0…bc31) with
    off-chain accounting; the on-chain proxy/funder balance is $0 even when
    the user has cash. The reliable source of truth is the Polymarket UI /
    data-api `value` endpoint, not get_balance_allowance. This call may
    return 0 even when orders succeed.
    """
    client = get_client()
    bal = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=config.POLYMARKET_SIG_TYPE,
        )
    )
    return int(bal.get("balance", "0")) / 1e6


def resolve_outcome_to_token_id(market_title: str, outcome: str) -> Optional[dict]:
    """Résout (market_title, outcome) → {token_id, condition_id, ...} via Gamma API.

    Also captures neg_risk + min_tick_size which v2 ordering requires.
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
                token_id = token_ids[idx]
                # Cache market meta from this Gamma response
                _market_meta_cache[token_id] = {
                    "neg_risk": bool(m.get("negRisk", False)),
                    "tick_size": str(m.get("minimumTickSize") or "0.01"),
                }
                return {
                    "token_id": token_id,
                    "condition_id": m.get("conditionId", ""),
                    "outcome_index": idx,
                    "market_slug": m.get("slug", ""),
                    "neg_risk": bool(m.get("negRisk", False)),
                    "tick_size": str(m.get("minimumTickSize") or "0.01"),
                }
    return None


def get_market_meta(token_id: str) -> dict:
    """Get {neg_risk, tick_size} for a token. Cached. Falls back to CLOB query."""
    if token_id in _market_meta_cache:
        return _market_meta_cache[token_id]
    try:
        # CLOB direct: /markets-by-token/{token_id} → returns neg_risk + min_tick_size
        r = httpx.get(
            f"{config.POLYMARKET_HOST}/markets-by-token/{token_id}",
            timeout=10,
        )
        if r.status_code == 200:
            m = r.json()
            meta = {
                "neg_risk": bool(m.get("neg_risk", False)),
                "tick_size": str(m.get("minimum_tick_size") or "0.01"),
            }
            _market_meta_cache[token_id] = meta
            return meta
    except Exception as e:
        log.warning(f"get_market_meta failed token={token_id[:10]}...: {e}")
    # Safe defaults: assume neg_risk=False with cent-tick (most binary markets)
    return {"neg_risk": False, "tick_size": "0.01"}


def get_market_price(token_id: str, side: str) -> Optional[float]:
    """Best ask (BUY) or best bid (SELL) for a token."""
    client = get_client()
    try:
        resp = client.get_price(token_id=token_id, side=side)
        if isinstance(resp, dict):
            resp = resp.get("price")
        return float(resp)
    except Exception as e:
        log.warning(f"get_price échec token={token_id[:10]}...: {e}")
        return None


def place_buy(token_id: str, size_usd: float, max_price: float = 0.99) -> dict:
    """Place a limit BUY at best ask. Returns the raw result."""
    client = get_client()
    ask = get_market_price(token_id, "buy")
    if ask is None:
        return {"status": "no_price", "token_id": token_id}
    if ask > max_price:
        return {"status": "price_too_high", "ask": ask, "max": max_price}
    size_shares = round(size_usd / ask, 2)
    if size_shares < 5:
        size_shares = 5  # Polymarket min order size
    if config.DRY_RUN:
        log.info(f"[DRY] BUY {size_shares:.2f} @ {ask:.4f} (${size_shares*ask:.2f}) tok={token_id[:10]}...")
        return {"status": "dry_run", "side": "BUY", "size_shares": size_shares,
                "price": ask, "cost_usd": size_shares * ask, "token_id": token_id}
    try:
        meta = get_market_meta(token_id)
        order_args = OrderArgs(price=ask, size=size_shares, side=BUY, token_id=token_id)
        opts = PartialCreateOrderOptions(
            neg_risk=meta["neg_risk"],
            tick_size=meta["tick_size"],
        )
        signed = client.create_order(order_args, opts)
        resp = client.post_order(signed)
        log.info(f"BUY submit (neg_risk={meta['neg_risk']}, tick={meta['tick_size']}): {resp}")
        return {"status": "submitted", "side": "BUY", "size_shares": size_shares,
                "price": ask, "cost_usd": size_shares * ask, "token_id": token_id, "resp": resp}
    except Exception as e:
        log.error(f"BUY échec: {type(e).__name__}: {e}")
        return {"status": "error", "error": str(e), "token_id": token_id}


def place_sell(token_id: str, size_shares: float, min_price: float = 0.01) -> dict:
    """Place a limit SELL at best bid."""
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
        meta = get_market_meta(token_id)
        order_args = OrderArgs(price=bid, size=size_shares, side=SELL, token_id=token_id)
        opts = PartialCreateOrderOptions(
            neg_risk=meta["neg_risk"],
            tick_size=meta["tick_size"],
        )
        signed = client.create_order(order_args, opts)
        resp = client.post_order(signed)
        log.info(f"SELL submit (neg_risk={meta['neg_risk']}, tick={meta['tick_size']}): {resp}")
        return {"status": "submitted", "side": "SELL", "size_shares": size_shares,
                "price": bid, "proceeds_usd": size_shares * bid, "token_id": token_id, "resp": resp}
    except Exception as e:
        log.error(f"SELL échec: {type(e).__name__}: {e}")
        return {"status": "error", "error": str(e), "token_id": token_id}

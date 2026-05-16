"""Polymarket CLOB order executor — paper/live routing.

Mirror du pattern live/alpaca_executor.py mais pour Polymarket via py-clob-client.

Modes d'exécution (combinaison de 2 env vars) :
  PAPER_TRADING=true  + LIVE_POLYMARKET=*       → no-op (laisse PaperPortfolio gérer)
  PAPER_TRADING=false + LIVE_POLYMARKET=false   → no-op (sécurité : double opt-in requis)
  PAPER_TRADING=false + LIVE_POLYMARKET=true    → ordres réels CLOB

Pourquoi double opt-in : éviter qu'un seul env var typo (PAPER_TRADING=false
oublié) bascule en live involontairement. LIVE_POLYMARKET=true doit être
explicite pour activer l'envoi d'ordres signés.

Variables env requises en mode live :
  - POLYMARKET_PRIVATE_KEY     EVM 0x... (clé privée du wallet signataire)
  - POLYMARKET_FUNDER_ADDRESS  0x... du proxy wallet Polymarket (visible dans
                                l'URL après login : polymarket.com/profile/0x...)
  - POLYMARKET_SIGNATURE_TYPE  1 (proxy/Magic) | 2 (email/Magic) | 0 (EOA direct)
                                Par défaut : 1 (cas le plus courant pour wallets créés
                                via le site Polymarket)

Variables env optionnelles :
  - POLYMARKET_HOST            défaut https://clob.polymarket.com
  - POLYMARKET_MAX_SLIPPAGE_PCT défaut 0.02 (2% au-dessus du prix cible)
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── Modes ────────────────────────────────────────────────────────────────────


def _is_paper() -> bool:
    return os.getenv("PAPER_TRADING", "true").lower() == "true"


def is_live() -> bool:
    """Le seul cas qui déclenche de vrais ordres signés."""
    return (not _is_paper()) and os.getenv("LIVE_POLYMARKET", "false").lower() == "true"


def _max_slippage_pct() -> float:
    try:
        return float(os.getenv("POLYMARKET_MAX_SLIPPAGE_PCT", "0.02"))
    except (TypeError, ValueError):
        return 0.02


# ── OrderResult ──────────────────────────────────────────────────────────────


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    filled_shares: float = 0.0
    filled_price: float = 0.0
    cost_usd: float = 0.0
    error: Optional[str] = None

    def __repr__(self) -> str:
        if self.success:
            return (
                f"OrderResult(OK id={self.order_id} "
                f"shares={self.filled_shares:.4f} @ {self.filled_price:.4f} "
                f"cost=${self.cost_usd:.2f})"
            )
        return f"OrderResult(FAILED: {self.error})"


# ── Client lazy-cached ───────────────────────────────────────────────────────

_client_lock = threading.Lock()
_client = None
_funder = None


def _build_client():
    """Initialise le ClobClient avec creds dérivées. Idempotent (cache).
    Importe py_clob_client paresseusement pour ne pas casser le mode paper
    si la lib n'est pas installée.
    """
    global _client, _funder
    with _client_lock:
        if _client is not None:
            return _client

        pk = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
        if not pk or not funder:
            raise RuntimeError(
                "Live Polymarket requires POLYMARKET_PRIVATE_KEY and "
                "POLYMARKET_FUNDER_ADDRESS in env."
            )
        try:
            sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        except ValueError:
            sig_type = 1
        host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")

        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host=host,
            chain_id=POLYGON,
            key=pk,
            signature_type=sig_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        _client = client
        _funder = funder
        log.info("polymarket clob client initialized (funder=%s, sig_type=%d)",
                 funder, sig_type)
        return _client


def reset_client_for_test() -> None:
    """Force re-init du client (tests only)."""
    global _client, _funder
    with _client_lock:
        _client = None
        _funder = None


# ── Public API ───────────────────────────────────────────────────────────────


def startup_check() -> bool:
    """Vérifie que le client peut s'initialiser et fetcher la balance.
    Retourne True si OK ou si mode paper (no-op). Fail-fast en live si KO.
    """
    if not is_live():
        log.info("polymarket_executor: paper/safe mode (no live orders)")
        return True
    try:
        client = _build_client()
        bal = check_balance()
        log.info("polymarket_executor: live mode OK, USDC balance=$%.2f", bal)
        return True
    except Exception as e:
        log.error("polymarket_executor startup_check FAILED: %s", e)
        return False


def check_balance() -> float:
    """USDC disponible sur le funder address. Retourne 0 en mode paper."""
    if not is_live():
        return 0.0
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    client = _build_client()
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    resp = client.get_balance_allowance(params)
    # resp = {"balance": "12345600", "allowance": "..."} en USDC base units (6 décimales)
    try:
        return float(resp.get("balance", "0")) / 1e6
    except (TypeError, ValueError):
        return 0.0


def get_best_ask(token_id: str) -> Optional[float]:
    """Meilleur ask actuel sur le carnet (None si pas de book)."""
    if not is_live():
        return None
    try:
        client = _build_client()
        resp = client.get_price(token_id=token_id, side="SELL")
        return float(resp.get("price")) if resp.get("price") is not None else None
    except Exception as e:
        log.warning("get_best_ask(%s) failed: %s", token_id, e)
        return None


def get_best_bid(token_id: str) -> Optional[float]:
    if not is_live():
        return None
    try:
        client = _build_client()
        resp = client.get_price(token_id=token_id, side="BUY")
        return float(resp.get("price")) if resp.get("price") is not None else None
    except Exception as e:
        log.warning("get_best_bid(%s) failed: %s", token_id, e)
        return None


def execute_buy(token_id: str, usd_size: float, target_price: float) -> OrderResult:
    """Place une LIMIT BUY visant `usd_size` USD, prix ≤ max(target_price, ask) × (1+slippage).

    Mode paper / non-live : no-op, retourne OrderResult(success=True) avec un
    order_id fictif (le PaperPortfolio fait le reste côté caller).
    Mode live : signe et poste un ordre FOK (Fill-Or-Kill). Si pas de fill
    immédiat, retourne success=False (skipped, no fill).
    """
    if not is_live():
        return OrderResult(success=True, order_id="paper", filled_shares=0.0,
                           filled_price=target_price, cost_usd=0.0)

    if usd_size <= 0 or target_price <= 0:
        return OrderResult(success=False, error="invalid usd_size or price")

    from py_clob_client.clob_types import OrderArgs, OrderType

    client = _build_client()

    # Prix max acceptable : on prend max(target, ask) × (1+slippage), bornée à 0.99
    ask = get_best_ask(token_id)
    base = max(target_price, ask) if ask else target_price
    limit_price = min(0.99, round(base * (1.0 + _max_slippage_pct()), 4))

    shares = round(usd_size / limit_price, 4)
    if shares < 1.0:
        return OrderResult(success=False,
                           error=f"size too small ({shares:.4f} shares)")

    try:
        order = client.create_order(OrderArgs(
            token_id=token_id, price=limit_price, size=shares, side="BUY",
        ))
        resp = client.post_order(order, orderType=OrderType.FOK)
        if not resp.get("success", False):
            return OrderResult(success=False,
                               error=f"post_order rejected: {resp.get('errorMsg', resp)}")
        order_id = resp.get("orderID") or resp.get("orderId") or ""
        # FOK : soit fill complet, soit rien
        filled = float(resp.get("makingAmount", 0)) / 1e6
        cost = float(resp.get("takingAmount", 0)) / 1e6
        avg = (cost / filled) if filled > 0 else limit_price
        return OrderResult(success=True, order_id=order_id,
                           filled_shares=filled, filled_price=avg, cost_usd=cost)
    except Exception as e:
        log.exception("execute_buy(%s, $%.2f) failed", token_id[:10], usd_size)
        return OrderResult(success=False, error=str(e))


def execute_sell(token_id: str, shares_size: float, target_price: float) -> OrderResult:
    """LIMIT SELL FOK pour `shares_size` outcome tokens.

    Mode paper / non-live : no-op success.
    Mode live : prix min = max(target_price, bid) × (1-slippage), borné à 0.01.
    """
    if not is_live():
        return OrderResult(success=True, order_id="paper",
                           filled_shares=shares_size, filled_price=target_price,
                           cost_usd=shares_size * target_price)

    if shares_size <= 0 or target_price <= 0:
        return OrderResult(success=False, error="invalid size or price")

    from py_clob_client.clob_types import OrderArgs, OrderType

    client = _build_client()
    bid = get_best_bid(token_id)
    base = max(target_price, bid) if bid else target_price
    # Pour SELL on veut accepter de vendre un peu moins cher si le marché a baissé
    limit_price = max(0.01, round(base * (1.0 - _max_slippage_pct()), 4))

    try:
        order = client.create_order(OrderArgs(
            token_id=token_id, price=limit_price, size=shares_size, side="SELL",
        ))
        resp = client.post_order(order, orderType=OrderType.FOK)
        if not resp.get("success", False):
            return OrderResult(success=False,
                               error=f"post_order rejected: {resp.get('errorMsg', resp)}")
        order_id = resp.get("orderID") or resp.get("orderId") or ""
        filled = float(resp.get("makingAmount", 0)) / 1e6
        proceeds = float(resp.get("takingAmount", 0)) / 1e6
        avg = (proceeds / filled) if filled > 0 else limit_price
        return OrderResult(success=True, order_id=order_id,
                           filled_shares=filled, filled_price=avg, cost_usd=proceeds)
    except Exception as e:
        log.exception("execute_sell(%s, %.4f) failed", token_id[:10], shares_size)
        return OrderResult(success=False, error=str(e))


def cancel_order(order_id: str) -> bool:
    if not is_live():
        return True
    try:
        client = _build_client()
        resp = client.cancel(order_id)
        return bool(resp.get("canceled") or resp.get("success"))
    except Exception as e:
        log.warning("cancel_order(%s) failed: %s", order_id, e)
        return False

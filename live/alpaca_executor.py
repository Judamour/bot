"""
alpaca_executor.py — Couche d'exécution Alpaca pour stocks US (NVDA, GOOGL, ...).

Mirror de l'API de live/order_executor.py. Utilisée pour les actifs routés Alpaca
(stocks US, ETFs). Les cryptos restent sur Kraken (déjà fonctionnel, frais 0.16%).

Variables d'environnement requises (chargées via config) :
  - ALPACA_API_KEY       (PK... pour paper, AK... pour live)
  - ALPACA_SECRET_KEY
  - APCA_API_BASE_URL    (https://paper-api.alpaca.markets ou https://api.alpaca.markets)

Endpoints utilisés :
  - POST  /v2/orders                  → place order
  - GET   /v2/orders/{id}             → poll fill
  - DELETE /v2/orders/{id}            → cancel
  - GET   /v2/account                 → cash USD
  - GET   /v2/assets/{symbol}         → check tradable

PAPER_TRADING=true → no-op simulé (cohérent avec order_executor.py Kraken).
PAPER_TRADING=false + ALPACA_PAPER=true → ordres sur paper Alpaca (bac à sable réel).
PAPER_TRADING=false + ALPACA_PAPER=false → ordres LIVE Alpaca.
"""

import time
import logging
import sys
import os
import json
import urllib.request
import urllib.parse
import urllib.error

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from live.order_executor import OrderResult
from live.notifier import notify

logger = logging.getLogger(__name__)

_VALID_ALPACA_SYMBOLS: set = set()


# ── Configuration ────────────────────────────────────────────────────────────

def _api_key() -> str:
    return os.getenv("ALPACA_API_KEY", "")


def _api_secret() -> str:
    return os.getenv("ALPACA_SECRET_KEY", "")


def _base_url() -> str:
    return os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")


def _is_paper_endpoint() -> bool:
    return "paper" in _base_url()


def is_alpaca_stock(symbol: str) -> bool:
    """True si le symbole doit être routé vers Alpaca (stocks US : pas de slash, alphanumérique).

    Exemples : "NVDA"→True, "GOOGL"→True, "BRK.B"→True, "BTC/USD"→False, "NVDAx/USD"→False.
    """
    if "/" in symbol or not symbol:
        return False
    return symbol.replace(".", "").isalnum() and symbol == symbol.upper()


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _request(method: str, path: str, body: dict = None, timeout: int = 30) -> dict:
    """
    Requête authentifiée vers Alpaca REST.
    Lève RuntimeError sur 4xx/5xx avec le message d'erreur Alpaca.
    """
    url = _base_url() + path
    headers = {
        "APCA-API-KEY-ID": _api_key(),
        "APCA-API-SECRET-KEY": _api_secret(),
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw)
            msg = err.get("message") or err.get("code") or str(err)
        except Exception:
            msg = raw.decode(errors="replace")[:300]
        raise RuntimeError(f"Alpaca {e.code}: {msg}") from None


# ── Account / balance ────────────────────────────────────────────────────────

def check_balance() -> float:
    """
    Cash USD disponible sur le compte Alpaca (paper ou live selon APCA_API_BASE_URL).
    Returns -1.0 si erreur connexion.
    """
    if config.PAPER_TRADING:
        return 0.0
    try:
        acct = _request("GET", "/v2/account")
        cash = float(acct.get("cash", 0))
        endpoint = "paper" if _is_paper_endpoint() else "live"
        logger.info(f"[ALPACA] Solde {endpoint}: {cash:.2f} USD (status={acct.get('status')})")
        return cash
    except Exception as e:
        logger.error(f"[ALPACA] check_balance ÉCHOUÉ: {e}")
        notify(f"⛔ <b>Alpaca check_balance ÉCHOUÉ</b>\nErreur: {e}")
        return -1.0


# ── Validation symbols ───────────────────────────────────────────────────────

def validate_symbols(symbols: list) -> tuple:
    """
    Vérifie que chaque ticker existe et est tradable sur Alpaca.

    Args:
        symbols: liste de tickers (ex ["NVDA", "GOOGL"])
    Returns:
        (valid, invalid)
    """
    global _VALID_ALPACA_SYMBOLS
    valid, invalid = [], []
    for sym in symbols:
        try:
            asset = _request("GET", f"/v2/assets/{urllib.parse.quote(sym)}")
            if asset.get("tradable") and asset.get("status") == "active":
                valid.append(sym)
                logger.info(f"[ALPACA validate] {sym} OK ({asset.get('exchange')}, fractionable={asset.get('fractionable')})")
            else:
                invalid.append(sym)
                logger.warning(f"[ALPACA validate] {sym} non tradable: status={asset.get('status')} tradable={asset.get('tradable')}")
        except Exception as e:
            invalid.append(sym)
            logger.warning(f"[ALPACA validate] {sym} introuvable: {e}")
    _VALID_ALPACA_SYMBOLS = set(valid)
    return valid, invalid


# ── Orders ───────────────────────────────────────────────────────────────────

def _wait_for_fill(order_id: str, max_wait_sec: int) -> dict | None:
    """Poll /v2/orders/{id} jusqu'à filled. None si timeout ou canceled."""
    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        try:
            o = _request("GET", f"/v2/orders/{order_id}")
            status = o.get("status")
            if status == "filled":
                return o
            if status in ("canceled", "expired", "rejected"):
                logger.warning(f"[ALPACA] Ordre {order_id} status={status}")
                return None
        except Exception as e:
            logger.warning(f"[ALPACA] fetch_order {order_id}: {e}")
        time.sleep(2)
    return None


def execute_buy(symbol: str, size: float, price_estimate: float,
                max_wait_sec: int = 30) -> OrderResult:
    """
    Place un ordre BUY market sur Alpaca.
    En PAPER_TRADING (config) : no-op simulé. Pour utiliser le paper Alpaca, mettre
    PAPER_TRADING=false et APCA_API_BASE_URL=https://paper-api.alpaca.markets.
    """
    if config.PAPER_TRADING:
        effective_price = price_estimate * (1 + config.SLIPPAGE)
        return OrderResult(success=True, order_id="PAPER", filled_size=size,
                           filled_price=effective_price)

    if _VALID_ALPACA_SYMBOLS and symbol not in _VALID_ALPACA_SYMBOLS:
        logger.warning(f"[ALPACA] BUY {symbol} skip — symbole non validé")
        return OrderResult(success=False, error="symbol_not_supported")

    payload = {
        "symbol": symbol,
        "qty": str(size),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }
    try:
        logger.info(f"[ALPACA] BUY {symbol} qty={size:.6f} @ ~{price_estimate:.4f}$ ({size*price_estimate:.2f}$)")
        order = _request("POST", "/v2/orders", body=payload)
        order_id = order.get("id", "?")

        filled = _wait_for_fill(order_id, max_wait_sec)
        if filled is None:
            try:
                _request("DELETE", f"/v2/orders/{order_id}")
                logger.warning(f"[ALPACA] BUY {order_id} annulé (timeout {max_wait_sec}s)")
            except Exception as e:
                logger.error(f"[ALPACA] Cancel {order_id} échoué: {e}")
            return OrderResult(success=False, order_id=order_id,
                               error=f"Timeout {max_wait_sec}s — ordre annulé")

        filled_qty = float(filled.get("filled_qty", size))
        filled_avg = float(filled.get("filled_avg_price") or price_estimate)
        notify(f"✅ <b>LIVE BUY</b> {symbol} (Alpaca)\n"
               f"Taille: {filled_qty:.6f} @ {filled_avg:.4f}$\nOrdre: {order_id}")
        return OrderResult(success=True, order_id=order_id,
                           filled_size=filled_qty, filled_price=filled_avg)

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[ALPACA] BUY {symbol} ÉCHOUÉ: {err_msg}")
        notify(f"⛔ <b>LIVE BUY ÉCHOUÉ</b> {symbol} (Alpaca)\nErreur: {err_msg[:200]}")
        return OrderResult(success=False, error=err_msg)


def execute_sell(symbol: str, size: float, price_estimate: float,
                 reason: str = "exit", max_wait_sec: int = 30) -> OrderResult:
    """Place un ordre SELL market sur Alpaca."""
    if config.PAPER_TRADING:
        effective_price = price_estimate * (1 - config.SLIPPAGE)
        return OrderResult(success=True, order_id="PAPER", filled_size=size,
                           filled_price=effective_price)

    if _VALID_ALPACA_SYMBOLS and symbol not in _VALID_ALPACA_SYMBOLS:
        logger.warning(f"[ALPACA] SELL {symbol} skip — symbole non validé")
        return OrderResult(success=False, error="symbol_not_supported")

    payload = {
        "symbol": symbol,
        "qty": str(size),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }
    try:
        logger.info(f"[ALPACA] SELL {symbol} qty={size:.6f} @ ~{price_estimate:.4f}$ ({reason})")
        order = _request("POST", "/v2/orders", body=payload)
        order_id = order.get("id", "?")

        filled = _wait_for_fill(order_id, max_wait_sec)
        if filled is None:
            logger.error(f"[ALPACA] SELL {order_id} non rempli après {max_wait_sec}s")
            notify(f"🚨 <b>LIVE SELL NON REMPLI</b> {symbol} (Alpaca)\n"
                   f"Ordre {order_id}\n⚠️ Intervention manuelle requise")
            return OrderResult(success=False, order_id=order_id,
                               error=f"Timeout {max_wait_sec}s")

        filled_qty = float(filled.get("filled_qty", size))
        filled_avg = float(filled.get("filled_avg_price") or price_estimate)
        icon = "🔴" if "stop" in reason else "⏹"
        notify(f"{icon} <b>LIVE SELL</b> {symbol} (Alpaca) [{reason}]\n"
               f"Taille: {filled_qty:.6f} @ {filled_avg:.4f}$")
        return OrderResult(success=True, order_id=order_id,
                           filled_size=filled_qty, filled_price=filled_avg)

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[ALPACA] SELL {symbol} ÉCHOUÉ: {err_msg}")
        notify(f"🚨 <b>LIVE SELL ÉCHOUÉ</b> {symbol} (Alpaca) [{reason}]\n"
               f"Erreur: {err_msg}\n⚠️ Position ouverte — intervention manuelle requise")
        return OrderResult(success=False, error=err_msg)


# ── Startup check ────────────────────────────────────────────────────────────

def startup_check() -> bool:
    """Sanity check au démarrage : connexion + status compte."""
    if config.PAPER_TRADING:
        return True
    try:
        acct = _request("GET", "/v2/account")
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            notify(f"⛔ <b>Alpaca STARTUP BLOQUÉ</b>\nstatus={acct.get('status')} "
                   f"trading_blocked={acct.get('trading_blocked')}")
            return False
        cash = float(acct.get("cash", 0))
        equity = float(acct.get("equity", 0))
        endpoint = "paper" if _is_paper_endpoint() else "LIVE"
        logger.info(f"[ALPACA] Startup {endpoint} OK — cash={cash:.2f}$ equity={equity:.2f}$")
        notify(f"🟢 <b>Alpaca {endpoint} OK</b>\nCash: {cash:.2f}$ | Equity: {equity:.2f}$")
        return True
    except Exception as e:
        logger.error(f"[ALPACA] startup_check ÉCHOUÉ: {e}")
        notify(f"⛔ <b>Alpaca startup ÉCHOUÉ</b>\nErreur: {e}")
        return False

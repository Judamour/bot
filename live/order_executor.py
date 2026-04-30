"""
order_executor.py — Couche d'exécution d'ordres réels pour le passage en live.

En PAPER_TRADING=true : toutes les fonctions sont des no-ops qui retournent des résultats simulés.
En PAPER_TRADING=false : place de vrais ordres sur Kraken via ccxt.

Architecture :
  - execute_buy(symbol, size, price_estimate) → OrderResult
  - execute_sell(symbol, size, price_estimate) → OrderResult
  - reconcile_positions(state, bot_id) → dict (positions réelles vs state JSON)
  - check_balance() → float (solde EUR réel)

Avant le passage en live :
  1. Vérifier que les clés Kraken ont la permission "Trade" (pas "Read Only")
  2. Appeler check_balance() au démarrage pour confirmer la connexion
  3. Appeler reconcile_positions() au démarrage pour détecter toute divergence
  4. Tester avec un ordre minimum (ex: 5€ BTC) avant tout déploiement réel
"""

import time
import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import get_exchange
from live.notifier import notify

logger = logging.getLogger(__name__)


# ── Résultat d'un ordre ──────────────────────────────────────────────────────

class OrderResult:
    def __init__(self, success: bool, order_id: str = None, filled_size: float = 0.0,
                 filled_price: float = 0.0, error: str = None):
        self.success      = success
        self.order_id     = order_id
        self.filled_size  = filled_size
        self.filled_price = filled_price
        self.error        = error

    def __repr__(self):
        if self.success:
            return f"OrderResult(OK id={self.order_id} size={self.filled_size:.6f} @ {self.filled_price:.4f})"
        return f"OrderResult(FAILED: {self.error})"


# ── Exécution d'ordres ───────────────────────────────────────────────────────

def execute_buy(symbol: str, size: float, price_estimate: float,
                max_wait_sec: int = 30) -> OrderResult:
    """
    Place un ordre d'achat market sur Kraken.

    En paper : retourne un OrderResult simulé immédiatement (pas d'appel réseau).
    En live : place un ordre market, attend la complétion (max max_wait_sec secondes),
              annule si partiellement rempli au-delà du délai.

    Args:
        symbol: Format Kraken (ex: "BTC/EUR", "NVDAx/EUR")
        size: Quantité à acheter (en unités de l'actif, pas en EUR)
        price_estimate: Prix estimé (utilisé pour le slippage paper et les logs)
        max_wait_sec: Délai maximum pour attendre la complétion de l'ordre

    Returns:
        OrderResult avec filled_size et filled_price réels (live) ou simulés (paper)
    """
    if config.PAPER_TRADING:
        # Simulation : slippage appliqué sur le prix estimé
        effective_price = price_estimate * (1 + config.SLIPPAGE)
        return OrderResult(success=True, order_id="PAPER", filled_size=size,
                           filled_price=effective_price)

    # ── LIVE ──
    # Pré-check : montant total < MIN_ORDER_EUR → skip (évite "insufficient funds" loop)
    order_value = size * price_estimate
    if order_value < config.MIN_ORDER_EUR:
        logger.warning(f"[ORDER] BUY {symbol} skip — montant {order_value:.2f}€ < min {config.MIN_ORDER_EUR}€")
        return OrderResult(success=False, error=f"min_order_size: {order_value:.2f}€ < {config.MIN_ORDER_EUR}€")

    try:
        exchange = get_exchange(use_auth=True)
        logger.info(f"[ORDER] BUY {symbol} size={size:.6f} @ ~{price_estimate:.4f}€ ({order_value:.2f}€)")

        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side="buy",
            amount=size,
        )
        order_id = order.get("id", "?")
        logger.info(f"[ORDER] Ordre BUY soumis: id={order_id}")

        # Polling jusqu'à complétion
        filled = _wait_for_fill(exchange, symbol, order_id, max_wait_sec)
        if filled is None:
            # Annuler si pas rempli
            try:
                exchange.cancel_order(order_id, symbol)
                logger.warning(f"[ORDER] BUY {order_id} annulé (timeout {max_wait_sec}s)")
            except Exception as e:
                logger.error(f"[ORDER] Impossible d'annuler {order_id}: {e}")
            return OrderResult(success=False, order_id=order_id,
                               error=f"Timeout {max_wait_sec}s — ordre annulé")

        notify(f"✅ <b>LIVE BUY</b> {symbol}\n"
               f"Taille: {filled['filled']:.6f} @ {filled['average']:.4f}€\n"
               f"Ordre: {order_id}")

        return OrderResult(
            success=True,
            order_id=order_id,
            filled_size=float(filled.get("filled", size)),
            filled_price=float(filled.get("average", price_estimate)),
        )

    except Exception as e:
        logger.error(f"[ORDER] BUY {symbol} ÉCHOUÉ: {e}")
        notify(f"⛔ <b>LIVE BUY ÉCHOUÉ</b> {symbol}\nErreur: {e}")
        return OrderResult(success=False, error=str(e))


def execute_sell(symbol: str, size: float, price_estimate: float,
                 reason: str = "exit", max_wait_sec: int = 30) -> OrderResult:
    """
    Place un ordre de vente market sur Kraken.

    En paper : retourne un OrderResult simulé.
    En live : place un ordre market et attend la complétion.

    Args:
        symbol: Format Kraken
        size: Quantité à vendre
        price_estimate: Prix estimé (pour logs)
        reason: Raison de l'exit (stop_loss, signal_exit, trailing_stop...)
        max_wait_sec: Délai maximum d'attente
    """
    if config.PAPER_TRADING:
        effective_price = price_estimate * (1 - config.SLIPPAGE)
        return OrderResult(success=True, order_id="PAPER", filled_size=size,
                           filled_price=effective_price)

    # ── LIVE ──
    try:
        exchange = get_exchange(use_auth=True)
        logger.info(f"[ORDER] SELL {symbol} size={size:.6f} @ ~{price_estimate:.4f}€ ({reason})")

        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side="sell",
            amount=size,
        )
        order_id = order.get("id", "?")

        filled = _wait_for_fill(exchange, symbol, order_id, max_wait_sec)
        if filled is None:
            # Pour un SELL (exit), on log l'échec mais on ne ré-essaie pas automatiquement
            # L'opérateur doit intervenir manuellement
            logger.error(f"[ORDER] SELL {order_id} non rempli après {max_wait_sec}s — intervention manuelle requise")
            notify(f"🚨 <b>LIVE SELL NON REMPLI</b> {symbol}\n"
                   f"Ordre {order_id} en attente depuis {max_wait_sec}s\n"
                   f"⚠️ Intervention manuelle requise")
            return OrderResult(success=False, order_id=order_id,
                               error=f"Timeout — vérifier Kraken manuellement")

        icon = "🔴" if "stop" in reason else "⏹"
        notify(f"{icon} <b>LIVE SELL</b> {symbol} [{reason}]\n"
               f"Taille: {filled['filled']:.6f} @ {filled['average']:.4f}€")

        return OrderResult(
            success=True,
            order_id=order_id,
            filled_size=float(filled.get("filled", size)),
            filled_price=float(filled.get("average", price_estimate)),
        )

    except Exception as e:
        logger.error(f"[ORDER] SELL {symbol} ÉCHOUÉ: {e}")
        notify(f"🚨 <b>LIVE SELL ÉCHOUÉ</b> {symbol} [{reason}]\n"
               f"Erreur: {e}\n⚠️ Position ouverte — intervention manuelle requise")
        return OrderResult(success=False, error=str(e))


# ── Utilitaires internes ─────────────────────────────────────────────────────

def _wait_for_fill(exchange, symbol: str, order_id: str, max_wait_sec: int) -> dict | None:
    """
    Attend qu'un ordre soit entièrement rempli.
    Retourne les détails de l'ordre rempli, ou None si timeout.
    """
    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        try:
            order = exchange.fetch_order(order_id, symbol)
            status = order.get("status", "open")
            if status == "closed":
                return order
            if status == "canceled":
                logger.warning(f"[ORDER] Ordre {order_id} annulé par Kraken")
                return None
        except Exception as e:
            logger.warning(f"[ORDER] fetch_order {order_id}: {e}")
        time.sleep(2)

    return None  # Timeout


# ── Réconciliation positions ─────────────────────────────────────────────────

def check_balance() -> float:
    """
    Récupère le solde EUR réel du compte Kraken.
    Utile au démarrage en mode live pour vérifier la connexion et le capital disponible.

    Returns:
        Solde EUR disponible, ou 0.0 si erreur.
    """
    if config.PAPER_TRADING:
        return 0.0  # Non pertinent en paper

    try:
        exchange = get_exchange(use_auth=True)
        balance = exchange.fetch_balance()
        eur = float(balance.get("EUR", {}).get("free", 0))
        logger.info(f"[ORDER] Solde Kraken : {eur:.2f}€ disponibles")
        return eur
    except Exception as e:
        logger.error(f"[ORDER] check_balance ÉCHOUÉ: {e}")
        notify(f"⛔ <b>Kraken check_balance ÉCHOUÉ</b>\nErreur: {e}\n"
               f"Vérifier les clés API et la connexion réseau.")
        return -1.0  # Sentinel : erreur de connexion (≠ 0 qui est valide si tout en positions)


def check_total_value_eur() -> float:
    """
    Valeur totale du compte (EUR free + valeur des positions converties en EUR).
    Permet de redémarrer le bot après crash même si tout le cash est alloué en positions.

    Returns:
        Valeur totale estimée, ou -1.0 si erreur de connexion.
    """
    if config.PAPER_TRADING:
        return 0.0

    try:
        exchange = get_exchange(use_auth=True)
        balance = exchange.fetch_balance()
        total = float(balance.get("EUR", {}).get("total", 0))

        # Convertir chaque position en valeur EUR via ticker
        for asset, info in balance.items():
            if asset in ("EUR", "info", "free", "used", "total", "timestamp", "datetime"):
                continue
            qty = float(info.get("total", 0)) if isinstance(info, dict) else 0
            if qty <= 0.0001:
                continue
            try:
                ticker = exchange.fetch_ticker(f"{asset}/EUR")
                last = float(ticker.get("last") or ticker.get("close") or 0)
                total += qty * last
            except Exception:
                logger.warning(f"[ORDER] Impossible de valoriser {asset} en EUR")
        return total
    except Exception as e:
        logger.error(f"[ORDER] check_total_value_eur ÉCHOUÉ: {e}")
        return -1.0


def reconcile_positions(state: dict, bot_id: str) -> dict:
    """
    Compare les positions du state JSON avec les positions réelles Kraken.
    En paper : retourne le state inchangé.
    En live : détecte les divergences et alerte via Telegram.

    Les divergences possibles :
    - Position dans state mais pas sur Kraken (position fermée manuellement)
    - Position sur Kraken mais pas dans state (crash pendant BUY avant sauvegarde)

    Returns:
        dict avec clés "state_only" et "exchange_only" (listes de symboles divergents)
    """
    if config.PAPER_TRADING:
        return {"state_only": [], "exchange_only": []}

    result = {"state_only": [], "exchange_only": []}
    try:
        exchange = get_exchange(use_auth=True)
        balance = exchange.fetch_balance()

        state_positions = set(state.get("positions", {}).keys())
        exchange_positions = set()

        # Détecter les positions sur Kraken (solde > 0 pour les actifs connus)
        for symbol in state_positions | set(config.SYMBOLS):
            asset = symbol.split("/")[0]  # ex: "BTC" depuis "BTC/EUR"
            bal = float(balance.get(asset, {}).get("total", 0))
            if bal > 0.0001:  # Seuil minimal pour ignorer les dust
                exchange_positions.add(symbol)

        result["state_only"]    = list(state_positions - exchange_positions)
        result["exchange_only"] = list(exchange_positions - state_positions)

        if result["state_only"] or result["exchange_only"]:
            msg = (f"⚠️ <b>Bot {bot_id.upper()} — Divergence positions</b>\n"
                   f"State seulement: {result['state_only'] or 'aucune'}\n"
                   f"Kraken seulement: {result['exchange_only'] or 'aucune'}\n"
                   f"Vérifier et corriger manuellement.")
            logger.warning(f"[ORDER] Divergence positions Bot {bot_id}: {result}")
            notify(msg)

    except Exception as e:
        logger.error(f"[ORDER] reconcile_positions Bot {bot_id}: {e}")

    return result


# ── Vérification au démarrage (appelée depuis multi_runner en mode live) ─────

def startup_check() -> bool:
    """
    Vérifications obligatoires avant de démarrer en mode live.
    Appeler en début de multi_runner.run() si PAPER_TRADING=false.

    Returns:
        True si tout est OK, False si un problème bloquant est détecté.
    """
    if config.PAPER_TRADING:
        return True

    logger.info("[ORDER] === DÉMARRAGE EN MODE LIVE — vérifications ===")
    notify("🟡 <b>Bot Trading LIVE</b> — démarrage en cours...\nVérifications en cours...")

    # 1. Vérifier la connexion (erreur réseau/auth → return -1, EUR=0 acceptable si positions)
    balance_eur = check_balance()
    if balance_eur < 0:
        notify("⛔ <b>LIVE STARTUP ÉCHOUÉ</b>\nConnexion Kraken impossible.\nBot arrêté.")
        return False

    # 2. Si EUR free = 0, vérifier qu'il y a des positions (cas redémarrage avec capital alloué)
    if balance_eur == 0:
        total = check_total_value_eur()
        if total <= 0:
            notify("⛔ <b>LIVE STARTUP ÉCHOUÉ</b>\nCompte vide (0 EUR + 0 positions).\nBot arrêté.")
            return False
        logger.info(f"[ORDER] EUR free=0 mais positions valorisées à {total:.2f}€ — démarrage OK")
        notify(f"✅ <b>Connexion Kraken OK</b>\nEUR libre: 0€ | Total compte: <b>{total:.2f}€</b>\n"
               f"(capital alloué en positions)")
        return True

    logger.info(f"[ORDER] ✓ Solde Kraken: {balance_eur:.2f}€")
    notify(f"✅ <b>Connexion Kraken OK</b>\nSolde disponible: <b>{balance_eur:.2f}€</b>")
    return True

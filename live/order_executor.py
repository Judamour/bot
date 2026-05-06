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
import urllib.request
import urllib.parse
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import get_exchange
from live.notifier import notify

logger = logging.getLogger(__name__)


# ── Cache des symbols valides Kraken (rempli au démarrage par validate_symbols) ──
_VALID_SYMBOLS_CACHE: set = set()
_KRAKEN_PAIR_MAPPING: dict = {}  # "NVDAx/USD" → "NVDAxUSD" (format Kraken native)
_NOTIF_DEDUP_CYCLE: dict = {}    # {symbol_reason: cycle_id} pour throttle Telegram

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


# ── Helpers : validation symbols + bypass ccxt xStocks ──────────────────────

def _kraken_pair(symbol: str) -> str:
    """Convert ccxt format (NVDAx/USD) to Kraken native (NVDAxUSD)."""
    return symbol.replace("/", "")


def validate_symbols(symbols: list) -> tuple:
    """
    Au démarrage : vérifie que chaque symbole est tradable via Kraken API.

    Pour cryptos : check ccxt markets[].
    Pour xStocks : check raw API AssetPairs?aclass=tokenized_asset.

    Returns:
        (valid_symbols, invalid_symbols) — listes filtrées
    """
    global _VALID_SYMBOLS_CACHE, _KRAKEN_PAIR_MAPPING

    valid, invalid = [], []

    # Sépare symboles routés Alpaca (stocks + crypto si flag) et restants Kraken
    from live import alpaca_executor
    alpaca_syms = [s for s in symbols if alpaca_executor.is_alpaca_routed(s)]
    kraken_syms = [s for s in symbols if not alpaca_executor.is_alpaca_routed(s)]

    if alpaca_syms:
        a_valid, a_invalid = alpaca_executor.validate_symbols(alpaca_syms)
        valid += a_valid
        invalid += a_invalid

    if not kraken_syms:
        _VALID_SYMBOLS_CACHE = set(valid)
        return valid, invalid

    try:
        exchange = get_exchange(use_auth=False)
        markets = exchange.load_markets()

        # Récupérer pairs xStocks via raw API. Kraken accepte l'altname (ex "NVDAxUSD")
        # comme `pair` dans AddOrder à condition de fournir `asset_class=tokenized_asset`
        # au payload. NB : c'est `asset_class` (pas `aclass`) — l'endpoint public expose
        # 1545 pairs avec asset_class= contre 256 avec aclass=.
        # On indexe par wsname (ex "NVDAx/USD") → altname pour le mapping config → API.
        xstocks_ws_to_altname: dict = {}
        try:
            url = "https://api.kraken.com/0/public/AssetPairs?asset_class=tokenized_asset"
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            for k, v in data.get("result", {}).items():
                ws = v.get("wsname")
                alt = v.get("altname")
                if not ws or not alt or v.get("status") != "online":
                    continue
                # L'altname est unique par wsname (ex NVDAxUSD pour NVDAx/USD).
                xstocks_ws_to_altname[ws] = alt
        except Exception as e:
            logger.warning(f"[validate] Fetch xStocks pairs failed: {e}")

        for sym in kraken_syms:
            # Try ccxt first
            if sym in markets:
                valid.append(sym)
                _KRAKEN_PAIR_MAPPING[sym] = _kraken_pair(sym)
            elif sym in xstocks_ws_to_altname:
                alt = xstocks_ws_to_altname[sym]
                valid.append(sym)
                _KRAKEN_PAIR_MAPPING[sym] = alt
                logger.info(f"[validate] {sym} → bypass ccxt (xStock altname: {alt}, aclass=tokenized_asset)")
            else:
                invalid.append(sym)
                logger.warning(f"[validate] {sym} INTROUVABLE sur Kraken — exclu")

        _VALID_SYMBOLS_CACHE = set(valid)
    except Exception as e:
        logger.error(f"[validate] ERREUR globale: {e}")
        # Fallback : tout accepter pour ne pas bloquer le bot
        valid = list(symbols)
        _VALID_SYMBOLS_CACHE = set(symbols)

    return valid, invalid


def _is_xstock(symbol: str) -> bool:
    """True si symbole est un xStock (non listé dans ccxt markets)."""
    return symbol in config.XSTOCKS


def _kraken_sign(path: str, data: dict, secret: str) -> str:
    """Sign Kraken private API request."""
    import hashlib, hmac, base64
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data.get("nonce", "")) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    sig = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(sig.digest()).decode()


def _kraken_private_post(endpoint: str, params: dict) -> dict:
    """
    Direct Kraken private API call (bypass ccxt).
    Utilisé pour xStocks que ccxt ne reconnaît pas.
    """
    api_key = config.API_KEY
    api_secret = config.API_SECRET
    if not api_key or not api_secret:
        raise ValueError("API_KEY/SECRET manquants")

    path = f"/0/private/{endpoint}"
    url = "https://api.kraken.com" + path

    nonce = str(int(time.time() * 1000))
    data = dict(params)
    data["nonce"] = nonce

    signature = _kraken_sign(path, data, api_secret)
    headers = {
        "API-Key": api_key,
        "API-Sign": signature,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "bot-trading/1.0",
    }

    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())

    if resp.get("error"):
        raise RuntimeError(f"Kraken API: {resp['error']}")
    return resp.get("result", {})


def _execute_xstock_order(symbol: str, side: str, size: float, price_estimate: float) -> OrderResult:
    """Place un ordre xStock via raw Kraken API (bypass ccxt).

    Kraken exige `asset_class=tokenized_asset` au payload pour résoudre la pair xStock
    (et nécessite la permission API "Trade tokenized assets" sur la clé). Sans le param,
    AddOrder répond `EQuery:Unknown asset pair` ; sans la permission, `Permission denied`.
    """
    pair = _KRAKEN_PAIR_MAPPING.get(symbol, _kraken_pair(symbol))
    try:
        result = _kraken_private_post("AddOrder", {
            "pair": pair,
            "asset_class": "tokenized_asset",
            "type": side,
            "ordertype": "market",
            "volume": str(size),
        })
        order_id = (result.get("txid") or ["?"])[0]
        logger.info(f"[ORDER-RAW] {side.upper()} {symbol} ({pair}) submitted: {order_id}")
        # Estimer le fill (on n'a pas le polling raw pour l'instant — return success direct)
        # TODO: implémenter polling QueryOrders raw si besoin du prix réel
        return OrderResult(
            success=True,
            order_id=order_id,
            filled_size=size,
            filled_price=price_estimate,
        )
    except Exception as e:
        logger.error(f"[ORDER-RAW] {side.upper()} {symbol} FAIL: {e}")
        return OrderResult(success=False, error=str(e))


_SILENT_ERROR_PATTERNS = (
    "does not have market symbol",   # ccxt local rejet (avant API)
    "EQuery:Unknown asset pair",     # Kraken : pair non résolue (asset_class manquant)
    "EGeneral:Internal error",       # Kraken : erreur interne sporadique sur xStocks
    "EGeneral:Permission denied",    # Kraken : clé API sans permission "Trade tokenized assets"
)


def _is_silent_kraken_error(err_msg: str) -> bool:
    """Erreurs connues à ne pas notifier sur Telegram (sinon spam à chaque cycle)."""
    return any(p in err_msg for p in _SILENT_ERROR_PATTERNS)


def _should_notify(symbol: str, reason: str, cycle_id: int = None) -> bool:
    """Throttle Telegram : 1 notif par symbol+reason par cycle."""
    key = f"{symbol}:{reason}"
    cur = _NOTIF_DEDUP_CYCLE.get(key)
    if cur == cycle_id:
        return False
    _NOTIF_DEDUP_CYCLE[key] = cycle_id
    return True


def reset_notif_dedup():
    """À appeler en début de cycle pour reset le dedup."""
    _NOTIF_DEDUP_CYCLE.clear()


# ── Exécution d'ordres ───────────────────────────────────────────────────────

def place_broker_stop(symbol: str, qty: float, stop_price: float,
                      take_profit: float | None = None) -> dict:
    """Place un stop-loss broker-side (protection si bot down). Retourne dict ids."""
    from live import alpaca_executor
    if alpaca_executor.is_alpaca_routed(symbol):
        return alpaca_executor.place_stop_loss(symbol, qty, stop_price, take_profit)
    return {}  # Kraken : non implémenté pour l'instant


def update_broker_stop(symbol: str, stop_order_id: str, new_stop_price: float,
                       qty: float | None = None) -> str | None:
    """Update un stop-loss broker (trailing). Retourne nouveau order_id ou None
    (caller doit re-créer si None)."""
    from live import alpaca_executor
    if alpaca_executor.is_alpaca_routed(symbol):
        return alpaca_executor.replace_stop_loss(stop_order_id, new_stop_price, qty)
    return stop_order_id


def cancel_broker_stop(symbol: str, stop_order_id: str) -> bool:
    """Annule un stop-loss broker (avant de SELL manuellement)."""
    if not stop_order_id:
        return True
    from live import alpaca_executor
    if alpaca_executor.is_alpaca_routed(symbol):
        return alpaca_executor.cancel_order(stop_order_id)
    return True


def get_broker_stop_status(symbol: str, stop_order_id: str) -> str | None:
    """Retourne le status d'un ordre broker ('new', 'filled', 'expired', 'canceled',
    'rejected', 'missing', ...). None si non-Alpaca ou erreur réseau."""
    if not stop_order_id:
        return None
    from live import alpaca_executor
    if alpaca_executor.is_alpaca_routed(symbol):
        return alpaca_executor.get_order_status(stop_order_id)
    return None


def renew_broker_stop_if_expired(symbol: str, position: dict) -> None:
    """
    Legacy : kept for backward compat. Préférer reconcile_broker_stop() qui
    gère aussi le cas filled. No-op si pas d'alpaca_stop_id ou status actif.
    """
    result = reconcile_broker_stop(symbol, position)
    # Volontairement ignore le cas "filled" ici — caller utilise l'API complète
    return None


def reconcile_broker_stop(symbol: str, position: dict) -> tuple[str, object]:
    """
    Vérifie l'état d'un broker stop et agit en conséquence. Retourne un tuple
    (action, data) :

      ("ok", None)             — stop actif ou pas de stop_id (no-op)
      ("renewed", new_id)      — stop expired/canceled re-placé (position muté)
      ("filled", filled_price) — stop déclenché → caller doit fermer la position
      ("error", err_msg)       — réseau/api error
      ("orphan", None)         — re-place échoué après expired (alpaca_stop_id reset)

    Cas "filled" : ne touche PAS au state, le caller est responsable de fermer
    la position (PnL, capital, trades, suppression). Permet aux différentes
    stratégies de gérer le close à leur façon.
    """
    sid = position.get("alpaca_stop_id")
    if not sid:
        return ("ok", None)

    from live import alpaca_executor
    if not alpaca_executor.is_alpaca_routed(symbol):
        return ("ok", None)

    order = alpaca_executor.get_order(sid)
    if order is None:
        return ("error", "fetch failed")

    status = order.get("status")

    if status == "filled":
        filled_price = float(order.get("filled_avg_price") or position.get("stop") or 0)
        return ("filled", filled_price)

    if status in ("expired", "canceled", "rejected", "missing"):
        ids = place_broker_stop(symbol, position["size"], position["stop"])
        new_id = ids.get("stop_id")
        if new_id:
            position["alpaca_stop_id"] = new_id
            logger.info(f"[STOP-RENEW] {symbol} re-placé après {status} @ {position['stop']:.4f}")
            return ("renewed", new_id)
        else:
            position["alpaca_stop_id"] = None
            logger.warning(f"[STOP-RENEW] {symbol} échec re-place ({status}) — SL interne uniquement")
            return ("orphan", None)

    return ("ok", None)


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
    # ── Routing : stocks Alpaca (avant le check PAPER_TRADING global) ───────
    # Stocks (NVDA, GOOGL, ...) → Alpaca (paper/live selon APCA_API_BASE_URL,
    # indépendant de config.PAPER_TRADING). Cryptos (BTC/USD, ...) → Kraken.
    from live import alpaca_executor
    if alpaca_executor.is_alpaca_routed(symbol):
        return alpaca_executor.execute_buy(symbol, size, price_estimate, max_wait_sec)

    if config.PAPER_TRADING:
        # Kraken paper : simulation sans appel API
        effective_price = price_estimate * (1 + config.SLIPPAGE)
        return OrderResult(success=True, order_id="PAPER", filled_size=size,
                           filled_price=effective_price)

    # ── LIVE Kraken ──
    # Pré-check : symbole validé au démarrage ?
    if _VALID_SYMBOLS_CACHE and symbol not in _VALID_SYMBOLS_CACHE:
        logger.warning(f"[ORDER] BUY {symbol} skip — symbole non validé Kraken (silent)")
        return OrderResult(success=False, error="symbol_not_supported")

    # Pré-check : montant total < MIN_ORDER_EUR → skip
    order_value = size * price_estimate
    if order_value < config.MIN_ORDER_EUR:
        logger.warning(f"[ORDER] BUY {symbol} skip — montant {order_value:.2f}$ < min {config.MIN_ORDER_EUR}$")
        return OrderResult(success=False, error=f"min_order_size: {order_value:.2f}$ < {config.MIN_ORDER_EUR}$")

    # ── xStocks : bypass ccxt via raw Kraken API ──
    if _is_xstock(symbol):
        logger.info(f"[ORDER] BUY {symbol} (xStock raw) size={size:.6f} @ ~{price_estimate:.4f}$ ({order_value:.2f}$)")
        result = _execute_xstock_order(symbol, "buy", size, price_estimate)
        if result.success:
            notify(f"✅ <b>LIVE BUY</b> {symbol}\nTaille: {size:.6f} @ ~{price_estimate:.4f}$\nOrdre: {result.order_id}")
        return result

    try:
        exchange = get_exchange(use_auth=True)
        logger.info(f"[ORDER] BUY {symbol} size={size:.6f} @ ~{price_estimate:.4f}$ ({order_value:.2f}$)")

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
        err_msg = str(e)
        logger.error(f"[ORDER] BUY {symbol} ÉCHOUÉ: {err_msg}")
        if not _is_silent_kraken_error(err_msg):
            notify(f"⛔ <b>LIVE BUY ÉCHOUÉ</b> {symbol}\nErreur: {err_msg[:200]}")
        return OrderResult(success=False, error=err_msg)


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
    # ── Routing : stocks Alpaca (avant PAPER_TRADING global) ───────────────
    from live import alpaca_executor
    if alpaca_executor.is_alpaca_routed(symbol):
        return alpaca_executor.execute_sell(symbol, size, price_estimate, reason, max_wait_sec)

    if config.PAPER_TRADING:
        effective_price = price_estimate * (1 - config.SLIPPAGE)
        return OrderResult(success=True, order_id="PAPER", filled_size=size,
                           filled_price=effective_price)

    # ── LIVE Kraken ──
    if _VALID_SYMBOLS_CACHE and symbol not in _VALID_SYMBOLS_CACHE:
        logger.warning(f"[ORDER] SELL {symbol} skip — symbole non validé")
        return OrderResult(success=False, error="symbol_not_supported")

    # ── xStocks : bypass ccxt ──
    if _is_xstock(symbol):
        logger.info(f"[ORDER] SELL {symbol} (xStock raw) size={size:.6f} @ ~{price_estimate:.4f}$ ({reason})")
        result = _execute_xstock_order(symbol, "sell", size, price_estimate)
        if result.success:
            icon = "🔴" if "stop" in reason else "⏹"
            notify(f"{icon} <b>LIVE SELL</b> {symbol} [{reason}]\nTaille: {size:.6f} @ ~{price_estimate:.4f}$")
        return result

    try:
        exchange = get_exchange(use_auth=True)
        logger.info(f"[ORDER] SELL {symbol} size={size:.6f} @ ~{price_estimate:.4f}$ ({reason})")

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
        err_msg = str(e)
        logger.error(f"[ORDER] SELL {symbol} ÉCHOUÉ: {err_msg}")
        if not _is_silent_kraken_error(err_msg):
            notify(f"🚨 <b>LIVE SELL ÉCHOUÉ</b> {symbol} [{reason}]\n"
                   f"Erreur: {err_msg}\n⚠️ Position ouverte — intervention manuelle requise")
        return OrderResult(success=False, error=err_msg)


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
    Récupère le solde de la devise de référence (USD ou EUR) du compte Kraken.
    Auto-détecte la devise selon config.SYMBOLS (premier pair).

    Returns:
        Solde disponible dans la devise de quote, -1.0 si erreur connexion.
    """
    if config.PAPER_TRADING:
        return 0.0

    # Détecte devise de quote depuis le 1er symbole de config
    quote_ccy = "USD"
    if config.SYMBOLS:
        first = config.SYMBOLS[0]
        if "/" in first:
            quote_ccy = first.split("/")[1]

    try:
        exchange = get_exchange(use_auth=True)
        balance = exchange.fetch_balance()
        amount = float(balance.get(quote_ccy, {}).get("free", 0))
        logger.info(f"[ORDER] Solde Kraken : {amount:.2f} {quote_ccy} disponibles")
        return amount
    except Exception as e:
        logger.error(f"[ORDER] check_balance ÉCHOUÉ: {e}")
        notify(f"⛔ <b>Kraken check_balance ÉCHOUÉ</b>\nErreur: {e}\n"
               f"Vérifier les clés API et la connexion réseau.")
        return -1.0


def check_total_value() -> float:
    """
    Valeur totale du compte (cash + positions converties dans la devise de référence).
    Permet de redémarrer après crash même si tout est alloué en positions.

    Returns:
        Valeur totale, -1.0 si erreur connexion.
    """
    if config.PAPER_TRADING:
        return 0.0

    quote_ccy = "USD"
    if config.SYMBOLS and "/" in config.SYMBOLS[0]:
        quote_ccy = config.SYMBOLS[0].split("/")[1]

    try:
        exchange = get_exchange(use_auth=True)
        balance = exchange.fetch_balance()
        total = float(balance.get(quote_ccy, {}).get("total", 0))

        for asset, info in balance.items():
            if asset in (quote_ccy, "info", "free", "used", "total", "timestamp", "datetime"):
                continue
            qty = float(info.get("total", 0)) if isinstance(info, dict) else 0
            if qty <= 0.0001:
                continue
            try:
                ticker = exchange.fetch_ticker(f"{asset}/{quote_ccy}")
                last = float(ticker.get("last") or ticker.get("close") or 0)
                total += qty * last
            except Exception:
                logger.warning(f"[ORDER] Impossible de valoriser {asset} en {quote_ccy}")
        return total
    except Exception as e:
        logger.error(f"[ORDER] check_total_value ÉCHOUÉ: {e}")
        return -1.0


# Aliases pour rétrocompat
check_total_value_eur = check_total_value


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

    # 0. Validate symbols (filter config.SYMBOLS to only tradable ones)
    valid_syms, invalid_syms = validate_symbols(config.SYMBOLS)
    logger.info(f"[ORDER] Symbols validés: {len(valid_syms)}/{len(config.SYMBOLS)}")
    if invalid_syms:
        logger.warning(f"[ORDER] Symbols invalides exclus: {invalid_syms}")
        notify(f"⚠️ <b>{len(invalid_syms)} symbols exclus</b>\n{', '.join(invalid_syms)}")

    # Détecte devise de quote
    quote_ccy = "USD"
    if config.SYMBOLS and "/" in config.SYMBOLS[0]:
        quote_ccy = config.SYMBOLS[0].split("/")[1]

    # 1. Connexion + balance cash
    cash = check_balance()
    if cash < 0:
        notify("⛔ <b>LIVE STARTUP ÉCHOUÉ</b>\nConnexion Kraken impossible.\nBot arrêté.")
        return False

    # 2. Si cash = 0, vérifier positions
    if cash == 0:
        total = check_total_value()
        if total <= 0:
            notify(f"⛔ <b>LIVE STARTUP ÉCHOUÉ</b>\nCompte vide (0 {quote_ccy} + 0 positions).\nBot arrêté.")
            return False
        logger.info(f"[ORDER] {quote_ccy} free=0 mais positions valorisées à {total:.2f} {quote_ccy} — démarrage OK")
        notify(f"✅ <b>Connexion Kraken OK</b>\n{quote_ccy} libre: 0 | Total compte: <b>{total:.2f} {quote_ccy}</b>\n"
               f"(capital alloué en positions)")
        return True

    logger.info(f"[ORDER] ✓ Solde Kraken: {cash:.2f} {quote_ccy}")
    notify(f"✅ <b>Connexion Kraken OK</b>\nSolde disponible: <b>{cash:.2f} {quote_ccy}</b>")
    return True

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
import math
import sys
import os
import re
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
    """True si le symbole est un stock US (pas de slash, alphanumérique).

    Exemples : "NVDA"→True, "GOOGL"→True, "BRK.B"→True, "BTC/USD"→False, "NVDAx/USD"→False.
    """
    if "/" in symbol or not symbol:
        return False
    return symbol.replace(".", "").isalnum() and symbol == symbol.upper()


def is_alpaca_crypto(symbol: str) -> bool:
    """True si le symbole est routé vers Alpaca pour le crypto.

    Activé quand ALPACA_CRYPTO=true (default true si URL est paper-api ; en live
    Alpaca crypto reste US-only, donc à mettre false pour les non-US).
    """
    if "/" not in symbol:
        return False
    flag = os.getenv("ALPACA_CRYPTO", "auto").lower()
    if flag == "true":
        return True
    if flag == "false":
        return False
    # auto : true si endpoint paper, false sinon
    return _is_paper_endpoint()


def is_alpaca_routed(symbol: str) -> bool:
    """True si le symbole doit être routé vers Alpaca (stocks toujours, crypto sous flag)."""
    return is_alpaca_stock(symbol) or is_alpaca_crypto(symbol)


# ── HTTP helpers ─────────────────────────────────────────────────────────────

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


def _request(method: str, path: str, body: dict = None, timeout: int = 30) -> dict:
    """
    Requête authentifiée vers Alpaca REST avec retry exponentiel sur transient
    errors (429, 5xx, network errors). Max 3 retries, backoff 1s/2s/4s.
    Lève RuntimeError sur 4xx (sauf 429) avec le message d'erreur Alpaca.
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

    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
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
            last_err = RuntimeError(f"Alpaca {e.code}: {msg}")
            # Retry only on transient statuses
            if e.code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning(f"[ALPACA] {method} {path} → {e.code} (retry {attempt+1}/{_MAX_RETRIES} dans {wait}s)")
                time.sleep(wait)
                continue
            raise last_err from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # Network/timeout — retry
            last_err = RuntimeError(f"Alpaca network: {e}")
            if attempt < _MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning(f"[ALPACA] {method} {path} network err (retry {attempt+1}/{_MAX_RETRIES} dans {wait}s): {e}")
                time.sleep(wait)
                continue
            raise last_err from None
    if last_err:
        raise last_err


# ── Market data API (data.alpaca.markets) ────────────────────────────────────

_DATA_BASE = "https://data.alpaca.markets"

# Mapping timeframe interne → Alpaca bars timeframe
_ALPACA_TF = {
    "1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
    "1h": "1Hour", "4h": "4Hour", "1d": "1Day",
}


def _data_request(path: str, params: dict, timeout: int = 30) -> dict:
    """GET sur data.alpaca.markets avec auth standard."""
    qs = urllib.parse.urlencode(params)
    url = f"{_DATA_BASE}{path}?{qs}"
    headers = {
        "APCA-API-KEY-ID": _api_key(),
        "APCA-API-SECRET-KEY": _api_secret(),
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_alpaca_ohlcv(symbol: str, timeframe: str = "4h", days: int = 55):
    """Fetch OHLCV stocks via Alpaca data API (cohérence prix data ↔ exec).

    Retourne pd.DataFrame indexé UTC avec colonnes open/high/low/close/volume.
    Lève RuntimeError sur erreur — caller fallback yfinance si voulu.
    """
    import pandas as pd
    from datetime import datetime, timedelta, timezone

    tf = _ALPACA_TF.get(timeframe.lower())
    if tf is None:
        raise ValueError(f"Timeframe non supporté Alpaca data: {timeframe}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    # Alpaca free tier (IEX) interdit fenêtres incluant les 15 dernières minutes
    end_safe = end - timedelta(minutes=20)

    params = {
        "timeframe": tf,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":   end_safe.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000,
        "adjustment": "split",  # price-adjusted pour splits, pas dividends
        "feed": os.getenv("ALPACA_DATA_FEED", "iex"),  # iex (free) ou sip (paid)
    }

    bars = []
    page_token = None
    print(f"  Téléchargement {symbol} [{tf}] — {days} jours (via Alpaca data)...")
    for _ in range(20):  # cap 20 pages = 200k bars max
        if page_token:
            params["page_token"] = page_token
        resp = _data_request(f"/v2/stocks/{symbol}/bars", params)
        bars.extend(resp.get("bars") or [])
        page_token = resp.get("next_page_token")
        if not page_token:
            break

    if not bars:
        raise RuntimeError(f"Aucune donnée Alpaca pour {symbol}")

    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]
    df = df[~df.index.duplicated(keep="last")]
    print(f"  ✓ {len(df)} bougies {symbol} en USD ({df.index[0].date()} → {df.index[-1].date()})")
    return df


# ── Account / balance ────────────────────────────────────────────────────────

def check_balance() -> float:
    """
    Cash USD disponible sur le compte Alpaca (paper ou live selon APCA_API_BASE_URL).
    Indépendant de config.PAPER_TRADING : Alpaca a son propre paper via URL.
    Returns -1.0 si erreur connexion.
    """
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

    Note : Alpaca a son propre mode paper/live via APCA_API_BASE_URL — on ignore
    config.PAPER_TRADING (qui ne concerne que Kraken). Si l'URL pointe sur
    paper-api.alpaca.markets, c'est de l'argent virtuel ; sinon c'est du live.
    """
    if _VALID_ALPACA_SYMBOLS and symbol not in _VALID_ALPACA_SYMBOLS:
        logger.warning(f"[ALPACA] BUY {symbol} skip — symbole non validé")
        return OrderResult(success=False, error="symbol_not_supported")

    # Crypto Alpaca → time_in_force=gtc + min cost basis 10$
    # Stocks Alpaca → time_in_force=day
    is_crypto = "/" in symbol
    tif = "gtc" if is_crypto else "day"
    order_value = size * price_estimate
    if is_crypto and order_value < 10.0:
        logger.warning(f"[ALPACA] BUY {symbol} skip — crypto min 10$ Alpaca, ordre {order_value:.2f}$")
        return OrderResult(success=False, error=f"alpaca_crypto_min: {order_value:.2f}$ < 10$")

    payload = {
        "symbol": symbol,
        "qty": str(size),
        "side": "buy",
        "type": "market",
        "time_in_force": tif,
    }
    try:
        logger.info(f"[ALPACA] BUY {symbol} qty={size:.6f} @ ~{price_estimate:.4f}$ ({order_value:.2f}$)")
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

        # POST-FILL CLAMP (iter-9): pour crypto, Alpaca déduit les fees en base
        # asset (25 bps). La qty fillée est NOMINALE — la qty disponible côté
        # broker est inférieure. Sans ce clamp, le bot stocke 1479.12 AVAX dans
        # son state alors que /v2/positions n'a que 1476.03 → SELL ultérieur
        # échoue "insufficient balance" (incident AVAX 2026-05-13, -€494).
        # Fix : refetch /v2/positions juste après fill et utiliser cette qty.
        if is_crypto:
            time.sleep(0.5)  # laisser Alpaca propager le fill vers /v2/positions
            try:
                broker_qty = _fetch_position_qty(symbol)
                if broker_qty is not None and broker_qty > 0 and broker_qty < filled_qty:
                    decimals = 6
                    factor = 10 ** decimals
                    clamped = math.floor(broker_qty * factor) / factor
                    logger.warning(
                        f"[ALPACA] BUY {symbol} post-fill clamp {filled_qty:.6f}→{clamped:.6f} "
                        f"(fee crypto en base asset)"
                    )
                    filled_qty = clamped
            except Exception as e:
                logger.warning(f"[ALPACA] post-fill clamp {symbol} skip: {e}")

        # Notif BUY fill: déléguée au caller (bot.py via buffer_buy) — évite double notif
        return OrderResult(success=True, order_id=order_id,
                           filled_size=filled_qty, filled_price=filled_avg)

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[ALPACA] BUY {symbol} ÉCHOUÉ: {err_msg}")
        # Erreur critique — notif immédiate
        from live.notifier import ICON_EXIT_LOSS
        notify(f"{ICON_EXIT_LOSS} <b>BUY {symbol} échec</b>\n<code>{err_msg[:160]}</code>")
        return OrderResult(success=False, error=err_msg)


def execute_sell(symbol: str, size: float, price_estimate: float,
                 reason: str = "exit", max_wait_sec: int = 30) -> OrderResult:
    """Place un ordre SELL market sur Alpaca. Voir execute_buy pour le mode paper/live."""
    if _VALID_ALPACA_SYMBOLS and symbol not in _VALID_ALPACA_SYMBOLS:
        logger.warning(f"[ALPACA] SELL {symbol} skip — symbole non validé")
        return OrderResult(success=False, error="symbol_not_supported")

    is_crypto = "/" in symbol
    tif = "gtc" if is_crypto else "day"

    # Pre-clamp via qty_available — évite "insufficient balance" sur crypto
    # (fees Alpaca déduites en base asset : achat 1479 AVAX, balance réelle 1476)
    if is_crypto:
        broker_qty = _fetch_position_qty(symbol)
        if broker_qty is not None and broker_qty > 0 and size > broker_qty:
            decimals = 6
            factor = 10 ** decimals
            new_qty = math.floor(broker_qty * factor) / factor
            logger.warning(
                f"[ALPACA] SELL {symbol} clamp {size:.6f}→{new_qty:.6f} "
                f"(broker available, fee crypto en base asset)"
            )
            size = new_qty

    payload = {
        "symbol": symbol,
        "qty": str(size),
        "side": "sell",
        "type": "market",
        "time_in_force": tif,
    }
    try:
        logger.info(f"[ALPACA] SELL {symbol} qty={size:.6f} @ ~{price_estimate:.4f}$ ({reason})")
        order = _request("POST", "/v2/orders", body=payload)
        order_id = order.get("id", "?")

        filled = _wait_for_fill(order_id, max_wait_sec)
        if filled is None:
            logger.error(f"[ALPACA] SELL {order_id} non rempli après {max_wait_sec}s")
            from live.notifier import ICON_CRITICAL
            notify(f"{ICON_CRITICAL} <b>SELL {symbol} non rempli</b>\n"
                   f"Timeout {max_wait_sec}s [{reason}] — intervention manuelle requise")
            return OrderResult(success=False, order_id=order_id,
                               error=f"Timeout {max_wait_sec}s")

        filled_qty = float(filled.get("filled_qty", size))
        filled_avg = float(filled.get("filled_avg_price") or price_estimate)
        # Notif SELL fill: déléguée au caller (bot.py via buffer_sell) — évite double notif
        return OrderResult(success=True, order_id=order_id,
                           filled_size=filled_qty, filled_price=filled_avg)

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[ALPACA] SELL {symbol} ÉCHOUÉ: {err_msg}")
        from live.notifier import ICON_CRITICAL
        notify(f"{ICON_CRITICAL} <b>SELL {symbol} échec [{reason}]</b>\n"
               f"<code>{err_msg[:160]}</code>\nIntervention manuelle requise")
        return OrderResult(success=False, error=err_msg)


# ── Stop-loss broker-side (protection même si bot down) ──────────────────────

def _fetch_position_qty(symbol: str, retries: int = 3, wait_s: float = 0.3) -> float | None:
    """
    GET /v2/positions/{symbol} → qty disponible côté broker (qty_available).
    None si pas de position ouverte ou erreur.

    Alpaca attend le symbole SANS slash dans le path pour les crypto :
      BTC/USD → /v2/positions/BTCUSD  (et non BTC%2FUSD qui renvoie 404).
    Les stocks (NVDA, JPM…) ne contiennent pas de slash → comportement inchangé.

    Retry sur 404 : juste après un fill ou un SELL partiel, /v2/positions peut
    renvoyer 404 ou un état pas encore consistant (lag Alpaca interne). On retry
    quelques fois pour laisser l'état se propager avant de renoncer.
    """
    sym_encoded = urllib.parse.quote(symbol.replace("/", ""), safe="")
    last_err = None
    for attempt in range(retries):
        try:
            p = _request("GET", f"/v2/positions/{sym_encoded}")
            # qty_available = qty - qty bloquée par d'autres ordres ouverts
            avail = p.get("qty_available") or p.get("qty") or 0
            return float(avail)
        except Exception as e:
            last_err = str(e)
            if "404" in last_err and attempt < retries - 1:
                time.sleep(wait_s)
                continue
            return None
    return None


_INSUFFICIENT_BALANCE_RE = re.compile(r"available:\s*([\d.]+)", re.IGNORECASE)


def get_spread_pct(symbol: str) -> float | None:
    """
    Retourne le spread relatif (ask-bid)/mid en %. None si erreur ou pas de quote.

    Usage : avant de placer un ordre sur un symbole illiquide pour skip si spread
    > seuil (ex: 1%). En paper Alpaca le spread n'est pas réaliste — fonction
    no-op-friendly retournant 0.0 sur paper. À activer en live.
    """
    if _is_paper_endpoint():
        return 0.0  # Paper Alpaca : pas de spread réaliste, skip check
    try:
        # Endpoint quotes (data API, pas trading)
        sym_encoded = urllib.parse.quote(symbol, safe="")
        if "/" in symbol:  # crypto
            url_path = f"/v1beta3/crypto/us/latest/quotes?symbols={sym_encoded}"
        else:
            url_path = f"/v2/stocks/quotes/latest?symbols={sym_encoded}"
        # Note : data.alpaca.markets a un base URL différent — wrap nécessaire
        # mais pour l'instant on stub : retourne 0 sauf erreur réseau.
        # TODO live : implémenter le vrai fetch contre data.alpaca.markets
        return 0.0
    except Exception:
        return None


def list_positions() -> dict:
    """
    GET /v2/positions → dict {symbol: position_dict} indexé par symbol normalisé.

    Alpaca retourne les cryptos sans slash (BTCUSD), on normalise vers le format
    avec slash (BTC/USD) pour matcher config.CRYPTO.

    Position dict contient : qty, qty_available, market_value, avg_entry_price,
    unrealized_pl, current_price, etc.
    Retourne {} si erreur.
    """
    try:
        positions = _request("GET", "/v2/positions")
        if not isinstance(positions, list):
            return {}
        result = {}
        for p in positions:
            sym = p.get("symbol", "")
            # Normalize crypto: BTCUSD → BTC/USD (3 chars base + USD/USDT/USDC suffix)
            if p.get("asset_class") == "crypto" and "/" not in sym:
                for suffix in ("USDT", "USDC", "USD"):
                    if sym.endswith(suffix) and len(sym) > len(suffix):
                        sym = f"{sym[:-len(suffix)]}/{suffix}"
                        break
            result[sym] = p
        return result
    except Exception as e:
        logger.warning(f"[ALPACA] list_positions: {e}")
        return {}


def place_stop_loss(symbol: str, qty: float, stop_price: float,
                    take_profit_price: float | None = None) -> dict:
    """
    Place un ordre STOP SELL chez Alpaca (protège la position même si le bot crashe).

    Si take_profit_price fourni ET qty entière, place ordre OCO (stop + TP).
    Sinon (fractional ou crypto), stop seul (TP géré bot-side).

    Retourne dict {"stop_id": ..., "tp_id": ...} ou {} si échec (ne raise pas
    pour ne pas bloquer le bot — la position reste ouverte sans broker-side stop).
    """
    import math
    is_crypto = "/" in symbol
    # Clamp qty à la quantité réellement disponible chez Alpaca (évite
    # "insufficient qty" quand state.json a un arrondi > position broker réelle).
    real_qty = _fetch_position_qty(symbol)
    if real_qty is not None and real_qty > 0:
        qty = min(float(qty), real_qty)
    # Down-round qty : crypto 6 décimales, stocks 5 (Alpaca round display à 5 dec
    # et l'interpréte comme la qty demandée → "insufficient qty" si on dépasse)
    decimals = 6 if is_crypto else 5
    factor = 10 ** decimals
    qty = math.floor(float(qty) * factor) / factor

    # Stocks fractionnaires Alpaca exigent time_in_force=day (gtc refusé)
    # Crypto Alpaca : gtc OK
    tif = "gtc" if is_crypto else "day"

    def _build_payload(q: float, allow_oco: bool) -> tuple[dict, str]:
        """Retourne (payload, kind_label) pour la qty donnée."""
        is_frac = q != int(q)
        oco = (allow_oco
               and take_profit_price is not None
               and not is_crypto
               and not is_frac)
        if oco:
            return {
                "symbol": symbol,
                "qty": str(q),
                "side": "sell",
                "type": "limit",
                "limit_price": str(round(take_profit_price, 2)),
                "time_in_force": tif,
                "order_class": "oco",
                "stop_loss": {"stop_price": str(round(stop_price, 2))},
                "take_profit": {"limit_price": str(round(take_profit_price, 2))},
            }, "OCO"
        if is_crypto:
            # Crypto Alpaca refuse type=stop simple → stop_limit avec limit 1%
            # sous le stop_price (sécurise le fill malgré la volatilité).
            return {
                "symbol": symbol,
                "qty": str(q),
                "side": "sell",
                "type": "stop_limit",
                "stop_price": str(round(stop_price, 2)),
                "limit_price": str(round(stop_price * 0.99, 2)),
                "time_in_force": tif,
            }, "STOP-LIMIT"
        return {
            "symbol": symbol,
            "qty": str(q),
            "side": "sell",
            "type": "stop",
            "stop_price": str(round(stop_price, 2)),
            "time_in_force": tif,
        }, "STOP"

    # 2 tentatives max : si la 1re échoue avec "insufficient balance", on extrait
    # la qty réellement disponible du message Alpaca et on retry une fois.
    for attempt in (1, 2):
        payload, kind = _build_payload(qty, allow_oco=True)
        try:
            o = _request("POST", "/v2/orders", body=payload)
            if kind == "OCO":
                legs = o.get("legs") or []
                stop_id = next((l["id"] for l in legs if l.get("type") == "stop"), o.get("id"))
                tp_id = next((l["id"] for l in legs if l.get("type") == "limit"), None)
                logger.info(f"[ALPACA] OCO {symbol} stop={stop_price:.2f} tp={take_profit_price:.2f}")
                return {"stop_id": stop_id, "tp_id": tp_id, "parent_id": o.get("id")}
            logger.info(f"[ALPACA] {kind} {symbol} qty={qty:.6f} @ {stop_price:.2f}$")
            return {"stop_id": o.get("id")}
        except Exception as e:
            err_msg = str(e)
            if attempt == 1 and "insufficient balance" in err_msg.lower():
                m = _INSUFFICIENT_BALANCE_RE.search(err_msg)
                if m:
                    avail = math.floor(float(m.group(1)) * factor) / factor
                    if 0 < avail < qty:
                        logger.warning(
                            f"[ALPACA] place_stop_loss {symbol} clamp {qty:.6f}→{avail:.6f} "
                            f"(broker available, fee crypto en base asset probable)"
                        )
                        qty = avail
                        continue
            logger.warning(f"[ALPACA] place_stop_loss {symbol} échec: {e} — bot SL interne reste actif")
            return {}
    return {}


def cancel_order(order_id: str) -> bool:
    """Cancel un ordre par id. True si OK ou déjà fillé/canceled."""
    if not order_id:
        return False
    try:
        _request("DELETE", f"/v2/orders/{order_id}")
        return True
    except Exception as e:
        msg = str(e)
        if "422" in msg or "not cancelable" in msg.lower():
            return True  # déjà fillé/canceled, OK
        logger.warning(f"[ALPACA] cancel_order {order_id}: {e}")
        return False


def get_order_status(order_id: str) -> str | None:
    """
    Retourne le status d'un ordre Alpaca ('new', 'filled', 'expired', 'canceled',
    'rejected', 'partially_filled', 'pending_new', 'accepted', etc.).
    None si erreur ou 404 (ordre purgé après ~30j chez Alpaca).
    """
    if not order_id:
        return None
    try:
        o = _request("GET", f"/v2/orders/{order_id}")
        return o.get("status")
    except Exception as e:
        msg = str(e)
        if "404" in msg or "not found" in msg.lower():
            return "missing"
        logger.warning(f"[ALPACA] get_order_status {order_id}: {e}")
        return None


def get_order(order_id: str) -> dict | None:
    """
    Retourne le dict complet d'un ordre Alpaca (status, filled_avg_price,
    filled_qty, filled_at, etc.). None si erreur. Dict avec status='missing'
    si 404 (ordre purgé) — permet au caller de discriminer sans 2e appel.
    """
    if not order_id:
        return None
    try:
        return _request("GET", f"/v2/orders/{order_id}")
    except Exception as e:
        msg = str(e)
        if "404" in msg or "not found" in msg.lower():
            return {"status": "missing"}
        logger.warning(f"[ALPACA] get_order {order_id}: {e}")
        return None


def replace_stop_loss(stop_order_id: str, new_stop_price: float,
                      qty: float | None = None) -> str | None:
    """
    Update un ordre stop existant (trailing). Si Alpaca refuse PATCH, fallback
    cancel + recreate. Retourne le nouvel order_id ou None si échec.

    Retry sur "insufficient balance" : si la qty bot est supérieure à la balance
    broker réelle (cas fee crypto 25 bps en base asset), parse le message Alpaca
    et retry une fois avec la qty effectivement disponible. Évite le double appel
    API du fallback cancel+recreate dans ce cas connu.
    """
    import math

    def _try_patch(q):
        body = {"stop_price": str(round(new_stop_price, 2))}
        if q is not None:
            body["qty"] = str(q)
        o = _request("PATCH", f"/v2/orders/{stop_order_id}", body=body)
        return o.get("id") or stop_order_id

    current_qty = qty
    for attempt in (1, 2):
        try:
            return _try_patch(current_qty)
        except Exception as e:
            err_msg = str(e)
            if (attempt == 1 and current_qty is not None
                    and "insufficient balance" in err_msg.lower()):
                m = _INSUFFICIENT_BALANCE_RE.search(err_msg)
                if m:
                    avail = float(m.group(1))
                    # Détermine les décimales selon le symbole de l'ordre
                    try:
                        order_info = _request("GET", f"/v2/orders/{stop_order_id}")
                        sym = order_info.get("symbol", "") or ""
                        decimals = 6 if "/" in sym else 5
                    except Exception:
                        decimals = 6  # default crypto-safe
                    factor = 10 ** decimals
                    new_qty = math.floor(avail * factor) / factor
                    if 0 < new_qty < current_qty:
                        logger.warning(
                            f"[ALPACA] PATCH stop {stop_order_id} clamp qty "
                            f"{current_qty:.6f}→{new_qty:.6f} (broker available)"
                        )
                        current_qty = new_qty
                        continue
            logger.warning(f"[ALPACA] PATCH stop {stop_order_id} échec ({e}) — fallback cancel+recreate")
            # Fallback : cancel old + recreate (perd l'id, le caller doit récupérer)
            cancel_order(stop_order_id)
            return None  # caller doit appeler place_stop_loss à nouveau
    return None


# ── Startup check ────────────────────────────────────────────────────────────

def startup_check() -> bool:
    """Sanity check démarrage. Notify seulement si problème (skip OK routine)."""
    try:
        acct = _request("GET", "/v2/account")
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            notify(f"⛔ Alpaca BLOQUÉ status={acct.get('status')} trading_blocked={acct.get('trading_blocked')}")
            return False
        cash = float(acct.get("cash", 0))
        equity = float(acct.get("equity", 0))
        endpoint = "paper" if _is_paper_endpoint() else "LIVE"
        # Log seulement, pas de notify si OK (réduit le spam au restart)
        logger.info(f"[ALPACA] Startup {endpoint} OK — cash={cash:.2f}$ equity={equity:.2f}$")
        # Notify uniquement si bascule vers LIVE (événement rare et important)
        if endpoint == "LIVE":
            notify(f"🟢 Alpaca LIVE startup OK — equity {equity:.0f}$")
        return True
    except Exception as e:
        logger.error(f"[ALPACA] startup_check ÉCHOUÉ: {e}")
        notify(f"⛔ Alpaca startup échec: {str(e)[:80]}")
        return False

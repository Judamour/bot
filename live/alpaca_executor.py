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
    """Place un ordre SELL market sur Alpaca. Voir execute_buy pour le mode paper/live."""
    if _VALID_ALPACA_SYMBOLS and symbol not in _VALID_ALPACA_SYMBOLS:
        logger.warning(f"[ALPACA] SELL {symbol} skip — symbole non validé")
        return OrderResult(success=False, error="symbol_not_supported")

    is_crypto = "/" in symbol
    tif = "gtc" if is_crypto else "day"
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


# ── Stop-loss broker-side (protection même si bot down) ──────────────────────

def _fetch_position_qty(symbol: str) -> float | None:
    """
    GET /v2/positions/{symbol} → qty disponible côté broker (qty_available).
    None si pas de position ouverte ou erreur.

    Permet de clamp la qty d'un stop pour éviter "insufficient qty" quand
    le state.json a un arrondi légèrement supérieur à la position Alpaca réelle.
    """
    try:
        sym_encoded = urllib.parse.quote(symbol, safe="")
        p = _request("GET", f"/v2/positions/{sym_encoded}")
        # qty_available = qty - qty bloquée par d'autres ordres ouverts
        avail = p.get("qty_available") or p.get("qty") or 0
        return float(avail)
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
    is_fractional = qty != int(qty)

    # Stocks fractionnaires Alpaca exigent time_in_force=day (gtc refusé)
    # Crypto Alpaca : gtc OK
    tif = "gtc" if is_crypto else "day"

    # OCO refusé sur fractional shares ET sur crypto paper → stop seul si l'un des deux
    use_oco = (take_profit_price is not None
               and not is_crypto
               and not is_fractional)

    try:
        if use_oco:
            payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": "sell",
                "type": "limit",
                "limit_price": str(round(take_profit_price, 2)),
                "time_in_force": tif,
                "order_class": "oco",
                "stop_loss": {"stop_price": str(round(stop_price, 2))},
                "take_profit": {"limit_price": str(round(take_profit_price, 2))},
            }
            o = _request("POST", "/v2/orders", body=payload)
            legs = o.get("legs") or []
            stop_id = next((l["id"] for l in legs if l.get("type") == "stop"), o.get("id"))
            tp_id   = next((l["id"] for l in legs if l.get("type") == "limit"), None)
            logger.info(f"[ALPACA] OCO {symbol} stop={stop_price:.2f} tp={take_profit_price:.2f}")
            return {"stop_id": stop_id, "tp_id": tp_id, "parent_id": o.get("id")}

        # Crypto Alpaca refuse type=stop simple → stop_limit avec limit 1% sous
        # le stop_price (sécurise le fill malgré la volatilité crypto).
        if is_crypto:
            limit_price = round(stop_price * 0.99, 2)
            payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": "sell",
                "type": "stop_limit",
                "stop_price": str(round(stop_price, 2)),
                "limit_price": str(limit_price),
                "time_in_force": tif,
            }
        else:
            payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": "sell",
                "type": "stop",
                "stop_price": str(round(stop_price, 2)),
                "time_in_force": tif,
            }
        o = _request("POST", "/v2/orders", body=payload)
        kind = "STOP-LIMIT" if is_crypto else "STOP"
        logger.info(f"[ALPACA] {kind} {symbol} qty={qty:.6f} @ {stop_price:.2f}$")
        return {"stop_id": o.get("id")}
    except Exception as e:
        logger.warning(f"[ALPACA] place_stop_loss {symbol} échec: {e} — bot SL interne reste actif")
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
    """
    try:
        body = {"stop_price": str(round(new_stop_price, 2))}
        if qty is not None:
            body["qty"] = str(qty)
        o = _request("PATCH", f"/v2/orders/{stop_order_id}", body=body)
        return o.get("id") or stop_order_id
    except Exception as e:
        logger.warning(f"[ALPACA] PATCH stop {stop_order_id} échec ({e}) — fallback cancel+recreate")
        # Fallback : cancel old + recreate (perd l'id, le caller doit récupérer)
        cancel_order(stop_order_id)
        return None  # caller doit appeler place_stop_loss à nouveau


# ── Startup check ────────────────────────────────────────────────────────────

def startup_check() -> bool:
    """Sanity check au démarrage : connexion + status compte. Indépendant de PAPER_TRADING."""
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

"""Wrapper Alpaca paper isolé pour le shadow bot.

Lit UNIQUEMENT les variables shadow (ALPACA_SHADOW_*). Aucun risque de
toucher au compte prod. Fonctions minimales : market buy/sell, place stop,
fetch account/positions.

GARDE-FOUS D'ISOLATION (validate_isolation()) :
- Vérifie que les vars shadow existent
- Vérifie qu'elles diffèrent des vars prod (key ET secret)
- Vérifie que base_url est le paper endpoint (pas live)
À appeler au démarrage du runner avant tout appel API.
"""
from __future__ import annotations
import os
import time
import json
import math
import re
import urllib.request
import urllib.parse

_INSUFFICIENT_RE = re.compile(r"available:\s*([\d.]+)", re.IGNORECASE)


def validate_isolation() -> None:
    """Fail-fast au démarrage : refuse de tourner si l'isolation est cassée.

    Évite les bugs catastrophiques où le shadow tirerait par erreur sur le
    compte prod (ordre dupliqué, exposition double, etc.).
    """
    shadow_key = os.environ.get("ALPACA_SHADOW_API_KEY", "")
    shadow_secret = os.environ.get("ALPACA_SHADOW_SECRET_KEY", "")
    prod_key = os.environ.get("ALPACA_API_KEY", "")
    prod_secret = os.environ.get("ALPACA_SECRET_KEY", "")
    base_url = os.environ.get("ALPACA_SHADOW_BASE_URL", "")

    if not shadow_key or not shadow_secret:
        raise RuntimeError(
            "Shadow isolation: ALPACA_SHADOW_API_KEY ou ALPACA_SHADOW_SECRET_KEY manquant"
        )
    if shadow_key == prod_key:
        raise RuntimeError(
            "Shadow isolation: ALPACA_SHADOW_API_KEY identique à ALPACA_API_KEY — "
            "ABORT (risque de tirer sur le compte prod)"
        )
    if shadow_secret == prod_secret:
        raise RuntimeError(
            "Shadow isolation: ALPACA_SHADOW_SECRET_KEY identique à ALPACA_SECRET_KEY — ABORT"
        )
    if "live" in base_url.lower() or "paper" not in base_url.lower():
        raise RuntimeError(
            f"Shadow isolation: ALPACA_SHADOW_BASE_URL doit pointer paper-api, got '{base_url}'"
        )


def _api_key() -> str:
    k = os.environ.get("ALPACA_SHADOW_API_KEY", "")
    if not k:
        raise RuntimeError("ALPACA_SHADOW_API_KEY missing in env")
    return k


def _api_secret() -> str:
    s = os.environ.get("ALPACA_SHADOW_SECRET_KEY", "")
    if not s:
        raise RuntimeError("ALPACA_SHADOW_SECRET_KEY missing in env")
    return s


def _base_url() -> str:
    return os.environ.get("ALPACA_SHADOW_BASE_URL", "https://paper-api.alpaca.markets")


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": _api_key(),
        "APCA-API-SECRET-KEY": _api_secret(),
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, body: dict | None = None, timeout: int = 15) -> dict:
    url = _base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


# ── Account ──────────────────────────────────────────────────────────────────

def get_account() -> dict:
    """Retourne equity/cash/buying_power du compte shadow."""
    return _request("GET", "/v2/account")


def get_positions() -> list:
    """Liste des positions ouvertes."""
    return _request("GET", "/v2/positions")


def get_position(symbol: str) -> dict | None:
    """Position d'un symbole (sans slash pour crypto)."""
    sym = symbol.replace("/", "")
    try:
        return _request("GET", f"/v2/positions/{urllib.parse.quote(sym, safe='')}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def get_open_orders() -> list:
    return _request("GET", "/v2/orders?status=open&limit=100")


def get_order(order_id: str) -> dict | None:
    """Fetch one order by id. Returns None on fetch failure or 404."""
    try:
        return _request("GET", f"/v2/orders/{order_id}")
    except Exception:
        return None


# ── Orders ───────────────────────────────────────────────────────────────────

def _wait_fill(order_id: str, max_wait_s: int = 30) -> dict | None:
    """Poll un ordre jusqu'à fill/cancel/expire. None si timeout."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            o = _request("GET", f"/v2/orders/{order_id}")
            if o.get("status") in ("filled", "canceled", "expired", "rejected"):
                return o
        except Exception:
            pass
        time.sleep(0.5)
    return None


def market_buy(symbol: str, qty: float) -> dict:
    """Market buy. Retourne {ok, id, filled_qty, filled_avg, status, error}.

    Pour crypto (24/7) : attend le fill (court).
    Pour stocks hors marché : retourne ok=True dès l'acceptation par Alpaca
    (l'ordre sera fillé à l'open). filled_qty=0 et filled_avg=0 dans ce cas —
    le caller doit lire les positions au cycle suivant pour avoir le fill réel.
    """
    is_crypto = "/" in symbol
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc" if is_crypto else "day",
    }
    try:
        o = _request("POST", "/v2/orders", body=payload)
        oid = o.get("id")
        # Crypto : on attend le fill (généralement < 2s)
        if is_crypto:
            filled = _wait_fill(oid, max_wait_s=15)
            if not filled or filled.get("status") != "filled":
                return {"ok": False, "id": oid,
                        "error": f"not filled (status={filled.get('status') if filled else 'timeout'})"}
            filled_qty = float(filled.get("filled_qty", qty))
            # POST-FILL CLAMP: Alpaca déduit fees en base asset (25 bps).
            # Refetch /v2/positions/{sym} pour avoir la qty réellement dispo,
            # évite mismatch state vs broker (cf incident AVAX 2026-05-13 sur Z).
            time.sleep(0.5)
            try:
                pos = get_position(symbol)
                if pos:
                    broker_qty = float(pos.get("qty_available") or pos.get("qty") or 0)
                    if 0 < broker_qty < filled_qty:
                        decimals = 6
                        factor = 10 ** decimals
                        clamped = math.floor(broker_qty * factor) / factor
                        print(f"[SHADOW] post-fill clamp {symbol} {filled_qty:.6f}→{clamped:.6f} (fee crypto)", flush=True)
                        filled_qty = clamped
            except Exception:
                pass
            return {
                "ok": True, "id": oid,
                "filled_qty": filled_qty,
                "filled_avg": float(filled.get("filled_avg_price") or 0),
                "status": "filled",
            }
        # Stock : check status immédiat. Si déjà fillé (marché ouvert), prendre le prix.
        # Sinon (queued/accepted), retour ok mais filled=0 — caller lit positions au prochain cycle.
        time.sleep(0.5)
        try:
            check = _request("GET", f"/v2/orders/{oid}")
            status = check.get("status", "")
        except Exception:
            status = "pending"
        if status in ("rejected", "canceled", "expired"):
            return {"ok": False, "id": oid, "error": f"rejected: status={status}"}
        if status == "filled":
            return {
                "ok": True, "id": oid,
                "filled_qty": float(check.get("filled_qty", qty)),
                "filled_avg": float(check.get("filled_avg_price") or 0),
                "status": "filled",
            }
        # Ordre en queue (accepted/pending_new/new/etc.) : OK, fill plus tard
        return {
            "ok": True, "id": oid,
            "filled_qty": 0.0, "filled_avg": 0.0,
            "status": status or "queued",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def market_sell(symbol: str, qty: float) -> dict:
    """Market sell. Clamp auto à qty_available si insufficient balance."""
    is_crypto = "/" in symbol
    # Pre-clamp via qty_available
    try:
        pos = get_position(symbol)
        if pos:
            avail = float(pos.get("qty_available") or pos.get("qty") or 0)
            if avail > 0 and qty > avail:
                decimals = 6 if is_crypto else 5
                factor = 10 ** decimals
                qty = math.floor(avail * factor) / factor
    except Exception:
        pass

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "gtc" if is_crypto else "day",
    }
    try:
        o = _request("POST", "/v2/orders", body=payload)
        oid = o.get("id")
        filled = _wait_fill(oid, max_wait_s=20)
        if not filled or filled.get("status") != "filled":
            return {"ok": False, "error": f"not filled (status={filled.get('status') if filled else 'timeout'})"}
        return {
            "ok": True,
            "id": oid,
            "filled_qty": float(filled.get("filled_qty", qty)),
            "filled_avg": float(filled.get("filled_avg_price") or 0),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def place_stop(symbol: str, qty: float, stop_price: float) -> dict:
    """Place stop sell. Crypto = stop_limit. Stocks = stop simple."""
    is_crypto = "/" in symbol
    # Clamp qty broker-réel
    try:
        pos = get_position(symbol)
        if pos:
            avail = float(pos.get("qty_available") or pos.get("qty") or 0)
            if avail > 0 and qty > avail:
                decimals = 6 if is_crypto else 5
                factor = 10 ** decimals
                qty = math.floor(avail * factor) / factor
    except Exception:
        pass

    if is_crypto:
        payload = {
            "symbol": symbol, "qty": str(qty), "side": "sell",
            "type": "stop_limit",
            "stop_price": str(round(stop_price, 2)),
            "limit_price": str(round(stop_price * 0.99, 2)),
            "time_in_force": "gtc",
        }
    else:
        # TIF=day forcé : Alpaca refuse GTC sur les fractional shares (422),
        # et toutes les positions shadow sont fractionnelles (sizing par notional).
        # Conséquence: les stops expirent chaque clôture NYSE et sont renouvelés
        # par _reconcile_stops_once() qui recalcule alors un chandelier frais.
        payload = {
            "symbol": symbol, "qty": str(qty), "side": "sell",
            "type": "stop",
            "stop_price": str(round(stop_price, 2)),
            "time_in_force": "day",
        }
    try:
        o = _request("POST", "/v2/orders", body=payload)
        return {"ok": True, "id": o.get("id"), "qty": qty}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def replace_stop(order_id: str, new_stop_price: float) -> dict:
    """PATCH existing stop order's price. Fewer API calls than cancel+replace.
    Falls back to cancel+create_new via caller logic when PATCH fails.

    Returns {"ok": True, "id": <new_or_same_id>} or {"ok": False, "error": ...}.
    """
    body = {"stop_price": str(round(new_stop_price, 2))}
    try:
        o = _request("PATCH", f"/v2/orders/{order_id}", body=body)
        return {"ok": True, "id": o.get("id", order_id)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def cancel_order(order_id: str) -> bool:
    if not order_id:
        return True
    try:
        _request("DELETE", f"/v2/orders/{order_id}")
        return True
    except Exception:
        return False

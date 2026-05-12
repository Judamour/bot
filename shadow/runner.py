"""Shadow bot runner — cycle 4h en parallèle de la prod.

Mode : compte Alpaca paper SÉPARÉ (ALPACA_SHADOW_*). Les ordres sont placés
RÉELLEMENT chez Alpaca paper (sur un compte distinct du prod), pas simulés.
Le state authoritative est chez Alpaca, on log juste les décisions localement
pour analyse.

Isolation :
- Lit UNIQUEMENT ALPACA_SHADOW_* (validate_isolation() au boot)
- N'importe PAS live/alpaca_executor.py (qui utilise les credentials prod)
"""
from __future__ import annotations
import sys
import os
import json
import time
import signal as sig_module
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Charge .env (mêmes vars que le bot prod, mais on n'utilise que les ALPACA_SHADOW_*)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import config
from data.market_snapshot import fetch_macro_context, fetch_ohlcv_cache
from shadow.scorer import compute_score
from shadow.strategies import ALL_DETECTORS
from shadow import broker

# Paths
LOG_DIR = "logs/shadow"
DECISIONS_LOG = f"{LOG_DIR}/decisions.jsonl"
EQUITY_LOG = f"{LOG_DIR}/equity.jsonl"
LOCAL_META = f"{LOG_DIR}/meta.json"  # méta locale (stops trailing, etc.)

# Cycle config — synchronisé sur les heartbeats prod (03/07/11/15/19/23 UTC)
CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]
MIN_SCORE = 55.0
TOP_N_SIGNALS = 5

# Sizing : risk parity
RISK_PER_TRADE_PCT = 0.01     # 1% du capital risqué
MAX_POSITION_PCT = 0.10       # max 10% capital par position
MAX_OPEN_POSITIONS = 10

# Trailing
ATR_MULT_TRAIL = 4.0

_stop = False


def _sig_handler(signum, frame):
    global _stop
    _stop = True
    print(f"[SHADOW] signal {signum} reçu — arrêt propre…", flush=True)


def log_event(kind: str, data: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_equity(equity: float, cash: float, n_positions: int):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "n_positions": n_positions,
        "perf_pct": round((equity - 100_000) / 100_000 * 100, 3),
    }
    with open(EQUITY_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_meta() -> dict:
    if os.path.exists(LOCAL_META):
        with open(LOCAL_META) as f:
            return json.load(f)
    return {"positions_meta": {}}  # {symbol: {strategy, score, stop, stop_order_id}}


def save_meta(meta: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOCAL_META, "w") as f:
        json.dump(meta, f, indent=2, default=str)


def _next_cycle_dt() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for h in CYCLE_HOURS_UTC:
        cand = now.replace(hour=h, minute=3, second=0, microsecond=0)  # +3 min après prod
        if cand > now:
            return cand
    tom = (now + timedelta(days=1)).replace(hour=CYCLE_HOURS_UTC[0], minute=3, second=0, microsecond=0)
    return tom


def _alpaca_symbol(sym: str) -> str:
    """Forme normalisée Alpaca pour les positions/orders."""
    return sym.replace("/", "")


def run_cycle():
    print(f"\n=== [SHADOW] Cycle {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC ===", flush=True)

    # 1. Account state via Alpaca shadow
    try:
        account = broker.get_account()
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        positions = broker.get_positions()
        pos_by_sym = {_alpaca_symbol(p["symbol"]): p for p in positions}
        print(f"[SHADOW] Equity: ${equity:.2f} | Cash: ${cash:.2f} | Positions: {len(positions)}", flush=True)
    except Exception as e:
        print(f"[SHADOW] account fetch échec: {e}", flush=True)
        return

    meta = load_meta()
    pos_meta = meta.setdefault("positions_meta", {})

    # 2. Macro context
    try:
        macro = fetch_macro_context()
    except Exception as e:
        print(f"[SHADOW] macro échec: {e}", flush=True)
        macro = {"vix": 18, "btc_trend": "bull", "qqq_regime_ok": True}
    ctx = {
        "vix": macro.get("vix", 18),
        "btc_trend": macro.get("btc_trend", "bull"),
        "qqq_ok": macro.get("qqq_regime_ok", True),
    }
    print(f"[SHADOW] VIX={ctx['vix']:.1f} BTC={ctx['btc_trend']} QQQ_ok={ctx['qqq_ok']}", flush=True)

    # 3. Fetch OHLCV
    symbols = list(getattr(config, "SYMBOLS", []) or
                   (list(getattr(config, "CRYPTO", [])) + list(getattr(config, "STOCKS", []))))
    try:
        cache_4h = fetch_ohlcv_cache(symbols, timeframe="4h", days=55)
        cache_1d = fetch_ohlcv_cache(symbols, timeframe="1d", days=220)
    except Exception as e:
        print(f"[SHADOW] OHLCV échec: {e}", flush=True)
        return
    print(f"[SHADOW] OHLCV: {len(cache_4h)} 4h | {len(cache_1d)} 1d", flush=True)

    # 4. Trailing stops update sur positions ouvertes
    from strategies.supertrend import compute_atr
    for sym, p in list(pos_by_sym.items()):
        df = cache_4h.get(sym) or cache_4h.get(sym[:-3] + "/" + sym[-3:])  # ex: SOLUSD ← SOL/USD
        # cherche aussi la forme avec slash
        df_with_slash = None
        for k, v in cache_4h.items():
            if k.replace("/", "") == sym:
                df_with_slash = v
                break
        df = df or df_with_slash
        if df is None or len(df) < 15:
            continue
        atr = float(compute_atr(df["high"], df["low"], df["close"], 14).iloc[-1] or 0)
        close = float(df["close"].iloc[-1])
        if atr <= 0:
            continue
        new_stop = round(close - ATR_MULT_TRAIL * atr, 2)
        m = pos_meta.get(sym, {})
        old_stop = m.get("stop", 0)
        if new_stop > old_stop:
            # Annuler ancien stop si présent + replacer
            old_id = m.get("stop_order_id")
            if old_id:
                broker.cancel_order(old_id)
            qty = float(p.get("qty_available") or p.get("qty") or 0)
            if qty > 0:
                # Cherche le symbole original (avec slash si crypto)
                orig_sym = p["symbol"]
                # Alpaca retourne BTCUSD sans slash — pour les ordres, accepte BTC/USD avec slash
                if any(c in orig_sym for c in ["BTC", "ETH", "SOL", "AVAX", "LINK"]) and "/" not in orig_sym:
                    orig_sym = orig_sym[:-3] + "/" + orig_sym[-3:]
                res = broker.place_stop(orig_sym, qty, new_stop)
                if res.get("ok"):
                    m["stop"] = new_stop
                    m["stop_order_id"] = res["id"]
                    pos_meta[sym] = m
                    log_event("trail", {"symbol": sym, "new_stop": new_stop, "qty": qty})
                    print(f"[SHADOW] TRAIL {sym} → {new_stop}", flush=True)

    # 5. Scan signaux pour symboles sans position
    candidates = []
    for sym in symbols:
        alp_sym = _alpaca_symbol(sym)
        if alp_sym in pos_by_sym:
            continue
        df_4h = cache_4h.get(sym)
        df_1d = cache_1d.get(sym)
        if df_4h is None:
            continue
        for detector in ALL_DETECTORS:
            try:
                sig = detector(sym, df_4h, df_1d)
                if sig is None:
                    continue
                sig.score = compute_score(sig, ctx)
                if sig.score >= MIN_SCORE:
                    candidates.append(sig)
            except Exception as e:
                print(f"[SHADOW] detector {detector.__name__} {sym}: {e}", flush=True)

    candidates.sort(key=lambda s: s.score, reverse=True)
    print(f"[SHADOW] {len(candidates)} signaux ≥ {MIN_SCORE}", flush=True)

    # 6. Ouvrir positions top N
    n_open = len(pos_by_sym)
    for sig in candidates:
        if n_open >= MAX_OPEN_POSITIONS:
            break
        if len([s for s in candidates[:TOP_N_SIGNALS] if s == sig]) == 0:
            continue
        # Sizing risk parity
        stop_dist = abs(sig.entry_price - sig.stop_price)
        if stop_dist <= 0:
            continue
        risk_eur = equity * RISK_PER_TRADE_PCT
        size = risk_eur / stop_dist
        max_size = (equity * MAX_POSITION_PCT) / sig.entry_price
        size = min(size, max_size)
        # Floor décimales
        is_crypto = "/" in sig.symbol
        import math
        decimals = 6 if is_crypto else 5
        factor = 10 ** decimals
        size = math.floor(size * factor) / factor
        if size <= 0:
            continue
        order_value = size * sig.entry_price
        if is_crypto and order_value < 10:
            continue  # min Alpaca crypto

        # Place market buy sur compte shadow
        res = broker.market_buy(sig.symbol, size)
        if not res.get("ok"):
            print(f"[SHADOW] BUY {sig.symbol} échec: {res.get('error')}", flush=True)
            continue

        fill_price = res.get("filled_avg") or sig.entry_price
        fill_qty = res.get("filled_qty") or size
        status = res.get("status", "filled")
        queued = (status != "filled")

        alp_sym = _alpaca_symbol(sig.symbol)
        pos_meta[alp_sym] = {
            "strategy": sig.strategy,
            "score": sig.score,
            "entry_price": fill_price if not queued else None,
            "stop": sig.stop_price,
            "stop_order_id": None,
            "buy_order_id": res.get("id"),
            "queued": queued,
            "entry_date": datetime.now(timezone.utc).isoformat(),
            "rationale": sig.rationale,
        }
        # Si ordre fillé immédiatement (crypto ou marché stock ouvert), placer le stop maintenant.
        # Sinon (queued après market close), le stop sera placé au prochain cycle quand on
        # verra la position effective dans /v2/positions.
        if not queued:
            stop_res = broker.place_stop(sig.symbol, fill_qty, sig.stop_price)
            pos_meta[alp_sym]["stop_order_id"] = stop_res.get("id") if stop_res.get("ok") else None

        log_event("entry", {
            "symbol": sig.symbol, "strategy": sig.strategy, "score": round(sig.score, 1),
            "size": fill_qty, "price": fill_price, "stop": sig.stop_price,
            "status": status,
            "risk_eur": round(risk_eur, 2), "rationale": sig.rationale,
        })
        suffix = f" [QUEUED — fill prochaine session]" if queued else ""
        print(f"[SHADOW] BUY {sig.symbol} ({sig.strategy}) score={sig.score:.1f} @ {fill_price:.4f} qty={fill_qty:.6f} stop={sig.stop_price:.4f}{suffix}", flush=True)
        n_open += 1

    # 7. Sauve meta + log equity
    save_meta(meta)
    try:
        account = broker.get_account()
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        log_equity(equity, cash, n_open)
        print(f"[SHADOW] Final: equity ${equity:.2f} ({(equity-100000)/1000:.2f}%) | cash ${cash:.2f} | positions {n_open}", flush=True)
    except Exception as e:
        print(f"[SHADOW] final account fetch échec: {e}", flush=True)


def main():
    sig_module.signal(sig_module.SIGINT, _sig_handler)
    sig_module.signal(sig_module.SIGTERM, _sig_handler)

    # ── ISOLATION CHECK ── fail-fast si credentials shadow corrompus
    try:
        broker.validate_isolation()
        print("[SHADOW] ✓ Isolation OK : credentials shadow distincts du prod", flush=True)
    except Exception as e:
        print(f"[SHADOW] ⛔ ISOLATION FAILED : {e}", flush=True)
        sys.exit(1)

    print(f"[SHADOW] Démarré — compte Alpaca paper isolé — cycle 4h", flush=True)
    try:
        run_cycle()
    except Exception as e:
        print(f"[SHADOW] cycle initial échec: {e}", flush=True)
        import traceback; traceback.print_exc()

    while not _stop:
        next_dt = _next_cycle_dt()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_s = max(1, int((next_dt - now).total_seconds()))
        print(f"[SHADOW] Prochain cycle: {next_dt.strftime('%H:%M')} UTC (dans {wait_s//60} min)", flush=True)
        slept = 0
        while slept < wait_s and not _stop:
            chunk = min(60, wait_s - slept)
            time.sleep(chunk)
            slept += chunk
        if _stop:
            break
        try:
            run_cycle()
        except Exception as e:
            print(f"[SHADOW] cycle échec: {e}", flush=True)
            import traceback; traceback.print_exc()

    print("[SHADOW] Arrêté.", flush=True)


if __name__ == "__main__":
    main()

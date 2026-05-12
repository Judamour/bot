"""Shadow bot runner — cycle 4h en parallèle de la prod.

- Lit les mêmes OHLCV (data.fetcher)
- Scanne tous les symboles avec tous les détecteurs
- Score chaque signal, garde les top N
- Ouvre/ferme positions virtuelles, met à jour trailing
- Logue tout dans logs/shadow/

Pas de connexion broker. Pas d'ordres réels. 100% simulation.
"""
from __future__ import annotations
import sys
import os
import json
import time
import signal as sig_module
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.market_snapshot import fetch_macro_context, fetch_ohlcv_cache
from shadow.scorer import compute_score
from shadow.strategies import ALL_DETECTORS
from shadow import simulator as sim

# Paths
LOG_DIR = "logs/shadow"
STATE_PATH = f"{LOG_DIR}/state.json"
DECISIONS_LOG = f"{LOG_DIR}/decisions.jsonl"
EQUITY_LOG = f"{LOG_DIR}/equity.jsonl"

# Cycle config — synchronisé sur les heartbeats prod (03/07/11/15/19/23 UTC)
CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]
MIN_SCORE = 40.0  # seuil minimum pour considérer un signal
TOP_N_SIGNALS = 5  # ouvre max N signaux par cycle (pour pas tout flooder)

_stop = False


def _sig_handler(signum, frame):
    global _stop
    _stop = True
    print(f"[SHADOW] signal {signum} reçu — arrêt propre…", flush=True)


def log_event(kind: str, data: dict, path: str = DECISIONS_LOG):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_equity(state: dict, eq: float):
    os.makedirs(LOG_DIR, exist_ok=True)
    initial = state.get("initial_capital", 100000)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": round(eq, 2),
        "capital_libre": round(state["capital"], 2),
        "n_positions": len(state["positions"]),
        "total_trades": len(state.get("trades", [])),
        "perf_pct": round((eq - initial) / initial * 100, 3),
    }
    with open(EQUITY_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _next_cycle_dt() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for h in CYCLE_HOURS_UTC:
        cand = now.replace(hour=h, minute=2, second=0, microsecond=0)  # +2min après prod
        if cand > now:
            return cand
    tom = (now + timedelta(days=1)).replace(hour=CYCLE_HOURS_UTC[0], minute=2, second=0, microsecond=0)
    return tom


def run_cycle():
    """Un cycle complet : fetch data, scan signaux, exécute fills virtuels."""
    print(f"\n=== [SHADOW] Cycle {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC ===", flush=True)
    state = sim.load_state(STATE_PATH)

    # 1. Fetch macro context
    try:
        macro = fetch_macro_context()
    except Exception as e:
        print(f"[SHADOW] macro fetch échec: {e}", flush=True)
        macro = {"vix": 18, "btc_trend": "bull", "qqq_regime_ok": True}
    ctx = {
        "vix": macro.get("vix", 18),
        "btc_trend": macro.get("btc_trend", "bull"),
        "qqq_ok": macro.get("qqq_regime_ok", True),
    }
    print(f"[SHADOW] VIX={ctx['vix']:.1f} BTC={ctx['btc_trend']} QQQ_ok={ctx['qqq_ok']}", flush=True)

    # 2. Fetch OHLCV pour tous les symboles
    symbols = list(getattr(config, "SYMBOLS", []) or
                   (list(getattr(config, "CRYPTO", [])) + list(getattr(config, "STOCKS", []))))
    cache_4h, cache_1d = {}, {}
    try:
        cache_4h = fetch_ohlcv_cache(symbols, timeframe="4h", days=55)
        cache_1d = fetch_ohlcv_cache(symbols, timeframe="1d", days=220)
    except Exception as e:
        print(f"[SHADOW] OHLCV fetch échec: {e}", flush=True)
        return
    print(f"[SHADOW] OHLCV : {len(cache_4h)} 4h | {len(cache_1d)} 1d", flush=True)

    # 3. Update stops trailing + check stops touchés (sur positions existantes)
    closes = {}
    for sym, df in cache_4h.items():
        if df is None or len(df) == 0:
            continue
        closes[sym] = float(df["close"].iloc[-1])

    for sym in list(state["positions"].keys()):
        df = cache_4h.get(sym)
        if df is None or len(df) == 0:
            continue
        last = df.iloc[-1]
        low = float(last["low"])
        close = float(last["close"])
        # Check stop touché
        if sim.check_stop(state, sym, low):
            trade = sim.close_position(state, sym, state["positions"][sym]["stop"], reason="stop_loss")
            if trade:
                log_event("exit", trade)
                print(f"[SHADOW] STOP {sym} @ {trade['price']:.4f} | PnL {trade['pnl']:+.2f}$ ({trade['pnl_pct']:+.2f}%)", flush=True)
            continue
        # Trailing update
        # ATR : recalcule à partir des dernières bougies
        from strategies.supertrend import compute_atr
        atr = float(compute_atr(df["high"], df["low"], df["close"], 14).iloc[-1] or 0)
        if atr > 0 and sim.update_trailing(state, sym, close, atr):
            new_stop = state["positions"][sym]["stop"]
            log_event("trail", {"symbol": sym, "new_stop": round(new_stop, 4)})

    # 4. Scan signaux pour tous les symboles non encore en position
    candidates = []
    for sym in symbols:
        if sym in state["positions"]:
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
                print(f"[SHADOW] detector {detector.__name__} {sym} échec: {e}", flush=True)

    # 5. Sort par score, prend top N, ouvre positions
    candidates.sort(key=lambda s: s.score, reverse=True)
    print(f"[SHADOW] {len(candidates)} signaux candidats au-dessus du seuil {MIN_SCORE}", flush=True)
    for sig in candidates[:TOP_N_SIGNALS]:
        if len(state["positions"]) >= sim.MAX_OPEN_POSITIONS:
            break
        current_price = closes.get(sig.symbol, sig.entry_price)
        trade = sim.open_position(state, sig, current_price)
        if trade:
            log_event("entry", trade)
            print(f"[SHADOW] BUY {sig.symbol} ({sig.strategy}) score={sig.score:.1f} @ {trade['price']:.4f} stop={trade['stop']:.4f}", flush=True)

    # 6. Save state + log equity
    eq = sim.equity(state, closes)
    sim.save_state(state, STATE_PATH)
    log_equity(state, eq)
    initial = state.get("initial_capital", 100000)
    perf = (eq - initial) / initial * 100
    print(f"[SHADOW] Equity: {eq:.2f}$ ({perf:+.2f}%) | Cash: {state['capital']:.2f}$ | Positions: {len(state['positions'])} | Trades: {len(state.get('trades', []))}", flush=True)


def main():
    sig_module.signal(sig_module.SIGINT, _sig_handler)
    sig_module.signal(sig_module.SIGTERM, _sig_handler)

    print(f"[SHADOW] Démarré — capital virtuel {sim.INITIAL_CAPITAL}$ — cycle 4h", flush=True)
    # Premier cycle immédiat au boot pour valider
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
        # Sleep par tranches pour pouvoir s'arrêter rapidement
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

    print("[SHADOW] Arrêté proprement.", flush=True)


if __name__ == "__main__":
    main()

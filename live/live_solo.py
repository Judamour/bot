"""
live_solo.py — Runner live isolé, 1 stratégie, cryptos uniquement, kill switch -10%.

Conçu pour démarrer en live avec un capital trop faible pour le multi-bot
(ex: 91€). Reste 100% indépendant de multi_runner.py et bot_z.py — n'écrit
JAMAIS dans logs/bot_z/ ou logs/supertrend/, etc.

Architecture :
  - 1 stratégie : Bot G (Trend Following Multi-Asset, le plus stable du backtest)
  - 3 symboles  : BTC/EUR, ETH/EUR, SOL/EUR (fractionables, min order ~3-5€)
  - State       : logs/live_solo/state.json
  - Trades log  : logs/live_solo/trades.jsonl
  - Kill switch : -10% du capital initial → ferme toutes positions, bot gelé

Usage :
  python live/live_solo.py        # 1 cycle puis stop
  python live/live_solo.py --loop # boucle toutes les 4h

Le multi-bot continue de tourner en parallèle en mode PAPER (logs séparés).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Override config AVANT d'importer la stratégie : symboles crypto uniquement
import config
SOLO_SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]
_ORIGINAL_SYMBOLS = list(config.SYMBOLS)
config.SYMBOLS = SOLO_SYMBOLS
# Force live trading pour ce runner uniquement (override .env si besoin)
config.PAPER_TRADING = False

from strategies.trend_following_strategy import run_trend_cycle
from data.fetcher import fetch_ohlcv
from live.notifier import notify
from live.order_executor import execute_sell, check_balance

STATE_FILE = "logs/live_solo/state.json"
TRADES_LOG = "logs/live_solo/trades.jsonl"
KILL_SWITCH_PCT = -0.10           # -10% du capital initial
INITIAL_CAPITAL_FALLBACK = 91.0   # Si state absent
CYCLE_INTERVAL_SEC = 4 * 3600     # 4h


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [SOLO][{level}] {msg}"
    print(line)
    os.makedirs("logs/live_solo", exist_ok=True)
    with open("logs/live_solo/runner.log", "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log(f"State corrompu ({e}) — reset", "WARN")
    return {
        "capital": INITIAL_CAPITAL_FALLBACK,
        "initial_capital": INITIAL_CAPITAL_FALLBACK,
        "original_capital": INITIAL_CAPITAL_FALLBACK,
        "positions": {},
        "trades": [],
        "dd_frozen": False,
        "started_at": datetime.now().isoformat(),
        "cycles": 0,
    }


def save_state(state: dict):
    os.makedirs("logs/live_solo", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def append_trade(trade: dict):
    os.makedirs("logs/live_solo", exist_ok=True)
    with open(TRADES_LOG, "a") as f:
        f.write(json.dumps(trade, default=str) + "\n")


def portfolio_value(state: dict, daily_cache: dict) -> float:
    total = float(state.get("capital", 0))
    for sym, pos in state.get("positions", {}).items():
        df = daily_cache.get(sym)
        price = float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else float(pos.get("entry", 0))
        total += price * float(pos.get("size", 0))
    return total


def fetch_macro() -> dict:
    """Macro context minimal pour Bot G — il utilise seulement vix et exposure_blocked."""
    # Mode solo crypto-only : VIX neutre 18.0 (pas de blocage), pas d'exposure_blocked.
    # Bot G utilisera ses propres filtres (SMA200, breakout, ADX, ATR) sur les données crypto.
    return {"vix": 18.0, "exposure_blocked": False}


def trigger_kill_switch(state: dict, daily_cache: dict):
    """Ferme toutes positions au market et gèle le bot."""
    log("⛔ KILL SWITCH ACTIVÉ — fermeture forcée de toutes positions", "WARN")
    for sym, pos in list(state.get("positions", {}).items()):
        df = daily_cache.get(sym)
        if df is None or len(df) == 0:
            log(f"  {sym} — pas de prix, skip vente", "WARN")
            continue
        current_price = float(df["close"].iloc[-1])
        size = float(pos.get("size", 0))
        if size <= 0:
            continue
        order = execute_sell(sym, size, current_price, reason="KILL_SWITCH_-10pct")
        if order.success:
            proceeds = order.filled_price * order.filled_size * (1 - config.EXCHANGE_FEE)
            state["capital"] = state.get("capital", 0) + proceeds
            state["positions"].pop(sym, None)
            append_trade({
                "ts": datetime.now().isoformat(),
                "symbol": sym, "side": "sell", "reason": "KILL_SWITCH",
                "size": order.filled_size, "price": order.filled_price,
                "proceeds": proceeds,
            })
            log(f"  {sym} — vendu {order.filled_size:.6f} @ {order.filled_price:.2f}€ → +{proceeds:.2f}€", "WARN")
        else:
            log(f"  {sym} — VENTE ÉCHOUÉE: {order.error} — INTERVENTION MANUELLE", "WARN")
    state["dd_frozen"] = True


def run_one_cycle() -> bool:
    """Retourne True si tout OK, False si kill switch ou erreur bloquante."""
    state = load_state()
    state["cycles"] = state.get("cycles", 0) + 1
    init = state.get("original_capital", INITIAL_CAPITAL_FALLBACK)

    log(f"=== CYCLE #{state['cycles']} | init {init:.2f}€ | capital {state['capital']:.2f}€ ===")

    # 1. Vérification connexion Kraken
    eur_free = check_balance()
    if eur_free < 0:
        log("Connexion Kraken impossible — abandon cycle", "WARN")
        return False
    log(f"Kraken EUR libre : {eur_free:.2f}€")

    # 2. Fetch OHLCV daily pour les 3 cryptos
    daily_cache = {}
    for sym in SOLO_SYMBOLS:
        try:
            df = fetch_ohlcv(sym, timeframe="1d", days=400)
            if df is not None and len(df) >= 230:  # SMA200 + marge
                daily_cache[sym] = df
                log(f"{sym} : {len(df)} bougies daily")
            else:
                log(f"{sym} : données insuffisantes ({len(df) if df is not None else 0})", "WARN")
        except Exception as e:
            log(f"{sym} : fetch erreur — {e}", "WARN")

    if not daily_cache:
        log("Aucune donnée — abandon cycle", "WARN")
        return False

    # 3. Kill switch CHECK avant tout trade
    total_before = portfolio_value(state, daily_cache)
    perf_before = (total_before - init) / init if init > 0 else 0
    log(f"Valeur portfolio : {total_before:.2f}€ ({perf_before*100:+.2f}% vs init)")

    if perf_before <= KILL_SWITCH_PCT and not state.get("dd_frozen"):
        trigger_kill_switch(state, daily_cache)
        save_state(state)
        notify(
            f"🚨 <b>KILL SWITCH</b>\n"
            f"Perte : <b>{perf_before*100:+.2f}%</b> (seuil {KILL_SWITCH_PCT*100:+.0f}%)\n"
            f"Capital final : {portfolio_value(state, daily_cache):.2f}€\n"
            f"⛔ Bot gelé — intervention manuelle requise"
        )
        return False

    if state.get("dd_frozen"):
        log("Bot gelé (kill switch précédent) — pas de nouveau cycle", "WARN")
        return False

    # 4. Run Bot G strategy
    macro = fetch_macro()
    try:
        state = run_trend_cycle(state, daily_cache, macro)
    except Exception as e:
        log(f"run_trend_cycle ERREUR : {e}", "WARN")
        import traceback
        log(traceback.format_exc(), "WARN")
        return False

    # 5. Save + notify
    total_after = portfolio_value(state, daily_cache)
    perf_after = (total_after - init) / init * 100 if init > 0 else 0
    save_state(state)

    n_pos = len(state.get("positions", {}))
    n_trades = len(state.get("trades", []))
    pos_list = list(state.get("positions", {}).keys()) or ["aucune"]

    notify(
        f"💰 <b>LIVE SOLO #{state['cycles']}</b>\n"
        f"Valeur : <b>{total_after:.2f}€</b> ({perf_after:+.2f}%)\n"
        f"Capital libre : {state['capital']:.2f}€\n"
        f"Positions : {', '.join(pos_list)}\n"
        f"Trades total : {n_trades}\n"
        f"Kill switch : {KILL_SWITCH_PCT*100:+.0f}% (déclenché à {init * (1+KILL_SWITCH_PCT):.2f}€)"
    )
    log(f"=== Cycle OK | valeur {total_after:.2f}€ ({perf_after:+.2f}%) ===")
    return True


def main():
    p = argparse.ArgumentParser(description="Live solo runner crypto-only")
    p.add_argument("--loop", action="store_true", help="Boucle infinie (4h interval)")
    args = p.parse_args()

    if args.loop:
        log(f"Mode loop activé — cycles toutes les {CYCLE_INTERVAL_SEC // 3600}h")
        while True:
            try:
                run_one_cycle()
            except Exception as e:
                log(f"Cycle ERREUR : {e}", "WARN")
                import traceback
                log(traceback.format_exc(), "WARN")
            log(f"Sleep {CYCLE_INTERVAL_SEC}s...")
            time.sleep(CYCLE_INTERVAL_SEC)
    else:
        ok = run_one_cycle()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

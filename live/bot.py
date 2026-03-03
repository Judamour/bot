import ccxt
import time
import json
import os
import sys
from datetime import datetime
from colorama import Fore, Style, init

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import fetch_ohlcv, get_exchange
from strategies.supertrend import generate_signals, calculate_position_size
from live.claude_filter import ask_claude

init(autoreset=True)

STATE_FILE = "logs/paper_state.json"


# ── Paper Trading State ──────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": config.PAPER_CAPITAL,
        "positions": {},
        "trades": [],
        "initial_capital": config.PAPER_CAPITAL,
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    colors = {"INFO": Fore.CYAN, "BUY": Fore.GREEN, "SELL": Fore.RED, "WARN": Fore.YELLOW}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{colors.get(level, '')}{ts} [{level}] {msg}{Style.RESET_ALL}")
    with open("logs/bot.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


# ── Trailing Stop (ratchet) ───────────────────────────────────────────────────

def apply_trailing_stop(position: dict, current_price: float, symbol: str) -> dict:
    """
    Ratchet stop : verrouille les profits par paliers de R.
      +1R atteint → stop monté au breakeven (entrée)
      +2R atteint → stop monté à +1R
      +3R atteint → stop monté à +2R

    Le stop ne descend jamais.
    """
    entry = position["entry"]
    initial_stop = position.get("initial_stop", position["stop"])
    stop_distance = entry - initial_stop

    if stop_distance <= 0 or current_price <= entry:
        return position

    r = (current_price - entry) / stop_distance

    if r >= 3:
        new_stop = entry + 2 * stop_distance      # Lock +2R
    elif r >= 2:
        new_stop = entry + 1 * stop_distance      # Lock +1R
    elif r >= 1:
        new_stop = entry                           # Breakeven
    else:
        return position

    new_stop = round(new_stop, 4)
    if new_stop > position["stop"]:
        position["stop"] = new_stop
        label = "breakeven" if new_stop == entry else f"+{r:.1f}R verrouillé"
        log(f"{symbol} — Trailing stop → {new_stop:.4f}€ ({label})", "INFO")

    return position


# ── Logique principale ────────────────────────────────────────────────────────

def process_symbol(symbol: str, state: dict) -> dict:
    """Analyse un symbole et exécute les ordres si nécessaire."""
    try:
        df = fetch_ohlcv(symbol, config.TIMEFRAME, days=45)
        df = generate_signals(df)
        last = df.iloc[-1]
        current_price = float(last["close"])
        signal = int(last["signal"])
        atr = float(last["atr"])
        adx = float(last["adx"])
        volume_ratio = float(last["volume_ratio"])

    except Exception as e:
        log(f"{symbol} — Erreur récupération données: {e}", "WARN")
        return state

    position = state["positions"].get(symbol)

    # ── Trailing stop avant vérification de sortie ──
    if position:
        position = apply_trailing_stop(position, current_price, symbol)
        state["positions"][symbol] = position

    # ── Vérifier stop-loss / take-profit ──
    if position:
        reason = None
        exit_price = current_price

        if current_price <= position["stop"]:
            reason = "stop_loss"
            exit_price = position["stop"]
        elif current_price >= position["tp"]:
            reason = "take_profit"
            exit_price = position["tp"]
        elif signal == -1:
            reason = "signal_exit"

        if reason:
            pnl = (exit_price - position["entry"]) * position["size"]
            state["capital"] += exit_price * position["size"]
            state["positions"].pop(symbol)

            trade = {
                "symbol": symbol,
                "entry_date": position["date"],
                "exit_date": str(datetime.now()),
                "entry_price": position["entry"],
                "exit_price": exit_price,
                "pnl": round(pnl, 2),
                "reason": reason,
            }
            state["trades"].append(trade)

            pnl_r = round(pnl / position.get("risk_eur", 1), 1)
            log(
                f"{'✓' if pnl > 0 else '✗'} {symbol} CLOSE [{reason}] | "
                f"{position['entry']:.4f}€ → {exit_price:.4f}€ | "
                f"PnL: {pnl:+.2f}€ ({pnl_r:+.1f}R) | Capital: {state['capital']:.2f}€",
                "BUY" if pnl > 0 else "SELL",
            )

    # ── Ouvrir position sur signal achat ──
    if signal == 1 and symbol not in state["positions"]:
        if len(state["positions"]) >= config.MAX_OPEN_TRADES:
            log(f"{symbol} — Signal ignoré (max {config.MAX_OPEN_TRADES} positions ouvertes)", "WARN")
            return state

        log(
            f"{symbol} — Signal BUY | ADX: {adx:.1f} | Vol×{volume_ratio:.2f} | "
            f"RSI: {last['rsi']:.1f} — consultation Claude...",
            "INFO",
        )
        confirme, raison = ask_claude(
            symbol=symbol,
            price=current_price,
            rsi=float(last["rsi"]),
            ema50=float(last["ema50"]),
            ema200=float(last["ema200"]),
            atr=atr,
            adx=adx,
            volume_ratio=volume_ratio,
            capital=state["capital"],
        )
        log(f"{symbol} — Claude: {'✓ CONFIRME' if confirme else '✗ IGNORE'} | {raison}", "INFO")

        if not confirme:
            return state

        pos = calculate_position_size(state["capital"], current_price, atr)
        cost = pos["size"] * current_price

        if cost > state["capital"]:
            log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€)", "WARN")
            return state

        state["capital"] -= cost
        state["positions"][symbol] = {
            "entry": current_price,
            "size": pos["size"],
            "stop": pos["stop_loss"],
            "initial_stop": pos["stop_loss"],   # référence pour le trailing stop
            "tp": pos["take_profit"],
            "date": str(datetime.now()),
            "risk_eur": pos["risk_eur"],
        }

        log(
            f"▲ {symbol} BUY | Prix: {current_price:.4f}€ | "
            f"Taille: {pos['size']} | SL: {pos['stop_loss']:.4f}€ | "
            f"TP: {pos['take_profit']:.4f}€ | Risque: {pos['risk_eur']:.2f}€",
            "BUY",
        )

    return state


def print_status(state: dict):
    trades = state["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    total_value = state["capital"] + sum(
        p["entry"] * p["size"] for p in state["positions"].values()
    )
    perf = (total_value - state["initial_capital"]) / state["initial_capital"] * 100

    if trades:
        log(
            f"PORTFOLIO | Libre: {state['capital']:.2f}€ | Total: {total_value:.2f}€ | "
            f"Perf: {perf:+.2f}% | Trades: {len(trades)} | "
            f"Win rate: {len(wins)/len(trades)*100:.1f}%"
        )
    else:
        log(f"PORTFOLIO | Capital: {state['capital']:.2f}€ | Aucun trade encore")

    if state["positions"]:
        log(f"Positions ouvertes: {list(state['positions'].keys())}")


def run():
    """Boucle principale du bot."""
    mode = "PAPER TRADING" if config.PAPER_TRADING else "LIVE TRADING"
    os.makedirs("logs", exist_ok=True)

    log(f"{'='*50}")
    log(f"  BOT DÉMARRÉ — Mode: {mode}")
    log(f"  Symboles: {config.SYMBOLS}")
    log(f"  Timeframe: {config.TIMEFRAME}")
    log(f"  Filtres: ADX>{config.ADX_THRESHOLD} | Volume>110% MA | EMA9>EMA21 | RSI<{config.RSI_OVERBOUGHT}")
    log(f"  Trailing stop: breakeven@+1R, lock+1R@+2R, lock+2R@+3R")
    log(f"{'='*50}")

    if not config.PAPER_TRADING:
        log("⚠ MODE LIVE ACTIVÉ — Vrai argent engagé !", "WARN")
        confirm = input("Confirmer avec 'OUI' : ")
        if confirm != "OUI":
            log("Annulé.")
            return

    state = load_state()
    log(f"Capital de départ: {state['capital']:.2f}€")

    intervals = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    sleep_time = intervals.get(config.TIMEFRAME, 3600)

    while True:
        try:
            log(f"--- Analyse en cours ({datetime.now().strftime('%H:%M:%S')}) ---")

            for symbol in config.SYMBOLS:
                state = process_symbol(symbol, state)

            save_state(state)
            print_status(state)

            log(f"Prochaine analyse dans {sleep_time // 60} minutes...")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            log("Bot arrêté manuellement.")
            save_state(state)
            break
        except Exception as e:
            log(f"Erreur inattendue: {e}", "WARN")
            time.sleep(60)


if __name__ == "__main__":
    run()

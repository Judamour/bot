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
        "positions": {},   # {symbol: {entry, size, stop, tp, date}}
        "trades": [],
        "initial_capital": config.PAPER_CAPITAL,
    }


def save_state(state: dict):
    os.makedirs("logs", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Logging ─────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    colors = {"INFO": Fore.CYAN, "BUY": Fore.GREEN, "SELL": Fore.RED, "WARN": Fore.YELLOW}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{colors.get(level, '')}{ts} [{level}] {msg}{Style.RESET_ALL}")

    os.makedirs("logs", exist_ok=True)
    with open("logs/bot.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


# ── Logique principale ───────────────────────────────────────────────────────

def process_symbol(symbol: str, state: dict) -> dict:
    """Analyse un symbole et exécute les ordres si nécessaire."""
    try:
        df = fetch_ohlcv(symbol, config.TIMEFRAME, days=60)
        df = generate_signals(df)
        last = df.iloc[-1]
        current_price = last["close"]
        signal = last["signal"]
        atr = last["atr"]

    except Exception as e:
        log(f"{symbol} — Erreur récupération données: {e}", "WARN")
        return state

    position = state["positions"].get(symbol)

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
            state["capital"] += (exit_price * position["size"])
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

            result = "WIN" if pnl > 0 else "LOSS"
            emoji = "✓" if pnl > 0 else "✗"
            log(
                f"{emoji} {symbol} CLOSE [{reason}] | "
                f"Entrée: {position['entry']:.2f}€ → Sortie: {exit_price:.2f}€ | "
                f"PnL: {pnl:+.2f}€ | Capital: {state['capital']:.2f}€",
                "BUY" if pnl > 0 else "SELL",
            )

    # ── Ouvrir position sur signal achat ──
    if signal == 1 and symbol not in state["positions"]:
        if len(state["positions"]) >= config.MAX_OPEN_TRADES:
            log(f"{symbol} — Signal achat ignoré (max {config.MAX_OPEN_TRADES} positions)", "WARN")
            return state

        # ── Validation par Claude ──
        log(f"{symbol} — Signal détecté, consultation Claude AI...", "INFO")
        confirme, raison = ask_claude(
            symbol=symbol,
            price=current_price,
            rsi=float(last["rsi"]),
            ema50=float(last["ema50"]),
            ema200=float(last["ema200"]),
            atr=atr,
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
            "tp": pos["take_profit"],
            "date": str(datetime.now()),
            "risk_eur": pos["risk_eur"],
        }

        log(
            f"▲ {symbol} BUY | Prix: {current_price:.2f}€ | "
            f"Taille: {pos['size']} | SL: {pos['stop_loss']:.2f}€ | "
            f"TP: {pos['take_profit']:.2f}€ | Risque: {pos['risk_eur']:.2f}€",
            "BUY",
        )

    return state


def print_status(state: dict):
    """Affiche le statut du portefeuille."""
    total_value = state["capital"]
    for sym, pos in state["positions"].items():
        # Valeur approximative (on utiliserait le prix live en production)
        total_value += pos["entry"] * pos["size"]

    perf = (total_value - state["initial_capital"]) / state["initial_capital"] * 100
    trades = state["trades"]
    wins = [t for t in trades if t["pnl"] > 0]

    log(
        f"PORTFOLIO | Capital libre: {state['capital']:.2f}€ | "
        f"Valeur totale: {total_value:.2f}€ | "
        f"Perf: {perf:+.2f}% | "
        f"Trades: {len(trades)} | "
        f"Win rate: {len(wins)/len(trades)*100:.1f}%" if trades else
        f"PORTFOLIO | Capital: {state['capital']:.2f}€ | Aucun trade encore"
    )

    if state["positions"]:
        log(f"Positions ouvertes: {list(state['positions'].keys())}")


def run():
    """Boucle principale du bot."""
    mode = "PAPER TRADING" if config.PAPER_TRADING else "LIVE TRADING"
    log(f"{'='*50}")
    log(f"  BOT DÉMARRÉ — Mode: {mode}")
    log(f"  Symboles: {config.SYMBOLS}")
    log(f"  Timeframe: {config.TIMEFRAME}")
    log(f"{'='*50}")

    if not config.PAPER_TRADING:
        log("⚠ MODE LIVE ACTIVÉ — Vrai argent engagé !", "WARN")
        confirm = input("Confirmer avec 'OUI' : ")
        if confirm != "OUI":
            log("Annulé.")
            return

    state = load_state()
    log(f"Capital de départ: {state['capital']:.2f}€")

    # Intervalle entre chaque analyse (selon timeframe)
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

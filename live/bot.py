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
from strategies.supertrend import generate_signals, calculate_position_size, add_indicators
from live.claude_filter import ask_claude
from live.notifier import notify

init(autoreset=True)

STATE_FILE   = "logs/paper_state.json"
SIGNALS_FILE = "logs/signals.jsonl"


# ── Signal logger ─────────────────────────────────────────────────────────────

def log_signal(event: str, symbol: str, data: dict):
    """
    Enregistre chaque évaluation de signal dans signals.jsonl.
    Format JSON Lines : une ligne JSON par événement, facilement analysable.

    Events: SCAN, BUY_EXECUTED, BUY_SKIP_CLAUDE, BUY_SKIP_MAX_POS,
            BUY_SKIP_CAPITAL, EXIT_SL, EXIT_TP, EXIT_SIGNAL, TRAILING_STOP
    """
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "symbol": symbol,
        **data,
    }
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ── Contexte BTC global ──────────────────────────────────────────────────────

def fetch_btc_context() -> dict:
    """Récupère le contexte macro BTC (prix + EMA200) une fois par cycle."""
    try:
        df = fetch_ohlcv("BTC/EUR", config.TIMEFRAME, days=45)
        df = add_indicators(df)
        last = df.iloc[-1]
        btc_price = float(last["close"])
        btc_ema200 = float(last["ema200"])
        above = btc_price > btc_ema200
        return {
            "btc_price": round(btc_price, 2),
            "btc_above_ema200": above,
            "btc_trend": "bull" if above else "bear",
        }
    except Exception as e:
        log(f"Contexte BTC indisponible: {e}", "WARN")
        return {}


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
        if new_stop == entry:
            notify(f"🔒 <b>{symbol}</b> stop breakeven → {new_stop:.2f}€")

    return position


# ── Logique principale ────────────────────────────────────────────────────────

def process_symbol(symbol: str, state: dict, btc_context: dict = None) -> dict:
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

    # ── Enregistrement scan complet avec breakdown des filtres ──
    scan_data = {
        "price": current_price,
        "signal": signal,
        "adx": round(adx, 2),
        "rsi": round(float(last["rsi"]), 2),
        "volume_ratio": round(volume_ratio, 3),
        "ema9": round(float(last["ema9"]), 4),
        "ema21": round(float(last["ema21"]), 4),
        "ema50": round(float(last["ema50"]), 4),
        "ema200": round(float(last["ema200"]), 4),
        "supertrend": round(float(last["supertrend"]), 4),
        "atr": round(atr, 4),
        "in_position": symbol in state["positions"],
        # Breakdown booléen de chaque filtre
        "f_supertrend_up": bool(last["f_supertrend_up"]),
        "f_trending":      bool(last["f_trending"]),
        "f_above_ema200":  bool(last["f_above_ema200"]),
        "f_structure":     bool(last["f_structure"]),
        "f_momentum":      bool(last["f_momentum"]),
        "f_rsi":           bool(last["f_rsi"]),
        "f_volume":        bool(last["f_volume"]),
    }
    if btc_context:
        scan_data.update(btc_context)
    log_signal("SCAN", symbol, scan_data)

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
            exit_price_eff = exit_price * (1 - config.SLIPPAGE)
            fee_exit = exit_price_eff * position["size"] * config.EXCHANGE_FEE
            proceeds = exit_price_eff * position["size"] - fee_exit
            state["capital"] += proceeds
            state["positions"].pop(symbol)

            pnl = proceeds - (position["entry"] * position["size"] + position.get("fee_entry", 0))
            trade = {
                "symbol": symbol,
                "entry_date": position["date"],
                "exit_date": str(datetime.now()),
                "entry_price": position["entry"],
                "exit_price": exit_price_eff,
                "pnl": round(pnl, 2),
                "reason": reason,
            }
            state["trades"].append(trade)
            log_signal(f"EXIT_{reason.upper()}", symbol, {
                "entry_price": position["entry"],
                "exit_price": exit_price_eff,
                "fee_exit": round(fee_exit, 4),
                "pnl": round(pnl, 2),
                "pnl_r": round(pnl / position.get("risk_eur", 1), 2),
                "duration_h": None,  # calculé à l'analyse
                "reason": reason,
            })

            pnl_r = round(pnl / position.get("risk_eur", 1), 1)
            log(
                f"{'✓' if pnl > 0 else '✗'} {symbol} CLOSE [{reason}] | "
                f"{position['entry']:.4f}€ → {exit_price_eff:.4f}€ | "
                f"PnL: {pnl:+.2f}€ ({pnl_r:+.1f}R) | Capital: {state['capital']:.2f}€",
                "BUY" if pnl > 0 else "SELL",
            )
            # ── Notification Telegram ──
            if reason == "take_profit":
                notify(f"✅ <b>{symbol}</b> TP +{pnl:.2f}€ ({pnl_r:+.1f}R)")
            elif reason == "stop_loss":
                notify(f"🔴 <b>{symbol}</b> SL {pnl:.2f}€ ({pnl_r:+.1f}R)")
            else:
                notify(f"⏹ <b>{symbol}</b> EXIT [{reason}] {pnl:+.2f}€ ({pnl_r:+.1f}R)")

    # ── Ouvrir position sur signal achat ──
    if signal == 1 and symbol in config.XSTOCKS and not _is_us_market_open():
        log(f"{symbol} — Marché US fermé, entrée ignorée", "INFO")
        return state

    if signal == 1 and symbol not in state["positions"]:
        if len(state["positions"]) >= config.MAX_OPEN_TRADES:
            log(f"{symbol} — Signal ignoré (max {config.MAX_OPEN_TRADES} positions ouvertes)", "WARN")
            log_signal("BUY_SKIP_MAX_POS", symbol, {"price": current_price, "open_positions": len(state["positions"])})
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
        log_signal("CLAUDE_FILTER", symbol, {
            "decision": "CONFIRME" if confirme else "IGNORE",
            "raison": raison,
            "adx": round(adx, 2),
            "rsi": round(float(last["rsi"]), 2),
            "volume_ratio": round(volume_ratio, 3),
            "price": current_price,
        })

        if not confirme:
            log_signal("BUY_SKIP_CLAUDE", symbol, {"raison": raison, "price": current_price})
            return state

        effective_buy = current_price * (1 + config.SLIPPAGE)
        pos = calculate_position_size(state["capital"], effective_buy, atr)
        fee_entry = effective_buy * pos["size"] * config.EXCHANGE_FEE
        total_cost = pos["size"] * effective_buy + fee_entry

        if total_cost > state["capital"]:
            log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€)", "WARN")
            return state

        state["capital"] -= total_cost
        state["positions"][symbol] = {
            "entry": effective_buy,
            "size": pos["size"],
            "stop": pos["stop_loss"],
            "initial_stop": pos["stop_loss"],
            "tp": pos["take_profit"],
            "date": str(datetime.now()),
            "risk_eur": pos["risk_eur"],
            "fee_entry": round(fee_entry, 4),
        }
        log_signal("BUY_EXECUTED", symbol, {
            "price": effective_buy,
            "size": pos["size"],
            "stop_loss": pos["stop_loss"],
            "take_profit": pos["take_profit"],
            "fee_entry": round(fee_entry, 4),
            "total_cost": round(total_cost, 4),
            "risk_eur": pos["risk_eur"],
            "adx": round(adx, 2),
            "rsi": round(float(last["rsi"]), 2),
            "volume_ratio": round(volume_ratio, 3),
            "capital_before": round(state["capital"] + total_cost, 2),
        })

        log(
            f"▲ {symbol} BUY | Prix: {effective_buy:.4f}€ | "
            f"Taille: {pos['size']} | SL: {pos['stop_loss']:.4f}€ | "
            f"TP: {pos['take_profit']:.4f}€ | Risque: {pos['risk_eur']:.2f}€ | "
            f"Frais: {fee_entry:.2f}€",
            "BUY",
        )
        notify(
            f"▲ <b>{symbol}</b> BUY | {effective_buy:.2f}€ | "
            f"SL {pos['stop_loss']:.2f}€ | TP {pos['take_profit']:.2f}€"
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


def _check_daily_snapshot(state: dict):
    """Enregistre un snapshot journalier dans signals.jsonl si la date a changé."""
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_snapshot_date", "") == today:
        return

    trades = state["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    total_value = state["capital"] + sum(
        p["entry"] * p["size"] for p in state["positions"].values()
    )
    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0
    pnl_pct = round((total_value - state["initial_capital"]) / state["initial_capital"] * 100, 2)

    log_signal("DAILY_SNAPSHOT", "ALL", {
        "capital": round(state["capital"], 2),
        "total_value": round(total_value, 2),
        "open_positions": len(state["positions"]),
        "total_trades": len(trades),
        "win_rate": win_rate,
        "pnl_pct": pnl_pct,
    })
    notify(
        f"📊 <b>Snapshot journalier</b>\n"
        f"Capital libre: {state['capital']:.2f}€\n"
        f"Valeur totale: {total_value:.2f}€ ({pnl_pct:+.2f}%)\n"
        f"Trades: {len(trades)} | Win rate: {win_rate}%"
    )
    state["last_snapshot_date"] = today


def _is_us_market_open() -> bool:
    """Marché US ouvert : lun-ven, 14h30-21h00 CET (heure d'hiver, UTC+1)."""
    from datetime import timezone, timedelta as td
    cet = datetime.now(timezone(td(hours=1)))
    if cet.weekday() >= 5:   # samedi=5, dimanche=6
        return False
    t = cet.hour * 60 + cet.minute
    open_t  = config.XSTOCK_MARKET_OPEN_CET[0]  * 60 + config.XSTOCK_MARKET_OPEN_CET[1]
    close_t = config.XSTOCK_MARKET_CLOSE_CET[0] * 60 + config.XSTOCK_MARKET_CLOSE_CET[1]
    return open_t <= t <= close_t


def _check_premarket(state: dict):
    """Déclenche l'analyse pré-marché Claude une fois par jour ouvré à 14h00 CET."""
    from datetime import timezone, timedelta as td
    cet = datetime.now(timezone(td(hours=1)))
    today = cet.strftime("%Y-%m-%d")
    if cet.weekday() >= 5:
        return
    ph, pm = config.XSTOCK_PREMARKET_CET
    if cet.hour * 60 + cet.minute < ph * 60 + pm:
        return
    if state.get("last_premarket_date", "") == today:
        return
    log("Lancement analyse pré-marché xStocks...", "INFO")
    try:
        from live.xstock_advisor import run_premarket_analysis
        run_premarket_analysis(state)
    except Exception as e:
        log(f"Erreur analyse pré-marché: {e}", "WARN")
    state["last_premarket_date"] = today


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

            btc_context = fetch_btc_context()
            if btc_context:
                log(
                    f"BTC context: {btc_context['btc_price']:.0f}€ | "
                    f"Trend: {btc_context['btc_trend'].upper()} | "
                    f"Above EMA200: {btc_context['btc_above_ema200']}",
                    "INFO",
                )

            for symbol in config.SYMBOLS:
                state = process_symbol(symbol, state, btc_context=btc_context)

            save_state(state)
            print_status(state)
            _check_daily_snapshot(state)
            _check_premarket(state)

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

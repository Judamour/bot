import ccxt
import time
import json
import os
import sys
from datetime import datetime
from colorama import Fore, Style, init

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import fetch_ohlcv, get_exchange, fetch_fear_greed, fetch_funding_rates, fetch_news_yfinance, fetch_news_macro_rss
from strategies.supertrend import generate_signals, calculate_position_size, add_indicators
from live.claude_filter import ask_claude
from live.notifier import notify, notify_file

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

def process_symbol(
    symbol: str,
    state: dict,
    btc_context: dict = None,
    vix_factor: float = 1.0,
    vix: float = 0.0,
    fear_greed: dict = None,
    funding_rate: float = 0.0,
    macro_news: list = None,
) -> dict:
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
    if fear_greed:
        scan_data["fear_greed_score"] = fear_greed.get("score")
        scan_data["fear_greed_label"] = fear_greed.get("label")
    if funding_rate != 0.0:
        scan_data["funding_rate"] = round(funding_rate, 6)
    scan_data["vix"] = round(vix, 1) if vix else None
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

        # Corrélation secteur — max 1 position par secteur
        sector = config.SECTORS.get(symbol)
        if sector:
            occupied = [s for s in state["positions"] if config.SECTORS.get(s) == sector]
            if occupied:
                log(f"{symbol} — Signal ignoré (secteur '{sector}' déjà occupé par {occupied[0]})", "INFO")
                log_signal("BUY_SKIP_SECTOR", symbol, {"sector": sector, "occupied_by": occupied[0], "price": current_price})
                return state

        # Filtre earnings xStocks
        if symbol in config.XSTOCKS:
            from data.fetcher import _xstock_ticker
            ticker = _xstock_ticker(symbol)
            if _has_earnings_soon(ticker):
                log(f"{symbol} — Signal ignoré (rapport trimestriel dans <24h)", "WARN")
                log_signal("BUY_SKIP_EARNINGS", symbol, {"ticker": ticker, "price": current_price})
                return state

        # Confirmation multi-timeframe (1d) — avant Claude pour économiser les appels API
        ok_1d, reason_1d = _confirm_daily_trend(symbol)
        log(f"{symbol} — MTF 1d: {'✓' if ok_1d else '✗'} {reason_1d}", "INFO")
        log_signal("MTF_FILTER", symbol, {"ok_1d": ok_1d, "reason": reason_1d, "price": current_price})
        if not ok_1d:
            log_signal("BUY_SKIP_MTF", symbol, {"reason": reason_1d, "price": current_price})
            return state

        log(
            f"{symbol} — Signal BUY | ADX: {adx:.1f} | Vol×{volume_ratio:.2f} | "
            f"RSI: {last['rsi']:.1f} — consultation Claude...",
            "INFO",
        )
        recent_trades = state.get("trades", [])[-20:]
        recent_wr = (
            sum(1 for t in recent_trades if t.get("pnl", 0) > 0) / len(recent_trades) * 100
            if recent_trades else None
        )

        # ── News : symbol-specific (lazy, seulement si BUY) + macro du cycle ──
        news = list(macro_news or [])
        if symbol in config.XSTOCKS:
            from data.fetcher import _xstock_ticker
            sym_ticker = _xstock_ticker(symbol)          # NVDAx/EUR → NVDA
            sym_news = fetch_news_yfinance(sym_ticker, limit=3, hours=48)
        else:
            crypto_ticker = symbol.split("/")[0] + "-USD"  # BTC/EUR → BTC-USD
            sym_news = fetch_news_yfinance(crypto_ticker, limit=3, hours=48)
        news = sym_news + news  # symbol en priorité, macro en complément
        news = news[:6]

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
            btc_context=btc_context,
            vix=vix,
            fear_greed=fear_greed,
            funding_rate=funding_rate,
            open_positions=len(state["positions"]),
            max_positions=config.MAX_OPEN_TRADES,
            recent_win_rate=recent_wr,
            rotation_factor=vix_factor,
            daily_trend_reason=reason_1d,
            news=news if news else None,
        )
        log(f"{symbol} — Claude: {'✓ CONFIRME' if confirme else '✗ IGNORE'} | {raison}", "INFO")
        log_signal("CLAUDE_FILTER", symbol, {
            "decision": "CONFIRME" if confirme else "IGNORE",
            "raison": raison,
            "adx": round(adx, 2),
            "rsi": round(float(last["rsi"]), 2),
            "volume_ratio": round(volume_ratio, 3),
            "price": current_price,
            "vix": round(vix, 1) if vix else None,
            "fear_greed_score": fear_greed.get("score") if fear_greed else None,
            "funding_rate": round(funding_rate, 6) if funding_rate else None,
            "btc_trend": btc_context.get("btc_trend") if btc_context else None,
            "rotation_factor": vix_factor,
        })

        if not confirme:
            log_signal("BUY_SKIP_CLAUDE", symbol, {"raison": raison, "price": current_price})
            return state

        effective_buy = current_price * (1 + config.SLIPPAGE)
        if vix_factor != 1.0:
            log(f"{symbol} — Capital factor {vix_factor:.2f}x (VIX/rotation)", "WARN" if vix_factor < 1.0 else "INFO")
        pos = calculate_position_size(state["capital"] * vix_factor, effective_buy, atr)
        fee_entry = effective_buy * pos["size"] * config.EXCHANGE_FEE
        total_cost = pos["size"] * effective_buy + fee_entry

        if total_cost > state["capital"]:
            log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€)", "WARN")
            return state

        state["capital"] -= total_cost
        position_data = {
            "entry": effective_buy,
            "size": pos["size"],
            "stop": pos["stop_loss"],
            "initial_stop": pos["stop_loss"],
            "tp": pos["take_profit"],
            "date": str(datetime.now()),
            "risk_eur": pos["risk_eur"],
            "fee_entry": round(fee_entry, 4),
        }

        state["positions"][symbol] = position_data
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
        _send_trade_chart(symbol, df, effective_buy, pos["stop_loss"], pos["take_profit"])

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

    # Backup paper_state.json — local + Telegram
    try:
        import shutil
        backup_dir = "logs/backups"
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = f"{backup_dir}/paper_state_{today}.json"
        shutil.copy2(STATE_FILE, backup_path)
        # Garder 30 jours de backups locaux
        backups = sorted(os.listdir(backup_dir))
        for old in backups[:-30]:
            os.remove(os.path.join(backup_dir, old))
        notify_file(STATE_FILE, f"📦 Backup {today} — {total_value:.2f}€ ({pnl_pct:+.2f}%)")
    except Exception as e:
        log(f"Backup paper_state échoué: {e}", "WARN")


def _is_us_market_open() -> bool:
    """Marché US ouvert : lun-ven, 9h30-16h00 ET (gère automatiquement EST/EDT)."""
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    t = et.hour * 60 + et.minute
    open_t  = config.XSTOCK_MARKET_OPEN_ET[0]  * 60 + config.XSTOCK_MARKET_OPEN_ET[1]
    close_t = config.XSTOCK_MARKET_CLOSE_ET[0] * 60 + config.XSTOCK_MARKET_CLOSE_ET[1]
    return open_t <= t <= close_t


def _check_premarket(state: dict, btc_context: dict = None, vix: float = 0.0, fear_greed: dict = None):
    """Déclenche l'analyse pré-marché Claude à 8h00 ET (= 14h CET hiver / 15h CEST été)."""
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    today = et.strftime("%Y-%m-%d")
    if et.weekday() >= 5:
        return
    ph, pm = config.XSTOCK_PREMARKET_ET
    if et.hour * 60 + et.minute < ph * 60 + pm:
        return
    if state.get("last_premarket_date", "") == today:
        return
    log("Lancement analyse pré-marché xStocks...", "INFO")
    try:
        from live.xstock_advisor import run_premarket_analysis
        run_premarket_analysis(state, btc_context=btc_context, vix=vix, fear_greed=fear_greed)
    except Exception as e:
        log(f"Erreur analyse pré-marché: {e}", "WARN")
    state["last_premarket_date"] = today


def _check_max_drawdown(state: dict) -> bool:
    """Retourne True si le drawdown depuis le capital initial dépasse MAX_DRAWDOWN (-15%)."""
    total_value = state["capital"] + sum(
        p["entry"] * p["size"] for p in state["positions"].values()
    )
    drawdown = (total_value - state["initial_capital"]) / state["initial_capital"]
    if drawdown <= config.MAX_DRAWDOWN:
        msg = f"MAX DRAWDOWN {drawdown*100:.1f}% — Bot arrêté (seuil {config.MAX_DRAWDOWN*100:.0f}%)"
        log(f"⛔ {msg}", "WARN")
        notify(f"⛔ <b>{msg}</b>\nReprise manuelle uniquement.")
        return True
    return False


def _has_earnings_soon(ticker: str) -> bool:
    """True si rapport trimestriel dans les 24h (avant ou après). Permissif si données indispo."""
    try:
        import yfinance as yf
        from datetime import timezone as tz
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return False
        dates = []
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", []) or []
        elif hasattr(cal, "loc") and "Earnings Date" in cal.index:
            val = cal.loc["Earnings Date"]
            dates = val if hasattr(val, "__iter__") else [val]
        now = datetime.now(tz.utc)
        for d in dates:
            if d is None:
                continue
            if hasattr(d, "to_pydatetime"):
                d = d.to_pydatetime()
            if hasattr(d, "tzinfo") and d.tzinfo is None:
                d = d.replace(tzinfo=tz.utc)
            if abs((d - now).total_seconds()) < 24 * 3600:
                return True
        return False
    except Exception:
        return False


def fetch_vix() -> float:
    """Retourne le VIX actuel (indice de peur US). Retourne 0 si indisponible."""
    try:
        import yfinance as yf
        df = yf.Ticker("^VIX").history(period="2d", interval="1h")
        if not df.empty:
            return round(float(df["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return 0.0


def _compute_rotation_factors(trades: list) -> dict:
    """
    Rotation du capital entre crypto et xStocks selon performance relative.
    Basé sur les 20 derniers trades fermés de chaque catégorie.

    Retourne {'crypto': factor, 'xstock': factor} avec facteurs entre 0.7 et 1.3.
    Bidirectionnel : si crypto > xStocks → crypto ×1.3 / xStocks ×0.7, et vice-versa.
    Neutre (1.0 / 1.0) si moins de 3 trades par catégorie.
    """
    recent = sorted(trades, key=lambda x: x.get("exit_date", ""))[-20:]

    crypto_pnl  = [t["pnl"] for t in recent if t.get("symbol") in config.CRYPTO]
    xstock_pnl  = [t["pnl"] for t in recent if t.get("symbol") in config.XSTOCKS]

    if len(crypto_pnl) < 3 or len(xstock_pnl) < 3:
        return {"crypto": 1.0, "xstock": 1.0}

    avg_crypto = sum(crypto_pnl) / len(crypto_pnl)
    avg_xstock = sum(xstock_pnl) / len(xstock_pnl)

    # diff positif → crypto surperforme → boost crypto, réduire xstock
    # diff négatif → xstock surperforme → boost xstock, réduire crypto
    diff = avg_crypto - avg_xstock
    raw = max(-0.3, min(0.3, diff / 20.0))   # ±20€ d'écart → ±0.3x

    return {
        "crypto": round(1.0 + raw, 2),
        "xstock": round(1.0 - raw, 2),
    }


def _confirm_daily_trend(symbol: str) -> tuple:
    """
    Confirmation multi-timeframe : vérifie que la tendance 1d valide le signal 4h.
    Retourne (ok: bool, raison: str). Permissif si données indisponibles.
    """
    try:
        df = fetch_ohlcv(symbol, "1d", days=250)
        df = add_indicators(df)
        last = df.iloc[-1]
        st_up = int(last["supertrend_dir"]) == 1
        above_200 = float(last["close"]) > float(last["ema200"])
        if st_up and above_200:
            return True, "1d ✓ (ST↑ + >EMA200)"
        parts = []
        if not st_up:
            parts.append("ST 1d baissier")
        if not above_200:
            parts.append("sous EMA200 1d")
        return False, ", ".join(parts)
    except Exception as e:
        return True, f"1d non vérifié ({e})"  # permissif si erreur


def _send_trade_chart(symbol: str, df, entry: float, stop: float, tp: float):
    """Génère un chart 4h (60 dernières bougies) et l'envoie via Telegram."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import tempfile

        plot_df = df.tail(60).copy()
        n = len(plot_df)
        xs = list(range(n))

        fig, ax = plt.subplots(figsize=(10, 4))
        fig.patch.set_facecolor("#0d1117")
        ax.set_facecolor("#161b22")

        ax.plot(xs, plot_df["close"], color="#e6edf3", linewidth=1.2)

        if "supertrend" in plot_df.columns and "supertrend_dir" in plot_df.columns:
            for i in range(1, n):
                c = "#3fb950" if plot_df["supertrend_dir"].iloc[i] == 1 else "#f85149"
                ax.plot([i-1, i],
                        [plot_df["supertrend"].iloc[i-1], plot_df["supertrend"].iloc[i]],
                        color=c, linewidth=1.5)

        if "ema200" in plot_df.columns:
            ax.plot(xs, plot_df["ema200"], color="#ffa657", linewidth=0.8, alpha=0.8)

        ax.axhline(entry, color="#58a6ff", linewidth=1.5, linestyle="--")
        ax.axhline(stop,  color="#f85149", linewidth=1,   linestyle=":")
        ax.axhline(tp,    color="#3fb950", linewidth=1,   linestyle=":")
        ax.fill_between(xs, stop, tp, alpha=0.04, color="#58a6ff")
        ax.scatter([n - 1], [entry], color="#3fb950", s=80, zorder=5)

        ax.set_title(
            f"{symbol} — BUY {entry:.2f}€  SL {stop:.2f}€  TP {tp:.2f}€",
            color="#e6edf3", fontsize=10, pad=6,
        )
        ax.tick_params(colors="#8b949e", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.set_xticks([])
        ax.grid(alpha=0.15, color="#30363d")
        plt.tight_layout(pad=0.5)

        tmp = tempfile.mktemp(suffix=".png")
        plt.savefig(tmp, dpi=120, bbox_inches="tight", facecolor="#0d1117")
        plt.close(fig)

        notify_file(tmp, f"▲ {symbol}  Entrée {entry:.2f}€  SL {stop:.2f}€  TP {tp:.2f}€")
        os.unlink(tmp)
    except Exception as e:
        log(f"{symbol} — Chart Telegram échoué: {e}", "WARN")


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

            # Valeurs par défaut en cas d'erreur de fetch
            fear_greed = {"score": 50, "label": "Neutral"}
            funding_rates = {}
            macro_news = []

            btc_context = fetch_btc_context()
            if btc_context:
                log(
                    f"BTC context: {btc_context['btc_price']:.0f}€ | "
                    f"Trend: {btc_context['btc_trend'].upper()} | "
                    f"Above EMA200: {btc_context['btc_above_ema200']}",
                    "INFO",
                )

            vix = fetch_vix()
            # Scaling linéaire : VIX 15 → ×1.0, VIX 25 → ×0.625, VIX 35+ → ×0.25
            vix_factor = round(max(0.25, 1.0 - max(0.0, vix - 15) * 0.0375), 2) if vix > 0 else 1.0
            if vix > 0:
                log(
                    f"VIX: {vix:.1f} → facteur taille ×{vix_factor} "
                    f"{'⚠ VOLATILITÉ ÉLEVÉE' if vix > 25 else '(normal)'}",
                    "WARN" if vix > 25 else "INFO",
                )

            fear_greed = fetch_fear_greed()
            fg_score = fear_greed.get("score", 50)
            log(
                f"Fear & Greed: {fg_score}/100 ({fear_greed.get('label', '?')}) "
                f"{'⚠ PEUR EXTRÊME' if fg_score <= 20 else '⚠ AVIDITÉ EXTRÊME' if fg_score >= 80 else ''}",
                "WARN" if fg_score <= 20 or fg_score >= 80 else "INFO",
            )

            funding_rates = fetch_funding_rates(config.CRYPTO)
            if funding_rates:
                high_fr = {s: r for s, r in funding_rates.items() if r > 0.001}
                if high_fr:
                    log(f"⚠ Funding rates élevés: {high_fr}", "WARN")

            macro_news = fetch_news_macro_rss(limit=4)
            if macro_news:
                log(f"News macro: {len(macro_news)} headlines chargés", "INFO")

            rotation = _compute_rotation_factors(state.get("trades", []))
            if rotation["crypto"] != 1.0:
                log(
                    f"Rotation capital: crypto ×{rotation['crypto']} | "
                    f"xStocks ×{rotation['xstock']} (perf relative 20 derniers trades)",
                    "INFO",
                )

            for symbol in config.SYMBOLS:
                category = "xstock" if symbol in config.XSTOCKS else "crypto"
                combined = round(vix_factor * rotation[category], 2)
                fr = funding_rates.get(symbol, 0.0)
                state = process_symbol(
                    symbol, state,
                    btc_context=btc_context,
                    vix_factor=combined,
                    vix=vix,
                    fear_greed=fear_greed,
                    funding_rate=fr,
                    macro_news=macro_news,
                )

            save_state(state)
            print_status(state)
            if _check_max_drawdown(state):
                save_state(state)
                break
            _check_daily_snapshot(state)
            _check_premarket(state, btc_context=btc_context, vix=vix, fear_greed=fear_greed)

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

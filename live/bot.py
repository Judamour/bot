import ccxt
import time
import json
import os
import sys
from datetime import datetime, timedelta, timezone  # timedelta requis par _next_cycle_utc()

COOLDOWN_HOURS_AFTER_EXIT = 12  # Anti-whipsaw : block re-entry pendant 12h après tout exit


def _is_in_cooldown(state: dict, symbol: str) -> tuple[bool, datetime | None]:
    """Retourne (in_cooldown, until). Nettoie les cooldowns expirés au passage."""
    cooldowns = state.get("cooldowns") or {}
    until_str = cooldowns.get(symbol)
    if not until_str:
        return False, None
    try:
        until = datetime.fromisoformat(until_str)
    except Exception:
        cooldowns.pop(symbol, None)
        return False, None
    now = datetime.now(until.tzinfo) if until.tzinfo else datetime.now()
    if now < until:
        return True, until
    cooldowns.pop(symbol, None)
    return False, None


def _set_cooldown(state: dict, symbol: str, hours: int = COOLDOWN_HOURS_AFTER_EXIT):
    """Set un cooldown sur le symbole pour `hours` heures (UTC)."""
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    state.setdefault("cooldowns", {})[symbol] = until.isoformat()
from colorama import Fore, Style, init

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import fetch_ohlcv, get_exchange, fetch_fear_greed, fetch_funding_rates, fetch_news_yfinance, fetch_news_macro_rss, fetch_qqq_regime
from strategies.supertrend import generate_signals, calculate_position_size, add_indicators
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
    # Écriture atomique : write temp + rename (évite JSON corrompu si crash pendant sauvegarde)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    colors = {"INFO": Fore.CYAN, "BUY": Fore.GREEN, "SELL": Fore.RED, "WARN": Fore.YELLOW}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{colors.get(level, '')}{ts} [{level}] {msg}{Style.RESET_ALL}")
    with open("logs/bot.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


# ── Trailing Stop (ATR continu) ───────────────────────────────────────────────

def apply_trailing_stop(position: dict, current_price: float, atr: float, symbol: str, df=None) -> dict:
    """
    Chandelier Exit : trailing stop = max(high[-22:]) - ATR_MULTIPLIER × ATR.
    Suit la structure du marché (plus haut récent) au lieu du prix instantané.
    Évite les stops à -0.0R/-0.2R (entrées en plein bruit). QuantifiedStrategies, StockCharts.

    Fallback ATR classique si df absent ou < 22 barres.
    """
    if df is not None and len(df) >= 22:
        recent_high = float(df["high"].tail(22).max())
        new_stop = round(recent_high - config.ATR_MULTIPLIER * atr, 4)
    else:
        new_stop = round(current_price - config.ATR_MULTIPLIER * atr, 4)
    if new_stop > position["stop"]:
        position["stop"] = new_stop
        log(f"{symbol} — Trailing stop → {new_stop:.4f}€", "INFO")
        # Sync broker-side stop (Alpaca) si présent
        sid = position.get("alpaca_stop_id")
        if sid:
            from live.order_executor import update_broker_stop, place_broker_stop
            new_id = update_broker_stop(symbol, sid, new_stop, qty=position["size"])
            if new_id is None:
                # PATCH a échoué → recreate
                ids = place_broker_stop(symbol, position["size"], new_stop)
                position["alpaca_stop_id"] = ids.get("stop_id")
            else:
                position["alpaca_stop_id"] = new_id
    return position


# ── Logique principale ────────────────────────────────────────────────────────

def process_symbol(
    symbol: str,
    state: dict,
    df=None,
    btc_context: dict = None,
    vix_factor: float = 1.0,
    vix: float = 0.0,
    fear_greed: dict = None,
    funding_rate: float = 0.0,
    macro_news: list = None,
    qqq_regime_ok: bool = True,
    qqq_description: str = "N/A",
    ohlcv_daily: dict = None,  # BUG-11 : cache daily passé depuis multi_runner pour éviter fetch redondant
    btc_dominance_up: bool = False,
) -> dict:
    """Analyse un symbole et exécute les ordres si nécessaire."""
    if state.get("dd_frozen"):
        return state  # Bot gelé — pas de nouveaux trades
    try:
        if df is None:
            df = fetch_ohlcv(symbol, config.TIMEFRAME, days=45)
        df = generate_signals(df)
        last = df.iloc[-1]
        current_price = float(last["close"])
        low_price     = float(last["low"])   # utilisé pour stop loss intraday (même fix que Bot C)
        signal = int(last["signal"])
        atr = float(last["atr"])
        adx = float(last["adx"])
        volume_ratio = float(last["volume_ratio"])

    except Exception as e:
        log(f"{symbol} — Erreur récupération données: {e}", "WARN")
        return state

    # ── Volatility targeting : exposure = clamp(TARGET_VOL / realized_vol, FLOOR, MAX_LEVERAGE) ──
    # Floor 0.50 (0.60 crypto) : sinon NVDA σ=40%, CRWD σ=60%, BTC σ=32% écrasés
    # à 0.25-0.46 → positions ridicules qui ratent les rallies (NVDA +25%, BTC +20%).
    is_crypto = symbol in config.CRYPTO if hasattr(config, "CRYPTO") else False
    vol_floor = 0.60 if is_crypto else 0.50
    try:
        daily_close = df["close"].resample("1D").last().dropna()
        if len(daily_close) >= 21:
            returns = daily_close.pct_change().dropna()
            realized_vol = float(returns.tail(20).std() * (252 ** 0.5))
            vol_exposure = round(max(vol_floor, min(config.MAX_LEVERAGE, config.TARGET_VOL / realized_vol)), 2) if realized_vol > 0 else 1.0
        else:
            realized_vol = 0.0
            vol_exposure = 1.0
    except Exception:
        realized_vol = 0.0
        vol_exposure = 1.0

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

    # ── Renouvellement broker stop expiré (Alpaca DAY tif sur fractional) ──
    if position:
        from live.order_executor import renew_broker_stop_if_expired
        renew_broker_stop_if_expired(symbol, position)

    # ── Trailing stop avant vérification de sortie ──
    if position:
        position = apply_trailing_stop(position, current_price, atr, symbol, df=df)
        state["positions"][symbol] = position

    # ── Stop break-even auto à +1R : zéro perte si reversal après +1R atteint ──
    if position and not position.get("breakeven_set"):
        risk_per_unit = abs(position["entry"] - position.get("initial_stop", position["stop"]))
        if risk_per_unit > 0 and current_price >= position["entry"] + risk_per_unit:
            new_stop = round(position["entry"] * 1.001, 4)  # entrée + 0.1% pour couvrir frais
            if new_stop > position["stop"]:
                position["stop"] = new_stop
                position["breakeven_set"] = True
                log(f"{symbol} — Stop déplacé au break-even ({new_stop:.4f}€) après +1R", "INFO")
                # Sync broker stop
                sid = position.get("alpaca_stop_id")
                if sid:
                    from live.order_executor import update_broker_stop, place_broker_stop
                    new_id = update_broker_stop(symbol, sid, new_stop, qty=position["size"])
                    if new_id is None:
                        ids = place_broker_stop(symbol, position["size"], new_stop)
                        position["alpaca_stop_id"] = ids.get("stop_id")
                    else:
                        position["alpaca_stop_id"] = new_id

    # ── Scale-out partiel : sortir 50% à +1.5R, laisse 50% courir en chandelier ──
    # Réduit la variance du PnL, sécurise une partie du gain. AQR "A Century of
    # Evidence on Trend-Following": gros gains viennent d'une minorité de trades
    # qui courent loin → trailing residuel les capture mieux qu'un TP fixe.
    if position and not position.get("scaled_out"):
        risk_per_unit = abs(position["entry"] - position.get("initial_stop", position["stop"]))
        if risk_per_unit > 0 and current_price >= position["entry"] + 1.5 * risk_per_unit:
            half_size = round(position["size"] / 2, 6)
            # Si la moitié restante serait sous min_order_eur, on fait un FULL exit
            # (sinon résiduel bloqué : SELL final rejeté pour < min order).
            half_value = half_size * current_price
            full_exit_required = half_value < config.MIN_ORDER_EUR
            from live.order_executor import (
                execute_sell as _exec_sell, cancel_broker_stop, place_broker_stop,
            )
            old_stop_id = position.get("alpaca_stop_id")
            if old_stop_id:
                cancel_broker_stop(symbol, old_stop_id)
                position["alpaca_stop_id"] = None

            if full_exit_required:
                # Position trop petite pour scale-out → FULL exit
                full_size = position["size"]
                _order = _exec_sell(symbol, full_size, current_price, reason="scale_out_full")
                if not _order.success:
                    log(f"{symbol} — Scale-out FULL exit échoué: {_order.error} — re-place stop", "WARN")
                    ids = place_broker_stop(symbol, position["size"], position["stop"])
                    if ids.get("stop_id"):
                        position["alpaca_stop_id"] = ids["stop_id"]
                else:
                    exit_eff = _order.filled_price * (1 - config.EXCHANGE_FEE)
                    fee_full = exit_eff * full_size * config.EXCHANGE_FEE
                    proceeds = exit_eff * full_size - fee_full
                    state["capital"] += proceeds
                    pnl = proceeds - (position["entry"] * full_size + position.get("fee_entry", 0))
                    state.setdefault("trades", []).append({
                        "symbol": symbol,
                        "entry_date": position.get("date"),
                        "exit_date": datetime.now(timezone.utc).isoformat(),
                        "entry_price": position["entry"],
                        "exit_price": _order.filled_price,
                        "pnl": round(pnl, 2),
                        "reason": "scale_out_full",
                        "result": "win" if pnl > 0 else "loss",
                    })
                    state["positions"].pop(symbol, None)
                    _set_cooldown(state, symbol)
                    log(f"{symbol} — Scale-out FULL (residuel < {config.MIN_ORDER_EUR}€) | "
                        f"PnL: {pnl:+.2f} | position fermée", "INFO")
                return state

            _order = _exec_sell(symbol, half_size, current_price, reason="scale_out")
            if not _order.success:
                log(f"{symbol} — Scale-out SELL échoué: {_order.error} — re-place stop original", "WARN")
                ids = place_broker_stop(symbol, position["size"], position["stop"])
                if ids.get("stop_id"):
                    position["alpaca_stop_id"] = ids["stop_id"]
            else:
                exit_eff = _order.filled_price * (1 - config.EXCHANGE_FEE)
                fee_partial = exit_eff * half_size * config.EXCHANGE_FEE
                proceeds_partial = exit_eff * half_size - fee_partial
                state["capital"] += proceeds_partial
                position["size"] -= half_size
                position["scaled_out"] = True
                log(f"{symbol} — Scale-out 50% à +1.5R [BROKER] | Prix: {_order.filled_price:.4f} | "
                    f"Taille restante: {position['size']:.6f}", "INFO")
                ids = place_broker_stop(symbol, position["size"], position["stop"])
                if ids.get("stop_id"):
                    position["alpaca_stop_id"] = ids["stop_id"]
                else:
                    log(f"{symbol} — Re-place broker stop après scale-out échoué — bot SL interne uniquement", "WARN")

    # ── Vérifier stop-loss / take-profit ──
    if position:
        reason = None
        exit_price = current_price

        if low_price <= position["stop"]:  # BUG-24 Bot A : utiliser low pour capter les stops intraday
            reason = "stop_loss"
            exit_price = position["stop"]
        elif signal == -1:
            reason = "signal_exit"
        else:
            # Time-stop 60j : libère le capital sur position zombie (pas TP/SL/signal)
            try:
                pos_date = position.get("date", "")
                if pos_date:
                    age_days = (datetime.now() - datetime.fromisoformat(pos_date.split(".")[0].replace(" ", "T"))).days
                    if age_days >= 60:
                        reason = "time_stop_60d"
                        exit_price = current_price
            except Exception:
                pass

        if reason:
            # Annuler le stop-loss broker AVANT le SELL manuel (sinon double-fill)
            from live.order_executor import (
                execute_sell as _exec_sell, cancel_broker_stop,
                handle_failed_sell, reset_sell_fail_count,
            )
            cancel_broker_stop(symbol, position.get("alpaca_stop_id"))
            cancel_broker_stop(symbol, position.get("alpaca_tp_id"))
            _order = _exec_sell(symbol, position["size"], exit_price, reason=reason)
            if not _order.success:
                if handle_failed_sell(symbol, position, _order.error or ""):
                    # Force-close : delisted/suspended OU 3 échecs consécutifs
                    log(f"⛔ {symbol} force-close après échec SELL persistant ({_order.error})", "WARN")
                    notify(f"⚠️ <b>{symbol}</b> force-close (delisted/persistent fail)\n"
                           f"Reason: {(_order.error or '')[:100]}")
                    state["positions"].pop(symbol, None)
                    _set_cooldown(state, symbol, hours=24)
                    return state
                log(f"⛔ SELL {symbol} échoué: {_order.error} — position maintenue", "WARN")
                return state
            reset_sell_fail_count(position)
            exit_price_eff = _order.filled_price * (1 - config.EXCHANGE_FEE)
            fee_exit = exit_price_eff * position["size"] * config.EXCHANGE_FEE
            proceeds = exit_price_eff * position["size"] - fee_exit
            state["capital"] += proceeds
            state["positions"].pop(symbol)
            _set_cooldown(state, symbol)  # Anti-whipsaw 12h

            pnl = proceeds - (position["entry"] * position["size"] + position.get("fee_entry", 0))
            trade = {
                "symbol": symbol,
                "entry_date": position["date"],
                "exit_date": datetime.now(timezone.utc).isoformat(),
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
            # BUG-13 : "take_profit" jamais généré (pas de TP hard dans cette stratégie) — cas absorbé par else
            if reason == "stop_loss":
                notify(f"🔴 <b>{symbol}</b> SL {pnl:.2f}€ ({pnl_r:+.1f}R)")
            else:
                notify(f"⏹ <b>{symbol}</b> EXIT [{reason}] {pnl:+.2f}€ ({pnl_r:+.1f}R)")

            # ── Win rate degradation alert ──
            recent = state.get("trades", [])[-10:]
            if len(recent) >= 10:
                wr = sum(1 for t in recent if t.get("pnl", 0) > 0) / len(recent) * 100
                if wr < 20:
                    from live.notifier import notify_winrate_drop
                    notify_winrate_drop("A", wr, len(recent))

    # ── Ouvrir position sur signal achat ──
    # Cooldown anti-whipsaw : bloque re-entry pendant 12h après tout exit
    if signal == 1 and symbol not in state["positions"]:
        in_cd, until = _is_in_cooldown(state, symbol)
        if in_cd:
            mins_left = int((until - datetime.now(until.tzinfo)).total_seconds() / 60)
            log(f"{symbol} — Signal ignoré (cooldown anti-whipsaw, reste {mins_left}min)", "INFO")
            log_signal("BUY_SKIP_COOLDOWN", symbol, {"price": current_price, "until": until.isoformat()})
            return state

    if signal == 1 and symbol in config.STOCKS and not _is_us_market_open():
        return state  # Silencieux — marché US fermé (évite spam logs)

    if signal == 1 and symbol in config.STOCKS and not qqq_regime_ok:
        log(f"{symbol} — Signal ignoré (régime baissier : {qqq_description})", "WARN")
        log_signal("BUY_SKIP_QQQ_REGIME", symbol, {"qqq": qqq_description, "price": current_price})
        return state

    if signal == 1 and symbol in config.CRYPTO and symbol != "BTC/EUR":
        if btc_context and not btc_context.get("btc_above_ema200", True):
            log(f"{symbol} — Signal ignoré (BTC bear — sous EMA200)", "WARN")
            log_signal("BUY_SKIP_BTC_REGIME", symbol, {"btc_trend": "bear", "price": current_price})
            return state

    # Funding rate gate (crypto only) : >0.05%/8h = marché surleveragé long, risque de liquidation cascade
    # Source: QuantJourney "Funding Rates: Hidden Cost, Sentiment Signal" 2026
    if signal == 1 and symbol in config.CRYPTO and funding_rate > 0.0005:
        log(f"{symbol} — Signal ignoré (funding {funding_rate*100:.3f}%/8h > 0.05% — marché surleveragé)", "WARN")
        log_signal("BUY_SKIP_FUNDING", symbol, {"funding_rate": funding_rate, "price": current_price})
        return state

    # BTC dominance gate (altcoins) : si BTC.D > SMA20 → flux vers BTC, altcoins sous-performent
    # Source: Alphaex Capital, Nexo "BTC Dominance Altcoin Season Signals"
    if signal == 1 and symbol in config.CRYPTO and symbol != "BTC/EUR" and btc_dominance_up:
        log(f"{symbol} — Signal ignoré (BTC.D en hausse — altseason terminée)", "INFO")
        log_signal("BUY_SKIP_BTC_DOMINANCE", symbol, {"price": current_price})
        return state

    if signal == 1 and symbol not in state["positions"]:
        # Portfolio exposure cap
        if state.get("_exposure_blocked"):
            log(f"{symbol} — Signal ignoré (exposition portfolio > 80%)", "WARN")
            return state

        if len(state["positions"]) >= config.MAX_OPEN_TRADES:
            log(f"{symbol} — Signal ignoré (max {config.MAX_OPEN_TRADES} positions ouvertes)", "WARN")
            log_signal("BUY_SKIP_MAX_POS", symbol, {"price": current_price, "open_positions": len(state["positions"])})
            return state

        # Symbol exclusivity cross-bots : un autre bot le détient déjà
        held_by_others = state.get("_held_by_other_bots") or set()
        if symbol in held_by_others:
            log(f"{symbol} — Signal ignoré (déjà détenu par un autre sub-bot)", "INFO")
            log_signal("BUY_SKIP_HELD_BY_OTHER", symbol, {"price": current_price})
            return state

        # Corrélation secteur — max MAX_PER_SECTOR positions par secteur (intra-bot)
        sector = config.SECTORS.get(symbol)
        if sector:
            occupied = [s for s in state["positions"] if config.SECTORS.get(s) == sector]
            if len(occupied) >= config.MAX_PER_SECTOR:
                log(f"{symbol} — Signal ignoré (secteur '{sector}' saturé intra-bot: {occupied})", "INFO")
                log_signal("BUY_SKIP_SECTOR", symbol, {"sector": sector, "occupied_by": occupied, "price": current_price})
                return state
            # Cap GLOBAL cross-bots : si secteur déjà saturé sur l'ensemble du portfolio
            blocked = state.get("_blocked_sectors") or set()
            if sector in blocked:
                log(f"{symbol} — Signal ignoré (secteur '{sector}' saturé GLOBAL cross-bots)", "INFO")
                log_signal("BUY_SKIP_SECTOR_GLOBAL", symbol, {"sector": sector, "price": current_price})
                return state

        # Filtre earnings stocks (Alpaca + xStocks legacy)
        if symbol in config.STOCKS:
            from data.fetcher import _xstock_ticker
            ticker = _xstock_ticker(symbol)
            if _has_earnings_soon(ticker):
                log(f"{symbol} — Signal ignoré (rapport trimestriel dans <24h)", "WARN")
                log_signal("BUY_SKIP_EARNINGS", symbol, {"ticker": ticker, "price": current_price})
                return state

        # Confirmation multi-timeframe (1d) — informatif pour Claude, non bloquant
        ok_1d, reason_1d = _confirm_daily_trend(symbol, ohlcv_daily=ohlcv_daily)
        log(f"{symbol} — MTF 1d: {'✓' if ok_1d else '⚠'} {reason_1d}", "INFO")
        log_signal("MTF_FILTER", symbol, {"ok_1d": ok_1d, "reason": reason_1d, "price": current_price})
        # Claude décide — pas de blocage hard sur le MTF

        log(
            f"{symbol} — Signal BUY | ADX: {adx:.1f} | Vol×{volume_ratio:.2f} | "
            f"RSI: {last['rsi']:.1f} — exécution autonome",
            "INFO",
        )

        effective_buy = current_price * (1 + config.SLIPPAGE)
        # Sizing baseline = total portfolio value (cash + positions MTM), pas cash seul.
        # Sinon les positions rétrécissent au fur et à mesure qu'on en ouvre (cash baisse).
        # Capital total stable → toutes les positions ont le même sizing théorique.
        positions_mtm = 0.0
        if ohlcv_daily:
            for _sym, _pos in state.get("positions", {}).items():
                _df = ohlcv_daily.get(_sym)
                if _df is not None and len(_df) > 0:
                    positions_mtm += float(_df["close"].iloc[-1]) * _pos.get("size", 0)
                else:
                    positions_mtm += _pos.get("entry", 0) * _pos.get("size", 0)
        else:
            positions_mtm = sum(p.get("entry", 0) * p.get("size", 0) for p in state.get("positions", {}).values())
        total_value = state["capital"] + positions_mtm
        # Floor en % du capital initial (capital-agnostic) plutôt qu'en € hardcodé
        init_cap_for_floor = state.get("original_capital", state.get("initial_capital", config.INITIAL_CAPITAL_PER_BOT))
        floor_eur = init_cap_for_floor * config.POSITION_MIN_PCT
        base_eur = max(floor_eur, total_value * config.POSITION_SIZE_PCT)
        # Dynamic sizing : réduire les positions en drawdown (protection anti-ruin)
        init_cap = state.get("original_capital", state.get("initial_capital", config.PAPER_CAPITAL))
        if init_cap > 0:
            dd_ratio = (state["capital"] - init_cap) / init_cap  # négatif en perte
            # Floor 0.70 : avant 0.30 = on étranglait à -12% DD (vu live: ×0.41-0.51).
            # Le freeze hard à -15% (config.MAX_DRAWDOWN) protège le capital, le scale
            # ne doit pas faire le même travail en doublon.
            dd_scale = max(0.70, 1.0 + dd_ratio * 2)
        else:
            dd_scale = 1.0
        position_eur = base_eur * vix_factor * vol_exposure * dd_scale
        size_factors = []
        if vix_factor != 1.0:
            size_factors.append(f"VIX ×{vix_factor:.2f}")
        if vol_exposure != 1.0:
            size_factors.append(f"vol ×{vol_exposure:.2f}")
        if dd_scale != 1.0:
            size_factors.append(f"DD ×{dd_scale:.2f}")
        if size_factors:
            log(
                f"{symbol} — Position {position_eur:.0f}€ ({config.POSITION_SIZE_PCT*100:.0f}% capital"
                f" × {' × '.join(size_factors)}"
                f"{f' | σ {realized_vol*100:.1f}%' if realized_vol > 0 else ''})",
                "WARN" if dd_scale < 0.8 or vix_factor < 1.0 else "INFO",
            )
        else:
            log(f"{symbol} — Position {position_eur:.0f}€ ({config.POSITION_SIZE_PCT*100:.0f}% du capital {state['capital']:.0f}€)", "INFO")
        pos = calculate_position_size(position_eur, effective_buy, atr)
        fee_entry = effective_buy * pos["size"] * config.EXCHANGE_FEE
        total_cost = pos["size"] * effective_buy + fee_entry

        if total_cost > state["capital"]:
            log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€)", "WARN")
            return state

        from live.order_executor import execute_buy as _exec_buy
        _order = _exec_buy(symbol, pos["size"], current_price)
        if not _order.success:
            log(f"⛔ BUY {symbol} échoué: {_order.error}", "WARN")
            return state
        effective_buy = _order.filled_price
        pos["size"] = _order.filled_size
        fee_entry = effective_buy * pos["size"] * config.EXCHANGE_FEE
        total_cost = pos["size"] * effective_buy + fee_entry

        state["capital"] -= total_cost

        # ── Stop-loss broker-side : protection même si bot down ──
        from live.order_executor import place_broker_stop
        stop_ids = place_broker_stop(symbol, pos["size"], pos["stop_loss"],
                                      take_profit=pos["take_profit"])

        position_data = {
            "entry": effective_buy,
            "size": pos["size"],
            "stop": pos["stop_loss"],
            "initial_stop": pos["stop_loss"],
            "tp": pos["take_profit"],
            "date": datetime.now(timezone.utc).isoformat(),
            "risk_eur": pos["risk_eur"],
            "fee_entry": round(fee_entry, 4),
            "alpaca_stop_id": stop_ids.get("stop_id"),
            "alpaca_tp_id":   stop_ids.get("tp_id"),
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
    init_cap = state.get("initial_capital", 0) or 0
    perf = ((total_value - init_cap) / init_cap * 100) if init_cap > 0 else 0.0

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
    init_cap_snap = state.get("initial_capital", 0) or 0
    pnl_pct = round((total_value - init_cap_snap) / init_cap_snap * 100, 2) if init_cap_snap > 0 else 0.0

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


def _check_max_drawdown(state: dict) -> bool:
    """Retourne True si le drawdown depuis le capital initial dépasse MAX_DRAWDOWN (-15%)."""
    total_value = state["capital"] + sum(
        p["entry"] * p["size"] for p in state["positions"].values()
    )
    init_cap_dd = state.get("initial_capital", 0) or 0
    drawdown = ((total_value - init_cap_dd) / init_cap_dd) if init_cap_dd > 0 else 0.0
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
    xstock_pnl  = [t["pnl"] for t in recent if t.get("symbol") in config.STOCKS]

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


def _compute_momentum_score(symbol: str) -> tuple:
    """
    Calcule le momentum 90j (retour sur 90 jours glissants) pour un symbole.
    Retourne (ok: bool, ret_90d: float).
    ok=False si ret_90d < 0 (downtrend sur 90j).
    Permissif (True) si erreur ou données insuffisantes.
    """
    try:
        df = fetch_ohlcv(symbol, "1d", days=95)
        if df is None or len(df) < 90:
            return True, 0.0
        price_now = float(df["close"].iloc[-1])
        price_90d = float(df["close"].iloc[-90])
        ret_90d = (price_now - price_90d) / price_90d
        return ret_90d >= 0, round(ret_90d, 4)
    except Exception:
        return True, 0.0


def _update_momentum_filter(state: dict) -> dict:
    """
    Calcule et met en cache le momentum 90j de tous les symboles.
    Tourne UNE FOIS PAR SEMAINE (clé 'momentum_filter_week' dans state).
    Retourne un dict {symbol: bool} (True = ok, False = exclu).
    """
    from datetime import date
    current_week = date.today().isocalendar()[:2]  # (year, week)
    cached_week = state.get("momentum_filter_week")
    if cached_week == list(current_week) and "momentum_filter" in state:
        return state["momentum_filter"]

    log("Calcul du filtre momentum 90j (hebdomadaire)...", "INFO")
    result = {}
    excluded = []
    for symbol in config.SYMBOLS:
        ok, ret = _compute_momentum_score(symbol)
        result[symbol] = ok
        if not ok:
            excluded.append(f"{symbol} ({ret*100:.1f}%)")
        # BUG-12 : sleep(1) supprimé — tourne 1x/semaine, pas de rate limiting nécessaire

    if excluded:
        msg = f"Momentum filter — Exclus (ret 90j < 0): {', '.join(excluded)}"
        log(msg, "WARN")
        notify(f"⚠️ <b>Momentum filter</b>\nExclus cette semaine:\n" + "\n".join(excluded))
    else:
        log("Momentum filter — Tous symboles positifs (ret 90j ≥ 0)", "INFO")

    state["momentum_filter"] = result
    state["momentum_filter_week"] = list(current_week)
    return result


def _confirm_daily_trend(symbol: str, ohlcv_daily: dict = None) -> tuple:
    """
    Confirmation multi-timeframe : vérifie que la tendance 1d valide le signal 4h.
    Retourne (ok: bool, raison: str). Permissif si données indisponibles.
    BUG-11 : utilise le cache ohlcv_daily de multi_runner si disponible (évite fetch réseau redondant).
    """
    try:
        df = (ohlcv_daily or {}).get(symbol) if ohlcv_daily else None
        if df is None:
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

    # Heures UTC des cycles : alignées sur 10h ET + 14h ET (US market) + crypto 24/7
    # 03:00 | 07:00 | 11:00 | 15:00(=10hET) | 19:00(=14hET) | 23:00
    CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]

    def _next_cycle_utc() -> datetime:
        """Retourne le prochain slot UTC du cycle (arrondi à l'heure exacte)."""
        now = datetime.utcnow()
        for h in CYCLE_HOURS_UTC:
            candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidate > now:
                return candidate
        # Prochain jour
        tomorrow = (now + timedelta(days=1)).replace(
            hour=CYCLE_HOURS_UTC[0], minute=0, second=0, microsecond=0
        )
        return tomorrow

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

            qqq_ok, qqq_desc = fetch_qqq_regime()
            log(
                f"Régime QQQ: {qqq_desc} {'✓ Risk-ON' if qqq_ok else '⚠ Risk-OFF — xStocks bloqués'}",
                "INFO" if qqq_ok else "WARN",
            )

            rotation = _compute_rotation_factors(state.get("trades", []))
            if rotation["crypto"] != 1.0:
                log(
                    f"Rotation capital: crypto ×{rotation['crypto']} | "
                    f"xStocks ×{rotation['xstock']} (perf relative 20 derniers trades)",
                    "INFO",
                )

            momentum_filter = _update_momentum_filter(state)

            for symbol in config.SYMBOLS:
                # Momentum filter : xStocks uniquement
                # Crypto = déjà protégé par EMA200 + Supertrend + BTC regime block
                is_crypto = symbol in config.CRYPTO
                if not is_crypto and not momentum_filter.get(symbol, True) and symbol not in state["positions"]:
                    log(f"{symbol} — Ignoré (momentum 90j négatif)", "INFO")
                    time.sleep(1)
                    continue

                category = "xstock" if symbol in config.STOCKS else "crypto"
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
                    qqq_regime_ok=qqq_ok,
                    qqq_description=qqq_desc,
                )
                time.sleep(3)  # Évite saturation RAM (1GB VPS) entre chaque symbole

            save_state(state)
            print_status(state)
            if _check_max_drawdown(state):
                save_state(state)
                break
            _check_daily_snapshot(state)

            next_run = _next_cycle_utc()
            wait_sec = max(0, (next_run - datetime.utcnow()).total_seconds())
            log(f"Prochaine analyse à {next_run.strftime('%H:%M UTC')} (dans {int(wait_sec // 60)} min)")
            time.sleep(wait_sec)

        except KeyboardInterrupt:
            log("Bot arrêté manuellement.")
            save_state(state)
            break
        except Exception as e:
            log(f"Erreur inattendue: {e}", "WARN")
            time.sleep(60)


if __name__ == "__main__":
    run()

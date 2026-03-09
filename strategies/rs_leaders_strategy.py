"""
Bot I: Relative Strength Leaders

Le complément le plus utile au stack actuel :
- Bot B : rotation momentum "broad" (score 1m/3m/6m, top 4, stop -12% fixe)
- Bot I : sélection plus fine des leaders relatifs avec filtres de qualité supplémentaires

Différences clés vs Bot B :
  - Score inclut la distance à SMA200 (favorise les leaders bien installés)
  - Filtre structure : SMA50 > SMA200 (golden cross daily)
  - Filtre volatilité : annual_vol < 90% (exclut les spikes purs)
  - Filtre extension : prix pas > 15% au-dessus de SMA50 (évite d'acheter les paraboliques)
  - Filtre qualité : ADX > 18
  - Stop : 2.5×ATR trailing (vs -12% fixe)
  - Sizing : volatility targeting (vs égal 25%)
  - Seuil de sortie : top 5 (entre si top 3, sort si hors top 5 → réduit le churn)

Score = 0.35×(1m) + 0.35×(3m) + 0.20×(6m) + 0.10×(distance SMA200)

Capital : 1000€ | Top 3 positions | Rebalancement 5+ jours
Performance de référence MSCI World Quality + Momentum : 12-16% CAGR
"""
import json
import math
import os
import sys
from datetime import datetime, date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import compute_atr, compute_adx

STATE_FILE = "logs/rs_leaders/state.json"
INITIAL_CAPITAL = 1000.0

# Score composite
W_1M = 0.35
W_3M = 0.35
W_6M = 0.20
W_SMA200_DIST = 0.10

# Paramètres de sélection
TOP_N = 3          # Nombre de positions à tenir
EXIT_RANK = 5      # Sortie si l'actif tombe hors top 5 (buffer contre le churn)
REBALANCE_MIN_DAYS = 5

# Filtres de qualité
ADX_MIN = 18
VOL_MAX = 0.90        # Exclut les actifs avec vol annualisée > 90%
EXTENSION_MAX = 0.15  # Prix ne doit pas être > 15% au-dessus de SMA50
HARD_STOP_PCT = 0.10  # Stop dur -10% depuis l'entrée

# Stop trailing et sizing
ATR_TRAIL = 2.5
ATR_PERIOD = 14
TARGET_VOL = 0.15    # Volatilité cible 15%
MAX_POS_PCT = 0.30   # Cap 30% du capital par position


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},
        "trades": [],
        "initial_capital": INITIAL_CAPITAL,
        "last_rebalance_date": None,
        "ranked_symbols": [],
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [BOT-I][{level}] {msg}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/rs_leaders.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


def _portfolio_value(state: dict, daily_cache: dict) -> float:
    total = state["capital"]
    for symbol, pos in state["positions"].items():
        df = daily_cache.get(symbol)
        price = float(df["close"].iloc[-1]) if df is not None else pos.get("entry", 0)
        total += price * pos["size"]
    return total


def _needs_rebalance(state: dict) -> bool:
    last = state.get("last_rebalance_date")
    if last is None:
        return True
    return (date.today() - date.fromisoformat(last)).days >= REBALANCE_MIN_DAYS


def _compute_rs_score(symbol: str, daily_cache: dict) -> tuple[float, dict]:
    """
    Calcule le score RS + vérifie les filtres de qualité.
    Retourne (score, indicators_dict) ou (nan, {}) si données insuffisantes.
    """
    df = daily_cache.get(symbol)
    if df is None or len(df) < 210:
        return float("nan"), {}

    close = df["close"]
    high = df["high"]
    low = df["low"]
    price_now = float(close.iloc[-1])

    # Lookbacks
    n1m = min(22, len(close) - 1)
    n3m = min(66, len(close) - 1)
    n6m = min(130, len(close) - 1)
    p1m = float(close.iloc[-n1m])
    p3m = float(close.iloc[-n3m])
    p6m = float(close.iloc[-n6m])
    if p1m <= 0 or p3m <= 0 or p6m <= 0:
        return float("nan"), {}

    r1m = (price_now - p1m) / p1m
    r3m = (price_now - p3m) / p3m
    r6m = (price_now - p6m) / p6m

    # SMA200 + SMA50
    sma200 = float(close.rolling(200).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    dist_sma200 = (price_now - sma200) / sma200 if sma200 > 0 else 0

    # RS Score
    rs_score = W_1M * r1m + W_3M * r3m + W_6M * r6m + W_SMA200_DIST * dist_sma200

    # Volatilité annualisée (20j)
    daily_vol = float(close.pct_change().rolling(20).std().iloc[-1])
    annual_vol = daily_vol * math.sqrt(252) if daily_vol == daily_vol else 1.0

    # ATR + ADX
    atr_series = compute_atr(high, low, close, ATR_PERIOD)
    atr = float(atr_series.iloc[-1])
    adx = float(compute_adx(high, low, close, ATR_PERIOD).iloc[-1])

    # Extension au-dessus de SMA50
    extension = (price_now - sma50) / sma50 if sma50 > 0 else 0

    indicators = {
        "price": price_now,
        "sma200": round(sma200, 4),
        "sma50": round(sma50, 4),
        "atr": round(atr, 4),
        "adx": round(adx, 1),
        "annual_vol": round(annual_vol, 3),
        "extension": round(extension, 3),
        "r1m": round(r1m, 4),
        "r3m": round(r3m, 4),
        "r6m": round(r6m, 4),
        "dist_sma200": round(dist_sma200, 3),
    }
    return rs_score, indicators


def _passes_filters(ind: dict) -> tuple[bool, str]:
    """Retourne (ok, raison_rejet). Tous les filtres doivent passer pour entrer."""
    p = ind["price"]
    sma200 = ind["sma200"]
    sma50 = ind["sma50"]

    # 1. Structure : triple alignment haussier
    if p < sma200:
        return False, "price < SMA200"
    if sma50 < sma200:
        return False, "SMA50 < SMA200 (pas de golden cross)"
    if p < sma50:
        return False, "price < SMA50"

    # 2. Qualité de tendance
    if ind["adx"] < ADX_MIN:
        return False, f"ADX {ind['adx']:.1f} < {ADX_MIN}"

    # 3. Volatilité : exclut les spikes purs
    if ind["annual_vol"] > VOL_MAX:
        return False, f"vol {ind['annual_vol']*100:.0f}% > {VOL_MAX*100:.0f}%"

    # 4. Extension : évite d'acheter un parabolique
    if ind["extension"] > EXTENSION_MAX:
        return False, f"extension {ind['extension']*100:.1f}% > {EXTENSION_MAX*100:.0f}%"

    return True, ""


def _vol_target_size(capital: float, annual_vol: float, entry_price: float) -> float:
    if annual_vol <= 0 or entry_price <= 0:
        return 0.0
    size_pct = min(TARGET_VOL / annual_vol, MAX_POS_PCT)
    return (capital * size_pct) / (entry_price * (1 + config.EXCHANGE_FEE))


def run_rs_leaders_cycle(state: dict, daily_cache: dict, macro_context: dict = None) -> dict:
    """Run one cycle of the Relative Strength Leaders strategy."""
    macro = macro_context or {}
    vix = macro.get("vix", 0.0)
    qqq_ok = macro.get("qqq_regime_ok", True)
    btc_bear = macro.get("btc_context", {}).get("btc_trend", "bull") == "bear"
    engine = macro.get("bot_z_engine", "BALANCED")  # filtre régime Bot Z

    # ── 0. Hard stop + trailing stop — vérifiés à CHAQUE cycle ──────────────
    for symbol in list(state["positions"].keys()):
        pos = state["positions"][symbol]
        df = daily_cache.get(symbol)
        if df is None:
            continue
        current_price = float(df["close"].iloc[-1])

        # Trailing stop update
        atr_s = compute_atr(df["high"], df["low"], df["close"], ATR_PERIOD)
        atr = float(atr_s.iloc[-1])
        new_stop = round(current_price - ATR_TRAIL * atr, 4)
        if new_stop > pos.get("stop", 0):
            pos["stop"] = new_stop
            state["positions"][symbol] = pos

        # Exit : ATR stop
        stop = pos.get("stop", 0)
        exit_reason = None
        exit_price = current_price
        low_price = float(df["low"].iloc[-1])  # Bot I : stop sur low comme A/C/G/H (stops intraday daily)
        if stop > 0 and low_price <= stop:
            exit_reason = "trailing_stop"
            exit_price = stop
        # Exit : hard stop -10%
        elif pos.get("entry", 0) > 0:
            loss = (current_price - pos["entry"]) / pos["entry"]
            if loss <= -HARD_STOP_PCT:
                exit_reason = f"hard_stop_{HARD_STOP_PCT*100:.0f}pct"
        # Exit : SMA50 cassé
        elif df is not None and len(df) >= 50:
            sma50 = float(df["close"].rolling(50).mean().iloc[-1])
            if current_price < sma50:
                exit_reason = "sma50_break"

        if exit_reason:
            if not config.PAPER_TRADING:
                from live.order_executor import execute_sell as _exec_sell
                _order = _exec_sell(symbol, pos["size"], exit_price, reason=exit_reason)
                if not _order.success:
                    log(f"⛔ SELL {symbol} échoué en live: {_order.error} — position maintenue", "WARN")
                    continue
                exit_eff = _order.filled_price * (1 - config.EXCHANGE_FEE)
            else:
                exit_eff = exit_price * (1 - config.SLIPPAGE)
            fee = exit_eff * pos["size"] * config.EXCHANGE_FEE
            proceeds = exit_eff * pos["size"] - fee
            pnl = proceeds - pos["cost"]
            state["capital"] += proceeds
            state["trades"].append({
                "symbol": symbol,
                "entry_date": pos["date"],
                "exit_date": str(datetime.now()),
                "entry_price": pos["entry"],
                "exit_price": round(exit_eff, 4),
                "pnl": round(pnl, 2),
                "reason": exit_reason,
                "result": "win" if pnl > 0 else "loss",
            })
            state["positions"].pop(symbol)
            log(
                f"{'✓' if pnl > 0 else '✗'} CLOSE {symbol} | "
                f"{pos['entry']:.4f}€ → {exit_eff:.4f}€ | PnL: {pnl:+.2f}€ | {exit_reason}",
                "BUY" if pnl > 0 else "SELL",
            )

    # ── 1. Score et ranking ───────────────────────────────────────────────────
    scores = {}
    indicators_map = {}
    for symbol in config.SYMBOLS:
        score, ind = _compute_rs_score(symbol, daily_cache)
        if score == score:  # not NaN
            scores[symbol] = score
            indicators_map[symbol] = ind

    ranked_all = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    ranked_symbols = [s for s, _ in ranked_all]
    state["ranked_symbols"] = ranked_symbols[:10]  # store top 10 for dashboard

    # Leaders qualifiés (filtres passés + RS positif)
    qualified = [
        (s, sc) for s, sc in ranked_all
        if sc > 0 and _passes_filters(indicators_map.get(s, {}))[0]
    ]
    top_symbols = [s for s, _ in qualified[:TOP_N]]

    log(
        f"Ranking ({len(scores)} actifs) | "
        f"Qualifiés: {len(qualified)} | "
        f"Top {TOP_N}: " + ", ".join(
            f"{s} {sc:.1%}" for s, sc in qualified[:TOP_N]
        )
    )

    # ── 2. Filtre macro — suspend le rebalancement si risque élevé ───────────
    if engine in ("SHIELD", "PRO"):
        log(f"Engine={engine} — rebalancement suspendu (régime défensif Bot Z)", "WARN")
        return state
    if vix > 30:
        log(f"VIX={vix:.1f} > 30 — rebalancement suspendu", "WARN")
        return state
    if not qqq_ok:
        log(f"QQQ bearish — rebalancement suspendu", "WARN")
        return state

    # ── 3. Sortir les positions qui sont hors top EXIT_RANK ──────────────────
    for symbol in list(state["positions"].keys()):
        rank = ranked_symbols.index(symbol) + 1 if symbol in ranked_symbols else 999
        ind = indicators_map.get(symbol, {})
        if not ind:  # BUG-28 : données temporairement indisponibles → skip exit sur ce symbole
            continue
        passes, _ = _passes_filters(ind)
        if rank > EXIT_RANK or not passes:
            pos = state["positions"][symbol]
            df = daily_cache.get(symbol)
            if df is None:
                continue
            _raw_exit = float(df["close"].iloc[-1])
            if not config.PAPER_TRADING:
                from live.order_executor import execute_sell as _exec_sell
                _order = _exec_sell(symbol, pos["size"], _raw_exit, reason=f"rs_exit_rank{rank}")
                if not _order.success:
                    log(f"⛔ SELL {symbol} échoué en live: {_order.error} — position maintenue", "WARN")
                    continue
                exit_price = _order.filled_price * (1 - config.EXCHANGE_FEE)
            else:
                exit_price = _raw_exit * (1 - config.SLIPPAGE)
            fee = exit_price * pos["size"] * config.EXCHANGE_FEE
            proceeds = exit_price * pos["size"] - fee
            pnl = proceeds - pos["cost"]
            state["capital"] += proceeds
            state["trades"].append({
                "symbol": symbol,
                "entry_date": pos["date"],
                "exit_date": str(datetime.now()),
                "entry_price": pos["entry"],
                "exit_price": round(exit_price, 4),
                "pnl": round(pnl, 2),
                "reason": f"rs_exit_rank{rank}",
                "result": "win" if pnl > 0 else "loss",
            })
            state["positions"].pop(symbol)
            log(
                f"{'✓' if pnl > 0 else '✗'} ROTATE OUT {symbol} | rang #{rank} | "
                f"PnL: {pnl:+.2f}€",
                "BUY" if pnl > 0 else "SELL",
            )

    # ── 4. Rebalancement hebdomadaire ─────────────────────────────────────────
    if not _needs_rebalance(state):
        log(f"Pas de rebalancement (dernier: {state.get('last_rebalance_date')}). "
            f"Positions: {list(state['positions'].keys())}")
        return state

    log("Rebalancement hebdomadaire déclenché")

    # ── 5. Entrer sur les leaders non déjà détenus ───────────────────────────
    portfolio_val = _portfolio_value(state, daily_cache)
    to_buy = [s for s in top_symbols if s not in state["positions"]]

    for symbol in to_buy:
        df = daily_cache.get(symbol)
        if df is None:
            continue
        ind = indicators_map.get(symbol, {})
        entry_price = float(df["close"].iloc[-1]) * (1 + config.SLIPPAGE)
        annual_vol = ind.get("annual_vol", 0.5)
        # PARITY : réduire l'exposition de 30% via vol cible plus conservatrice
        vol_target = TARGET_VOL * (0.70 if engine == "PARITY" else 1.0)
        size = _vol_target_size(state["capital"], annual_vol, entry_price)
        size *= (vol_target / TARGET_VOL)

        if size <= 0:
            log(f"{symbol} — Size=0", "WARN")
            continue

        fee = entry_price * size * config.EXCHANGE_FEE
        total_cost = size * entry_price + fee
        if total_cost > state["capital"] or size <= 0:
            log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€)", "WARN")
            continue

        if not config.PAPER_TRADING:
            from live.order_executor import execute_buy as _exec_buy
            _order = _exec_buy(symbol, size, entry_price)
            if not _order.success:
                log(f"⛔ BUY {symbol} échoué en live: {_order.error}", "WARN")
                continue
            entry_price = _order.filled_price
            size = _order.filled_size
            fee = entry_price * size * config.EXCHANGE_FEE
            total_cost = size * entry_price + fee

        atr_s = compute_atr(df["high"], df["low"], df["close"], ATR_PERIOD)
        atr = float(atr_s.iloc[-1])
        stop_loss = round(entry_price - ATR_TRAIL * atr, 4)

        state["capital"] -= total_cost
        state["positions"][symbol] = {
            "entry": round(entry_price, 4),
            "size": round(size, 6),
            "cost": round(total_cost, 4),
            "stop": stop_loss,
            "date": str(datetime.now()),
            "atr": round(atr, 4),
            "rs_score": round(scores.get(symbol, 0), 4),
            "vol_pct": round(annual_vol * 100, 1),
        }
        log(
            f"▲ BUY {symbol} | {entry_price:.4f}€ | {size:.6f} units | "
            f"SL: {stop_loss:.4f}€ | RS: {scores.get(symbol, 0):.1%} | "
            f"vol: {annual_vol*100:.0f}% | ext: {ind.get('extension', 0)*100:.1f}%",
            "BUY",
        )

    state["last_rebalance_date"] = str(date.today())

    total = _portfolio_value(state, daily_cache)
    perf = (total - state["initial_capital"]) / state["initial_capital"] * 100
    log(
        f"Rebalancement terminé | Positions: {list(state['positions'].keys())} | "
        f"Capital libre: {state['capital']:.2f}€ | Perf: {perf:+.2f}%"
    )
    return state

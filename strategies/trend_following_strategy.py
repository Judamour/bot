"""
Bot G: Trend Following Multi-Asset — Volatility Targeting
Inspired by CTA fund methodology (AQR, Man Group, SG CTA Index).

Vs Bot C (Breakout) :
  - Bot C : Donchian 55j, 3 crypto seulement, Turtle sizing 1% fixe
  - Bot G : SMA200+SMA50+breakout 50j, 16 actifs (crypto+stocks), vol targeting

Entry  : price > SMA200 AND price > SMA50 AND breakout 50j AND ADX > 20 (daily)
Exit   : price < SMA200 (trend cassé) OR 3×ATR trailing stop
Sizing : Volatility targeting — size = capital × (TARGET_VOL / annual_vol)
         capped at MAX_POSITION_PCT (10%) per position
Universe: config.SYMBOLS — tous les 16 actifs (crypto + xStocks)
Timeframe: daily

Performance CTA historique : 10-18% CAGR — régulier sur cycles longs.
"""
import json
import math
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import compute_atr, compute_adx
from live.notifier import notify

STATE_FILE = "logs/trend/state.json"
INITIAL_CAPITAL = 1000.0

# Indicateurs
SMA_LONG = 200
SMA_SHORT = 50
BREAKOUT_PERIOD = 50
ADX_MIN = 20
ATR_PERIOD = 20
STOP_ATR_MULT = 3.0

# Position sizing — volatility targeting
TARGET_VOL = 0.15       # Volatilité annualisée cible : 15%
MAX_POSITION_PCT = 0.10 # Cap : 10% du capital par position
MAX_POSITIONS = 8       # Max positions simultanées

# Filtre macro
VIX_NO_ENTRY = 35       # Suspend les nouvelles entrées si VIX > 35


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},
        "trades": [],
        "initial_capital": INITIAL_CAPITAL,
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [BOT-G][{level}] {msg}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/trend.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


def _add_indicators(df):
    """Ajoute SMA200, SMA50, breakout_high(50j), ATR(20), ADX(14), daily_vol(20j)."""
    df = df.copy()
    df["sma200"] = df["close"].rolling(SMA_LONG).mean()
    df["sma50"] = df["close"].rolling(SMA_SHORT).mean()
    # Shift(1) = high de hier — évite le look-ahead bias
    df["breakout_high"] = df["high"].rolling(BREAKOUT_PERIOD).max().shift(1)
    df["atr"] = compute_atr(df["high"], df["low"], df["close"], ATR_PERIOD)
    df["adx"] = compute_adx(df["high"], df["low"], df["close"], 14)
    df["daily_vol"] = df["close"].pct_change().rolling(20).std()
    return df.dropna()


def _vol_target_size(capital: float, annual_vol: float, entry_price: float) -> float:
    """
    Volatility targeting : size = capital × min(TARGET_VOL / annual_vol, MAX_POSITION_PCT)
    Retourne 0 si donnée insuffisante.
    """
    if annual_vol <= 0 or entry_price <= 0:
        return 0.0
    size_pct = min(TARGET_VOL / annual_vol, MAX_POSITION_PCT)
    dollar = capital * size_pct
    return dollar / (entry_price * (1 + config.EXCHANGE_FEE))


def run_trend_cycle(state: dict, daily_cache: dict, macro_context: dict = None) -> dict:
    """Run one cycle of the multi-asset trend following strategy."""
    vix = macro_context.get("vix", 0.0) if macro_context else 0.0
    no_new_entries = vix > VIX_NO_ENTRY
    if no_new_entries:
        log(f"VIX={vix:.1f} > {VIX_NO_ENTRY} — nouvelles entrées suspendues", "WARN")

    for symbol in config.SYMBOLS:
        df = daily_cache.get(symbol)
        if df is None or len(df) < SMA_LONG + 30:
            log(f"{symbol} — Données insuffisantes ({len(df) if df is not None else 0} barres)", "WARN")
            continue

        df_ind = _add_indicators(df)
        if df_ind.empty:
            continue

        last = df_ind.iloc[-1]
        current_price = float(last["close"])
        sma200 = float(last["sma200"])
        sma50 = float(last["sma50"])
        breakout_high = float(last["breakout_high"])
        atr = float(last["atr"])
        adx = float(last["adx"])
        daily_vol = float(last["daily_vol"])
        annual_vol = daily_vol * math.sqrt(252)

        position = state["positions"].get(symbol)

        # ── Trailing stop update ──
        if position:
            new_stop = round(current_price - STOP_ATR_MULT * atr, 4)
            if new_stop > position["stop"]:
                position["stop"] = new_stop
                state["positions"][symbol] = position
                log(f"{symbol} — Trailing stop → {new_stop:.4f}€")

        # ── Exit checks ──
        if position:
            exit_reason = None
            exit_price = current_price

            if current_price <= position["stop"]:
                exit_reason = "atr_stop"
                exit_price = position["stop"]
            elif current_price < sma200:
                exit_reason = "sma200_break"

            if exit_reason:
                exit_eff = exit_price * (1 - config.SLIPPAGE)
                fee = exit_eff * position["size"] * config.EXCHANGE_FEE
                proceeds = exit_eff * position["size"] - fee
                pnl = proceeds - position["cost"]
                state["capital"] += proceeds
                state["trades"].append({
                    "symbol": symbol,
                    "entry_date": position["date"],
                    "exit_date": str(datetime.now()),
                    "entry_price": position["entry"],
                    "exit_price": round(exit_eff, 4),
                    "pnl": round(pnl, 2),
                    "reason": exit_reason,
                    "result": "win" if pnl > 0 else "loss",
                })
                state["positions"].pop(symbol)
                log(
                    f"{'✓' if pnl > 0 else '✗'} CLOSE {symbol} | "
                    f"{position['entry']:.4f}€ → {exit_eff:.4f}€ | "
                    f"PnL: {pnl:+.2f}€ | {exit_reason}",
                    "BUY" if pnl > 0 else "SELL",
                )
                notify(
                    f"{'✅' if pnl > 0 else '🔴'} <b>Bot G — Trend</b>\n"
                    f"{'✓' if pnl > 0 else '✗'} <b>{symbol}</b> {exit_reason.upper()}\n"
                    f"{position['entry']:.4f}€ → {exit_eff:.4f}€\n"
                    f"PnL : <b>{pnl:+.2f}€</b>"
                )
                continue

        # ── Entry checks (pas de position sur ce symbole) ──
        if symbol not in state["positions"]:
            if no_new_entries:
                continue
            if len(state["positions"]) >= MAX_POSITIONS:
                continue

            trend_ok = current_price > sma200 and current_price > sma50
            breakout = current_price > breakout_high if breakout_high > 0 else False
            adx_ok = adx > ADX_MIN

            if trend_ok and breakout and adx_ok:
                entry_price = current_price * (1 + config.SLIPPAGE)
                size = _vol_target_size(state["capital"], annual_vol, entry_price)

                if size <= 0:
                    log(f"{symbol} — Size=0 (vol={annual_vol*100:.0f}% trop haute ou capital insuffisant)", "WARN")
                    continue

                stop_loss = round(entry_price - STOP_ATR_MULT * atr, 4)
                fee = entry_price * size * config.EXCHANGE_FEE
                total_cost = size * entry_price + fee

                if total_cost > state["capital"]:
                    log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€ < {total_cost:.2f}€)", "WARN")
                    continue

                state["capital"] -= total_cost
                state["positions"][symbol] = {
                    "entry": round(entry_price, 4),
                    "size": round(size, 6),
                    "cost": round(total_cost, 4),
                    "stop": stop_loss,
                    "date": str(datetime.now()),
                    "atr": round(atr, 4),
                    "vol_pct": round(annual_vol * 100, 1),
                }
                log(
                    f"▲ BUY {symbol} | {entry_price:.4f}€ | {size:.6f} units | "
                    f"SL: {stop_loss:.4f}€ (3×ATR) | vol_ann: {annual_vol*100:.1f}% | ADX: {adx:.1f}",
                    "BUY",
                )
                notify(
                    f"📈 <b>Bot G — Trend</b>\n"
                    f"▲ <b>{symbol}</b> BUY — SMA200+Breakout50j\n"
                    f"Prix : {entry_price:.4f}€ | Stop : {stop_loss:.4f}€\n"
                    f"Investi : {total_cost:.2f}€ | Vol : {annual_vol*100:.1f}%"
                )
            else:
                # Log seulement si au moins un filtre passe (évite le spam)
                if trend_ok or breakout:
                    log(
                        f"{symbol} | {current_price:.4f}€ | "
                        f"Trend: {'✓' if trend_ok else '✗'} | "
                        f"Break50j: {'✓' if breakout else '✗'} ({breakout_high:.4f}€) | "
                        f"ADX: {adx:.1f} {'✓' if adx_ok else '✗'}"
                    )

    total = state["capital"] + sum(
        float(daily_cache[s]["close"].iloc[-1]) * p["size"]
        for s, p in state["positions"].items() if s in daily_cache
    )
    perf = (total - state["initial_capital"]) / state["initial_capital"] * 100
    log(
        f"Cycle done | Positions: {list(state['positions'].keys())} | "
        f"Capital libre: {state['capital']:.2f}€ | Perf: {perf:+.2f}%"
    )
    return state

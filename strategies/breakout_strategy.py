"""
Bot C: Donchian Channel Breakout — Turtle System 2
Based on Richard Dennis' Turtle Trading rules (1983).

Entry  : close > Donchian_upper(55) on daily data + ADX > 20
Exit   : close < Donchian_lower(20) on daily data
Stop   : 2 × ATR(20) below entry price (Turtle N-stop)
Sizing : 1% risk per trade (Turtle unit sizing), capped at 33% per position
Universe: BTC/EUR, ETH/EUR, SOL/EUR (crypto — best terrain for Donchian trends)

Win rate ~35-45%, but big winners offset losers (profit factor 1.5-2.5).
Expected performance: 15-20% CAGR (SG CTA Index reference)
"""
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import compute_atr, compute_adx
from live.notifier import notify

STATE_FILE = "logs/breakout/state.json"
INITIAL_CAPITAL = 1000.0

# Assets — crypto only (strongest Donchian trends)
BREAKOUT_SYMBOLS = [s for s in config.CRYPTO if s in ("BTC/EUR", "ETH/EUR", "SOL/EUR")]

# System 2 parameters (Turtle original)
ENTRY_PERIOD = 55     # Donchian entry channel
EXIT_PERIOD = 20      # Donchian exit channel (shorter = let winners run)
ATR_PERIOD = 20       # ATR period (Dennis used 20-day)
STOP_ATR_MULT = 2.0   # Stop = 2N below entry (Turtle rule)
ADX_MIN = 20          # Modern filter: only enter in trending market

# Position sizing
RISK_PCT = 0.01           # 1% of capital per trade (Turtle unit sizing)
MAX_POSITION_PCT = 0.33   # Max 33% of capital per position
VIX_SCALE_START = 20      # En dessous : risk_pct plein
VIX_SCALE_END = 40        # Au dessus : risk_pct réduit à 50%


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},        # {symbol: {entry, size, cost, stop, date, atr}}
        "trades": [],
        "initial_capital": INITIAL_CAPITAL,
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [BOT-C][{level}] {msg}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/breakout.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


def add_donchian_indicators(df):
    """
    Add Donchian channel bands, ATR(20), and ADX to daily DataFrame.
    Uses shift(1) to avoid look-ahead bias.
    """
    df = df.copy()
    # Entry channel: 55-day high (shift 1 = yesterday's max, avoiding look-ahead)
    df["dc_upper"] = df["high"].rolling(window=ENTRY_PERIOD).max().shift(1)
    # Exit channel: 20-day low (close below this = exit long)
    df["dc_lower_exit"] = df["low"].rolling(window=EXIT_PERIOD).min().shift(1)
    # ATR(20) = Dennis' "N" — 1 unit of volatility
    df["atr_N"] = compute_atr(df["high"], df["low"], df["close"], ATR_PERIOD)
    # ADX for trend filter
    df["adx"] = compute_adx(df["high"], df["low"], df["close"], 14)
    return df.dropna()


def _turtle_unit_size(capital: float, atr: float, entry_price: float,
                      risk_pct: float = RISK_PCT) -> float:
    """
    Turtle position sizing: size such that 1N move = risk_pct of capital.
    dollar_per_N = risk_pct × capital
    size = dollar_per_N / atr
    """
    if atr <= 0 or entry_price <= 0:
        return 0.0
    dollar_per_N = risk_pct * capital
    size = dollar_per_N / atr
    # Cap to MAX_POSITION_PCT of capital
    max_size = (capital * MAX_POSITION_PCT) / entry_price
    return min(size, max_size)


def run_breakout_cycle(state: dict, daily_cache: dict, macro_context: dict = None) -> dict:
    """
    Run one cycle of the Donchian breakout strategy.
    Checks each BREAKOUT_SYMBOL for exit and entry conditions.
    """
    # ── Ajustement du risque selon le VIX ──
    vix = macro_context.get("vix", 0.0) if macro_context else 0.0
    if vix <= VIX_SCALE_START:
        vix_risk_factor = 1.0
    elif vix >= VIX_SCALE_END:
        vix_risk_factor = 0.5
    else:
        vix_risk_factor = 1.0 - 0.5 * (vix - VIX_SCALE_START) / (VIX_SCALE_END - VIX_SCALE_START)
    effective_risk_pct = round(RISK_PCT * vix_risk_factor, 4)
    if vix > VIX_SCALE_START:
        log(f"VIX={vix:.1f} → risk_pct réduit à {effective_risk_pct:.3%} (facteur {vix_risk_factor:.2f})", "WARN")

    for symbol in BREAKOUT_SYMBOLS:
        df = daily_cache.get(symbol)
        if df is None or len(df) < ENTRY_PERIOD + 10:
            log(f"{symbol} — Insufficient data ({len(df) if df is not None else 0} bars), skipping", "WARN")
            continue

        df_ind = add_donchian_indicators(df)
        if df_ind.empty:
            continue

        last = df_ind.iloc[-1]
        current_price = float(last["close"])
        atr = float(last["atr_N"])
        adx = float(last["adx"])
        dc_upper = float(last["dc_upper"])
        dc_lower_exit = float(last["dc_lower_exit"])

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
                exit_reason = "stop_loss"
                exit_price = position["stop"]
            elif current_price < dc_lower_exit:
                exit_reason = "donchian_exit"

            if exit_reason:
                exit_price_eff = exit_price * (1 - config.SLIPPAGE)
                fee_exit = exit_price_eff * position["size"] * config.EXCHANGE_FEE
                proceeds = exit_price_eff * position["size"] - fee_exit
                pnl = proceeds - position["cost"]
                state["capital"] += proceeds

                state["trades"].append({
                    "symbol": symbol,
                    "entry_date": position["date"],
                    "exit_date": str(datetime.now()),
                    "entry_price": position["entry"],
                    "exit_price": exit_price_eff,
                    "pnl": round(pnl, 2),
                    "reason": exit_reason,
                    "result": "win" if pnl > 0 else "loss",
                })
                state["positions"].pop(symbol)
                log(
                    f"{'✓' if pnl > 0 else '✗'} CLOSE {symbol} | "
                    f"{position['entry']:.4f}€ → {exit_price_eff:.4f}€ | "
                    f"PnL: {pnl:+.2f}€ | {exit_reason}",
                    "BUY" if pnl > 0 else "SELL",
                )
                notify(
                    f"{'✅' if pnl > 0 else '🔴'} <b>Bot C — Breakout</b>\n"
                    f"{'✓' if pnl > 0 else '✗'} <b>{symbol}</b> {exit_reason.upper()}\n"
                    f"{position['entry']:.4f}€ → {exit_price_eff:.4f}€\n"
                    f"PnL : <b>{pnl:+.2f}€</b>"
                )
                continue  # Don't check entry after closing

        # ── Entry checks (no open position for this symbol) ──
        if symbol not in state["positions"]:
            breakout = current_price > dc_upper if dc_upper > 0 else False
            adx_ok = adx > ADX_MIN

            if breakout and adx_ok:
                entry_price = current_price * (1 + config.SLIPPAGE)
                stop_loss = entry_price - STOP_ATR_MULT * atr
                size = _turtle_unit_size(state["capital"], atr, entry_price, effective_risk_pct)

                if size <= 0:
                    log(f"{symbol} — Size=0 (ATR too large or capital too small)", "WARN")
                    continue

                fee_entry = entry_price * size * config.EXCHANGE_FEE
                total_cost = size * entry_price + fee_entry

                if total_cost > state["capital"]:
                    log(f"{symbol} — Insufficient capital ({state['capital']:.2f}€ < {total_cost:.2f}€)", "WARN")
                    continue

                state["capital"] -= total_cost
                state["positions"][symbol] = {
                    "entry": round(entry_price, 4),
                    "size": round(size, 6),
                    "cost": round(total_cost, 4),
                    "stop": round(stop_loss, 4),
                    "date": str(datetime.now()),
                    "atr": round(atr, 4),
                }
                log(
                    f"▲ BUY {symbol} | {entry_price:.4f}€ | {size:.4f} unités | "
                    f"SL: {stop_loss:.4f}€ | ADX: {adx:.1f} | "
                    f"Breakout: >{dc_upper:.4f}€ | N={atr:.4f}",
                    "BUY",
                )
                notify(
                    f"📈 <b>Bot C — Breakout</b>\n"
                    f"▲ <b>{symbol}</b> BUY — Donchian 55j\n"
                    f"Prix : {entry_price:.4f}€ | Stop : {stop_loss:.4f}€\n"
                    f"Investi : {total_cost:.2f}€ | ADX : {adx:.1f}"
                )
            else:
                log(
                    f"{symbol} | Prix: {current_price:.4f}€ | "
                    f"DC55: {dc_upper:.4f}€ | ADX: {adx:.1f} | "
                    f"Breakout: {'✓' if breakout else '✗'} | ADX: {'✓' if adx_ok else '✗'}"
                )

    return state

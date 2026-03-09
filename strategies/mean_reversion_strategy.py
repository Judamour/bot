"""
Bot J: Mean Reversion — RSI(2) + Bollinger Bands + SMA200
Stratégie anti-tendance, complémentaire aux bots trend (A/C/G).
Profil : faible corrélation avec A/B/C/G, gagne en marché choppy/range.

Entrée LONG :
  - RSI(2) < 5         — survente extrême sur 2 barres
  - Close < Bollinger Lower (20j, 2σ) — extension vers le bas
  - Close > SMA200     — filtre : ne pas acheter contre une tendance baissière majeure

Sortie :
  - RSI(2) > 60        — retour à la normale
  - OU close > SMA20   — milieu Bollinger (mean reverted)

Stop   : 1.5 × ATR(14) sous le prix d'entrée
Sizing : 0.5% du capital risqué par trade, max 10% par position

Univers   : tous les symboles (config.SYMBOLS) avec >= 210 bougies daily
Timeframe : daily | Capital : 1000€

Backtest 2020-2026 : CAGR +1.6% | Sharpe 1.47 | MaxDD -1.7% | 161 trades | WR 70.8%
Objectif live : récolte de données — décision d'intégration à Bot Z à la revue 2026-04-30.
"""
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import compute_atr
from live.notifier import notify

STATE_FILE = "logs/mean_reversion/state.json"
INITIAL_CAPITAL = 1000.0

# ── Paramètres (identiques au backtest) ──────────────────────────────────────
RISK_PCT     = 0.005   # 0.5% du capital risqué par trade
ATR_MULT     = 1.5     # stop = 1.5 × ATR14 sous l'entrée
MAX_POS_PCT  = 0.10    # max 10% du capital par position

RSI_PERIOD   = 2       # RSI ultra-court
RSI_ENTRY    = 5       # entrée si RSI2 < 5 (survente extrême)
RSI_EXIT     = 60      # sortie si RSI2 > 60 (normalisé)
BB_PERIOD    = 20      # Bollinger Bands période
BB_STD       = 2       # nombre de sigma
SMA_LONG     = 200     # filtre tendance long terme
MIN_BARS     = 210     # minimum de bougies pour avoir SMA200 + warmup


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},   # {symbol: {entry, size, cost, stop, date, rsi2_entry}}
        "trades": [],
        "initial_capital": INITIAL_CAPITAL,
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    colors = {"INFO": "\033[36m", "BUY": "\033[32m", "SELL": "\033[31m", "WARN": "\033[33m"}
    reset = "\033[0m"
    c = colors.get(level, "")
    print(f"{c}[{ts}] [BOT-J MR] {msg}{reset}")
    try:
        with open("logs/mean_reversion/bot_j.log", "a") as f:
            f.write(f"[{ts}] [{level}] {msg}\n")
    except Exception:
        pass


def _compute_rsi(series, period: int = 2):
    """RSI pur pandas (identique au backtest)."""
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)


def _compute_indicators(df):
    """
    Calcule les indicateurs nécessaires pour Bot J.
    Retourne un DataFrame enrichi ou None si pas assez de données.
    """
    if df is None or len(df) < MIN_BARS:
        return None

    c  = df["close"]
    h  = df["high"]
    lo = df["low"]

    rsi2     = _compute_rsi(c, RSI_PERIOD)
    sma200   = c.rolling(SMA_LONG).mean()
    bb_mid   = c.rolling(BB_PERIOD).mean()
    bb_std   = c.rolling(BB_PERIOD).std()
    bb_lower = bb_mid - BB_STD * bb_std
    atr14    = compute_atr(h, lo, c, 14)

    result = df.copy()
    result["rsi2"]     = rsi2
    result["sma200"]   = sma200
    result["bb_mid"]   = bb_mid
    result["bb_lower"] = bb_lower
    result["atr14"]    = atr14
    return result.dropna(subset=["rsi2", "sma200", "bb_lower", "atr14"])


def run_mr_cycle(state: dict, daily_cache: dict, macro_context: dict = None) -> dict:
    """
    Cycle Bot J — Mean Reversion.
    daily_cache : {symbol: DataFrame OHLCV daily}
    """
    macro_context = macro_context or {}
    engine = macro_context.get("bot_z_engine", "BALANCED")  # filtre régime Bot Z

    # ── 0. Gestion stops et sorties sur positions ouvertes ────────────────────
    for symbol in list(state["positions"].keys()):
        pos = state["positions"][symbol]
        df  = daily_cache.get(symbol)
        if df is None or df.empty:
            continue

        enriched = _compute_indicators(df)
        if enriched is None or enriched.empty:
            continue

        last = enriched.iloc[-1]
        current_price = float(last["close"])
        rsi2          = float(last["rsi2"])
        bb_mid        = float(last["bb_mid"])

        exit_reason = None
        exit_price  = current_price

        # Stop loss
        if current_price <= pos["stop"]:
            exit_reason = "stop_loss"
            exit_price  = pos["stop"]
        # RSI normalisé ou retour au milieu Bollinger
        elif rsi2 > RSI_EXIT or current_price > bb_mid:
            exit_reason = "mean_reverted" if current_price > bb_mid else "rsi_exit"

        if exit_reason:
            exit_eff  = exit_price * (1 - config.SLIPPAGE)
            fee_exit  = exit_eff * pos["size"] * config.EXCHANGE_FEE
            proceeds  = exit_eff * pos["size"] - fee_exit
            pnl       = proceeds - pos["cost"]
            state["capital"] += proceeds

            state["trades"].append({
                "symbol":      symbol,
                "entry_date":  pos["date"],
                "exit_date":   str(datetime.now()),
                "entry_price": pos["entry"],
                "exit_price":  round(exit_eff, 4),
                "pnl":         round(pnl, 2),
                "reason":      exit_reason,
                "result":      "win" if pnl > 0 else "loss",
                "rsi2_entry":  pos.get("rsi2_entry", 0),
            })
            state["positions"].pop(symbol)

            _log(
                f"{'✓' if pnl > 0 else '✗'} CLOSE {symbol} | "
                f"{pos['entry']:.4f}€ → {exit_eff:.4f}€ | "
                f"PnL: {pnl:+.2f}€ | {exit_reason}",
                "BUY" if pnl > 0 else "SELL",
            )
            notify(
                f"{'✅' if pnl > 0 else '🔴'} <b>Bot J — Mean Reversion</b>\n"
                f"{'✓' if pnl > 0 else '✗'} <b>{symbol}</b> {exit_reason.upper()}\n"
                f"{pos['entry']:.4f}€ → {exit_eff:.4f}€\n"
                f"PnL : <b>{pnl:+.2f}€</b>"
            )

    # ── 1. Chercher des signaux d'entrée ──────────────────────────────────────
    if engine in ("SHIELD", "PRO"):
        _log(f"Engine={engine} — nouveaux BUY bloqués (régime défensif Bot Z)", "WARN")
        return state

    for symbol in config.SYMBOLS:
        if symbol in state["positions"]:
            continue  # déjà en position

        df = daily_cache.get(symbol)
        if df is None or df.empty:
            continue

        enriched = _compute_indicators(df)
        if enriched is None or enriched.empty:
            continue

        last          = enriched.iloc[-1]
        current_price = float(last["close"])
        rsi2          = float(last["rsi2"])
        bb_lower      = float(last["bb_lower"])
        sma200        = float(last["sma200"])
        atr14         = float(last["atr14"])

        if any(v != v for v in [rsi2, bb_lower, sma200, atr14]):  # NaN check
            continue

        # Signal Mean Reversion : survente extrême + extension bas + au-dessus SMA200
        if rsi2 < RSI_ENTRY and current_price < bb_lower and current_price > sma200 and atr14 > 0:
            entry_price = current_price * (1 + config.SLIPPAGE)
            stop_loss   = entry_price - ATR_MULT * atr14
            risk        = entry_price - stop_loss
            if risk <= 0:
                continue

            # Sizing : 0.5% capital risqué, max 10% capital | PARITY : ×0.70
            size_factor = 0.70 if engine == "PARITY" else 1.0
            size = min(
                state["capital"] * RISK_PCT * size_factor / risk,
                state["capital"] * MAX_POS_PCT * size_factor / entry_price,
            )
            size = max(size, 0)
            if size <= 0:
                continue

            fee_entry  = entry_price * size * config.EXCHANGE_FEE
            total_cost = size * entry_price + fee_entry

            if total_cost > state["capital"]:
                _log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€ < {total_cost:.2f}€)", "WARN")
                continue

            state["capital"] -= total_cost
            state["positions"][symbol] = {
                "entry":      round(entry_price, 4),
                "size":       round(size, 6),
                "cost":       round(total_cost, 4),
                "stop":       round(stop_loss, 4),
                "date":       str(datetime.now()),
                "atr14":      round(atr14, 4),
                "rsi2_entry": round(rsi2, 2),
            }

            _log(
                f"▲ BUY {symbol} | RSI2={rsi2:.1f} | {entry_price:.4f}€ | "
                f"SL: {stop_loss:.4f}€ (1.5×ATR) | {total_cost:.2f}€",
                "BUY",
            )
            notify(
                f"📈 <b>Bot J — Mean Reversion</b>\n"
                f"▲ <b>{symbol}</b> BUY — RSI2={rsi2:.1f} (survente)\n"
                f"Prix : {entry_price:.4f}€ | Stop : {stop_loss:.4f}€\n"
                f"Investi : {total_cost:.2f}€ | ATR : {atr14:.4f}"
            )

    # ── 2. Résumé du cycle ────────────────────────────────────────────────────
    total_pos_val = sum(
        float(daily_cache[s]["close"].iloc[-1]) * p["size"]
        for s, p in state["positions"].items()
        if s in daily_cache and not daily_cache[s].empty
    )
    total = state["capital"] + total_pos_val
    perf  = (total - state["initial_capital"]) / state["initial_capital"] * 100

    _log(
        f"Positions: {list(state['positions'].keys()) or '—'} | "
        f"Capital libre: {state['capital']:.2f}€ | "
        f"Total: {total:.2f}€ | Perf: {perf:+.2f}%"
    )

    return state

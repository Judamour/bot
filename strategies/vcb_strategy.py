"""
Bot H: Volatility Compression Breakout (VCB)

Inspiré des travaux de Mark Minervini et du style CANSLIM (O'Neil).
Utilisé par certains desks quant systématiques.

Concept :
  Les plus grosses tendances démarrent après une phase de compression de volatilité.
  grosse hausse → consolidation serrée (vol en baisse) → breakout explosif

  La volatilité baisse → les vendeurs disparaissent → les institutions accumulent
  → un catalyseur arrive → explosion du prix.

Conditions d'entrée (4h) :
  1. Trend    : prix > SMA200 AND prix > SMA50
  2. Compression : ATR(14) décroissant pendant >= 5 périodes
                   AND Bollinger Band width < percentile 20% (sur 100 barres)
  3. Breakout : prix > plus haut des 20 dernières bougies (shift 1 — no lookahead)

Stop   : 1.5×ATR (serré — l'énergie est accumulée, le prix ne doit pas reculer)
Sortie : trailing stop 3×ATR

Univers : BTC/EUR, ETH/EUR, SOL/EUR, NVDAx/EUR, AMDx/EUR, METAx/EUR, PLTRx/EUR
          (actifs à forte volatilité où les compressions explosent le plus)
Timeframe : 4h | Capital : 1000€ | Max positions : 5 | Size : 20% / position
"""
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import compute_atr

STATE_FILE = "logs/vcb/state.json"
INITIAL_CAPITAL = 1000.0

# Univers : actifs à forte volatilité — compressions les plus explosives
VCB_SYMBOLS = [
    s for s in config.SYMBOLS
    if s in ("BTC/EUR", "ETH/EUR", "SOL/EUR",
             "NVDAx/EUR", "AMDx/EUR", "METAx/EUR", "PLTRx/EUR")
]

# Indicateurs
SMA_LONG = 200
SMA_SHORT = 50
BB_PERIOD = 20              # Bollinger Bands période
BB_PERCENTILE_LOOKBACK = 100  # Fenêtre de calcul du percentile BB width
ATR_PERIOD = 14
N_COMPRESSION = 5           # ATR doit décliner pendant >= 5 barres consécutives
BREAKOUT_PERIOD = 20        # Breakout = plus haut des 20 dernières bougies

# Risk management
ATR_STOP_ENTRY = 1.5        # Stop entrée : 1.5×ATR (serré — énergie compressée)
ATR_TRAIL = 3.0             # Trailing stop : 3×ATR
POSITION_PCT = 0.20         # 20% du capital par position
MAX_POSITIONS = 5


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
    print(f"{ts} [BOT-H][{level}] {msg}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/vcb.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


def _add_indicators(df):
    """
    Ajoute SMA200, SMA50, BB width percentile, ATR compression flag, breakout_high.
    Tous calculés avec shift(1) ou sur données passées pour éviter le look-ahead.
    """
    df = df.copy()

    # Trend
    df["sma200"] = df["close"].rolling(SMA_LONG).mean()
    df["sma50"] = df["close"].rolling(SMA_SHORT).mean()

    # ATR(14)
    df["atr"] = compute_atr(df["high"], df["low"], df["close"], ATR_PERIOD)

    # ── Bollinger Band width percentile ──────────────────────────────────────
    bb_mid = df["close"].rolling(BB_PERIOD).mean()
    bb_std = df["close"].rolling(BB_PERIOD).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_width = (bb_upper - bb_lower) / (bb_mid + 1e-10)

    # Percentile 0-1 sur 100 barres glissantes (0 = ultra compressé, 1 = très large)
    bb_min = bb_width.rolling(BB_PERCENTILE_LOOKBACK).min()
    bb_max = bb_width.rolling(BB_PERCENTILE_LOOKBACK).max()
    df["bb_width_pct"] = (bb_width - bb_min) / (bb_max - bb_min + 1e-10)

    # ── ATR compression : ATR décroissant >= N_COMPRESSION barres consécutives ─
    atr_declining = (df["atr"].diff() < 0).astype(int)
    df["atr_compressed"] = atr_declining.rolling(N_COMPRESSION).sum() >= N_COMPRESSION

    # ── Breakout : plus haut des 20 dernières bougies (shift 1 = no lookahead) ─
    df["breakout_high"] = df["high"].rolling(BREAKOUT_PERIOD).max().shift(1)

    return df.dropna()


def run_vcb_cycle(state: dict, ohlcv_4h: dict, macro_context: dict = None) -> dict:
    """Run one cycle of the Volatility Compression Breakout strategy."""
    macro_context = macro_context or {}
    vix = macro_context.get("vix", 0.0)
    engine = macro_context.get("bot_z_engine", "BALANCED")  # filtre régime Bot Z

    for symbol in VCB_SYMBOLS:
        df = ohlcv_4h.get(symbol)
        if df is None or len(df) < SMA_LONG + BB_PERCENTILE_LOOKBACK + 10:
            log(f"{symbol} — Données insuffisantes ({len(df) if df is not None else 0} barres)", "WARN")
            continue

        df_ind = _add_indicators(df)
        if df_ind.empty:
            continue

        last = df_ind.iloc[-1]
        current_price = float(last["close"])
        sma200 = float(last["sma200"])
        sma50 = float(last["sma50"])
        atr = float(last["atr"])
        bb_pct = float(last["bb_width_pct"])
        atr_compressed = bool(last["atr_compressed"])
        breakout_high = float(last["breakout_high"])

        position = state["positions"].get(symbol)

        # ── Trailing stop update ──
        if position:
            new_stop = round(current_price - ATR_TRAIL * atr, 4)
            if new_stop > position["stop"]:
                position["stop"] = new_stop
                state["positions"][symbol] = position
                log(f"{symbol} — Trailing stop → {new_stop:.4f}€")

        # ── Exit checks ──
        if position:
            exit_reason = None
            exit_price = current_price

            if current_price <= position["stop"]:
                exit_reason = "trailing_stop"
                exit_price = position["stop"]

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
                continue

        # ── Entry checks ──
        if symbol not in state["positions"]:
            if len(state["positions"]) >= MAX_POSITIONS:
                continue

            # Filtre engine Bot Z : bloquer nouveaux BUY en mode défensif
            if engine in ("SHIELD", "PRO"):
                log(f"{symbol} — BUY bloqué (engine={engine})")
                continue

            trend_ok = current_price > sma200 and current_price > sma50
            compression_ok = atr_compressed and bb_pct < 0.20  # BB width dans les 20% les plus bas
            breakout_ok = current_price > breakout_high if breakout_high > 0 else False

            if trend_ok and compression_ok and breakout_ok:
                entry_price = current_price * (1 + config.SLIPPAGE)
                # PARITY : réduire l'exposition de 30%
                size_factor = 0.70 if engine == "PARITY" else 1.0
                dollar_size = state["capital"] * POSITION_PCT * size_factor
                size = dollar_size / (entry_price * (1 + config.EXCHANGE_FEE))
                stop_loss = round(entry_price - ATR_STOP_ENTRY * atr, 4)
                fee = entry_price * size * config.EXCHANGE_FEE
                total_cost = size * entry_price + fee

                if total_cost > state["capital"] or size <= 0:
                    log(f"{symbol} — Capital insuffisant ({state['capital']:.2f}€)", "WARN")
                    continue

                state["capital"] -= total_cost
                state["positions"][symbol] = {
                    "entry": round(entry_price, 4),
                    "size": round(size, 6),
                    "cost": round(total_cost, 4),
                    "stop": stop_loss,
                    "date": str(datetime.now()),
                    "atr": round(atr, 4),
                    "bb_pct": round(bb_pct, 3),
                }
                log(
                    f"▲ BUY {symbol} | {entry_price:.4f}€ | {size:.6f} units | "
                    f"SL: {stop_loss:.4f}€ (1.5×ATR) | "
                    f"BB_pct: {bb_pct:.1%} | VIX: {vix:.1f}",
                    "BUY",
                )
            else:
                # Log si trend OK mais compression pas encore là (utile pour surveiller)
                if trend_ok and not compression_ok:
                    log(
                        f"{symbol} | {current_price:.4f}€ | Trend: ✓ | "
                        f"Compression: {'✓' if atr_compressed else '✗ (ATR)'} "
                        f"BB: {bb_pct:.1%} {'✓' if bb_pct < 0.20 else '✗ (>' + '20%)'} | "
                        f"Breakout: {'✓' if breakout_ok else '✗'}"
                    )

    return state

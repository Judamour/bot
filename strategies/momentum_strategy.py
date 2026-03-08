"""
Bot B: Momentum Rotation Strategy
Based on Gary Antonacci's Dual Momentum principles.

Selection: composite score = 0.4×(1m) + 0.4×(3m) + 0.2×(6m)
Universe  : all SYMBOLS (crypto + xStocks)
Hold      : top TOP_N assets with positive absolute momentum
Rebalance : weekly (every 7+ days)
Sizing    : equal weight — capital / TOP_N per position
No stop loss, no Claude filter — pure quantitative rotation.

Expected performance: 15-20% CAGR (Antonacci dual momentum research)
"""
import json
import os
import sys
from datetime import datetime, date

import pytz

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from live.notifier import notify


def _is_us_market_open() -> bool:
    """Vérifie si la bourse US est ouverte (lundi-vendredi, 9h30-16h ET)."""
    try:
        et = datetime.now(pytz.timezone("America/New_York"))
        return et.weekday() < 5 and 9 * 60 + 30 <= et.hour * 60 + et.minute <= 16 * 60
    except Exception:
        return True

STATE_FILE = "logs/momentum/state.json"
INITIAL_CAPITAL = 1000.0
TOP_N = 4                     # Hold top N assets
REBALANCE_MIN_DAYS = 6        # Minimum days between rebalances
POSITION_WEIGHT = 1.0 / TOP_N  # 25% per position if TOP_N=4
STOP_LOSS_PCT = 0.12          # Stop individuel : -12% depuis l'entrée
VIX_PAUSE_THRESHOLD = 30      # Pause rebalancement si VIX > 30 (stress marché)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},        # {symbol: {entry, size, cost, date, score}}
        "trades": [],
        "initial_capital": INITIAL_CAPITAL,
        "top_symbols": [],
        "last_rebalance_date": None,
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [BOT-B][{level}] {msg}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/momentum.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


def compute_momentum_score(symbol: str, daily_cache: dict) -> float:
    """
    Composite momentum score = 0.4×(1m) + 0.4×(3m) + 0.2×(6m)
    Uses daily OHLCV. Returns float('nan') if insufficient data.
    """
    df = daily_cache.get(symbol)
    if df is None or len(df) < 130:
        return float("nan")

    close = df["close"]
    price_now = float(close.iloc[-1])

    # Trading-day lookbacks (approx)
    n1m = min(22, len(close) - 1)
    n3m = min(66, len(close) - 1)
    n6m = min(130, len(close) - 1)

    p1m = float(close.iloc[-n1m])
    p3m = float(close.iloc[-n3m])
    p6m = float(close.iloc[-n6m])

    if p1m <= 0 or p3m <= 0 or p6m <= 0:
        return float("nan")

    r1m = (price_now - p1m) / p1m
    r3m = (price_now - p3m) / p3m
    r6m = (price_now - p6m) / p6m

    return 0.4 * r1m + 0.4 * r3m + 0.2 * r6m


def _needs_rebalance(state: dict) -> bool:
    """True if >= REBALANCE_MIN_DAYS since last rebalance (or never rebalanced)."""
    last = state.get("last_rebalance_date")
    if last is None:
        return True
    last_date = date.fromisoformat(last)
    return (date.today() - last_date).days >= REBALANCE_MIN_DAYS


def _portfolio_value(state: dict, daily_cache: dict) -> float:
    """Total portfolio value: free capital + mark-to-market of open positions."""
    total = state["capital"]
    for symbol, pos in state["positions"].items():
        df = daily_cache.get(symbol)
        if df is not None:
            total += float(df["close"].iloc[-1]) * pos["size"]
        else:
            # Fallback: use entry price (no mark-to-market)
            total += pos["entry"] * pos["size"]
    return total


def run_momentum_cycle(state: dict, daily_cache: dict, macro_context: dict = None) -> dict:
    """
    Run one cycle of the momentum rotation strategy.
    Only rebalances weekly; otherwise tracks positions passively.
    """
    # ── 0. Stop loss individuel (-12%) — vérifié à chaque cycle ──
    for symbol in list(state["positions"].keys()):
        pos = state["positions"][symbol]
        df = daily_cache.get(symbol)
        if df is None:
            continue
        current_price = float(df["close"].iloc[-1])
        entry = pos.get("entry", current_price)
        loss_pct = (current_price - entry) / entry if entry > 0 else 0
        if loss_pct <= -STOP_LOSS_PCT:
            exit_price = current_price * (1 - config.SLIPPAGE)
            fee_exit = exit_price * pos["size"] * config.EXCHANGE_FEE
            proceeds = exit_price * pos["size"] - fee_exit
            pnl = proceeds - pos["cost"]
            state["capital"] += proceeds
            state["trades"].append({
                "symbol": symbol,
                "entry_date": pos["date"],
                "exit_date": str(datetime.now()),
                "entry_price": pos["entry"],
                "exit_price": exit_price,
                "pnl": round(pnl, 2),
                "reason": f"stop_loss_{STOP_LOSS_PCT*100:.0f}pct",
                "result": "win" if pnl > 0 else "loss",
            })
            state["positions"].pop(symbol)
            log(
                f"✗ STOP {symbol} | {pos['entry']:.4f}€ → {exit_price:.4f}€ | "
                f"PnL: {pnl:+.2f}€ | ({loss_pct*100:.1f}% < -{STOP_LOSS_PCT*100:.0f}%)",
                "SELL",
            )
            notify(
                f"🔴 <b>Bot B — Momentum</b>\n"
                f"✗ <b>{symbol}</b> STOP LOSS\n"
                f"{pos['entry']:.4f}€ → {exit_price:.4f}€\n"
                f"PnL : <b>{pnl:+.2f}€</b> ({loss_pct*100:.1f}%)"
            )

    # ── 1. Filtre macro (VIX + QQQ) ──
    if macro_context:
        vix = macro_context.get("vix", 0.0)
        qqq_ok = macro_context.get("qqq_regime_ok", True)
        if vix > VIX_PAUSE_THRESHOLD:
            log(f"VIX={vix:.1f} > {VIX_PAUSE_THRESHOLD} — rebalancement suspendu (stress marché)", "WARN")
            return state
        if not qqq_ok:
            qqq_desc = macro_context.get("qqq_description", "N/A")
            log(f"QQQ régime baissier ({qqq_desc}) — rebalancement suspendu", "WARN")
            return state

    # ── 2. Compute scores for all symbols ──
    scores = {}
    for symbol in config.SYMBOLS:
        score = compute_momentum_score(symbol, daily_cache)
        if score == score:  # not NaN
            scores[symbol] = score

    # ── 3. Rank — keep only positive absolute momentum ──
    positive = {s: sc for s, sc in scores.items() if sc > 0}
    ranked = sorted(positive.items(), key=lambda x: x[1], reverse=True)
    top_symbols = [s for s, _ in ranked[:TOP_N]]

    log(
        f"Scores computed: {len(scores)} symbols | "
        f"Top {TOP_N}: " + ", ".join(f"{s} {sc:.1%}" for s, sc in ranked[:TOP_N])
    )

    # ── 4. Check if rebalance needed ──
    if not _needs_rebalance(state):
        log(f"No rebalance (last: {state.get('last_rebalance_date')}). "
            f"Holding: {list(state['positions'].keys())}")
        return state

    log("Weekly rebalance triggered!")

    # ── 5. Close positions no longer in top_N ──
    for symbol in list(state["positions"].keys()):
        if symbol not in top_symbols:
            pos = state["positions"][symbol]
            df = daily_cache.get(symbol)
            if df is None:
                log(f"{symbol} — No data for exit, keeping position", "WARN")
                continue

            exit_price = float(df["close"].iloc[-1]) * (1 - config.SLIPPAGE)
            fee_exit = exit_price * pos["size"] * config.EXCHANGE_FEE
            proceeds = exit_price * pos["size"] - fee_exit
            pnl = proceeds - pos["cost"]
            state["capital"] += proceeds

            state["trades"].append({
                "symbol": symbol,
                "entry_date": pos["date"],
                "exit_date": str(datetime.now()),
                "entry_price": pos["entry"],
                "exit_price": exit_price,
                "pnl": round(pnl, 2),
                "reason": "momentum_rotation",
                "result": "win" if pnl > 0 else "loss",
            })
            state["positions"].pop(symbol)
            log(
                f"{'✓' if pnl > 0 else '✗'} CLOSE {symbol} | "
                f"{pos['entry']:.4f}€ → {exit_price:.4f}€ | PnL: {pnl:+.2f}€ | rotated out",
                "BUY" if pnl > 0 else "SELL",
            )
            notify(
                f"{'✅' if pnl > 0 else '🔴'} <b>Bot B — Momentum</b>\n"
                f"{'✓' if pnl > 0 else '✗'} <b>{symbol}</b> ROTATION OUT\n"
                f"{pos['entry']:.4f}€ → {exit_price:.4f}€\n"
                f"PnL : <b>{pnl:+.2f}€</b>"
            )

    # ── 6. Open positions for new top_N not already held ──
    # Sizing basé sur la valeur totale du portefeuille / TOP_N (équilibré)
    to_buy = [s for s in top_symbols if s not in state["positions"]]
    total_portfolio = _portfolio_value(state, daily_cache)
    target_per_pos = total_portfolio / TOP_N  # Cible uniforme par position

    for symbol in to_buy:
        if symbol in config.XSTOCKS and not _is_us_market_open():
            log(f"{symbol} — Marché US fermé, BUY ignoré")
            continue

        df = daily_cache.get(symbol)
        if df is None:
            log(f"{symbol} — No data, skipping entry", "WARN")
            continue

        entry_price = float(df["close"].iloc[-1]) * (1 + config.SLIPPAGE)
        if entry_price <= 0:
            continue

        size = target_per_pos / (entry_price * (1 + config.EXCHANGE_FEE))
        fee_entry = entry_price * size * config.EXCHANGE_FEE
        total_cost = size * entry_price + fee_entry

        if total_cost > state["capital"] or size <= 0:
            log(f"{symbol} — Insufficient capital ({state['capital']:.2f}€)", "WARN")
            continue

        state["capital"] -= total_cost
        state["positions"][symbol] = {
            "entry": round(entry_price, 4),
            "size": round(size, 6),
            "cost": round(total_cost, 4),
            "date": str(datetime.now()),
            "score": round(scores.get(symbol, 0), 4),
        }
        log(
            f"▲ BUY {symbol} | {entry_price:.4f}€ | {size:.4f} unités | "
            f"Score: {scores.get(symbol, 0):.1%} | Coût: {total_cost:.2f}€",
            "BUY",
        )
        notify(
            f"📈 <b>Bot B — Momentum</b>\n"
            f"▲ <b>{symbol}</b> BUY\n"
            f"Prix : {entry_price:.4f}€ | Investi : {total_cost:.2f}€\n"
            f"Score momentum : {scores.get(symbol, 0):.1%}"
        )

    state["top_symbols"] = top_symbols
    state["last_rebalance_date"] = str(date.today())

    # ── 7. Summary ──
    total = _portfolio_value(state, daily_cache)
    perf = (total - state["initial_capital"]) / state["initial_capital"] * 100
    log(
        f"Rebalance done | Positions: {list(state['positions'].keys())} | "
        f"Capital libre: {state['capital']:.2f}€ | Perf: {perf:+.2f}%"
    )
    return state

"""
Multi-Bot Contest Runner
Runs technical trading strategies simultaneously with a shared market data hub.

Architecture:
  One MarketSnapshot (macro + OHLCV) → Bot A + Bot B + Bot C + Bot G + Bot H + Bot I + Bot J

  Bot A: Supertrend + filters + MR RSI(2)     — dispatched par Bot Z
  Bot B: Momentum Rotation (Antonacci)         — dispatched par Bot Z
  Bot C: Donchian Breakout Turtle System 2     — dispatched par Bot Z
  Bot G: Trend Following Multi-Asset (CTA)     — dispatched par Bot Z
  Bot H: VCB Breakout                          — expérimental (1000€ fixes)
  Bot I: RS Leaders                            — expérimental (1000€ fixes)
  Bot J: Mean Reversion                        — expérimental (1000€ fixes)

Usage:
    python live/multi_runner.py
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from colorama import Fore, Style, init

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.market_snapshot import fetch_macro_context, fetch_ohlcv_cache, compute_breadth
from strategies.momentum_strategy import (
    run_momentum_cycle, load_state as load_mom, save_state as save_mom,
)
from strategies.breakout_strategy import (
    run_breakout_cycle, BREAKOUT_SYMBOLS,
    load_state as load_brk, save_state as save_brk,
)
from strategies.trend_following_strategy import (
    run_trend_cycle, load_state as load_trd, save_state as save_trd,
)
from strategies.vcb_strategy import (
    run_vcb_cycle, load_state as load_vcb, save_state as save_vcb,
)
from strategies.rs_leaders_strategy import (
    run_rs_leaders_cycle, load_state as load_rsl, save_state as save_rsl,
)
from strategies.mean_reversion_strategy import (
    run_mr_cycle, load_state as load_mr, save_state as save_mr,
)
from live.bot_z import run_bot_z_cycle, print_bot_z_summary
import live.bot as bot_a

init(autoreset=True)

STATE_A_FILE = "logs/supertrend/state.json"
CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]
INITIAL_CAPITAL_PER_BOT = config.INITIAL_CAPITAL_PER_BOT
Z_BUDGET_FILE = "logs/bot_z/budget.json"


def _apply_z_budget(state: dict, z_budget_eur: float, ohlcv_cache: dict | None = None) -> dict:
    """Aligne le `capital` cash d'un sub-bot sur (z_budget − positions mark-to-market).

    Le budget Bot Z représente l'allocation cible totale du sub-bot (cash + positions).
    On dérive donc le cash disponible : capital = max(0, z_budget − positions_value_mtm).
    Cela garantit que sum(states.capital + positions_mtm) ≈ sum(z_budget) ≈ broker_equity,
    éliminant le drift cumulatif des bots qui ne tradent pas (B, C) — leur capital cash
    s'aligne automatiquement sur leur budget cible au lieu de s'empiler par scale ratio.

    Si le bot est "mort" (capital + positions < 5€), injecte le budget comme capital frais.
    """
    if "original_capital" not in state:
        state["original_capital"] = state.get("initial_capital", INITIAL_CAPITAL_PER_BOT)

    def _mtm_price(symbol: str, fallback: float) -> float:
        if ohlcv_cache and symbol in ohlcv_cache:
            try:
                df = ohlcv_cache[symbol]
                if df is not None and not df.empty:
                    import math
                    px = float(df["close"].iloc[-1])
                    if not math.isnan(px) and px > 0:
                        return px
            except Exception:
                pass
        return float(fallback or 0)

    positions_mtm = sum(
        _mtm_price(sym, p.get("entry", 0)) * float(p.get("size", 0) or 0)
        for sym, p in state.get("positions", {}).items()
    )
    total_value = state.get("capital", 0) + positions_mtm

    if total_value < 5.0 and z_budget_eur > 0:
        bot_id = state.get("_bot_id", "?")
        log(f"[Z→] Bot {bot_id} mort ({total_value:.2f}€) — injection {z_budget_eur:.0f}€", "WARN")
        from live.notifier import notify_bot_revived
        notify_bot_revived(bot_id, z_budget_eur)
        state["capital"] = round(z_budget_eur, 2)
        state["positions"] = {}
        state["trades"] = []
        state["initial_capital"] = round(z_budget_eur, 2)
        state["z_budget_eur"] = round(z_budget_eur, 2)
        state["original_capital"] = round(z_budget_eur, 2)
        state["dd_frozen"] = False
        return state

    if z_budget_eur < config.MIN_ORDER_EUR * 2:
        state["capital"] = round(z_budget_eur, 2)
        state["initial_capital"] = round(z_budget_eur, 2)
        state["z_budget_eur"] = round(z_budget_eur, 2)
        state["_below_min_order"] = True
        return state
    state["_below_min_order"] = False

    cash_target = max(0.0, z_budget_eur - positions_mtm)
    state["capital"] = round(cash_target, 2)
    state["initial_capital"] = round(z_budget_eur, 2)
    state["z_budget_eur"] = round(z_budget_eur, 2)

    # Audit dormant : compteur de cycles consécutifs sans positions ni trades.
    # Alerte console à 90 cycles (≈15j en 4h) — signal stratégie inadaptée à l'univers.
    n_pos = len(state.get("positions", {}) or {})
    n_tr = len(state.get("trades", []) or [])
    if n_pos == 0 and n_tr == 0:
        state["dormant_cycles"] = int(state.get("dormant_cycles", 0)) + 1
        if state["dormant_cycles"] in (90, 180, 360):
            bid = state.get("_bot_id", "?")
            log(f"💤 Bot {bid} dormant depuis {state['dormant_cycles']} cycles "
                f"(~{state['dormant_cycles']*4//24}j) — vérifier si stratégie adaptée à l'univers", "WARN")
    else:
        state["dormant_cycles"] = 0
    return state


# ── State A management ───────────────────────────────────────────────────────

def load_state_a() -> dict:
    if os.path.exists(STATE_A_FILE):
        with open(STATE_A_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL_PER_BOT,
        "positions": {},
        "trades": [],
        "initial_capital": INITIAL_CAPITAL_PER_BOT,
    }


def save_state_a(state: dict):
    os.makedirs("logs/supertrend", exist_ok=True)
    tmp = STATE_A_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_A_FILE)


# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    colors = {"INFO": Fore.CYAN, "WARN": Fore.YELLOW, "OK": Fore.GREEN}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{colors.get(level, '')}{ts} [MULTI] {msg}{Style.RESET_ALL}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/multi_runner.log", "a") as f:
        f.write(f"{ts} [MULTI][{level}] {msg}\n")


# ── Contest display ───────────────────────────────────────────────────────────

def _portfolio_value(state: dict, price_cache: dict = None) -> float:
    """Total portfolio value using available prices."""
    total = state.get("capital", 0)
    for symbol, pos in state.get("positions", {}).items():
        if price_cache and symbol in price_cache:
            price = float(price_cache[symbol]["close"].iloc[-1])
        else:
            price = pos.get("entry", 0)
        total += price * pos.get("size", 0)
    return total


def print_contest_status(state_a: dict, state_b: dict, state_c: dict,
                          state_g: dict, state_h: dict, state_i: dict,
                          state_j: dict = None,
                          daily_cache: dict = None):
    bots = [
        ("A — Supertrend+MR",     state_a),
        ("B — Momentum",          state_b),
        ("C — Breakout",          state_c),
        ("G — Trend Multi-Asset", state_g),
        ("H — VCB Breakout",      state_h),
        ("I — RS Leaders",        state_i),
        ("J — Mean Reversion",    state_j or {}),
    ]

    print(f"\n{Fore.CYAN}{'='*72}")
    print(f"  CONTEST MULTI-BOT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*72}{Style.RESET_ALL}")
    print(f"{'Bot':<26} {'Libre':>10} {'Total':>10} {'Positions':>10} {'Trades':>8} {'Perf':>8}")
    print("-" * 72)

    # Map bot_id → state pour filtrer sur ACTIVE_BOTS (ne pas afficher les bots
    # désactivés dont le capital fantôme fausse la perf totale).
    bots_map = [
        ("a", "A — Supertrend+MR",     state_a),
        ("b", "B — Momentum",          state_b),
        ("c", "C — Breakout",          state_c),
        ("g", "G — Trend Multi-Asset", state_g),
        ("h", "H — VCB Breakout",      state_h),
        ("i", "I — RS Leaders",        state_i),
        ("j", "J — Mean Reversion",    state_j or {}),
    ]
    active_states = []
    for bid, name, state in bots_map:
        if not state or bid not in config.ACTIVE_BOTS:
            continue
        active_states.append(state)
        total = _portfolio_value(state, daily_cache)
        init = state.get("initial_capital", INITIAL_CAPITAL_PER_BOT)
        perf = (total - init) / init * 100 if init > 0 else 0
        trades = len(state.get("trades", []))
        positions = ", ".join(state.get("positions", {}).keys()) or "—"
        capital = state.get("capital", 0)

        color = Fore.GREEN if perf > 0 else Fore.RED if perf < 0 else Fore.WHITE
        print(
            f"Bot {name:<22} {capital:>8.2f}$  {total:>8.2f}$  "
            f"{positions:<12} {trades:>6}  {color}{perf:>+6.1f}%{Style.RESET_ALL}"
        )

    # TOTAL : référence = broker equity (fetché par _sync_broker_capital_periodic)
    # plutôt que sum(initial_capital) qui fluctue avec les budgets dispatchés Bot Z.
    total_all = sum(_portfolio_value(s, daily_cache) for s in active_states)
    broker_ref = float(config.INITIAL_CAPITAL or 0) or sum(
        s.get("original_capital", s.get("initial_capital", INITIAL_CAPITAL_PER_BOT))
        for s in active_states
    )
    combined_perf = (total_all - broker_ref) / broker_ref * 100 if broker_ref > 0 else 0
    color = Fore.GREEN if combined_perf > 0 else Fore.RED
    print("-" * 72)
    print(f"{'TOTAL (broker '+f'{broker_ref:.0f}'+'$)':<26} {'':>10} {total_all:>8.2f}$  "
          f"{'':>10} {'':>8}  {color}{combined_perf:>+6.1f}%{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*72}{Style.RESET_ALL}\n")


# ── Broker capital sync (boot + chaque cycle) ────────────────────────────────

DRIFT_REALIGN_THRESHOLD = 0.15  # 15% : au-delà, hard-realign capital sub-bots


def _fetch_broker_equity() -> float | None:
    """Fetch equity Alpaca (paper ou live). Retourne None si Alpaca off ou échec."""
    try:
        if getattr(config, "ALPACA_ENABLED", False):
            from live import alpaca_executor as _ax
            acct = _ax._request("GET", "/v2/account")
            return float(acct.get("equity") or acct.get("cash") or 0)
    except Exception as e:
        log(f"⚠ Fetch broker equity échec: {e}", "WARN")
    return None


def _states_total_value(states: list[dict], ohlcv_cache: dict | None = None) -> float:
    """Somme cash + positions (mark-to-market si OHLCV dispo, sinon entry price)."""
    total = 0.0
    for s in states:
        if not isinstance(s, dict):
            continue
        total += float(s.get("capital", 0) or 0)
        for sym, p in s.get("positions", {}).items():
            entry = float(p.get("entry", 0) or 0)
            size  = float(p.get("size", 0) or 0)
            price = entry
            if ohlcv_cache and sym in ohlcv_cache:
                try:
                    df = ohlcv_cache[sym]
                    if df is not None and not df.empty:
                        live = float(df["close"].iloc[-1])
                        import math
                        if not math.isnan(live) and live > 0:
                            price = live
                except Exception:
                    pass
            total += price * size
    return total


def _sync_broker_capital_periodic(active_states: list[tuple[str, dict]],
                                  ohlcv_cache: dict | None = None,
                                  is_first_cycle: bool = False) -> tuple[float | None, bool]:
    """
    Re-fetch broker equity, log drift vs sum(states). Si drift > seuil, hard-realign.
    Retourne (broker_equity, realigned).

    `is_first_cycle=True` : skip le hard realign brutal au boot — laisse
    `_apply_z_budget` réaligner proprement via z_budget − positions_mtm. Le drift
    transitoire est attendu après restart (states sub-bots du cycle précédent).
    """
    global INITIAL_CAPITAL_PER_BOT
    broker_equity = _fetch_broker_equity()
    if not broker_equity or broker_equity <= 0:
        return None, False

    states = [s for _, s in active_states]
    sum_states = _states_total_value(states, ohlcv_cache)
    if sum_states <= 0:
        return broker_equity, False

    drift_ratio = sum_states / broker_equity - 1
    drift_pct   = abs(drift_ratio) * 100
    log(f"[CAPITAL_SYNC] broker={broker_equity:.0f}$ states_sum={sum_states:.0f}$ "
        f"drift={drift_ratio*100:+.1f}%", "INFO")

    if is_first_cycle and drift_pct > DRIFT_REALIGN_THRESHOLD * 100:
        log(f"[CAPITAL_SYNC] First cycle après restart : skip hard realign "
            f"(drift {drift_pct:.0f}% sera lissé par _apply_z_budget)", "INFO")
        active_count = max(len(active_states), 1)
        config.INITIAL_CAPITAL = broker_equity
        config.INITIAL_CAPITAL_PER_BOT = round(broker_equity / active_count, 2)
        INITIAL_CAPITAL_PER_BOT = config.INITIAL_CAPITAL_PER_BOT
        return broker_equity, False

    if drift_pct <= DRIFT_REALIGN_THRESHOLD * 100:
        # Drift acceptable : on garde les capitals des bots (contest dynamique préservé)
        # mais on met à jour la cible per-bot pour les fresh states / nouveaux trades
        active_count = max(len(active_states), 1)
        config.INITIAL_CAPITAL = broker_equity
        config.INITIAL_CAPITAL_PER_BOT = round(broker_equity / active_count, 2)
        INITIAL_CAPITAL_PER_BOT = config.INITIAL_CAPITAL_PER_BOT
        return broker_equity, False

    # ── DRIFT > seuil : hard realign ──
    # En LIVE, on n'écrase JAMAIS les capitals automatiquement (un drift peut être
    # un dépôt/retrait broker légitime, ou positions fantômes alarmantes).
    if not config.PAPER_TRADING:
        log(f"⚠️ DRIFT {drift_pct:.0f}% détecté en LIVE — réalignement automatique DÉSACTIVÉ", "WARN")
        try:
            from live.notifier import notify
            notify(f"🚨 <b>DRIFT CAPITAL DÉTECTÉ (LIVE)</b>\n"
                   f"Broker : {broker_equity:.0f}$\n"
                   f"States : {sum_states:.0f}$ ({drift_ratio*100:+.1f}%)\n"
                   f"Réalignement manuel requis.")
        except Exception:
            pass
        return broker_equity, False

    # Paper : hard-realign. Capital dispo = broker_equity / N - valeur positions ouvertes.
    active_count = max(len(active_states), 1)
    per_bot = broker_equity / active_count
    log(f"⚠️ DRIFT {drift_pct:.0f}% > {DRIFT_REALIGN_THRESHOLD*100:.0f}% → HARD REALIGN "
        f"chaque bot à {per_bot:.0f}$", "WARN")
    for label, st in active_states:
        positions_value = 0.0
        for sym, p in st.get("positions", {}).items():
            price = float(p.get("entry", 0) or 0)
            if ohlcv_cache and sym in ohlcv_cache:
                try:
                    df = ohlcv_cache[sym]
                    if df is not None and not df.empty:
                        live = float(df["close"].iloc[-1])
                        import math
                        if not math.isnan(live) and live > 0:
                            price = live
                except Exception:
                    pass
            positions_value += price * float(p.get("size", 0) or 0)
        new_cash = max(0.0, per_bot - positions_value)
        old_cap  = st.get("capital", 0)
        st["capital"]         = round(new_cash, 2)
        st["initial_capital"] = round(per_bot, 2)
        log(f"[REALIGN] Bot {label} : capital {old_cap:.0f}$ → {new_cash:.0f}$ "
            f"(positions {positions_value:.0f}$)")
    config.INITIAL_CAPITAL = broker_equity
    config.INITIAL_CAPITAL_PER_BOT = round(per_bot, 2)
    INITIAL_CAPITAL_PER_BOT = config.INITIAL_CAPITAL_PER_BOT

    # Pas de notify Telegram en paper : le drift résiduel est attendu (perf différentielle
    # entre sub-bots vs broker équity), le realign est silencieux. On notifie seulement
    # si le drift est massif (> 50%) — vrai signe d'anomalie.
    if drift_pct > 50:
        try:
            from live.notifier import notify
            notify(f"⚠️ <b>DRIFT CAPITAL CORRIGÉ (paper)</b>\n"
                   f"Broker : {broker_equity:.0f}$\n"
                   f"States avant : {sum_states:.0f}$ ({drift_ratio*100:+.1f}%)\n"
                   f"Bots réalignés à {per_bot:.0f}$ chacun.")
        except Exception:
            pass
    return broker_equity, True


# ── Timing ───────────────────────────────────────────────────────────────────

# ── Stop monitor (15min daemon thread) ──────────────────────────────────────

_STOP_MONITOR_INTERVAL_SEC = 900  # 15 minutes
_COOLDOWN_HOURS_AFTER_STOP = 12   # Anti-whipsaw : block re-entry pendant 12h
_stop_monitor_stop = threading.Event()


def _close_position_from_broker_fill(state: dict, bot_id: str, symbol: str, filled_price: float):
    """
    Ferme une position dans le state suite à un fill broker-side (stop déclenché).
    Met à jour capital, append trade, supprime de positions, set cooldown.
    Notify Telegram pour visibilité.
    """
    from live.notifier import notify
    position = state.get("positions", {}).get(symbol)
    if not position:
        return

    size = float(position.get("size", 0))
    entry = float(position.get("entry", filled_price))
    cost = float(position.get("cost", entry * size))
    fee = filled_price * size * config.EXCHANGE_FEE
    proceeds = filled_price * size - fee
    pnl = proceeds - cost

    state["capital"] = state.get("capital", 0) + proceeds
    state.setdefault("trades", []).append({
        "symbol": symbol,
        "entry_date": position.get("date"),
        "exit_date": str(datetime.now()),
        "entry_price": entry,
        "exit_price": filled_price,
        "pnl": round(pnl, 2),
        "reason": "broker_stop_fill",
        "result": "win" if pnl > 0 else "loss",
    })
    state["positions"].pop(symbol, None)

    # Cooldown anti-whipsaw : block re-entry sur ce symbole pendant 12h
    cooldown_until = datetime.now(timezone.utc) + timedelta(hours=_COOLDOWN_HOURS_AFTER_STOP)
    state.setdefault("cooldowns", {})[symbol] = cooldown_until.isoformat()

    icon = "✓" if pnl > 0 else "✗"
    log(f"[STOP-FILL] Bot {bot_id}/{symbol} broker stop déclenché @ {filled_price:.4f} | "
        f"PnL: {pnl:+.2f} | Cooldown 12h", "WARN")
    notify(f"🔴 <b>BROKER STOP déclenché</b>\n"
           f"Bot {bot_id} — <b>{symbol}</b>\n"
           f"{entry:.4f} → {filled_price:.4f}\n"
           f"PnL: <b>{pnl:+.2f}$</b> ({icon})\n"
           f"Cooldown 12h activé")


def _stop_monitor_loop(states_registry: dict):
    """
    Daemon : entre les cycles 4h, vérifie toutes les 15min l'état des stops broker.
    - Stop expiré (Alpaca DAY tif sur fractional) → re-place automatiquement
    - Stop fillé (déclenché) → ferme position dans state, cooldown 12h, notify
    - Sans ce monitor, une position serait non-protégée jusqu'au prochain cycle 4h.

    Lit/mute les positions in-memory partagées avec le main thread. Pas de lock :
    les races sont rares (15min vs 4h) et non-fatales (pire cas = duplicate stop
    annulé au cycle suivant).
    """
    from live.order_executor import reconcile_broker_stop
    log("[STOP-MONITOR] démarré (interval 15min, cooldown 12h après fill)")
    while not _stop_monitor_stop.is_set():
        # Wait first so on bot startup the main 4h cycle runs before the monitor
        if _stop_monitor_stop.wait(_STOP_MONITOR_INTERVAL_SEC):
            break
        try:
            checked = renewed = filled = 0
            for bot_id, state in states_registry.items():
                positions = state.get("positions") or {}
                for symbol in list(positions.keys()):
                    position = positions.get(symbol)
                    if not position or not position.get("alpaca_stop_id"):
                        continue
                    checked += 1
                    try:
                        action, data = reconcile_broker_stop(symbol, position)
                        if action == "renewed":
                            renewed += 1
                        elif action == "filled":
                            _close_position_from_broker_fill(state, bot_id, symbol, float(data))
                            filled += 1
                    except Exception as e:
                        log(f"[STOP-MONITOR] {bot_id}/{symbol} échec: {e}", "WARN")
            if checked > 0:
                log(f"[STOP-MONITOR] {checked} stops vérifiés, "
                    f"{renewed} renouvelés, {filled} fillés (positions fermées)")
        except Exception as e:
            log(f"[STOP-MONITOR] iteration: {e}", "WARN")


def _next_cycle_utc() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for h in CYCLE_HOURS_UTC:
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    tomorrow = (now + timedelta(days=1)).replace(
        hour=CYCLE_HOURS_UTC[0], minute=0, second=0, microsecond=0
    )
    return tomorrow


# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    os.makedirs("logs/supertrend", exist_ok=True)
    os.makedirs("logs/momentum",   exist_ok=True)
    os.makedirs("logs/breakout",   exist_ok=True)
    os.makedirs("logs/trend",      exist_ok=True)
    os.makedirs("logs/vcb",             exist_ok=True)
    os.makedirs("logs/rs_leaders",      exist_ok=True)
    os.makedirs("logs/mean_reversion",  exist_ok=True)
    os.makedirs("logs/bot_z",           exist_ok=True)

    # ── Auto-sync capital depuis le broker (Alpaca > Kraken fallback) ──────
    # Plus de drift state vs broker : on prend la balance live à chaque boot
    # comme source de vérité, divisée entre les sub-bots actifs. Si states
    # n'existent pas (1er boot après reset), ils seront créés avec ce capital.
    global INITIAL_CAPITAL_PER_BOT
    broker_equity = None
    try:
        if getattr(config, "ALPACA_ENABLED", False):
            from live import alpaca_executor as _ax
            acct = _ax._request("GET", "/v2/account")
            broker_equity = float(acct.get("equity") or acct.get("cash") or 0)
            endpoint = "paper" if _ax._is_paper_endpoint() else "LIVE"
            log(f"💰 Broker equity (Alpaca {endpoint}): {broker_equity:.2f}$", "INFO")
    except Exception as e:
        log(f"⚠ Fetch broker equity échec: {e}", "WARN")

    if broker_equity and broker_equity > 0:
        active_count = max(len(config.ACTIVE_BOTS), 1)
        new_per_bot = round(broker_equity / active_count, 2)
        log(f"💰 Allocation dynamique : {broker_equity:.2f}$ / {active_count} bots actifs = {new_per_bot:.2f}$/bot", "INFO")
        config.INITIAL_CAPITAL = broker_equity
        config.INITIAL_CAPITAL_PER_BOT = new_per_bot
        INITIAL_CAPITAL_PER_BOT = new_per_bot

    log(f"{'='*60}", "INFO")
    log("  MULTI-BOT CONTEST STARTED", "INFO")
    log(f"  Bot A: Supertrend+MR      → logs/supertrend/state.json", "INFO")
    log(f"  Bot B: Momentum Rotation  → logs/momentum/state.json", "INFO")
    log(f"  Bot C: Donchian Breakout  → logs/breakout/state.json", "INFO")
    log(f"  Bot G: Trend Multi-Asset  → logs/trend/state.json", "INFO")
    log(f"  Bot H: VCB Breakout       → logs/vcb/state.json", "INFO")
    log(f"  Bot I: RS Leaders         → logs/rs_leaders/state.json", "INFO")
    log(f"  Bot J: Mean Reversion     → logs/mean_reversion/state.json", "INFO")
    log(f"  Capital initial: {INITIAL_CAPITAL_PER_BOT:.0f}$ × {len(config.ACTIVE_BOTS)} actifs = {INITIAL_CAPITAL_PER_BOT*len(config.ACTIVE_BOTS):.0f}$", "INFO")
    log(f"{'='*60}", "INFO")

    state_a = load_state_a()
    state_b = load_mom()
    state_c = load_brk()
    state_g = load_trd()
    state_h = load_vcb()
    state_i = load_rsl()
    state_j = load_mr()

    # ── Sync states fraîches avec capital dynamique (issu du broker) ────────
    # Chaque strategy a son INITIAL_CAPITAL hardcodé (1000) — on override pour
    # les states fraîches (sans trades, sans positions) avec INITIAL_CAPITAL_PER_BOT
    # calculé dynamiquement depuis Alpaca au boot. Préserve les states évolués.
    for _label, _st in [("B", state_b), ("C", state_c), ("G", state_g),
                         ("H", state_h), ("I", state_i), ("J", state_j)]:
        is_fresh = not _st.get("positions") and not _st.get("trades")
        if is_fresh:
            old_cap = _st.get("capital", 0)
            _st["capital"] = INITIAL_CAPITAL_PER_BOT
            _st["initial_capital"] = INITIAL_CAPITAL_PER_BOT
            log(f"[CAPITAL] Bot {_label} fresh state : {old_cap:.2f}$ → {INITIAL_CAPITAL_PER_BOT:.2f}$ (broker sync)")

    # ── Startup checks ──────────────────────────────────────────────────────
    if not config.PAPER_TRADING:
        from live.order_executor import startup_check, reconcile_positions
        log("⚠️  Mode LIVE Kraken — vérifications au démarrage...", "WARN")
        if not startup_check():
            log("⛔ Startup check ÉCHOUÉ — connexion Kraken impossible. Arrêt du bot.", "WARN")
            return
        log("✓ Startup check OK — connexion Kraken vérifiée", "OK")
        # Reconcile positions Kraken pour chaque bot actif
        for _bot_id, _state in [("a", state_a), ("b", state_b), ("c", state_c),
                                  ("g", state_g), ("h", state_h), ("i", state_i), ("j", state_j)]:
            reconcile_positions(_state, _bot_id)

    # Alpaca a son propre mode paper/live (via APCA_API_BASE_URL) — toujours vérifier
    if getattr(config, "ALPACA_ENABLED", False) and getattr(config, "STOCKS", []):
        from live import alpaca_executor as _ax
        log("⚠️  Univers contient des stocks — vérification Alpaca...", "WARN")
        if not _ax.startup_check():
            log("⛔ Alpaca startup ÉCHOUÉ — stocks indisponibles ce cycle.", "WARN")
        else:
            log("✓ Alpaca startup OK", "OK")

    _prev_budget   = {}   # Track previous budget for change detection
    z_summary      = None # BUG-01 : initialisé ici pour éviter NameError si Bot Z crashe au 1er cycle
    z_budget_alloc = {}   # BUG-03 : initialisé ici pour éviter spam Telegram après crash Bot Z
    _cycle_count   = 0    # 1er cycle = skip hard realign brutal (transitoire après restart)

    log(f"Bot A capital: {state_a['capital']:.2f}€ | Positions: {list(state_a['positions'].keys())}")
    log(f"Bot B capital: {state_b['capital']:.2f}€ | Positions: {list(state_b['positions'].keys())}")
    log(f"Bot C capital: {state_c['capital']:.2f}€ | Positions: {list(state_c['positions'].keys())}")
    log(f"Bot G capital: {state_g['capital']:.2f}€ | Positions: {list(state_g['positions'].keys())}")
    log(f"Bot H capital: {state_h['capital']:.2f}€ | Positions: {list(state_h['positions'].keys())}")
    log(f"Bot I capital: {state_i['capital']:.2f}€ | Positions: {list(state_i['positions'].keys())}")
    log(f"Bot J capital: {state_j['capital']:.2f}€ | Positions: {list(state_j['positions'].keys())}")

    # ── Démarre le monitor stops 15min en daemon thread ─────────────────────
    # Comble le trou de protection nocturne entre les cycles 4h après expiration
    # des stops Alpaca DAY (fractional shares). Re-place automatiquement si expiré.
    _states_registry = {
        "A": state_a, "B": state_b, "C": state_c, "G": state_g,
        "H": state_h, "I": state_i, "J": state_j,
    }
    _monitor_thread = threading.Thread(
        target=_stop_monitor_loop,
        args=(_states_registry,),
        daemon=True,
        name="stop-monitor",
    )
    _monitor_thread.start()

    while True:
        try:
            log(f"\n--- Cycle {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

            from live.notifier import resend_pending_alerts
            resend_pending_alerts()

            # ── 1. Shared macro data (one fetch for all bots) ─────────────────
            log("Fetching shared macro context...")
            macro = fetch_macro_context()

            btc_context   = macro.get("btc_context", {})
            vix           = macro.get("vix", 0.0)
            vix_factor    = macro.get("vix_factor", 1.0)
            fear_greed    = macro.get("fear_greed", {"score": 50, "label": "Neutral"})
            funding_rates = macro.get("funding_rates", {})
            macro_news    = macro.get("macro_news", [])
            qqq_ok        = macro.get("qqq_regime_ok", True)
            qqq_desc      = macro.get("qqq_description", "N/A")

            log(
                f"BTC: {btc_context.get('btc_price', '?')}€ ({btc_context.get('btc_trend', '?')}) | "
                f"VIX: {vix:.1f} (×{vix_factor}) | "
                f"F&G: {fear_greed.get('score', '?')}/100 | "
                f"QQQ: {'✓' if qqq_ok else '✗'}"
            )

            # ── 2. Pre-fetch OHLCV 4h for Bot A + Bot H VCB (55 days) ──────────
            # Bot H (VCB) requires SMA200 + BB_PERCENTILE_LOOKBACK(100) + 10 = 310 bars
            # 55 days × 6 bars/day = 330 bars > 310 minimum
            log(f"Pre-fetching 4h OHLCV ({len(config.SYMBOLS)} symbols, 55 days)...")
            ohlcv_4h = fetch_ohlcv_cache(config.SYMBOLS, timeframe="4h", days=55)
            log(f"4h cache: {len(ohlcv_4h)}/{len(config.SYMBOLS)} symbols ready")

            # ── 3. Pre-fetch daily OHLCV for Bots B & C (220 days) ───────────
            log(f"Pre-fetching daily OHLCV ({len(config.SYMBOLS)} symbols, 220 days)...")
            ohlcv_daily = fetch_ohlcv_cache(config.SYMBOLS, timeframe="1d", days=400)
            log(f"Daily cache: {len(ohlcv_daily)}/{len(config.SYMBOLS)} symbols ready")

            # Breadth indicator (régime continu, recycle ohlcv_daily)
            breadth_data = compute_breadth(ohlcv_daily)
            macro["breadth"] = breadth_data["breadth"]
            log(f"Breadth: {breadth_data['breadth']*100:.0f}% ({breadth_data['symbols_above']}/{breadth_data['symbols_total']} > SMA200)")

            # ── 3b. Re-sync capital broker à chaque cycle (anti-drift) ────────
            _active_for_sync = []
            if "a" in config.ACTIVE_BOTS: _active_for_sync.append(("A", state_a))
            if "b" in config.ACTIVE_BOTS: _active_for_sync.append(("B", state_b))
            if "c" in config.ACTIVE_BOTS: _active_for_sync.append(("C", state_c))
            if "g" in config.ACTIVE_BOTS: _active_for_sync.append(("G", state_g))
            if "h" in config.ACTIVE_BOTS: _active_for_sync.append(("H", state_h))
            if "i" in config.ACTIVE_BOTS: _active_for_sync.append(("I", state_i))
            if "j" in config.ACTIVE_BOTS: _active_for_sync.append(("J", state_j))
            broker_equity_now, realigned = _sync_broker_capital_periodic(_active_for_sync, ohlcv_daily, is_first_cycle=(_cycle_count == 0))
            _cycle_count += 1
            if realigned:
                # Persist états réalignés avant Bot Z (sinon Bot Z lit l'ancien capital)
                save_state_a(state_a); save_mom(state_b); save_brk(state_c); save_trd(state_g)
                save_vcb(state_h); save_rsl(state_i); save_mr(state_j)

            # ── 4. Bot Z — Pilot (allocation dispatch AVANT les sub-bots) ────────
            try:
                # Passe ohlcv_daily pour mark-to-market réel des positions
                # broker_equity_now : anti-drift cumulé sur z_capital
                z_summary = run_bot_z_cycle(macro, ohlcv=ohlcv_daily, broker_equity=broker_equity_now)
                print_bot_z_summary(z_summary)
                log(f"[Z] Engine: {z_summary.get('current_engine','?')} | "
                    f"Capital: {z_summary.get('z_capital_eur', z_summary.get('total_simulated_eur',0)):.2f}€ | "
                    f"Régime: {z_summary.get('regime','?')} | MTM: {'live' if z_summary.get('mtm_live') else 'entry-price'} | "
                    f"Budget: {z_summary.get('budget',{})}")

                # Budget dispatch Bot Z → sub-bots
                # Protection : sanity cap dans bot_z.py empêche z_capital aberrant
                # si Bot Z crashe (weighted_return > 15% → recalage sur ratio réel).
                z_budget_alloc = z_summary.get("budget", {})
                if z_budget_alloc:
                    # Tag bot_id pour les logs de _apply_z_budget (injection capital mort)
                    state_a["_bot_id"] = "A"
                    state_b["_bot_id"] = "B"
                    state_c["_bot_id"] = "C"
                    state_g["_bot_id"] = "G"
                    # ── KILL SWITCH GLOBAL (live uniquement) ──
                    # Si Bot Z portfolio chute ≤ KILL_SWITCH_PCT (-10% par défaut), gèle tous
                    # les bots pour bloquer les nouvelles entrées. Positions ouvertes conservées
                    # — tu dois décider manuellement de les fermer ou pas.
                    z_perf = z_summary.get("perf_pct", 0) / 100.0
                    kill_switch_active = (
                        not config.PAPER_TRADING
                        and z_perf <= config.KILL_SWITCH_PCT
                        and not state.get("kill_switch_triggered", False)
                    )
                    if kill_switch_active:
                        log(f"⛔ KILL SWITCH GLOBAL : Bot Z {z_perf*100:+.2f}% ≤ {config.KILL_SWITCH_PCT*100:+.0f}%", "WARN")
                        for s in (state_a, state_b, state_c, state_g, state_h, state_i, state_j):
                            if isinstance(s, dict):
                                s["dd_frozen"] = True
                        state["kill_switch_triggered"] = True
                        try:
                            from live.notifier import notify
                            notify(f"🚨 <b>KILL SWITCH GLOBAL ACTIVÉ</b>\n"
                                   f"Bot Z perf : <b>{z_perf*100:+.2f}%</b> (seuil {config.KILL_SWITCH_PCT*100:+.0f}%)\n"
                                   f"Tous les bots gelés — intervention manuelle requise.")
                        except Exception:
                            pass

                    # Default 0 (pas INITIAL_CAPITAL_PER_BOT) : si bot pas dans VALID_BOTS,
                    # il ne reçoit pas le capital initial par défaut → pas de capital fantôme.
                    # _apply_z_budget skip si budget=0 (via flag _below_min_order).
                    if "a" in config.ACTIVE_BOTS:
                        state_a = _apply_z_budget(state_a, z_budget_alloc.get("a", 0), ohlcv_daily)
                    if "b" in config.ACTIVE_BOTS:
                        state_b = _apply_z_budget(state_b, z_budget_alloc.get("b", 0), ohlcv_daily)
                    if "c" in config.ACTIVE_BOTS:
                        state_c = _apply_z_budget(state_c, z_budget_alloc.get("c", 0), ohlcv_daily)
                    if "g" in config.ACTIVE_BOTS:
                        state_g = _apply_z_budget(state_g, z_budget_alloc.get("g", 0), ohlcv_daily)

                    # Sauvegarder immédiatement : persistance même si un bot crashe ensuite
                    save_state_a(state_a)
                    save_mom(state_b)
                    save_brk(state_c)
                    save_trd(state_g)

                    log(f"[Z→] Budget dispatché — A:{z_budget_alloc.get('a',0):.0f}€ "
                        f"B:{z_budget_alloc.get('b',0):.0f}€ "
                        f"C:{z_budget_alloc.get('c',0):.0f}€ "
                        f"G:{z_budget_alloc.get('g',0):.0f}€")

                    # Notifier si changement significatif (>15%) par rapport au cycle précédent
                    if _prev_budget:
                        budget_changed = any(
                            abs(z_budget_alloc.get(b, 0) - _prev_budget.get(b, 0)) / max(_prev_budget.get(b, 1), 1) > 0.15
                            for b in z_budget_alloc
                        )
                        if budget_changed:
                            from live.notifier import notify_z_dispatch
                            notify_z_dispatch(
                                z_budget_alloc,
                                z_summary.get('z_capital_eur', 10000),
                                z_summary.get('current_engine', '?'),
                                prev_budget=_prev_budget,
                                perf_pct=z_summary.get('perf_pct', 0),
                            )
                    _prev_budget = dict(z_budget_alloc)

            except Exception as ez:
                log(f"Bot Z erreur (non bloquant): {ez}", "WARN")

            # Injecter l'engine Bot Z dans macro pour H/I/J (filtre régime sans dispatch capital)
            macro["bot_z_engine"] = z_summary.get("current_engine", "BALANCED") if z_summary else "BALANCED"

            # ── 4b. Portfolio exposure cap — suspension si > 80% ─────────────
            MAX_PORTFOLIO_EXPOSURE = 0.80
            all_active = [("A", state_a), ("B", state_b), ("C", state_c),
                          ("G", state_g), ("H", state_h), ("I", state_i), ("J", state_j)]
            total_pos_value = sum(
                sum(p.get("entry", 0) * p.get("size", 0) for p in s.get("positions", {}).values())
                for _, s in all_active
            )
            total_capital = sum(s.get("initial_capital", INITIAL_CAPITAL_PER_BOT) for _, s in all_active)
            exposure_pct = total_pos_value / total_capital if total_capital > 0 else 0
            exposure_high = exposure_pct > MAX_PORTFOLIO_EXPOSURE
            if exposure_high:
                from live.notifier import notify_exposure_high
                sector_counts = {}
                for _, s in all_active:
                    for sym in s.get("positions", {}):
                        sec = config.SECTORS.get(sym, "other")
                        sector_counts[sec] = sector_counts.get(sec, 0) + 1
                details = " | ".join(f"{s}:{n}" for s, n in sorted(sector_counts.items()))
                notify_exposure_high(exposure_pct * 100, details)
                log(f"⚠ Exposition portfolio {exposure_pct*100:.0f}% > {MAX_PORTFOLIO_EXPOSURE*100:.0f}% — nouvelles entrées suspendues", "WARN")
            # Passer le flag aux bots pour bloquer les nouvelles entrées
            macro["exposure_blocked"] = exposure_high

            # ── 5. Bot A: Supertrend + filters ────────────────────────────────
            log(f"\n{Fore.CYAN}--- Bot A: Supertrend+MR ---{Style.RESET_ALL}")
            state_a["_exposure_blocked"] = exposure_high

            rotation = bot_a._compute_rotation_factors(state_a.get("trades", []))
            momentum_filter = bot_a._update_momentum_filter(state_a)

            for symbol in config.SYMBOLS:
                is_crypto = symbol in config.CRYPTO
                # Momentum filter: xStocks only
                if (not is_crypto
                        and not momentum_filter.get(symbol, True)
                        and symbol not in state_a["positions"]):
                    log(f"[A] {symbol} — Skip (momentum 90j négatif)")
                    continue

                df_4h = ohlcv_4h.get(symbol)
                category = "xstock" if symbol in config.XSTOCKS else "crypto"
                combined = round(vix_factor * rotation[category], 2)
                fr = funding_rates.get(symbol, 0.0)

                state_a = bot_a.process_symbol(
                    symbol, state_a,
                    df=df_4h,
                    btc_context=btc_context,
                    vix_factor=combined,
                    vix=vix,
                    fear_greed=fear_greed,
                    funding_rate=fr,
                    macro_news=macro_news,
                    qqq_regime_ok=qqq_ok,
                    qqq_description=qqq_desc,
                    ohlcv_daily=ohlcv_daily,  # BUG-11 : évite fetch réseau redondant dans _confirm_daily_trend
                    btc_dominance_up=macro.get("btc_dominance", {}).get("trend_up", False),
                )
                time.sleep(1)

            save_state_a(state_a)
            log(
                f"[A] Capital: {state_a['capital']:.2f}€ | "
                f"Positions: {list(state_a['positions'].keys())} | "
                f"Trades: {len(state_a['trades'])}"
            )

            # ── 6. Bot B: Momentum Rotation ───────────────────────────────────
            if "b" in config.ACTIVE_BOTS:
                log(f"\n{Fore.GREEN}--- Bot B: Momentum Rotation ---{Style.RESET_ALL}")
                state_b = run_momentum_cycle(state_b, ohlcv_daily, macro)
                save_mom(state_b)
                log(f"[B] Capital: {state_b['capital']:.2f}€ | Holdings: {list(state_b['positions'].keys())} | Trades: {len(state_b['trades'])}")
            else:
                log(f"\n{Fore.GREEN}--- Bot B: désactivé (ACTIVE_BOTS) ---{Style.RESET_ALL}")

            # ── 7. Bot C: Donchian Breakout ───────────────────────────────────
            if "c" in config.ACTIVE_BOTS:
                log(f"\n{Fore.YELLOW}--- Bot C: Donchian Breakout ---{Style.RESET_ALL}")
                brk_cache = {s: ohlcv_daily.get(s) for s in BREAKOUT_SYMBOLS if s in ohlcv_daily}
                state_c = run_breakout_cycle(state_c, brk_cache, macro)
                save_brk(state_c)
                log(f"[C] Capital: {state_c['capital']:.2f}€ | Positions: {list(state_c['positions'].keys())} | Trades: {len(state_c['trades'])}")
            else:
                log(f"\n{Fore.YELLOW}--- Bot C: désactivé (ACTIVE_BOTS) ---{Style.RESET_ALL}")

            # ── 8. Bot G: Trend Following Multi-Asset ─────────────────────────
            if "g" in config.ACTIVE_BOTS:
                log(f"\n{Fore.CYAN}--- Bot G: Trend Following Multi-Asset ---{Style.RESET_ALL}")
                state_g = run_trend_cycle(state_g, ohlcv_daily, macro)
                save_trd(state_g)
                log(f"[G] Capital: {state_g['capital']:.2f}€ | Positions: {list(state_g['positions'].keys())} | Trades: {len(state_g['trades'])}")
            else:
                log(f"\n{Fore.CYAN}--- Bot G: désactivé (ACTIVE_BOTS) ---{Style.RESET_ALL}")

            # ── 12. Bot H: VCB Breakout ───────────────────────────────────────
            if "h" in config.ACTIVE_BOTS:
                log(f"\n{Fore.RED}--- Bot H: VCB Breakout ---{Style.RESET_ALL}")
                state_h = run_vcb_cycle(state_h, ohlcv_4h, macro)
                save_vcb(state_h)
                log(f"[H] Capital: {state_h['capital']:.2f}€ | Positions: {list(state_h['positions'].keys())} | Trades: {len(state_h['trades'])}")
            else:
                log(f"\n{Fore.RED}--- Bot H: désactivé (ACTIVE_BOTS) ---{Style.RESET_ALL}")

            # ── 13. Bot I: RS Leaders ─────────────────────────────────────────
            if "i" in config.ACTIVE_BOTS:
                log(f"\n{Fore.CYAN}--- Bot I: RS Leaders ---{Style.RESET_ALL}")
                state_i = run_rs_leaders_cycle(state_i, ohlcv_daily, macro)
                save_rsl(state_i)
                log(f"[I] Capital: {state_i['capital']:.2f}€ | Positions: {list(state_i['positions'].keys())} | Trades: {len(state_i['trades'])}")
            else:
                log(f"\n{Fore.CYAN}--- Bot I: désactivé (ACTIVE_BOTS) ---{Style.RESET_ALL}")

            # ── 14. Bot J: Mean Reversion ─────────────────────────────────────
            if "j" in config.ACTIVE_BOTS:
                log(f"\n{Fore.WHITE}--- Bot J: Mean Reversion ---{Style.RESET_ALL}")
                state_j = run_mr_cycle(state_j, ohlcv_daily, macro)
                save_mr(state_j)
                log(f"[J] Capital: {state_j['capital']:.2f}€ | Positions: {list(state_j['positions'].keys())} | Trades: {len(state_j['trades'])}")
            else:
                log(f"\n{Fore.WHITE}--- Bot J: désactivé (ACTIVE_BOTS) ---{Style.RESET_ALL}")

            # ── 15. Contest summary ───────────────────────────────────────────
            print_contest_status(state_a, state_b, state_c, state_g, state_h, state_i, state_j, ohlcv_daily)

            # ── Cycle summary Telegram (résumé compact chaque cycle) ──────────
            if z_summary:
                try:
                    from live.notifier import notify_cycle_summary
                    _blocked_engines = ("SHIELD", "PRO")
                    _z_engine = z_summary.get("current_engine", "BALANCED")
                    obs_bots_info = {
                        "h": {
                            "total_trades": len(state_h.get("trades", [])),
                            "open_trades":  len(state_h.get("positions", {})),
                            "blocked":      _z_engine in _blocked_engines,
                        },
                        "i": {
                            "total_trades": len(state_i.get("trades", [])),
                            "open_trades":  len(state_i.get("positions", {})),
                            "blocked":      _z_engine in _blocked_engines,
                        },
                        "j": {
                            "total_trades": len(state_j.get("trades", [])),
                            "open_trades":  len(state_j.get("positions", {})),
                            "blocked":      _z_engine in _blocked_engines,
                        },
                    }
                    main_bots_info = {
                        "a": {"positions": len(state_a.get("positions", {})), "dd_frozen": state_a.get("dd_frozen", False)},
                        "b": {"positions": len(state_b.get("positions", {})), "dd_frozen": state_b.get("dd_frozen", False)},
                        "c": {"positions": len(state_c.get("positions", {})), "dd_frozen": state_c.get("dd_frozen", False)},
                        "g": {"positions": len(state_g.get("positions", {})), "dd_frozen": state_g.get("dd_frozen", False)},
                    }
                    notify_cycle_summary(
                        engine    = _z_engine,
                        vix       = z_summary.get("last_regime_info", {}).get("vix", vix or 0),
                        regime    = z_summary.get("regime", "?"),
                        z_capital = z_summary.get("z_capital_eur", 10000),
                        perf_pct  = z_summary.get("perf_pct", 0),
                        budget    = z_summary.get("budget", {}),
                        obs_bots  = obs_bots_info,
                        main_bots = main_bots_info,
                    )
                except Exception as _e:
                    log(f"[notify_cycle_summary] erreur: {_e}", "WARN")

            # ── 15b. Daily health report (1x/jour au cycle 19h UTC = 21h Paris) ──
            _now_utc = datetime.now(timezone.utc)
            if _now_utc.hour == 19 and z_summary:
                try:
                    from live.notifier import notify_daily_health
                    _all_bots = [
                        ("A", "Supertrend", state_a), ("B", "Momentum", state_b),
                        ("C", "Breakout", state_c), ("G", "Trend CTA", state_g),
                        ("H", "VCB", state_h), ("I", "RS Leaders", state_i), ("J", "MeanRev", state_j),
                    ]
                    _bots_status = []
                    for _bid, _bname, _bstate in _all_bots:
                        _bval = _portfolio_value(_bstate, ohlcv_daily)
                        _binit = _bstate.get("original_capital", _bstate.get("initial_capital", INITIAL_CAPITAL_PER_BOT))
                        _bpnl = ((_bval - _binit) / _binit * 100) if _binit > 0 else 0
                        _bots_status.append({
                            "id": _bid, "name": _bname,
                            "capital": _bval,
                            "positions": len(_bstate.get("positions", {})),
                            "trades": len(_bstate.get("trades", [])),
                            "dd_frozen": _bstate.get("dd_frozen", False),
                            "pnl_pct": _bpnl,
                        })
                    notify_daily_health(
                        _bots_status,
                        z_capital=z_summary.get("z_capital_eur", 10000),
                        engine=z_summary.get("current_engine", "?"),
                        days_running=z_summary.get("days_running", 0),
                    )
                except Exception as _e:
                    log(f"[daily_health] erreur: {_e}", "WARN")

            # ── 16. Drawdown checks ───────────────────────────────────────────
            # DD baseline = z_budget_eur (allocation cible actuelle Bot Z) ou
            # original_capital pour les bots non-pilotés (H/I/J). Le z_budget
            # reflète la sous/sur-pondération régime — un bot sous-pondéré n'est
            # PAS en drawdown, juste alloué moins. Évite les faux freezes en BULL.
            UNFREEZE_DD = -0.08  # -8% : marge de 7% depuis seuil -15%
            for name, state in [("A", state_a), ("B", state_b), ("C", state_c), ("G", state_g), ("H", state_h), ("I", state_i), ("J", state_j)]:
                total = _portfolio_value(state, ohlcv_daily)
                init = state.get("z_budget_eur") or state.get("original_capital") or state.get("initial_capital", INITIAL_CAPITAL_PER_BOT)
                dd = (total - init) / init if init > 0 else 0
                was_frozen = state.get("dd_frozen", False)
                if dd <= config.MAX_DRAWDOWN and not was_frozen:
                    state["dd_frozen"] = True
                    log(f"⛔ Bot {name}: MAX DRAWDOWN {dd*100:.1f}% — bot gelé (positions conservées)", "WARN")
                    from live.notifier import notify_bot_frozen
                    notify_bot_frozen(
                        bot_id=name,
                        dd=dd,
                        threshold=config.MAX_DRAWDOWN,
                        state=state,
                        vix=(z_summary.get("last_regime_info", {}).get("vix", vix) if z_summary else vix),
                        regime=(z_summary.get("regime") if z_summary else None),
                        engine=(z_summary.get("current_engine") if z_summary else None),
                        unfreeze_threshold=UNFREEZE_DD,
                    )
                elif was_frozen and dd > UNFREEZE_DD:
                    state["dd_frozen"] = False
                    log(f"🔥 Bot {name}: dégelé (DD={dd*100:.1f}% > {UNFREEZE_DD*100:.0f}%)", "INFO")
                    from live.notifier import notify_bot_unfrozen
                    notify_bot_unfrozen(
                        bot_id=name,
                        dd=dd,
                        unfreeze_threshold=UNFREEZE_DD,
                        state=state,
                    )

            # ── 17. Snapshot journalier pour Bot A ────────────────────────────
            bot_a._check_daily_snapshot(state_a)

            # ── 18. Wait for next cycle ───────────────────────────────────────
            next_run = _next_cycle_utc()
            wait_sec = max(0, (next_run - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds())
            log(
                f"Prochain cycle: {next_run.strftime('%H:%M UTC')} "
                f"(dans {int(wait_sec // 60)} min)"
            )
            time.sleep(wait_sec)

        except KeyboardInterrupt:
            log("Multi-bot arrêté manuellement.")
            save_state_a(state_a)
            save_mom(state_b)
            save_brk(state_c)
            save_trd(state_g)
            save_vcb(state_h)
            save_rsl(state_i)
            save_mr(state_j)
            break
        except Exception as e:
            log(f"Erreur inattendue: {e}", "WARN")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    run()

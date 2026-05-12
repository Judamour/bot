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
import urllib.request
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

def _validate_state(state: dict, bot_id: str = "?") -> bool:
    """Vérifie structure minimale d'un state. Backup le fichier en .corrupt si invalide.
    Retourne True si state utilisable, False si fallback nécessaire."""
    if not isinstance(state, dict):
        return False
    if "capital" not in state or not isinstance(state.get("capital"), (int, float)):
        return False
    if "positions" not in state or not isinstance(state.get("positions"), dict):
        return False
    if "trades" not in state or not isinstance(state.get("trades"), list):
        return False
    # Sanity bounds : capital négatif extrême = corruption (NaN/inf serialized)
    cap = state["capital"]
    if cap != cap or cap < -1e9 or cap > 1e12:  # NaN ou bornes absurdes
        return False
    return True


def _load_state_safe(path: str, bot_id: str, default: dict) -> dict:
    """Load avec validation. Si corrupt → backup .corrupt-<ts>.bak + return default."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            state = json.load(f)
        if _validate_state(state, bot_id):
            return state
        log(f"[STATE] Bot {bot_id} : state.json invalide → backup + fresh default", "WARN")
    except Exception as e:
        log(f"[STATE] Bot {bot_id} : load échoué ({e}) → backup + fresh default", "WARN")
    # Backup le fichier corrompu
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = f"{path}.corrupt-{ts}.bak"
        os.replace(path, backup_path)
        from live.notifier import notify
        notify(f"⚠️ <b>STATE CORRUPT</b>\nBot {bot_id} : {path}\n→ {backup_path}\nReset au capital initial")
    except Exception:
        pass
    return default


def load_state_a() -> dict:
    return _load_state_safe(STATE_A_FILE, "A", {
        "capital": INITIAL_CAPITAL_PER_BOT,
        "positions": {},
        "trades": [],
        "initial_capital": INITIAL_CAPITAL_PER_BOT,
    })


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

# ── Reconcile Alpaca positions ↔ states (au boot) ──────────────────────────

def _reconcile_alpaca_positions(states_registry: dict) -> int:
    """
    Au boot : compare positions broker (Alpaca) vs positions states. Trois cas :
    - State a position que Alpaca n'a pas → broker a vendu (manual, margin,
      stop fillé pendant offline, etc.) → ferme la position dans state au prix
      de stop (ou entry si stop indisponible), log trade reason='reconcile_missing'
    - Alpaca a position que aucun state n'a → manual buy ou import → log warning,
      ne rien toucher (hors scope bot)
    - Qty mismatch → broker = source de vérité, ajuster state qty + log
    Retourne nombre de divergences corrigées.
    """
    from live import alpaca_executor
    broker_positions = alpaca_executor.list_positions()
    if not broker_positions:
        # Soit pas de positions broker, soit erreur réseau. Si erreur, list_positions
        # log déjà un warning. On ne bloque pas le boot.
        # Vérifier quand même si un state a des positions Alpaca-routées qui n'existent
        # plus → ce sont des candidates au close-reconcile.
        pass

    # Symbols Alpaca-routés présents dans les states
    state_symbols_by_bot = {}
    for bot_id, state in states_registry.items():
        positions = state.get("positions") or {}
        state_symbols_by_bot[bot_id] = {
            s: p for s, p in positions.items()
            if alpaca_executor.is_alpaca_routed(s)
        }

    fixes = 0
    from datetime import datetime as _dt
    from live.notifier import notify

    # 1. Positions in state but not in broker
    for bot_id, positions in state_symbols_by_bot.items():
        for symbol, pos in list(positions.items()):
            if symbol in broker_positions:
                continue
            # Broker n'a plus la position → fermer dans state
            close_price = float(pos.get("stop") or pos.get("entry") or 0)
            if close_price <= 0:
                log(f"[RECONCILE] {bot_id}/{symbol} : broker absent, prix close inconnu — skip", "WARN")
                continue
            size = float(pos.get("size", 0))
            entry = float(pos.get("entry", close_price))
            cost = float(pos.get("cost", entry * size))
            proceeds = close_price * size * (1 - config.EXCHANGE_FEE)
            pnl = proceeds - cost
            state = states_registry[bot_id]
            state["capital"] = state.get("capital", 0) + proceeds
            state.setdefault("trades", []).append({
                "symbol": symbol,
                "entry_date": pos.get("date"),
                "exit_date": datetime.now(timezone.utc).isoformat(),
                "entry_price": entry,
                "exit_price": close_price,
                "pnl": round(pnl, 2),
                "reason": "reconcile_missing",
                "result": "win" if pnl > 0 else "loss",
            })
            state["positions"].pop(symbol, None)
            log(f"[RECONCILE] Bot {bot_id}/{symbol} : broker absent → close @ {close_price:.4f} | PnL {pnl:+.2f}", "WARN")
            notify(f"⚠️ <b>RECONCILE</b>\nBot {bot_id} — {symbol}\nbroker absent (margin/manual)\nclosed @ {close_price:.4f} | PnL <b>{pnl:+.2f}</b>")
            fixes += 1

    # 2. Qty mismatch
    # Utiliser `qty` (total holdings), PAS `qty_available` qui exclut les qty
    # bloquées par les stops ouverts (qty_available ≈ 0 quand un stop couvre 100%
    # de la position — ce qui est notre cas normal).
    for bot_id, positions in state_symbols_by_bot.items():
        for symbol, pos in positions.items():
            if symbol not in broker_positions:
                continue
            broker_qty = float(broker_positions[symbol].get("qty") or 0)
            state_qty = float(pos.get("size", 0))
            # BAC est dans Bot A (147.7) ET Bot G (38.8) → broker agrège (186.5).
            # Skip mismatch check si symbole partagé entre plusieurs bots :
            # somme des state_qty ≈ broker_qty est attendu.
            shared = sum(
                1 for _b, _ps in state_symbols_by_bot.items()
                if symbol in _ps
            ) > 1
            if shared:
                continue
            # Tolérance 0.5% (rounding broker)
            if state_qty > 0 and abs(broker_qty - state_qty) / state_qty > 0.005:
                log(f"[RECONCILE] Bot {bot_id}/{symbol} : qty state={state_qty} vs broker={broker_qty} — ajusté à broker", "WARN")
                pos["size"] = broker_qty
                fixes += 1

    # 3. Broker positions not in any state (informational only)
    all_state_symbols = set()
    for positions in state_symbols_by_bot.values():
        all_state_symbols.update(positions.keys())
    for sym in broker_positions:
        if sym not in all_state_symbols:
            qty = broker_positions[sym].get("qty", "?")
            log(f"[RECONCILE] Broker a {sym} (qty={qty}) hors scope bot — ignoré", "INFO")

    if fixes > 0:
        log(f"[RECONCILE] {fixes} divergence(s) corrigée(s)")
    else:
        log(f"[RECONCILE] States ↔ broker synchronisés (0 divergence)")
    return fixes


# ── Detect "permanent" SELL errors (delisted/suspended/halted) ──────────────

_PERMANENT_SELL_ERROR_PATTERNS = (
    "asset is not active",
    "asset not found",
    "not tradable",
    "halted",
    "suspended",
    "delisted",
    "no position",
)


def _is_permanent_sell_error(err_msg: str) -> bool:
    if not err_msg:
        return False
    low = err_msg.lower()
    return any(p in low for p in _PERMANENT_SELL_ERROR_PATTERNS)


# ── Max trades per day (anti-spam) ──────────────────────────────────────────

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "20"))


def _trades_today_total(states: list) -> int:
    """Compte les trades exécutés aujourd'hui UTC à travers tous les bots."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    for s in states:
        for t in s.get("trades", []) or []:
            d = str(t.get("exit_date") or "")[:10]
            if d == today:
                n += 1
    return n


# ── Archive trades (anti-bloat state.json) ──────────────────────────────────

KEEP_LAST_N_TRADES = int(os.getenv("KEEP_LAST_N_TRADES", "500"))


def _archive_trades_if_needed(state: dict, bot_id: str) -> int:
    """Si state["trades"] dépasse KEEP_LAST_N_TRADES, archive les plus vieux dans
    logs/archive/<bot>_trades_<YYYY-MM>.jsonl et tronque le state.
    Retourne le nombre de trades archivés."""
    trades = state.get("trades") or []
    if len(trades) <= KEEP_LAST_N_TRADES:
        return 0
    excess = len(trades) - KEEP_LAST_N_TRADES
    to_archive = trades[:excess]
    keep = trades[excess:]
    os.makedirs("logs/archive", exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m")
    archive_path = f"logs/archive/{bot_id}_trades_{today}.jsonl"
    with open(archive_path, "a") as f:
        for t in to_archive:
            f.write(json.dumps(t, default=str) + "\n")
    state["trades"] = keep
    log(f"[ARCHIVE] Bot {bot_id} : {excess} trades archivés → {archive_path}", "INFO")
    return excess


# ── Cross-bot symbol exclusivity ────────────────────────────────────────────
# Évite que 2 bots détiennent le même symbole : le broker agrège, les stops
# trailing se conflictent, le PnL state diverge. Compute la liste des symboles
# déjà détenus, propagée via macro["held_by_other_bots"] pour chaque bot.

def _compute_held_symbols(states: list) -> dict:
    """Retourne {symbol: True} pour tous les symboles tenus par un sub-bot."""
    held = {}
    for state in states:
        for sym in (state.get("positions") or {}):
            held[sym] = True
    return held


# ── Inactivity alert (bot silencieux trop longtemps) ────────────────────────

INACTIVITY_ALERT_HOURS = int(os.getenv("INACTIVITY_ALERT_HOURS", "168"))  # 7 jours
INACTIVITY_FILE = "logs/bot_z/inactivity.json"


def _check_inactivity_alert(states: list) -> None:
    """Si aucun trade exit_date dans tous les bots depuis INACTIVITY_ALERT_HOURS,
    notify Telegram une fois (dedup via fichier d'état). Reset au prochain trade."""
    last_exit = None
    for s in states:
        for t in s.get("trades", []) or []:
            exit_date = str(t.get("exit_date") or "")[:19]  # YYYY-MM-DD HH:MM:SS
            if not exit_date:
                continue
            try:
                d = datetime.fromisoformat(exit_date)
                if last_exit is None or d > last_exit:
                    last_exit = d
            except Exception:
                continue

    if last_exit is None:
        return  # Pas de trades du tout, nothing to alert (bot fresh)

    # Make timezone-aware for compare
    if last_exit.tzinfo is None:
        last_exit = last_exit.replace(tzinfo=timezone.utc)
    hours_since = (datetime.now(timezone.utc) - last_exit).total_seconds() / 3600

    state_alert = {}
    if os.path.exists(INACTIVITY_FILE):
        try:
            with open(INACTIVITY_FILE) as f:
                state_alert = json.load(f)
        except Exception:
            state_alert = {}

    last_iso = last_exit.isoformat()
    already_alerted_for = state_alert.get("alerted_for_last_exit")

    if hours_since >= INACTIVITY_ALERT_HOURS and already_alerted_for != last_iso:
        from live.notifier import notify
        log(f"⚠ INACTIVITY: aucun trade depuis {hours_since:.0f}h (seuil {INACTIVITY_ALERT_HOURS}h)", "WARN")
        notify(f"⚠️ <b>BOT INACTIVITY</b>\n"
               f"Aucun trade depuis <b>{hours_since:.0f}h</b>\n"
               f"Dernier exit : {last_exit.strftime('%Y-%m-%d %H:%M UTC')}\n"
               f"Vérifier si stratégies bloquées (régime/filter)")
        state_alert["alerted_for_last_exit"] = last_iso
        os.makedirs(os.path.dirname(INACTIVITY_FILE), exist_ok=True)
        with open(INACTIVITY_FILE, "w") as f:
            json.dump(state_alert, f)


# ── Daily circuit breaker (-3% par jour) ────────────────────────────────────

DAILY_BREAKER_FILE = "logs/bot_z/daily_breaker.json"
DAILY_BREAKER_THRESHOLD = -0.03  # -3% intraday → block new entries


def _daily_breaker_check(current_equity: float) -> bool:
    """
    Track equity journalière, déclenche breaker à -3% sur la journée UTC.

    Persiste dans logs/bot_z/daily_breaker.json :
        {"date": "YYYY-MM-DD", "anchor_equity": 101000.50, "triggered": false}

    Retourne True si breaker actif (= bloquer nouvelles entrées).
    Reset auto au passage de minuit UTC.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {}
    if os.path.exists(DAILY_BREAKER_FILE):
        try:
            with open(DAILY_BREAKER_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {}

    # Reset au changement de jour
    if state.get("date") != today:
        state = {"date": today, "anchor_equity": current_equity, "triggered": False}
        os.makedirs(os.path.dirname(DAILY_BREAKER_FILE), exist_ok=True)
        with open(DAILY_BREAKER_FILE, "w") as f:
            json.dump(state, f, indent=2)
        return False

    anchor = state.get("anchor_equity", current_equity)
    if anchor <= 0:
        return False

    daily_pct = (current_equity - anchor) / anchor

    # Trigger sur seuil
    if daily_pct <= DAILY_BREAKER_THRESHOLD and not state.get("triggered"):
        state["triggered"] = True
        state["triggered_at"] = datetime.now(timezone.utc).isoformat()
        state["triggered_equity"] = current_equity
        with open(DAILY_BREAKER_FILE, "w") as f:
            json.dump(state, f, indent=2)
        from live.notifier import notify
        log(f"⛔ DAILY BREAKER déclenché : equity {current_equity:.2f}$ vs anchor {anchor:.2f}$ ({daily_pct*100:.2f}%) — block new entries jusqu'à minuit UTC", "WARN")
        notify(f"⛔ <b>DAILY CIRCUIT BREAKER</b>\n"
               f"Loss intraday : <b>{daily_pct*100:.2f}%</b>\n"
               f"Equity : {current_equity:.0f}$ (anchor {anchor:.0f}$)\n"
               f"Block new entries jusqu'à 00h UTC.\n"
               f"Positions ouvertes continuent leur stop normalement.")

    return bool(state.get("triggered"))


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
        "exit_date": datetime.now(timezone.utc).isoformat(),
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


_RECONCILE_DEDUP_FILE = "logs/bot_z/reconcile_alerts.json"
_RECONCILE_DEDUP_TTL_SEC = 24 * 3600
_RECONCILE_DRIFT_PCT_THRESHOLD = 0.01   # 1% écart relatif
_RECONCILE_VALUE_THRESHOLD_USD = 50     # ignore dust < $50


def _reconcile_broker_positions(states_registry: dict) -> None:
    """
    Compare les positions Alpaca au cumul des states bot. Alerte Telegram :
      - DRIFT : symbole où broker_qty ≠ sum(bot_qty) au-delà du seuil (1% / $50)
      - FANTÔME : position broker absente de tous les states (>$50)

    Détecte les bugs de synchro (cf. cas BAC du 04/05/2026 — position fantôme
    de 186 actions invisible au bot pendant 8 jours). Dédup 24h/symbole pour
    éviter de spammer.
    """
    from live import alpaca_executor as ax
    from live.notifier import notify
    import time as _time

    try:
        with open(_RECONCILE_DEDUP_FILE) as f:
            dedup = json.load(f)
    except Exception:
        dedup = {}
    now = _time.time()
    dedup = {k: v for k, v in dedup.items()
             if now - v < _RECONCILE_DEDUP_TTL_SEC}

    try:
        url = ax._base_url() + "/v2/positions"
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": ax._api_key(),
            "APCA-API-SECRET-KEY": ax._api_secret(),
        })
        broker_positions = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        log(f"[RECONCILE] fetch broker positions échec: {e}", "WARN")
        return

    # Bot interne : "SOL/USD" / "BTC/USD" ; Alpaca liste retourne "SOLUSD" / "BTCUSD"
    # → normaliser sur la forme sans slash pour comparer.
    def _norm(s: str) -> str:
        return (s or "").replace("/", "")

    bot_totals: dict[str, float] = {}
    for state in states_registry.values():
        for sym, p in (state.get("positions") or {}).items():
            if p and p.get("size"):
                key = _norm(sym)
                bot_totals[key] = bot_totals.get(key, 0) + float(p["size"])

    drift_count = orphan_count = 0
    for pos in broker_positions:
        sym = pos.get("symbol") or ""
        key = _norm(sym)
        # qty = position totale (incluant ce qui est réservé par les stops actifs).
        # qty_available est piégeux ici : quand un stop broker couvre 100% de la
        # position, qty_available ≈ 0 → comparaison faussement < bot_qty.
        broker_qty = float(pos.get("qty") or pos.get("qty_available") or 0)
        bot_qty = bot_totals.get(key, 0)
        mv = float(pos.get("market_value") or 0)
        if broker_qty <= 0:
            continue

        # Position fantôme : présente côté broker, absente côté bot
        if bot_qty == 0:
            if mv >= _RECONCILE_VALUE_THRESHOLD_USD:
                dkey = f"orphan:{key}"
                if dkey not in dedup:
                    notify(
                        f"🚨 <b>POSITION FANTÔME BROKER</b>\n"
                        f"{sym}: {broker_qty:.6f} (~{mv:.0f}$)\n"
                        f"Absente du state bot — inspection manuelle requise"
                    )
                    dedup[dkey] = now
                log(f"[RECONCILE] ORPHAN {sym}: qty={broker_qty:.6f} mv=${mv:.0f}", "WARN")
                orphan_count += 1
            continue

        # Drift : écart relatif > seuil ET valeur de l'écart > seuil
        diff = abs(broker_qty - bot_qty)
        rel = diff / broker_qty
        diff_value = diff * (mv / broker_qty)
        if rel > _RECONCILE_DRIFT_PCT_THRESHOLD and diff_value > _RECONCILE_VALUE_THRESHOLD_USD:
            if key not in dedup:
                notify(
                    f"🚨 <b>DRIFT BROKER ↔ BOT</b>\n"
                    f"{sym}: broker {broker_qty:.4f}, bot {bot_qty:.4f}\n"
                    f"Écart {diff:.4f} (~{diff_value:.0f}$)\n"
                    f"→ Inspection manuelle requise"
                )
                dedup[key] = now
            log(f"[RECONCILE] DRIFT {sym}: broker={broker_qty:.6f} "
                f"bot={bot_qty:.6f} diff=${diff_value:.0f}", "WARN")
            drift_count += 1

    if drift_count or orphan_count:
        log(f"[RECONCILE] {drift_count} drift(s), {orphan_count} fantôme(s) détecté(s)", "WARN")

    try:
        os.makedirs(os.path.dirname(_RECONCILE_DEDUP_FILE), exist_ok=True)
        with open(_RECONCILE_DEDUP_FILE, "w") as f:
            json.dump(dedup, f)
    except Exception:
        pass


def _stop_monitor_loop(states_registry: dict):
    """
    Daemon : entre les cycles 4h, vérifie toutes les 15min l'état des stops broker.
    - Stop expiré (Alpaca DAY tif sur fractional) → re-place automatiquement
    - Stop fillé (déclenché) → ferme position dans state, cooldown 12h, notify
    - Réconcilie aussi les positions broker ↔ bot (drift / fantôme) avec alerte
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
            checked = renewed = adopted = filled = 0
            for bot_id, state in states_registry.items():
                positions = state.get("positions") or {}
                for symbol in list(positions.keys()):
                    position = positions.get(symbol)
                    if not position:
                        continue
                    # Compte aussi les orphelines (alpaca_stop_id manquant) :
                    # reconcile_broker_stop tentera de placer un stop pour rattraper.
                    checked += 1
                    try:
                        action, data = reconcile_broker_stop(symbol, position)
                        if action == "renewed":
                            renewed += 1
                        elif action == "adopted":
                            adopted += 1
                        elif action == "filled":
                            _close_position_from_broker_fill(state, bot_id, symbol, float(data))
                            filled += 1
                    except Exception as e:
                        log(f"[STOP-MONITOR] {bot_id}/{symbol} échec: {e}", "WARN")
            if checked > 0:
                log(f"[STOP-MONITOR] {checked} stops vérifiés, "
                    f"{renewed} renouvelés, {adopted} adoptés, "
                    f"{filled} fillés (positions fermées)")
            # Réconciliation broker↔bot (détecte drift et positions fantômes)
            try:
                _reconcile_broker_positions(states_registry)
            except Exception as e:
                log(f"[RECONCILE] iteration: {e}", "WARN")
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

    # ── Archive vieux trades (anti-bloat state.json) ────────────────────────
    for _bid, _st in [("a", state_a), ("b", state_b), ("c", state_c), ("g", state_g),
                       ("h", state_h), ("i", state_i), ("j", state_j)]:
        try:
            _archive_trades_if_needed(_st, _bid)
        except Exception as e:
            log(f"[ARCHIVE] Bot {_bid} échec: {e}", "WARN")

    # ── Reconcile états bot ↔ positions broker Alpaca ──────────────────────
    # Détecte : positions vendues par broker hors-bot (margin, manual dashboard,
    # broker stop fillé pendant offline), qty mismatch, positions broker hors scope.
    # Source de vérité = broker (Alpaca).
    _states_for_reconcile = {
        "A": state_a, "B": state_b, "C": state_c, "G": state_g,
        "H": state_h, "I": state_i, "J": state_j,
    }
    try:
        _reconcile_alpaca_positions(_states_for_reconcile)
        # Save states après reconcile (sinon les corrections sont perdues si crash avant 1er save)
        save_state_a(state_a); save_mom(state_b); save_brk(state_c)
        save_trd(state_g); save_vcb(state_h); save_rsl(state_i); save_mr(state_j)
    except Exception as e:
        log(f"[RECONCILE] échec : {e}", "WARN")

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
            # ── Daily circuit breaker (-3% intraday) ──
            # Calcule equity portfolio et déclenche le breaker si daily loss > seuil
            try:
                _broker_eq = _fetch_broker_equity()
                _equity_for_breaker = _broker_eq if _broker_eq else _states_total_value(
                    [state_a, state_b, state_c, state_g, state_h, state_i, state_j], ohlcv_daily
                )
                breaker_active = _daily_breaker_check(_equity_for_breaker)
            except Exception as e:
                log(f"[DAILY-BREAKER] check failed: {e}", "WARN")
                breaker_active = False

            # ── Max trades / jour (anti-spam) ──
            trades_today = _trades_today_total([state_a, state_b, state_c, state_g, state_h, state_i, state_j])
            max_trades_reached = trades_today >= MAX_TRADES_PER_DAY
            if max_trades_reached:
                log(f"⚠ MAX_TRADES_PER_DAY={MAX_TRADES_PER_DAY} atteint ({trades_today}) — block new entries jusqu'à minuit UTC", "WARN")

            # ── Inactivity alert (bot trop silencieux) ──
            try:
                _check_inactivity_alert([state_a, state_b, state_c, state_g, state_h, state_i, state_j])
            except Exception as e:
                log(f"[INACTIVITY] check failed: {e}", "WARN")

            # ── Symbol exclusivity cross-bots ──
            # Pour chaque bot, on calcule les symboles tenus par les AUTRES bots.
            # Le BUY logic skippe ces symboles pour éviter conflits (broker agrège,
            # stops bagarrent). Recalculé à chaque cycle.
            _all_states = [state_a, state_b, state_c, state_g, state_h, state_i, state_j]

            # ── Cap corrélation cross-bots : MAX_PER_SECTOR_GLOBAL ──
            # Compte les positions par secteur à travers TOUS les bots actifs.
            # Si un secteur dépasse le cap → bloque les nouvelles entrées sur ce
            # secteur uniquement (les autres restent libres).
            global_sector_counts = {}
            for _st in [state_a, state_b, state_c, state_g, state_h, state_i, state_j]:
                for _sym in (_st.get("positions") or {}):
                    _sec = config.SECTORS.get(_sym, "other")
                    global_sector_counts[_sec] = global_sector_counts.get(_sec, 0) + 1
            blocked_sectors = {
                sec for sec, n in global_sector_counts.items()
                if n >= config.MAX_PER_SECTOR_GLOBAL
            }
            if blocked_sectors:
                log(f"⚠ Cap secteur GLOBAL atteint : {blocked_sectors} (counts: {global_sector_counts})", "INFO")
            macro["blocked_sectors"] = blocked_sectors

            # ── Symbol exclusivity : compute held_by_others par bot ──
            held_symbols_global = _compute_held_symbols(_all_states)
            # Note : pour chaque bot, les symboles tenus par LUI-MÊME ne comptent pas
            # comme "held by others" — c'est sa propre position. On filtre côté bot.
            macro["held_symbols_global"] = held_symbols_global

            # Passer le flag aux bots pour bloquer les nouvelles entrées
            # exposure_high OR daily_breaker OR max_trades → block new entries
            macro["exposure_blocked"] = bool(exposure_high or breaker_active or max_trades_reached)

            # ── 5. Bot A: Supertrend + filters ────────────────────────────────
            log(f"\n{Fore.CYAN}--- Bot A: Supertrend+MR ---{Style.RESET_ALL}")
            state_a["_exposure_blocked"] = bool(exposure_high or breaker_active or max_trades_reached)
            state_a["_blocked_sectors"] = blocked_sectors
            state_a["_held_by_other_bots"] = {
                s for s in held_symbols_global if s not in (state_a.get("positions") or {})
            }

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
                category = "xstock" if symbol in config.STOCKS else "crypto"
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

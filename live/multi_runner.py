"""
Multi-Bot Contest Runner
Runs 5 trading strategies simultaneously with a shared market data hub.

Architecture:
  One MarketSnapshot (macro + OHLCV) → Bot A + Bot B + Bot C + Bot D + Bot E

  Bot A: Supertrend + filters + MR RSI(2)     — 1000€ capital
         [live/bot.py — unchanged logic]
  Bot B: Momentum Rotation (Antonacci)         — 1000€ capital
         [strategies/momentum_strategy.py]
  Bot C: Donchian Breakout Turtle System 2     — 1000€ capital
         [strategies/breakout_strategy.py]
  Bot D: LLM-Driven (DeepSeek V3.2 Reasoner)  — 1000€ capital
         [strategies/llm_strategy.py]
  Bot E: LLM-Driven (Claude Sonnet 4.6)       — 1000€ capital
         [strategies/claude_llm_strategy.py]

API efficiency: 1× macro fetch + 2× OHLCV cache (4h + daily)
vs 5 independent bots × 2 fetches = 10× → 5× savings

Usage:
    python live/multi_runner.py

State files:
    logs/supertrend/state.json   — Bot A
    logs/momentum/state.json     — Bot B
    logs/breakout/state.json     — Bot C
    logs/llm/state.json          — Bot D
    logs/claude_llm/state.json   — Bot E
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from colorama import Fore, Style, init

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.market_snapshot import fetch_macro_context, fetch_ohlcv_cache
from strategies.momentum_strategy import (
    run_momentum_cycle, load_state as load_mom, save_state as save_mom,
)
from strategies.breakout_strategy import (
    run_breakout_cycle, BREAKOUT_SYMBOLS,
    load_state as load_brk, save_state as save_brk,
)
from strategies.llm_strategy import (
    run_llm_cycle, load_state as load_llm, save_state as save_llm,
)
from strategies.claude_llm_strategy import (
    run_claude_cycle, load_state as load_cla, save_state as save_cla,
)
from strategies.haiku_llm_strategy import (
    run_haiku_cycle, load_state as load_hai, save_state as save_hai,
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
INITIAL_CAPITAL_PER_BOT = 1000.0
Z_BUDGET_FILE = "logs/bot_z/budget.json"


def _apply_z_budget(state: dict, z_budget_eur: float) -> dict:
    """Applique l'allocation Bot Z à l'état d'un sub-bot en scalant le capital proportionnellement.

    Si Bot Z alloue 4 000€ à Bot A (qui avait 1 000€ initial), le capital disponible
    est multiplié ×4 tout en préservant le ratio de PnL accumulé.
    """
    prev = state.get("z_budget_eur", state.get("initial_capital", INITIAL_CAPITAL_PER_BOT))
    if prev <= 0:
        prev = INITIAL_CAPITAL_PER_BOT
    if abs(z_budget_eur - prev) / prev > 0.02:  # changement > 2% → rescale
        scale = z_budget_eur / prev
        state["capital"] = round(state["capital"] * scale, 2)
        state["initial_capital"] = round(z_budget_eur, 2)
    state["z_budget_eur"] = round(z_budget_eur, 2)
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
    with open(STATE_A_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


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
                          state_d: dict, state_e: dict, state_f: dict,
                          state_g: dict, state_h: dict, state_i: dict,
                          state_j: dict = None,
                          daily_cache: dict = None):
    bots = [
        ("A — Supertrend+MR",     state_a),
        ("B — Momentum",          state_b),
        ("C — Breakout",          state_c),
        ("D — DeepSeek LLM",      state_d),
        ("E — Claude Sonnet",     state_e),
        ("F — Claude Haiku",      state_f),
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

    all_states = [state_a, state_b, state_c, state_d, state_e, state_f, state_g, state_h, state_i]
    if state_j:
        all_states.append(state_j)
    for name, state in bots:
        if not state:
            continue
        total = _portfolio_value(state, daily_cache)
        init = state.get("initial_capital", INITIAL_CAPITAL_PER_BOT)
        perf = (total - init) / init * 100 if init > 0 else 0
        trades = len(state.get("trades", []))
        positions = ", ".join(state.get("positions", {}).keys()) or "—"
        capital = state.get("capital", 0)

        color = Fore.GREEN if perf > 0 else Fore.RED if perf < 0 else Fore.WHITE
        print(
            f"Bot {name:<22} {capital:>8.2f}€  {total:>8.2f}€  "
            f"{positions:<12} {trades:>6}  {color}{perf:>+6.1f}%{Style.RESET_ALL}"
        )

    # Combined
    n_bots = len(all_states)
    total_all = sum(_portfolio_value(s, daily_cache) for s in all_states)
    total_init = sum(s.get("initial_capital", INITIAL_CAPITAL_PER_BOT) for s in all_states)
    combined_perf = (total_all - total_init) / total_init * 100 if total_init > 0 else 0
    color = Fore.GREEN if combined_perf > 0 else Fore.RED
    print("-" * 72)
    print(f"{'TOTAL ('+ str(n_bots*1000) +'€ base)':<26} {'':>10} {total_all:>8.2f}€  "
          f"{'':>10} {'':>8}  {color}{combined_perf:>+6.1f}%{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*72}{Style.RESET_ALL}\n")


# ── Timing ───────────────────────────────────────────────────────────────────

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
    os.makedirs("logs/llm",        exist_ok=True)
    os.makedirs("logs/claude_llm", exist_ok=True)
    os.makedirs("logs/haiku_llm",  exist_ok=True)
    os.makedirs("logs/trend",      exist_ok=True)
    os.makedirs("logs/vcb",             exist_ok=True)
    os.makedirs("logs/rs_leaders",      exist_ok=True)
    os.makedirs("logs/mean_reversion",  exist_ok=True)
    os.makedirs("logs/bot_z",           exist_ok=True)

    log(f"{'='*60}", "INFO")
    log("  MULTI-BOT CONTEST STARTED", "INFO")
    log(f"  Bot A: Supertrend+MR      → logs/supertrend/state.json", "INFO")
    log(f"  Bot B: Momentum Rotation  → logs/momentum/state.json", "INFO")
    log(f"  Bot C: Donchian Breakout  → logs/breakout/state.json", "INFO")
    log(f"  Bot D: DeepSeek Reasoner  → logs/llm/state.json", "INFO")
    log(f"  Bot E: Claude Sonnet      → logs/claude_llm/state.json", "INFO")
    log(f"  Bot F: Claude Haiku       → logs/haiku_llm/state.json", "INFO")
    log(f"  Bot G: Trend Multi-Asset  → logs/trend/state.json", "INFO")
    log(f"  Bot H: VCB Breakout       → logs/vcb/state.json", "INFO")
    log(f"  Bot I: RS Leaders         → logs/rs_leaders/state.json", "INFO")
    log(f"  Bot J: Mean Reversion     → logs/mean_reversion/state.json", "INFO")
    log(f"  Capital initial: {INITIAL_CAPITAL_PER_BOT:.0f}€ × 10 = {INITIAL_CAPITAL_PER_BOT*10:.0f}€", "INFO")
    log(f"{'='*60}", "INFO")

    state_a = load_state_a()
    state_b = load_mom()
    state_c = load_brk()
    state_d = load_llm()
    state_e = load_cla()
    state_f = load_hai()
    state_g = load_trd()
    state_h = load_vcb()
    state_i = load_rsl()
    state_j = load_mr()

    _prev_budget = {}  # Track previous budget for change detection

    log(f"Bot A capital: {state_a['capital']:.2f}€ | Positions: {list(state_a['positions'].keys())}")
    log(f"Bot B capital: {state_b['capital']:.2f}€ | Positions: {list(state_b['positions'].keys())}")
    log(f"Bot C capital: {state_c['capital']:.2f}€ | Positions: {list(state_c['positions'].keys())}")
    log(f"Bot D capital: {state_d['capital']:.2f}€ | Positions: {list(state_d['positions'].keys())}")
    log(f"Bot E capital: {state_e['capital']:.2f}€ | Positions: {list(state_e['positions'].keys())}")
    log(f"Bot F capital: {state_f['capital']:.2f}€ | Positions: {list(state_f['positions'].keys())}")
    log(f"Bot G capital: {state_g['capital']:.2f}€ | Positions: {list(state_g['positions'].keys())}")
    log(f"Bot H capital: {state_h['capital']:.2f}€ | Positions: {list(state_h['positions'].keys())}")
    log(f"Bot I capital: {state_i['capital']:.2f}€ | Positions: {list(state_i['positions'].keys())}")
    log(f"Bot J capital: {state_j['capital']:.2f}€ | Positions: {list(state_j['positions'].keys())}")

    while True:
        try:
            log(f"\n--- Cycle {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

            # ── 0. Resend pending API credit alerts ───────────────────────────
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
            ohlcv_daily = fetch_ohlcv_cache(config.SYMBOLS, timeframe="1d", days=220)
            log(f"Daily cache: {len(ohlcv_daily)}/{len(config.SYMBOLS)} symbols ready")

            # ── 4. Bot Z — Pilot (allocation dispatch AVANT les sub-bots) ────────
            try:
                # Passe ohlcv_daily pour mark-to-market réel des positions
                z_summary = run_bot_z_cycle(macro, ohlcv=ohlcv_daily)
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
                    state_a = _apply_z_budget(state_a, z_budget_alloc.get("a", INITIAL_CAPITAL_PER_BOT))
                    state_b = _apply_z_budget(state_b, z_budget_alloc.get("b", INITIAL_CAPITAL_PER_BOT))
                    state_c = _apply_z_budget(state_c, z_budget_alloc.get("c", INITIAL_CAPITAL_PER_BOT))
                    state_g = _apply_z_budget(state_g, z_budget_alloc.get("g", INITIAL_CAPITAL_PER_BOT))

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
                            notify_z_dispatch(z_budget_alloc, z_summary.get('z_capital_eur', 10000), z_summary.get('current_engine', '?'))
                    _prev_budget = dict(z_budget_alloc)

            except Exception as ez:
                log(f"Bot Z erreur (non bloquant): {ez}", "WARN")

            # Injecter l'engine Bot Z dans macro pour H/I/J (filtre régime sans dispatch capital)
            macro["bot_z_engine"] = z_summary.get("current_engine", "BALANCED") if z_summary else "BALANCED"

            # ── 5. Bot A: Supertrend + filters ────────────────────────────────
            log(f"\n{Fore.CYAN}--- Bot A: Supertrend+MR ---{Style.RESET_ALL}")

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
                )
                time.sleep(1)

            save_state_a(state_a)
            log(
                f"[A] Capital: {state_a['capital']:.2f}€ | "
                f"Positions: {list(state_a['positions'].keys())} | "
                f"Trades: {len(state_a['trades'])}"
            )

            # ── 6. Bot B: Momentum Rotation ───────────────────────────────────
            log(f"\n{Fore.GREEN}--- Bot B: Momentum Rotation ---{Style.RESET_ALL}")
            state_b = run_momentum_cycle(state_b, ohlcv_daily, macro)
            save_mom(state_b)
            log(
                f"[B] Capital: {state_b['capital']:.2f}€ | "
                f"Holdings: {list(state_b['positions'].keys())} | "
                f"Trades: {len(state_b['trades'])}"
            )

            # ── 7. Bot C: Donchian Breakout ───────────────────────────────────
            log(f"\n{Fore.YELLOW}--- Bot C: Donchian Breakout ---{Style.RESET_ALL}")
            brk_cache = {s: ohlcv_daily.get(s) for s in BREAKOUT_SYMBOLS if s in ohlcv_daily}
            state_c = run_breakout_cycle(state_c, brk_cache, macro)
            save_brk(state_c)
            log(
                f"[C] Capital: {state_c['capital']:.2f}€ | "
                f"Positions: {list(state_c['positions'].keys())} | "
                f"Trades: {len(state_c['trades'])}"
            )

            # ── 8. Bot D: DeepSeek LLM (DÉSACTIVÉ — coût tokens) ────────────
            log(f"\n[D] Bot D DeepSeek — désactivé (coût tokens)")

            # ── 9. Bot E: Claude Sonnet (DÉSACTIVÉ — coût tokens) ────────────
            log(f"[E] Bot E Claude Sonnet — désactivé (coût tokens)")

            # ── 10. Bot F: Claude Haiku (DÉSACTIVÉ — coût tokens) ─────────────
            log(f"[F] Bot F Claude Haiku — désactivé (coût tokens)")

            # ── 11. Bot G: Trend Following Multi-Asset ────────────────────────
            log(f"\n{Fore.CYAN}--- Bot G: Trend Following Multi-Asset ---{Style.RESET_ALL}")
            state_g = run_trend_cycle(state_g, ohlcv_daily, macro)
            save_trd(state_g)
            log(
                f"[G] Capital: {state_g['capital']:.2f}€ | "
                f"Positions: {list(state_g['positions'].keys())} | "
                f"Trades: {len(state_g['trades'])}"
            )

            # ── 12. Bot H: VCB Breakout ───────────────────────────────────────
            log(f"\n{Fore.RED}--- Bot H: VCB Breakout ---{Style.RESET_ALL}")
            state_h = run_vcb_cycle(state_h, ohlcv_4h, macro)
            save_vcb(state_h)
            log(
                f"[H] Capital: {state_h['capital']:.2f}€ | "
                f"Positions: {list(state_h['positions'].keys())} | "
                f"Trades: {len(state_h['trades'])}"
            )

            # ── 13. Bot I: RS Leaders ─────────────────────────────────────────
            log(f"\n{Fore.CYAN}--- Bot I: RS Leaders ---{Style.RESET_ALL}")
            state_i = run_rs_leaders_cycle(state_i, ohlcv_daily, macro)
            save_rsl(state_i)
            log(
                f"[I] Capital: {state_i['capital']:.2f}€ | "
                f"Positions: {list(state_i['positions'].keys())} | "
                f"Trades: {len(state_i['trades'])}"
            )

            # ── 14. Bot J: Mean Reversion ─────────────────────────────────────
            log(f"\n{Fore.WHITE}--- Bot J: Mean Reversion ---{Style.RESET_ALL}")
            state_j = run_mr_cycle(state_j, ohlcv_daily, macro)
            save_mr(state_j)
            log(
                f"[J] Capital: {state_j['capital']:.2f}€ | "
                f"Positions: {list(state_j['positions'].keys())} | "
                f"Trades: {len(state_j['trades'])}"
            )

            # ── 15. Contest summary ───────────────────────────────────────────
            print_contest_status(state_a, state_b, state_c, state_d, state_e, state_f, state_g, state_h, state_i, state_j, ohlcv_daily)

            # ── 16. Drawdown checks ───────────────────────────────────────────
            for name, state in [("A", state_a), ("B", state_b), ("C", state_c), ("D", state_d), ("E", state_e), ("F", state_f), ("G", state_g), ("H", state_h), ("I", state_i), ("J", state_j)]:
                total = _portfolio_value(state, ohlcv_daily)
                init = state.get("initial_capital", INITIAL_CAPITAL_PER_BOT)
                dd = (total - init) / init
                if dd <= config.MAX_DRAWDOWN:
                    log(f"⛔ Bot {name}: MAX DRAWDOWN {dd*100:.1f}% atteint", "WARN")
                    from live.notifier import notify
                    notify(
                        f"⛔ <b>Bot {name} MAX DRAWDOWN</b> {dd*100:.1f}%\n"
                        f"Seuil: {config.MAX_DRAWDOWN*100:.0f}%"
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
            save_llm(state_d)
            save_cla(state_e)
            save_hai(state_f)
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

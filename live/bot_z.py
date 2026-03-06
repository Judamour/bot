"""
live/bot_z.py — Regime Engine + Shadow Allocator
=================================================
Tourne EN PARALLÈLE des bots A-I sans toucher à l'exécution réelle.

Phase actuelle : SHADOW MODE
  - Observe les signaux de chaque bot (via leurs state files)
  - Détecte le régime de marché
  - Simule ce qu'il aurait alloué
  - Log les décisions dans logs/bot_z/shadow.jsonl
  - N'exécute AUCUN trade réel

Architecture :
  Bots A→I (state files) + Macro (VIX, QQQ, BTC)
      ↓
  Regime Engine (4 régimes)
      ↓
  Shadow Portfolio Engine (allocation simulée)
      ↓
  logs/bot_z/shadow.jsonl + logs/bot_z/state.json

Régimes :
  BULL       : QQQ > SMA200 + VIX < 18
  RANGE      : QQQ > SMA200 + VIX 18-30
  BEAR       : QQQ < SMA200
  HIGH_VOL   : VIX > 30

Règles d'allocation par régime :
  BULL     : G×1.3, I×1.2, B×1.0, H×1.0, C×0.8, A×0.5
  RANGE    : A×1.2, I×1.0, B×0.8, G×0.7, H×0.5, C×0.5
  BEAR     : A×1.5, tous les autres × 0.0 (cash)
  HIGH_VOL : A×0.8, tous les autres × 0.3
"""
import json
import os
import sys
import math
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

STATE_FILE  = "logs/bot_z/state.json"
SHADOW_LOG  = "logs/bot_z/shadow.jsonl"
INITIAL_CAP = 6000.0   # capital simulé total (6 bots actifs × 1000€)

# ── Poids par régime ──────────────────────────────────────────────────────────
# weight = fraction du capital max alloué au bot (1.0 = plein budget)
REGIME_WEIGHTS = {
    "BULL": {
        "a": 0.5, "b": 1.0, "c": 0.8, "g": 1.3, "h": 1.0, "i": 1.2,
    },
    "RANGE": {
        "a": 1.2, "b": 0.8, "c": 0.5, "g": 0.7, "h": 0.5, "i": 1.0,
    },
    "BEAR": {
        "a": 1.5, "b": 0.0, "c": 0.0, "g": 0.2, "h": 0.0, "i": 0.0,
    },
    "HIGH_VOL": {
        "a": 0.8, "b": 0.3, "c": 0.3, "g": 0.3, "h": 0.3, "i": 0.3,
    },
}

# ── State files des bots ──────────────────────────────────────────────────────
BOT_STATE_FILES = {
    "a": "logs/supertrend/state.json",
    "b": "logs/momentum/state.json",
    "c": "logs/breakout/state.json",
    "g": "logs/trend/state.json",
    "h": "logs/vcb/state.json",
    "i": "logs/rs_leaders/state.json",
}

BOT_NAMES = {
    "a": "Supertrend+MR",
    "b": "Momentum",
    "c": "Breakout",
    "g": "Trend Multi-Asset",
    "h": "VCB Breakout",
    "i": "RS Leaders",
}

# ── Limites portefeuille ──────────────────────────────────────────────────────
MAX_EXPOSURE_PER_ASSET = 0.30   # max 30% du capital total sur un même actif
MAX_BOTS_SAME_ASSET    = 2      # max 2 bots simultanés long sur le même actif


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital_simulated": INITIAL_CAP,
        "regime_history": [],
        "allocation_history": [],
        "shadow_trades": [],
        "initial_capital": INITIAL_CAP,
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _log_shadow(entry: dict):
    os.makedirs(os.path.dirname(SHADOW_LOG), exist_ok=True)
    with open(SHADOW_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_bot_state(bot_id: str) -> dict:
    path = BOT_STATE_FILES.get(bot_id)
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"capital": 1000.0, "positions": {}, "trades": []}


# ── Régime ────────────────────────────────────────────────────────────────────

def detect_regime(macro: dict) -> str:
    """Détecte le régime à partir du contexte macro partagé."""
    vix = macro.get("vix", 0.0)
    qqq_ok = macro.get("qqq_regime_ok", True)
    btc_trend = macro.get("btc_context", {}).get("btc_trend", "bull")

    if vix > 30:
        return "HIGH_VOL"
    if not qqq_ok:
        return "BEAR"
    if vix < 18:
        return "BULL"
    return "RANGE"


# ── Exposition actuelle ───────────────────────────────────────────────────────

def get_exposure(all_states: dict) -> dict:
    """
    Calcule l'exposition actuelle par actif à travers tous les bots.
    Retourne {symbol: [bot_ids]} — liste des bots longs sur cet actif.
    """
    exposure = {}
    for bot_id, state in all_states.items():
        for sym in state.get("positions", {}):
            if sym not in exposure:
                exposure[sym] = []
            exposure[sym].append(bot_id)
    return exposure


# ── Rolling Sharpe des bots (performance récente) ────────────────────────────

def compute_rolling_score(bot_id: str, state: dict, window: int = 20) -> float:
    """
    Score de qualité récente : Sharpe approximatif sur les N derniers trades.
    Retourne 1.0 si pas assez de trades.
    """
    trades = state.get("trades", [])[-window:]
    if len(trades) < 5:
        return 1.0
    pnls = [t.get("pnl", 0) for t in trades]
    avg = sum(pnls) / len(pnls)
    std = math.sqrt(sum((p - avg) ** 2 for p in pnls) / len(pnls)) if len(pnls) > 1 else 1.0
    sharpe = avg / std if std > 0 else 0.0
    # Normaliser entre 0.3 et 1.5
    return max(0.3, min(1.5, 1.0 + sharpe * 0.3))


# ── Allocation simulée ────────────────────────────────────────────────────────

def compute_shadow_allocation(regime: str, all_states: dict, macro: dict) -> dict:
    """
    Calcule l'allocation que Bot Z donnerait à chaque bot.

    Retourne {bot_id: {budget_eur, weight_regime, weight_quality, budget_pct}}
    """
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["RANGE"])
    exposure = get_exposure(all_states)

    total_weight = sum(weights.values()) or 1.0
    allocation = {}

    for bot_id, state in all_states.items():
        regime_w  = weights.get(bot_id, 0.5)
        quality_w = compute_rolling_score(bot_id, state)
        final_w   = regime_w * quality_w

        budget_pct = final_w / total_weight
        budget_eur = INITIAL_CAP * budget_pct

        # Contrainte: si le bot a des positions sur des actifs déjà saturés, réduire
        positions = state.get("positions", {})
        for sym in positions:
            bots_on_sym = exposure.get(sym, [])
            if len(bots_on_sym) > MAX_BOTS_SAME_ASSET:
                budget_eur *= 0.5   # réduire si surexposition

        allocation[bot_id] = {
            "budget_eur":    round(budget_eur, 0),
            "budget_pct":    round(budget_pct * 100, 1),
            "weight_regime": round(regime_w, 2),
            "weight_quality": round(quality_w, 2),
            "weight_final":  round(final_w, 2),
            "bot_name":      BOT_NAMES.get(bot_id, bot_id),
            "open_positions": list(positions.keys()),
        }

    return allocation


# ── Analyse du portefeuille simulé ───────────────────────────────────────────

def analyze_cross_exposure(all_states: dict, allocation: dict) -> dict:
    """
    Identifie les surexpositions potentielles.
    Retourne {symbol: {bots: [...], exposure_pct: float, warning: bool}}
    """
    exposure = get_exposure(all_states)
    total_val = sum(s.get("capital", 1000) for s in all_states.values())
    alerts = {}

    for sym, bots in exposure.items():
        # Estimation grossière de l'exposition en €
        bot_budgets = [allocation.get(b, {}).get("budget_eur", 0) for b in bots]
        estimated_exposure_pct = sum(bot_budgets) / INITIAL_CAP * 100

        alerts[sym] = {
            "bots": bots,
            "n_bots": len(bots),
            "estimated_exposure_pct": round(estimated_exposure_pct, 1),
            "warning": len(bots) > MAX_BOTS_SAME_ASSET or estimated_exposure_pct > MAX_EXPOSURE_PER_ASSET * 100,
        }

    return alerts


# ── Cycle principal ───────────────────────────────────────────────────────────

def run_bot_z_cycle(macro: dict) -> dict:
    """
    Exécute un cycle Bot Z (shadow mode).
    Retourne le résumé du cycle pour logging.
    """
    ts = datetime.now().isoformat()

    # 1. Charger les états de tous les bots
    all_states = {bot_id: load_bot_state(bot_id) for bot_id in BOT_STATE_FILES}

    # 2. Détecter le régime
    regime = detect_regime(macro)

    # 3. Allocation simulée
    allocation = compute_shadow_allocation(regime, all_states, macro)

    # 4. Analyse exposition croisée
    cross = analyze_cross_exposure(all_states, allocation)

    # 5. Warnings
    warnings_list = [
        f"{sym}: {info['n_bots']} bots longs ({info['estimated_exposure_pct']:.0f}% exposition)"
        for sym, info in cross.items() if info["warning"]
    ]

    # 6. Portfolio value simulé
    portfolio_values = {}
    total_simulated = 0
    for bot_id, state in all_states.items():
        val = state.get("capital", 1000) + sum(
            p.get("entry", 0) * p.get("size", 0)
            for p in state.get("positions", {}).values()
        )
        portfolio_values[bot_id] = round(val, 2)
        total_simulated += val

    perf_pct = (total_simulated - 6000) / 6000 * 100

    # 7. Construction du résumé
    summary = {
        "timestamp": ts,
        "regime": regime,
        "vix": macro.get("vix", 0),
        "qqq_ok": macro.get("qqq_regime_ok", True),
        "btc_trend": macro.get("btc_context", {}).get("btc_trend", "?"),
        "allocation": allocation,
        "cross_exposure": cross,
        "warnings": warnings_list,
        "portfolio_values": portfolio_values,
        "total_simulated_eur": round(total_simulated, 2),
        "perf_pct": round(perf_pct, 2),
    }

    # 8. Log shadow
    _log_shadow(summary)

    # 9. Mise à jour state
    state = load_state()
    state["regime_history"].append({"ts": ts, "regime": regime})
    state["regime_history"] = state["regime_history"][-200:]
    state["allocation_history"].append({"ts": ts, "allocation": {k: v["budget_eur"] for k, v in allocation.items()}})
    state["allocation_history"] = state["allocation_history"][-200:]
    state["last_regime"] = regime
    state["last_allocation"] = allocation
    state["last_cross_exposure"] = cross
    state["last_warnings"] = warnings_list
    state["last_portfolio_values"] = portfolio_values
    state["total_simulated_eur"] = round(total_simulated, 2)
    state["perf_pct"] = round(perf_pct, 2)
    save_state(state)

    return summary


# ── Display ───────────────────────────────────────────────────────────────────

def print_bot_z_summary(summary: dict):
    from colorama import Fore, Style, init
    init(autoreset=True)

    regime_colors = {
        "BULL": Fore.GREEN, "RANGE": Fore.YELLOW,
        "BEAR": Fore.RED,   "HIGH_VOL": Fore.MAGENTA,
    }
    r = summary["regime"]
    c = regime_colors.get(r, Fore.WHITE)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{Fore.CYAN}{'─'*72}")
    print(f"  BOT Z — SHADOW MODE | {ts}")
    print(f"  Régime : {c}{r}{Style.RESET_ALL} | VIX={summary['vix']:.1f} | "
          f"QQQ={'✓' if summary['qqq_ok'] else '✗'} | BTC={summary['btc_trend']}")
    print(f"{'─'*72}{Style.RESET_ALL}")

    print(f"  {'Bot':<20} {'Budget':>8} {'%Cap':>6} {'W.Rég':>7} {'W.Qual':>7} {'Positions'}")
    print(f"  {'─'*70}")
    alloc = summary.get("allocation", {})
    for bot_id in sorted(alloc):
        a = alloc[bot_id]
        budget_c = Fore.GREEN if a["weight_final"] >= 1.0 else (Fore.YELLOW if a["weight_final"] >= 0.5 else Fore.RED)
        pos_str = ", ".join(a["open_positions"][:3]) or "—"
        print(f"  {a['bot_name']:<20} {budget_c}{a['budget_eur']:>7.0f}€{Style.RESET_ALL}  "
              f"{a['budget_pct']:>5.1f}%  {a['weight_regime']:>6.2f}  {a['weight_quality']:>6.2f}  {pos_str}")

    if summary["warnings"]:
        print(f"\n  {Fore.YELLOW}⚠ SUREXPOSITIONS :{Style.RESET_ALL}")
        for w in summary["warnings"]:
            print(f"    • {w}")

    pv = summary.get("portfolio_values", {})
    total = summary.get("total_simulated_eur", 0)
    perf = summary.get("perf_pct", 0)
    perf_c = Fore.GREEN if perf >= 0 else Fore.RED
    print(f"\n  Portefeuille simulé : {total:.2f}€  ({perf_c}{perf:+.2f}%{Style.RESET_ALL})")
    print(f"{Fore.CYAN}{'─'*72}{Style.RESET_ALL}\n")


# ── Standalone (test) ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test avec macro fictif
    macro_test = {
        "vix": 16.5,
        "qqq_regime_ok": True,
        "btc_context": {"btc_trend": "bull"},
    }
    summary = run_bot_z_cycle(macro_test)
    print_bot_z_summary(summary)

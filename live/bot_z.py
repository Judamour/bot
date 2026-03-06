"""
live/bot_z.py — Regime Engine + Portfolio Allocator (Shadow Mode)
=================================================================
Tourne EN PARALLÈLE des bots A-I sans toucher à l'exécution réelle.

Phase actuelle : SHADOW MODE
  - Observe les signaux de chaque bot (via leurs state files)
  - Détecte le régime de marché (4 états)
  - Simule l'allocation optimale
  - Log les décisions dans logs/bot_z/shadow.jsonl
  - N'exécute AUCUN trade réel

Architecture :
  Bots A→I (state files) + Macro (VIX, QQQ, BTC)
      ↓
  Regime Engine (4 régimes + score composite)
      ↓
  Portfolio Engine (base + overlay + volatility targeting)
      ↓
  Risk Manager (cap par bot, cap par actif, cash VIX)
      ↓
  logs/bot_z/shadow.jsonl + logs/bot_z/state.json

Régimes (détection améliorée backtest 2020-2026) :
  BULL     : QQQ > SMA200 + BTC tendance haussière + VIX < 25
  RANGE    : QQQ > SMA200 + VIX entre 18 et 30 (pas clairement bull)
  BEAR     : QQQ < SMA200 ou VIX > 30
  HIGH_VOL : VIX > 35 (priorité absolue → cash partiel)

Calibration validée sur backtest 2020-2026 :
  BULL     : G×1.2, B×1.0, A×0.8, C×0.5
  RANGE    : A×1.0, G×0.8, B×0.8, C×0.7
  BEAR     : C×1.5, G×1.2, A×0.3, B×0.0   ← C et G sont les seuls défensifs (prouvé 2022)
  HIGH_VOL : C×1.0, G×0.8, A×0.5, B×0.3

Structure portefeuille :
  Base (70%) : G=30%, A=20%, B=20%, C=20%, cash=10%  [stable, always-on]
  Overlay (30%) : allocation dynamique Bot Z par régime
  Priorité conflits : G > C > A > B (G le plus fiable en backtest)

Limites de risque :
  MAX_BOT_WEIGHT    = 0.40  (max 40% portefeuille sur un bot)
  MAX_ASSET_EXPOSURE = 0.30 (max 30% portefeuille sur un actif)
  MAX_BOTS_SAME_ASSET = 2   (max 2 bots simultanés sur le même actif)
  CASH_VIX_THRESHOLD = 35   (VIX > 35 → cash 30%)
  TARGET_VOL        = 0.15  (volatilité cible annualisée 15%)
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

# ── Calibration par régime (validée sur backtest 2020-2026) ──────────────────
# weight = importance relative du bot dans ce régime (normalisé à 1.0 après)
REGIME_WEIGHTS = {
    "BULL": {
        "a": 0.8, "b": 1.0, "c": 0.5, "g": 1.2, "h": 0.8, "i": 1.0,
    },
    "RANGE": {
        "a": 1.0, "b": 0.8, "c": 0.7, "g": 0.8, "h": 0.5, "i": 0.8,
    },
    "BEAR": {
        # C (-2.5%) et G (-3.4%) validés comme défensifs en 2022
        # A (-49%) et B (-43%) : catastrophiques en bear — à zéro ou minimal
        "a": 0.3, "b": 0.0, "c": 1.5, "g": 1.2, "h": 0.0, "i": 0.0,
    },
    "HIGH_VOL": {
        # Forte réduction globale, C et G comme refuge
        "a": 0.5, "b": 0.3, "c": 1.0, "g": 0.8, "h": 0.3, "i": 0.5,
    },
}

# Poids de base stables (70% du capital) — structure "pilier"
# Validé : G et C sont les bots les plus robustes sur 6 ans
BASE_WEIGHTS = {"g": 0.30, "a": 0.20, "b": 0.20, "c": 0.20, "cash": 0.10}

# Priorité pour résolution de conflits (actif partagé entre bots)
# Ordre : G > C > A > B > I > H (d'après fiabilité backtest)
BOT_PRIORITY = ["g", "c", "a", "b", "i", "h"]

# ── Limites de risque ────────────────────────────────────────────────────────
MAX_BOT_WEIGHT     = 0.40   # max 40% du capital sur un bot (base + overlay)
MAX_ASSET_EXPOSURE = 0.30   # max 30% du capital sur un même actif
MAX_BOTS_SAME_ASSET = 2     # max 2 bots simultanés long sur le même actif
CASH_VIX_THRESHOLD = 35.0   # VIX > 35 → forcer cash 30%
TARGET_VOL         = 0.15   # volatilité cible portefeuille annualisée 15%
BASE_PCT           = 0.70   # 70% en base stable
OVERLAY_PCT        = 0.30   # 30% en overlay dynamique

# ── State files des bots ────────────────────────────────────────────────────
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


# ── Régime ──────────────────────────────────────────────────────────────────

def detect_regime(macro: dict) -> str:
    """
    Détecte le régime de marché.
    Logique améliorée vs v1 : utilise BTC trend + seuils VIX révisés.

    HIGH_VOL (prioritaire) : VIX > 35
    BEAR                   : QQQ < SMA200 ou VIX > 30
    BULL                   : QQQ > SMA200 + BTC bull + VIX < 25
    RANGE                  : tout le reste
    """
    vix = macro.get("vix", 15.0)
    qqq_ok = macro.get("qqq_regime_ok", True)
    btc_ctx = macro.get("btc_context", {})
    btc_trend = btc_ctx.get("btc_trend", "bull")

    if vix > CASH_VIX_THRESHOLD:
        return "HIGH_VOL"
    if not qqq_ok or vix > 30:
        return "BEAR"
    if vix < 25 and btc_trend in ("bull", "strong_bull"):
        return "BULL"
    return "RANGE"


def detect_regime_score(macro: dict) -> dict:
    """
    Retourne le régime + un score de confiance [0-1] pour chaque régime.
    Permet d'interpoler les allocations en transition de régime.
    """
    vix = macro.get("vix", 15.0)
    qqq_ok = macro.get("qqq_regime_ok", True)
    btc_trend = macro.get("btc_context", {}).get("btc_trend", "bull")

    regime = detect_regime(macro)

    # Score de confiance basé sur la force du signal
    if regime == "HIGH_VOL":
        confidence = min(1.0, (vix - CASH_VIX_THRESHOLD) / 15.0 + 0.5)
    elif regime == "BEAR":
        confidence = 0.8 if not qqq_ok else min(1.0, (vix - 25) / 10.0 + 0.4)
    elif regime == "BULL":
        confidence = max(0.4, (25 - vix) / 10.0) * (1.1 if btc_trend == "strong_bull" else 1.0)
        confidence = min(1.0, confidence)
    else:
        confidence = 0.6

    return {"regime": regime, "confidence": round(confidence, 2), "vix": vix, "qqq_ok": qqq_ok}


# ── Exposition actuelle ──────────────────────────────────────────────────────

def get_exposure(all_states: dict) -> dict:
    """
    Calcule l'exposition par actif à travers tous les bots.
    Retourne {symbol: [bot_ids]} trié par priorité BOT_PRIORITY.
    """
    exposure = {}
    for bot_id, state in all_states.items():
        for sym in state.get("positions", {}):
            if sym not in exposure:
                exposure[sym] = []
            exposure[sym].append(bot_id)

    # Trier par priorité pour résolution de conflits
    for sym in exposure:
        exposure[sym] = sorted(
            exposure[sym],
            key=lambda b: BOT_PRIORITY.index(b) if b in BOT_PRIORITY else 99
        )
    return exposure


# ── Qualité récente des bots ─────────────────────────────────────────────────

def compute_rolling_score(bot_id: str, state: dict, window: int = 20) -> float:
    """
    Score de qualité récente basé sur les N derniers trades.
    Sharpe approximatif normalisé entre 0.3 et 1.5.
    Retourne 1.0 si pas assez de trades (neutre).
    """
    trades = state.get("trades", [])[-window:]
    if len(trades) < 5:
        return 1.0
    pnls = [t.get("pnl", 0) for t in trades]
    avg = sum(pnls) / len(pnls)
    std = math.sqrt(sum((p - avg) ** 2 for p in pnls) / len(pnls)) if len(pnls) > 1 else 1.0
    sharpe = avg / std if std > 0 else 0.0
    return max(0.3, min(1.5, 1.0 + sharpe * 0.3))


def compute_bot_volatility(state: dict, window: int = 20) -> float:
    """
    Estime la volatilité récente d'un bot à partir des PnL normalisés.
    Retourne une vol annualisée approximative (std × sqrt(252)).
    """
    trades = state.get("trades", [])[-window:]
    if len(trades) < 3:
        return TARGET_VOL  # vol cible par défaut

    capital = state.get("capital", 1000.0)
    if capital <= 0:
        return TARGET_VOL

    # Returns normalisés par capital
    norm_returns = [t.get("pnl", 0) / capital for t in trades]
    avg = sum(norm_returns) / len(norm_returns)
    variance = sum((r - avg) ** 2 for r in norm_returns) / len(norm_returns)
    std = math.sqrt(variance)

    # Approximation : ~1 trade tous les 5 jours → annualise sur 252/5 = 50 périodes
    trades_per_year = 252 / max(5, 365 / len(trades))
    annual_vol = std * math.sqrt(trades_per_year)
    return max(0.05, min(1.0, annual_vol))  # clamp entre 5% et 100%


# ── Allocation principale ────────────────────────────────────────────────────

def compute_shadow_allocation(regime: str, all_states: dict, macro: dict) -> dict:
    """
    Calcule l'allocation Portfolio Bot Z pour chaque bot.

    Structure :
      Base (70%) : poids fixes G/A/B/C
      Overlay (30%) : pondération dynamique par régime + qualité récente
      Contraintes : cap par bot 40%, cap par actif 30%, cash VIX

    Retourne {bot_id: {budget_eur, budget_pct, weight_*, open_positions, ...}}
    """
    vix = macro.get("vix", 15.0)
    regime_w = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["RANGE"])
    exposure = get_exposure(all_states)

    # Cash forcé si VIX > seuil (reste inactif)
    forced_cash_pct = 0.0
    if vix > CASH_VIX_THRESHOLD:
        forced_cash_pct = 0.30
        effective_capital = INITIAL_CAP * (1 - forced_cash_pct)
    else:
        effective_capital = INITIAL_CAP

    # ── Base (70%) ───────────────────────────────────────────────────────────
    base = {k: v for k, v in BASE_WEIGHTS.items() if k != "cash"}
    cash_base = BASE_WEIGHTS.get("cash", 0.10)
    base_capital = effective_capital * BASE_PCT

    # ── Overlay (30%) ────────────────────────────────────────────────────────
    overlay_capital = effective_capital * OVERLAY_PCT
    total_overlay_w = sum(regime_w.get(b, 0) for b in all_states) or 1.0

    # ── Combinaison ─────────────────────────────────────────────────────────
    allocation = {}
    for bot_id, state in all_states.items():
        # Base
        base_w = base.get(bot_id, 0.0)
        base_eur = base_capital * base_w

        # Overlay (qualité récente modulée par régime)
        overlay_regime_w = regime_w.get(bot_id, 0.3)
        quality_w = compute_rolling_score(bot_id, state)
        overlay_final_w = overlay_regime_w * quality_w
        overlay_eur = overlay_capital * (overlay_final_w / total_overlay_w)

        # Total avant contraintes
        total_eur = base_eur + overlay_eur

        # Volatility targeting : réduire si vol récente > cible
        bot_vol = compute_bot_volatility(state)
        vol_scale = min(1.0, TARGET_VOL / max(bot_vol, 0.01))
        # N'applique le scaling que sur l'overlay (la base est fixe)
        scaled_overlay = overlay_eur * vol_scale
        total_eur = base_eur + scaled_overlay

        # Cap par bot
        max_budget = effective_capital * MAX_BOT_WEIGHT
        total_eur = min(total_eur, max_budget)

        # Réduction si surexposition actif
        positions = state.get("positions", {})
        for sym in positions:
            bots_on_sym = exposure.get(sym, [])
            if len(bots_on_sym) > MAX_BOTS_SAME_ASSET:
                # Réduire les bots moins prioritaires
                priority_bots = bots_on_sym[:MAX_BOTS_SAME_ASSET]
                if bot_id not in priority_bots:
                    total_eur *= 0.3  # réduire fortement si non-prioritaire

        budget_pct = total_eur / effective_capital if effective_capital > 0 else 0

        allocation[bot_id] = {
            "budget_eur":      round(total_eur, 0),
            "budget_pct":      round(budget_pct * 100, 1),
            "weight_base":     round(base_w, 2),
            "weight_regime":   round(overlay_regime_w, 2),
            "weight_quality":  round(quality_w, 2),
            "weight_vol_scale": round(vol_scale, 2),
            "weight_final":    round(overlay_final_w, 2),
            "bot_name":        BOT_NAMES.get(bot_id, bot_id),
            "open_positions":  list(positions.keys()),
        }

    return allocation


# ── Analyse du portefeuille ──────────────────────────────────────────────────

def analyze_cross_exposure(all_states: dict, allocation: dict) -> dict:
    """
    Identifie les surexpositions par actif (multi-bots sur le même actif).
    Retourne {symbol: {bots, n_bots, estimated_exposure_pct, warning, priority_bot}}
    """
    exposure = get_exposure(all_states)
    alerts = {}

    for sym, bots in exposure.items():
        bot_budgets = [allocation.get(b, {}).get("budget_eur", 0) for b in bots]
        estimated_pct = sum(bot_budgets) / INITIAL_CAP * 100

        # Bot prioritaire pour cet actif
        priority_bot = bots[0] if bots else None  # déjà trié par priorité dans get_exposure

        alerts[sym] = {
            "bots": bots,
            "n_bots": len(bots),
            "estimated_exposure_pct": round(estimated_pct, 1),
            "priority_bot": priority_bot,
            "warning": (len(bots) > MAX_BOTS_SAME_ASSET
                        or estimated_pct > MAX_ASSET_EXPOSURE * 100),
        }

    return alerts


# ── Cycle principal ──────────────────────────────────────────────────────────

def run_bot_z_cycle(macro: dict) -> dict:
    """
    Exécute un cycle Bot Z (shadow mode).
    Retourne le résumé du cycle pour logging et dashboard.
    """
    ts = datetime.now().isoformat()

    # 1. Charger les états de tous les bots
    all_states = {bot_id: load_bot_state(bot_id) for bot_id in BOT_STATE_FILES}

    # 2. Détecter le régime (avec score de confiance)
    regime_info = detect_regime_score(macro)
    regime = regime_info["regime"]

    # 3. Allocation simulée
    allocation = compute_shadow_allocation(regime, all_states, macro)

    # 4. Analyse exposition croisée
    cross = analyze_cross_exposure(all_states, allocation)

    # 5. Warnings
    warnings_list = []
    for sym, info in cross.items():
        if info["warning"]:
            prio = info["priority_bot"]
            prio_name = BOT_NAMES.get(prio, prio) if prio else "?"
            warnings_list.append(
                f"{sym}: {info['n_bots']} bots ({info['estimated_exposure_pct']:.0f}%) "
                f"— priorité {prio_name}"
            )

    # 6. Cash forcé (VIX > seuil)
    vix = macro.get("vix", 15.0)
    cash_note = ""
    if vix > CASH_VIX_THRESHOLD:
        cash_note = f"CASH 30% forcé (VIX={vix:.1f} > {CASH_VIX_THRESHOLD})"
        warnings_list.append(cash_note)

    # 7. Portfolio value simulé
    portfolio_values = {}
    total_simulated = 0
    for bot_id, state in all_states.items():
        val = state.get("capital", 1000) + sum(
            p.get("entry", 0) * p.get("size", 0)
            for p in state.get("positions", {}).values()
        )
        portfolio_values[bot_id] = round(val, 2)
        total_simulated += val

    initial_total = len(all_states) * 1000.0
    perf_pct = (total_simulated - initial_total) / initial_total * 100

    # 8. Construction du résumé
    summary = {
        "timestamp": ts,
        "regime": regime,
        "regime_confidence": regime_info["confidence"],
        "vix": vix,
        "qqq_ok": macro.get("qqq_regime_ok", True),
        "btc_trend": macro.get("btc_context", {}).get("btc_trend", "?"),
        "allocation": allocation,
        "cross_exposure": cross,
        "warnings": warnings_list,
        "portfolio_values": portfolio_values,
        "total_simulated_eur": round(total_simulated, 2),
        "perf_pct": round(perf_pct, 2),
    }

    # 9. Log shadow
    _log_shadow(summary)

    # 10. Mise à jour state
    state = load_state()
    state["regime_history"].append({"ts": ts, "regime": regime, "confidence": regime_info["confidence"]})
    state["regime_history"] = state["regime_history"][-200:]
    state["allocation_history"].append({"ts": ts, "allocation": {k: v["budget_eur"] for k, v in allocation.items()}})
    state["allocation_history"] = state["allocation_history"][-200:]
    state["last_regime"] = regime
    state["last_regime_info"] = regime_info
    state["last_allocation"] = allocation
    state["last_cross_exposure"] = cross
    state["last_warnings"] = warnings_list
    state["last_portfolio_values"] = portfolio_values
    state["total_simulated_eur"] = round(total_simulated, 2)
    state["perf_pct"] = round(perf_pct, 2)
    save_state(state)

    return summary


# ── Display ──────────────────────────────────────────────────────────────────

def print_bot_z_summary(summary: dict):
    from colorama import Fore, Style, init
    init(autoreset=True)

    regime_colors = {
        "BULL": Fore.GREEN, "RANGE": Fore.YELLOW,
        "BEAR": Fore.RED,   "HIGH_VOL": Fore.MAGENTA,
    }
    r = summary["regime"]
    c = regime_colors.get(r, Fore.WHITE)
    conf = summary.get("regime_confidence", 1.0)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{Fore.CYAN}{'─'*78}")
    print(f"  BOT Z — SHADOW MODE | {ts}")
    print(f"  Régime : {c}{r}{Style.RESET_ALL} (conf={conf:.0%}) | VIX={summary['vix']:.1f} | "
          f"QQQ={'✓' if summary['qqq_ok'] else '✗'} | BTC={summary['btc_trend']}")
    print(f"{'─'*78}{Style.RESET_ALL}")

    print(f"  {'Bot':<20} {'Budget':>8} {'%Cap':>6} {'Base':>6} {'Rég':>6} {'Qual':>6} {'Vol':>6}  Positions")
    print(f"  {'─'*76}")
    alloc = summary.get("allocation", {})
    for bot_id in sorted(alloc):
        a = alloc[bot_id]
        pct = a["budget_pct"]
        budget_c = Fore.GREEN if pct >= 20 else (Fore.YELLOW if pct >= 10 else Fore.RED)
        pos_str = ", ".join(a["open_positions"][:3]) or "—"
        print(f"  {a['bot_name']:<20} {budget_c}{a['budget_eur']:>7.0f}€{Style.RESET_ALL}  "
              f"{pct:>5.1f}%  {a['weight_base']:>5.2f}  {a['weight_regime']:>5.2f}  "
              f"{a['weight_quality']:>5.2f}  {a['weight_vol_scale']:>5.2f}  {pos_str}")

    if summary["warnings"]:
        print(f"\n  {Fore.YELLOW}⚠ ALERTES :{Style.RESET_ALL}")
        for w in summary["warnings"]:
            print(f"    • {w}")

    total = summary.get("total_simulated_eur", 0)
    perf = summary.get("perf_pct", 0)
    perf_c = Fore.GREEN if perf >= 0 else Fore.RED
    initial = len(alloc) * 1000.0
    print(f"\n  Portefeuille réel : {total:.2f}€  ({perf_c}{perf:+.2f}%{Style.RESET_ALL}) "
          f"vs initial {initial:.0f}€")
    print(f"{Fore.CYAN}{'─'*78}{Style.RESET_ALL}\n")


# ── Standalone (test) ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test avec différents régimes
    for scenario, macro_test in [
        ("BULL", {"vix": 16.5, "qqq_regime_ok": True,  "btc_context": {"btc_trend": "bull"}}),
        ("BEAR", {"vix": 32.0, "qqq_regime_ok": False, "btc_context": {"btc_trend": "bear"}}),
        ("HIGH_VOL", {"vix": 38.0, "qqq_regime_ok": False, "btc_context": {"btc_trend": "bear"}}),
    ]:
        print(f"\n{'='*40} TEST {scenario} {'='*40}")
        summary = run_bot_z_cycle(macro_test)
        print_bot_z_summary(summary)

"""
live/bot_z.py — Bot Z Enhanced — Paper Trading Production
==========================================================
Phase : PAPER TRADING (démarré 2026-03-06, revue 2026-04-30)
Capital : 10 000€ (4 bots validés × 2 500€)

Architecture Bot Z Enhanced (validée backtest 2020-2026, Run 4) :
  Bots A, B, C, G (state files) + Macro (VIX, QQQ, BTC)
      ↓
  Regime Engine (4 régimes VIX+QQQ+BTC)
      ↓
  Momentum Overlay (BTC < EMA200 AND QQQ < SMA200 → force BEAR)
      ↓
  Portfolio Engine (régime pur, allocation 100% dynamique)
      ↓
  Circuit Breaker (-25% DD → expo 30%, récupération +0.5%/j)
      ↓
  logs/bot_z/shadow.jsonl + logs/bot_z/state.json

Régimes + Momentum Overlay :
  BULL     : QQQ > SMA200 + BTC bull + VIX < 25
  RANGE    : QQQ > SMA200 + VIX entre 18 et 30
  BEAR     : QQQ < SMA200 ou VIX > 30 — ou forcé par MO (BTC+QQQ bearish)
  HIGH_VOL : VIX > 35 — ou forcé par MO (un seul indicateur bearish)

Calibration BEAR v2 (prouvée 2022 : -9% vs -17% equal-weight) :
  BULL     : G×1.2, B×1.0, A×0.8, C×0.5
  RANGE    : A×1.0, G×0.8, B×0.8, C×0.7
  BEAR     : C×1.5, G×1.2, A×0.3, B×0.0  ← SEULS défensifs 2022
  HIGH_VOL : C×1.0, G×0.8, A×0.5, B×0.3

Circuit Breaker :
  DD portefeuille < -25% → cb_factor réduit à 0.30 (70% cash)
  Récupération progressive +0.5%/jour quand DD > -10%

Résultats backtest (2020-2026, 6 ans) :
  Enhanced : CAGR +59.8% | Sharpe 1.61 | MaxDD -18.9%
  2022 bear : -9.0% (vs -16.8% equal-weight) — edge BEAR confirmé
  Walk-forward OOS 2023-2026 : +41.5% — edge réel (non sur-ajusté)
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

# ── Paper Trading — configuration production ──────────────────────────────────
INITIAL_CAP       = 10000.0  # 10 000€ total (4 bots validés × 2 500€)
PAPER_START_DATE  = "2026-03-06"
PAPER_REVIEW_DATE = "2026-04-30"

# ── Calibration BEAR v2 (validée backtest 2020-2026) ─────────────────────────
# Bots valides pour Bot Z Enhanced : A, B, C, G uniquement
# H=0 trades en daily | I=churn excessif
VALID_BOTS = ["a", "b", "c", "g"]

REGIME_WEIGHTS = {
    "BULL":     {"a": 0.8, "b": 1.0, "c": 0.5, "g": 1.2},
    "RANGE":    {"a": 1.0, "b": 0.8, "c": 0.7, "g": 0.8},
    "BEAR":     {"a": 0.3, "b": 0.0, "c": 1.5, "g": 1.2},  # C+G : seuls défensifs 2022
    "HIGH_VOL": {"a": 0.5, "b": 0.3, "c": 1.0, "g": 0.8},
}

# Priorité pour résolution de conflits actif (G le plus fiable en backtest)
BOT_PRIORITY = ["g", "c", "a", "b"]

# ── Limites de risque ────────────────────────────────────────────────────────
MAX_BOT_WEIGHT      = 0.40   # max 40% du capital sur un bot
MAX_ASSET_EXPOSURE  = 0.30   # max 30% du capital sur un même actif
MAX_BOTS_SAME_ASSET = 2      # max 2 bots simultanés long sur le même actif
CASH_VIX_THRESHOLD  = 35.0   # VIX > 35 → forcer cash 30%
TARGET_VOL          = 0.15   # volatilité cible (pour rolling score)

# ── Circuit Breaker ──────────────────────────────────────────────────────────
CB_THRESHOLD  = -0.25   # -25% DD → réduction exposition
CB_MIN_FACTOR = 0.30    # exposition minimale (30% = 70% cash)
CB_RECOVERY   = 0.005   # +0.5%/cycle de récupération progressive

# ── State files des bots valides ─────────────────────────────────────────────
BOT_STATE_FILES = {
    "a": "logs/supertrend/state.json",
    "b": "logs/momentum/state.json",
    "c": "logs/breakout/state.json",
    "g": "logs/trend/state.json",
}

BOT_NAMES = {
    "a": "Supertrend+MR",
    "b": "Momentum",
    "c": "Breakout",
    "g": "Trend Multi-Asset",
}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "initial_capital": INITIAL_CAP,
        "capital_simulated": INITIAL_CAP,
        "paper_start_date": PAPER_START_DATE,
        "paper_review_date": PAPER_REVIEW_DATE,
        "cb_peak": INITIAL_CAP,
        "cb_factor": 1.0,
        "regime_history": [],
        "allocation_history": [],
        "shadow_trades": [],
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
    Détecte le régime avec Momentum Overlay (Bot Z Enhanced).

    Ordre de priorité :
      1. HIGH_VOL : VIX > 35
      2. BEAR     : QQQ < SMA200 ou VIX > 30
      3. BULL     : QQQ > SMA200 + BTC bull + VIX < 25
      4. RANGE    : tout le reste

    Momentum Overlay (couche supplémentaire) :
      BTC bearish (trend bear) AND QQQ bearish → force BEAR
      Un seul bearish → force HIGH_VOL si le régime était BULL/RANGE
    """
    vix      = macro.get("vix", 15.0)
    qqq_ok   = macro.get("qqq_regime_ok", True)
    btc_ctx  = macro.get("btc_context", {})
    btc_trend = btc_ctx.get("btc_trend", "bull")

    # Régime de base
    if vix > CASH_VIX_THRESHOLD:
        regime = "HIGH_VOL"
    elif not qqq_ok or vix > 30:
        regime = "BEAR"
    elif vix < 25 and btc_trend in ("bull", "strong_bull"):
        regime = "BULL"
    else:
        regime = "RANGE"

    # Momentum Overlay : BTC EMA200 + QQQ SMA200
    btc_bearish = btc_trend in ("bear", "strong_bear")
    qqq_bearish = not qqq_ok

    if btc_bearish and qqq_bearish:
        regime = "BEAR"
    elif (btc_bearish or qqq_bearish) and regime in ("BULL", "RANGE"):
        regime = "HIGH_VOL"

    return regime


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

def compute_shadow_allocation(regime: str, all_states: dict, macro: dict,
                              cb_factor: float = 1.0) -> dict:
    """
    Calcule l'allocation Bot Z Enhanced pour chaque bot.

    Structure : régime pur 100% dynamique + circuit breaker
      - Poids par régime (calibration v2, validée backtest 2022)
      - Modulation par qualité récente (rolling score)
      - Exposition finale × cb_factor (circuit breaker)
      - Cap par bot 40%

    Retourne {bot_id: {budget_eur, budget_pct, weight_*, cb_factor, open_positions}}
    """
    vix      = macro.get("vix", 15.0)
    regime_w = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["RANGE"])
    exposure = get_exposure(all_states)

    # Cash forcé si VIX > seuil
    if vix > CASH_VIX_THRESHOLD:
        cb_factor = min(cb_factor, 0.70)  # 30% cash minimum en HIGH_VOL extrême

    effective_capital = INITIAL_CAP * cb_factor

    # ── Poids bruts par régime × qualité récente ─────────────────────────────
    raw_weights = {}
    for bot_id, state in all_states.items():
        rw      = regime_w.get(bot_id, 0.0)
        quality = compute_rolling_score(bot_id, state)
        raw_weights[bot_id] = rw * quality

    total_w = sum(raw_weights.values()) or 1.0
    norm_weights = {k: v / total_w for k, v in raw_weights.items()}

    # ── Budget final par bot ─────────────────────────────────────────────────
    allocation = {}
    for bot_id, state in all_states.items():
        w        = norm_weights[bot_id]
        total_eur = effective_capital * w

        # Cap par bot (40%)
        max_budget = INITIAL_CAP * MAX_BOT_WEIGHT
        total_eur  = min(total_eur, max_budget)

        # Réduction si surexposition actif
        positions = state.get("positions", {})
        for sym in positions:
            bots_on_sym = exposure.get(sym, [])
            if len(bots_on_sym) > MAX_BOTS_SAME_ASSET:
                priority_bots = bots_on_sym[:MAX_BOTS_SAME_ASSET]
                if bot_id not in priority_bots:
                    total_eur *= 0.3

        budget_pct = total_eur / INITIAL_CAP if INITIAL_CAP > 0 else 0
        quality_w  = compute_rolling_score(bot_id, state)

        allocation[bot_id] = {
            "budget_eur":    round(total_eur, 0),
            "budget_pct":    round(budget_pct * 100, 1),
            "weight_regime": round(regime_w.get(bot_id, 0.0), 2),
            "weight_quality": round(quality_w, 2),
            "weight_final":  round(w, 3),
            "cb_factor":     round(cb_factor, 2),
            "bot_name":      BOT_NAMES.get(bot_id, bot_id),
            "open_positions": list(positions.keys()),
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
    Exécute un cycle Bot Z Enhanced (paper trading production).
    Retourne le résumé du cycle pour logging et dashboard.
    """
    ts = datetime.now().isoformat()

    # 1. Charger les états de tous les bots valides
    all_states = {bot_id: load_bot_state(bot_id) for bot_id in BOT_STATE_FILES}

    # 2. Détecter le régime + Momentum Overlay
    regime_info = detect_regime_score(macro)
    regime = regime_info["regime"]

    # 3. Circuit Breaker — calcul DD portefeuille
    state = load_state()
    portfolio_values = {}
    total_simulated = 0.0
    for bot_id, s in all_states.items():
        val = s.get("capital", INITIAL_CAP / len(BOT_STATE_FILES)) + sum(
            p.get("entry", 0) * p.get("size", 0)
            for p in s.get("positions", {}).values()
        )
        portfolio_values[bot_id] = round(val, 2)
        total_simulated += val

    cb_peak   = max(state.get("cb_peak", INITIAL_CAP), total_simulated)
    cb_factor = state.get("cb_factor", 1.0)
    port_dd   = (total_simulated - cb_peak) / cb_peak if cb_peak > 0 else 0.0

    if port_dd < CB_THRESHOLD:
        cb_factor = max(CB_MIN_FACTOR, cb_factor - 0.05)
    elif port_dd > -0.10:
        cb_factor = min(1.0, cb_factor + CB_RECOVERY)

    cb_active = cb_factor < 1.0

    # 4. Allocation Enhanced (régime pur + CB)
    allocation = compute_shadow_allocation(regime, all_states, macro, cb_factor)

    # 5. Analyse exposition croisée
    cross = analyze_cross_exposure(all_states, allocation)

    # 6. Warnings
    warnings_list = []
    if cb_active:
        warnings_list.append(
            f"CIRCUIT BREAKER actif — DD={port_dd*100:.1f}% | expo={cb_factor*100:.0f}%"
        )
    vix = macro.get("vix", 15.0)
    if vix > CASH_VIX_THRESHOLD:
        warnings_list.append(f"HIGH_VOL forcé (VIX={vix:.1f} > {CASH_VIX_THRESHOLD})")
    for sym, info in cross.items():
        if info["warning"]:
            prio = info["priority_bot"]
            prio_name = BOT_NAMES.get(prio, prio) if prio else "?"
            warnings_list.append(
                f"{sym}: {info['n_bots']} bots ({info['estimated_exposure_pct']:.0f}%) "
                f"— priorité {prio_name}"
            )

    # 7. Métriques de performance
    perf_pct = (total_simulated - INITIAL_CAP) / INITIAL_CAP * 100
    days_running = (datetime.now() - datetime.fromisoformat(PAPER_START_DATE)).days

    # 8. Construction du résumé
    summary = {
        "timestamp":          ts,
        "regime":             regime,
        "regime_confidence":  regime_info["confidence"],
        "vix":                vix,
        "qqq_ok":             macro.get("qqq_regime_ok", True),
        "btc_trend":          macro.get("btc_context", {}).get("btc_trend", "?"),
        "allocation":         allocation,
        "cross_exposure":     cross,
        "warnings":           warnings_list,
        "portfolio_values":   portfolio_values,
        "total_simulated_eur": round(total_simulated, 2),
        "initial_capital":    INITIAL_CAP,
        "perf_pct":           round(perf_pct, 2),
        "cb_factor":          round(cb_factor, 2),
        "cb_active":          cb_active,
        "port_dd":            round(port_dd * 100, 2),
        "days_running":       days_running,
        "paper_start":        PAPER_START_DATE,
        "paper_review":       PAPER_REVIEW_DATE,
    }

    # 9. Log shadow
    _log_shadow(summary)

    # 10. Mise à jour state
    state["cb_peak"]   = round(cb_peak, 2)
    state["cb_factor"] = round(cb_factor, 3)
    state["regime_history"].append({"ts": ts, "regime": regime, "confidence": regime_info["confidence"]})
    state["regime_history"] = state["regime_history"][-500:]
    state["allocation_history"].append({"ts": ts, "allocation": {k: v["budget_eur"] for k, v in allocation.items()}})
    state["allocation_history"] = state["allocation_history"][-500:]
    state["last_regime"]         = regime
    state["last_regime_info"]    = regime_info
    state["last_allocation"]     = allocation
    state["last_cross_exposure"] = cross
    state["last_warnings"]       = warnings_list
    state["last_portfolio_values"] = portfolio_values
    state["total_simulated_eur"] = round(total_simulated, 2)
    state["perf_pct"]            = round(perf_pct, 2)
    state["days_running"]        = days_running
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

    cb_factor = summary.get("cb_factor", 1.0)
    cb_active = summary.get("cb_active", False)
    port_dd   = summary.get("port_dd", 0.0)
    days      = summary.get("days_running", 0)
    review    = summary.get("paper_review", PAPER_REVIEW_DATE)
    cb_c      = Fore.RED if cb_active else Fore.GREEN

    print(f"\n{Fore.CYAN}{'─'*78}")
    print(f"  BOT Z ENHANCED — PAPER TRADING | {ts}")
    print(f"  Jour {days} / revue {review} | Capital initial : {INITIAL_CAP:.0f}€")
    print(f"  Régime : {c}{r}{Style.RESET_ALL} (conf={conf:.0%}) | VIX={summary['vix']:.1f} | "
          f"QQQ={'✓' if summary['qqq_ok'] else '✗'} | BTC={summary['btc_trend']}")
    print(f"  CB : {cb_c}×{cb_factor:.0%}{Style.RESET_ALL} (DD={port_dd:+.1f}%) | "
          f"Expo effective : {INITIAL_CAP * cb_factor:.0f}€")
    print(f"{'─'*78}{Style.RESET_ALL}")

    print(f"  {'Bot':<22} {'Budget':>8} {'%Cap':>6} {'Rég':>6} {'Qual':>6}  Positions")
    print(f"  {'─'*76}")
    alloc = summary.get("allocation", {})
    for bot_id in sorted(alloc):
        a = alloc[bot_id]
        pct = a["budget_pct"]
        budget_c = Fore.GREEN if pct >= 20 else (Fore.YELLOW if pct >= 10 else Fore.RED)
        pos_str = ", ".join(a["open_positions"][:3]) or "—"
        print(f"  {a['bot_name']:<22} {budget_c}{a['budget_eur']:>7.0f}€{Style.RESET_ALL}  "
              f"{pct:>5.1f}%  {a['weight_regime']:>5.2f}  {a['weight_quality']:>5.2f}  {pos_str}")

    if summary["warnings"]:
        print(f"\n  {Fore.YELLOW}⚠ ALERTES :{Style.RESET_ALL}")
        for w in summary["warnings"]:
            print(f"    • {w}")

    total  = summary.get("total_simulated_eur", 0)
    perf   = summary.get("perf_pct", 0)
    perf_c = Fore.GREEN if perf >= 0 else Fore.RED
    print(f"\n  Portefeuille : {total:.2f}€  ({perf_c}{perf:+.2f}%{Style.RESET_ALL}) "
          f"vs initial {INITIAL_CAP:.0f}€")
    print(f"{Fore.CYAN}{'─'*78}{Style.RESET_ALL}\n")


# ── Standalone (test) ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test avec différents régimes
    for scenario, macro_test in [
        ("BULL",     {"vix": 16.5, "qqq_regime_ok": True,  "btc_context": {"btc_trend": "bull"}}),
        ("RANGE→HV", {"vix": 22.0, "qqq_regime_ok": True,  "btc_context": {"btc_trend": "bear"}}),
        ("BEAR",     {"vix": 32.0, "qqq_regime_ok": False, "btc_context": {"btc_trend": "bear"}}),
        ("HIGH_VOL", {"vix": 38.0, "qqq_regime_ok": False, "btc_context": {"btc_trend": "bear"}}),
    ]:
        print(f"\n{'='*40} TEST {scenario} {'='*40}")
        summary = run_bot_z_cycle(macro_test)
        print_bot_z_summary(summary)

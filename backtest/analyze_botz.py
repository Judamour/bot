"""
backtest/analyze_botz.py — Analyse historique Bot Z Meta v2
===========================================================
Lit logs/bot_z/shadow.jsonl et génère un rapport complet pour
optimiser les paramètres avant le passage en live.

Usage :
    python backtest/analyze_botz.py
    python backtest/analyze_botz.py --csv        # export CSV
    python backtest/analyze_botz.py --min-cycles 20  # données minimales

Ce rapport permet de répondre aux questions clés :
  - Quel engine a le mieux performé ?
  - Le circuit breaker se déclenche-t-il trop tôt / tard ?
  - Les régimes détectés sont-ils pertinents ?
  - Les rolling scores convergent-ils ?
  - Les switchs d'engine sont-ils trop fréquents ?
"""
import json
import os
import sys
import math
import argparse
import csv
from datetime import datetime
from collections import defaultdict

# Couleurs terminal
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
    G = Fore.GREEN; R = Fore.RED; Y = Fore.YELLOW; C = Fore.CYAN; M = Fore.MAGENTA; W = Fore.WHITE; DIM = Style.DIM; RST = Style.RESET_ALL
except ImportError:
    G = R = Y = C = M = W = DIM = RST = ""

SHADOW_LOG = "logs/bot_z/shadow.jsonl"
REPORT_DIR = "backtest/results/botz_analysis"
INITIAL_CAP = 10000.0

ENGINE_COLORS = {"ENHANCED": G, "OMEGA": C, "OMEGA_V2": Y, "PRO": R}
REGIME_COLORS = {"BULL": G, "BEAR": R, "RANGE": C, "HIGH_VOL": Y}

BOT_NAMES = {"a": "Supertrend+MR", "b": "Momentum", "c": "Breakout", "g": "Trend"}


def load_shadow(path: str) -> list:
    records = []
    if not os.path.exists(path):
        print(f"{R}Fichier introuvable : {path}{RST}")
        print(f"  → Assurez-vous que Bot Z a tourné au moins une fois.")
        sys.exit(1)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def compute_mcps(records: list) -> dict | None:
    """Marginal Contribution to Portfolio Sharpe (MCPS) — méthode hedge fund.

    Pour chaque bot, calcule si son ajout augmente ou réduit le Sharpe global.
    Utilise les bot_values de shadow.jsonl (valeurs € par bot à chaque cycle).
    Nécessite 20+ cycles pour être significatif.
    """
    bot_ids = ["a", "b", "c", "g"]
    bot_value_series = {b: [] for b in bot_ids}

    for r in records:
        bv = r.get("bot_values", {})
        if bv and all(b in bv and bv[b] > 0 for b in bot_ids):
            for b in bot_ids:
                bot_value_series[b].append(float(bv[b]))

    n = min(len(v) for v in bot_value_series.values())
    if n < 10:
        return None

    # Returns cycle par cycle pour chaque bot
    bot_returns = {}
    for b in bot_ids:
        vals = bot_value_series[b][:n]
        bot_returns[b] = [(vals[i] / vals[i-1] - 1) if vals[i-1] > 0 else 0.0
                          for i in range(1, n)]

    m = n - 1  # nombre de returns

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    def std(xs):
        if len(xs) < 2:
            return 0.0
        mu = mean(xs)
        return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))

    def sharpe(rets):
        if len(rets) < 3:
            return 0.0
        mu, sigma = mean(rets), std(rets)
        if sigma == 0:
            return 0.0
        return mu / sigma * math.sqrt(6 * 365)  # 6 cycles/jour

    def corr(x, y):
        mx, my = mean(x), mean(y)
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        dx = std(x) * len(x) ** 0.5
        dy = std(y) * len(y) ** 0.5
        return num / (dx * dy) if dx > 0 and dy > 0 else 0.0

    # Portfolio = moyenne équipondérée des 4 bots
    port_rets = [sum(bot_returns[b][i] for b in bot_ids) / len(bot_ids) for i in range(m)]
    port_sharpe = sharpe(port_rets)

    results = {}
    for b in bot_ids:
        rets_b = bot_returns[b]
        rho = corr(rets_b, port_rets)
        sharpe_b = sharpe(rets_b)
        mcps = sharpe_b - rho * port_sharpe  # contribution marginale

        # Sharpe portfolio sans ce bot
        others = [bid for bid in bot_ids if bid != b]
        port_without = [sum(bot_returns[bid][i] for bid in others) / len(others) for i in range(m)]
        sharpe_without = sharpe(port_without)
        incremental = port_sharpe - sharpe_without  # >0 = bot utile

        results[b] = {
            "sharpe_solo": round(sharpe_b, 3),
            "corr_to_portfolio": round(rho, 3),
            "mcps": round(mcps, 3),
            "sharpe_without": round(sharpe_without, 3),
            "incremental_sharpe": round(incremental, 3),
        }

    return {"port_sharpe": round(port_sharpe, 3), "bots": results, "n_cycles": m}


def fmt_pct(val, sign=True):
    v = float(val)
    return (("+" if v >= 0 and sign else "") + f"{v:.2f}%")


def fmt_eur(val):
    return f"{float(val):,.2f}€"


def bar(val, max_val, width=20, char="█"):
    if max_val <= 0:
        return ""
    n = max(0, min(width, int(val / max_val * width)))
    return char * n + "░" * (width - n)


def separator(title="", width=76):
    if title:
        side = (width - len(title) - 2) // 2
        print(f"\n{C}{'─'*side} {title} {'─'*(width-side-len(title)-2)}{RST}")
    else:
        print(f"{DIM}{'─'*width}{RST}")


# ── Analyse principale ────────────────────────────────────────────────────────

def analyze(records: list, export_csv: bool = False):
    n = len(records)
    if n == 0:
        print(f"{R}Aucun enregistrement dans shadow.jsonl{RST}")
        return

    first = records[0]
    last  = records[-1]
    start = first.get("timestamp", "?")[:10]
    end   = last.get("timestamp",  "?")[:10]
    days  = last.get("days_running", 0)

    z_initial  = INITIAL_CAP
    z_current  = float(last.get("z_capital_eur", last.get("total_simulated_eur", INITIAL_CAP)))
    z_perf_pct = (z_current - z_initial) / z_initial * 100

    # ── Pic et MaxDD ──────────────────────────────────────────────────────────
    peak = z_initial
    max_dd = 0.0
    for r in records:
        v = float(r.get("z_capital_eur", r.get("total_simulated_eur", z_initial)))
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)

    # ── Distribution engines ──────────────────────────────────────────────────
    engine_counts = defaultdict(int)
    engine_pnl    = defaultdict(float)   # P&L cumulé par cycle quand engine actif
    engine_start_val = {}
    prev_val = z_initial

    for i, r in enumerate(records):
        eng = r.get("current_engine", "UNKNOWN")
        val = float(r.get("z_capital_eur", r.get("total_simulated_eur", prev_val)))
        engine_counts[eng] += 1
        if i > 0:
            cycle_pnl = val - prev_val
            engine_pnl[eng] += cycle_pnl
        prev_val = val

    # ── Distribution régimes ──────────────────────────────────────────────────
    regime_counts = defaultdict(int)
    regime_pnl    = defaultdict(float)
    prev_val = z_initial

    for i, r in enumerate(records):
        reg = r.get("regime", "UNKNOWN")
        val = float(r.get("z_capital_eur", r.get("total_simulated_eur", prev_val)))
        regime_counts[reg] += 1
        if i > 0:
            regime_pnl[reg] += val - prev_val
        prev_val = val

    # ── Switchs d'engine ─────────────────────────────────────────────────────
    switches = []
    prev_eng = None
    for r in records:
        eng = r.get("current_engine", "?")
        reason = r.get("engine_reason", {})
        if eng != prev_eng and prev_eng is not None:
            switches.append({
                "ts":               r.get("timestamp", "?")[:16],
                "from":             prev_eng,
                "to":               eng,
                "hard_rule":        reason.get("hard_rule_pro", False),
                "vix":              reason.get("vix", "?"),
                "dd_pct":           reason.get("port_dd_pct", "?"),
                "regime":           reason.get("regime", "?"),
                "raw_engine":       reason.get("raw_engine", "?"),
                "regime_confidence": r.get("regime_confidence", ""),
                "btc_realized_vol": r.get("btc_realized_vol", ""),
            })
        prev_eng = eng

    # ── Circuit Breaker ───────────────────────────────────────────────────────
    cb_activations = []
    in_cb = False
    for r in records:
        cb = r.get("cb_active", False) or float(r.get("cb_factor", 1.0)) < 1.0
        cf = float(r.get("cb_factor", 1.0))
        dd = float(r.get("port_dd", 0))
        if cb and not in_cb:
            cb_activations.append({"ts": r.get("timestamp","?")[:16], "factor": cf, "dd_pct": dd})
            in_cb = True
        elif not cb:
            in_cb = False

    cb_min_factor = min((float(r.get("cb_factor", 1.0)) for r in records), default=1.0)

    # ── Qualité des bots (rolling scores) ────────────────────────────────────
    bot_scores_history = defaultdict(list)
    for r in records:
        reason = r.get("engine_reason", {})
        scores = reason.get("rolling_scores", {})
        for b, s in scores.items():
            bot_scores_history[b].append(float(s))

    bot_score_avg = {b: sum(v)/len(v) for b, v in bot_scores_history.items() if v}
    bot_score_last = {b: v[-1] for b, v in bot_scores_history.items() if v}

    # ── Volatilité des bots ───────────────────────────────────────────────────
    bot_vols_history = defaultdict(list)
    for r in records:
        reason = r.get("engine_reason", {})
        vols = reason.get("bot_vols", {})
        for b, v in vols.items():
            bot_vols_history[b].append(float(v))

    bot_vol_avg = {b: sum(v)/len(v) for b, v in bot_vols_history.items() if v}

    # ── Allocations moyennes ──────────────────────────────────────────────────
    bot_budget_history = defaultdict(list)
    for r in records:
        budget = r.get("budget", {})
        for b, v in budget.items():
            bot_budget_history[b].append(float(v))

    bot_budget_avg = {b: sum(v)/len(v) for b, v in bot_budget_history.items() if v}

    # ── Contribution au profit par bot (risque concentration) ────────────────
    # Approximation : profit attribué au bot proportionnellement à son budget
    bot_profit_contrib = defaultdict(float)
    prev_val = z_initial
    for r in records[1:]:
        val = float(r.get("z_capital_eur", r.get("total_simulated_eur", prev_val)))
        cycle_pnl = val - prev_val
        if cycle_pnl == 0:
            prev_val = val
            continue
        budget = r.get("budget", {})
        total_budget = sum(float(v) for v in budget.values()) or 1.0
        for b, bgt in budget.items():
            weight = float(bgt) / total_budget
            bot_profit_contrib[b] += cycle_pnl * weight
        prev_val = val

    total_contrib = sum(abs(v) for v in bot_profit_contrib.values()) or 1.0
    bot_contrib_pct = {b: v / total_contrib for b, v in bot_profit_contrib.items()}

    # ── VIX moyen ────────────────────────────────────────────────────────────
    vix_values = [float(r.get("vix", 0)) for r in records if r.get("vix")]
    vix_avg = sum(vix_values) / len(vix_values) if vix_values else 0
    vix_max = max(vix_values) if vix_values else 0

    # ── Vol targeting (Meta v2+) ──────────────────────────────────────────────
    vol_factors   = [float(r.get("vol_factor", 1.0)) for r in records if "vol_factor" in r]
    portfolio_vols = [float(r.get("portfolio_vol", 0)) for r in records if "portfolio_vol" in r]
    vol_factor_avg = sum(vol_factors) / len(vol_factors) if vol_factors else 1.0
    vol_factor_min = min(vol_factors) if vol_factors else 1.0
    vol_factor_max = max(vol_factors) if vol_factors else 1.0
    n_vol_reduced  = sum(1 for v in vol_factors if v < 0.95)
    n_vol_boosted  = sum(1 for v in vol_factors if v > 1.05)
    port_vol_avg   = sum(portfolio_vols) / len(portfolio_vols) if portfolio_vols else 0.0

    # ── Corrélation inter-bots (Meta v2+) ─────────────────────────────────────
    corr_values    = [float(r.get("avg_bot_corr", 0)) for r in records if "avg_bot_corr" in r]
    corr_avg       = sum(corr_values) / len(corr_values) if corr_values else 0.0
    corr_max       = max(corr_values) if corr_values else 0.0
    n_corr_reduced = sum(1 for r in records if float(r.get("corr_factor", 1.0)) < 1.0)

    # ── Allocation drift (Meta v2+) ────────────────────────────────────────────
    drift_values  = [float(r.get("alloc_drift", 0)) for r in records if "alloc_drift" in r]
    drift_avg     = sum(drift_values) / len(drift_values) if drift_values else 0.0
    drift_max     = max(drift_values) if drift_values else 0.0
    drift_last    = drift_values[-1] if drift_values else 0.0
    n_drift_warn  = sum(1 for v in drift_values if v > 0.20)

    # ── Regime confidence & persistence (Meta v2+) ────────────────────────────
    conf_values     = [float(r.get("regime_confidence", 1.0)) for r in records if "regime_confidence" in r]
    strength_values = [float(r.get("regime_strength", 1.0)) for r in records if "regime_strength" in r]
    conf_avg        = sum(conf_values) / len(conf_values) if conf_values else 1.0
    strength_avg    = sum(strength_values) / len(strength_values) if strength_values else 1.0
    n_low_conf      = sum(1 for v in conf_values if v < 0.5)

    # ── BTC realized vol overrides (Meta v2+) ─────────────────────────────────
    btc_vols         = [float(r.get("btc_realized_vol", 0)) for r in records if "btc_realized_vol" in r]
    btc_vol_avg      = sum(btc_vols) / len(btc_vols) if btc_vols else 0.0
    btc_vol_max      = max(btc_vols) if btc_vols else 0.0
    n_btc_highvol    = sum(1 for v in btc_vols if v > 0.80)

    # ─────────────────────────────────────────────────────────────────────────
    # AFFICHAGE
    # ─────────────────────────────────────────────────────────────────────────

    print(f"\n{C}{'═'*76}")
    print(f"  BOT Z META v2 — RAPPORT D'ANALYSE HISTORIQUE")
    print(f"  {start} → {end} | {days} jours | {n} cycles enregistrés")
    print(f"{'═'*76}{RST}")

    # ── 1. Résumé global ─────────────────────────────────────────────────────
    separator("RÉSUMÉ GLOBAL")
    perf_c = G if z_perf_pct >= 0 else R
    print(f"  Capital initial : {fmt_eur(z_initial)}")
    print(f"  Capital actuel  : {fmt_eur(z_current)}  ({perf_c}{fmt_pct(z_perf_pct)}{RST})")
    print(f"  P&L absolu      : {perf_c}{fmt_eur(z_current - z_initial)}{RST}")
    print(f"  Max Drawdown    : {R}{fmt_pct(max_dd)}{RST}")
    print(f"  VIX moyen       : {vix_avg:.1f}  |  Max VIX : {vix_max:.1f}")
    mtm_live_count = sum(1 for r in records if r.get("mtm_live", False))
    print(f"  Mark-to-market  : {mtm_live_count}/{n} cycles au prix live ({mtm_live_count/n*100:.0f}%)")

    # ── 2. Performance par engine ─────────────────────────────────────────────
    separator("PERFORMANCE PAR ENGINE")
    print(f"  {'Engine':<12} {'Cycles':>7} {'% temps':>8} {'P&L cumulé':>12}  Barre")
    separator()
    total_cycles = sum(engine_counts.values()) or 1
    max_abs_pnl = max(abs(v) for v in engine_pnl.values()) if engine_pnl else 1
    for eng in ["ENHANCED", "OMEGA", "OMEGA_V2", "PRO"]:
        cnt  = engine_counts[eng]
        pnl  = engine_pnl[eng]
        pct  = cnt / total_cycles * 100
        ec   = ENGINE_COLORS.get(eng, W)
        pnl_c = G if pnl >= 0 else R
        b = bar(abs(pnl), max_abs_pnl, 16)
        print(f"  {ec}{eng:<12}{RST} {cnt:>7}  {pct:>7.1f}%  {pnl_c}{pnl:>+10.2f}€{RST}  {b}")

    # ── 3. Performance par régime ─────────────────────────────────────────────
    separator("PERFORMANCE PAR RÉGIME")
    print(f"  {'Régime':<12} {'Cycles':>7} {'% temps':>8} {'P&L cumulé':>12}  Barre")
    separator()
    max_abs_rpnl = max(abs(v) for v in regime_pnl.values()) if regime_pnl else 1
    for reg in ["BULL", "RANGE", "HIGH_VOL", "BEAR"]:
        cnt  = regime_counts.get(reg, 0)
        pnl  = regime_pnl.get(reg, 0)
        pct  = cnt / total_cycles * 100
        rc   = REGIME_COLORS.get(reg, W)
        pnl_c = G if pnl >= 0 else R
        b = bar(abs(pnl), max_abs_rpnl, 16)
        print(f"  {rc}{reg:<12}{RST} {cnt:>7}  {pct:>7.1f}%  {pnl_c}{pnl:>+10.2f}€{RST}  {b}")

    # ── 4. Switchs d'engine ───────────────────────────────────────────────────
    separator(f"SWITCHS D'ENGINE ({len(switches)} au total)")
    if not switches:
        print(f"  {DIM}Aucun switch — engine stable{RST}")
    else:
        print(f"  {'Date':<17} {'Transition':<20} {'Hard rule':>10} {'VIX':>5} {'DD%':>6}  Régime")
        separator()
        for sw in switches[-20:]:  # derniers 20
            hard = f"{R}OUI{RST}" if sw["hard_rule"] else f"{G}non{RST}"
            fr_c = ENGINE_COLORS.get(sw["from"], W)
            to_c = ENGINE_COLORS.get(sw["to"], W)
            print(f"  {sw['ts']:<17} {fr_c}{sw['from']:<10}{RST}→ {to_c}{sw['to']:<9}{RST} {hard:>14}  "
                  f"{sw['vix']:>5}  {sw['dd_pct']:>6.1f}%  {sw['regime']}")
        if len(switches) > 20:
            print(f"  {DIM}... {len(switches)-20} switchs antérieurs non affichés{RST}")

        # Fréquence des switchs
        freq = len(switches) / max(days, 1)
        print(f"\n  Fréquence : {freq:.2f} switch/jour | {len(switches)} total sur {days} jours")
        if freq > 0.3:
            print(f"  {Y}⚠ Fréquence élevée — envisager d'augmenter l'hysteresis{RST}")
        else:
            print(f"  {G}✓ Fréquence normale{RST}")

    # ── 5. Circuit Breaker ────────────────────────────────────────────────────
    separator(f"CIRCUIT BREAKER ({len(cb_activations)} activation(s))")
    if not cb_activations:
        print(f"  {G}✓ Circuit Breaker jamais déclenché{RST}")
    else:
        print(f"  {'Date':<17} {'Factor':>8} {'DD%':>8}")
        separator()
        for cb in cb_activations:
            print(f"  {cb['ts']:<17} {R}×{cb['factor']:.0%}{RST}  {cb['dd_pct']:>+7.2f}%")
    print(f"  Factor minimum atteint : {R if cb_min_factor < 1.0 else G}×{cb_min_factor:.0%}{RST}")

    # ── 6. Qualité et volatilité des bots ─────────────────────────────────────
    separator("QUALITÉ ET VOLATILITÉ DES BOTS (rolling)")
    print(f"  {'Bot':<20} {'Score moy':>10} {'Score act':>10} {'Vol moy':>10} {'Budget moy':>12}")
    separator()
    for b in ["a", "b", "c", "g"]:
        name = BOT_NAMES.get(b, b)
        sc_avg  = bot_score_avg.get(b, 1.0)
        sc_last = bot_score_last.get(b, 1.0)
        vol_avg = bot_vol_avg.get(b, 0.15)
        bgt_avg = bot_budget_avg.get(b, 0)
        sc_c = G if sc_last > 1.1 else (R if sc_last < 0.9 else W)
        print(f"  {name:<20} {sc_avg:>10.3f} {sc_c}{sc_last:>10.3f}{RST} {vol_avg:>9.1%} {bgt_avg:>10.0f}€")

    # ── 7. Contribution au profit par bot (risque concentration) ─────────────
    separator("CONTRIBUTION AU PROFIT PAR BOT — Risque concentration")
    print(f"  {'Bot':<20} {'Contrib €':>10} {'Contrib %':>10}  Barre  Alerte")
    separator()
    DANGER_THRESHOLD = 0.70
    has_contrib = sum(abs(v) for v in bot_profit_contrib.values()) > 0
    for b in ["a", "b", "c", "g"]:
        name  = BOT_NAMES.get(b, b)
        pnl   = bot_profit_contrib.get(b, 0)
        pct   = bot_contrib_pct.get(b, 0)
        bc    = {"a": C, "b": G, "c": Y, "g": M}.get(b, W)
        pnl_c = G if pnl >= 0 else R
        b_bar = bar(abs(pct), 1.0, 20)
        alert = f"  {R}⚠ CONCENTRATION > 70%{RST}" if pct > DANGER_THRESHOLD else ""
        if has_contrib:
            print(f"  {bc}{name:<20}{RST} {pnl_c}{pnl:>+9.0f}€{RST}  {pct:>8.0%}  {b_bar}{alert}")
        else:
            print(f"  {bc}{name:<20}{RST}  {DIM}pas encore de données P&L{RST}")
    if has_contrib:
        top = max(bot_contrib_pct, key=bot_contrib_pct.get)
        top_pct = bot_contrib_pct[top]
        if top_pct > DANGER_THRESHOLD:
            print(f"\n  {R}DANGER : {BOT_NAMES.get(top, top)} concentre {top_pct:.0%} du profit{RST}")
            print(f"  {R}→ Envisager MAX_BOT_WEIGHT 0.40 → 0.30 pour ce bot{RST}")
        else:
            print(f"\n  {G}✓ Concentration acceptable — aucun bot > 70%{RST}")

    # ── 8. Allocations moyennes ───────────────────────────────────────────────
    separator("ALLOCATION MOYENNE DISPATCHING")
    total_avg = sum(bot_budget_avg.values()) or 1
    for b in ["a", "b", "c", "g"]:
        name = BOT_NAMES.get(b, b)
        avg  = bot_budget_avg.get(b, 0)
        pct  = avg / INITIAL_CAP * 100
        bc   = {"a": C, "b": G, "c": Y, "g": M}.get(b, W)
        b_bar = bar(avg, INITIAL_CAP * 0.5, 24)
        print(f"  {bc}{name:<20}{RST} {avg:>8.0f}€  {pct:>5.1f}%  {b_bar}")

    # ── 8. Métriques Meta v2+ ─────────────────────────────────────────────────
    separator("META v2+ — VOL TARGETING / CORRÉLATION / DRIFT")

    # Vol targeting
    has_vt = len(vol_factors) > 0
    if has_vt:
        vt_c = Y if n_vol_reduced > 0 or n_vol_boosted > 0 else G
        print(f"  Vol targeting     : vol_factor moy={vol_factor_avg:.2f}  min={vol_factor_min:.2f}  max={vol_factor_max:.2f}")
        print(f"  Portfolio vol moy : {port_vol_avg:.0%}  |  "
              f"Réduit {n_vol_reduced}× (vol>cible)  |  Boosté {n_vol_boosted}× (vol<cible)")
        if n_vol_reduced > n * 0.3:
            print(f"  {Y}⚠ Vol targeting réduisait fréquemment l'expo → régime volatile{RST}")
        elif n_vol_boosted > n * 0.3:
            print(f"  {Y}⚠ Vol targeting boostait fréquemment → portfolio trop calme{RST}")
        else:
            print(f"  {G}✓ Vol targeting stable — ajustements marginaux{RST}")
    else:
        print(f"  {DIM}Vol targeting : données absentes (cycles pré-v2+){RST}")

    # Corrélation
    has_corr = len(corr_values) > 0
    if has_corr:
        print(f"\n  Corrélation moy   : {corr_avg:.0%}  |  Max : {corr_max:.0%}  |  "
              f"Réductions expo : {n_corr_reduced}×")
        if n_corr_reduced > 0:
            print(f"  {Y}⚠ Bots trop corrélés {n_corr_reduced} cycles → exposition réduite ×0.80{RST}")
        else:
            print(f"  {G}✓ Corrélation inter-bots sous le seuil — diversification suffisante{RST}")
    else:
        print(f"  {DIM}Corrélation : données absentes (cycles pré-v2+){RST}")

    # Drift allocation
    has_drift = len(drift_values) > 0
    if has_drift:
        drift_c = R if drift_last > 0.20 else (Y if drift_last > 0.10 else G)
        print(f"\n  Drift allocation  : moy={drift_avg:.0%}  max={drift_max:.0%}  actuel={drift_c}{drift_last:.0%}{RST}")
        print(f"  Warnings drift>20%: {n_drift_warn}× sur {len(drift_values)} cycles")
        if drift_last > 0.20:
            print(f"  {R}✗ Drift élevé : allocation cible ≠ réalité → backtest peu représentatif{RST}")
        elif drift_avg > 0.15:
            print(f"  {Y}⚠ Drift moyen élevé — budget dispatch branché résoudrait ce point{RST}")
        else:
            print(f"  {G}✓ Drift faible — paper trading cohérent avec backtest{RST}")
    else:
        print(f"  {DIM}Drift : données absentes (cycles pré-v2+){RST}")

    # Regime confidence & persistence
    if len(conf_values) > 0:
        print(f"\n  Confiance régime  : moy={conf_avg:.0%}  |  Faible (<50%) : {n_low_conf}×")
        print(f"  Persistance moy   : {strength_avg:.0%}  (pleine à 7j de régime stable)")
        if n_low_conf > n * 0.2:
            print(f"  {Y}⚠ Régime souvent incertain — OMEGA favorisé (neutre) {n_low_conf} cycles{RST}")
        else:
            print(f"  {G}✓ Régime stable et confiant la plupart du temps{RST}")
    else:
        print(f"  {DIM}Confidence/persistance : données absentes (cycles pré-v2+){RST}")

    # BTC realized vol
    if len(btc_vols) > 0:
        print(f"\n  BTC realized vol  : moy={btc_vol_avg:.0%}  max={btc_vol_max:.0%}  "
              f"  Overrides HIGH_VOL: {n_btc_highvol}×")
        if n_btc_highvol > 0:
            print(f"  {Y}⚠ BTC vol >80% a forcé HIGH_VOL {n_btc_highvol}× (VIX était potentiellement bas){RST}")
        else:
            print(f"  {G}✓ BTC vol sous le seuil — pas d'override crypto{RST}")
    else:
        print(f"  {DIM}BTC realized vol : données absentes (cycles pré-v2+){RST}")

    # ── 10. MCPS — Contribution marginale au Sharpe ──────────────────────────
    separator("MCPS — CONTRIBUTION MARGINALE AU SHARPE (méthode hedge fund)")
    mcps_data = compute_mcps(records)
    if mcps_data is None:
        print(f"  {DIM}Données insuffisantes (<10 cycles avec bot_values) — relancer après 2 semaines{RST}")
    else:
        nb = mcps_data["n_cycles"]
        ps = mcps_data["port_sharpe"]
        print(f"  Sharpe portfolio actuel : {C}{ps:+.3f}{RST}  ({nb} cycles analysés)")
        print(f"  {'Bot':<22} {'Sharpe solo':>12} {'Corr/Portfolio':>15} {'MCPS':>8} {'Sharpe sans':>12} {'Incrémental':>12}  Verdict")
        separator()
        for b in ["a", "b", "c", "g"]:
            d = mcps_data["bots"].get(b, {})
            name = BOT_NAMES.get(b, b.upper())
            col = G if d.get("incremental_sharpe", 0) > 0 else R
            verdict = "UTILE" if d.get("incremental_sharpe", 0) > 0 else "RETIRE"
            mcps_col = G if d.get("mcps", 0) > 0 else R
            print(f"  {name:<22} {d.get('sharpe_solo',0):>+12.3f} {d.get('corr_to_portfolio',0):>+15.3f} "
                  f"{mcps_col}{d.get('mcps',0):>+8.3f}{RST} {d.get('sharpe_without',0):>+12.3f} "
                  f"{col}{d.get('incremental_sharpe',0):>+12.3f}{RST}  {col}{verdict}{RST}")
        print(f"\n  {DIM}MCPS > 0 → bot contribue positivement au Sharpe global.{RST}")
        print(f"  {DIM}Incrémental < 0 → retirer ce bot améliorerait le portfolio.{RST}")
        print(f"  {DIM}Règle hedge fund : bot accepté si Sharpe_bot > ρ × Sharpe_portfolio{RST}")

    # ── 11. Recommandations ───────────────────────────────────────────────────
    separator("RECOMMANDATIONS PRÉ-LIVE")
    recs = []

    # Drawdown
    if max_dd < -15:
        recs.append((R, f"MaxDD {max_dd:.1f}% — envisager un CB plus agressif ou seuil PRO plus bas"))
    elif max_dd < -8:
        recs.append((Y, f"MaxDD {max_dd:.1f}% — dans les limites backtest, surveiller"))
    else:
        recs.append((G, f"MaxDD {max_dd:.1f}% — excellent, dans les objectifs"))

    # Switchs
    if len(switches) > 0 and days > 0 and len(switches)/days > 0.3:
        recs.append((Y, f"Switchs fréquents ({len(switches)/days:.2f}/j) — augmenter hysteresis OMEGA à 7j ?"))
    elif len(switches) == 0 and days > 14:
        recs.append((Y, "Aucun switch depuis 14+ jours — vérifier que la détection régime fonctionne"))

    # CB
    if len(cb_activations) > 3:
        recs.append((Y, f"{len(cb_activations)} activations CB — seuils peut-être trop sensibles"))
    elif len(cb_activations) == 0 and days > 30:
        recs.append((G, "CB jamais déclenché sur 30+ jours — seuils cohérents"))

    # PRO
    pro_pct = engine_counts.get("PRO", 0) / total_cycles * 100
    if pro_pct > 40:
        recs.append((Y, f"PRO actif {pro_pct:.0f}% du temps — hard rules peut-être trop sensibles (VIX threshold ?)"))
    elif pro_pct < 5 and days > 30:
        recs.append((G, f"PRO actif seulement {pro_pct:.0f}% — conditions de marché favorables"))

    # Performance
    if z_perf_pct > 0:
        cagr_approx = (z_perf_pct / max(days, 1)) * 365
        recs.append((G, f"CAGR annualisé estimé : +{cagr_approx:.1f}%/an"))
    else:
        recs.append((R, f"Performance négative — analyser les engines actifs et les régimes"))

    # MTM
    if mtm_live_count < n * 0.5:
        recs.append((Y, "< 50% des cycles avec MTM live — vérifier que ohlcv est bien passé à run_bot_z_cycle()"))

    for col, msg in recs:
        print(f"  {col}{'●'}{RST} {msg}")

    # ── Export CSV ────────────────────────────────────────────────────────────
    if export_csv:
        os.makedirs(REPORT_DIR, exist_ok=True)

        # Equity + engine timeline (enrichi Meta v2+)
        eq_path = os.path.join(REPORT_DIR, "equity_timeline.csv")
        with open(eq_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "timestamp", "z_capital", "perf_pct", "regime", "engine",
                "cb_factor", "port_dd", "vix", "btc_trend", "mtm_live",
                "regime_confidence", "regime_strength", "days_in_regime",
                "vol_factor", "portfolio_vol", "avg_bot_corr", "corr_factor",
                "alloc_drift", "btc_realized_vol",
            ])
            for r in records:
                w.writerow([
                    r.get("timestamp", "")[:16],
                    r.get("z_capital_eur", r.get("total_simulated_eur", "")),
                    r.get("perf_pct", ""),
                    r.get("regime", ""),
                    r.get("current_engine", ""),
                    r.get("cb_factor", ""),
                    r.get("port_dd", ""),
                    r.get("vix", ""),
                    r.get("btc_trend", ""),
                    r.get("mtm_live", False),
                    r.get("regime_confidence", ""),
                    r.get("regime_strength", ""),
                    r.get("days_in_regime", ""),
                    r.get("vol_factor", ""),
                    r.get("portfolio_vol", ""),
                    r.get("avg_bot_corr", ""),
                    r.get("corr_factor", ""),
                    r.get("alloc_drift", ""),
                    r.get("btc_realized_vol", ""),
                ])
        print(f"\n  {G}✓ Equity timeline  → {eq_path}{RST}")

        # Switchs (enrichi)
        sw_path = os.path.join(REPORT_DIR, "engine_switches.csv")
        with open(sw_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "from_engine", "to_engine", "hard_rule",
                        "vix", "dd_pct", "regime", "regime_confidence", "btc_realized_vol"])
            for sw in switches:
                w.writerow([sw["ts"], sw["from"], sw["to"], sw["hard_rule"],
                            sw["vix"], sw["dd_pct"], sw["regime"],
                            sw.get("regime_confidence", ""), sw.get("btc_realized_vol", "")])
        print(f"  {G}✓ Engine switches  → {sw_path}{RST}")

        # Budget history
        bgt_path = os.path.join(REPORT_DIR, "budget_history.csv")
        with open(bgt_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "engine", "regime", "budget_a", "budget_b", "budget_c", "budget_g",
                        "vol_factor", "corr_factor", "alloc_drift"])
            for r in records:
                bgt = r.get("budget", {})
                w.writerow([
                    r.get("timestamp", "")[:16],
                    r.get("current_engine", ""),
                    r.get("regime", ""),
                    bgt.get("a", ""), bgt.get("b", ""), bgt.get("c", ""), bgt.get("g", ""),
                    r.get("vol_factor", ""),
                    r.get("corr_factor", ""),
                    r.get("alloc_drift", ""),
                ])
        print(f"  {G}✓ Budget history   → {bgt_path}{RST}")

        # Meta v2+ metrics timeline
        meta_path = os.path.join(REPORT_DIR, "meta_v2plus_metrics.csv")
        with open(meta_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "regime_confidence", "regime_strength", "days_in_regime",
                        "vol_factor", "portfolio_vol", "avg_bot_corr", "corr_factor",
                        "alloc_drift", "btc_realized_vol",
                        "score_a", "score_b", "score_c", "score_g",
                        "vol_a", "vol_b", "vol_c", "vol_g"])
            for r in records:
                reason = r.get("engine_reason", {})
                scores = reason.get("rolling_scores", {})
                vols   = reason.get("bot_vols", {})
                w.writerow([
                    r.get("timestamp", "")[:16],
                    r.get("regime_confidence", ""), r.get("regime_strength", ""),
                    r.get("days_in_regime", ""),
                    r.get("vol_factor", ""), r.get("portfolio_vol", ""),
                    r.get("avg_bot_corr", ""), r.get("corr_factor", ""),
                    r.get("alloc_drift", ""), r.get("btc_realized_vol", ""),
                    scores.get("a", ""), scores.get("b", ""), scores.get("c", ""), scores.get("g", ""),
                    vols.get("a", ""), vols.get("b", ""), vols.get("c", ""), vols.get("g", ""),
                ])
        print(f"  {G}✓ Meta v2+ metrics → {meta_path}{RST}")

    # ── Pied de page ──────────────────────────────────────────────────────────
    separator()
    print(f"  {DIM}Fichier source : {SHADOW_LOG} | {n} cycles | généré le {datetime.now().strftime('%Y-%m-%d %H:%M')}{RST}")
    print(f"{C}{'═'*76}{RST}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyse historique Bot Z Meta v2")
    parser.add_argument("--csv", action="store_true", help="Exporter les données en CSV")
    parser.add_argument("--min-cycles", type=int, default=3,
                        help="Nombre minimum de cycles pour afficher un rapport (défaut: 3)")
    parser.add_argument("--last", type=int, default=0,
                        help="N'analyser que les N derniers cycles (0 = tous)")
    parser.add_argument("--shadow", type=str, default=SHADOW_LOG,
                        help=f"Chemin du fichier shadow.jsonl (défaut: {SHADOW_LOG})")
    args = parser.parse_args()

    records = load_shadow(args.shadow)

    if len(records) < args.min_cycles:
        print(f"{Y}Seulement {len(records)} cycle(s) — minimum requis : {args.min_cycles}{RST}")
        print(f"Attendez que Bot Z tourne quelques cycles et relancez.")
        sys.exit(0)

    if args.last > 0:
        records = records[-args.last:]
        print(f"{DIM}Analyse limitée aux {args.last} derniers cycles{RST}")

    analyze(records, export_csv=args.csv)

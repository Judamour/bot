"""
bot_z_omega.py — Bot Z Omega allocator pour live trading.

Port direct de `backtest_bot_z_omega` (multi_backtest.py:1658).

Backtest 3y nouvel univers USD : CAGR +44.6% / Sharpe 1.55 / MaxDD -17.4%
→ bat NASDAQ-100 sur les 3 axes (vs +27.9% / 1.35 / -22.8%).

Algorithme :
  1. Expected Return Engine  : ER_score = 0.35×Sharpe_18w + 0.25×PF_18w
                                         + 0.20×slope_12w + 0.20×regime_fit
  2. Risk Engine             : risk_score = 0.4×vol_4w + 0.3×downside_vol + 0.3×current_dd
  3. Z-score normalisation cross-bot par composante
  4. Correlation Penalty     : si corr_avg > 0.5, pénalise (max -70%)
  5. Softmax weights         : final_score → poids softmax (β=3.0 = concentration)
  6. Momentum Overlay        : BTC EMA200 + QQQ SMA200 → BEAR si bearish
  7. Circuit Breaker         : DD > -25% → cb_factor *= 0.95 (min 0.30)

LIMITATION LIVE :
  Cet algo a besoin d'historique des returns + equity de chaque sub-bot.
  Stockage dans state.json sous "omega_history" (limité aux 26 dernières semaines).
  Warm-up : ~18 cycles hebdo (≈4 mois) avant de pouvoir scoresizer correctement.
  Avant warmup → equal-weight via softmax(0) = équivalent fallback Meta v2.
"""
import math
from typing import Optional

import numpy as np


# ── Paramètres Omega (cycles hebdomadaires après resample weekly) ─────────────
SHARPE_WIN   = 18    # Sharpe rolling 18 semaines (~90j)
VOL_WIN      = 4     # vol 4 semaines (~20j)
SLOPE_WIN    = 12    # slope equity 12 semaines (~60j)
CORR_WIN     = 4     # corrélation 4 semaines
SOFTMAX_BETA = 3.0   # concentration softmax (plus haut = plus concentré)
MAX_HISTORY  = 26    # garder 26 semaines max (6 mois) dans state

# Circuit breaker (identique à Meta v2)
OMEGA_CB_THRESHOLD = -0.25
OMEGA_CB_MIN_FACTOR = 0.30
OMEGA_CB_RECOVERY = 0.005

# Régime weights (BULL pour BTC bull, BEAR pour les 2 bearish, etc.)
REGIME_WEIGHTS_OMEGA = {
    "BULL":     {"a": 0.8, "b": 1.0, "c": 0.5, "g": 1.2},
    "RANGE":    {"a": 1.0, "b": 0.8, "c": 0.7, "g": 0.8},
    "BEAR":     {"a": 0.3, "b": 0.0, "c": 1.5, "g": 1.2},
    "HIGH_VOL": {"a": 0.5, "b": 0.3, "c": 1.0, "g": 0.8},
}


def _z_score(values: list) -> list:
    """Z-score normalization cross-bot pour une composante."""
    if not values:
        return []
    arr = np.array(values, dtype=float)
    m, s = float(arr.mean()), float(arr.std())
    if s < 1e-8:
        return [0.0] * len(values)
    return list((arr - m) / s)


def update_omega_history(state: dict, bot_values: dict, bot_capitals: dict) -> dict:
    """
    Ajoute un point hebdomadaire (returns + equity normalisé) à l'historique.
    À appeler 1× par semaine (ou 1× par cycle si on accepte la résolution 4h).

    state : Bot Z state.json
    bot_values : {"a": current_value, "b": ..., ...} valeur portfolio par bot
    bot_capitals : initial capital par bot (pour normaliser)
    """
    history = state.setdefault("omega_history", {})
    prev_values = history.get("last_values", {})

    # Calcul des retours par bot
    returns = {}
    eq_norm = {}
    for k, val in bot_values.items():
        cap = bot_capitals.get(k, 1.0)
        eq_norm[k] = val / cap if cap > 0 else 1.0
        prev = prev_values.get(k, val)
        returns[k] = (val / prev - 1) if prev > 0 else 0.0

    # Append + truncate
    rets_hist = history.setdefault("returns", {})
    eqs_hist = history.setdefault("equity_norm", {})
    peaks = history.setdefault("peaks", {})

    for k in bot_values:
        rets_hist.setdefault(k, []).append(returns[k])
        rets_hist[k] = rets_hist[k][-MAX_HISTORY:]
        eqs_hist.setdefault(k, []).append(eq_norm[k])
        eqs_hist[k] = eqs_hist[k][-MAX_HISTORY:]
        peaks[k] = max(peaks.get(k, eq_norm[k]), eq_norm[k])

    history["last_values"] = dict(bot_values)
    return state


def _count_recent_trades(trades: list, days: int = 30) -> int:
    """Compte les trades dont exit_date est dans les `days` derniers jours."""
    if not trades:
        return 0
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=days)
    n = 0
    for t in trades:
        ts = t.get("exit_date") or t.get("entry_date") or ""
        if not ts:
            continue
        try:
            # Format observé: "2026-05-01 13:37:30.516237" ou ISO 8601
            dt = datetime.fromisoformat(str(ts).replace(" ", "T", 1)) if "T" not in str(ts) else datetime.fromisoformat(str(ts))
            if dt >= cutoff:
                n += 1
        except Exception:
            continue
    return n


def _activity_factor(sub_state: dict) -> float:
    """Pénalise les bots qui ne déploient pas leur capital (positions=0 + trades récents=0).

    Évite le capital fantôme : un bot dormant ne doit pas conserver 14-28%
    du portfolio en cash inutilisé. Sa part se redéploie vers les bots actifs.
    Quand le bot reprend l'activité (positions ≥ 1), factor = 1.0 → réallocation
    automatique au cycle suivant.

    Mesure l'activité sur les **30 derniers jours** (lookback temporel) au lieu
    du cumul historique : un bot qui a fait 20 trades l'an dernier mais 0 ce mois-ci
    est traité comme dormant, pas comme actif.
    """
    n_positions = len(sub_state.get("positions", {}) or {})
    if n_positions >= 1:
        return 1.0

    trades = sub_state.get("trades", []) or []
    n_recent = _count_recent_trades(trades, days=30)
    if n_recent == 0:
        return 0.10
    if n_recent < 5:
        return 0.30
    if n_recent < 15:
        return 0.60
    return 1.0


def compute_omega_allocation(
    state: dict,
    valid_bots: list,
    regime: str,
    macro: dict,
    cb_factor_in: float = 1.0,
    sub_states: dict | None = None,
) -> dict:
    """
    Calcule les poids Omega à partir de l'historique stocké dans state.

    Retourne {bot_id: weight, ...} dont la somme = 1.0.

    Si historique insuffisant (warmup < SHARPE_WIN), retourne equal-weight.

    Si `sub_states` fourni, applique un activity factor multiplicatif aux weights
    pour pénaliser les bots qui ne déploient pas leur capital (évite le capital
    fantôme bloquant 14-28% du portfolio sur des bots inactifs en BULL régime).
    """
    history = state.get("omega_history", {})
    rets_hist = history.get("returns", {})
    eqs_hist = history.get("equity_norm", {})
    peaks = history.get("peaks", {})

    n_bots = len(valid_bots)
    if n_bots == 0:
        return {}

    # Si un seul bot actif, retourne 100%
    if n_bots == 1:
        return {valid_bots[0]: 1.0}

    # Vérifier warmup
    warmup = min((len(rets_hist.get(k, [])) for k in valid_bots), default=0)

    if warmup < SHARPE_WIN:
        # Equal-weight pendant warmup
        return {k: 1.0 / n_bots for k in valid_bots}

    # ── Composantes ER + Risk par bot ─────────────────────────────────────────
    er_components = {k: {} for k in valid_bots}
    risk_components = {k: {} for k in valid_bots}

    raw_regime_w = REGIME_WEIGHTS_OMEGA.get(regime, REGIME_WEIGHTS_OMEGA["RANGE"])
    total_rw = sum(raw_regime_w.values()) or 1.0

    for k in valid_bots:
        hist = rets_hist.get(k, [])
        eq_h = eqs_hist.get(k, [])

        # Sharpe 18w
        r_w = np.array(hist[-SHARPE_WIN:])
        sharpe_w = (float(r_w.mean() / r_w.std() * math.sqrt(252))
                    if r_w.std() > 1e-8 else 0.0)

        # Profit Factor 18w
        pos_sum = sum(r for r in r_w if r > 0)
        neg_sum = abs(sum(r for r in r_w if r < 0))
        pf_w = min((pos_sum / neg_sum) if neg_sum > 1e-8 else 3.0, 5.0)

        # Slope equity 12w
        if len(eq_h) >= SLOPE_WIN:
            eq_s = np.array(eq_h[-SLOPE_WIN:])
            x = np.arange(len(eq_s))
            slope = (float(np.polyfit(x, eq_s / max(eq_s[0], 1e-8), 1)[0]) * 252
                     if eq_s.std() > 1e-8 else 0.0)
        else:
            slope = 0.0

        # Regime fit
        regime_fit = raw_regime_w.get(k, 0) / (total_rw / n_bots)

        er_components[k] = {
            "sharpe": sharpe_w,
            "pf": pf_w,
            "slope": slope,
            "regime_fit": regime_fit,
        }

        # Vol 4w
        r4 = np.array(hist[-VOL_WIN:])
        vol_4 = float(r4.std() * math.sqrt(252)) if r4.std() > 1e-8 else 0.01

        # Downside vol 4w
        down_r = r4[r4 < 0]
        down_vol = (float(down_r.std() * math.sqrt(252))
                    if len(down_r) > 1 and down_r.std() > 1e-8 else vol_4)

        # Current DD
        last_eq = eq_h[-1] if eq_h else 1.0
        peak_k = peaks.get(k, last_eq)
        dd_k = abs(last_eq / peak_k - 1) if peak_k > 0 else 0.0

        risk_components[k] = {"vol": vol_4, "down_vol": down_vol, "dd": dd_k}

    # ── Z-score normalisation cross-bot ───────────────────────────────────────
    sharpe_z = dict(zip(valid_bots, _z_score([er_components[k]["sharpe"] for k in valid_bots])))
    pf_z = dict(zip(valid_bots, _z_score([er_components[k]["pf"] for k in valid_bots])))
    slope_z = dict(zip(valid_bots, _z_score([er_components[k]["slope"] for k in valid_bots])))
    regime_z = dict(zip(valid_bots, _z_score([er_components[k]["regime_fit"] for k in valid_bots])))
    vol_z = dict(zip(valid_bots, _z_score([risk_components[k]["vol"] for k in valid_bots])))
    dvol_z = dict(zip(valid_bots, _z_score([risk_components[k]["down_vol"] for k in valid_bots])))
    dd_z = dict(zip(valid_bots, _z_score([risk_components[k]["dd"] for k in valid_bots])))

    er_score = {k: 0.35 * sharpe_z[k] + 0.25 * pf_z[k] + 0.20 * slope_z[k] + 0.20 * regime_z[k]
                for k in valid_bots}
    risk_score = {k: 0.4 * vol_z[k] + 0.3 * dvol_z[k] + 0.3 * dd_z[k] for k in valid_bots}
    net_score = {k: er_score[k] - risk_score[k] for k in valid_bots}

    # ── Correlation Penalty ───────────────────────────────────────────────────
    corr_penalty = {k: 1.0 for k in valid_bots}
    if warmup >= CORR_WIN:
        try:
            rets_mat = np.array([rets_hist[k][-CORR_WIN:] for k in valid_bots])
            corr_m = np.corrcoef(rets_mat)
            n = len(valid_bots)
            for ii, k in enumerate(valid_bots):
                others = [corr_m[ii, jj] for jj in range(n) if jj != ii]
                avg_c = float(np.mean(others)) if others else 0.0
                corr_penalty[k] = max(0.3, 1.0 - max(0.0, avg_c - 0.5) / 0.5)
        except Exception:
            pass

    final_scores = {k: net_score[k] * corr_penalty[k] for k in valid_bots}

    # ── Softmax → poids ───────────────────────────────────────────────────────
    max_s = max(final_scores.values())
    exp_s = {k: math.exp(SOFTMAX_BETA * (final_scores[k] - max_s)) for k in valid_bots}
    total_e = sum(exp_s.values()) or 1.0
    weights = {k: exp_s[k] / total_e for k in valid_bots}

    # ── Activity factor : pénalise les bots qui ne déploient pas leur capital ─
    if sub_states:
        adjusted = {k: weights[k] * _activity_factor(sub_states.get(k, {})) for k in valid_bots}
        total_adj = sum(adjusted.values()) or 1.0
        weights = {k: v / total_adj for k, v in adjusted.items()}

    return weights


def update_circuit_breaker(state: dict, current_capital: float) -> float:
    """
    Update CB based on portfolio DD. Returns new cb_factor.
    À appeler à chaque cycle.
    """
    cb_peak = max(state.get("cb_peak", current_capital), current_capital)
    cb_factor = state.get("cb_factor", 1.0)
    port_dd = (current_capital - cb_peak) / cb_peak if cb_peak > 0 else 0.0

    if port_dd < OMEGA_CB_THRESHOLD:
        cb_factor = max(OMEGA_CB_MIN_FACTOR, cb_factor - 0.05)
    elif port_dd > -0.05:
        cb_factor = min(1.0, cb_factor + OMEGA_CB_RECOVERY)

    state["cb_peak"] = cb_peak
    state["cb_factor"] = cb_factor
    return cb_factor

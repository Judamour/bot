"""
live/bot_z.py — Bot Z Meta v2 — Paper Trading Production
=========================================================
Phase : PAPER TRADING (démarré 2026-03-06, revue 2026-04-30)
Capital : 10 000€ (4 bots validés × 2 500€)

Architecture Bot Z Meta v2 (validée backtest 2020-2026, Run 10) :
  Bots A, B, C, G (state files) + Macro (VIX, QQQ, BTC)
      ↓
  Regime Engine (4 régimes VIX+QQQ+BTC + Momentum Overlay)
      ↓
  Meta Engine Selector (BULL / BALANCED / PARITY / SHIELD)
      ↓
  Portfolio Engine (allocation dynamique selon engine actif)
      ↓
  Circuit Breaker (seuils adaptés par engine)
      ↓
  logs/bot_z/shadow.jsonl + logs/bot_z/state.json

Sélection d'engine (Meta v2) :
  Hard rules (non-négociables) :
    SHIELD forcé si (BTC+QQQ both bearish ET VIX>26) OU VIX>32 OU DD<-12%
    BULL bloqué si BTC ou QQQ bearish

  Scoring data-driven (si pas de hard rule) :
    score = 0.50 × regime_fit + 0.30 × rolling_quality + 0.20 × inverse_vol
    → engine avec meilleur score (hysteresis 7/5/4/3 jours)

Engines disponibles :
  BULL  : régime pur v2 (BULL max CAGR) — Sharpe 1.61, MaxDD -18.9%
  BALANCED     : ER/Risk proxy + softmax quality — base neutre + quality boost
  PARITY  : BALANCED + Risk Parity (blend inverse-vol) — Sharpe 2.03, MaxDD -7.6%
  SHIELD       : défensif pur C+G, VIX scaling — Sharpe 1.90, MaxDD -9.1%

Résultats backtest (2020-2026, 6 ans) :
  Meta v2 : CAGR +43.2% | Sharpe 1.70 | MaxDD -9.6% | 2022 +1.0%
  Distribution : BULL 17% / BALANCED 30% / PARITY 28% / SHIELD 25%
"""
import json
import os
import sys
import math
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from live.notifier import notify

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

# ── Améliorations Meta v2+ ────────────────────────────────────────────────────
SWITCH_PENALTY         = 0.05   # pénalité de score si changement d'engine (évite micro-switchs)
TARGET_PORTFOLIO_VOL   = 0.20   # vol annualisée cible portefeuille (vol targeting global)
BTC_HIGH_VOL_THRESHOLD = 0.80   # vol réalisée BTC > 80% annualisé → force HIGH_VOL
CORR_REDUCE_THRESHOLD  = 0.70   # corrélation inter-bots > 70% → réduction exposition ×0.80
REGIME_PERSIST_DAYS    = 7      # jours pour confiance pleine dans un régime (persist factor)

# ── Weight caps — garde-fous contre sur-concentration ────────────────────────
# Bot C : +0.6% CAGR réel corrigé → cap à 25% (était 64% en SHIELD, trop haut)
# Bot G : vrai stabilisateur (+23.4%) → peut monter à 55% en SHIELD
# Bot A : peut descendre à 5% en SHIELD (mais jamais 0 — moteur principal)
WEIGHT_CAPS = {
    "a": {"min": 0.05, "max": 0.50},
    "b": {"min": 0.00, "max": 0.30},
    "c": {"min": 0.00, "max": 0.25},
    "g": {"min": 0.15, "max": 0.55},
}

# ── Transition smooth entre engines ──────────────────────────────────────────
# Asymétrie : réagir vite en crise, prudemment sur les rebonds
TRANSITION_SPEED_DEFENSIVE = 0.40  # vers SHIELD/PARITY : 40%/cycle (~10h pour atteindre cible)
TRANSITION_SPEED_OFFENSIVE  = 0.20  # vers BULL/BALANCED  : 20%/cycle (~20h — évite faux rebonds)
ENGINE_DEFENSIVENESS = {"BULL": 0, "BALANCED": 1, "PARITY": 2, "SHIELD": 3}

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

# ── Engine weights — Meta v2 ─────────────────────────────────────────────────
# BULL = REGIME_WEIGHTS (ci-dessus)
SHIELD_WEIGHTS = {
    "BULL":     {"a": 0.2, "b": 0.0, "c": 1.8, "g": 1.0},
    "RANGE":    {"a": 0.3, "b": 0.0, "c": 1.8, "g": 1.0},
    "BEAR":     {"a": 0.1, "b": 0.0, "c": 2.0, "g": 1.0},
    "HIGH_VOL": {"a": 0.2, "b": 0.0, "c": 1.8, "g": 1.0},
}
BALANCED_WEIGHTS = {  # base neutre + quality scoring fait le tri
    "BULL":     {"a": 0.9, "b": 1.1, "c": 0.7, "g": 1.0},
    "RANGE":    {"a": 1.0, "b": 0.9, "c": 0.8, "g": 0.9},
    "BEAR":     {"a": 0.4, "b": 0.1, "c": 1.3, "g": 1.1},
    "HIGH_VOL": {"a": 0.6, "b": 0.4, "c": 1.2, "g": 1.0},
}

ENGINE_REGIME_FIT = {
    "BULL": {"BULL": 1.0, "RANGE": 0.6, "HIGH_VOL": 0.3, "BEAR": 0.1},
    "BALANCED":    {"BULL": 0.8, "RANGE": 0.8, "HIGH_VOL": 0.7, "BEAR": 0.5},
    "PARITY": {"BULL": 0.5, "RANGE": 0.7, "HIGH_VOL": 0.9, "BEAR": 0.8},
    "SHIELD":      {"BULL": 0.3, "RANGE": 0.5, "HIGH_VOL": 0.8, "BEAR": 1.0},
}
META_ENGINE_HYSTERESIS = {"BULL": 7, "BALANCED": 5, "PARITY": 4, "SHIELD": 3}


# ── Sélecteur d'engine Meta v2 ────────────────────────────────────────────────

def select_engine_live(vix: float, btc_bearish: bool, qqq_bearish: bool,
                       port_dd: float, regime: str,
                       rolling_scores: dict, bot_vols: dict,
                       current_engine: str = "BALANCED",
                       regime_confidence: float = 1.0,
                       regime_strength: float = 1.0) -> str:
    """
    Sélectionne l'engine optimal pour ce cycle (production, sans shadow tracking).

    Hard rules (non-négociables) :
      SHIELD forcé si (BTC+QQQ both bearish ET VIX>26) OU VIX>32 OU DD<-12%
      BULL bloqué si BTC ou QQQ bearish

    Scoring (si pas de hard rule) :
      rf    = regime_fit × regime_confidence × regime_strength
      score = 0.50 × rf + 0.30 × rolling_quality_norm + 0.20 × inverse_vol_norm
              - SWITCH_PENALTY si engine différent du current (évite micro-switchs)
    """
    # Hard rules
    force_pro = ((btc_bearish and qqq_bearish and vix > 26) or vix > 32 or port_dd < -0.12)
    if force_pro:
        return "SHIELD"
    block_enhanced = btc_bearish or qqq_bearish

    # Normalisation des proxies de qualité et risque
    max_quality = max(rolling_scores.values()) if rolling_scores else 1.0
    avg_quality = (sum(rolling_scores.values()) / len(rolling_scores)) if rolling_scores else 1.0
    quality_norm = avg_quality / max_quality if max_quality > 0 else 0.5

    max_vol = max(bot_vols.values()) if bot_vols else TARGET_VOL
    avg_vol = (sum(bot_vols.values()) / len(bot_vols)) if bot_vols else TARGET_VOL

    best_engine = None
    best_score  = -1.0
    candidates  = ["BULL", "BALANCED", "PARITY", "SHIELD"]

    for eng in candidates:
        if eng == "BULL" and block_enhanced:
            continue

        # regime_fit pondéré par confiance × persistance du régime
        rf = ENGINE_REGIME_FIT[eng].get(regime, 0.5) * regime_confidence * regime_strength

        # Inverse vol : chaque engine a un profil différent
        if eng == "SHIELD":
            # SHIELD favorisé quand vol haute
            inv_risk_norm = min(1.0, avg_vol / max(TARGET_VOL, 0.01))
        elif eng == "PARITY":
            # PARITY favorisé en stress modéré
            stress = max(0.0, (vix - 20) / 20.0)
            inv_risk_norm = min(1.0, 0.3 + stress * 0.7)
        else:
            # BULL/BALANCED : favorisés quand vol basse
            inv_risk_norm = max(0.0, 1.0 - avg_vol / max(max_vol, 0.01))

        score = 0.50 * rf + 0.30 * quality_norm + 0.20 * inv_risk_norm

        # Pénalité de switch : évite les micro-switchs sur signaux marginaux
        if eng != current_engine:
            score -= SWITCH_PENALTY

        if score > best_score:
            best_score  = score
            best_engine = eng

    return best_engine or "BALANCED"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "initial_capital": INITIAL_CAP,
        "z_capital": INITIAL_CAP,
        "paper_start_date": PAPER_START_DATE,
        "paper_review_date": PAPER_REVIEW_DATE,
        "cb_peak": INITIAL_CAP,
        "cb_factor": 1.0,
        "current_engine": "BALANCED",
        "pending_engine": "BALANCED",
        "days_pending": 0,
        "last_bot_values": {},
        "last_alloc_weights": {},
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


def _write_budget(budget: dict):
    """Écrit le budget alloué par Bot Z pour chaque sub-bot (logs/bot_z/budget.json)."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    budget_path = os.path.join(os.path.dirname(STATE_FILE), "budget.json")
    with open(budget_path, "w") as f:
        json.dump({"ts": datetime.now().isoformat(), "budget": budget}, f, indent=2)


def _apply_weight_caps(weights: dict) -> dict:
    """
    Applique les caps min/max avec algorithme itératif jusqu'à convergence.
    Nécessaire car clipper un bot redistribue son poids sur les autres,
    qui peuvent alors dépasser leur propre cap — d'où les itérations.
    Converge en 3-5 itérations pour 4 bots.
    """
    w = {b: weights.get(b, 0.0) for b in VALID_BOTS}
    for _ in range(15):
        total = sum(w.values()) or 1.0
        w = {b: v / total for b, v in w.items()}
        changed = False
        for b in VALID_BOTS:
            cap = WEIGHT_CAPS.get(b, {})
            lo, hi = cap.get("min", 0.0), cap.get("max", 1.0)
            new_w = max(lo, min(hi, w[b]))
            if abs(new_w - w[b]) > 1e-6:
                changed = True
            w[b] = new_w
        if not changed:
            break
    total = sum(w.values()) or 1.0
    return {b: v / total for b, v in w.items()}


def _smooth_weights(target: dict, prev: dict, current_engine: str, prev_engine: str) -> dict:
    """
    Interpolation progressive des poids vers la cible selon la direction du switch.
    Asymétrie : rapide vers la défense (crise), lent vers l'offensive (évite faux rebonds).

    Exemple BALANCED→SHIELD (speed=0.40) :
      cycle+1 : poids intermédiaire à 40% de l'écart comblé
      cycle+4 : ~85% de la cible atteinte
    """
    def_curr = ENGINE_DEFENSIVENESS.get(current_engine, 1)
    def_prev = ENGINE_DEFENSIVENESS.get(prev_engine, 1)
    going_defensive = def_curr > def_prev
    speed = TRANSITION_SPEED_DEFENSIVE if going_defensive else TRANSITION_SPEED_OFFENSIVE

    smoothed = {}
    for b in VALID_BOTS:
        t = target.get(b, 1.0 / len(VALID_BOTS))
        p = prev.get(b, 1.0 / len(VALID_BOTS))
        smoothed[b] = p + speed * (t - p)

    total = sum(smoothed.values()) or 1.0
    return {b: v / total for b, v in smoothed.items()}


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

    # BTC realized vol override : capte les crises crypto que VIX détecte avec retard
    btc_vol = macro.get("btc_realized_vol", 0.0)
    if btc_vol > BTC_HIGH_VOL_THRESHOLD and regime in ("BULL", "RANGE"):
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

    Ramp-up progressif (évite de sur-pondérer un score instable) :
      < 5 trades  → neutre 1.0
      5-20 trades → blend progressif entre neutre et score réel
      >= 20 trades → score plein
    """
    trades = state.get("trades", [])[-window:]
    n = len(trades)
    if n < 5:
        return 1.0  # neutre — pas assez de data
    pnls = [t.get("pnl", 0) for t in trades]
    avg = sum(pnls) / n
    std = math.sqrt(sum((p - avg) ** 2 for p in pnls) / n) if n > 1 else 1.0
    sharpe = avg / std if std > 0 else 0.0
    raw = max(0.3, min(1.5, 1.0 + sharpe * 0.3))
    # Blend progressif : confiance croît de 0 à 1 entre 5 et 20 trades
    confidence = min(1.0, (n - 5) / 15.0)
    return 1.0 + confidence * (raw - 1.0)


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


# ── Helpers Meta v2+ ─────────────────────────────────────────────────────────

def compute_portfolio_vol(z_capital_history: list) -> float:
    """
    Volatilité annualisée du portefeuille Z sur les 20 derniers cycles (4h).
    Retourne TARGET_PORTFOLIO_VOL si pas assez de données.
    """
    if len(z_capital_history) < 5:
        return TARGET_PORTFOLIO_VOL
    vals = z_capital_history[-21:]
    returns = [(vals[i] / vals[i - 1] - 1) for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(returns) < 2:
        return TARGET_PORTFOLIO_VOL
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / len(returns)
    if variance < 1e-10:
        return TARGET_PORTFOLIO_VOL  # pas de vol mesurable → utilise la cible (vol_factor=1.0)
    std = math.sqrt(variance)
    # 6 cycles/jour × 365 jours = 2190 cycles/an
    annual_vol = std * math.sqrt(2190)
    return max(0.05, min(2.0, annual_vol))


def compute_bot_correlation(all_states: dict) -> float:
    """
    Corrélation moyenne (pairwise) entre les bots sur les 20 derniers trades.
    Retourne 0.0 si pas assez de données.
    """
    window = 20
    bot_returns = {}
    for bot_id, state in all_states.items():
        trades = state.get("trades", [])[-window:]
        if len(trades) < 5:
            continue
        capital = state.get("capital", 1000.0)
        if capital <= 0:
            continue
        bot_returns[bot_id] = [t.get("pnl", 0) / capital for t in trades]

    bots = list(bot_returns.keys())
    if len(bots) < 2:
        return 0.0

    min_len = min(len(bot_returns[b]) for b in bots)
    if min_len < 3:
        return 0.0

    series = {b: bot_returns[b][-min_len:] for b in bots}

    def pearson(xs, ys):
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if dx == 0 or dy == 0:
            return 0.0
        return num / (dx * dy)

    pairs = [(bots[i], bots[j]) for i in range(len(bots)) for j in range(i + 1, len(bots))]
    if not pairs:
        return 0.0
    corrs = [abs(pearson(series[a], series[b])) for a, b in pairs]
    return sum(corrs) / len(corrs)


def compute_btc_realized_vol(ohlcv: dict) -> float:
    """
    Volatilité réalisée BTC sur 20 bougies (annualisée, timeframe 4h).
    Retourne 0.0 si pas de données disponibles.
    """
    btc_df = None
    if ohlcv:
        btc_df = ohlcv.get("BTC/EUR")
        if btc_df is None:
            btc_df = ohlcv.get("BTC/USDT")
    if btc_df is None or btc_df.empty:
        return 0.0
    closes = btc_df["close"].dropna()
    if len(closes) < 5:
        return 0.0
    returns = closes.pct_change().dropna().iloc[-20:]
    if len(returns) < 3:
        return 0.0
    std = float(returns.std())
    # 6 candles/jour × 365 jours
    annual_vol = std * math.sqrt(6 * 365)
    return max(0.0, min(5.0, annual_vol))


def compute_allocation_drift(target_weights: dict, all_states: dict) -> float:
    """
    Mesure l'écart entre allocation cible (Bot Z) et allocation réelle (sub-bots).
    drift = sum(|target_weight[b] - actual_weight[b]|) pour b in VALID_BOTS
    Valide que backtest ≈ paper live. Drift > 0.20 = warning.
    """
    actual_values = {}
    total_actual = 0.0
    for bot_id, state in all_states.items():
        val = state.get("capital", 1000.0)
        for pos in state.get("positions", {}).values():
            val += pos.get("entry", 0) * pos.get("size", 0)
        actual_values[bot_id] = val
        total_actual += val

    if total_actual <= 0:
        return 0.0

    drift = 0.0
    for bot_id in VALID_BOTS:
        target_w = target_weights.get(bot_id, 0.0)
        actual_w = actual_values.get(bot_id, 0.0) / total_actual
        drift += abs(target_w - actual_w)
    return round(drift, 4)


# ── Allocation principale ────────────────────────────────────────────────────

def compute_shadow_allocation(regime: str, all_states: dict, macro: dict,
                              cb_factor: float = 1.0,
                              engine: str = "BULL") -> dict:
    """
    Calcule l'allocation Bot Z Meta v2 pour chaque bot.

    Structure : engine sélectionné + circuit breaker
      - Poids par engine/régime (BULL, BALANCED, PARITY, SHIELD)
      - PARITY : blend 50% BALANCED + 50% inverse-vol (risk parity)
      - Modulation par qualité récente (rolling score, sauf SHIELD)
      - Exposition finale × cb_factor (circuit breaker)
      - Cap par bot 40%

    Retourne {bot_id: {budget_eur, budget_pct, weight_*, cb_factor, open_positions}}
    """
    vix      = macro.get("vix", 15.0)

    # Choisir le tableau de poids selon l'engine
    if engine == "SHIELD":
        base_w = SHIELD_WEIGHTS.get(regime, SHIELD_WEIGHTS["RANGE"])
    elif engine in ("BALANCED", "PARITY"):
        base_w = BALANCED_WEIGHTS.get(regime, BALANCED_WEIGHTS["RANGE"])
    else:  # BULL (défaut)
        base_w = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["RANGE"])

    regime_w = base_w
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
        # SHIELD : pas de boost qualité (défensif pur, C+G fixes)
        if engine == "SHIELD":
            raw_weights[bot_id] = rw
        else:
            raw_weights[bot_id] = rw * quality

    # PARITY : blend 50% BALANCED weights + 50% inverse-vol (risk parity)
    if engine == "PARITY":
        bot_vols = {b: compute_bot_volatility(s) for b, s in all_states.items()}
        max_vol  = max(bot_vols.values()) if bot_vols else TARGET_VOL
        inv_vols = {b: (1.0 / max(v, 0.01)) for b, v in bot_vols.items()}
        total_iv = sum(inv_vols.values()) or 1.0
        rp_weights = {b: inv_vols[b] / total_iv for b in inv_vols}

        total_omega = sum(raw_weights.values()) or 1.0
        omega_norm  = {b: raw_weights[b] / total_omega for b in raw_weights}

        # Blend 50/50
        blended = {b: 0.5 * omega_norm.get(b, 0) + 0.5 * rp_weights.get(b, 0)
                   for b in raw_weights}
        raw_weights = blended

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
            "weight_regime": round(base_w.get(bot_id, 0.0), 2),
            "weight_quality": round(quality_w, 2),
            "weight_final":  round(w, 3),
            "cb_factor":     round(cb_factor, 2),
            "bot_name":      BOT_NAMES.get(bot_id, bot_id),
            "open_positions": list(positions.keys()),
            "engine":        engine,
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


# ── Notifications positions ──────────────────────────────────────────────────

def _notify_position_changes(all_states: dict, prev_positions: dict, engine: str, regime: str):
    """
    Détecte les ouvertures/fermetures de positions dans tous les bots Z (A/B/C/G)
    et envoie une notification Telegram pour chaque changement.
    Utilisé uniquement pour les bots sans notification native (B, C, G).
    Bot A gère ses propres notifications dans live/bot.py.
    """
    for bot_id in VALID_BOTS:
        if bot_id == "a":
            continue  # Bot A notifie lui-même
        state = all_states.get(bot_id, {})
        bot_name = BOT_NAMES.get(bot_id, bot_id.upper())

        current_syms = set(state.get("positions", {}).keys())
        prev_syms = set(prev_positions.get(bot_id, []))

        # Positions ouvertes ce cycle
        for sym in current_syms - prev_syms:
            pos = state["positions"][sym]
            entry = pos.get("entry", 0)
            size = pos.get("size", 0)
            cost = pos.get("cost", entry * size)
            stop = pos.get("stop", 0)
            stop_str = f" | Stop : {stop:.4f}€" if stop else ""
            notify(
                f"📈 <b>Bot {bot_id.upper()} — {bot_name}</b>\n"
                f"▲ <b>{sym}</b> BUY\n"
                f"Prix : {entry:.4f}€ | Investi : {cost:.2f}€{stop_str}\n"
                f"Engine Z : {engine} | Régime : {regime}"
            )

        # Positions fermées ce cycle
        for sym in prev_syms - current_syms:
            trades = state.get("trades", [])
            last_trade = next(
                (t for t in reversed(trades) if t.get("symbol") == sym), None
            )
            if last_trade:
                pnl = last_trade.get("pnl", 0)
                entry_p = last_trade.get("entry_price", 0)
                exit_p = last_trade.get("exit_price", 0)
                reason = last_trade.get("reason", "exit")
                notify(
                    f"{'✅' if pnl > 0 else '🔴'} <b>Bot {bot_id.upper()} — {bot_name}</b>\n"
                    f"{'✓' if pnl > 0 else '✗'} <b>{sym}</b> {reason.upper()}\n"
                    f"{entry_p:.4f}€ → {exit_p:.4f}€\n"
                    f"PnL : <b>{pnl:+.2f}€</b> | Engine Z : {engine}"
                )
            else:
                notify(
                    f"⏹ <b>Bot {bot_id.upper()} — {bot_name}</b>\n"
                    f"<b>{sym}</b> fermé\n"
                    f"Engine Z : {engine} | Régime : {regime}"
                )


# ── Cycle principal ──────────────────────────────────────────────────────────

def run_bot_z_cycle(macro: dict, ohlcv: dict = None) -> dict:
    """
    Exécute un cycle Bot Z Meta v2 (paper trading production).

    Args:
        macro  : données macro (vix, qqq_regime_ok, btc_context, ...)
        ohlcv  : dict {symbol: DataFrame} pour mark-to-market réel des positions.
                 Si None, fallback au prix d'entrée (conservatif mais inexact).

    Retourne le résumé du cycle pour logging et dashboard.
    """
    ts = datetime.now().isoformat()

    # 1. Charger les états de tous les bots valides
    all_states = {bot_id: load_bot_state(bot_id) for bot_id in BOT_STATE_FILES}

    # 2. BTC realized vol → injectée dans macro avant détection de régime
    btc_vol = compute_btc_realized_vol(ohlcv) if ohlcv else 0.0
    macro = dict(macro)  # copie pour ne pas muter l'argument
    macro["btc_realized_vol"] = btc_vol

    # Détecter le régime + Momentum Overlay
    regime_info = detect_regime_score(macro)
    regime = regime_info["regime"]
    regime_confidence = regime_info["confidence"]

    # 3. Charger state + regime persistence tracking
    state = load_state()

    # Snapshot des positions du cycle précédent (pour détection des changements)
    prev_positions = state.get("last_positions", {b: [] for b in VALID_BOTS})

    # Persistance du régime : confiance pleine après REGIME_PERSIST_DAYS jours
    last_regime_for_persist = state.get("last_regime", regime)
    days_in_regime = state.get("days_in_regime", 0)
    if regime == last_regime_for_persist:
        days_in_regime += 1
    else:
        days_in_regime = 0  # reset si changement de régime
    regime_strength = min(1.0, days_in_regime / max(REGIME_PERSIST_DAYS, 1))

    # Valeur actuelle de chaque sub-bot (capital libre + positions mark-to-market)
    # Si ohlcv disponible → prix live ; sinon → prix d'entrée (approximation)
    bot_values = {}
    mtm_prices = {}  # prix utilisés pour le mark-to-market (pour logging)
    for bot_id, s in all_states.items():
        val = s.get("capital", 1000.0)
        for sym, p in s.get("positions", {}).items():
            entry_price = p.get("entry", 0)
            # Mark-to-market : utilise le dernier close OHLCV si disponible
            if ohlcv and sym in ohlcv:
                try:
                    df = ohlcv[sym]
                    if df is not None and not df.empty:
                        live_price = float(df["close"].iloc[-1])
                        mtm_prices[sym] = round(live_price, 4)
                        val += live_price * p.get("size", 0)
                        continue
                except Exception:
                    pass
            # Fallback : prix d'entrée
            mtm_prices.setdefault(sym, entry_price)
            val += entry_price * p.get("size", 0)
        bot_values[bot_id] = round(val, 2)

    # Retour pondéré du cycle : retours de chaque bot × poids du cycle précédent
    z_capital       = state.get("z_capital", INITIAL_CAP)
    prev_bot_values = state.get("last_bot_values", {b: 1000.0 for b in VALID_BOTS})
    prev_weights    = state.get("last_alloc_weights", {b: 1.0 / len(VALID_BOTS) for b in VALID_BOTS})

    cycle_returns = {
        b: (bot_values[b] / prev_bot_values[b] - 1) if prev_bot_values.get(b, 0) > 0 else 0.0
        for b in VALID_BOTS
    }
    weighted_return = sum(prev_weights.get(b, 0) * cycle_returns.get(b, 0) for b in VALID_BOTS)

    # Sanity cap : un retour > 15% sur un cycle 4h signale un saut de données
    # (ex : Bot Z crashé → sub-bots ont accumulé des gains → faux "mega-cycle")
    # On recale z_capital sur la somme réelle des bots dans ce cas
    MAX_CYCLE_RETURN = 0.15
    if abs(weighted_return) > MAX_CYCLE_RETURN:
        sum_prev = sum(prev_bot_values.get(b, 0) for b in VALID_BOTS)
        sum_curr = sum(bot_values.get(b, 0) for b in VALID_BOTS)
        if sum_prev > 0 and sum_curr > 0:
            # Recalage : z_capital × ratio réel des portefeuilles
            corrected_capital = z_capital * (sum_curr / sum_prev)
            print(
                f"[BOT Z] SANITY: weighted_return={weighted_return:.1%} > {MAX_CYCLE_RETURN:.0%} "
                f"(données sautées — bot Z crashé ?) → recalage z_capital "
                f"{z_capital:.0f}€ → {corrected_capital:.0f}€ (ratio bots {sum_curr:.0f}/{sum_prev:.0f})"
            )
            weighted_return = sum_curr / sum_prev - 1  # retour réel sur la période manquée

    new_z_capital   = max(0.0, z_capital * (1 + weighted_return))

    cb_peak   = max(state.get("cb_peak", INITIAL_CAP), new_z_capital)
    cb_factor = state.get("cb_factor", 1.0)
    port_dd   = (new_z_capital - cb_peak) / cb_peak if cb_peak > 0 else 0.0

    if port_dd < CB_THRESHOLD:
        cb_factor = max(CB_MIN_FACTOR, cb_factor - 0.05)
    elif port_dd > -0.10:
        cb_factor = min(1.0, cb_factor + CB_RECOVERY)

    # 4. Meta v2 — Sélection d'engine avec hysteresis
    vix = macro.get("vix", 15.0)
    btc_trend   = macro.get("btc_context", {}).get("btc_trend", "bull")
    btc_bearish = btc_trend in ("bear", "strong_bear")
    qqq_bearish = not macro.get("qqq_regime_ok", True)

    rolling_scores = {b: compute_rolling_score(b, s) for b, s in all_states.items()}
    bot_vols       = {b: compute_bot_volatility(s)   for b, s in all_states.items()}

    # Hysteresis : on attend N jours de confirmation avant de switcher
    current_engine = state.get("current_engine", "BALANCED")
    pending_engine = state.get("pending_engine", "BALANCED")
    days_pending   = state.get("days_pending", 0)

    # Sélection engine avec switch penalty + confidence × persistence
    raw_engine = select_engine_live(
        vix, btc_bearish, qqq_bearish, port_dd, regime,
        rolling_scores, bot_vols,
        current_engine=current_engine,
        regime_confidence=regime_confidence,
        regime_strength=regime_strength,
    )

    prev_engine = current_engine  # pour détecter les switchs
    if raw_engine != pending_engine:
        pending_engine = raw_engine
        days_pending   = 0
    else:
        # Plafonner au seuil d'hystérésis pour éviter compteur infini
        hyst_max = META_ENGINE_HYSTERESIS.get(pending_engine, 5)
        days_pending = min(days_pending + 1, hyst_max)

    engine_switched = False
    if days_pending >= META_ENGINE_HYSTERESIS.get(pending_engine, 5):
        engine_switched = (current_engine != pending_engine)
        if engine_switched:
            _engine_emojis = {"BULL": "🟢", "BALANCED": "🔵", "PARITY": "🟡", "SHIELD": "🔴"}
            notify(
                f"🔄 <b>Bot Z — Changement d'engine</b>\n"
                f"{_engine_emojis.get(prev_engine, '⚪')} {prev_engine} → "
                f"{_engine_emojis.get(pending_engine, '⚪')} <b>{pending_engine}</b>\n"
                f"Régime : {regime} | VIX : {vix:.1f}\n"
                f"Capital Z : {new_z_capital:.2f}€ ({(new_z_capital/INITIAL_CAP-1)*100:+.1f}%)"
            )
        current_engine = pending_engine

    # Raisons de sélection d'engine (pour historique et debug)
    _hard_pro = ((btc_bearish and qqq_bearish and vix > 26) or vix > 32 or port_dd < -0.12)
    engine_reason = {
        "hard_rule_pro":      _hard_pro,
        "block_enhanced":     btc_bearish or qqq_bearish,
        "btc_bearish":        btc_bearish,
        "qqq_bearish":        qqq_bearish,
        "vix":                round(vix, 1),
        "port_dd_pct":        round(port_dd * 100, 2),
        "regime":             regime,
        "regime_confidence":  round(regime_confidence, 2),
        "regime_strength":    round(regime_strength, 2),
        "btc_realized_vol":   round(btc_vol, 3),
        "raw_engine":         raw_engine,
        "rolling_scores":     {b: round(v, 3) for b, v in rolling_scores.items()},
        "bot_vols":           {b: round(v, 3) for b, v in bot_vols.items()},
        "engine_switched":    engine_switched,
        "prev_engine":        prev_engine,
    }

    # CB seuils adaptés par engine (SHIELD plus sensible)
    cb_tiers = {
        "BULL": [(-0.25, CB_MIN_FACTOR)],
        "BALANCED":    [(-0.25, CB_MIN_FACTOR)],
        "PARITY": [(-0.20, 0.50), (-0.30, CB_MIN_FACTOR)],
        "SHIELD":      [(-0.10, 0.80), (-0.20, 0.50), (-0.30, CB_MIN_FACTOR)],
    }
    tiers = cb_tiers.get(current_engine, [(-0.25, CB_MIN_FACTOR)])
    target_factor = 1.0
    for threshold, floor in sorted(tiers, key=lambda x: x[0]):
        if port_dd < threshold:
            target_factor = floor
    if target_factor < cb_factor:
        cb_factor = max(target_factor, cb_factor - 0.05)

    # Vol targeting global : ajuste l'exposition si le portefeuille est trop volatil
    z_capital_history = state.get("z_capital_history", [])
    z_capital_history.append(new_z_capital)
    z_capital_history = z_capital_history[-25:]  # garde 25 valeurs (20 retours + marge)
    portfolio_vol = compute_portfolio_vol(z_capital_history)
    vol_factor = max(0.3, min(1.5, TARGET_PORTFOLIO_VOL / max(portfolio_vol, 0.01)))
    # Ramp-up protection : pas de levier les 20 premiers cycles (portfolio_vol peu fiable)
    if len(z_capital_history) < 20:
        vol_factor = min(vol_factor, 1.0)
    cb_factor_vol = round(cb_factor * vol_factor, 3)

    # Corrélation inter-bots : si trop corrélés → réduit l'exposition de 20%
    avg_corr = compute_bot_correlation(all_states)
    corr_factor = 0.80 if avg_corr > CORR_REDUCE_THRESHOLD else 1.0
    cb_factor_final = max(CB_MIN_FACTOR, cb_factor_vol * corr_factor)

    # 5. Allocation Meta v2 (engine sélectionné + CB final)
    allocation = compute_shadow_allocation(regime, all_states, macro, cb_factor_final, current_engine)

    # 6. Budget dispatch — risk parity blend + écriture logs/bot_z/budget.json
    alloc_weights = {b: allocation[b]["weight_final"] for b in VALID_BOTS if b in allocation}

    # Risk parity : 60% poids engine/régime + 40% inverse-vol (stabilise la contribution au risque)
    valid_vols = {b: max(bot_vols.get(b, TARGET_PORTFOLIO_VOL), 0.05) for b in VALID_BOTS}
    inv_vol_raw = {b: 1.0 / valid_vols[b] for b in VALID_BOTS}
    total_inv = sum(inv_vol_raw.values())
    inv_vol_w = {b: inv_vol_raw[b] / total_inv for b in VALID_BOTS}
    blended = {b: 0.60 * alloc_weights.get(b, 0.0) + 0.40 * inv_vol_w.get(b, 0.0) for b in VALID_BOTS}
    total_b = sum(blended.values())
    if total_b > 0:
        blended = {b: v / total_b for b, v in blended.items()}

    # 6a. Weight caps — évite sur-concentration (ex: Bot C à 64% en SHIELD)
    blended = _apply_weight_caps(blended)
    # Sauvegarder la cible APRÈS caps (avant smooth) → dashboard transition exacte
    state["target_capped_weights"] = dict(blended)

    # 6b. Transition smooth — interpolation progressive vers la cible
    #     Asymétrie : rapide en crise (SHIELD ×0.40/cycle), lent sur rebond (BULL ×0.20/cycle)
    prev_blended = state.get("last_blended_weights", {b: 1.0 / len(VALID_BOTS) for b in VALID_BOTS})
    prev_engine_smooth = state.get("prev_engine_smooth", current_engine)
    blended = _smooth_weights(blended, prev_blended, current_engine, prev_engine_smooth)
    state["last_blended_weights"] = blended
    state["prev_engine_smooth"] = current_engine

    budget = {b: round(new_z_capital * cb_factor_final * blended.get(b, 0.0), 2) for b in VALID_BOTS}
    _write_budget(budget)

    # 7. Analyse exposition croisée + drift d'allocation
    cross = analyze_cross_exposure(all_states, allocation)
    alloc_drift = compute_allocation_drift(alloc_weights, all_states)

    # 8. Warnings
    warnings_list = []
    if cb_factor_final < 1.0:
        warnings_list.append(
            f"CIRCUIT BREAKER actif — DD={port_dd*100:.1f}% | expo={cb_factor_final*100:.0f}%"
        )
    elif vol_factor > 1.05:
        n_hist = len(z_capital_history)
        warnings_list.append(
            f"Vol targeting — levier ×{vol_factor:.2f} (port_vol={portfolio_vol:.0%} < cible 20% | {n_hist} cycles)"
        )
    # N'alerter que si un changement est en attente (pending ≠ actif)
    if pending_engine != current_engine:
        warnings_list.append(f"Changement pending : {current_engine} → {pending_engine} (j={days_pending}/{META_ENGINE_HYSTERESIS.get(pending_engine,5)})")
    if vix > CASH_VIX_THRESHOLD:
        warnings_list.append(f"HIGH_VOL forcé (VIX={vix:.1f} > {CASH_VIX_THRESHOLD})")
    if btc_vol > BTC_HIGH_VOL_THRESHOLD:
        warnings_list.append(f"BTC realized vol élevée ({btc_vol:.0%}) → HIGH_VOL override")
    if avg_corr > CORR_REDUCE_THRESHOLD:
        warnings_list.append(f"Corrélation inter-bots élevée ({avg_corr:.0%}) → expo ×0.80")
    if alloc_drift > 0.20:
        warnings_list.append(f"Drift allocation élevé ({alloc_drift:.0%}) — backtest ≠ paper")
    for sym, info in cross.items():
        if info["warning"]:
            prio = info["priority_bot"]
            prio_name = BOT_NAMES.get(prio, prio) if prio else "?"
            warnings_list.append(
                f"{sym}: {info['n_bots']} bots ({info['estimated_exposure_pct']:.0f}%) "
                f"— priorité {prio_name}"
            )

    # 9. Métriques de performance
    perf_pct = (new_z_capital - INITIAL_CAP) / INITIAL_CAP * 100
    days_running = (datetime.now() - datetime.fromisoformat(PAPER_START_DATE)).days

    # 10. Construction du résumé
    summary = {
        "timestamp":          ts,
        "regime":             regime,
        "regime_confidence":  regime_confidence,
        "regime_strength":    round(regime_strength, 2),
        "days_in_regime":     days_in_regime,
        "vix":                vix,
        "qqq_ok":             macro.get("qqq_regime_ok", True),
        "btc_trend":          macro.get("btc_context", {}).get("btc_trend", "?"),
        "btc_realized_vol":   round(btc_vol, 3),
        "allocation":         allocation,
        "cross_exposure":     cross,
        "alloc_drift":        alloc_drift,
        "warnings":           warnings_list,
        "bot_values":         bot_values,
        "total_simulated_eur": round(new_z_capital, 2),
        "z_capital_eur":      round(new_z_capital, 2),
        "initial_capital":    INITIAL_CAP,
        "perf_pct":           round(perf_pct, 2),
        "cb_factor":          round(cb_factor_final, 2),
        "cb_active":          cb_factor_final < 1.0,
        "cb_factor_raw":      round(cb_factor, 2),
        "vol_factor":         round(vol_factor, 3),
        "portfolio_vol":      round(portfolio_vol, 3),
        "corr_factor":        corr_factor,
        "avg_bot_corr":       round(avg_corr, 3),
        "port_dd":            round(port_dd * 100, 2),
        "days_running":       days_running,
        "paper_start":        PAPER_START_DATE,
        "paper_review":       PAPER_REVIEW_DATE,
        "current_engine":     current_engine,
        "pending_engine":     pending_engine,
        "days_pending":       days_pending,
        "budget":             budget,
        "engine_reason":      engine_reason,
        "mtm_prices":         mtm_prices,
        "mtm_live":           (ohlcv is not None),
    }

    # 11. Notifications positions (B, C, G — Bot A gère les siennes)
    _notify_position_changes(all_states, prev_positions, current_engine, regime)

    # 12. Log shadow
    _log_shadow(summary)

    # 12. Mise à jour state
    state["initial_capital"]     = INITIAL_CAP  # toujours synchronisé avec la constante
    state["cb_peak"]             = round(cb_peak, 2)
    state["cb_factor"]           = round(cb_factor, 3)
    state["z_capital"]           = round(new_z_capital, 2)
    state["z_capital_history"]   = z_capital_history
    # Référence pour le prochain cycle = budget dispatché (pas les valeurs pre-dispatch).
    # Sinon : pre-dispatch=4000€ → post-dispatch=10000€ → faux retour +150% au cycle suivant.
    state["last_bot_values"]     = {b: round(budget.get(b, bot_values.get(b, 0)), 2) for b in VALID_BOTS}
    state["last_bot_raw_values"] = bot_values  # valeurs réelles pour debug/MTM
    state["last_alloc_weights"]  = blended  # poids réels du budget (blended = 60% engine + 40% inv-vol + caps + smooth)
    state["current_engine"]      = current_engine
    state["pending_engine"]      = pending_engine
    state["days_pending"]        = days_pending
    state["days_in_regime"]      = days_in_regime
    state["last_portfolio_vol"]  = round(portfolio_vol, 4)
    state["last_vol_factor"]     = round(vol_factor, 4)
    state["last_avg_corr"]       = round(avg_corr, 4)
    state["last_alloc_drift"]    = alloc_drift
    state["regime_history"].append({
        "ts": ts, "regime": regime, "engine": current_engine,
        "confidence": regime_confidence, "strength": round(regime_strength, 2),
    })
    state["regime_history"] = state["regime_history"][-500:]
    state["allocation_history"].append({"ts": ts, "allocation": {k: v["budget_eur"] for k, v in allocation.items()}})
    state["allocation_history"]  = state["allocation_history"][-500:]
    state["last_positions"]      = {b: list(all_states.get(b, {}).get("positions", {}).keys()) for b in VALID_BOTS}
    state["last_regime"]         = regime
    state["last_regime_info"]    = regime_info
    state["last_allocation"]     = allocation
    state["last_cross_exposure"] = cross
    state["last_warnings"]       = warnings_list
    state["total_simulated_eur"] = round(new_z_capital, 2)
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
    engine    = summary.get("current_engine", "BULL")
    eng_colors = {"BULL": Fore.GREEN, "BALANCED": Fore.CYAN,
                  "PARITY": Fore.YELLOW, "SHIELD": Fore.RED}
    ec        = eng_colors.get(engine, Fore.WHITE)

    print(f"\n{Fore.CYAN}{'─'*78}")
    print(f"  BOT Z META v2 — PAPER TRADING | {ts}")
    print(f"  Jour {days} / revue {review} | Capital initial : {INITIAL_CAP:.0f}€")
    print(f"  Régime : {c}{r}{Style.RESET_ALL} (conf={conf:.0%}) | VIX={summary['vix']:.1f} | "
          f"QQQ={'✓' if summary['qqq_ok'] else '✗'} | BTC={summary['btc_trend']}")
    print(f"  Engine : {ec}{engine}{Style.RESET_ALL} "
          f"(pending={summary.get('pending_engine','?')}, j={summary.get('days_pending',0)})")
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

    total  = summary.get("z_capital_eur", summary.get("total_simulated_eur", 0))
    perf   = summary.get("perf_pct", 0)
    perf_c = Fore.GREEN if perf >= 0 else Fore.RED
    budget = summary.get("budget", {})
    print(f"\n  Portefeuille Bot Z : {total:.2f}€  ({perf_c}{perf:+.2f}%{Style.RESET_ALL}) "
          f"vs initial {INITIAL_CAP:.0f}€")
    if budget:
        print(f"  Budget dispatché → " + " | ".join(f"{b.upper()}:{v:.0f}€" for b, v in budget.items()))
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

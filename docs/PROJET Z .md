Bot Z Meta v2+ — Documentation complète (mise à jour 2026-03-06)
=================================================================

SOURCE DE VÉRITÉ : architecture live Bot Z Meta v2
Backtests → docs/BACKTEST_RESULTS.md | Stratégies → docs/BOTS.md

STATUT RÉEL (important avant de lire) :
  Bot Z = allocateur ANALYTIQUE (pas encore exécutif)
  Il calcule l'allocation et écrit budget.json, mais A/B/C/G
  tradent encore avec leur capital propre (1 000€ chacun).
  Budget dispatch branché prévu après revue 2026-04-30.

---

Vue d'ensemble
--------------

Bot Z est un meta-portfolio manager qui supervise 4 stratégies de trading (A/B/C/G)
avec un capital de 10 000€. Il ne trade pas lui-même — il calcule à chaque cycle
quelle stratégie mérite combien de capital, selon les conditions de marché actuelles.

Il tourne toutes les 4h (03h, 07h, 11h, 15h, 19h, 23h UTC), en PREMIER, avant que
les sous-bots exécutent leurs trades. Le budget est écrit dans logs/bot_z/budget.json.

Fichiers clés :
  live/bot_z.py              ← Pilote central (cycle + allocation + logging)
  live/multi_runner.py       ← Lance Bot Z en step 4, AVANT A/B/C/G
  logs/bot_z/state.json      ← État persistant (z_capital, engine, CB, weights)
  logs/bot_z/shadow.jsonl    ← Historique complet (1 ligne JSON par cycle)
  logs/bot_z/budget.json     ← Budget dispatché ce cycle (lu par A/B/C/G à terme)
  backtest/analyze_botz.py   ← Rapport historique + export CSV pour optimisation

Bots valides pour Bot Z :
  A — Supertrend+MR         (logs/supertrend/state.json)
  B — Momentum Antonacci    (logs/momentum/state.json)
  C — Breakout Turtle       (logs/breakout/state.json)
  G — Trend Multi-Asset     (logs/trend/state.json)

Résultats backtest (2020-2026, 6 ans, multi_backtest.py Run 10) :
  Meta v2 : CAGR +43.2% | Sharpe 1.70 | MaxDD -9.6% | 2022 +1.0%
  Distribution engines : ENHANCED 17% / OMEGA 30% / OMEGA_V2 28% / PRO 25%

Améliorations Meta v2+ (implémentées 2026-03-06) :
  Switch cost penalty, regime confidence×persistence, volatility targeting,
  BTC realized vol override, corrélation inter-bots, allocation drift tracking.

---

Entrées du cycle
----------------

À chaque cycle, Bot Z reçoit les données macro :
  - vix              — valeur exacte du VIX (yfinance ^VIX)
  - qqq_regime_ok    — bool : QQQ > SMA200 (yfinance)
  - btc_context.btc_trend — "bull", "strong_bull", "bear", "strong_bear"
                           (BTC vs EMA200 4h, Binance)

Il lit aussi les state files des 4 sous-bots :
  capital     — cash libre du bot
  positions   — {symbol: {entry, size}} positions ouvertes
  trades      — historique des trades clôturés (pour quality score)

---

Étape 1 — Détection du régime de marché
-----------------------------------------

Priorité :
  1. HIGH_VOL  →  VIX > 35
  2. BEAR      →  QQQ < SMA200  OU  VIX > 30
  3. BULL      →  QQQ > SMA200  ET  BTC bull  ET  VIX < 25
  4. RANGE     →  tout le reste

Momentum Overlay (couche supplémentaire) :
  - BTC bearish ET QQQ bearish → force BEAR (peu importe le régime de base)
  - BTC bearish OU QQQ bearish (un seul) → force HIGH_VOL si on était en BULL ou RANGE

BTC Realized Vol Override (Meta v2+) :
  - btc_vol_20d = std(BTC_returns_20_candles_4h) × sqrt(6×365)
  - Si btc_vol_20d > BTC_HIGH_VOL_THRESHOLD (80%) ET régime BULL/RANGE → force HIGH_VOL
  - Capte les crises crypto que VIX détecte avec retard

Score de confiance calculé aussi (0–1) selon la force du signal :
  HIGH_VOL : min(1.0, (vix - 35) / 15 + 0.5)
  BEAR     : 0.8 si not qqq_ok, sinon (vix-25)/10 + 0.4
  BULL     : max(0.4, (25-vix)/10) × 1.1 si strong_bull
  RANGE    : 0.6 fixe

---

Étape 2 — Calcul du z_capital (tracking P&L réel)
----------------------------------------------------

Bot Z calcule sa propre valeur de portefeuille (10 000€ au départ)
via les retours pondérés des sous-bots.

Mark-to-market RÉEL (depuis 2026-03-06) :
  Si ohlcv daily disponible → utilise df["close"].iloc[-1] comme prix live
  Sinon fallback au prix d'entrée (approximation conservative)

  bot_values[b] = state.capital + sum(live_price × position.size)
  cycle_returns[b] = bot_values[b] / prev_bot_values[b] - 1
  weighted_return = sum(prev_weights[b] × cycle_returns[b])
  new_z_capital = max(0.0, z_capital × (1 + weighted_return))

Les poids du cycle précédent (prev_weights) sont utilisés pour éviter
le biais de look-ahead.

---

Étape 3 — Circuit Breaker
--------------------------

Drawdown calculé sur z_capital vs son pic historique (cb_peak) :
  port_dd = (new_z_capital - cb_peak) / cb_peak

Récupération : DD > -10% → +0.005/cycle

CB tiers adaptés par engine (appliqués après la sélection d'engine) :

  Engine     Tier 1              Tier 2              Tier 3
  ENHANCED   DD<-25% → ×0.30    —                   —
  OMEGA      DD<-25% → ×0.30    —                   —
  OMEGA_V2   DD<-20% → ×0.50    DD<-30% → ×0.30     —
  PRO        DD<-10% → ×0.80    DD<-20% → ×0.50     DD<-30% → ×0.30

Si VIX > 35, le cb_factor est plafonné à 0.70 (30% cash minimum forcé).
exposition effective = z_capital × cb_factor

---

Étape 4 — Sélection de l'engine (Meta v2)
-------------------------------------------

Hard rules (priorité absolue)

  PRO forcé si :
    (BTC bearish ET QQQ bearish ET VIX > 26)
    OU VIX > 32
    OU DD < -12%

  ENHANCED bloqué si :
    BTC bearish OU QQQ bearish

Scoring data-driven (si aucune hard rule)

  Pour chaque engine candidat :
    rf    = regime_fit × regime_confidence × regime_strength
    score = 0.50 × rf + 0.30 × quality_norm + 0.20 × inv_risk_norm
            - SWITCH_PENALTY (0.05) si engine ≠ current_engine

  Switch cost penalty (Meta v2+) :
    Évite les micro-switchs sur signaux marginaux. L'engine en place
    bénéficie d'un avantage de 0.05 points, forçant un signal net pour switcher.

  Regime confidence × persistence (Meta v2+) :
    regime_confidence : score [0-1] selon la force du signal de régime
    regime_strength   : min(1.0, days_in_current_regime / REGIME_PERSIST_DAYS)
                        → 1j=0.14, 3j=0.43, 7j=1.0
    rf = regime_fit × regime_confidence × regime_strength
    En début de régime ou en transition → rf réduit → OMEGA favorisé (neutre)

  regime_fit — table fixe calibrée sur backtest 2020-2026 :

    Engine     BULL   RANGE  HIGH_VOL  BEAR
    ENHANCED   1.0    0.6    0.3       0.1
    OMEGA      0.8    0.8    0.7       0.5
    OMEGA_V2   0.5    0.7    0.9       0.8
    PRO        0.3    0.5    0.8       1.0

  quality_norm — Sharpe proxy sur les 20 derniers trades (avec ramp-up) :
    < 5 trades  → score neutre 1.0 (évite l'instabilité en début de paper)
    5-20 trades → blend progressif : confidence = (n-5) / 15.0
                  score = 1.0 + confidence × (raw - 1.0)
    >= 20 trades → score plein raw = clamp(1.0 + sharpe×0.3, 0.3, 1.5)
    quality_norm = avg(scores) / max(scores)

  inv_risk_norm — profil différent par engine :
    PRO     : favorisé quand vol haute → min(1.0, avg_vol / TARGET_VOL)
    OMEGA_V2: favorisé en stress modéré → 0.3 + ((VIX-20)/20) × 0.7
    ENHANCED/OMEGA : favorisés quand vol basse → 1.0 - avg_vol / max_vol

  Engine reasons loggés dans shadow.jsonl :
    hard_rule_pro, block_enhanced, btc_bearish, qqq_bearish, vix,
    port_dd_pct, regime, regime_confidence, regime_strength, btc_realized_vol,
    raw_engine, rolling_scores, bot_vols, engine_switched, prev_engine

Hysteresis (confirmations requises avant switch)

  Engine cible   Jours de confirmation
  ENHANCED       7 jours
  OMEGA          5 jours
  OMEGA_V2       4 jours
  PRO            3 jours (protection rapide)

Pendant la confirmation : current_engine inchangé, pending_engine et
days_pending trackés en state.

---

Étape 5 — Calcul de l'allocation
----------------------------------

Tables de poids par engine × régime :

ENHANCED → REGIME_WEIGHTS :
  Régime     A     B     C     G
  BULL       0.8   1.0   0.5   1.2
  RANGE      1.0   0.8   0.7   0.8
  BEAR       0.3   0.0   1.5   1.2
  HIGH_VOL   0.5   0.3   1.0   0.8

OMEGA → OMEGA_WEIGHTS :
  Régime     A     B     C     G
  BULL       0.9   1.1   0.7   1.0
  RANGE      1.0   0.9   0.8   0.9
  BEAR       0.4   0.1   1.3   1.1
  HIGH_VOL   0.6   0.4   1.2   1.0

PRO → PRO_WEIGHTS (B toujours = 0, défensif pur) :
  Régime     A     B     C     G
  BULL       0.2   0.0   1.8   1.0
  RANGE      0.3   0.0   1.8   1.0
  BEAR       0.1   0.0   2.0   1.0
  HIGH_VOL   0.2   0.0   1.8   1.0

OMEGA_V2 → blend 50/50 OMEGA + inverse-vol (risk parity) :
  inv_vol[b] = 1 / compute_bot_volatility(b)   # vol annualisée approx
  rp_weight[b] = inv_vol[b] / sum(inv_vol)
  final_weight[b] = 0.5 × omega_norm[b] + 0.5 × rp_weight[b]

Modulation qualité (sauf PRO) :
  raw_weight[b] = regime_weight[b] × rolling_score[b]
  PRO : poids fixes, rolling score ignoré

Normalisation + caps :
  norm_weight[b] = raw_weight[b] / sum(raw_weights)
  budget_eur[b] = min(z_capital × cb_factor_final × norm_weight[b], 4000€)
  Cap à 40% du capital initial (4 000€) par bot.

Volatility targeting global (Meta v2+) :
  portfolio_vol = std(z_capital_returns_20_cycles) × sqrt(2190)  # 6 cycles/jour
  vol_factor    = clip(TARGET_PORTFOLIO_VOL / portfolio_vol, 0.3, 1.5)
  cb_factor_vol = cb_factor × vol_factor
  Si portfolio_vol ≈ target → vol_factor = 1.0 (pas d'impact)
  Si portfolio_vol faible   → vol_factor > 1.0 (augmente expo progressivement)
  Si portfolio_vol élevée   → vol_factor < 1.0 (réduit expo)
  Sécurité : retourne TARGET_PORTFOLIO_VOL si variance ≈ 0 (début paper → vol_factor=1.0)

Corrélation inter-bots dynamique (Meta v2+) :
  avg_corr = mean(pearson(A,B), pearson(A,C), pearson(A,G), pearson(B,C), pearson(B,G), pearson(C,G))
  fenêtre : 20 derniers trades par bot
  Si avg_corr > CORR_REDUCE_THRESHOLD (70%) → cb_factor_vol × 0.80
  Protection crise (liquidations simultanées = corrélations qui explosent)

cb_factor_final = max(CB_MIN_FACTOR, cb_factor_vol × corr_factor)

Allocation drift tracking (Meta v2+) :
  drift = sum(|target_weight[b] - actual_weight[b]|) pour b in VALID_BOTS
  actual_weight[b] = valeur_bot / total_portefeuille_réel
  Loggé dans summary["alloc_drift"]. Warning si drift > 0.20 (20%).
  Valide que l'allocation cible Bot Z correspond à la réalité paper.

Priorité anti-surexposition actif (G > C > A > B) :
  Si un même actif est détenu par plus de 2 bots simultanément,
  le bot de moindre priorité voit son budget × 0.3.

---

Étape 6 — Budget dispatch
---------------------------

Écrit dans logs/bot_z/budget.json AVANT que les sous-bots tournent :

  {
    "ts": "2026-03-06T15:00:00",
    "budget": {
      "a": 2430.0,
      "b": 2970.0,
      "c": 1890.0,
      "g": 2700.0
    }
  }

Statut : budget calculé et loggé. A/B/C/G ne lisent pas encore ce
fichier pour leur sizing — ils utilisent leur capital propre (1 000€).
C'est la prochaine étape architecturale (à faire après quelques semaines
de données paper).

---

Persistance (logs/bot_z/state.json)
-------------------------------------

Sauvegardé après chaque cycle :

  Champ                Contenu
  z_capital            Capital Bot Z actuel (€) — z_capital × (1+weighted_return)
  cb_peak              Plus haut historique de z_capital
  cb_factor            Facteur CB brut (avant vol_factor et corr_factor)
  current_engine       Engine actif
  pending_engine       Engine en attente de confirmation
  days_pending         Cycles de confirmation accumulés
  days_in_regime       Jours consécutifs dans le régime actuel (persist factor)
  last_bot_values      Valeurs de A/B/C/G au cycle précédent (pour MTM)
  last_alloc_weights   Poids normalisés du cycle précédent
  z_capital_history    Dernières 25 valeurs de z_capital (pour portfolio_vol)
  last_portfolio_vol   Vol annualisée du portefeuille (20 cycles)
  last_vol_factor      Facteur vol targeting appliqué ce cycle
  last_avg_corr        Corrélation moyenne inter-bots (20 trades)
  last_alloc_drift     Drift allocation cible vs réelle
  last_regime          Dernier régime détecté
  last_regime_info     {regime, confidence, vix, qqq_ok}
  last_allocation      Allocation complète du dernier cycle
  last_cross_exposure  Surexpositions par actif détectées
  last_warnings        Alertes du dernier cycle
  regime_history       500 derniers régimes avec timestamps + strength
  allocation_history   500 dernières allocations (budget_eur par bot)
  total_simulated_eur  Alias de z_capital (dashboard compat)
  perf_pct             Performance % vs 10 000€ initial
  days_running         Jours depuis PAPER_START_DATE (2026-03-06)

Shadow log (logs/bot_z/shadow.jsonl) :
  Une ligne JSON par cycle, contient tout le résumé + engine_reason +
  mtm_prices + mtm_live flag. Base du graphique d'equity du dashboard
  et de l'analyse historique (analyze_botz.py).

---

Script d'analyse historique
-----------------------------

backtest/analyze_botz.py — lit shadow.jsonl et génère :

  1. Résumé global (capital, CAGR, Sharpe, MaxDD)
  2. Performance par engine (CAGR, temps actif, n_cycles)
  3. Performance par régime (PnL, % temps)
  4. Switchs d'engine avec raisons (hard_rule_pro, btc_bearish, etc.)
  5. Activations Circuit Breaker (durée, DD moyen)
  6. Qualité et vol des bots (rolling scores convergence)
  7. Allocations moyennes par engine
  8. Recommandations automatiques (seuils, hysteresis)

Usage :
  python backtest/analyze_botz.py           # rapport terminal
  python backtest/analyze_botz.py --csv     # + export CSV
  python backtest/analyze_botz.py --last 30 # 30 derniers cycles

---

Lacunes connues et roadmap
---------------------------

1. Budget dispatch non consommé (A/B/C/G lisent pas budget.json)
   → Prochaine étape : brancher après quelques semaines de data paper
   → 3 niveaux : (1) nouvelles entrées seulement, (2) reduce_only si
     exposition > budget, (3) sortie forcée graduelle si budget=0 prolongé

2. Rolling scores instables en début de paper (< 5 trades)
   → Quality score neutre 1.0 pour tous — sélection engine repose
     sur regime_fit + inv_risk_norm uniquement les premières semaines
   → Prévu résolu après 3-4 semaines de paper

3. Bot Z n'exécute aucun ordre
   → Observateur/allocateur pur — ne peut pas forcer la fermeture
     d'une position d'un sous-bot même si allocation tombe à 0€

4. Revue 2026-04-30
   → Analyser shadow.jsonl avec analyze_botz.py (drift, vol, corr, switches)
   → Décider passage en live ou ajustement paramètres
   → Brancher budget dispatch + risk budgeting par trade si données suffisantes

5. Risk budgeting par trade (prochaine étape après revue)
   → risk_per_trade_eur = 0.4% × z_capital (ex. 40€ sur 10 000€)
   → size = risk_per_trade / |entry - stop|
   → Effet estimé : Sharpe +15-30%, MaxDD -20-30%

---

Configuration production (live/bot_z.py)
-----------------------------------------

  INITIAL_CAP             = 10 000€ (4 bots × 2 500€ notionnel)
  PAPER_START_DATE        = 2026-03-06
  PAPER_REVIEW_DATE       = 2026-04-30
  TARGET_VOL              = 0.15   (vol cible rolling score par bot)
  MAX_BOT_WEIGHT          = 0.40   (cap 40% = 4 000€ par bot)
  MAX_ASSET_EXPOSURE      = 0.30   (cap 30% sur un même actif)
  MAX_BOTS_SAME_ASSET     = 2      (max 2 bots longs sur le même actif)
  CASH_VIX_THRESHOLD      = 35.0   (VIX > 35 → 30% cash forcé)
  CB_THRESHOLD            = -0.25  (-25% DD → réduction exposition)
  CB_MIN_FACTOR           = 0.30   (70% cash minimum en CB max)
  CB_RECOVERY             = 0.005  (+0.5%/cycle de récupération)
  BOT_PRIORITY            = [G, C, A, B] (résolution conflits actif)

  — Améliorations Meta v2+ —
  SWITCH_PENALTY          = 0.05   (pénalité score si changement d'engine)
  TARGET_PORTFOLIO_VOL    = 0.20   (vol cible portefeuille — vol targeting global)
  BTC_HIGH_VOL_THRESHOLD  = 0.80   (BTC 20d annualized vol > 80% → force HIGH_VOL)
  CORR_REDUCE_THRESHOLD   = 0.70   (corrélation inter-bots > 70% → expo ×0.80)
  REGIME_PERSIST_DAYS     = 7      (jours pour confiance pleine dans un régime)

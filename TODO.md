# TODO — Bot Trading

## Critique (peut faire perdre de l'argent)

- [x] **Max drawdown coupe-circuit** — Arrêter le bot si capital chute de -15% (configurable). Notification Telegram + log. Reprise manuelle uniquement
- [x] **Filtre earnings/actualités** — Bloquer les entrées xStock dans les 24h avant/après un rapport trimestriel (yfinance `calendar`)
- [x] **Summer time CEST** — Timezone America/New_York (ZoneInfo) → gère automatiquement EST/EDT
- [x] **Corrélation positions** — Max 1 position par secteur (tech/auto/ecommerce/crypto) — `config.SECTORS`

## Important (performance et fiabilité)

- [x] **Telegram** — @Damortrading_bot configuré et opérationnel
- [x] **Fix filtre volume xStocks** — Volume 0 → NaN + ffill dans supertrend.py
- [x] **Backup paper_state.json** — Backup local `logs/backups/` (30j) + envoi Telegram quotidien
- [x] **Log rotation** — logrotate configuré VPS (daily, 7 fichiers, 10MB max, copytruncate)
- [x] **Backtesting xStocks** — Fonctionne via yfinance (1d pour >60j, 1h→4h pour ≤60j)

## Confort et monitoring

- [x] **Alerte bot planté** — cron root toutes les 5 min → redémarre + Telegram si bot down
- [x] **Dashboard xStocks** — Badge "US OUVERT/FERMÉ" + section xStocks séparée dans ticker
- [x] **Sharpe ratio + max drawdown** — Sharpe annualisé dans dashboard
- [x] **Endpoint /api/health** — bot_running, last_analysis, open_positions, timestamp
- [x] **Dashboard mobile** — CSS auto-fill + responsive
- [x] **Fear & Greed + VIX dashboard** — 2 cartes temps réel, WebSocket sentiment_update
- [x] **Bot Z Dashboard** — Portfolio Z hero (engine, capital, equity curve, allocation doughnut)
- [x] **Lazy loading tabs** — Charts des onglets secondaires chargés au 1er clic (fix page infinie)
- [ ] **Tests end-to-end** — Signal xStock → paper state → Telegram → log signals.jsonl

## Avancé (plus tard)

- [x] **Multi-timeframe** — Confirmation 1d avant entrée 4h
- [x] **Régime de marché VIX linéaire** — VIX15→×1.0, VIX25→×0.63, VIX35+→×0.25
- [x] **Rotation capital** — Bidirectionnel : meilleure catégorie ×1.3, moins bonne ×0.7
- [x] **Prompts Claude enrichis** — BTC context + VIX + Fear & Greed + funding + news
- [x] **Fear & Greed Index** — alternative.me, injecté dans Claude + dashboard
- [x] **Funding rates crypto** — Binance fapi publique, injecté dans Claude
- [x] **Logs enrichis revue mensuelle** — vix, fear_greed, funding_rate, btc_trend dans SCAN

## Bot Z — Pipeline complet

- [x] **Bot Z Meta v2** — Pilote 10 000€ sur A/B/C/G, 4 engines (ENHANCED/OMEGA/OMEGA_V2/PRO)
- [x] **Mark-to-market réel** — Prix live via OHLCV daily (vs prix d'entrée)
- [x] **Engine reasons logging** — hard_rule_pro, btc_bearish, qqq_bearish, vix, scores loggés
- [x] **Quality ramp-up** — Blend progressif neutre→score réel entre 5 et 20 trades
- [x] **Budget dispatch** — logs/bot_z/budget.json écrit avant chaque cycle sub-bots
- [x] **analyze_botz.py** — Rapport historique + export CSV pour optimisation pré-live
- [ ] **Budget dispatch consommé** — A/B/C/G lisent budget.json pour leur position sizing
      Niveau 1 : agit sur nouvelles entrées seulement (sizing = budget / n_symbols)
      Niveau 2 : si exposition > budget → reduce_only sur prochaine clôture
      Niveau 3 : budget=0 prolongé → sortie forcée graduelle (prochaines 3 clôtures)
      → A FAIRE APRÈS revue 2026-04-30 avec données réelles

## Revue 2026-04-30 — Protocole complet

### 0. Récupérer les données du VPS
```bash
ssh ubuntu@51.210.13.248
sudo cp /home/botuser/bot-trading/logs/bot_z/shadow.jsonl /tmp/shadow_2026-04-30.jsonl
exit
scp ubuntu@51.210.13.248:/tmp/shadow_2026-04-30.jsonl backtest/results/
```

### 1. Rapport Bot Z (rapport principal)
```bash
python backtest/analyze_botz.py --csv
# Génère : equity_timeline.csv | engine_switches.csv | budget_history.csv | meta_v2plus_metrics.csv
```

**Questions clés à répondre** :
- [ ] Fréquence switchs engine (cible < 0.3/jour) — si > : augmenter hysteresis OMEGA à 7j
- [ ] % temps en PRO (cible < 30%) — si > : hard rules trop sensibles ?
- [ ] MaxDD paper (cible < -12%) — si > : CB trop lent ?
- [ ] Rolling scores convergés ? (cible : score > 1.0 pour G et C après 20 trades)
- [ ] Vol_factor moyen (cible : 0.85-1.15 → vol targeting actif mais stable)
- [ ] Allocation drift moyen (cible < 15%) — si > 20% : budget dispatch urgent
- [ ] Corrélation inter-bots (cible < 50%) — si > 70% régulier : stratégies trop similaires
- [ ] BTC override HIGH_VOL : combien d'épisodes ? Ont-ils évité des pertes ?
- [ ] Regime confidence moyenne (cible > 0.65) — si < : paramètres régime à ajuster

### 2. Rapport Contest (A/B/C/G individuels)
```bash
# Sur VPS ou en local avec state files récupérés
python backtest/analyze_botz.py --csv
# + vérifier manuellement logs/supertrend/state.json, momentum, breakout, trend
```
- [ ] Quel bot a le meilleur win rate 20 derniers trades ?
- [ ] Bot B (Momentum) : combien de rotations ? VIX>30 a bloqué combien de fois ?
- [ ] Bot C (Breakout) : le VIX scaling a-t-il réduit les positions ?

### 3. Décision go/no-go
- [ ] **Budget dispatch** — Brancher si drift < 15% et rolling scores convergés
- [ ] **Risk budgeting par trade** — si budget dispatch branché : ajouter risk_per_trade_eur
- [ ] **Passage en live** — Décision finale (cible : live en 2026-07 si paper ≥ 4 mois)
- [ ] **Ajustement paramètres** — switch penalty, regime_persist_days, btc_vol_threshold

## Prochaine session — Priorités

- [ ] **Budget dispatch + Risk Budgeting par trade** (audit ChatGPT — upgrade prioritaire)
      Aujourd'hui Bot Z envoie budget en € mais les bots tradent avec leurs règles internes.
      Upgrade : Bot Z envoie aussi `risk_per_trade_eur` dans budget.json :
        `{ "a": { "budget_eur": 2430, "risk_per_trade_eur": 40 }, ... }`
      Chaque bot calcule : `size = risk_per_trade / |entry - stop|` (au lieu de capital × pct)
      Tous les trades risquent le même montant → portefeuille beaucoup plus stable.
      Effet estimé : Sharpe 1.70 → 1.9-2.1 | MaxDD -9.6% → -7 à -8%
      Niveau 1 : nouvelles entrées seulement (sizing risk-based)
      Niveau 2 : reduce_only si exposition > budget
      Niveau 3 : sortie forcée graduelle si budget=0 prolongé
      Bonus : max_portfolio_risk = 3% → bloque nouvelles positions si somme risques > 3%

- [ ] **Script revue mensuelle** — Parser signals.jsonl → rapport win rate par contexte macro
      (VIX range, F&G range, BTC trend, acceptance rate Claude) → Telegram

- [ ] **Backtesting paramétrique Bot A** — Scanner ATR_MULTIPLIER [2.0→4.0] ×
      TAKE_PROFIT_RATIO [2.0→3.5] × Supertrend mult [3.5→5.5]

- [ ] **Time-based exit** — Sortir après 10 bougies (40h en 4h) sans atteindre le TP

- [ ] **Kraken xStocks live** — Passer PAPER_TRADING=false après revue ~1 mois

## Améliorations Bot Z Meta v2 — Pipeline (audit ChatGPT 2026-03-06)

### Déjà implémenté (ne pas refaire)
- [x] Regime confidence score — `detect_regime_score()` retourne confidence [0-1]
- [x] Strategy decay partial — quality ramp-up (5→20 trades) + Omega v2 meta-learning
- [x] Multi-tier CB par engine — PRO 3 tiers, OMEGA_V2 2 tiers
- [x] Anti-surexposition actif — max 2 bots même actif, priorité G>C>A>B

### A implémenter — par priorité

- [x] **[CRITIQUE avant live] Allocation drift tracking**
      `drift = sum(|target_weight[b] - actual_weight[b]|)` — warning si > 20%
      Implémenté dans `compute_allocation_drift()` — loggé dans shadow.jsonl

- [x] **[PRIORITE 1] Volatility targeting global du portefeuille**
      `vol_factor = clip(TARGET_PORTFOLIO_VOL / portfolio_vol_20d, 0.3, 1.5)`
      Appliqué sur cb_factor avant budget dispatch
      Nouvelles constantes : TARGET_PORTFOLIO_VOL=0.20

- [x] **[PRIORITE 2] Switch cost penalty dans le scoring**
      `-SWITCH_PENALTY (0.05)` si engine candidat ≠ current_engine

- [x] **[PRIORITE 3] Regime confidence dans le scoring**
      `rf = regime_fit × regime_confidence × regime_strength`

- [x] **[PRIORITE 4] Regime persistence factor**
      `regime_strength = min(1.0, days_in_regime / REGIME_PERSIST_DAYS)`
      Loggé dans state.json (days_in_regime) et shadow.jsonl

- [x] **[PRIORITE 5] Crypto realized vol pour régime**
      `compute_btc_realized_vol()` → BTC 20 candles 4h annualisé
      Si vol > BTC_HIGH_VOL_THRESHOLD (80%) et BULL/RANGE → force HIGH_VOL

- [x] **[PRIORITE 6] Corrélation dynamique des bots**
      `compute_bot_correlation()` — pairwise pearson sur 20 trades
      avg_corr > CORR_REDUCE_THRESHOLD (70%) → exposition ×0.80

### Notes audit ChatGPT (2026-03-06) — A relire en debut de session

### 1. Risk Budgeting par trade — upgrade Sharpe
Principe : chaque trade risque X% du portefeuille total, peu importe le bot ou l'actif.
  risk_per_trade = 0.4% × 10 000€ = 40€
  size = 40€ / |entry - stop|
Effet : Sharpe estimé +15-30% | MaxDD -20-30% | portefeuille plus prévisible et scalable.
C'est la dernière étape avant un système professionnel. A brancher avec le budget dispatch.

### 2. Strategy decay — vigilance long terme
Trend/momentum/breakout sont tous corrélés au facteur "trend following".
Protections déjà en place : multi-stratégies, meta-allocation, regime detection.
Facteurs manquants à long terme :
  - Volatility factor (long vol en crash, short vol en calme)
  - Relative value / pairs trading (ETH/BTC, SPY/QQQ — décorrélé du trend)
Règle quant : remplacer ~20% des stratégies tous les 3 ans.
Bot Z est une plateforme évolutive — peut superviser 8-10 bots sans refonte.

### 3. Verdict global ChatGPT
"Architecture équivalente à un fonds multi-stratégies simplifié."
Estimation réaliste performance live : CAGR 25-30% (backtests donnent souvent 2× le réel).
Point le plus solide : walk-forward OOS 2023-2026 valide l'edge hors-échantillon.

## Fait (historique)

- [x] Stratégie Supertrend + EMA + ADX + RSI + volume + trailing stop ratchet
- [x] Filtre Claude AI avant chaque BUY (claude-haiku-4-5-20251001, max_tokens=160)
- [x] Analyse pré-marché Claude à 8h00 ET (xStocks, max_tokens=1200)
- [x] Filtre heures marché US (entrées bloquées hors 9h30-16h00 ET)
- [x] xStocks Kraken : NVDAx, AAPLx, MSFTx, METAx, GOOGx, PLTRx, AMDx, AVGOx, GLDx, NFLXx, CRWDx
- [x] Crypto : BTC/EUR, ETH/EUR, SOL/EUR, BNB/EUR, TON/EUR
- [x] Dashboard dark — onglets A→F + Contest + Portfolio Z
- [x] CI/CD GitHub Actions → VPS OVH (push → déploiement ~30s)
- [x] Sécurité VPS — UFW + fail2ban + SSH hardening
- [x] Contest 6 bots : A (Supertrend) + B (Momentum) + C (Breakout) + D (DeepSeek) + E (Sonnet) + F (Haiku)
- [x] Bot Z lancé en paper trading 2026-03-06 (10 000€, revue 2026-04-30)
- [x] Backtest complet multi_backtest.py (2020-2026) — Meta v2 CAGR +43.2%, Sharpe 1.70, MaxDD -9.6%

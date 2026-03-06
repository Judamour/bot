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

## Prochaine session — Priorités

- [ ] **Revue données paper** — Lancer `python backtest/analyze_botz.py --csv` sur données VPS
      → Fréquence switchs engine, % hard_rule_pro, durée par engine, CB activations
      → Vérifier que quality scores convergent (trade count par bot)

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

- [ ] **[CRITIQUE avant live] Allocation drift tracking**
      Mesurer l'écart entre allocation cible (Bot Z) et allocation réelle (positions sub-bots).
      `drift = sum(|poids_cible[b] - poids_reel[b]|) for b in VALID_BOTS`
      Si drift > 0.20 → warning log + Telegram. Valide que backtest ≈ paper live.
      Sans ça, le Sharpe 1.70 du backtest n'a pas de valeur opérationnelle.

- [ ] **[PRIORITE 1] Volatility targeting global du portefeuille**
      `portfolio_vol = std(z_capital_returns_20d) * sqrt(252)`
      `vol_factor = clip(TARGET_VOL / portfolio_vol, 0.3, 1.5)`
      `exposition = z_capital * cb_factor * vol_factor`  (avant calcul budgets)
      Effet estimé : Sharpe 1.70 → ~1.9 | MaxDD -9.6% → ~-7%
      Référence : AQR, Man AHL, Winton — tous utilisent VT comme brique de base.
      TARGET_VOL = 0.20 (20% annualisé — adapté crypto+stocks)

- [ ] **[PRIORITE 2] Switch cost penalty dans le scoring**
      Ajouter `-0.05` au score d'un engine différent du current_engine.
      `if candidate != current_engine: score -= SWITCH_PENALTY`
      Evite les micro-switchs sur signaux marginaux. Moins de friction de performance.

- [ ] **[PRIORITE 3] Regime confidence dans le scoring** (trivial — déjà calculé)
      Utiliser `regime_confidence` dans la formule :
      `score = 0.50 * regime_fit * regime_confidence + 0.30 * quality + 0.20 * inv_risk`
      En transition de régime (confidence 0.6), pondère moins le regime_fit → privilégie OMEGA.

- [ ] **[PRIORITE 4] Regime persistence factor**
      `regime_strength = min(1.0, days_in_current_regime / 7.0)`
      facteurs : 1j→0.6 | 3j→0.8 | 7j→1.0
      `regime_fit *= regime_strength`
      Evite les faux signaux en début de régime.

- [ ] **[PRIORITE 5] Crypto realized vol pour régime**
      VIX est aveugle aux crises crypto. Ajouter :
      `btc_vol_20d = std(btc_daily_returns_20d) * sqrt(252)`
      Si btc_vol_20d > 0.80 ET régime BULL/RANGE → forcer HIGH_VOL
      Capte les crashes crypto que VIX détecte avec retard.

- [ ] **[PRIORITE 6] Corrélation dynamique des bots**
      `avg_corr = mean(corr(returns_A,B), corr(A,C), corr(A,G), corr(B,C), corr(B,G), corr(C,G))`
      fenêtre 20 trades. Si avg_corr > 0.70 → `portfolio_exposure *= 0.8`
      Protection crise (liquidations simultanées = corrélations qui explosent).

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

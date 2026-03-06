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

- [ ] **Script revue mensuelle** — Parser signals.jsonl → rapport win rate par contexte macro
      (VIX range, F&G range, BTC trend, acceptance rate Claude) → Telegram

- [ ] **Backtesting paramétrique Bot A** — Scanner ATR_MULTIPLIER [2.0→4.0] ×
      TAKE_PROFIT_RATIO [2.0→3.5] × Supertrend mult [3.5→5.5]

- [ ] **Time-based exit** — Sortir après 10 bougies (40h en 4h) sans atteindre le TP

- [ ] **Kraken xStocks live** — Passer PAPER_TRADING=false après revue ~1 mois

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

# TODO — Bot Trading

## 🔴 Critique (peut faire perdre de l'argent)

- [x] **Max drawdown coupe-circuit** — Arrêter le bot si capital chute de -15% (configurable). Notification Telegram + log. Reprise manuelle uniquement
- [x] **Filtre earnings/actualités** — Bloquer les entrées xStock dans les 24h avant/après un rapport trimestriel (yfinance `calendar`)
- [x] **Summer time CEST** — Timezone America/New_York (ZoneInfo) → gère automatiquement EST/EDT
- [x] **Corrélation positions** — Max 1 position par secteur (tech/auto/ecommerce/crypto) — `config.SECTORS`

## 🟡 Important (performance et fiabilité)

- [x] **Telegram** — @Damortrading_bot configuré et opérationnel (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID dans .env VPS)
- [x] **Fix filtre volume xStocks** — Volume 0 → NaN + ffill dans supertrend.py
- [x] **Backup paper_state.json** — Backup local `logs/backups/` (30j) + envoi Telegram quotidien
- [x] **Log rotation** — logrotate configuré VPS (daily, 7 fichiers, 10MB max, copytruncate)
- [x] **Backtesting xStocks** — Fonctionne via yfinance (1d pour >60j, 1h→4h pour ≤60j)

## 🟢 Confort et monitoring

- [x] **Alerte bot planté** — cron root toutes les 5 min → redémarre + Telegram si bot down
- [x] **Dashboard xStocks** — Badge "US OUVERT/FERMÉ" + section xStocks séparée dans ticker + chart yfinance
- [x] **Sharpe ratio + max drawdown** — Sharpe annualisé dans dashboard (7e metric card)
- [x] **Endpoint /api/health** — bot_running, last_analysis, open_positions, timestamp
- [x] **Dashboard mobile** — CSS auto-fill + responsive (stack panels, chart 260px)
- [x] **Section Claude AI dashboard** — Tableau filtres BUY (CONFIRMÉ/IGNORÉ, raison, RSI, ADX) + analyse pré-marché
- [x] **Fear & Greed + VIX dashboard** — 2 cartes temps réel, WebSocket sentiment_update
- [ ] **Tests end-to-end** — Signal xStock → paper state → Telegram → log signals.jsonl

## 🔵 Avancé (plus tard)

- [x] **Multi-timeframe** — Confirmation 1d (ST↑ + >EMA200) avant entrée 4h, avant appel Claude
- [x] **Régime de marché VIX linéaire** — VIX15→×1.0, VIX25→×0.63, VIX35+→×0.25 (progressif, pas binaire)
- [x] **Rotation capital** — Bidirectionnel : meilleure catégorie ×1.3, moins bonne ×0.7 (20 derniers trades, combiné VIX)
- [x] **Notifications enrichies** — Chart matplotlib 60 bougies (prix + ST + EMA200 + SL/TP) joint au BUY Telegram
- [x] **Prompts Claude enrichis** — BTC context + VIX + Fear & Greed + funding rates + news + portfolio state injectés
- [x] **News injection Claude** — yfinance par symbole (NVDA, BTC-USD) + Yahoo Finance RSS S&P500+Nasdaq (0€, zéro clé)
- [x] **Fear & Greed Index** — alternative.me, injecté dans Claude + dashboard
- [x] **Funding rates crypto** — Binance fapi publique, injecté dans Claude
- [x] **LINK/EUR + AVAX/EUR** — 15 symboles total (8 crypto + 7 xStocks)
- [x] **Logs enrichis revue mensuelle** — vix, fear_greed, funding_rate, btc_trend, rotation_factor dans chaque SCAN/CLAUDE_FILTER
- [ ] **Script revue mensuelle** — Parser signals.jsonl → rapport win rate par contexte macro (VIX range, F&G range, BTC trend, acceptance rate Claude)
- [ ] **Backtesting paramétrique** — Scanner ATR_MULTIPLIER [2.0→4.0] × TAKE_PROFIT_RATIO [2.0→3.5] × Supertrend mult [3.5→5.5]
- [ ] **Time-based exit** — Sortir après 10 bougies (40h en 4h) sans atteindre le TP
- [ ] **Kraken xStocks live** — Passer `PAPER_TRADING=false` + ordres Kraken ccxt (après revue data ~1 mois)

## Fait ✓

- [x] Stratégie Supertrend + EMA + ADX + RSI + volume + trailing stop ratchet
- [x] Filtre Claude AI avant chaque BUY (claude-haiku-4-5-20251001, max_tokens=160)
- [x] Analyse pré-marché Claude à 8h00 ET (xStocks, max_tokens=1200)
- [x] Filtre heures marché US (entrées bloquées hors 9h30-16h00 ET)
- [x] xStocks Kraken : NVDAx/EUR, AAPLx/EUR, TSLAx/EUR, MSFTx/EUR, METAx/EUR, AMZNx/EUR, GOOGx/EUR
- [x] Crypto : BTC/EUR, ETH/EUR, SOL/EUR, BNB/EUR, ADA/EUR, DOT/EUR, LINK/EUR, AVAX/EUR
- [x] Contexte BTC global (bull/bear + EMA200) injecté dans chaque analyse
- [x] Snapshot journalier dans signals.jsonl
- [x] Dashboard dark (Lightweight Charts + Chart.js) — marqueurs BUY/SELL, SL/TP
- [x] CI/CD GitHub Actions → VPS Oracle Cloud
- [x] Sécurité VPS — UFW + fail2ban + SSH hardening

mkdir -p ~/.ssh && echo "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDSiDpmVmg4p0uk+s0Aa8fS8YJMByOJXzKiL2pqmXjUeKbQjgXHFlVcHEcyXdcvxpgb04iXZzLMZVZU1ElR9SV8YZmoCEU/S8Gg/VIYj28fAiwoQ4o7orX3LXUwwaRHgBmGq3kEFovR+hhtB5Wo7dJA/Sti8O5FO2Us/QSqAemPcOerzYzMaHAbdNRQt4WjcrgymeP6rLoIfjkQZ4MhAdHWBPxSzV7PnPts3pH7K6qNnL5Gpw+tBKavZ+0D2+F0FgxmtAOLlcoLaxJN1FsG7k75/ByzILkk2Q8hKfclxl3n+PQWTN6+8C0GYfK7Gnu6x8OWHU5NM7AnHUzC+VXv7t6mUuhgoqaTRWG8RPrtTefpxZi99GeiodF82ujHffrv1H6C+bVM9O20L9OQUnjEuyn5E021LW7QRrwjgK16QRSLStR10Xlzf2gmZ8bCHX5H7xFbQa4v8EP5tGOURJ1PPRh01xVdT8zIKZPpjHGUiHjrvwZ3KbQYCnNtGbSNLz2qCHh575PyGVYYvDMxTXX+RatH3YlvwYHTwZzXpLVcOudJKXP35T/R87zw+0eIme9qKaJHC+ex2hOfvK8WhxY2A0ZfNGQL0vnwMFobzBEFwXibNNZ4ifWn2CermbZh57tmfSYaFTTv6iuialUK0b5sao35OGD1mQyJV6xlww== damoria@rog-flow" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
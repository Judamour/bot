# TODO — Bot Trading

## 🔴 Critique (peut faire perdre de l'argent)

- [x] **Max drawdown coupe-circuit** — Arrêter le bot si capital chute de -15% (configurable). Notification Telegram + log. Reprise manuelle uniquement
- [x] **Filtre earnings/actualités** — Bloquer les entrées xStock dans les 24h avant/après un rapport trimestriel (yfinance `calendar`)
- [x] **Summer time CEST** — Timezone America/New_York (ZoneInfo) → gère automatiquement EST/EDT
- [x] **Corrélation positions** — Max 1 position par secteur (tech/auto/ecommerce/crypto) — `config.SECTORS`

## 🟡 Important (performance et fiabilité)

- [ ] **Telegram** — Créer bot @BotFather → TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID → ajouter dans .env VPS → tester
- [x] **Fix filtre volume xStocks** — Volume 0 → NaN + ffill dans supertrend.py
- [x] **Backup paper_state.json** — Backup local `logs/backups/` (30j) + envoi Telegram quotidien
- [x] **Log rotation** — logrotate configuré VPS (daily, 7 fichiers, 10MB max, copytruncate)
- [x] **Backtesting xStocks** — Fonctionne via yfinance (1d pour >60j, 1h→4h pour ≤60j)

## 🟢 Confort et monitoring

- [ ] **Alerte bot planté** — Healthcheck toutes les 5 min via cron + Telegram si le service `bot` est down
- [ ] **Dashboard xStocks** — Indicateur "Marché US OUVERT/FERMÉ", section xStocks séparée des cryptos
- [ ] **Sharpe ratio + max drawdown** dans le dashboard (actuellement seulement win rate + PnL)
- [ ] **Endpoint /api/health** — Statut bot (up/down), dernière analyse, nb positions ouvertes
- [ ] **Tests end-to-end** — Signal xStock → paper state → Telegram → log signals.jsonl
- [ ] **Dashboard mobile** — Responsive design

## 🔵 Avancé (plus tard)

- [ ] **Multi-timeframe** — Confirmation 1d avant entrée 4h (moins de faux signaux)
- [ ] **Régime de marché** — Réduire taille positions en haute volatilité (VIX > 25)
- [ ] **Rotation capital** — Allouer plus aux cryptos si xStocks sous-performent
- [ ] **Notifications enrichies** — Image du chart joint au signal BUY sur Telegram
- [ ] **Kraken xStocks live** — Passer `PAPER_TRADING=false` + ordres Kraken ccxt pour les NVDAx, AAPLx... via le même client que les cryptos

## Fait ✓

- [x] Stratégie Supertrend + EMA + ADX + RSI + volume + trailing stop ratchet
- [x] Filtre Claude AI avant chaque BUY (claude-haiku)
- [x] Analyse pré-marché Claude à 14h00 CET (xStocks)
- [x] Filtre heures marché US (entrées bloquées hors 14h30-21h00 CET)
- [x] xStocks via yfinance (NVDA, AAPL, TSLA, MSFT, META, AMZN, GOOG) — paper trading
- [x] Contexte BTC global (bull/bear) injecté dans chaque analyse
- [x] Snapshot journalier dans signals.jsonl
- [x] Dashboard dark (Lightweight Charts + Chart.js) — marqueurs BUY/SELL, SL/TP
- [x] CI/CD GitHub Actions → VPS Oracle Cloud
- [x] Sécurité VPS — UFW + fail2ban + SSH hardening

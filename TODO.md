# TODO — Bot Trading

## Prochaine session

- [ ] **Telegram** — Créer bot @BotFather → TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID → ajouter dans .env VPS → tester
- [ ] **XTB Connexion** — Ajouter xAPIconnector, créer `live/xtb_client.py`, credentials démo dans .env (XTB_LOGIN, XTB_PASSWORD, XTB_MODE)
- [ ] **XTB Fetcher OHLCV** — Remplacer yfinance par XTB `getChartRangeRequest` pour les xStocks (NVDA.US, AAPL.US…)
- [ ] **XTB Paper orders** — Envoyer BUY/SELL au compte démo XTB via `tradeTransaction`
- [ ] **Dashboard xStocks** — Indicateur "Marché US OUVERT/FERMÉ", section xStocks séparée des cryptos
- [ ] **Tests end-to-end** — Signal xStock → ordre démo XTB → Telegram → log signals.jsonl
- [ ] **Guide prod XTB** — Documenter le passage démo → argent réel (XTB_MODE=real)

## Backlog

- [ ] Backtesting xStocks — valider la stratégie Supertrend sur données historiques actions
- [ ] Filtre volume xStocks — volume souvent nul hors heures de marché, adapter le filtre
- [ ] Summer time — ajuster les heures marché US en CEST (UTC+2) : 15h30-22h00
- [ ] Notifications Telegram enrichies — chart image joint au signal BUY
- [ ] Multi-timeframe — confirmation 1d avant entrée sur 4h
- [ ] Dashboard mobile — responsive design

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

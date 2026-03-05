# Documentation des 6 Bots — Appels API, Paramètres, Timing

## Vue d'ensemble

6 bots en compétition simultanée, chacun avec **1000€ de capital paper**.
Runner : `live/multi_runner.py` — un seul processus, données macro partagées.

**Cycles d'exécution** : 03h00, 07h00, 11h00, 15h00, 19h00, 23h00 UTC
(soit toutes les 4h, 6 fois par jour)

---

## Données partagées (1 seul fetch pour les 6 bots)

Avant chaque cycle, `data/market_snapshot.py` récupère tout :

| Donnée | Source | Fréquence | Note |
|--------|--------|-----------|------|
| OHLCV crypto 4h | Binance API publique | 6×/jour | 16 symboles × 45 jours |
| OHLCV xStocks 4h | yfinance | 6×/jour | 11 symboles × 45 jours |
| OHLCV daily | Binance + yfinance | 6×/jour | 27 symboles × 220 jours (B & C) |
| BTC context | Binance → EMA200 4h | 6×/jour | Trend bull/bear |
| VIX | yfinance `^VIX` 1h | 6×/jour | Valeur exacte |
| Fear & Greed | alternative.me | 6×/jour | Score 0-100 + label |
| Funding rates | Binance FAPI publique | 6×/jour | Crypto seulement |
| News macro | Yahoo Finance RSS | 6×/jour | 4 headlines S&P500+Nasdaq |
| QQQ régime | yfinance QQQ vs SMA200 | 6×/jour | Bool OK/BEARISH |

**Coût API marché** : 100% gratuit (APIs publiques, pas de clé)

---

## Bot A — Supertrend + Mean Reversion + Filtre Claude

**Fichier** : `live/bot.py`
**State** : `logs/supertrend/state.json`

### Stratégie
Signal principal : Supertrend flip haussier sur 4h
Signal secondaire : Mean Reversion RSI(2) < 10

### Paramètres indicateurs
| Paramètre | Valeur |
|-----------|--------|
| Supertrend ATR période | 14 |
| Supertrend multiplicateur | 4.5 |
| EMA lente | 200 |
| EMA rapide | 50 + 9 + 21 |
| RSI période | 14, seuil < 70 |
| ADX seuil | > 22 |
| Volume | > 110% MA |
| MR RSI(2) entrée | < 10 |
| MR RSI(2) sortie | > 90 |

### Filtres durs (tous requis pour BUY)
1. Supertrend flip UP
2. RSI(14) < 70
3. Prix > EMA200

### Filtres doux (contexte Claude)
- ADX > 20 (tendance)
- Volume > 110% MA
- EMA50 > EMA200 (structure haussière)
- EMA9 > EMA21 (momentum court terme)
- Tendance 1d (ST UP + prix > EMA200 daily)
- QQQ > SMA200 (régime Risk-ON)

### Stop loss
- Trend following : **3×ATR** trailing
- Mean Reversion : **1×ATR** serré

### Sizing
- 15% du capital disponible par position
- Plancher 20€ minimum
- VIX scaling : VIX15→×1.0, VIX25→×0.63, VIX35+→×0.25
- Max 6 positions simultanées

### Appel API Claude (filtre BUY)
- **Modèle** : `claude-haiku-4-5-20251001`
- **max_tokens** : 160
- **Quand** : seulement si les 3 filtres durs sont passés (rare)
- **Fréquence estimée** : 0-3 appels/jour
- **Coût estimé** : ~0.01-0.02€/jour
- **Prompt contient** :
  - Prix, RSI, ADX, ATR, EMA50/200
  - 6 filtres doux + reason MTF 1d
  - BTC trend + VIX + Fear & Greed + funding rate
  - Portfolio : slots libres, capital, win rate 20 derniers trades
  - News : 3 headlines symbol (yfinance) + 3 macro (RSS)
- **Réponse attendue** : `DÉCISION: CONFIRME ou IGNORE` + `RAISON: ...`

### Analyse pré-marché quotidienne
- **Modèle** : claude-haiku (via `live/xstock_advisor.py`)
- **max_tokens** : 1200
- **Quand** : 8h00 ET (14h CET / 15h CEST) chaque jour ouvré
- **Envoyé via** : Telegram + dashboard onglet Claude AI

---

## Bot B — Momentum Rotation (Dual Momentum Antonacci)

**Fichier** : `strategies/momentum_strategy.py`
**State** : `logs/momentum/state.json`

### Stratégie
Rotation hebdomadaire vers les 4 actifs à plus fort momentum absolu.
Basé sur Gary Antonacci (2012) — "Dual Momentum Investing".

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Score composite | 0.4×(1m) + 0.4×(3m) + 0.2×(6m) |
| Univers | 16 symboles (CRYPTO + XSTOCKS) |
| Top N | 4 positions |
| Rebalancement | Tous les 6+ jours |
| Sizing | total_portfolio / 4 (25% chacun) |
| Data requis | 130 jours minimum |

### Filtres macro (pause rebalancement)
- VIX > 30 → rebalancement suspendu
- QQQ < SMA200 → rebalancement suspendu
- Stop individuel -12% vérifié **à chaque cycle** (pas seulement rebalancement)

### Stop loss
- **-12% depuis l'entrée** par position

### Appel API
**Aucun.** Stratégie 100% quantitative, pas d'appel LLM.

---

## Bot C — Donchian Breakout Turtle System 2

**Fichier** : `strategies/breakout_strategy.py`
**State** : `logs/breakout/state.json`

### Stratégie
Breakout sur canal de Donchian 55 jours, basé sur les règles Turtle (Richard Dennis, 1983).
Univers restreint : **BTC/EUR, ETH/EUR, SOL/EUR** uniquement (meilleurs trends Donchian).

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Canal entrée | Donchian high 55 jours |
| Canal sortie | Donchian low 20 jours |
| ATR période | 20 (Dennis "N") |
| Filtre ADX | > 20 |
| Stop loss | 2×ATR (Turtle N-stop) |
| Risk par trade | 1% du capital |
| Max position | 33% du capital |
| Data requis | 65+ jours |

### VIX scaling du risque
- VIX ≤ 20 → risk_pct = 1.0% (plein)
- VIX 20-40 → risk_pct = 1.0% → 0.5% (linéaire)
- VIX ≥ 40 → risk_pct = 0.5% (moitié)

### Stop loss
- **2×ATR trailing** (Turtle N-stop), mis à jour à chaque cycle
- Sortie Donchian si prix < low 20j

### Appel API
**Aucun.** Stratégie 100% quantitative, pas d'appel LLM.

---

## Bot D — DeepSeek Reasoner V3.2 (LLM)

**Fichier** : `strategies/llm_strategy.py`
**State** : `logs/llm/state.json`

### Stratégie
Le LLM est la stratégie. DeepSeek R1 reçoit le contexte technique + macro et décide BUY/SELL/HOLD.

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Modèle | `deepseek-reasoner` (via env `DEEPSEEK_MODEL`) |
| max_tokens | 2048 (chain-of-thought R1) |
| Position size | 100€ fixe |
| Max positions | 6 |
| ATR stop | 2×ATR trailing |
| Pre-filter | Supertrend UP + ADX > 18 (ou position ouverte) |

### Appel API DeepSeek
- **URL** : `https://api.deepseek.com` (compatible OpenAI SDK)
- **Quand** : si Supertrend UP + ADX > 18, **ou** position déjà ouverte
- **Économie estimée** : ~80% des appels évités via pre-filter
- **Fréquence estimée** : 0-5 appels par cycle (max 30/jour)
- **Prix** : $0.28/1M tokens input + $0.42/1M tokens output
- **Prompt contient** (identique E & F) :
  - Supertrend direction, RSI(14), ADX(14), ATR(14), EMA50/200
  - 20 dernières bougies 4h OHLC
  - BTC trend + prix, VIX, Fear & Greed, QQQ régime
  - Portfolio : capital libre, slots, win rate 20 trades, position en cours + stop
- **Réponse attendue** : JSON `{"action":"BUY|SELL|HOLD","confidence":0-100,"reason":"..."}`

### Filtre heures de marché
- xStocks : BUY bloqué si marché US fermé (hors 9h30-16h00 ET, hors weekends)
- Crypto : pas de restriction horaire

---

## Bot E — Claude Sonnet 4.6 (LLM)

**Fichier** : `strategies/claude_llm_strategy.py`
**State** : `logs/claude_llm/state.json`

### Stratégie
Identique à Bot D mais utilise Claude Sonnet 4.6 (Anthropic).

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Modèle | `claude-sonnet-4-6` (via env `CLAUDE_LLM_MODEL`) |
| max_tokens | 280 |
| Position size | 100€ fixe |
| Max positions | 6 |
| ATR stop | 2×ATR trailing |
| Pre-filter | Supertrend UP + ADX > 18 (ou position ouverte) |

### Appel API Claude (Anthropic)
- **SDK** : `anthropic` Python
- **Quand** : si Supertrend UP + ADX > 18, **ou** position ouverte
- **Prix** : $3.00/1M tokens input + $15.00/1M tokens output
- **Prompt** : identique Bot D (même template `_build_prompt`)
- **Délai** : `time.sleep(1)` entre chaque symbole (évite rate limiting)

---

## Bot F — Claude Haiku 4.5 (LLM, témoin)

**Fichier** : `strategies/haiku_llm_strategy.py`
**State** : `logs/haiku_llm/state.json`

### Stratégie
Identique à Bot D/E mais avec Claude Haiku 4.5 (modèle le plus petit/rapide).
**Rôle** : témoin comparatif — Haiku vs Sonnet vs DeepSeek R1.

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Modèle | `claude-haiku-4-5-20251001` (via env `HAIKU_LLM_MODEL`) |
| max_tokens | 160 |
| Position size | 100€ fixe |
| Max positions | 6 |
| ATR stop | 2×ATR trailing |
| Pre-filter | Supertrend UP + ADX > 18 (ou position ouverte) |

### Appel API Claude (Anthropic)
- **SDK** : `anthropic` Python
- **Prix** : $0.80/1M tokens input + $4.00/1M tokens output
- **Délai** : `time.sleep(1)` entre chaque symbole

---

## Résumé des appels API par cycle

| Bot | API | Nb appels/cycle (estimé) | Coût/cycle |
|-----|-----|--------------------------|------------|
| A | Anthropic Haiku | 0-3 (si signal passe filtres durs) | ~$0.001 |
| B | Aucun | 0 | $0 |
| C | Aucun | 0 | $0 |
| D | DeepSeek Reasoner | 0-10 (si pre-filter passe) | ~$0.002 |
| E | Anthropic Sonnet | 0-10 (si pre-filter passe) | ~$0.05 |
| F | Anthropic Haiku | 0-10 (si pre-filter passe) | ~$0.008 |
| A pré-marché | Anthropic Haiku | 1/jour ouvré | ~$0.005/jour |

**Total journalier estimé** : $0.05-0.15/jour selon activité du marché

---

## Alertes crédit API (système persistant)

Si une API retourne une erreur de crédit/quota :
1. Telegram immédiat : "Crédits X épuisés"
2. `logs/api_alerts.json` mis à jour
3. Rappel Telegram à **chaque cycle** jusqu'au rechargement
4. Bannière rouge en haut du **dashboard**
5. Dès que l'API répond à nouveau : alerte "Crédits rechargés" + bannière disparaît

Mots-clés détectés : `credit`, `billing`, `insufficient`, `balance`, `quota`, `402`, `payment`, `funds`, `overdue`

---

## Symboles tradés

**Crypto (5)** : BTC/EUR, ETH/EUR, SOL/EUR, BNB/EUR, TON/EUR
**xStocks paper Kraken (11)** : NVDAx, AAPLx, MSFTx, METAx, GOOGx, PLTRx, AMDx, AVGOx, GLDx, NFLXx, CRWDx

**Bot C uniquement** : BTC/EUR, ETH/EUR, SOL/EUR (meilleurs trends Donchian)

---

## Gestion du risque commune

| Règle | Valeur |
|-------|--------|
| Max drawdown portfolio | -15% → alerte Telegram |
| Frais taker Kraken | 0.26% |
| Slippage estimé | 0.10% |
| Max drawdown individuel (B) | -12% stop |
| Max drawdown individuel (C) | 2×ATR Turtle N-stop |
| Max drawdown individuel (D/E/F) | 2×ATR trailing stop |

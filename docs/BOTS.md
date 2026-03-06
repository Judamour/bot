# Documentation des 9 Bots — Appels API, Paramètres, Timing

## Vue d'ensemble

9 bots en compétition simultanée, chacun avec **1000€ de capital paper**.
Runner : `live/multi_runner.py` — un seul processus, données macro partagées.

**Cycles d'exécution** : 03h00, 07h00, 11h00, 15h00, 19h00, 23h00 UTC
(soit toutes les 4h, 6 fois par jour)

---

## Données partagées (1 seul fetch pour les 9 bots)

Avant chaque cycle, `data/market_snapshot.py` récupère tout :

| Donnée | Source | Fréquence | Note |
|--------|--------|-----------|------|
| OHLCV crypto 4h | Binance API publique | 6×/jour | 7 crypto × 45 jours |
| OHLCV xStocks 4h | yfinance | 6×/jour | 13 xStocks × 45 jours |
| OHLCV daily | Binance + yfinance | 6×/jour | 20 symboles × 220 jours (B, C, G, I) |
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

## Bot G — Trend Following Multi-Asset (Volatility Targeting)

**Fichier** : `strategies/trend_following_strategy.py`
**State** : `logs/trend/state.json`
**Origine** : recommandation ChatGPT — architecture CTA funds (AQR, Man Group, SG CTA Index)

### Pourquoi ce bot plutôt qu'améliorer Bot C

Bot C fait du Donchian breakout sur 3 crypto seulement avec un sizing Turtle fixe (1% du capital).
Bot G applique la même philosophie trend following mais :
- Sur **tous les 16 actifs** (crypto + xStocks)
- Avec un **sizing adaptatif** : les actifs calmes reçoivent plus de capital, les volatils moins
- Avec un **filtre de tendance SMA200** (plus robuste que Donchian pur en range)

### Stratégie
Acheter les actifs en tendance haussière confirmée, tenir jusqu'à ce que la tendance se brise.
Les positions les plus grosses vont aux actifs les moins volatils (risk parity partiel).

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| SMA long | 200j |
| SMA court | 50j |
| Breakout entrée | High 50j (shift 1 — no lookahead) |
| Filtre ADX | > 20 |
| ATR période | 20 |
| Stop loss | 3×ATR trailing |
| Sortie tendance | prix < SMA200 |
| Target vol | 15% annualisé |
| Max/position | 10% du capital |
| Max positions | 8 |
| Filtre VIX | > 35 → suspend nouvelles entrées |
| Timeframe | Daily (ohlcv_daily, déjà fetchés pour B et C) |
| Data requis | 230 jours minimum |

### Sizing — Volatility Targeting (détail)
```
daily_vol = std(daily_returns, 20 jours)
annual_vol = daily_vol × √252
size_pct   = min(TARGET_VOL / annual_vol, MAX_POSITION_PCT)
size_units = (capital × size_pct) / entry_price
```
Exemple : BTC annual_vol=60% → size_pct=15%/60%=25% mais cappé à 10%
Exemple : NVDAx annual_vol=30% → size_pct=15%/30%=50% mais cappé à 10%
Exemple : GLDx annual_vol=12% → size_pct=15%/12%=125% cappé à 10% (or = actif calme → taille max)

### Appel API
**Aucun.** Stratégie 100% quantitative, pas d'appel LLM.

### Performance historique de référence
CTA trend following indices : 10-18% CAGR sur longue période.
Sous-performe en marchés range/choppy (2022-2023), sur-performe lors de grandes tendances (2020-2021, 2024).

---

## Bot H — Volatility Compression Breakout (VCB)

**Fichier** : `strategies/vcb_strategy.py`
**State** : `logs/vcb/state.json`
**Origine** : recommandation ChatGPT — stratégie utilisée par certains desks quant (Minervini, O'Neil CANSLIM)

### Concept
Les plus grosses tendances démarrent après une phase de **compression de volatilité** :
1. Grosse hausse → consolidation serrée → la vol baisse
2. Les vendeurs disparaissent, les institutions accumulent
3. Un catalyseur arrive → explosion du prix

Détecter la compression avant qu'elle n'explose = edge statistique réel.

### Pourquoi différent de Bot C (Donchian Breakout)
- Bot C : attend un nouveau high 55j, sans regarder l'état de la volatilité avant le breakout
- Bot H : exige **compression préalable** (ATR décroissant + BB width < 20e percentile) avant d'entrer sur un breakout 20j — filtre 90% des faux breakouts

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Timeframe | 4h (ohlcv_4h, déjà fetchés) |
| SMA long | 200 périodes |
| SMA court | 50 périodes |
| BB période | 20 périodes |
| BB width percentile | < 20% sur 100 barres glissantes |
| ATR compression | ATR(14) décroissant ≥ 5 barres consécutives |
| Breakout entrée | High 20 barres (shift 1 — no lookahead) |
| Stop entrée | 1.5×ATR (serré — l'énergie ne doit pas reculer) |
| Trailing stop | 3×ATR |
| Position size | 20% du capital par position |
| Max positions | 5 |
| Data requis | ~310 barres 4h ≈ 52 jours (couverts par le fetch 45j) |

### Bollinger Band width percentile (détail)
```
bb_mid   = SMA20(close)
bb_width = (BB_upper - BB_lower) / bb_mid
bb_pct   = (bb_width - rolling_min(100)) / (rolling_max(100) - rolling_min(100))
```
`bb_pct < 0.20` = la bande est dans ses 20% les plus serrés des 100 dernières barres

### ATR compression (détail)
```
atr_diff     = ATR.diff()          # négatif = ATR qui baisse
declining    = (atr_diff < 0)      # True quand ATR baisse
compress_cnt = declining.rolling(5).sum()
compressed   = compress_cnt >= 5   # ATR a baissé 5 barres de suite
```

### Univers cible
BTC/EUR, ETH/EUR, SOL/EUR, NVDAx/EUR, AMDx/EUR, METAx/EUR, PLTRx/EUR
(actifs à forte volatilité où les compressions explosent le plus violemment)

### Appel API
**Aucun.** Stratégie 100% quantitative.

### Performance historique de référence
VCB sur portefeuille multi-actifs : 15-25% annualisé dans les backtests, mais **très irrégulier**.
Sous-performe en marchés plats (peu de compressions), sur-performe lors de grandes phases de tendance interrompues de consolidations.

---

## Bot I — Relative Strength Leaders

**Fichier** : `strategies/rs_leaders_strategy.py`
**State** : `logs/rs_leaders/state.json`
**Origine** : recommandation ChatGPT — basé sur MSCI World Quality + Momentum (12-16% CAGR historique)

### Concept
Sélection des **leaders en force relative** sur l'univers complet des 20 symboles :
- Calcul d'un score composite de momentum multi-période pour chaque actif
- Filtres de qualité stricts : structure SMA triple, ADX, volatilité, extension
- Tenir les 3 leaders, sortir si l'actif tombe hors top 5 (buffer anti-churn)
- Sizing via volatility targeting : les actifs plus calmes reçoivent plus de capital

### Différences vs Bot B (Momentum Rotation)
| Critère | Bot B | Bot I |
|---------|-------|-------|
| Score | 1m/3m/6m equal weight | 1m/3m/6m + distance SMA200 (weighted) |
| Filtre structure | Aucun | SMA50 > SMA200 (golden cross) |
| Filtre volatilité | Aucun | vol < 90% annualisée |
| Filtre extension | Aucun | prix pas > 15% au-dessus SMA50 |
| Filtre qualité | Aucun | ADX > 18 |
| Stop loss | -12% fixe | 2.5×ATR trailing + hard stop -10% |
| Exit SMA | Aucun | SMA50 break |
| Sizing | total/4 égal | Volatility targeting (TARGET_VOL=15%) |
| Seuil de sortie | Top 4 strict | Top 5 (buffer 2 rangs) |

### Score RS composite
```
rs_score = 0.35 × (perf 1m) + 0.35 × (perf 3m) + 0.20 × (perf 6m) + 0.10 × (distance SMA200)
```
Périodes : 1m=22j, 3m=66j, 6m=130j.

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Timeframe | Daily (ohlcv_daily, 220 jours) |
| Top N positions | 3 |
| Exit rank | > 5 (sortie si non dans top 5) |
| Rebalancement min | 5 jours |
| ADX seuil | > 18 |
| Vol annualisée max | < 90% |
| Extension SMA50 max | < 15% |
| Hard stop | -10% depuis l'entrée |
| Trailing stop | 2.5×ATR |
| Exit SMA | SMA50 break |
| Target vol | 15% |
| Max position | 30% du capital |
| Pause macro | VIX > 30 ou QQQ bearish |

### Volatility targeting (sizing)
```
size_pct = min(TARGET_VOL / annual_vol, MAX_POS_PCT)
           = min(0.15 / annual_vol, 0.30)
size_units = (capital × size_pct) / entry_price
```
Ex : actif avec vol 30% → size 50% du capital cap à 30% → 30%. Actif vol 50% → 30% du capital.

### Univers
Tous les 20 symboles (7 crypto + 13 xStocks). Le score et les filtres sélectionnent naturellement les meilleurs.

### Appel API
**Aucun.** Stratégie 100% quantitative.

### Performance de référence
MSCI World Quality + Momentum index : 12-16% CAGR sur 10 ans. La version filtrée (ADX, extension) vise à réduire les drawdowns des années 2022-type.

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
| G | Aucun | 0 | $0 |
| H | Aucun | 0 | $0 |
| I | Aucun | 0 | $0 |
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

**Crypto (7)** : BTC/EUR, ETH/EUR, SOL/EUR, BNB/EUR, TON/EUR, LINK/EUR, AVAX/EUR
**xStocks paper Kraken (13)** : NVDAx, AAPLx, TSLAx, MSFTx, METAx, AMZNx, GOOGx, PLTRx, AMDx, AVGOx, GLDx, NFLXx, CRWDx

**Bot C uniquement** : BTC/EUR, ETH/EUR, SOL/EUR (meilleurs trends Donchian)
**Bot H uniquement** : BTC/EUR, ETH/EUR, SOL/EUR, NVDAx, AMDx, METAx, PLTRx (actifs à forte volatilité)
**Bots A, B, D, E, F, G, I** : tous les 20 symboles

---

---

## Bot Z — Portfolio Engine (Paper Trading Production)

**Fichier** : `live/bot_z.py`
**State** : `logs/bot_z/state.json`
**Log** : `logs/bot_z/shadow.jsonl`
**Dashboard** : endpoint `/api/bot_z`

**Phase actuelle : PAPER TRADING** — démarré le 2026-03-06, revue le 2026-04-30
**Capital** : 10 000€ (4 bots validés : A, B, C, G — 2 500€ chacun en base)
**Structure** : Bot Z Enhanced (régime pur 100% dynamique + MO + CB)

---

### Architecture Bot Z Enhanced

```
Bots A, B, C, G (state files + signaux live)
    ↓
Regime Engine (VIX + QQQ SMA200 + BTC trend)
    ↓
Momentum Overlay (BTC EMA200 + QQQ SMA200)
    ↓
Allocation Régime Pur (calibration BEAR v2)
    ↓
Circuit Breaker (DD < -25% → expo 30%)
    ↓
logs/bot_z/ + dashboard
```

---

### Détection du régime + Momentum Overlay

**Régime de base :**

| Régime | Conditions |
|--------|-----------|
| HIGH_VOL | VIX > 35 (prioritaire) |
| BEAR | QQQ < SMA200 ou VIX > 30 |
| BULL | QQQ > SMA200 + BTC bull + VIX < 25 |
| RANGE | Tout le reste |

**Momentum Overlay (couche supplémentaire) :**
- BTC trend bear **ET** QQQ < SMA200 → force **BEAR** (indépendamment du VIX)
- Un seul indicateur bearish → force **HIGH_VOL** si régime était BULL/RANGE
- Réagit avant le VIX (plus proactif, capte les retournements tôt)

---

### Calibration BEAR v2 — Poids par régime

Validée sur backtest 2020-2026 (2022 bear : -9.0% avec Enhanced vs -16.8% equal-weight)

| Bot | BULL | RANGE | BEAR | HIGH_VOL |
|-----|------|-------|------|----------|
| A — Supertrend | 0.8 | 1.0 | **0.3** | 0.5 |
| B — Momentum | 1.0 | 0.8 | **0.0** | 0.3 |
| C — Breakout | 0.5 | 0.7 | **1.5** | 1.0 |
| G — Trend Multi-Asset | 1.2 | 0.8 | **1.2** | 0.8 |

**Logique BEAR :** C (-2.5% en 2022) et G (-3.4%) sont les seuls défensifs prouvés.
A (-49.3%) et B (-43.5%) sont désactivés/réduits au minimum.

Les poids bruts sont normalisés → somme = 1.0, puis modulés par le Circuit Breaker.

---

### Circuit Breaker

```
port_dd = (capital_courant - peak_historique) / peak_historique

Si port_dd < -25% → cb_factor réduit à 0.30 (70% cash)
Si port_dd > -10% → cb_factor récupère +0.5%/cycle

exposition_effective = capital_total × cb_factor
budget_bot = exposition_effective × weight_bot
```

---

### Allocation finale par bot

```
weight_brut  = regime_weight[bot] × quality_score[bot]
quality_score = Sharpe rolling 20 derniers trades (normalisé 0.3-1.5)

weight_norm  = weight_brut / sum(weight_bruts)
budget_eur   = exposition_effective × weight_norm
budget_eur   = min(budget_eur, capital_total × 0.40)  # cap 40% par bot
```

**Exemple en BULL à 10 000€ (cb_factor = 1.0) :**

| Bot | Poids brut | Poids norm | Budget |
|-----|-----------|------------|--------|
| A | 0.8 | 22.9% | 2 290€ |
| B | 1.0 | 28.6% | 2 860€ |
| C | 0.5 | 14.3% | 1 430€ |
| G | 1.2 | 34.2% | 3 420€ |

**Exemple en BEAR à 10 000€ (cb_factor = 1.0) :**

| Bot | Poids brut | Poids norm | Budget |
|-----|-----------|------------|--------|
| A | 0.3 | 10.0% | 1 000€ |
| B | 0.0 | 0.0% | 0€ |
| C | 1.5 | 50.0% | 5 000€ |
| G | 1.2 | 40.0% | 4 000€ |

---

### Contraintes portefeuille

| Limite | Valeur |
|--------|--------|
| Max par bot | 40% du capital |
| Max par actif | 30% du capital |
| Max bots simultanés sur même actif | 2 |
| Cash forcé si VIX > 35 | 30% minimum |

---

### Résultats backtest (référence)

| Métrique | Bot Z Enhanced | Equal-Weight |
|----------|---------------|-------------|
| CAGR (2020-2026) | **+59.8%** | +46.4% |
| Sharpe | **1.61** | 1.20 |
| MaxDD | **-18.9%** | -31.1% |
| 2022 (bear) | **-9.0%** | -16.8% |
| Walk-Forward OOS | **+41.5%** | +33.8% |

*Voir `docs/BACKTEST_RESULTS.md` pour le détail complet des 6 runs.*

---

### Évolution prévue — Bot Z Adaptive

**Version future : Bot Z Adaptive (backtest Run 6 effectué)**

Un meta-switch sélectionne automatiquement le profil de risk management selon le régime :

| Profil | Condition d'activation | Comportement |
|--------|----------------------|-------------|
| **ENHANCED** | Bull propre : BTC+QQQ bull + VIX<22 + DD>-5% + corr<60% | Max CAGR, CB single-tier |
| **BALANCED** | Transition / bull fragile | Compromis, CB 2-tiers, vol 25% |
| **PRO** | Bear/stress : VIX>30 ou corr>70% ou DD<-12% | Max protection, CB 3-tiers, vol 20% |

Hysteresis : délai de confirmation avant switch (ENHANCED→switch 7j / BALANCED 5j / PRO 3j)

**Résultat Run 6 (2020-2026) :** CAGR +29.4%, Sharpe 1.60, MaxDD -11.7%
Distribution : ENHANCED 16% / BALANCED 42% / PRO 42%

**Statut** : seuils PRO trop sensibles (VIX>28) → v2 avec VIX>30 + 2 conditions simultanées prévue.

---

### Bot Z Omega — Portfolio Optimizer (Run 7)

**Architecture différente des autres structures** : au lieu de poids régime fixes, Omega optimise dynamiquement les poids à chaque barre via un moteur ER/Risk.

**Expected Return Engine** par bot (z-score cross-sectionnel) :
- `0.35 × Sharpe_90d` + `0.25 × PF_90d` + `0.20 × equity_slope_60d` + `0.20 × regime_fit`

**Risk Engine** par bot (z-score cross-sectionnel) :
- `0.40 × vol_20d` + `0.30 × downside_vol` + `0.30 × current_dd_abs`

**Score final** = `(ER_score − risk_score) × corr_penalty` → softmax(β=3) → poids

**Résultats Run 7 :** CAGR +55.5% | **Sharpe 1.96** (meilleur de toutes les structures) | **MaxDD -8.7%** | 2022 bear : **+0.2%**

**Statut** : backtest validé, candidat production pour capital modéré (risque-ajusté optimal).

---

### Feuille de route Bot Z

| Phase | Statut | Description |
|-------|--------|-------------|
| ~~Shadow Mode~~ | ✅ Terminé | Observation sans exécution |
| **Paper Trading Enhanced** | 🟢 En cours | 10 000€, démarré 2026-03-06 |
| Revue résultats | 📅 2026-04-30 | Analyse 55 jours de data live |
| Bot Z Omega backtest | ✅ Terminé | Sharpe 1.96, MaxDD -8.7%, 2022 +0.2% |
| Bot J Mean Reversion | ✅ Backtest | MaxDD -1.7%, WinRate 70.8%, corr basse avec trend |
| Bot Z Omega v2 (RP+ML) | ✅ Backtest | Sharpe 2.03 (record), MaxDD -7.6% |
| Bot Z Adaptive v2 | 🔲 Prévu | Seuils PRO ajustés + backtest |
| Live Trading | 🔲 Futur | Après validation ~6 mois paper |

---

## Backtests 6 ans (2020-2026)

**Script** : `backtest/multi_backtest.py`
**Résultats** : `docs/BACKTEST_RESULTS.md`
**Graphique** : `backtest/results/multi_equity.png`
**CSV** : `backtest/results/multi_summary.csv`, `bot_z_comparison.csv`

### Lancer le backtest

```bash
python backtest/multi_backtest.py
```

Durée : ~45s (fetch 16 symboles × 6 ans + 7 bots + 8 structures Bot Z + MC 5000)

### 8 structures Bot Z simulées

| Structure | CAGR | Sharpe | MaxDD | Clé résultats |
|-----------|------|--------|-------|---------------|
| Equal-Weight | +46.4% | 1.20 | -31.1% | Baseline |
| Régime pur | +54.6% | 1.40 | -27.5% | Calibration v2 |
| Hybride 70/30 | +44.2% | 1.30 | -25.3% | Base fixe + overlay |
| **Omega** | **+55.5%** | **1.96** | **-8.7%** | ER+Risk+Corr dynamique |
| **Enhanced** (prod) | **+59.8%** | **1.61** | **-18.9%** | MO + CB single |
| Pro | +29.9% | 1.90 | -9.1% | VT + multi-CB |
| Adaptive | +29.4% | 1.60 | -11.7% | Meta-switch E/B/P |
| **Omega** | +55.5% | **1.96** | **-8.7%** | ER+Risk+Corr+softmax |
| Omega v2 | +26.1% | **2.03** | **-7.6%** | Omega + Risk Parity + Meta-Learning |

### Validations statistiques incluses

- **Walk-Forward** : IS 2020-2022 / OOS 2023-2026 → Bot Z OOS +41.5% (**EDGE RÉEL**)
- **Monte Carlo** : 5000 simulations ordre trades aléatoire → 100% positif tous bots
- **Sharpe corrigé** : calculé sur retours actifs uniquement (|r| > 1e-8)

### Métriques calculées

- **CAGR** : rendement annualisé composé
- **Sharpe** : return / volatilité × √252 sur retours actifs (> 1.5 = excellent)
- **Max DD** : pire drawdown depuis le pic
- **Profit Factor** : gains bruts / pertes brutes (> 1.5 = stratégie rentable)
- **Performance par année** : 2020→2026 (inclut bull 2021, bear 2022, rebond 2023)

### Note méthodologique

- Crypto : Binance depuis 2020 (6 ans) | xStocks : yfinance depuis 2022 (4 ans)
- Frais et slippage appliqués : 0.26% + 0.10% par trade
- Simulation Bot Z : retours quotidiens composés (correct) — pas de ratios cumulés
- Pas de filtre Claude sur Bot A en backtest (assume toujours CONFIRME)

---

## Gestion du risque commune

| Règle | Valeur |
|-------|--------|
| Max drawdown portfolio | -15% → alerte Telegram |
| Frais taker Kraken | 0.26% |
| Slippage estimé | 0.10% |
| Stop loss Bot A | 3×ATR trailing (trend) / 1×ATR (MR) |
| Stop loss Bot B | -12% fixe par position |
| Stop loss Bot C | 2×ATR Turtle N-stop |
| Stop loss Bot D/E/F | 2×ATR trailing |
| Stop loss Bot G | 3×ATR trailing + SMA200 break |
| Stop loss Bot H | 1.5×ATR entrée + 3×ATR trailing |
| Stop loss Bot I | 2.5×ATR trailing + hard -10% + SMA50 break |

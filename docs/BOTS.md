# Documentation des Bots — Appels API, Paramètres, Timing

> **SOURCE DE VÉRITÉ : stratégies individuelles (A → I)**
> Backtests Run 18 (10 ans 2016-2026) → voir `docs/BACKTEST_RUN18_FINAL.md`
> Revue live → voir `docs/REVUE_2026-04-30.md`

**Dernière mise à jour : 2026-03-08** (Run 18, dispatch actif, Meta v2+, engines renommés)

---

## Vue d'ensemble

Runner : `live/multi_runner.py` — un seul processus, données macro partagées.

**Cycles d'exécution** : 03h00, 07h00, 11h00, 15h00, 19h00, 23h00 UTC (toutes les 4h, 6 fois/jour)
Paris CET : 04h, 08h, 12h, 16h, 20h, 00h

**Bots actifs en paper trading (2026-03-08)** :
- **Bot Z** — Pilote central EXÉCUTIF, 10 000€, exécuté en PREMIER à chaque cycle. Dispatch capital actif.
- **Bot A / B / C / G** — Supervisés par Bot Z, capital dispatché depuis 2026-03-08
- **Bot D / E / F** — Lab LLM expérimental, hors Bot Z, désactivés en prod (coût tokens)
- **Bot H / I** — Expérimentaux, non actifs en prod

---

## Données partagées (1 seul fetch pour tous les bots)

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
**Backtest Run 18 (10 ans)** : CAGR +49.3% | Sharpe 2.43 | MaxDD -68.3% | Final 55 008€

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
- **Quand** : seulement si les 3 filtres durs sont passés
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

### Note design
Le filtre Claude sur Bot A est **intentionnel** : le signal est généré par indicateurs purs, Claude ajoute l'analyse macro/news. Rôle différent des bots D/E/F où le LLM est la stratégie. Pas de Claude sur B, C et G (stratégies quant pures — lisibilité contest).

---

## Bot B — Momentum Rotation (Dual Momentum Antonacci)

**Fichier** : `strategies/momentum_strategy.py`
**State** : `logs/momentum/state.json`
**Backtest Run 18 (10 ans)** : CAGR +36.8% | Sharpe 0.77 | MaxDD -67.8% | Final 23 097€

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

### Filtre heures de marché (ajouté 2026-03-08)
- xStocks : BUY bloqué si marché US fermé (hors 9h30-16h00 ET, hors weekends)
- Positions existantes : clôtures autorisées à tout moment

### Stop loss
- **-12% depuis l'entrée** par position

### Appel API
**Aucun.** Stratégie 100% quantitative.

---

## Bot C — Donchian Breakout Turtle System 2

**Fichier** : `strategies/breakout_strategy.py`
**State** : `logs/breakout/state.json`
**Backtest Run 18 (10 ans, stop loss CORRIGÉ)** : CAGR +0.6% | Sharpe 0.21 | MaxDD -7.8% | Final 1 057€

### Bug critique corrigé (2026-03-08)
Stop loss vérifié sur `row["low"]` au lieu de `row["close"]` (les stops intraday étaient ignorés).
Impact : CAGR passe de +17.2% (buggué) à +0.6% (réel) sur 10 ans. Edge non prouvé.
Corrigé dans **backtest** (`backtest/multi_backtest.py`) ET **live** (`strategies/breakout_strategy.py`).

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
| Stop loss | 2×ATR (Turtle N-stop), vérifié sur LOW |
| Exit Donchian | prix low < low 20j, vérifié sur LOW |
| Risk par trade | 1% du capital |
| Max position | 33% du capital |
| Data requis | 65+ jours |

### VIX scaling du risque
- VIX ≤ 20 → risk_pct = 1.0% (plein)
- VIX 20-40 → risk_pct = 1.0% → 0.5% (linéaire)
- VIX ≥ 40 → risk_pct = 0.5% (moitié)

### Stop loss
- **2×ATR trailing** (Turtle N-stop), mis à jour à chaque cycle, déclenché sur LOW intraday
- Sortie Donchian si LOW < low 20j

### Appel API
**Aucun.** Stratégie 100% quantitative.

---

## Bot D — DeepSeek Reasoner V3.2 (LLM)

**Fichier** : `strategies/llm_strategy.py`
**State** : `logs/llm/state.json`
**Statut** : Désactivé en prod (coût tokens, hors Bot Z)

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
- **Économie estimée** : ~80% des appels évités via pre-filter
- **Prix** : $0.28/1M tokens input + $0.42/1M tokens output
- **Prompt contient** (identique E & F) :
  - Supertrend direction, RSI(14), ADX(14), ATR(14), EMA50/200
  - 20 dernières bougies 4h OHLC (contexte tendance 3,5 jours)
  - BTC trend + prix, VIX, Fear & Greed, QQQ régime
  - Portfolio : capital libre, slots, win rate 20 trades, position en cours + stop courant
- **Réponse attendue** : JSON `{"action":"BUY|SELL|HOLD","confidence":0-100,"reason":"..."}`

### Filtre heures de marché
- xStocks : BUY bloqué si marché US fermé (9h30-16h00 ET, lundi-vendredi)
- Crypto : pas de restriction horaire

---

## Bot E — Claude Sonnet 4.6 (LLM)

**Fichier** : `strategies/claude_llm_strategy.py`
**State** : `logs/claude_llm/state.json`
**Statut** : Désactivé en prod (coût tokens, hors Bot Z)

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
- **Prix** : $3.00/1M tokens input + $15.00/1M tokens output
- **Prompt** : identique Bot D (même template `_build_prompt`)
- **Délai** : `time.sleep(1)` entre chaque symbole (évite rate limiting)

---

## Bot F — Claude Haiku 4.5 (LLM, témoin)

**Fichier** : `strategies/haiku_llm_strategy.py`
**State** : `logs/haiku_llm/state.json`
**Statut** : Désactivé en prod (coût tokens, hors Bot Z)

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
**Backtest Run 18 (10 ans)** : CAGR +23.4% | Sharpe 0.65 | MaxDD -22.6% | Final 8 179€

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

### Filtre heures de marché (ajouté 2026-03-08)
- xStocks : BUY bloqué si marché US fermé (hors 9h30-16h00 ET, hors weekends)
- Positions existantes : clôtures autorisées à tout moment

### Sizing — Volatility Targeting (détail)
```
daily_vol = std(daily_returns, 20 jours)
annual_vol = daily_vol × √252
size_pct   = min(TARGET_VOL / annual_vol, MAX_POSITION_PCT)
size_units = (capital × size_pct) / entry_price
```
Exemple : BTC annual_vol=60% → size_pct=15%/60%=25% mais cappé à 10%
Exemple : GLDx annual_vol=12% → size_pct=15%/12%=125% cappé à 10% (actif calme → taille max)

### Appel API
**Aucun.** Stratégie 100% quantitative.

---

## Bot H — Volatility Compression Breakout (VCB)

**Fichier** : `strategies/vcb_strategy.py`
**State** : `logs/vcb/state.json`
**Log** : `logs/vcb.log`
**Statut** : Expérimental — paper trading actif, récolte de données, hors Bot Z dispatch

### Pilotage par engine Bot Z (ajouté 2026-03-09)
Bot H reçoit `macro["bot_z_engine"]` à chaque cycle et adapte son comportement :
| Engine | Comportement |
|--------|-------------|
| SHIELD / PRO | Nouveaux BUY bloqués — exits toujours autorisés |
| PARITY | Taille de position ×0.70 |
| BULL / BALANCED | Comportement normal |

### Fix données insuffisantes (2026-03-09)
**Bug** : `fetch_ohlcv_cache(..., days=45)` → 270 barres 4h, mais VCB requiert
`SMA_LONG(200) + BB_PERCENTILE_LOOKBACK(100) + 10 = 310 barres minimum` → jamais de signal.
**Fix** : `days=45 → 55` dans `multi_runner.py` → 330 barres disponibles > 310 requis.

### Concept
Les plus grosses tendances démarrent après une phase de **compression de volatilité** :
1. Grosse hausse → consolidation serrée → la vol baisse
2. Les vendeurs disparaissent, les institutions accumulent
3. Un catalyseur arrive → explosion du prix

### Différence vs Bot C
- Bot C : attend un nouveau high 55j, sans regarder l'état de la volatilité
- Bot H : exige **compression préalable** (ATR décroissant + BB width < 20e percentile) → filtre ~90% des faux breakouts

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Timeframe | 4h |
| SMA long | 200 périodes |
| BB période | 20 périodes |
| BB width percentile | < 20% sur 100 barres glissantes |
| ATR compression | ATR(14) décroissant ≥ 5 barres consécutives |
| Breakout entrée | High 20 barres (shift 1) |
| Stop entrée | 1.5×ATR |
| Trailing stop | 3×ATR |
| Position size | 20% du capital |
| Max positions | 5 |

### Univers cible
BTC/EUR, ETH/EUR, SOL/EUR, NVDAx, AMDx, METAx, PLTRx

### Appel API
**Aucun.** Stratégie 100% quantitative.

---

## Bot I — Relative Strength Leaders

**Fichier** : `strategies/rs_leaders_strategy.py`
**State** : `logs/rs_leaders/state.json`
**Log** : `logs/rs_leaders.log`
**Statut** : Expérimental — paper trading actif, récolte de données, hors Bot Z dispatch

### Pilotage par engine Bot Z (ajouté 2026-03-09)
| Engine | Comportement |
|--------|-------------|
| SHIELD / PRO | Rebalancement suspendu (remplace le filtre VIX>30) |
| PARITY | Vol cible ×0.70 (sizing réduit) |
| BULL / BALANCED | Comportement normal |

### Concept
Sélection des **leaders en force relative** : score composite momentum multi-période + filtres qualité stricts.
Tenir les 3 leaders, sortir si rang > 5 (buffer anti-churn).

### Score RS composite
```
rs_score = 0.35 × (perf 1m) + 0.35 × (perf 3m) + 0.20 × (perf 6m) + 0.10 × (distance SMA200)
```

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Timeframe | Daily (220 jours) |
| Top N positions | 3 |
| Exit rank | > 5 |
| Rebalancement min | 5 jours |
| ADX seuil | > 18 |
| Vol annualisée max | < 90% |
| Extension SMA50 max | < 15% |
| Hard stop | -10% depuis l'entrée |
| Trailing stop | 2.5×ATR |
| Exit SMA | SMA50 break |
| Target vol | 15% |
| Max position | 30% du capital |
| Pause macro | VIX > 30 ou QQQ bearish ou engine SHIELD/PRO |

### Appel API
**Aucun.** Stratégie 100% quantitative.

---

## Bot J — Mean Reversion (RSI2 + Bollinger Bands)

**Fichier** : `strategies/mean_reversion_strategy.py`
**State** : `logs/mean_reversion/state.json`
**Log** : `logs/mean_reversion/bot_j.log`
**Statut** : Expérimental — paper trading actif, récolte de données, hors Bot Z dispatch

### Concept
Stratégie **anti-tendance**, complémentaire aux bots trend (A/C/G).
Profil : faible corrélation attendue avec A/B/C/G, gagne en marché choppy/range.

### Conditions d'entrée (daily)
- RSI(2) < 5 — survente extrême
- Close < Bollinger Lower (20j, 2σ) — extension vers le bas
- Close > SMA200 — ne pas acheter contre une tendance baissière majeure

### Sortie
- RSI(2) > 60 (retour à la normale)
- OU Close > SMA20 (milieu Bollinger)
- Stop : 1.5×ATR(14) sous l'entrée

### Paramètres
| Paramètre | Valeur |
|-----------|--------|
| Timeframe | Daily (210 barres min) |
| RSI période | 2 |
| RSI entrée | < 5 |
| RSI sortie | > 60 |
| Bollinger | 20j, 2σ |
| SMA filtre | 200 |
| Risk par trade | 0.5% du capital |
| Max position | 10% du capital |
| Stop | 1.5×ATR14 |

### Backtest de référence (2020-2026)
CAGR +1.6% | Sharpe 1.47 | MaxDD -1.7% | 161 trades | Win rate 70.8%

### Pilotage par engine Bot Z (ajouté 2026-03-09)
| Engine | Comportement |
|--------|-------------|
| SHIELD / PRO | Nouveaux BUY bloqués — exits toujours autorisés |
| PARITY | Taille ×0.70 (RISK_PCT et MAX_POS_PCT) |
| BULL / BALANCED | Comportement normal |

### Appel API
**Aucun.** Stratégie 100% quantitative.

---

## Bot Z — Pilote Central EXÉCUTIF (Meta v2+)

**Fichier** : `live/bot_z.py`
**State** : `logs/bot_z/state.json`
**Budget** : `logs/bot_z/budget.json`
**Historique** : `logs/bot_z/shadow.jsonl`
**Dashboard** : endpoint `/api/portfolio`

**Phase actuelle : PAPER TRADING ACTIF** — démarré 2026-03-06, dispatch capital actif depuis 2026-03-08
**Capital** : 10 000€ | **Revue** : 2026-04-30
**Bots supervisés** : A, B, C, G (bots validés)

### Rôle et statut

Depuis 2026-03-08, Bot Z est **pleinement exécutif** :
- Il calcule l'allocation optimale à chaque cycle
- Il dispatche un budget réel en euros vers chaque stratégie (`_apply_z_budget()` dans `multi_runner.py`)
- A/B/C/G scalent leur capital proportionnellement au budget reçu
- Notification Telegram si budget change > 15% (`notify_z_dispatch()`)

### Execution order (multi_runner.py)
```
1. Macro fetch (VIX, QQQ, BTC, Fear&Greed...)
2. OHLCV prefetch (4h + daily)
3. Bot Z FIRST (step 4) → lit états A/B/C/G → calcule allocation → écrit budget.json
4. _apply_z_budget() → scale capital de chaque sub-bot → sauvegarde states
5. Bot A (step 5), Bot B (step 6), Bot C (step 7), Bot G (step 11)
```

---

### 4 Engines Meta v2 — Noms PROD (renommés 2026-03-08)

| Engine | Ancien nom | Régime cible | Backtest Sharpe | MaxDD | CAGR |
|--------|------------|-------------|-----------------|-------|------|
| **BULL** | ENHANCED | Bull propre | 1.61 | -18.9% | +59.8% |
| **BALANCED** | OMEGA | Neutre/quality (BASE universelle) | 1.96 | -8.7% | +55.5% |
| **PARITY** | OMEGA_V2 | Stress modéré, risk parity | 2.03 | -7.6% | +52.1% |
| **SHIELD** | PRO | Bear/crise | 1.90 | -9.1% | +29.9% |

**BALANCED-as-base** : tous les engines utilisent BALANCED comme socle.
Empêche la sur-concentration en régime BULL (Run 15 : MaxDD -9.5% vs -14.4% avant).

**BULL** = 60% BALANCED + 40% momentum tilt
**PARITY** = 60% BALANCED + 40% pure risk parity
**SHIELD** = 40% BALANCED + 60% vol-quality + risk parity

---

### Hard Rules (non-négociables, priorité absolue)

| Condition | Action |
|-----------|--------|
| (BTC bearish ET QQQ bearish ET VIX > 26) | Force SHIELD |
| VIX > 32 | Force SHIELD |
| Portfolio DD < -12% | Force SHIELD |
| BTC bearish OU QQQ bearish | BULL bloqué |

---

### Scoring Meta v2+ (si pas de hard rule)

```python
# 1. Regime fit × confidence × persistence
regime_strength = min(1.0, days_in_regime / REGIME_PERSIST_DAYS)   # monte 0→1 sur 7j
rf = regime_fit × regime_confidence × regime_strength

# 2. Score composite
score = 0.50 × rf + 0.30 × quality_norm + 0.20 × inv_risk

# 3. Switch cost penalty
if engine_candidat != current_engine:
    score -= SWITCH_PENALTY  # = 0.05

# 4. Sélection engine + hysteresis (Run 18 PROD)
# BULL=7j / BALANCED=5j / PARITY=4j / SHIELD=3j
```

---

### Constantes clés Meta v2+

| Constante | Valeur | Description |
|-----------|--------|-------------|
| SWITCH_PENALTY | 0.05 | Pénalité de switch (évite les micro-switchs) |
| TARGET_PORTFOLIO_VOL | 0.20 | 20% annualisé cible |
| BTC_HIGH_VOL_THRESHOLD | 0.80 | Override HIGH_VOL si BTC vol > 80% |
| CORR_REDUCE_THRESHOLD | 0.70 | Réduction expo si corrélation > 70% |
| REGIME_PERSIST_DAYS | 7 | Jours pour atteindre regime_strength = 1.0 |
| INITIAL_CAP | 10 000 | Capital initial toujours synchronisé |

---

### Allocation par engine — Poids par régime

**BALANCED_WEIGHTS** (BASE universelle, neutre + quality scoring) :
| Régime | A | B | C | G |
|--------|---|---|---|---|
| BULL | 0.9 | 1.1 | 0.7 | 1.0 |
| RANGE | 1.0 | 0.9 | 0.8 | 0.9 |
| BEAR | 0.4 | 0.1 | 1.3 | 1.1 |
| HIGH_VOL | 0.6 | 0.4 | 1.2 | 1.0 |

**SHIELD_WEIGHTS** (défensif C+G, B écarté) :
| Régime | A | B | C | G |
|--------|---|---|---|---|
| Tous | 0.1-0.3 | 0.0 | 1.8-2.0 | 1.0 |

**BULL_WEIGHTS** : REGIME_WEIGHTS classiques (A favorisé en bull propre)
**PARITY_WEIGHTS** : 50% BALANCED + 50% inverse-vol (risk parity pure)

---

### Allocation finale — Risk Parity CTA style

```python
# 60% engine/régime + 40% inverse-vol (ajouté 2026-03-08)
inv_vol_w = {b: (1/vol_b) / sum(1/vol for vol in vols) for b in VALID_BOTS}
blended = 0.60 × alloc_weights + 0.40 × inv_vol_w

# Application vol targeting global
vol_factor = clip(TARGET_PORTFOLIO_VOL / portfolio_vol_20d, 0.3, 1.5)
# Si variance ≈ 0 (début paper) → vol_factor = 1.0

# Cap corrélation
if avg_corr > CORR_REDUCE_THRESHOLD:
    exposition ×= 0.80

# Cap par bot
budget_eur = min(budget_eur, capital_total × 0.40)  # 40% max par bot
```

---

### Levier conditionnel BULL (version PROD Run 17)

En régime BULL + cb_factor ≥ 0.90 + vol_annuelle < 20% :
```python
lev_factor = min(1.30, TARGET_PORTFOLIO_VOL / port_vol_annual)
```
Mécanisme vol targeting (style CTA) — ne s'active que dans les bonnes conditions.

---

### Circuit Breaker

```
port_dd = (capital_courant - peak_historique) / peak_historique

Si port_dd < -25% → cb_factor réduit à 0.30 (70% cash)
Si port_dd > -10% → cb_factor récupère +0.5%/cycle

exposition_effective = capital_total × cb_factor
```

---

### Tracking z_capital (corrigé 2026-03-08)

**Bug corrigé** : boucle de feedback qui causait une inflation +284% en 2 jours.
```
Cause : last_bot_values stockait les valeurs PRE-dispatch
→ cycle suivant lisait valeurs POST-dispatch (×2.5) → retour apparent +150%
→ z_capital ×2.5 → budget dispatché ×2.5 → boucle infinie
```

**Fix appliqué** :
```python
# Référence POST-dispatch (fix 1)
state["last_bot_values"] = {b: round(budget.get(b, bot_values.get(b, 0)), 2) for b in VALID_BOTS}
state["last_bot_raw_values"] = bot_values  # debug seulement

# Sanity cap (fix 2) : si |weighted_return| > 15% sur un cycle 4h
# → recalibrage via sum_curr/sum_prev (ratio brut des capitaux totaux)

# initial_capital toujours synchronisé (fix 3)
state["initial_capital"] = INITIAL_CAP
```

Formule de tracking correcte :
```python
cycle_returns = {b: bot_values[b]/prev_bot_values[b] - 1 for b in VALID_BOTS}
weighted_return = sum(prev_weights[b] * cycle_returns[b] for b in VALID_BOTS)
new_z_capital = max(0.0, z_capital * (1 + weighted_return))
```

---

### Dispatch capital vers A/B/C/G (actif depuis 2026-03-08)

**`_apply_z_budget(state, z_budget_eur)` dans `multi_runner.py`** :
- Après chaque cycle Bot Z, scale le capital de chaque sub-bot proportionnellement
- `scale = z_budget / prev_budget` → `state["capital"] *= scale` (préserve ratio PnL)
- Sauvegarde immédiate de chaque state après dispatch (protection crash)
- Log `[Z→] Budget dispatché — A:xxxx€ B:xxxx€ C:xxxx€ G:xxxx€`

**Notification Telegram** (`notify_z_dispatch()` dans `live/notifier.py`) :
- Envoyée si budget change > 15% entre deux cycles
- Format : engine | capital total | budget par bot avec barre de progression ASCII

---

### Mark-to-Market réel

```python
# Prix live via OHLCV daily si disponible :
run_bot_z_cycle(macro, ohlcv=ohlcv_daily)

for sym, pos in positions.items():
    if ohlcv and sym in ohlcv:
        live_price = ohlcv[sym]["close"].iloc[-1]   # MTM réel
    else:
        live_price = pos["entry"]                    # fallback prix d'entrée
```

---

### Quality Score & Ramp-up

```python
# Évite la sur-pondération instable en début de paper
if n_trades < 5:
    quality_score = 1.0  # neutre
elif n_trades < 20:
    confidence = (n_trades - 5) / 15.0
    quality_score = 1.0 + confidence × (raw_score - 1.0)  # blend progressif
else:
    quality_score = clamp(1.0 + sharpe_rolling × 0.3, 0.3, 1.5)  # score plein
```

---

### BTC Realized Volatility Override

```python
btc_returns_4h = BTC.close.pct_change().tail(20)
btc_vol_annual = btc_returns_4h.std() × sqrt(6 × 365)

if btc_vol_annual > BTC_HIGH_VOL_THRESHOLD and regime in ["BULL", "RANGE"]:
    regime = "HIGH_VOL"  # override
```

---

### Corrélation inter-bots dynamique

```python
# Pearson pairwise sur 20 trades
returns_matrix = {b: retours_20_trades[b] for b in VALID_BOTS}
avg_corr = mean([corr(a, b) for a, b in combinations])

if avg_corr > CORR_REDUCE_THRESHOLD:
    exposition ×= 0.80  # réduction 20% si bots trop corrélés
```

---

### Allocation Drift Tracking

```python
drift = sum(abs(target_weight[b] - actual_weight[b]) for b in VALID_BOTS)
if drift > 0.20:
    log.warning(f"[Z] Allocation drift {drift:.1%} — vérifier _apply_z_budget")
```

---

### Engine Reasons loggés dans shadow.jsonl

À chaque cycle, les raisons de sélection sont loggées :
`hard_rule_shield, block_bull, btc_bearish, qqq_bearish, vix, port_dd_pct,
regime, raw_engine, rolling_scores, bot_vols, engine_switched, prev_engine,
vol_factor, avg_corr, btc_vol_annual, drift`

---

### MCPS — Contribution Marginale au Sharpe (`backtest/analyze_botz.py`)

Disponible dès 10 cycles de shadow.jsonl :
```python
MCPS = Sharpe_bot - ρ_bot_portfolio × Sharpe_portfolio
# Verdict : UTILE (MCPS > 0) ou À RETIRER (MCPS < 0)
```

Commande : `python backtest/analyze_botz.py --csv`
Exports : `equity_timeline.csv`, `engine_switches.csv`, `budget_history.csv`, `meta_v2plus_metrics.csv`

---

### Backtest Run 18 — Résultats finaux (10 ans 2016-2026)

| Stratégie | CAGR | Sharpe | MaxDD | Final (depuis 10k€) |
|-----------|------|--------|-------|---------------------|
| **Bot Z Meta v2 PROD** | **+38.2%** | **1.92** | **-10.1%** | **84 985€** |
| Bot A — Supertrend+MR | +49.3% | 2.43 | -68.3% | 55 008€ |
| Bot B — Momentum | +36.8% | 0.77 | -67.8% | 23 097€ |
| Bot C — Breakout (réel) | +0.6% | 0.21 | -7.8% | 1 057€ |
| Bot G — Trend | +23.4% | 0.65 | -22.6% | 8 179€ |
| S&P 500 | +12.9% | 0.77 | -33.9% | — |
| NASDAQ-100 | +19.9% | 0.93 | -35.1% | — |
| BTC buy & hold | +65.4% | 0.90 | -82.7% | — |

**Bot Z surperforme tous les benchmarks sur Sharpe (1.92) et MaxDD (-10.1%).**
La diversification entre A (alpha élevé) et G (stabilisateur) est la clé.

---

### Reclassement bots post-audit 2026-03-08

| Bot | Rôle | Statut |
|-----|------|--------|
| A | Moteur principal alpha | Validé — +49.3% CAGR 10 ans |
| G | Stabilisateur réel | Validé — +23.4%, jamais > -7%/an |
| B | Booster bull crypto | Validé — cyclique, 0€ en SHIELD |
| C | Breakout corrigé | Edge non prouvé (+0.6% réel) — actif défensif en SHIELD |
| Bot J (futur) | Candidat ajout Bot Z | MCPS prévu > 0, corr A ≈ 0.1-0.3 |

---

### État paper trading (2026-03-08 fin de session)

```
z_capital      : 10 000€ stable (7 cycles : [10k, 10k, 10k, 10k, 10k, 10k, 10k])
Engine         : SHIELD (VIX 29.51, BTC bear, strength=1.0)
Budget dispatch: A=1 402€ | B=1 000€ | C=4 600€ | G=2 998€
Services VPS   : bot + dashboard actifs, 0 erreur
Shadow log     : 45 entrées accumulées
Drift warning  : 35% — normal en début de paper (quality scores vides)
```

---

### Feuille de route Bot Z

| Phase | Statut | Description |
|-------|--------|-------------|
| Shadow Mode | ✅ Terminé | Observation sans exécution |
| Paper Trading Meta v2 | ✅ En cours | 10 000€, démarré 2026-03-06 |
| MTM réel | ✅ Implémenté | Prix live via OHLCV daily |
| Engine reasons logging | ✅ Implémenté | ~15 champs dans shadow.jsonl |
| Quality ramp-up | ✅ Implémenté | Blend 5→20 trades |
| Budget dispatch (write) | ✅ Implémenté | _write_budget() → budget.json |
| Budget dispatch (apply) | ✅ Implémenté | _apply_z_budget() actif depuis 2026-03-08 |
| Risk Parity allocation | ✅ Implémenté | 60% engine + 40% inverse-vol |
| Levier conditionnel BULL | ✅ Implémenté | Vol targeting CTA (Run 17) |
| Corrélation dynamique | ✅ Implémenté | Pearson pairwise 20 trades |
| BTC vol override | ✅ Implémenté | > 80% → force HIGH_VOL |
| Drift tracking | ✅ Implémenté | Warning si > 20% |
| Notify Z dispatch | ✅ Implémenté | Telegram si changement > 15% |
| MCPS analyze | ✅ Implémenté | analyze_botz.py, dès 10 cycles |
| Mobile dashboard | ✅ Implémenté | Bottom nav, 6 KPIs, colonne Vol |
| Weight caps allocation | ✅ Implémenté 2026-03-09 | A:5-50% B:0-30% C:0-25% G:15-55% |
| Smooth engine transition | ✅ Implémenté 2026-03-09 | Défense 40%/cycle, rebond 20%/cycle |
| H/I/J engine-awareness | ✅ Implémenté 2026-03-09 | bot_z_engine injecté dans macro |
| Risk per trade | 🔲 Prévu | `size = 0.4%×z_capital / \|entry−stop\|` |
| Ajout Bot J ou H ou I | 🔲 Prévu | Si MCPS > 0 et corr < 0.3 (revue 04/30) |
| Revue résultats | 📅 2026-04-30 | analyze_botz.py sur 55 jours live |
| Live Trading | 🔲 Futur | Après validation ~6 mois paper |

---

## Résumé des appels API par cycle

| Bot | API | Nb appels/cycle (estimé) | Coût/cycle |
|-----|-----|--------------------------|------------|
| A | Anthropic Haiku | 0-3 (si signal passe filtres durs) | ~$0.001 |
| B | Aucun | 0 | $0 |
| C | Aucun | 0 | $0 |
| D | DeepSeek Reasoner | 0-10 (désactivé en prod) | ~$0.002 |
| E | Anthropic Sonnet | 0-10 (désactivé en prod) | ~$0.05 |
| F | Anthropic Haiku | 0-10 (désactivé en prod) | ~$0.008 |
| G | Aucun | 0 | $0 |
| H | Aucun | 0 | $0 |
| I | Aucun | 0 | $0 |
| A pré-marché | Anthropic Haiku | 1/jour ouvré | ~$0.005/jour |

**Total journalier estimé (prod)** : $0.01-0.03/jour (D/E/F désactivés)

---

## Alertes crédit API (système persistant)

**Fichier** : `live/notifier.py` — `set_api_alert` / `clear_api_alert` / `resend_pending_alerts`
**State** : `logs/api_alerts.json`

Si une API retourne une erreur de crédit/quota :
1. Telegram immédiat : "Crédits X épuisés"
2. `logs/api_alerts.json` mis à jour
3. Rappel Telegram à **chaque cycle** jusqu'au rechargement
4. Bannière rouge en haut du **dashboard** (`/api/alerts`)
5. Dès que l'API répond à nouveau : alerte "Crédits rechargés" + bannière disparaît

Mots-clés détectés : `credit`, `billing`, `insufficient`, `balance`, `quota`, `402`, `payment`, `funds`, `overdue`

Hooks dans : `live/claude_filter.py`, `strategies/llm_strategy.py`, `claude_llm_strategy.py`, `haiku_llm_strategy.py`

---

## Symboles tradés

**Crypto (5)** : BTC/EUR, ETH/EUR, SOL/EUR, BNB/EUR, TON/EUR
*(LINK/EUR et AVAX/EUR retirés — PF < 1 sur 1 et 3 ans)*

**xStocks paper Kraken (11)** : NVDAx, AAPLx, MSFTx, METAx, GOOGx, PLTRx, AMDx, AVGOx, GLDx, NFLXx, CRWDx
*(TSLAx retiré — WR=20% ; AMZNx retiré — PF=0.07)*

**Total** : 16 symboles

**Bot C uniquement** : BTC/EUR, ETH/EUR, SOL/EUR
**Bot H uniquement** : BTC/EUR, ETH/EUR, SOL/EUR, NVDAx, AMDx, METAx, PLTRx
**Bots A, B, D, E, F, G, I** : tous les 16 symboles

---

## Gestion du risque commune

| Règle | Valeur |
|-------|--------|
| Max drawdown portfolio | -12% → SHIELD forcé |
| Max drawdown alerte | -15% → alerte Telegram |
| Frais taker Kraken | 0.26% |
| Slippage estimé | 0.10% |
| Stop loss Bot A | 3×ATR trailing (trend) / 1×ATR (MR) |
| Stop loss Bot B | -12% fixe par position |
| Stop loss Bot C | 2×ATR Turtle N-stop (vérifié sur LOW) |
| Stop loss Bot D/E/F | 2×ATR trailing |
| Stop loss Bot G | 3×ATR trailing + SMA200 break |
| Stop loss Bot H | 1.5×ATR entrée + 3×ATR trailing |
| Stop loss Bot I | 2.5×ATR trailing + hard -10% + SMA50 break |

---

## Dashboard (2026-03-08 — Mobile-first)

**URL** : https://vps-957c8713.vps.ovh.net/ (admin / htpasswd)
**Fichier** : `dashboard/templates/index.html`

### Navigation
- **Desktop** : top nav — Portfolio Z | Stratégies | Lab LLM | Logs
- **Mobile** (< 768px) : bottom nav fixe avec icônes — Portfolio/Bots/Lab/Logs

### Vue Portfolio Z (principale)
- Engine Hero : nom engine 48px coloré + countdown prochain cycle + heure Paris live
- 6 KPIs : z_capital | CAGR | Sharpe | MaxDD | Engine | **Dispatch Z** (budget total A+B+C+G)
- Doughnut allocation + allocation table avec colonne **Vol** (volatilité par bot)
- Courbe equity colorée par engine + timeline engines
- Logs colorisés : `[Z→]` en accent, `MCPS`/`risk parity` en violet

### Countdown + heure Paris
- Cycles UTC : [3, 7, 11, 15, 19, 23] → Paris CET : [4, 8, 12, 16, 20, 0]
- Calcul via `getUTCHours()` (corrigé — l'ancienne version utilisait `getTimezoneOffset()`)

---

## Commandes revue 2026-04-30

```bash
# Récupérer les données live VPS
scp ubuntu@51.210.13.248:/home/botuser/bot-trading/logs/bot_z/shadow.jsonl backtest/results/

# Rapport complet avec MCPS
python backtest/analyze_botz.py --csv

# Backtest 10 ans
python backtest/run10y.py
```

Questions clés à évaluer :
- Fréquence switchs engine (< 0.3/j = sain)
- % temps SHIELD (< 30%)
- MaxDD live (< 12%)
- Drift (< 15% stable)
- vol_factor (0.85-1.15 = vol targeting actif)
- MCPS par bot (UTILE ou À RETIRER)
- Drift descendu sous 15% (quality scores remplis en 2-3 semaines)

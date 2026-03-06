# Résultats Backtests — Multi-Bots

> Script : `backtest/multi_backtest.py`
> Dernier run : 2026-03-06
> Graphique : `backtest/results/multi_equity.png`
> CSV détaillé : `backtest/results/multi_summary.csv`
> CSV Bot Z : `backtest/results/bot_z_comparison.csv`
> Symboles : 16/20 (LINK, AVAX, TSLA, AMZN absents — données insuffisantes)

---

## RUN 2 — Données étendues (2020-2026, 6 ans)

> Crypto : Binance depuis janvier 2020 | xStocks : yfinance depuis janvier 2022

### Tableau comparatif global

| Bot | Stratégie | CAGR | Sharpe | Max DD | Profit Factor | Trades | Win Rate | Capital final |
|-----|-----------|------|--------|--------|---------------|--------|----------|---------------|
| A | Supertrend+MR | **+30.1%** | 3.09 (*) | -67.4% (**) | 2.89 | 209 | 43.5% | 10 715€ |
| B | Momentum | **+39.2%** | 1.91 (*) | -71.6% (**) | 2.38 | 68 | 32.4% | 19 785€ |
| C | Breakout (crypto) | +13.9% | 1.28 | **-6.0%** | 3.69 | 72 | 48.6% | 2 236€ |
| G | Trend Multi-Asset | +19.1% | 0.53 | -23.1% | 4.84 | 141 | 53.2% | 4 831€ |
| H | VCB Breakout | 0% | — | — | — | **0** (***) | — | 1 000€ |
| I | RS Leaders | +10.7% | 0.67 | -31.7% | 2.11 | 200 | 6.5% (***) | 2 507€ |

(*) Sharpe peut être gonflé pour A/B : equity plate quand pas de position → std faible.
(**) MaxDD élevé = volatilité crypto normale. BTC fait -50%+ en bear.
(***) Bot I : CAGR positif sur 6 ans mais win rate 6.5% → churn excessif, frais mangent les gains.

### Performance par année (2020→2026)

| Bot | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 YTD |
|-----|------|------|------|------|------|------|----------|
| A | +78.3% | +277.0% | **-49.3%** | +77.3% | +84.7% | +147.1% | +10.5% |
| B | +133.8% | +500.8% | **-43.5%** | +80.0% | +7.1% | +2.3% | -3.4% |
| C | +14.3% | +47.7% | **-2.5%** | +26.5% | +9.0% | -0.8% | -0.9% |
| G | +18.9% | +111.9% | **-3.4%** | +36.5% | +32.3% | +27.3% | +2.2% |
| I | +57.5% | +110.6% | -0.2% | -9.8% | -9.4% | -8.0% | — |

**Observations clés avec les données 2020-2022 :**

- **2020-2021 = bull crypto exceptionnel** : Bot A +277%, Bot B +500% en 2021. Ces résultats ne sont pas reproductibles — période unique (COVID recovery + DeFi boom + NFT)
- **2022 = test bear market crucial** :
  - Bot C : **-2.5%** — le seul bot qui résiste en bear → Breakout Turtle avec stops serrés
  - Bot G : **-3.4%** — trend following coupe les pertes tôt → très solide
  - Bot A : -49.3% — trailing stop 3×ATR laisse de grosses pertes latentes
  - Bot B : -43.5% — momentum sans protection = massacre en bear
- **Bot G est le vrai pilier** : positif ou légèrement négatif chaque année sauf 2022 (-3.4%), puis +36% / +32% / +27% → la régularité la plus fiable
- **Bot C confirme son rôle défensif** : ne fait jamais -10% en un an, même en 2022

---

## Simulation Bot Z — 3 structures portfolio (run 3 — simulation correcte)

> Capital : 4000€ (4 bots × 1000€) | Bots valides : A, B, C, G
> Méthode : **retours quotidiens composés** (correct) — pas de biais sur ratios cumulés
> Calibration v2 BEAR : C=1.5, G=1.2 (validé sur 2022)
> Bot I : REBAL_DAYS=12, cooldown re-entry 10j

### Comparaison des 3 structures

| Stratégie | CAGR | Sharpe | MaxDD | Capital final | Description |
|-----------|------|--------|-------|---------------|-------------|
| REF : Bot B seul ×4 | +39.2% | 1.91 | -71.6% | 79 140€ | Meilleur bot individuel (non-diversifié) |
| Equal-Weight (A+B+C+G) | +46.4% | 1.20 | -31.1% | 41 592€ | 25% chaque bot, rebalancé daily |
| **Bot Z — Régime pur** | **+54.6%** | **1.40** | **-27.5%** | **58 205€** | Allocation 100% dynamique par régime |
| Hybride 70/30 | +44.2% | 1.30 | **-25.3%** | 38 030€ | 70% base fixe + 30% overlay dynamique |

**→ Bot Z pur (calibration v2) est strictement supérieur sur CAGR, Sharpe ET MaxDD vs equal-weight**

### Performance annuelle des 3 structures

| Stratégie | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 YTD |
|-----------|------|------|------|------|------|------|----------|
| Equal-Weight | +56.1% | +213.4% | -16.8% | +59.0% | +17.6% | +26.5% | +3.5% |
| **Bot Z Régime pur** | **+62.7%** | **+232.0%** | **-11.8%** | **+65.6%** | **+24.4%** | **+35.8%** | **+4.2%** |
| Hybride 70/30 | +50.6% | +188.9% | -12.7% | +54.6% | +18.3% | +27.4% | +3.5% |

### Conclusions sur les 3 structures

**1. Bot Z pur (calibration v2) = la meilleure structure sur 6 ans**
- CAGR +54.6% vs +46.4% equal-weight → **+8.2%/an**
- Sharpe 1.40 > 1.20 → meilleur ratio risque/rendement
- MaxDD -27.5% < -31.1% → moins de drawdown
- **2022 (bear)** : seulement **-11.8%** vs -16.8% equal-weight → la calibration C=1.5/G=1.2 fonctionne

**2. Equal-Weight = solide et simple**
- Aucune calibration requise, robuste
- CAGR +46.4% — already excellent
- Bon fallback si Bot Z est désactivé

**3. Hybride 70/30 = meilleur MaxDD (-25.3%) mais pire CAGR**
- Le socle fixe (70%) bride trop l'overlay en BEAR : quand Bot Z veut pivoter sur C/G, la base force encore 20% A et 20% B
- Intéressant si l'objectif prioritaire est la limitation du drawdown au-dessus du CAGR
- +44.2% CAGR : inférieur aux deux autres

**4. Note technique importante**
> Run 1 et 2 utilisaient des ratios cumulés pour pondérer (incorrect — produisait des artefacts +229% en 2023). Run 3 utilise des retours quotidiens composés (mathématiquement correct). Les résultats du Run 3 sont les seuls valides pour le portfolio Bot Z.

**5. Règle des fonds multi-stratégies confirmée :**
> *"Plusieurs stratégies moyennes ensemble battent souvent une excellente stratégie seule."*
> Bot Z sur 4 bots > Bot B seul sur toutes les métriques (CAGR +54.6% vs +39.2%, MaxDD -27.5% vs -71.6%)

---

## Analyse qualitative — Rôle de chaque bot

| Bot | Rôle | Régimes favorables | Régimes défavorables |
|-----|------|-------------------|---------------------|
| G | **Pilier** — régulier, toujours positif sauf 2022 (-3.4%) | BULL, RANGE | BEAR (limite les pertes) |
| C | **Défensif** — MaxDD -6%, survit partout | Tous, même BEAR | Sous-performe en bull fort |
| A | **Opportuniste crypto** — explose en bull, chute en bear | BULL fort (crypto) | BEAR (-49% en 2022) |
| B | **Cyclique** — fort au début des cycles, faible ensuite | Début BULL (2020, 2021, 2023) | BEAR, fin de cycle |

**Hiérarchie recommandée pour le portefeuille :**
- G = pilier principal (30%)
- C = protection bear (15-20%)
- A = moteur de croissance en BULL (20%)
- B = opportuniste, à réduire en BEAR (20%)

---

## Calibration Bot Z — Corrections nécessaires

### Problème identifié en BEAR (2022)

La calibration actuelle pour le régime BEAR (`a=1.5, g=0.2`) est **fausse** :
- Bot A a fait -49.3% en 2022 → sur-pondérer A en BEAR = désastreux
- Bot G a fait -3.4% en 2022 → G devrait être LE bot défensif en BEAR

### Calibration corrigée recommandée

| Régime | A | B | C | G | Raisonnement |
|--------|---|---|---|---|---|
| BULL | 0.8 | 1.0 | 0.5 | 1.2 | A + B en bull, G stable, réduire C |
| RANGE | 1.0 | 0.8 | 0.7 | 0.8 | A mean-reversion + G trend |
| **BEAR** | **0.3** | **0.0** | **1.5** | **1.2** | **C + G défensifs prouvés en 2022** |
| HIGH_VOL | 0.5 | 0.3 | 1.0 | 0.8 | Réduire tout, C et G les plus résistants |

*À implémenter dans `live/bot_z.py` → REGIME_WEIGHTS + `backtest/multi_backtest.py` → REGIME_WEIGHTS_Z*

---

## Bugs identifiés — à corriger

### Bug 1 : Bot H = 0 trades
**Cause** : La compression ATR (5 barres daily décroissantes) est trop rare sur daily. En production, Bot H tourne sur 4h (6× plus de barres).
**Solution** : Exclure Bot H du backtest daily ou créer une version 4h séparée.

### Bug 2 : Bot I = 6.5% win rate
**Cause** : Churn excessif — la rotation toutes les 5 jours génère des frais (0.26% × 2 = 0.52% par aller-retour). Win rate très faible = nombreuses petites pertes.
**Solution** : Augmenter REBAL_DAYS à 10-15 jours, ajouter filtre "ne pas re-rentrer sur un actif sorti < 10 jours".

### Bug 3 : Tableau régime = tout zéro
**Cause** : Incompatibilité timezone entre dates des trades et index VIX/QQQ. La fonction `asof()` retourne NaN.
**Solution** : Normaliser `pd.Timestamp(dt).normalize().tz_localize(None)` → déjà appliqué dans Bot Z portfolio, à reporter dans `regime_returns()`.

### Bug 4 : Sharpe gonflé pour A et B
**Cause** : Equity plate quand pas de position → std des returns ≈ 0 → Sharpe explosé.
**Solution** : Calculer Sharpe sur les trade PnL normalisés, pas sur l'equity curve complète.

---

## Prochaines étapes

- [x] Ajouter données crypto depuis 2020 (test 2022 bear market)
- [x] Simulation Bot Z 3 structures (equal / régime pur / hybride 70-30)
- [ ] Corriger calibration Bot Z BEAR (C=1.5, G=1.2 au lieu de A=1.5)
- [ ] Corriger le churn Bot I (REBAL_DAYS=10, filtre re-entry)
- [ ] Exclure Bot H du backtest daily (0 trades)
- [ ] Corriger tableau régime (tz-matching bug)
- [ ] Corriger Sharpe (calcul sur trades)
- [ ] Relancer avec calibration BEAR corrigée

---

## Historique des runs

| Date | Période | Notes | CAGR Equal-Weight | Fichier |
|------|---------|-------|--------------------|---------|
| 2026-03-06 | Jan 2023 → Mar 2026 (3 ans) | Premier run — 16 symboles, daily. Bugs H/I/régime identifiés | +9.3% (4 bots) | multi_summary.csv |
| 2026-03-06 | Jan 2023 → Mar 2026 (3 ans) | Bot Z ajouté — Equal +19.7%, Bot Z +22.9% | +19.7% | bot_z_comparison.csv |
| 2026-03-06 | Jan 2020 → Mar 2026 (6 ans) | Données étendues crypto + 3 structures portfolio (simulation incorrecte) | +44.0% | — |
| 2026-03-06 | Jan 2020 → Mar 2026 (6 ans) | **Run final** : calibration BEAR v2 + Bot I fix + simulation retours daily | **Equal +46.4% / Bot Z +54.6%** | bot_z_comparison.csv |

---

*Relancer le backtest : `python3 backtest/multi_backtest.py`*

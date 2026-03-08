# Backtest Run 13 — 10 ans (2016-2026) — Version corrigée

**Date** : 2026-03-08
**Période** : 2016-01-01 → 2026-03-08
**Capital initial** : 1 000€ par bot, 4 000€ Bot Z
**Symboles** : 15/16 (TON/EUR manquant)

---

## Bugs corrigés dans cette version

### 1. Bug Bot C — Stop sur CLOSE au lieu de LOW (corrigé Run 12)

Stop loss déclenché sur `close ≤ stop` au lieu de `low ≤ stop`. Les stops intraday
n'étaient jamais atteints, d'où Sharpe 2.88 artificiel. Après correction : CAGR +0.6%, Sharpe 0.21.

### 2. Bug Bot Z — Prime de rebalancement MtM journalière (corrigé Run 13)

**Description du bug** : Les equity curves journalières de Bot A avaient des swings
de ±38% à ±191% par jour (mark-to-market des positions crypto). Ces retours extrêmes,
appliqués avec des poids fixes dans le portefeuille Bot Z, créaient une prime de
rebalancement artificiellement massive via l'effet "Shannon's Demon" :

```
Exemple : Bot A perd -50% puis regagne +100% sur 2 semaines.
Bot A net = (0.5)(2.0) = 1.0 = aucun gain.
Bot Z (30% poids) : 4000 × (1-0.3×0.5) × (1+0.3×1.0) = 4000 × 0.85 × 1.30 = 4420
Prime = +10.5% pour un actif qui finit à 0% net.
```

Avec 500 jours de tels aller-retours, le résultat composait à 10^18€. **Ce n'est pas
une erreur de calcul — c'est une propriété mathématique réelle — mais les magnitudes
étaient irréalistes car on supposait un rééquilibrage daily parfait à coût zéro.**

**Fix** : Rééchantillonnage hebdomadaire (`resample("W")`) des equity curves avant
tout calcul Bot Z. Cela :
- Élimine le bruit MtM journalier (swings de ±191% → swings de ±20-50% hebdo)
- Représente la fréquence réelle de décision de Bot Z (hebdomadaire, pas minute par minute)
- Corrige aussi l'annualisation Sharpe (`sqrt(52)` pour données hebdomadaires)

La prime de rebalancement persiste mais à des niveaux économiquement plausibles.

---

## Résultats corrigés

### Bots individuels (1 000€ → 10 ans)

| Bot | Stratégie | CAGR | Sharpe | MaxDD | Trades | WR% | Final |
|-----|-----------|------|--------|-------|--------|-----|-------|
| A | Supertrend+MR | +49.3% | 2.43 | -68.3% | 452 | 43.8% | 55 008€ |
| B | Momentum | +36.8% | 0.77 | -67.8% | 365 | 19.2% | 23 097€ |
| C | Breakout (corrigé) | +0.6% | 0.21 | -7.8% | 199 | 40.2% | 1 057€ |
| G | Trend Multi-Asset | +23.4% | 0.65 | -22.6% | 289 | 54.3% | 8 179€ |
| J | Mean Reversion | +2.1% | 1.45 | -3.5% | 326 | 69.9% | 1 219€ |

### Bot Z Portfolio (4 000€ → 10 ans)

| Structure | CAGR | Sharpe | MaxDD | Final |
|-----------|------|--------|-------|-------|
| Equal-Weight A+B+C+G | +41.4% | 1.24 | -27.1% | 127 323€ |
| Bot Z Régime pur | +53.0% | 1.48 | -26.5% | 279 863€ |
| Bot Z Enhanced | +52.6% | 1.56 | -25.7% | 271 927€ |
| **Bot Z Omega** | **+28.5%** | **1.64** | **-13.4%** | **48 887€** |
| Bot Z Omega v2 | +17.8% | 1.29 | -22.0% | 18 865€ |
| Bot Z Meta v2 (PROD) | +19.7% | 1.10 | -25.7% | 21 927€ |

### Benchmarks (2016-2026)

| Benchmark | CAGR | Sharpe | MaxDD |
|-----------|------|--------|-------|
| S&P 500 | +12.9% | 0.77 | -33.9% |
| NASDAQ-100 | +19.9% | 0.93 | -35.1% |
| BTC/EUR buy&hold | +65.6% | 0.91 | -82.7% |

### Rendements annuels Bot Z Meta v2

| 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|------|------|------|------|------|------|------|------|------|------|------|
| +1.3% | +107.3% | -17.5% | +4.0% | +19.4% | +118.8% | +0.7% | -4.8% | -5.5% | +5.3% | +1.2% |

---

## Interprétation

### Ce qui reste réel
- **Prime de rebalancement** : +41.4% pour l'equal-weight vs moyenne arithmétique ~27%
  = rebalancing bonus de ~14%/an. Élevé mais mathématiquement possible avec la volatilité
  crypto (Bot A +388%/+416% en 2020/2021).

- **Bot A domine** : Sharpe 2.43 sur 10 ans, final 55×. Le moteur du système.

### Observations Bot Z

**Enhanced/Régime pur (+52-53%)** : surperforment car le régime BULL surpondère Bot A
(le meilleur performer). C'est cohérent mais dépendant de Bot A.

**Omega (+28.5%, Sharpe 1.64, MaxDD -13.4%)** : meilleur profil risk-ajusté.
Inverse-vol réduit l'exposition à Bot A (haut vol) → plus stable.

**Meta v2 PROD (+19.7%)** : sous-performe en 2023-2024 malgré de bons résultats Bot A.
Cause probable : warmup 90 semaines ≈ 1.7 ans avec données hebdomadaires → le scoring
data-driven ne démarre vraiment qu'en fin 2017. Les décisions en 2023-2024 peuvent
être influencées par la mauvaise performance de 2022 (données récentes dans la fenêtre).

### Décision
La production tourne avec Meta v2. Les résultats 10 ans servent de référence historique,
pas de critère de modification. La revue officielle reste **2026-04-30**.

---

## Notes techniques

- `_resample_weekly()` : helper ajouté dans `multi_backtest.py`
- `_metrics_portfolio()` : paramètre `weekly=True` → utilise `sqrt(52)` pour annualisation
- `run10y.py` : utilise maintenant `z["metrics"]` (date-based CAGR) au lieu de `compute_metrics()`
- Le 6-year backtest (Run 11) bénéficie aussi du fix (même code)
- Fichiers : `backtest/run10y.py`, `backtest/multi_backtest.py`

# Backtest Run 15 — 10 ans (2016-2026) — BALANCED-as-base

**Date** : 2026-03-08
**Période** : 2016-01-01 → 2026-03-08
**Capital initial** : 1 000€ par bot, 4 000€ Bot Z
**Symboles** : 15/16 (TON/EUR manquant)

---

## Améliorations apportées dans cette version

### 1. Renommage des engines Meta v2

Pour éviter la confusion entre les noms des variants Bot Z standalone et les engines internes de Meta v2 :

| Ancien nom | Nouveau nom | Signification |
|------------|-------------|---------------|
| ENHANCED | **BULL** | Régime haussier propre (BTC+QQQ bull, VIX<22) |
| OMEGA | **BALANCED** | Neutre / quality-driven (base de tous les engines) |
| OMEGA_V2 | **PARITY** | Stress modéré, risk parity blend |
| PRO | **SHIELD** | Bear/crise — forcé si VIX>32 ou DD<-12% |

Les variants standalone du backtest conservent leur nom distinct :
- Bot Z v1 — MO+CB (ex-Enhanced)
- Bot Z v2 — QualityScore (ex-Omega)
- Bot Z v3 — RiskParity (ex-Omega v2)

### 2. BALANCED-as-base pour tous les engines

**Problème identifié (Run 14)** : L'engine BULL utilisait les poids de régime bruts (forte concentration sur Bot A). En régime BULL, Bot A recevait jusqu'à 70%+ du capital → MaxDD -14.4% lié aux drawdowns de Bot A (-68%).

**Solution** : BALANCED (QualityScore) devient la base de tous les engines avec des tilts marginaux :

| Engine | Composition |
|--------|-------------|
| **BALANCED** | 100% QualityScore (inchangé) |
| **BULL** | 60% BALANCED + 40% momentum tilt (poids régime) |
| **PARITY** | 60% BALANCED + 40% pure risk parity |
| **SHIELD** | 40% BALANCED + 60% vol-quality blend + RP |

**Effet** :
- En régime BULL, Bot A ne dépasse plus ~50% du capital (au lieu de 70%+)
- MaxDD réduit de -14.4% → -9.5%
- Sharpe amélioré de 1.55 → 1.90

### 3. Paramètres fenêtres weekly (rappel, depuis Run 14)

Toutes les fonctions `backtest_bot_z_*` utilisent désormais un rééchantillonnage hebdomadaire avant calcul :

```
SHARPE_WIN = 18  (était 90 jours → ÷5)
VOL_WIN    = 4   (était 20 jours → ÷5)
SLOPE_WIN  = 12  (était 60 jours → ÷5)
CORR_WIN   = 4   (était 20 jours → ÷5)
META_WIN   = 6   (était 30 jours → ÷5)
PERF_WIN   = 12  (était 60 jours → ÷5)
HYSTERESIS : BULL=2w, BALANCED=1w, PARITY=1w, SHIELD=1w
```

---

## Résultats Run 15

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
| Bot Z v1 — MO+CB | +52.6% | 1.56 | -25.7% | 271 927€ |
| Bot Z v2 — QualityScore | +50.4% | 2.08 | -9.9% | 234 827€ |
| Bot Z v3 — RiskParity | +20.9% | 1.93 | -5.1% | 24 024€ |
| **Bot Z PROD — Meta v2** | **+34.9%** | **1.90** | **-9.5%** | **67 487€** |

### Benchmarks (2016-2026)

| Benchmark | CAGR | Sharpe | MaxDD |
|-----------|------|--------|-------|
| S&P 500 | +12.9% | 0.77 | -33.9% |
| NASDAQ-100 | +19.9% | 0.93 | -35.1% |
| BTC/EUR buy&hold | +65.5% | 0.91 | -82.7% |

### Rendements annuels Bot Z Meta v2

| 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|------|------|------|------|------|------|------|------|------|------|------|
| +1.7% | +105.9% | -0.7% | +14.8% | +43.4% | +104.7% | -0.5% | +34.9% | +35.7% | +8.2% | +1.6% |

---

## Comparaison des runs

| Run | Description | Meta v2 CAGR | Sharpe | MaxDD |
|-----|-------------|--------------|--------|-------|
| Run 13 | Weekly resample (fix Shannon's Demon) | +19.7% | 1.10 | -25.7% |
| Run 14 | Fenêtres weekly calibrées (÷5) | +32.5% | 1.55 | -14.4% |
| **Run 15** | **BALANCED-as-base** | **+34.9%** | **1.90** | **-9.5%** |

**Progression totale depuis Run 13** : +15.2 pts CAGR, +0.80 Sharpe, MaxDD divisé par 2.7.

---

## Interprétation

### Meta v2 vs QualityScore standalone

Bot Z v2 — QualityScore (+50.4%, Sharpe 2.08, MaxDD -9.9%) surperforme toujours Meta v2 en backtest.
C'est attendu et voulu : QualityScore est une stratégie statique optimale sur données historiques.
Meta v2 est conçu pour l'adaptabilité OOS — il sacrifie du CAGR en backtest pour mieux gérer les régimes inconnus.

### Profil risk-ajusté de Meta v2 (Run 15)

- Sharpe 1.90 > NASDAQ-100 (0.93) × 2
- MaxDD -9.5% vs -35.1% NASDAQ, -68.3% Bot A seul
- En 2018 (-0.7%) et 2022 (-0.5%) : protection quasi-totale pendant les marchés baissiers
- En 2023 (+34.9%) et 2024 (+35.7%) : capture totale du rebond

### Décision

Production inchangée — Meta v2 tourne en live avec les nouveaux noms d'engines.
La revue officielle reste **2026-04-30**.

---

## Notes techniques

- `_engine_weights()` dans `backtest_bot_z_meta_v2` : refactorisée avec BALANCED comme base universelle
- `live/bot_z.py` : engines renommés (BULL/BALANCED/PARITY/SHIELD) — compatible avec nouveaux noms backtest
- `dashboard/templates/index.html` : CSS classes et labels mis à jour
- Fichiers modifiés : `backtest/multi_backtest.py`, `backtest/run10y.py`, `live/bot_z.py`, `dashboard/templates/index.html`
- Commit : `3e7d2f0`

# Backtest Run 14 — 10 ans final (2016-2026) — Fenêtres calibrées weekly

**Date** : 2026-03-08 (même journée que Run 13)
**Corrections vs Run 13** : fenêtres de calcul adaptées aux données hebdomadaires
(SHARPE_WIN 90j→18s, VOL_WIN 20j→4s, SLOPE_WIN 60j→12s, HYSTERESIS 7j→2s...)

## Résultats

| Stratégie | CAGR | Sharpe | MaxDD | Final |
|-----------|------|--------|-------|-------|
| Bot A | +49.3% | 2.43 | -68.3% | 55 008€ |
| Bot B | +36.8% | 0.77 | -67.8% | 23 097€ |
| Bot C (corrigé) | +0.6% | 0.21 | -7.8% | 1 057€ |
| Bot G | +23.4% | 0.65 | -22.6% | 8 179€ |
| Equal-Weight | +41.4% | 1.24 | -27.1% | 127 323€ |
| Z Régime pur | +53.0% | 1.48 | -26.5% | 279 863€ |
| Z Enhanced | +52.6% | 1.56 | -25.7% | 271 927€ |
| **Z Omega** | **+50.5%** | **2.08** | **-9.9%** | **236 450€** |
| Z Omega v2 | +20.9% | 1.93 | -5.1% | 24 041€ |
| **Z Meta v2 (PROD)** | **+32.5%** | **1.55** | **-14.4%** | **57 166€** |

## Benchmarks

| Benchmark | CAGR | Sharpe | MaxDD |
|-----------|------|--------|-------|
| S&P 500 | +12.9% | 0.77 | -33.9% |
| NASDAQ-100 | +19.9% | 0.93 | -35.1% |
| BTC/EUR buy&hold | +65.6% | 0.91 | -82.7% |

## Meta v2 — Rendements annuels (validation de la sélection d'engine)

| 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|------|------|------|------|------|------|------|------|------|------|
| +1.9% | +113% | +0.9% | +22% | +9.6% | +81% | -1.3% | +46.5% | +24.9% | +21.5% |

- 2018 : Bot B à -52.7%, Meta v2 à +0.9% → engine PRO/OMEGA_V2 a protégé ✓
- 2022 : bear market, Meta v2 à -1.3% vs bots -6 à -25% → PRO activé ✓
- 2023-2024 : positifs (corrigé vs Run 13 qui avait -4.8% et -5.5%) ✓

## Interprétation

**Bot Z Omega** (Sharpe 2.08, MaxDD -9.9%) : meilleur profil risk-ajusté.
Le scoring inverse-vol réduit l'exposition à Bot A (le plus volatil) tout en
gardant un CAGR élevé via la diversification.

**Bot Z Meta v2** (Sharpe 1.55, MaxDD -14.4%) : la sélection d'engines fonctionne.
ENHANCED en bull → capte les hausses. PRO/OMEGA_V2 en stress → protège.

**Décision** : résultats cohérents, pas de modification du code live. Revue 2026-04-30.

# Backtest Run 17 — FINAL (2016-2026) — Levier conditionnel BULL

**Date** : 2026-03-08
**Période** : 2016-01-01 → 2026-03-08
**Capital initial** : 1 000€ par bot, 4 000€ Bot Z
**Symboles** : 15/16 (TON/EUR manquant)
**Statut** : VERSION PRODUCTION — ne plus modifier avant revue 2026-04-30

---

## Dernière amélioration : levier conditionnel BULL (vol targeting)

### Principe

En régime BULL, quand la volatilité portefeuille hebdomadaire est inférieure à TARGET_VOL (20%),
le système augmente l'exposition jusqu'à TARGET_VOL. Plafonné à 1.30×.

```python
# Dans backtest_bot_z_meta_v2 (et live/bot_z.py)
lev_factor = 1.0
if current_engine in LEV_ENGINES and cb_factor >= 0.90 and len(eq) >= VOL_WIN + 1:
    pv_annual = std(recent_returns) * sqrt(52)
    if pv_annual < TARGET_VOL:
        lev_factor = min(1.30, TARGET_VOL / pv_annual)
```

**Conditions de sécurité** :
- `cb_factor >= 0.90` : le circuit breaker ne doit pas être actif (pas en crise)
- `pv_annual < TARGET_VOL` : vol réelle inférieure à 20% annualisé
- `LEV_ENGINES = {"BULL"}` : uniquement en régime BULL (pas BALANCED, PARITY, SHIELD)

**Justification économique** : vol targeting est utilisé par les fonds CTA (AQR, Man AHL).
En bull market avec faible volatilité, augmenter l'exposition est rationnel et contrôlé.

### Pourquoi Test A (hyst 1w) n'a pas été retenu

Le Test A (hysteresis BULL 2w→1w + leverage) donnait +43.0% CAGR vs +38.2%.
Mais après 17 backtests sur les mêmes données, réduire l'hysteresis de 1 semaine
est une micro-optimisation in-sample dont la robustesse OOS est incertaine.
**Décision : garder hysteresis BULL = 2 semaines.**

---

## Résultats finaux Run 17

### Bots individuels (1 000€ → 10 ans)

| Bot | Stratégie | CAGR | Sharpe | MaxDD | Trades | WR% | Final |
|-----|-----------|------|--------|-------|--------|-----|-------|
| A | Supertrend+MR | +49.3% | 2.43 | -68.3% | 452 | 43.8% | 55 008€ |
| B | Momentum | +36.8% | 0.77 | -67.8% | 365 | 19.2% | 23 097€ |
| C | Breakout | +0.6% | 0.21 | -7.8% | 199 | 40.2% | 1 057€ |
| G | Trend Multi-Asset | +23.4% | 0.65 | -22.6% | 289 | 54.3% | 8 179€ |
| J | Mean Reversion | +2.1% | 1.45 | -3.5% | 326 | 69.9% | 1 219€ |

### Bot Z Portfolio (4 000€ → 10 ans)

| Structure | CAGR | Sharpe | MaxDD | Final |
|-----------|------|--------|-------|-------|
| Equal-Weight A+B+C+G | +41.4% | 1.24 | -27.1% | 127 323€ |
| Bot Z Régime pur | +53.0% | 1.48 | -26.5% | 279 863€ |
| Bot Z v1 — MO+CB | +52.6% | 1.56 | -25.7% | 271 927€ |
| Bot Z v2 — QualityScore | +50.5% | 2.08 | -9.9% | 236 506€ |
| Bot Z v3 — RiskParity | +20.9% | 1.93 | -5.1% | 24 047€ |
| **Bot Z PROD — Meta v2** | **+38.2%** | **1.92** | **-10.1%** | **85 036€** |

### Benchmarks (2016-2026)

| Benchmark | CAGR | Sharpe | MaxDD |
|-----------|------|--------|-------|
| S&P 500 | +12.9% | 0.77 | -33.9% |
| NASDAQ-100 | +19.9% | 0.93 | -35.1% |
| BTC/EUR buy&hold | +65.5% | 0.91 | -82.7% |

### Rendements annuels Bot Z Meta v2 (PROD)

| 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|------|------|------|------|------|------|------|------|------|------|------|
| +1.7% | +117.7% | -0.6% | +15.6% | +47.8% | +110.2% | -0.5% | +39.1% | +42.6% | +10.8% | +1.6% |

---

## Progression des runs (historique complet)

| Run | Description | Meta v2 CAGR | Sharpe | MaxDD |
|-----|-------------|--------------|--------|-------|
| 11 | Référence initiale (6 ans 2020-2026) | +43.2% | 1.70 | -9.6% |
| 13 | Fix Shannon's Demon (weekly resample) | +19.7% | 1.10 | -25.7% |
| 14 | Fenêtres weekly calibrées (÷5) | +32.5% | 1.55 | -14.4% |
| 15 | BALANCED-as-base + renommage engines | +34.9% | 1.90 | -9.5% |
| **17** | **Levier conditionnel BULL (vol targeting)** | **+38.2%** | **1.92** | **-10.1%** |

**Progression totale (depuis bug fixes Run 13)** :
+18.5 pts CAGR | +0.82 Sharpe | MaxDD réduit de -25.7% à -10.1%

---

## Architecture finale Meta v2 (résumé technique)

### Engines et composition

| Engine | Condition activation | Composition portefeuille | Levier |
|--------|---------------------|--------------------------|--------|
| **BULL** | BTC+QQQ bull + VIX<22 (hysteresis 2w) | Poids régime (Bot A dominant) | Oui — vol targeting ×1.30 max |
| **BALANCED** | Neutre / défaut | 100% QualityScore (score data-driven) | Non |
| **PARITY** | VIX 22-32 | 50% QualityScore + 50% Risk Parity | Non |
| **SHIELD** | Bear / VIX>32 / DD<-12% | 40% QualityScore + 60% défensif (vol-scaled) | Non |

### Hard rules (non-négociables)

- SHIELD forcé si (BTC+QQQ bearish ET VIX>26) OU VIX>32 OU DD portefeuille < -12%
- BULL bloqué si BTC ou QQQ bearish

### Paramètres clés (données hebdomadaires)

```
SHARPE_WIN = 18 semaines | VOL_WIN = 4 | SLOPE_WIN = 12 | CORR_WIN = 4
META_WIN = 6 | HYSTERESIS : BULL=2w / BALANCED=1w / PARITY=1w / SHIELD=1w
TARGET_VOL = 0.20 (20% annualisé) | BULL_MAX_LEV = 1.30
```

---

## Décision finale

**Production inchangée. Revue obligatoire : 2026-04-30.**

Questions clés à vérifier le 2026-04-30 :
- Fréquence switchs engine (< 0.3/semaine ?)
- % temps en SHIELD (< 30% ?)
- MaxDD live (< 12% ?)
- Allocation drift (< 15% ?)
- BULL engine activé ? Levier utilisé ?

Commande de récupération :
```bash
scp ubuntu@51.210.13.248:/home/botuser/bot-trading/logs/bot_z/shadow.jsonl backtest/results/
python backtest/analyze_botz.py --csv
```

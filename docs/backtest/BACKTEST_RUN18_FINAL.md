# BACKTEST RUN 18 — FINAL
Date : 2026-03-08
Script : `backtest/run10y.py`
Période : 2016-01-01 → 2026-03-08 (10 ans)
Capital initial : 1 000€ par bot solo | 4 000€ Bot Z (10 000€ paper live)

---

## Contexte

Run 18 = premier backtest après la mise en production du paper trading (2026-03-08).
Confirm les résultats Run 17. Aucune modification du code de production entre Run 17 et Run 18.

Corrections appliquées depuis Run 12 (actives depuis Run 13+) :
- Bot C : stop loss sur `low` (pas `close`) — edge réel = +0.6% (vs +17% buggué)
- Bot Z : weekly resampling correct (élimine prime MtM journalière)
- run10y.py : CAGR calculé sur `z["metrics"]` (pas `compute_metrics`)

---

## Résultats — Bots individuels (1 000€ initial)

| Bot | Stratégie | CAGR | Sharpe | MaxDD | Trades | WR% | Final | PF |
|-----|-----------|------|--------|-------|--------|-----|-------|----|
| A | Supertrend+MR | +49.3% | 2.43 | -68.3% | 452 | 43.8% | 55 008€ | 2.72 |
| B | Momentum | +36.8% | 0.77 | -67.8% | 365 | 19.2% | 23 097€ | 1.66 |
| C | Breakout (réel) | +0.6% | 0.21 | -7.8% | 199 | 40.2% | 1 057€ | 1.07 |
| G | Trend Multi-Asset | +23.4% | 0.65 | -22.6% | 289 | 54.3% | 8 179€ | 4.50 |
| J | Mean Reversion | +2.1% | 1.45 | -3.5% | 326 | 69.9% | 1 219€ | 1.78 |

---

## Résultats — Variants Bot Z (4 000€ initial)

| Variant | CAGR | Sharpe | MaxDD | Final |
|---------|------|--------|-------|-------|
| Equal-Weight A+B+C+G | +41.4% | 1.24 | -27.1% | 127 323€ |
| Bot Z Régime pur | +53.0% | 1.48 | -26.5% | 279 863€ |
| Bot Z v1 — MO+CB | +52.6% | 1.56 | -25.7% | 271 927€ |
| Bot Z v2 — QualityScore | +50.5% | 2.08 | -9.9% | 236 472€ |
| Bot Z v3 — RiskParity | +20.9% | 1.93 | -5.1% | 24 041€ |
| **Bot Z PROD — Meta v2** | **+38.2%** | **1.92** | **-10.1%** | **84 985€** |

---

## Benchmarks (2016 → 2026)

| Benchmark | CAGR | Sharpe | MaxDD |
|-----------|------|--------|-------|
| S&P 500 | +12.9% | 0.77 | -33.9% |
| NASDAQ-100 | +19.9% | 0.93 | -35.1% |
| BTC/EUR buy & hold | +65.4% | 0.90 | -82.7% |

---

## Rendements annuels — Bot Z Meta v2 (PROD)

| 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|------|------|------|------|------|------|------|------|------|------|------|
| +1.7% | +117.6% | -0.6% | +15.6% | +47.8% | +110.2% | -0.5% | +39.1% | +42.6% | +10.8% | +1.6% |

Années négatives : 2018 (-0.6%) et 2022 (-0.5%) — drawdowns < 1% en bear market.

---

## Rendements annuels — Bots individuels

| Bot | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|-----|------|------|------|------|------|------|------|------|------|------|------|
| A | -27.5% | +181.1% | +74.6% | +87.6% | +388.0% | +416.0% | -6.9% | +99.2% | +92.5% | +77.9% | +5.2% |
| B | +3.2% | +244.4% | -52.7% | +78.9% | -12.1% | +608.3% | -25.6% | +71.9% | +5.3% | -7.9% | -0.7% |
| C | -5.3% | +7.2% | -2.7% | +2.5% | +2.7% | -2.2% | -1.3% | +6.9% | +1.1% | -2.0% | -0.8% |
| G | +4.6% | +15.5% | +7.7% | +24.8% | +49.2% | +100.0% | -6.6% | +34.1% | +36.1% | +26.8% | +2.1% |
| J | 0.0% | +2.9% | +2.2% | +0.9% | +0.6% | +4.7% | -2.6% | +4.6% | +3.5% | +1.9% | +1.5% |

---

## Analyse

### Clé du système

Bot A génère le CAGR (+49.3%) mais avec un drawdown catastrophique (-68.3%).
Bot Z réduit ce drawdown à -10.1% en diversifiant intelligemment et en basculant
en mode SHIELD lors des crises. Le coût : CAGR réduit à +38.2%.

**Sharpe 1.92 vs 0.77 S&P 500 = rendement ajusté au risque 2.5× supérieur.**

### Rôle de chaque bot dans le système

| Bot | Rôle réel | Contribution |
|-----|-----------|-------------|
| A | Moteur de CAGR | porte ~65% du profit en bull market |
| B | Booster crypto bull | cyclique (excellent 2017/2021, nul 2018/2022) |
| C | Amortisseur défensif | edge faible mais MaxDD -7.8% = tampon de sécurité |
| G | Stabilisateur | +23.4% CAGR, jamais < -7% sur une année |
| J | Candidat futur Bot Z | Sharpe 1.45, corrélation faible avec A (à observer) |

### Bot C — Note importante

CAGR +0.6% sur 10 ans = edge non prouvé en trading réel.
Son rôle dans Bot Z est défensif (SHIELD surpondère C pour stabiliser).
Ne pas le retirer — sa valeur est dans la diversification, pas la performance brute.

### Bot Z Meta v2 — Architecture confirmée

- **4 engines** : BULL / BALANCED / PARITY / SHIELD (sélection data-driven)
- **Scoring** : 0.50×regime_fit + 0.30×quality_norm + 0.20×inv_risk - 0.05×switch_penalty
- **Hysteresis** : BULL=7j / BALANCED=5j / PARITY=4j / SHIELD=3j
- **Vol targeting** : vol_factor = clip(0.20/portfolio_vol_20d, 0.3, 1.5)
- **Hard rules** : SHIELD forcé si VIX>32 ou DD<-12% ou (BTC+QQQ bearish ET VIX>26)

---

## État au lancement paper trading (2026-03-08)

```
Engine         : SHIELD (VIX 29.51, BTC bear)
Budget dispatch: A=1 402€ | B=1 000€ | C=4 600€ | G=2 998€
z_capital      : 10 000€ (stable, 7 cycles confirmés)
Services VPS   : running, 0 erreur
```

---

## Décision

**NE PLUS MODIFIER LE CODE.**
Observer le système en paper trading pendant 3 mois minimum.
Revue obligatoire : **2026-04-30**.

```bash
# Commandes revue 30 avril
scp ubuntu@51.210.13.248:/home/botuser/bot-trading/logs/bot_z/shadow.jsonl backtest/results/
python backtest/analyze_botz.py --csv
python backtest/run10y.py
```

Questions clés à vérifier :
- Fréquence switchs engine < 0.3/semaine ?
- % temps en SHIELD < 30% ?
- MaxDD live < 12% ?
- Drift allocation < 15% (actuellement 35% — normal en début de paper) ?
- Engine BULL activé au moins une fois ?
- MCPS Bot J > 0 ? → candidat ajout à VALID_BOTS

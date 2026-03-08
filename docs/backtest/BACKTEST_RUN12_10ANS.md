# Backtest Run 12 — 10 ans (2016 → 2026)
## Bot Trading — Résultats complets sur 10 ans

**Date d'exécution** : 2026-03-07 (v2 corrigée — stop loss sur LOW)
**Script** : `python backtest/run10y.py`
**Période** : 2016-01-01 → 2026-03-07 (10.2 ans)
**Données** : yfinance `period="10y"` — 15/16 symboles (TON indisponible)

---

## 0. Correction appliquée — Bug stop loss Bot C

**Bug identifié** (audit ChatGPT 2026-03-07) : le backtest Bot C vérifiait le stop ATR
et le Donchian exit sur le **CLOSE** de la bougie au lieu du **LOW**.

Conséquence : tout mouvement intraday (low < stop, close > stop) était ignoré.
Sur crypto daily avec des amplitudes de 5-15% intraday, cela supprimait des dizaines
de stops réels sur 10 ans.

**Fix appliqué dans `backtest/multi_backtest.py`** :
```python
# Avant (bug)
if row["close"] <= pos["stop"]:
    exit_reason, ep = "atr_stop", pos["stop"]
elif row["close"] < row["don_low"]:
    exit_reason = "don_exit"

# Après (corrigé)
if row["low"] <= pos["stop"]:
    exit_reason, ep = "atr_stop", pos["stop"]
elif row["low"] < row["don_low"]:
    exit_reason, ep = "don_exit", float(row["don_low"])
```

---

## 1. Performance individuelle des bots (résultats corrigés)

| Bot | Stratégie | CAGR | Sharpe | MaxDD | Trades | WR% | Capital final¹ | PF |
|-----|-----------|------|--------|-------|--------|-----|----------------|-----|
| **A** | Supertrend+MR | **+48.9%** | **2.42** | -68.3% | 452 | 43.8% | **53 560€** | 2.72 |
| **B** | Momentum Rotation | +36.8% | 0.77 | -67.8% | 365 | 19.2% | 23 097€ | 1.66 |
| **C** | Donchian Breakout | **+0.6%** | **0.21** | **-7.8%** | 199 | 40.2% | **1 057€** | 1.07 |
| **G** | Trend Multi-Asset | +23.4% | 0.65 | -22.6% | 289 | 54.3% | 8 179€ | 4.50 |
| **J** | Mean Reversion | +2.1% | 1.45 | -3.5% | 326 | 69.9% | 1 219€ | 1.78 |

¹ À partir de 1 000€ initial, sans protection Bot Z.

### Avant/après la correction pour Bot C

| Métrique | Run 12 bugué | Run 12 corrigé | Delta |
|----------|-------------|----------------|-------|
| CAGR | +17.2% | **+0.6%** | -16.6pp |
| Sharpe | 2.88 | **0.21** | -2.67 |
| MaxDD | -6.6% | **-7.8%** | -1.2pp |
| Trades | 101 | **199** | +98 (stops ignorés) |
| Win rate | 57.4% | **40.2%** | -17pp |

**Conclusion** : les performances exceptionnelles de Bot C étaient un artefact de backtest.
La vraie stratégie Donchian sur BTC/ETH/SOL sur 10 ans est quasi plate (+0.6%/an).

### Benchmarks (2016 → 2026)

| Benchmark | CAGR | Sharpe | MaxDD |
|-----------|------|--------|-------|
| S&P 500 (buy & hold) | +12.9% | 0.77 | -33.9% |
| NASDAQ-100 (QQQ) | +19.9% | 0.93 | -35.1% |
| BTC/EUR (buy & hold) | +65.5% | 0.91 | -82.7% |
| **Bot A** | **+48.9%** | **2.42** | -68.3% |
| ~~Bot C~~ | ~~+17.2%~~  | ~~2.88~~ | ~~-6.6%~~ |
| **Bot C (corrigé)** | **+0.6%** | **0.21** | -7.8% |

**Lecture clé** :
- Bot A bat le S&P 500 (+48.9% vs +12.9%) avec un **Sharpe 3× supérieur** (2.42 vs 0.77)
- Bot A bat le NASDAQ (+48.9% vs +19.9%) avec un **Sharpe 2.5× supérieur**
- Bot C corrigé : **sous-performe le S&P 500** — la stratégie Donchian sur BTC/ETH/SOL seule n'a pas d'edge robuste sur 10 ans

---

## 2. Rendements annuels par bot

| Année | Bot A | Bot B | Bot C (corrigé) | Bot G | Bot J |
|-------|-------|-------|-----------------|-------|-------|
| **2016** | -30.0% | +3.2% | -5.3% | +4.6% | +0.0% |
| **2017** | +183.8% | +244.4% | +7.2% | +15.5% | +2.9% |
| **2018** | +74.6% | -52.7% | -2.7% | +7.7% | +2.2% |
| **2019** | +87.6% | +78.9% | +2.5% | +24.8% | +0.9% |
| **2020** | +388.0% | -12.1% | +2.7% | +49.2% | +0.6% |
| **2021** | +416.0% | +608.3% | -2.2% | +100.0% | +4.7% |
| **2022** | -6.9% | -25.6% | -1.3% | -6.6% | -2.6% |
| **2023** | +99.2% | +71.9% | +6.9% | +34.1% | +4.6% |
| **2024** | +92.5% | +5.3% | +1.1% | +36.1% | +3.5% |
| **2025** | +77.9% | -7.9% | -2.0% | +26.8% | +1.9% |
| **2026 YTD** | +5.2% | -0.7% | -0.8% | +2.1% | +1.5% |

### Résistance aux années difficiles

| Crise | Bot A | Bot C (corrigé) | Bot G | S&P 500 |
|-------|-------|-----------------|-------|---------|
| 2016 (début) | **-30.0%** | -5.3% | +4.6% | +9.5% |
| 2018 (bear crypto) | **+74.6%** | -2.7% | +7.7% | -6.2% |
| 2022 (bear marché) | **-6.9%** | -1.3% | **-6.6%** | -19.4% |

**Observations** :
- Bot C reste stable en 2022 (-1.3%) mais aucun gain significatif aucune année
- **Bot A en 2022 : -6.9%** pendant que le S&P perd -19.4% — la vraie résistance
- **Bot G** : aucune perte > -7% sur 10 ans, positif 8 années sur 10 — le vrai stabilisateur

---

## 3. Bot Z Meta v2 (PROD) — Note sur les résultats

> ⚠️ **Les résultats Bot Z (CAGR +415%, +8703% en 2017) sont erronés** et non représentatifs.

La même anomalie de compounding détectée dans le backtest 46 ans est présente sur 10 ans.

**Référence valide : Run 11 (6 ans, 2020-2026)** — données correctes calibrées :

| Metric | Run 11 (6 ans) |
|--------|----------------|
| CAGR | +43.2% |
| Sharpe | 1.70 |
| MaxDD | -9.6% |
| 2022 | +1.3% |
| Capital final (4 000€) | 29 961€ |

---

## 4. Synthèse des 3 backtests (post-correction)

| Période | Bot A CAGR | Bot A Sharpe | Bot C Sharpe | Bot C MaxDD |
|---------|-----------|-------------|-------------|-------------|
| **6 ans** (2020-2026) Run 11 | +35.5% | 3.38 | ~~2.14~~ | ~~-6.0%~~ |
| **10 ans** (2016-2026) Run 12 | +48.9% | 2.42 | **0.21** | **-7.8%** |
| **46 ans** (1980-2026) | +26.0% | 1.46 | (non corrigé) | (non corrigé) |

> **TODO** : relancer le backtest 46 ans avec le fix pour avoir les vrais chiffres Bot C long terme.
> **TODO** : vérifier et corriger le même bug dans le backtest Run 11 (6 ans) pour Bot C.

**Ce que montrent les horizons temporels** :
1. **Bot A** : Sharpe décroît en allongeant la période (3.38 → 2.42 → 1.46). Reste positif sur 46 ans à +26%/an. C'est le moteur du système.
2. **Bot C** : après correction, quasi-flat. L'edge apparent était un artefact de backtest. À surveiller en paper trading pour valider si la stratégie live performe différemment.
3. **Bot G** : CAGR +23.4% Sharpe 0.65 — devient le 2e contributeur le plus fiable après Bot A.
4. **Bot B** : volatil, crypto-dépendant. Utile en bull crypto, destructeur sinon.

---

## 5. Reclassement des bots après audit

| Bot | Rôle | Statut |
|-----|------|--------|
| **A** | Moteur principal (+48.9% CAGR) | Validé — stratégie réelle |
| **G** | Diversification stable (+23.4%) | Validé — vrai stabilisateur |
| **B** | Booster bull crypto (+36.8%) | Validé — mais cyclique |
| **J** | Stabilisateur cash (+2.1%) | Validé — tampon |
| **C** | Breakout crypto (+0.6%) | **Réévalué** — edge non prouvé sur 10 ans |

---

## 6. Enseignement final

Le bug stop loss de Bot C montre l'importance de l'audit de code avant de conclure sur les performances. **Les chiffres miraculeux (Sharpe 2.88, MaxDD -6.6%) étaient un artefact.**

Après correction :
- **Bot A** reste le moteur indiscutable du système (Sharpe 2.42, +48.9% sur 10 ans)
- **Bot G** est le vrai stabilisateur (jamais > -7%/an, positif 8 ans sur 10)
- **Bot Z** protège les deux avec une allocation dynamique — référence Run 11 : MaxDD -9.6%
- **Bot C** à surveiller en paper trading avant toute conclusion définitive

**Décision** : ne pas toucher. Observer jusqu'à la revue 2026-04-30. Corriger aussi Run 11 et backtest 46 ans.

---

*Script* : `backtest/run10y.py`
*Fix* : `backtest/multi_backtest.py` — commit `0b37c85`
*Données* : `backtest/results/run10y_summary.csv`, `run10y_equity.png`
*Référence* : `docs/BACKTEST_RUN11_AUDIT.md` (Run 11, 6 ans, validé ChatGPT)

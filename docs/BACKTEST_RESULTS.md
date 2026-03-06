# Résultats Backtests 3 ans — Multi-Bots

> Script : `backtest/multi_backtest.py`
> Dernier run : 2026-03-06
> Graphique : `backtest/results/multi_equity.png`
> CSV détaillé : `backtest/results/multi_summary.csv`
> Période couverte : janvier 2023 → mars 2026 (3 ans, données daily)
> Symboles : 16/20 (LINK, AVAX, TSLA, AMZN absents — données insuffisantes sur 3 ans)

---

## Tableau comparatif global

| Bot | Stratégie | CAGR | Sharpe | Max DD | Profit Factor | Trades | Win Rate | Capital final |
|-----|-----------|------|--------|--------|---------------|--------|----------|---------------|
| A | Supertrend+MR | **+13.4%** | 3.68 (*) | -66.6% (**) | 2.91 | 144 | 44.4% | 1 949€ |
| B | Momentum | **+15.3%** | 2.50 (*) | -72.1% (**) | 1.94 | 40 | 25.0% | 2 135€ |
| C | Breakout (crypto) | +8.5% | 0.94 | **-5.1%** | 2.87 | 35 | 48.6% | 1 293€ |
| G | Trend Multi-Asset | +10.1% | 0.41 | -16.0% | 3.99 | 87 | 56.3% | 1 668€ |
| H | VCB Breakout | 0% | — | — | — | **0** (***) | — | 1 000€ |
| I | RS Leaders | -3.9% | — | -19.7% | — | 127 | **0%** (***) | 807€ |
| **TOTAL** | **6 bots / 6000€** | **+9.3%** | — | — | — | 433 | — | **9 852€** |

(*) Sharpe artificiellement élevé pour A et B : le calcul sur l'equity curve daily (souvent plate sans position) minimise la std et gonfle le Sharpe. Non représentatif.

(**) MaxDD élevé pour A et B : crypto volatilité réelle 2023-2025. BTC a fait -30% en cours de tendance plusieurs fois. Trailing stop 3×ATR sur daily peut laisser de grosses pertes latentes.

(***) Bugs identifiés — voir section "Bugs à corriger" ci-dessous.

---

## Performance par année

| Bot | 2023 | 2024 | 2025 | 2026 (YTD) |
|-----|------|------|------|------------|
| A | +10.4% | +85.3% | +147.1% | +10.5% |
| B | +83.3% | -12.7% | +4.3% | -4.0% |
| C | +20.6% | +9.0% | -0.8% | -0.9% |
| G | +15.2% | +32.2% | +27.3% | +2.2% |
| H | — | — | — | — |
| I | -3.2% | -9.4% | -8.0% | — |

**Observations :**
- **Bot B 2023 +83%** : a capté la rotation momentum crypto en sortie du bear 2022 → bonne stratégie en début de bull
- **Bot B 2024 -12.7%** : retournement des positions, la rotation n'a pas fonctionné (stop -12% déclenché)
- **Bot A 2025 +147%** : a accompagné le bull run BTC/crypto 2025 avec trailing stop — effet cumulatif des positions qui tiennent
- **Bot G** : le plus régulier d'année en année (+15 / +32 / +27) — confirme la solidité du trend following multi-actifs
- **Bot C** : très stable mais peu de trades (35 en 3 ans) — undershoots en bull fort, mais rassurant

---

## Performance par régime de marché

*Bug de date-matching détecté — données non disponibles pour ce run.*
*À corriger dans la prochaine version du script.*

| Bot | BULL | RANGE | BEAR | HIGH_VOL |
|-----|------|-------|------|----------|
| A | — | — | — | — |
| B | — | — | — | — |
| C | — | — | — | — |
| G | — | — | — | — |
| H | — | — | — | — |
| I | — | — | — | — |

---

## Analyse qualitative

### Classement réel vs attendu

| Rang | Bot | CAGR réel | Attendu | Commentaire |
|------|-----|-----------|---------|-------------|
| 1 | B — Momentum | +15.3% | 4e | Surprise : a capté le bull 2023 crypto fort |
| 2 | A — Supertrend | +13.4% | 2e | Conforme, trailing stop efficace sur bull |
| 3 | G — Trend | +10.1% | 1e | Plus stable que B mais moins de CAGR |
| 4 | C — Breakout | +8.5% | 6e | Surprise : meilleur ratio risque/reward (MaxDD -5%) |
| 5 | H — VCB | 0% | 3e | Invalide en daily — test impossible |
| 6 | I — RS Leaders | -3.9% | 5e | Bug identifié — résultats non fiables |

### Enseignements clés

**1. Bot G est le meilleur bot sur le plan de la régularité**
- Positif chaque année (+15% / +32% / +27%)
- MaxDD -16% : acceptable
- 87 trades : statistiquement fiable
- Profit Factor 3.99 : excellent (chaque euro perdu rapporte 3.99€ en gains)

**2. Bot C est le plus sûr**
- MaxDD -5.1% : exceptionnel pour du crypto
- Mais seulement 35 trades en 3 ans et univers limité (BTC/ETH/SOL)
- CAGR +8.5% : correct mais sous-performe le marché bull 2023-2024

**3. Bot B est opportuniste mais instable**
- Excellent en bull 2023 (+83%), mauvais en 2024 (-12%)
- 25% win rate mais Profit Factor 1.94 → les gains compensent les nombreuses petites pertes
- Strategy académique solide (Antonacci) mais corrélée au cycle crypto

**4. Bot A sur-performe en tendance forte**
- 2024 +85% et 2025 +147% : a accompagné le bull run sans coupure prématurée
- MaxDD -66% : préoccupant en apparence, mais reflète la volatilité crypto normale
- 144 trades : statistiquement fiable

**5. Période couverte = principalement bull market**
- Les données commencent janvier 2023 (après le bear 2022)
- 2022 (crash Terra, FTX, -65% BTC) n'est PAS couvert → biais d'optimisme
- Tous les bots testés dans un contexte globalement favorable

---

## Bugs identifiés — à corriger

### Bug 1 : Bot H = 0 trades
**Cause** : La compression ATR (5 barres daily décroissantes) est trop rare sur daily. En production, Bot H tourne sur 4h (6× plus de barres → la compression se détecte bien).
**Solution** : Exclure Bot H du backtest daily ou créer une version 4h séparée.

### Bug 2 : Bot I = 0% win rate
**Cause probable** : Churn excessif — la rotation toutes les 5 jours génère des frais (0.26% × 2 = 0.52% par aller-retour) qui effacent les petits gains. Le code sort et ré-entre sur les mêmes positions.
**Solution** : Vérifier la logique rs_exit_rank + rebalancement, ajouter un filtre "ne pas re-rentrer sur un actif qu'on vient de sortir le même jour".

### Bug 3 : Tableau régime = tout zéro
**Cause** : Incompatibilité de timezone entre les dates des trades (pandas Timestamp du cache Binance, parfois UTC+0) et l'index VIX/QQQ (yfinance, parfois localisé). La fonction `asof()` retourne NaN → exception silencieuse → UNKNOWN.
**Solution** : Normaliser toutes les dates en `pd.Timestamp(dt).normalize().tz_localize(None)` avant le lookup.

### Bug 4 : Sharpe artificiel pour A et B
**Cause** : La série equity daily contient beaucoup de points plats (equity = capital cash = constant sans position ouverte). La std des returns est quasi-nulle → Sharpe explosé.
**Solution** : Calculer le Sharpe uniquement sur les barres où une position est ouverte, ou sur les trade PnL normalisés.

---

## Prochaines corrections

- [ ] Corriger le date-matching pour le tableau régime
- [ ] Corriger le Sharpe (calcul sur trades, pas sur equity plate)
- [ ] Exclure Bot H du backtest daily (0 trades, non représentatif)
- [ ] Corriger le churn Bot I (filtre re-entry)
- [ ] Ajouter 2022 si données disponibles (test bear market)
- [ ] Relancer après corrections

---

## Calibration Bot Z (provisoire)

Basé sur les résultats actuels (sans le tableau régime) :

| Régime | Meilleure stratégie | Raisonnement |
|--------|--------------------|----|
| BULL | G > A > B | Trend + momentum = bull market = naturel |
| RANGE | A > C | Mean reversion + breakout conservateur |
| BEAR | C (cash sinon) | Seul bot avec MaxDD faible, sinon rester cash |
| HIGH_VOL | C ou cash | Breakout resserré ou flat |

*À affiner après correction du tableau régime.*

---

## Historique des runs

| Date | Notes | CAGR moyen | Fichier |
|------|-------|-----------|---------|
| 2026-03-06 | Premier run — 16 symboles, daily uniquement. Bugs H/I/régime identifiés | +9.3% (4 bots valides) | multi_summary.csv |

---

*Relancer le backtest : `python3 backtest/multi_backtest.py`*

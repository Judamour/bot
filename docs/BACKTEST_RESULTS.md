# Résultats Backtests 3 ans — Multi-Bots

> Généré par `backtest/multi_backtest.py`
> Mise à jour : à compléter après chaque run
> Graphique : `backtest/results/multi_equity.png`
> CSV détaillé : `backtest/results/multi_summary.csv`

---

## Tableau comparatif global

| Bot | Stratégie | CAGR | Sharpe | Max DD | Profit Factor | Trades | Win Rate | Capital final |
|-----|-----------|------|--------|--------|---------------|--------|----------|---------------|
| A | Supertrend+MR | — | — | — | — | — | — | — |
| B | Momentum | — | — | — | — | — | — | — |
| C | Breakout (crypto) | — | — | — | — | — | — | — |
| G | Trend Multi-Asset | — | — | — | — | — | — | — |
| H | VCB Breakout | — | — | — | — | — | — | — |
| I | RS Leaders | — | — | — | — | — | — | — |
| **TOTAL** | **6 bots / 6000€** | — | — | — | — | — | — | — |

*Remplir après avoir lancé : `python backtest/multi_backtest.py`*

---

## Performance par année

| Bot | 2022 (bear) | 2023 (bull lent) | 2024 (bull fort) | 2025 (YTD) |
|-----|-------------|------------------|------------------|------------|
| A | — | — | — | — |
| B | — | — | — | — |
| C | — | — | — | — |
| G | — | — | — | — |
| H | — | — | — | — |
| I | — | — | — | — |

**Interprétation :**
- 2022 = test de résistance bear market (-20% S&P500, -65% BTC)
- 2023 = bull progressif, rotation sectorielle
- 2024 = bull fort tech/crypto (NVDA ×4, BTC ×150%)

---

## Performance par régime de marché

| Bot | BULL | RANGE | BEAR | HIGH_VOL |
|-----|------|-------|------|----------|
| A | — | — | — | — |
| B | — | — | — | — |
| C | — | — | — | — |
| G | — | — | — | — |
| H | — | — | — | — |
| I | — | — | — | — |

**Définition des régimes :**
- BULL : QQQ > SMA200 + VIX < 18
- RANGE : QQQ > SMA200 + VIX 18-30
- BEAR : QQQ < SMA200
- HIGH_VOL : VIX > 30

*Ce tableau est la base de calibration du Bot Z (poids par régime).*

---

## Analyse qualitative

### Classement attendu (à valider)

| Rang | Bot | Raison |
|------|-----|--------|
| 1 | G | Trend following multi-actifs = stratégie la plus étudiée |
| 2 | I | RS + filtres qualité = MSCI Momentum 12-16% CAGR historique |
| 3 | H | VCB performant mais rare → peu de trades |
| 4 | B | Momentum classique, moins filtré que I |
| 5 | A | Dépend du cycle de marché |
| 6 | C | Univers limité (3 crypto seulement) |

### Corrélations attendues

- G et C : très corrélés (deux trend following) → confirme utilité de garder uniquement G
- B et I : corrélés mais I plus filtré → I devrait dominer B
- H : décorrélé (exige compression préalable) → bon diversificateur
- A : peu corrélé aux autres (4h, mean reversion inclus)

---

## Simulations Bot Z (à compléter)

### Perf réelle vs perf simulée Bot Z

| Période | Perf réelle 6 bots | Perf simulée Bot Z | Delta |
|---------|-------------------|-------------------|-------|
| — | — | — | — |

*Disponible après 3 mois de shadow mode (logs/bot_z/shadow.jsonl).*

---

## Conclusions et décisions

### Bots à conserver (à remplir)

- [ ] Bot A :
- [ ] Bot B :
- [ ] Bot C :
- [ ] Bot G :
- [ ] Bot H :
- [ ] Bot I :

### Calibration Bot Z

Après analyse des régimes, les poids seront ajustés dans `live/bot_z.py` :

```python
REGIME_WEIGHTS = {
    "BULL":     {"a": ?, "b": ?, "c": ?, "g": ?, "h": ?, "i": ?},
    "RANGE":    {"a": ?, "b": ?, "c": ?, "g": ?, "h": ?, "i": ?},
    "BEAR":     {"a": ?, "b": ?, "c": ?, "g": ?, "h": ?, "i": ?},
    "HIGH_VOL": {"a": ?, "b": ?, "c": ?, "g": ?, "h": ?, "i": ?},
}
```

### Décision de basculer en capital mutualisé

Conditions requises :
- [ ] Backtests validés sur 3 ans
- [ ] 3 mois de shadow mode Bot Z
- [ ] Au moins 20 trades par bot actif
- [ ] Sharpe Bot Z simulé > Sharpe moyen bots séparés

---

## Historique des runs

| Date | Notes | Fichier résultats |
|------|-------|-------------------|
| — | Premier run — à compléter | — |

---

*Pour relancer le backtest après mise à jour des données : `python backtest/multi_backtest.py`*
*Les résultats remplacent l'ancien CSV automatiquement.*

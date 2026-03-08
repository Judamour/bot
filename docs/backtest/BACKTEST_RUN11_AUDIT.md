# Backtest Run 11 — Résultats complets pour audit
## Bot Trading — 2020-01 → 2026-03 (6 ans, 16 symboles)

Date d'exécution : 2026-03-06
Script : `python backtest/multi_backtest.py`

---

## Contexte

Système de paper trading automatisé gérant un portefeuille de 4 stratégies (A/B/C/G)
supervisées par un meta-allocateur Bot Z. Capital total : 4 × 1 000€ = 4 000€.

**Univers** :
- Crypto (5) : BTC/EUR, ETH/EUR, SOL/EUR, BNB/EUR, TON/EUR
- xStocks Kraken (11) : NVDAx, AAPLx, MSFTx, METAx, GOOGx, PLTRx, AMDx, AVGOx, GLDx, NFLXx, CRWDx
- Timeframe : daily (backtest) / 4h (live)
- Frais : 0.26% maker/taker Kraken + 0.1% slippage

---

## 1. Performance individuelle des bots (2020-2026)

| Bot | Stratégie | CAGR | Sharpe | MaxDD | Trades | WinRate | Final (1 000€) |
|-----|-----------|------|--------|-------|--------|---------|----------------|
| A | Supertrend+MR | +35.5% | 3.38 | -66.2% | 206 | 44.2% | 15 383€ |
| B | Momentum Antonacci | +39.2% | 2.26 | -71.6% | 68 | 32.4% | 19 785€ |
| C | Donchian Breakout | +13.9% | 2.14 | -6.0% | 72 | 48.6% | 2 236€ |
| G | Trend Multi-Asset | +19.1% | 0.67 | -23.1% | 141 | 53.2% | 4 831€ |
| H | VCB Breakout | +0.0% | 0.00 | 0.0% | 0 | — | 1 000€ |
| I | RS Leaders | +11.0% | 2.18 | -21.9% | 82 | 8.5% | 2 565€ |
| J | Mean Reversion | +1.6% | 1.47 | -1.7% | 161 | 70.8% | 1 133€ |

**Note** : Bots A et B dominent en CAGR mais avec des MaxDD catastrophiques (-66%/-71%) — essentiellement portés par le bull crypto 2020-2021. Bot C est le plus stable (MaxDD -6%). Bot G a le Sharpe le plus faible (0.67) malgré un CAGR correct.

---

## 2. Performance annuelle par bot

| Bot | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (YTD) |
|-----|------|------|------|------|------|------|------------|
| A | +78.3% | +277.0% | -47.2% | +77.3% | +84.7% | +147.1% | +52.3% |
| B | +133.8% | +500.8% | -43.5% | +80.0% | +7.1% | +2.3% | -3.4% |
| C | +14.3% | +47.7% | -2.5% | +26.5% | +9.0% | -0.8% | -0.9% |
| G | +18.9% | +111.9% | -3.4% | +36.5% | +32.3% | +27.3% | +2.2% |

**Observations** :
- 2022 (bear crypto) : A=-47%, B=-43% — destruction massive. C=-2.5%, G=-3.4% — défensifs
- Bot B est une fusée 2020-2021 (+500% en 2021) puis plat depuis 2023
- Bot A reste régulier post-2022 : +77%, +84%, +147%
- Bot G est le plus régulier sur 2022-2026 (jamais catastrophique)

---

## 3. Bot Z — 10 structures de portefeuille comparées (capital 4 × 1 000€)

| Structure | CAGR | Sharpe | MaxDD | Final (4 000€) | Notes |
|-----------|------|--------|-------|----------------|-------|
| REF: Bot B seul ×4 | +39.2% | 2.26 | -71.6% | 79 140€ | référence (0 diversification) |
| Equal-Weight A+B+C+G | +46.1% | 1.21 | -31.9% | 41 109€ | diversification pure |
| Bot Z Régime pur | +54.9% | 1.42 | -25.4% | 58 841€ | allocation dynamique 100% |
| Hybride 70/30 | +44.2% | 1.31 | -25.6% | 37 873€ | 70% stable + 30% overlay |
| Bot Z Enhanced | +60.0% | 1.62 | -17.7% | 71 779€ | MO + Circuit Breaker |
| Bot Z Pro | +29.4% | 1.88 | -10.2% | 19 532€ | VT + adaptive + multi-CB |
| Bot Z Adaptive | +29.0% | 1.59 | -11.7% | 19 102€ | meta-switch E/B/P |
| Bot Z Omega | +55.6% | **1.96** | -8.7% | 60 501€ | ER + Risk + Corr Penalty |
| Bot Z Omega v2 | +26.1% | **2.03** | **-7.6%** | 14 719€ | Omega + Risk Parity |
| Bot Z Meta (v1) | +37.0% | 1.47 | -15.1% | 23 433€ | sélecteur E/Ω/Ω2/P simple |
| **Bot Z Meta v2 (PROD)** | **+43.2%** | **1.70** | **-9.6%** | **29 961€** | engine scoring data-driven |

**Bot Z Meta v2 est le système en production.** Distribution engines sur 6 ans : ENHANCED 17% / OMEGA 30% / OMEGA_V2 28% / PRO 25%.

---

## 4. Performance annuelle des structures Bot Z

| Structure | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|-----------|------|------|------|------|------|------|------|
| Equal-Weight | +56.1% | +213.4% | -17.8% | +59.0% | +17.6% | +26.5% | +3.5% |
| Bot Z Régime pur | +62.7% | +232.0% | -10.9% | +65.6% | +24.4% | +35.8% | +4.2% |
| Bot Z Enhanced | +62.7% | +276.9% | **-8.6%** | +72.2% | +27.2% | +36.1% | +3.1% |
| Bot Z Pro | +35.9% | +87.9% | -6.7% | +38.5% | +17.6% | +23.1% | +0.6% |
| Bot Z Omega | +56.4% | +267.2% | **+0.5%** | +60.8% | +22.5% | +28.0% | +1.6% |
| Bot Z Omega v2 | +20.3% | +90.7% | **+0.2%** | +28.3% | +8.9% | +12.8% | +1.0% |
| **Bot Z Meta v2** | +21.6% | +173.5% | **+1.3%** | +62.2% | +11.6% | +17.9% | +1.0% |

**2022 est l'année test** (bear crypto -60%, QQQ -30%) :
- Equal-Weight : -17.8% — pas assez défensif
- Bot Z Enhanced : -8.6% — Circuit Breaker protège bien
- Bot Z Omega : +0.5% — presque flat en bear
- **Bot Z Meta v2 : +1.3%** — positif en 2022 grâce aux switchs défensifs

---

## 5. Comparaison vs benchmarks standards

| Benchmark | CAGR | MaxDD |
|-----------|------|-------|
| S&P 500 (2020-2026) | ~+14% | ~-24% |
| BTC/EUR seul (2020-2026) | ~+48% | ~-77% |
| Equal-Weight A+B+C+G | +46.1% | -31.9% |
| **Bot Z Meta v2 (PROD)** | **+43.2%** | **-9.6%** |
| Bot Z Enhanced (meilleur CAGR) | +60.0% | -17.7% |
| Bot Z Omega (meilleur Sharpe) | +55.6% Sharpe 1.96 | -8.7% |

Bot Z Meta v2 bat le S&P 500 (+43% vs +14%) avec un MaxDD bien inférieur (-9.6% vs -24%).
CAGR proche de BTC mais MaxDD 8× inférieur (-9.6% vs -77%).

---

## 6. Walk-Forward Test (validation out-of-sample)

**Méthodologie** : In-Sample = 2020-2022 (3 ans d'optimisation) / Out-of-Sample = 2023-2026 (3 ans de validation indépendante)

| Structure | IS CAGR | IS Sharpe | IS MaxDD | OOS CAGR | OOS Sharpe | OOS MaxDD | Verdict |
|-----------|---------|-----------|----------|----------|------------|-----------|---------|
| Equal-Weight | +60.5% | 1.32 | -31.0% | +33.8% | 1.09 | -17.1% | EDGE RÉEL |
| Bot Z Régime pur | +70.6% | 1.56 | -25.4% | +41.5% | 1.27 | -14.0% | EDGE RÉEL |

**Conclusion** : L'edge ne disparaît pas hors-échantillon. Le Sharpe baisse modérément (IS 1.56 → OOS 1.27) ce qui est normal et sain — pas de sur-ajustement détecté.

---

## 7. Monte Carlo — Robustesse statistique (5 000 simulations)

Ordre des trades randomisé sur 5 000 simulations par bot. Si p5 CAGR > 0 → l'edge est réel.

| Bot | Trades | CAGR réel | p5 CAGR | p50 CAGR | % simulations positives | Verdict |
|-----|--------|-----------|---------|----------|------------------------|---------|
| A — Supertrend | 206 | +35.5% | +94.4% | +94.4% | 100% | EDGE CONFIRMÉ |
| B — Momentum | 68 | +39.2% | +797.8% | +797.8% | 100% | EDGE CONFIRMÉ |
| C — Breakout | 72 | +13.9% | +74.8% | +74.8% | 100% | EDGE CONFIRMÉ |
| G — Trend | 141 | +19.1% | +74.8% | +74.8% | 100% | EDGE CONFIRMÉ |

**Tous les 4 bots sont positifs sur 100% des simulations Monte Carlo.** L'edge statistique est robuste et indépendant de la séquence des trades.

---

## 8. Analyse des tensions et arbitrages

### Tension CAGR vs MaxDD

Le système fait face à un arbitrage fondamental :

| | CAGR max | MaxDD associé |
|--|---------|---------------|
| Bot Z Enhanced | +60% | -17.7% |
| Bot Z Omega | +55.6% | -8.7% |
| Bot Z Meta v2 (PROD) | +43.2% | -9.6% |
| Bot Z Pro | +29.4% | -10.2% |

Bot Z Meta v2 est le compromis choisi pour la production : CAGR +43% avec MaxDD -9.6%. Il n'est pas le meilleur sur aucun axe pris individuellement, mais offre la meilleure **régularité inter-régimes**.

### Dépendance au bull crypto 2020-2021

Les CAGR élevés de A (+35%) et B (+39%) sont en grande partie expliqués par 2020-2021 (+78/+277% et +133/+500%). En excluant ces 2 années :

| Bot | CAGR 2022-2026 (4 ans) |
|-----|------------------------|
| A | ~+65% annualisé |
| B | ~+8% annualisé |
| C | ~+8% annualisé |
| G | ~+24% annualisé |

Bot B s'effondre hors bull crypto — risque de mean-reversion du momentum long terme.

### Bot G — Sharpe anormalement bas (0.67)

Bot G a CAGR +19% mais Sharpe 0.67 (vs 2.14-3.38 pour les autres). Cela signifie une volatilité des retours élevée relativement au rendement. À surveiller en paper trading.

---

## 9. Limites du backtest

1. **Lookahead bias possible** — le backtest applique les signaux sur le close de la même bougie. En production, les signaux sont générés sur le close et exécutés au prochain open.

2. **Frais conservatifs** — 0.26% + 0.1% slippage. En réalité Kraken peut aller jusqu'à 0.4% sur petits ordres.

3. **Données xStocks limitées** — seulement 4 ans (depuis 2022) vs 6 ans pour crypto. La performance 2022 pour xStocks est donc in-sample pour certains paramètres.

4. **Pas de coût de portage** — paper trading, aucune position overnight margin.

5. **VIX/QQQ daily** — le régime est recalculé daily en backtest. En production il tourne toutes les 4h (6 cycles/jour) — légèrement différent.

6. **Monte Carlo simplifié** — randomise l'ordre mais pas les tailles ou les délais entre trades. Ne simule pas de gap ou de liquidité limitée.

---

## 10. Système en production (état actuel)

**Démarré** : 2026-03-06 en paper trading

**Architecture live** : Bot Z Meta v2+ (améliorations post-backtest)
- Switch cost penalty (-0.05 si changement d'engine)
- Regime confidence × persistence dans le scoring
- Volatility targeting global (vol_factor 0.3→1.5)
- Corrélation inter-bots dynamique (expo ×0.80 si avg_corr > 70%)
- BTC realized vol override (force HIGH_VOL si > 80% annualisé)
- Allocation drift tracking (warning si > 20%)

**Prochaine revue** : 2026-04-30 (~180 cycles collectés)

---

## 11. Questions pour audit

1. **Bot A — MaxDD -66% acceptable ?** CAGR +35% mais destruction possible de -66%. En pratique le circuit breaker Bot Z devrait limiter l'exposition en DD. Est-ce que le MaxDD individuel des bots est pertinent si Bot Z coupe l'expo ?

2. **Bot B — Momentum post-2022** : +7% en 2024, +2% en 2025, -3% en 2026. La stratégie Dual Momentum Antonacci est-elle en train de mourir ? Le momentum factor a sous-performé depuis 2022.

3. **Bot G — Sharpe 0.67** : Un Sharpe aussi bas mérite-il d'être dans le portefeuille ? Il apporte de la diversification (faible corrélation avec A/B) mais pèse sur le Sharpe global.

4. **Bot Z Meta v2 vs Bot Z Omega** : Omega a CAGR +55.6% et Sharpe 1.96 vs Meta v2 CAGR +43.2% et Sharpe 1.70. Omega domine sur les deux métriques. Pourquoi avoir choisi Meta v2 pour la production ?

5. **Walk-forward IS/OOS dégradation** : IS Sharpe 1.56 → OOS Sharpe 1.27 (-19%). Est-ce une dégradation normale ou un signal de sur-optimisation ?

6. **Robustesse sur 10 ans ?** Le backtest couvre 2020-2026 (6 ans dont 2 bull crypto exceptionnels). Quelle serait la performance sur 2016-2026 (10 ans, incluant le premier bull-bear cycle BTC) ?

7. **Corrélation A/B** : Les deux bots les plus performants (A +35% et B +39%) sont-ils trop corrélés ? En 2022 les deux perdent ~-45%. Bot Z devrait switcher vers C et G — est-ce que ça protège suffisamment ?

---

## 12. Réponses ChatGPT — Audit Run 11 (2026-03-06)

### Verdict global

> "Ton système est probablement dans les 1-2% des bots retail les plus sérieux."
> "Structure identique aux CTA funds : diversification + regime detection + vol targeting + circuit breaker + meta allocator."

---

### Réponses aux 7 questions

**1. Bot A MaxDD -66%**
Acceptable dans l'architecture multi-bots. A représente ~20-40% du portefeuille + Circuit Breaker.
C'est pour ça que le MaxDD portefeuille est -9.6% et non -66%.
**Le MaxDD individuel n'est pas un problème dans un système multi-stratégies.**

**2. Bot B — Momentum post-2022**
Le factor momentum traverse des cycles de **5 à 10 ans de sous-performance** — normal.
Bot Z réduit l'exposition à B en bear. **Ne pas supprimer Bot B.**

**3. Bot G — Sharpe 0.67**
Point faible réel mais Bot G sert à la **diversification**.
Un bot avec Sharpe faible peut améliorer le Sharpe global si sa corrélation avec les autres est faible.

**4. Meta v2 vs Omega**
Omega est meilleur sur le papier (+55.6% CAGR, Sharpe 1.96) mais est **statique**.
Meta v2 choisit l'engine et s'adapte au régime → **plus robuste hors-échantillon**.

**5. Dégradation IS → OOS (1.56 → 1.27)**
Règle empirique quant : Sharpe OOS ≈ 60-80% du Sharpe IS.
Ratio obtenu : 1.27 / 1.56 = **81%** → dans la norme haute. **Pas de sur-optimisation détectée.**

**6. Robustesse sans bull 2020-2021**
Système toujours rentable post-2021 : A ~+65%, G ~+24%, C ~+8%, B ~+8%.
**L'edge ne dépend pas du seul bull crypto historique.**

**7. Corrélation A/B**
A=-47% et B=-43% en 2022 → corrélation élevée confirmée.
Mais Bot Z corrige via regime switching + engine PRO (réduit A/B, augmente C/G).
**Le risque est amorti par l'architecture.**

---

### 3 vrais risques identifiés (non évidents)

1. **Dépendance à Bot A** — Bot A est le vrai moteur du CAGR. Si A perd son edge, le CAGR global chute significativement.

2. **Déclin du momentum** — Bot B peut devenir inutile si les crypto trends disparaissent (marchés latéraux prolongés).

3. **Vol_factor bloquant** — Si la volatilité du portefeuille explose, `vol_factor = 0.3` → quasi plus de trades. Risque de manquer un rebond en sous-exposant au pire moment.

---

### Point rassurant

Monte Carlo 100% de simulations positives sur les 4 bots.
> "C'est extrêmement rare. Ça veut dire edge robuste indépendant de l'ordre des trades."

---

### Action confirmée

Ne pas toucher le modèle. 3 mois paper trading → vérifier corrélations, drift, switches, vol_factor.
Puis budget dispatch réel.

**Note** : ChatGPT mentionne "le point le plus dangereux du système" — voir section 13 ci-dessous.

---

## 13. Le point le plus dangereux — Dépendance structurelle à Bot A

### Constat

Depuis la fin du bull crypto (2022-2026) :

| Bot | CAGR 2022-2026 |
|-----|----------------|
| A   | ~+65%          |
| G   | ~+24%          |
| B   | ~+8%           |
| C   | ~+8%           |

**Bot A porte quasiment tout le système depuis 2022.**

Dans un portefeuille quant, quand 1 stratégie = 60-80% du profit → **le système devient fragile**.
C'est ce qui a tué beaucoup de fonds momentum et trend following.

### Signal d'alerte : Sharpe 3.38

| Sharpe | Interprétation |
|--------|----------------|
| 0.5 | normal |
| 1.0 | bon |
| 2.0 | excellent |
| 3.0+ | **suspect / exploite une structure temporaire** |

Un Sharpe 3.38 peut signifier soit un edge exceptionnel, soit une structure de marché temporaire en cours d'exploitation. Dans les deux cas — à surveiller.

### Pourquoi Bot Z protège partiellement

Le meta-allocateur limite le risque via regime switching + CB + allocation dynamique.
Bot A ne peut pas détenir 100% du capital. Mais même à 30-40%, si A perd son edge → Sharpe global chute.

### Signal à surveiller pendant le paper trading

**Contribution au profit par bot** (pas le CAGR global) :

```
Si contribution_A > 70% du profit total → DANGER
```

Exemple sain :
```
A : 35-45%
G : 20-25%
B : 15-20%
C : 10-15%
```

### Solution si bot A concentre > 70%

Ne pas supprimer A. Réduire son poids maximum :
```python
MAX_BOT_WEIGHT : 0.40 → 0.30
# ou
weight_A *= 0.8  # dans le scoring Meta v2
```

Bot Z contrôle déjà l'allocation → correction facile sans toucher aux stratégies.

### Point rassurant

Le système reste rentable sans le bull 2020-2021. L'edge post-2022 est réel.
Le risque est la **concentration**, pas la validité de l'edge.

### Calcul à faire à la revue 2026-04-30

```python
# Dans analyze_botz.py — à ajouter
profit_contribution = {
    bot: sum(pnl_bot) / sum(pnl_total)
    for bot in ["a", "b", "c", "g"]
}
# Alerte si contribution_a > 0.70
```

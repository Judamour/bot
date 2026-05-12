# Shadow Bot v2 — Concentrated High-Conviction Design

**Date** : 2026-05-12
**Author** : brainstorming session (claude opus 4.7)
**Status** : Draft → Review

## 1. Context & Motivation

### 1.1 Current state

Le shadow bot est un banc de test architectural parallèle au Bot Z prod. Architecture actuelle :

- Single-engine qui scanne tous les détecteurs (`supertrend`, `donchian`, `mean_reversion`, `momentum`, `trend_multi_asset`)
- Ranke les signaux par score composite 0-100
- Alloue le capital aux top-N (N=5)
- Tourne en parallèle sur un compte Alpaca paper isolé ($100K initial)
- Cycle 4h synchronisé sur la prod (heartbeats 03/07/11/15/19/23 UTC + 3 min)

### 1.2 Performance gap

Le backtest 3y du shadow (post-fix bugs dédup et pending_buys, commit `7803182`) donne :

| Métrique | Shadow v1 | Bot A solo | Bot Z PROD Meta v2 | Z v2 QualityScore |
|---|---|---|---|---|
| CAGR | **+3.5%** | +33% | +27% | +43% |
| Sharpe | 0.24 | 2.24 | 1.71 | 1.37 |
| MaxDD | -20% | -62% | -10% | -23% |
| Trades 3y | 171 | n/a | n/a | n/a |

Le shadow sous-performe la prod d'un facteur ~10x sur le CAGR. **Objectif v2 : battre Bot Z v2 QualityScore (+43% CAGR) en pur return**, en gardant l'identité single-engine scan-all.

### 1.3 Root cause analysis

Sources d'underperformance identifiées :

1. **Timeframe mismatch** : backtest sur daily, prod runner sur 4h → 2-3x moins de signaux
2. **Sous-sizing** : 1% risk-parity + 10% max-position vs Z 25% position-size → 4x moins de capital déployé par trade
3. **Pas de concentration** : top-5 equal-weight vs Z qui surpondère ses meilleurs sub-bots
4. **Pas de filtre subjectif** : pas de Claude AI veto comme prod (et user contrainte : pas de clés API LLM disponibles)
5. **Régime mou** : pénalité -10 score sur VIX>28 (cumulable, contournable), pas de hard cutoff comme prod SHIELD

## 2. Goals & Non-Goals

### 2.1 Goals

- **G1** : CAGR backtest 3y ≥ **30%** (gate de validation, vise 43%+)
- **G2** : Sharpe ≥ 1.0
- **G3** : MaxDD **mieux que -25%** (drawdown moins sévère, ex : -20% pass, -30% fail)
- **G4** : Comptabilité cohérente : `|sum(trade_pnl) − (final − initial)| < 1% × initial` (vérification anti-régression bug fix de ce jour)
- **G5** : Zéro appel LLM (contrainte budget : pas de clés API)
- **G6** : Garde l'identité shadow : single-engine scan-all + score composite

### 2.2 Non-Goals

- **NG1** : Ne PAS copier Bot Z (architecture multi-engine). Le shadow doit rester un challenger distinct.
- **NG2** : Ne PAS implémenter d'ML adaptive (bandit, RL) en v2. Réservé v3 si v2 plafonne.
- **NG3** : Ne PAS ajouter de nouveaux détecteurs en v2. On exploite les 5 existants mieux.
- **NG4** : Ne PAS toucher au système Bot Z prod (zéro impact prod, isolation préservée).

## 3. Design

### 3.1 5 leviers d'amélioration

| Levier | Avant (v1) | Après (v2) | Impact attendu |
|---|---|---|---|
| Timeframe backtest | daily | **4h** (align prod) | +signaux × 2-3, perf réaliste |
| Concentration | top-5 equal | **top-3 score-weighted** (30/20/15%) | +taille moy par trade × 2.5 |
| Quality gate | score ≥ 55 mou | **score ≥ 65 + MTF + volume + cooldown** (hard gates) | -30% mauvaises entrées |
| Régime | -10 score (mou) | **SHIELD hard** (VIX>30 ou bear+QQQ<SMA200) → no new entries | évite trades en crise |
| Coupe-circuit | aucun | **MaxDD halt** -15% / 7j | borne pire scénario |

### 3.2 Sizing par rang

Le sizing remplace `RISK_PER_TRADE_PCT × stop_dist` par une allocation directe en % du capital disponible, modulée par le rang dans le top-3 :

| Rang | % du cash dispo | Stop initial |
|---|---|---|
| 1 (best score) | 30% | entry − 1.5 × ATR |
| 2 | 20% | entry − 1.5 × ATR |
| 3 | 15% | entry − 1.5 × ATR |
| Total cycle | 65% max déployé | réserve cash 35% min |

**Note** : le cap MAX_POSITION_PCT (10% historique) est REMPLACÉ par les % par rang. La protection vient du fait qu'on traite à la fois un cycle = 3 trades max et 10 positions max ouvertes (cumul de plusieurs cycles).

### 3.3 Trailing stop adaptatif

```
Pour chaque position ouverte, à chaque cycle :
  pnl_pct = (close - entry) / entry
  if pnl_pct >= 0.05:                       # position gagnante > +5%
    atr_mult = 3.0                          # on laisse respirer
  else:
    atr_mult = 1.5                          # stop serré
  new_stop = close - atr_mult * ATR(14)
  if new_stop > current_stop:
    update_stop_order(new_stop)
```

Logique : on serre les pertes (1.5×ATR initial et tant que +5% n'est pas atteint), puis on lâche du mou (3×ATR) une fois que le profit est acquis. Asymétrie qui réduit MaxDD sans tuer les winners.

### 3.4 Hard Quality Gate (remplace Claude veto)

User contrainte : pas de clés API LLM. On remplace le Claude Haiku veto initialement proposé par 4 hard gates mécaniques. Top-3 candidats DOIVENT passer ces 4 gates, sinon skip :

| Gate | Règle | Implémentation |
|---|---|---|
| G1 Score plancher | `score ≥ 65` | comparaison directe |
| G2 MTF alignment | `sig.rationale["mtf_aligned"] == True` | déjà calculé par 4 détecteurs sur 5 — voir note ci-dessous |
| G3 Volume réel | `sig.rationale["volume_ratio"] ≥ 1.0` | déjà calculé par les 5 détecteurs |
| G4 Cooldown stop | `symbol not in risk_guard.cooldowns OR cooldowns[symbol] < now` | lookup dict |

**Pré-requis détecteurs** : la phase d'implémentation doit vérifier que les 5 détecteurs (`supertrend`, `donchian`, `mean_reversion`, `momentum`, `trend_multi_asset`) populent bien `rationale["mtf_aligned"]` et `rationale["volume_ratio"]`. Si manquant (ex : un détecteur où MTF n'a pas de sens), définir le comportement explicitement (auto-pass ou auto-fail), pas de silence.

Le cooldown G4 dure **5 jours après un stop loss** sur le même symbole. Empêche le revenge trading sur un signal qui vient de casser.

### 3.5 Régime SHIELD

```python
def shield_active(macro: dict) -> bool:
    vix = macro.get("vix", 18)
    btc_trend = macro.get("btc_trend", "bull")
    qqq_ok = macro.get("qqq_regime_ok", True)
    if vix > 30:
        return True
    if btc_trend == "bear" and not qqq_ok:
        return True
    return False
```

SHIELD actif → skip step "scan + new entries", on continue uniquement à gérer les positions existantes (trailing). C'est un **HARD GATE** au niveau cycle, pas un pénalité de score.

### 3.6 Risk Guard (MaxDD halt + cooldowns)

Module `shadow/risk_guard.py` qui maintient un state persistant :

```json
{
  "halt_until": null,                          // ISO datetime ou null
  "peak_equity": 99850.0,
  "peak_date": "2026-05-10",
  "cooldowns": {                               // symbole → fin cooldown ISO
    "NVDA": "2026-05-17T20:00:00Z"
  },
  "stop_events": [                             // 10 derniers stops (audit)
    {"sym": "NVDA", "ts": "2026-05-12T20:00:00Z", "pnl": -123.45}
  ]
}
```

API :
- `risk_guard.is_halted() -> bool` (consulté en début de cycle)
- `risk_guard.update_equity(equity, ts)` (à la fin de chaque cycle)
  - met à jour `peak_equity` si nouveau peak
  - si `equity < peak × 0.85` → set `halt_until = ts + 7d`
- `risk_guard.register_stop(symbol, pnl, ts)` (à chaque stop déclenché)
  - ajoute `cooldowns[symbol] = ts + 5d`
  - push dans `stop_events`
- `risk_guard.is_in_cooldown(symbol) -> bool` (consulté par quality_gate)

Persistance : `logs/shadow/risk_state.json`, sauvé après chaque update.

## 4. Components & file layout

```
shadow/
├── runner.py        ← MODIFIÉ : TOP_N=3, score-weighted sizing, hooks SHIELD/halt, trailing adaptatif
├── scorer.py        ← inchangé
├── strategies.py    ← inchangé (5 détecteurs)
├── broker.py        ← inchangé
├── quality_gate.py  ← NOUVEAU : 4 hard gates (score/MTF/vol/cooldown)
├── regime.py        ← NOUVEAU : detect SHIELD conditions
└── risk_guard.py    ← NOUVEAU : MaxDD halt -15% / 7j + cooldowns post-stop

backtest/
└── run_shadow.py    ← MODIFIÉ : timeframe 4h via data.fetcher, TOP_N=3, hooks gates/regime/risk_guard

logs/shadow/
└── risk_state.json  ← NOUVEAU : state persisté MaxDD halt + cooldowns
```

**Note backtest** : remplacer `yf.download(interval="1d")` par `data.fetcher.fetch_ohlcv(sym, "4h", days=N)` pour aligner sur la prod. Le module `data.fetcher` est déjà utilisé par `live/bot.py` et `backtest/multi_backtest.py`.

## 5. Data flow d'un cycle

```
Cycle 4h start (heartbeat 03/07/11/15/19/23 UTC + 3 min)
  │
  ├─ 1. Load Alpaca shadow account state
  │    → equity, cash, open positions, open buy orders (pending_buys)
  │
  ├─ 2. Load risk_guard state (logs/shadow/risk_state.json)
  │    → MaxDD halt actif ? cooldowns par symbole ?
  │    → si halt actif : skip step 5-8 (gestion positions seulement)
  │
  ├─ 3. Fetch macro context : VIX, BTC trend, QQQ vs SMA200
  │    → regime.shield_active(macro) ?
  │    → si SHIELD : skip step 5-8 (gestion positions seulement)
  │
  ├─ 4. Fetch OHLCV 4h (60j, signaux principaux) + 1d (220j, MTF + SMA200 régime QQQ)
  │
  ├─ 5. Manage existing positions : trailing stop adaptatif (1.5 ou 3×ATR selon gain)
  │    → détection stop déclenché : comparer `meta.json` (cycle précédent) vs
  │      `broker.get_positions()` (cycle actuel). Si un symbole présent en meta
  │      mais absent des positions Alpaca → stop touché entre-temps. PnL réalisé
  │      via `broker.get_account()` history ou via dernier prix marché ×
  │      qty connue dans meta. → risk_guard.register_stop(sym, pnl, ts).
  │      Nettoyer l'entrée orpheline dans meta.
  │
  ├─ 6. Scan signaux (symboles sans position et sans pending_buy)
  │    → dedup intra-cycle par symbole (best score), sort desc
  │
  ├─ 7. Pour top-3 candidats :
  │    a. quality_gate.passes(sig, risk_guard) ? sinon : skip
  │    b. size = cash_available × WEIGHT_BY_RANK[rang] (30/20/15%)
  │    c. broker.market_buy + place stop initial 1.5×ATR
  │
  └─ 8. risk_guard.update_equity(new_equity, ts)
       Log decisions + equity snapshot
```

**Principe** : les checks défensifs (halt + SHIELD) sont en early-return, ce qui garantit que les positions existantes sont toujours gérées (trailing) même en mode défensif — on coupe juste les NEW entries.

## 6. Error handling

Defensive, not destructive. Un échec partiel ne tue jamais un cycle entier.

| Échec | Comportement | Logique |
|---|---|---|
| `broker.get_account()` fail | Skip cycle | Pas de décision sans état authoritative |
| `broker.get_open_orders()` fail | Skip cycle (return) | Évite doublons (fix bug du jour, déjà en place) |
| Macro fetch fail | Assume neutre (pas de SHIELD) | Log warning, continue |
| OHLCV fetch fail (1 symbole) | Skip ce symbole | Log, continue avec les autres |
| Détecteur lève exception | Skip ce sig | Log nom détecteur+sym, continue |
| `risk_state.json` corrompu | Reset (fresh state, peak=equity actuelle) | Log alert, ne bloque pas le cycle |
| `broker.market_buy` fail | Skip ce sig, passe au suivant rang | Log, ne consomme pas le slot |
| Place stop fail après fill OK | Log alert, position sans stop (audit) | Sera replacé au cycle suivant via trailing |

## 7. Testing

### 7.1 Unit tests

| Test | Cible | Fixtures |
|---|---|---|
| `test_regime.py` | Truth table VIX/BTC/QQQ → SHIELD bool (4 cas) | mock macro dict |
| `test_quality_gate.py` | Chacune des 4 gates indépendamment + combinaisons | mock Signal + risk_guard |
| `test_risk_guard.py` | MaxDD halt déclenche à -15%, expire à +7j | mock equity timeline |
| `test_risk_guard.py` (cooldown) | register_stop ajoute cooldown, is_in_cooldown expire à +5j | mock datetime |
| `test_sizing.py` | top-1=30%, top-2=20%, top-3=15%, cash résiduel ≥35% | inline |

### 7.2 Integration test

`python backtest/run_shadow.py` doit produire :
- Sortie identique au format actuel (`metrics`, `by_strategy`, `equity_curve_monthly`)
- Logs des halts/SHIELD/cooldowns activations
- Critère de viabilité (gate pour passer en prod) :
  - CAGR 3y ≥ **30%** (vise 43%+)
  - Sharpe ≥ 1.0
  - MaxDD **mieux que** -25% (ex : -20% pass, -30% fail)
  - Comptabilité : `|sum(trade_pnl) − (final − initial)| < 1% × initial`

### 7.3 Smoke prod

Après deploy VPS :
- `sudo systemctl restart shadow`
- Vérifier dans `logs/shadow/shadow.log` : `(dédup, SHIELD=X, halt=Y, pending_buys=Z)` au prochain cycle
- Vérifier `logs/shadow/risk_state.json` créé avec peak_equity = equity actuelle Alpaca

### 7.4 Paper live validation

- Tourner 30 jours minimum sur compte Alpaca shadow paper isolé
- Comparer equity_curve live vs backtest sur même fenêtre temporelle (max 20% d'écart attendu)
- Vérifier zéro drift entre `broker.get_positions()` et `meta.json` local (>5% = bug)
- Si CAGR live extrapolé < 25% sur 30j : revue, possiblement abandon v2 et passage v3 (ML bandit ou Bot A clone)

## 8. Rollout

1. Implémenter v2 sur branche `shadow-v2` (worktree isolé)
2. Backtest 3y, valider gate G1-G4
3. Si gate validée : merge sur `main`, commit, push (CI/CD déploie sur VPS)
4. Restart `shadow.service` sur VPS
5. Smoke test : vérifier logs du 1er cycle post-deploy
6. Observer 30 jours live shadow
7. Si validation paper OK : décider migration ou non vers prod (orthogonal à ce design)

## 8.bis Constants tunables (consolidées)

Toutes les constantes paramétrables doivent vivre dans un seul bloc en tête de `shadow/runner.py` (ou un nouveau `shadow/config_v2.py` si on préfère séparer) pour éviter les magic numbers dispersés :

```python
# Tunables v2
SCORE_FLOOR        = 65          # G1 quality gate
COOLDOWN_DAYS      = 5           # G4 cooldown post-stop
HALT_DD_PCT        = -0.15       # MaxDD halt threshold
HALT_DURATION_DAYS = 7           # durée halt après déclenchement
WEIGHT_BY_RANK     = [0.30, 0.20, 0.15]   # sizing par rang du top-3
TOP_N_SIGNALS      = 3           # concentration
ATR_MULT_STOP_INIT = 1.5         # stop initial
ATR_MULT_TRAIL     = 3.0         # trailing après +5% gain
PROFIT_LOOSEN_PCT  = 0.05        # seuil pour passer trailing tight → loose
VIX_SHIELD_THRESHOLD = 30.0
```

Le backtest et le runner DOIVENT lire ces mêmes constantes (import partagé) pour garantir cohérence backtest ↔ prod.

## 9. Risks & mitigations

| Risque | Mitigation |
|---|---|
| Backtest plafonne <30% CAGR | Revue : tester hybride avec leverage 1.5× sur top-1 (alt B). Si toujours <30% : passer à v3 (réservé, hors scope v2) |
| Hard quality gate trop strict → zéro signal | Logger combien de signals échouent par gate. Ajuster score plancher si nécessaire (65 → 60) |
| Cooldown 5j trop long sur marché volatile | Paramétrable, tester 3/5/7 en backtest |
| Sizing par rang trop concentré → MaxDD > 25% | Réduire 30/20/15 → 25/18/12 si MaxDD critique en backtest |
| Bug régression sur la comptabilité (cf bug du jour) | Test G4 explicite dans validation backtest |

## 10. Open questions

- **Q1** : si les 3 candidats du cycle passent tous les hard gates, alloue-t-on toujours 30/20/15% ou ajuste-t-on selon le delta de score (ex: si score1=90 et score2=66, le sizing devrait-il refléter plus de conviction sur le 1) ? **Réponse v2** : non, on garde flat 30/20/15 pour simplicité, à raffiner si CAGR atteint.

- **Q2** : que faire si la 1ère position du jour tape son stop dans la même session 4h ? Compte-t-on ça comme un cooldown (5j) ou exception ? **Réponse v2** : cooldown standard. Pas d'exception, prédictibilité prime.

- **Q3** : la persistance `risk_state.json` est lue chaque cycle ; doit-on backup périodique pour éviter corruption ? **Réponse v2** : write-temp-then-rename pour atomicité, pas de backup explicite (un reset fresh est acceptable).

- **Q4** : `logs/shadow/` est-il dans `.gitignore` ? **Réponse v2** : OUI (cf `.gitignore` racine, `logs/` est ignored). Donc `risk_state.json` survit aux `git pull` côté VPS. Vérifier en phase impl.

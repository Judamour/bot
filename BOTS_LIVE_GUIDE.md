# Bots Trading — Guide live deployment & subtilités

Document de référence pour passer **Bot Z** et **Shadow** en live trading sans surprise.
Mise à jour: 2026-05-13 (post session iter-1 à iter-8 + fix AVAX).

> Tous les "subtilités" listés ici ont été découverts en paper trading. Chaque
> point est un bug observé ou un edge case qui aurait coûté de l'argent en live
> s'il n'avait pas été corrigé en amont.

---

## 1. Les deux bots

### Bot Z — Prod (paper actuel, futur live)
- **Pilote**: `live/bot_z.py` + `live/multi_runner.py`
- **Logique**: Multi-bot dispatcher (4 sub-bots A/B/C/G + Meta v2 engine BULL/BALANCED/PARITY/SHIELD)
- **Mode actuel**: QualityScore (`QUALITYSCORE_MODE=True` dans `bot_z_omega.py`) — bypass switching, BALANCED continu
- **Capital**: `INITIAL_CAPITAL_PER_BOT` synchronisé depuis `/v2/account` Alpaca au boot
- **Cycle**: 4h main + monitor 15min
- **Symbols**: 21 actifs (5 crypto + 16 stocks/ETF)
- **Broker**: Alpaca paper (`APCA_API_BASE_URL` détermine paper/live)
- **Performance backtest 3y QualityScore**: CAGR +43% / Sharpe 1.37 / MaxDD -22.8%

### Shadow — Banc de test alternatif
- **Pilote**: `shadow/runner.py`
- **Logique**: Single engine, ranks signaux par score composite, top-2 sizing-weighted
- **Cycle**: 4h main + monitor 15min (porté depuis Z)
- **Symbols**: 21 actifs (même que Z mais broker ISOLÉ)
- **Broker**: Alpaca paper sur compte **distinct** (`ALPACA_SHADOW_*` env vars)
- **Performance backtest** (avec real SHIELD):
  - Bull 3y (2023-2026): **+41.7%** CAGR / Sharpe **1.40** / MaxDD **-15.5%**
  - Bear-inclus 4y3m (2022-2026): **+2.8%** CAGR / Sharpe **0.56** / MaxDD **-8.1%**

### Comparaison Z vs Shadow (backtest réaliste)

| Métrique | Bot Z QualityScore | Shadow iter-8 |
|---|---|---|
| Bull 3y CAGR | +43% | **+41.7%** |
| Bull Sharpe | 1.37 | **1.40** ✓ |
| Bull MaxDD | -22.8% | **-15.5%** ✓ |
| Bear-inclus CAGR | non mesuré | **+2.8%** |
| Bear-inclus MaxDD | non mesuré | **-8.1%** |

Shadow tient tête à Z en CAGR/Sharpe avec un **meilleur profil de risque** (MaxDD nettement réduit).

---

## 2. ⚠️ SUBTILITÉS CRITIQUES À RESPECTER EN LIVE

### 2.1 Fee crypto en base asset (incident AVAX 2026-05-13, -€494)

**Le bug**: quand tu achètes une crypto sur Alpaca, la fee 25bps est déduite **en base asset** (pas en cash):
```
BUY 1479.12 AVAX (notional $14,648)
→ Alpaca prélève fee 25bps = 3.69 AVAX en base
→ Tu reçois 1476.03 AVAX (PAS 1479.12)
```

Si le bot stocke `position.size = 1479.12` (la qty NOMINALE), tout SELL/STOP ultérieur demande 1479 → Alpaca répond `403: insufficient balance, available 1476.03` → ordre rejeté → position non protégée.

**Triple barrière déployée** (commits `3365af5` + `e5a41b7`):

1. **BUY post-fill clamp** (`live/alpaca_executor.py:execute_buy` + `shadow/broker.py:market_buy`):
   ```python
   if is_crypto:
       time.sleep(0.5)  # propagation Alpaca
       broker_qty = _fetch_position_qty(symbol)
       if broker_qty < filled_qty:
           filled_qty = math.floor(broker_qty * 1e6) / 1e6  # 6 décimales
   ```
   → la qty enregistrée dans le state matche IMMÉDIATEMENT le broker.

2. **SELL preemptive clamp** (`execute_sell`):
   ```python
   if is_crypto:
       broker_qty = _fetch_position_qty(symbol)
       if size > broker_qty:
           size = math.floor(broker_qty * 1e6) / 1e6
   ```
   → safety net si le state aurait dérivé pour une autre raison.

3. **STOP reactive retry** (`place_stop_loss`, `replace_stop_loss`): parse l'erreur `insufficient balance for X (requested: A, available: B)` et retry avec qty B. Already existed pre-session.

### 2.2 Initial stop pas placé sur stocks queued (incident shadow 2026-05-13)

**Le bug**: stocks placés via BUY hors marché US (avant 13:30 UTC) → `status=queued` → fill à market open → trailing block du runner suivant calcule `new_stop = close - 4×ATR` MAIS le price a baissé entre submission et fill → `new_stop < old_stop` (stop initial planifié) → guard `if new_stop > old_stop` **ne fire pas** → **aucun stop server-side placé**.

Résultat: 7 stocks shadow (CRWD/GOOGL/KO/LLY/NVDA/QQQ/SPY) sans stop pendant 4-12h.

**Fix déployé** (commit `083ccec`): détection `stop_order_id is None` → force placement avec anchor `entry - ATR_MULT_STOP_INIT × ATR`. Code dans `shadow/runner.py` bloc trailing.

### 2.3 Drift state vs broker après SELL fail (cas AVAX)

**Le bug**: le bot tente un SELL → fail (insufficient balance) → log "position maintenue" → le cycle suivant, le bot détecte le stop hit côté state mais le SELL avait DÉJÀ retry et fillé... ou pas. Soit:
- Le bot supprime la position du state (croit fermé)
- Mais le broker garde la position ouverte
- → ORPHAN position sans surveillance

**Fix déployé** via:
1. La triple barrière crypto (2.1) qui empêche la source du problème
2. Stop monitor 15min (`reconcile_broker_stop`) qui détecte les orphan et place un stop

### 2.4 Stops "day" expirent overnight (stocks)

**Comportement Alpaca**: stop orders sur fractional stocks utilisent `time_in_force=day` (sinon Alpaca rejette). Le stop expire à market close (20:00 UTC) et la position est non-protégée jusqu'au prochain placement.

**Fix**: monitor 15min détecte `status in ("expired", "canceled", "rejected")` → re-place automatiquement. Code dans `_reconcile_stops_once` (shadow) et `reconcile_broker_stop` (Z, `live/order_executor.py:336`).

### 2.5 Cycle 4h insuffisant pour réagir aux fills entre cycles

**Le bug**: position fillée à 13:31 UTC (open marché US), prochain main cycle à 15:03 UTC = 1h32 d'exposition potentiellement sans stop si trailing block fail.

**Fix**: thread `_stop_monitor_loop` tournant toutes les **15 min** en parallèle du main 4h (commit `4fb7499` pour shadow, déjà existant sur Z). Fait:
- **ADOPT**: place un stop sur position orpheline
- **RENEW**: re-place stop expired/canceled/rejected
- **FILLED**: détecte stop fired par broker, register cooldown

### 2.6 SHIELD doit utiliser VIX réel (pas stub)

**Le bug**: dans `backtest/run_shadow.py` pré-iter-5, VIX était hardcodé à 18 → SHIELD ne fired JAMAIS → +43.7% CAGR optimiste (irréaliste).

**Fix iter-5**: fetch VIX réel via yfinance `^VIX` dans le backtest. Live runner utilise déjà `fetch_macro_context()` qui pull VIX réel via yfinance.

**Conséquence importante**: le CAGR live attendu est ~+27% (Z PROD measured), pas +43%. Les chiffres backtests sans real SHIELD étaient gonflés.

### 2.7 Hystérésis equity_bear (anti-whipsaw 2022 dead-cat bounces)

**Le bug**: en 2022, SPY a brièvement repassé > SMA200 en avril, août, novembre. Sans hystérésis, shadow flippait `equity_bear=False` → scan full universe → entrée tech qui rechute → -$132 cumulé sur 2022.

**Fix iter-5** (`shadow/regime.py:equity_bear_active`):
```python
if not qqq_regime_ok:
    return True          # bear: SPY < SMA200
if not qqq_full_uptrend: # SPY > SMA50 > SMA200 requis pour exit
    return True          # sticky exit (anti-whipsaw)
return False
```

---

## 3. Architecture trailing stop — Chandelier Exit

**Bot Z et Shadow utilisent le même algorithme** (porté de Z vers Shadow iter-6 #5, commit `3365af5`):

```python
# Chandelier Exit (LeBeau 1992)
chandelier_high = df["high"].tail(22).max()  # ~ 1 mois daily ou 3-4j en 4h
atr_mult = ATR_MULT_TRAIL if pnl_pct >= 0.05 else ATR_MULT_STOP_INIT
new_stop = chandelier_high - atr_mult * atr  # ATR_MULT_STOP_INIT=4.0, ATR_MULT_TRAIL=5.0

if new_stop > old_stop:  # monotone — ne descend jamais
    broker.replace_stop(stop_order_id, new_stop)  # PATCH /v2/orders/{id}
```

**Pourquoi Chandelier vs close-anchored**: tracking the recent HIGH lock les profits plus tôt sur les extensions. Effet mesuré shadow: **+17 pts CAGR vs close-anchored** sur 3y bull.

**PATCH semantic** (commit `3365af5`): updates stop level via Alpaca PATCH endpoint (single API call) au lieu de cancel+create (fenêtre de vulnérabilité entre les deux + 2× plus d'appels).

---

## 4. Audit logging — décisions traçables

**Shadow** (iter-8, commit `79e901c`) loggue maintenant le même niveau de détail que Bot Z. Fichier: `logs/shadow/decisions.jsonl`.

**Events disponibles**:
```
regime          → contexte macro chaque cycle (VIX/BTC/SHIELD/equity_bear)
signal          → chaque signal détecté avec score + rationale + passed_floor
gate_reject     → rejet quality_gate avec code G1_score/G2_mtf/G3_volume/G4_cooldown
sector_reject   → rejet diversification avec sector
scan_summary    → totaux cycle (evaluated/fired/passed/accepted)
scan_skip       → cycle skipped (SHIELD/HALT actif)
scan_skip_held  → symbole déjà ouvert ou pending
entry           → BUY exécuté avec rank/notional/score
trail / init    → trailing stop placé (PATCH ou create)
stop_adopt      → monitor place stop manquant
stop_renew      → stop renouvelé après expire/cancel
stop_filled     → stop fired par broker
macro_take_profit → exit forcé en SHIELD/HALT à +15%
exit_detected   → position disparue entre cycles
detector_error  → exception détecteur
```

**Requêtes utiles** pour post-mortem:
```bash
# Pourquoi shadow n'a pas acheté NVDA aujourd'hui?
jq -c 'select(.symbol == "NVDA")' logs/shadow/decisions.jsonl | tail -20

# Distribution des rejets quality gate
jq -r 'select(.kind == "gate_reject") | .reason' logs/shadow/decisions.jsonl | sort | uniq -c

# Combien de fois SHIELD a fired ce mois
jq -c 'select(.kind == "regime" and .shielded)' logs/shadow/decisions.jsonl | wc -l

# Score moyen des signaux fired
jq -r 'select(.kind == "signal") | .score' logs/shadow/decisions.jsonl | awk '{s+=$1;n++} END {print s/n}'
```

**Bot Z** loggue dans `logs/signals.jsonl` (4000+ entries cumulées). Mêmes principes.

---

## 5. Checklist passage en LIVE — vérifications obligatoires

Avant de basculer `APCA_API_BASE_URL` du paper au live, exécuter:

### A. Vérifier que tous les fix sont déployés

```bash
ssh ubuntu@51.210.13.248 "cd /home/botuser/bot-trading && git log --oneline -30 | grep -iE 'fix|fee|clamp|monitor|chandelier'"
```

Doit contenir au minimum:
- ✓ `e5a41b7` post-fill clamp on BUY crypto
- ✓ `3365af5` chandelier exit + SELL insufficient balance
- ✓ `083ccec` initial stop on positions without stop_order_id
- ✓ `4fb7499` port 15-min stop-monitor thread (shadow)
- ✓ `c2989e3` constants v2 wired
- ✓ `a5708d3` runner v2 wire (shadow)

### B. Tester les protections sur paper

```bash
# 1. Triple barrière crypto active?
ssh ubuntu@51.210.13.248 "sudo grep -E 'clamp|insufficient balance' /home/botuser/bot-trading/logs/bot.log | tail -10"
# Doit voir: "[ALPACA] BUY X post-fill clamp Y→Z (fee crypto)" sur chaque BUY crypto

# 2. Monitor 15min tourne?
ssh ubuntu@51.210.13.248 "sudo grep 'STOP-MONITOR' /home/botuser/bot-trading/logs/shadow/shadow.log | tail -3"
# Doit voir "[STOP-MONITOR] démarré (interval 900s)" + un check toutes 15min

# 3. Stops server-side sur 100% des positions?
ssh ubuntu@51.210.13.248 "sudo -u botuser /home/botuser/bot-trading/venv/bin/python3 -c '
import sys; sys.path.insert(0, \".\")
from dotenv import load_dotenv; load_dotenv(\"/home/botuser/bot-trading/.env\")
from live import alpaca_executor
pos = alpaca_executor._request(\"GET\", \"/v2/positions\")
orders = alpaca_executor._request(\"GET\", \"/v2/orders?status=open&limit=200\")
stop_syms = {o[\"symbol\"].replace(\"/\",\"\") for o in orders if o.get(\"type\") in (\"stop\",\"stop_limit\")}
unprotected = [p[\"symbol\"] for p in pos if p[\"symbol\"].replace(\"/\",\"\") not in stop_syms and float(p.get(\"qty\",0)) > 1e-5]
print(f\"Positions: {len(pos)}, Stops: {len(stop_syms)}, Unprotected (excl. dust): {unprotected}\")
'"
# Doit retourner: "Unprotected (excl. dust): []"
```

### C. Variables d'environnement à check

```bash
ssh ubuntu@51.210.13.248 "sudo grep -E 'PAPER_TRADING|APCA_API_BASE_URL|ALPACA_SHADOW' /home/botuser/bot-trading/.env"
```

Pour passage LIVE Bot Z:
- `PAPER_TRADING=true` (vestige Kraken, sans effet sur Alpaca)
- `APCA_API_BASE_URL=https://api.alpaca.markets` (live) au lieu de `https://paper-api.alpaca.markets`
- `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` = clés LIVE Alpaca (différentes du paper)
- `ALPACA_SHADOW_*` = inchangé (shadow reste paper)

### D. Cooldowns / state propre

```bash
# Pas de positions phantom en state?
ssh ubuntu@51.210.13.248 "sudo grep -E 'PnL|CLOSE' /home/botuser/bot-trading/logs/bot.log | tail -5"
# Vérifier qu'aucun "position maintenue" récent (= drift potentiel)
```

### E. Backtest récent qui valide les params

```bash
cd "Developpement REACT/bot trading"
python backtest/run_shadow.py 2>&1 | grep -E "CAGR|Sharpe|Max Draw"
# Bear-inclus doit donner: CAGR ~+2.8%, Sharpe ~0.56, MaxDD ~-8.1%
```

---

## 6. Risques résiduels connus

Documentation honnête de ce qu'on ne sait pas couvrir.

### Long-only — pas de gain en bear pur
Shadow et Z gagnent en bull et survivent en bear (rotation défensifs + cash). Ils ne **profitent pas** activement d'un crash. Pour ça il faudrait shorts ou inverse ETF. **Décision**: laisser long-only — testé SQQQ/SH et TLT en iter-6/7, n'ont pas marché.

### Volatility decay sur leveraged products
Si un jour on ajoute SQQQ/SOXL/etc., conscience que ces ETFs perdent ~95% sur 5 ans même si le sous-jacent est flat. Trailing stops + cooldowns limitent l'exposure window mais ne suppriment pas le decay.

### Score n'est pas prédictif du PnL
Testé iter-2: SCORE_FLOOR 65→70 a perdu -14.6 pts CAGR. Le score actuel est heuristique (ADX/volume/RSI/MTF/edge_bonus) mais ne corrèle pas linéairement avec le PnL futur. **Ne pas tuner aveuglément**.

### MaxDD réel peut excéder le backtest
Le backtest 2022-2026 inclut le pire bear récent. Mais 2008 GFC (SPY -57%) ou 1973-74 (SPY -48%) sont hors échantillon. **Le risk guard halt à -15% rolling DD protège mais pas garantit**.

### Réliance sur 1 broker (Alpaca)
Si Alpaca downtime, les deux bots sont aveugles. Pas de failover.

---

## 7. Commits clés de la session iter-1 → iter-8 (2026-05-13)

Chronologique. Tous sur `origin/main`.

```
b2adc24 iter-1 tuning — drop bleeders + concentrate sizing
5236bbd iter-3 — TOP_N=2 + stops 4.0/5.0 (bat Z baseline)
a5708d3 runner v2 wire (full v2 stack)
c55da00 iter-4 — defensive rotation in equity_bear
db4c95f iter-5 — real macro + hystérésis + drop momentum
8a54359 cache OHLCV (4.2× faster iterations)
083ccec FIX initial stop bug (queued orders)
c2989e3 push missing constants for runner imports
3365af5 chandelier exit + fix Z SELL insufficient balance
2e051c2 iter-6 final — chandelier + diversif + macro-exits
4fb7499 port 15-min stop-monitor thread (parity with Z)
bfc650e iter-7 cleanup — drop SQQQ/SH dead code (long-only)
79e901c iter-8 full audit logging (parity with Z signals.jsonl)
e5a41b7 fix(crypto): post-fill clamp on BUY — anti-fee mismatch
```

---

## 8. Architecture finale shadow (iter-8, état au 2026-05-13)

```
Détecteurs actifs    : trend_multi_asset + donchian (long-only)
                      (supertrend/momentum/mean_reversion/inverse_bear droppés après A/B)
Univers              : 21 actifs (5 cryptos + 16 stocks/ETF)
Cycle main           : 4h synchronisé sur 03/07/11/15/19/23 UTC +3min
Cycle monitor        : 15min daemon thread (ADOPT/RENEW/FILLED detection)
Sizing               : score-weighted top-2 [.60, .30] × size_factor (0.5 en bear)
Diversification      : max 1 position par secteur (8 secteurs définis)
Trailing             : Chandelier 22-bar high - ATR×(4.0 init / 5.0 trail loose >5%)
Stop update          : PATCH /v2/orders/{id}, fallback cancel+create
Quality gate         : G1 score≥65 + G2 MTF + G3 volume≥1 + G4 cooldown
Risk guard           : MaxDD -15% halt 7j + cooldown 5j par symbole
SHIELD regime        : VIX>35 ou (btc bear + qqq < SMA200) → no entries
Equity bear regime   : SPY < SMA200 → rotation défensifs (7 actifs), sizing × 0.5
                      sortie avec hystérésis: SPY > SMA50 > SMA200 requis
Macro context        : real VIX (yfinance ^VIX) + real BTC trend (SMA200)
                      + SPY full uptrend (SMA50/200)
Macro exit forcé     : si SHIELD/HALT + pnl ≥ +15% → close au market
Crypto fee handling  : triple barrière (BUY post-fill clamp / SELL preemptive / STOP retry)
Persistence          : logs/shadow/{meta,risk_state}.json (atomic write)
Audit                : logs/shadow/decisions.jsonl (15+ event types, JSON Lines)
Cache backtest       : backtest/cache/ pickle 6h TTL, 4.2× speedup
```

---

## 9. À NE PAS oublier en routine

1. **Surveiller equity drift** : si `z_capital` dans `logs/bot_z/state.json` dérive >5% de l'equity Alpaca réelle, investiguer (le drift fix `_apply_z_budget` a été déployé iter-5 mais pas garanti à 100%).
2. **Renouveler token Claude OAuth** : cron auto-refresh tourne mais vérifier `/home/ubuntu/token_refresh.log` mensuellement.
3. **Backup state.json avant chaque modif manuelle** : `cp logs/bot_z/state.json logs/bot_z/state.json.bak_$(date +%Y%m%d)`.
4. **Garder `.env` hors git** : déjà dans `.gitignore` mais double-check.
5. **Ne JAMAIS commit `ALPACA_API_KEY` ni `ALPACA_SHADOW_API_KEY`** : ce sont les clés LIVE.

---

*Dernière mise à jour: 2026-05-13. Auteur: session pair-prog (Bot Trading + Claude). Pour update, modifier ce fichier et commit.*

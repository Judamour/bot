# Polymarket CopyTrade Paper Bot — Design

**Date** : 2026-05-15
**Status** : Design approved by user, pending implementation plan
**Owner** : damoria

## Goal

Paper-trade replication of 3 consistently-profitable Polymarket wallets to validate a copytrading strategy without capital risk. After a 30-day evaluation window meeting defined success criteria, decide whether to capitalize and go live.

## Non-goals (v1)

- Live trading on Polymarket (deferred; geoblock + funding to solve separately)
- Polymarket account creation / proxy wallet provisioning
- Submitting orders via the CLOB API
- VPN / VPS region migration
- Dynamic target wallet selection from the leaderboard
- Alpha generation via LLM research (this was Bot P v1, deprecated)

## Context

The French VPS cannot access Polymarket signup/CLOB write endpoints without a VPN, but the **public Data API** (`https://data-api.polymarket.com/`) and **Gamma API** are accessible — read-only operations are not geoblocked. A paper bot needs only read access, so v1 ships with zero geoblock work.

A prior Bot P (Mar 2026) used an LLM research pipeline (Scanner → Classifier → Researcher → Forecaster → Risk) on Polymarket markets. Result was 4 paper trades, then the daemon was stopped (`bot-p-v2.service` currently in failed state). The LLM approach is not being resurrected — the new bot is a copytrader of identified top wallets.

Three target wallets were identified by querying the Polymarket leaderboard API (`lb-api.polymarket.com/profit?window=All|30d|7d`) and selecting wallets present in ≥2 windows (sustained edge, not one-shot luck):

| Pseudonym | Wallet | All-time | 30d | 7d |
|---|---|---|---|---|
| RN1 | `0x2005d16a84ceefa912d4e380cd32e7ff827875ea` | #4 ($9.0M) | #7 ($1.0M) | #8 ($356K) |
| bossoskil1 | `0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a` | — | #1 ($3.0M) | #3 ($1.0M) |
| surfandturf | `0x9f2fe025f84839ca81dd8e0338892605702d2ca8` | — | #2 ($2.1M) | #10 ($276K) |

## Architecture

Standalone systemd service (`bot-cp.service`) running independently of Bot Z, Shadow, and Bot P. Same architectural pattern as the existing Shadow bot (single-process polling loop, JSONL state, isolation by design).

```
bot-cp.service (systemd, French VPS, no VPN)
└── live/copytrade/runner.py — polling loop, 60s tick
    ├── targets.py          ← hardcoded 3 wallets + metadata
    ├── data_api.py         ← Polymarket Data API client (read-only)
    ├── aum_estimator.py    ← target portfolio value at a given timestamp
    ├── paper_portfolio.py  ← simulated cash, positions, MTM
    ├── state.py            ← persist/restore last_seen_ts, portfolio
    └── notifier.py         ← Telegram alerts (reuse live/notifier.py)
```

No write access to any broker. No private keys. No outbound traffic to Polymarket CLOB. The bot only reads public data and writes to local JSONL files.

## Components

### `targets.py`
Hardcoded `TARGETS` list of dicts: `{pseudonym, wallet, allocation_pct}`. v1 splits 33/33/33; allocation is a const, not derived.

### `data_api.py`
Thin wrapper around three Polymarket Data API endpoints:
- `GET /trades?user={wallet}&limit=N` — recent trades
- `GET /positions?user={wallet}` — open positions with `currentValue`, `curPrice`, `size`
- `GET /value?user={wallet}` — total portfolio value (sanity check vs sum of positions)

Headers required: `Origin: https://polymarket.com`, `Referer: https://polymarket.com/`, `User-Agent: Mozilla/5.0`. Without them, requests 403.

Exponential backoff on 429/5xx. Per-wallet rate limit budget tracked locally.

### `aum_estimator.py`
Given a wallet and a target timestamp, return estimated AUM in USD.

```
aum(wallet, ts) = cash_usdc_at(wallet, ts) + Σ position.size × price_at(market, ts)
```

For v1, approximate with current snapshot (positions at fetch time, not historical). The drift is acceptable for paper because target wallets we follow have AUM in $100K-$10M range — a few minutes of price drift on micro-positions is <1% error on the ratio.

Cached 60s per wallet to avoid hammering the API.

### `paper_portfolio.py`
Per-wallet simulated portfolio:
```
{
  "wallet1": {
    "cash_usd": 333.33,
    "positions": [
      {"conditionId": "0x...", "asset": "...", "title": "...",
       "outcome": "Yes", "side": "BUY", "size": 12.5, "avg_price": 0.45,
       "cost_usd": 5.62, "opened_ts": 1775..., "target_trade_hash": "0x..."}
    ],
    "realized_pnl_usd": 0.0
  },
  "wallet2": {...},
  "wallet3": {...}
}
```

MTM computed by fetching current prices from Polymarket CLOB price endpoint (public). Equity per wallet = cash + Σ size × cur_price.

### `runner.py`
Main loop:

```
boot:
  load_state()
  smoke_test_data_api()
  log "bot-cp started, 3 wallets, capital $1000"

every 60s:
  for w in TARGETS:
    new_trades = data_api.trades(w.wallet, since=state.last_seen[w])
    for t in sorted(new_trades, by=timestamp ASC):
      if t.hash in already_processed: skip
      aum = aum_estimator.aum(w.wallet, t.timestamp)
      trade_pct = min(t.size_usd / aum, 0.50)
      paper_size = trade_pct * paper_capital_per_wallet
      if paper_size < 1.0: log_skip(); continue
      if t.side == "BUY":
        paper_portfolio.buy(w, t.market, t.outcome, t.price, paper_size, t.hash)
      else:  # SELL
        frac = t.size_outcomes / target_position_size_at(w, t.market, t.timestamp)
        paper_portfolio.sell(w, t.market, fraction=frac, price=t.price, t.hash)
      log_decision(w, t, paper_size, rationale)
      state.last_seen[w] = max(state.last_seen[w], t.timestamp)
  mtm_all_positions()
  if today changed: append_equity_snapshot()
  save_state()
```

### `state.py`
Two files:
- `logs/copytrade/state.json` — `{last_seen_ts: {wallet: ts}, processed_hashes: [...]}`
- `logs/copytrade/portfolio.json` — paper portfolio (above)

Atomic writes (write tmp, rename) to avoid corruption on crash.

### `notifier.py`
Reuse `live/notifier.py` Telegram client. Emit:
- Bot start (with wallet list + capital)
- Each copy trade (size, market title, target pseudonym, side)
- Hourly heartbeat with current per-wallet equity
- Daily summary at 18h UTC (PnL today, total trades, top winner / loser market)
- Error rate > 10/hour → alert
- AUM dropped to 0 on a target wallet → "target wallet inactive" alert

## Data flow

```
Polymarket Data API
        │
        │  GET /trades?user=W&limit=50
        ▼
   data_api.py ──┐
        │       │ filter ts > last_seen[W]
        │       ▼
        │  for each new trade t:
        │       │
        │       │ aum_estimator.aum(W, t.timestamp)  ← /positions, /value
        │       ▼
        │  trade_pct = t.size / aum
        │  paper_size = trade_pct × capital_per_wallet
        │       │
        │       ▼
        │  paper_portfolio.buy/sell()
        │       │
        ▼       ▼
   decisions.jsonl + portfolio.json (atomic write)
        │
        ▼
   Telegram alert + dashboard /api/copytrade
```

## State files

```
logs/copytrade/
├── state.json          ← last_seen_ts per wallet, dedup hashes (last 1000)
├── portfolio.json      ← cash + positions per wallet
├── decisions.jsonl     ← one line per detected trade with copy outcome
├── equity.jsonl        ← daily snapshot {ts, per_wallet_eq, total_eq}
└── copytrade.log       ← stdout + stderr (rotated by systemd)
```

`decisions.jsonl` example line:
```json
{"ts": 1775977803, "wallet": "RN1", "target_hash": "0x...", "side": "BUY",
 "market": "FC Barcelona vs Real Madrid CF / Yes", "target_size_usd": 5000,
 "target_aum_estimate": 100000, "trade_pct": 0.05, "paper_size_usd": 16.67,
 "price": 0.62, "action": "executed", "rationale": "ok"}
```

## Error handling

| Failure mode | Behavior |
|---|---|
| HTTP 429 / 5xx Data API | Exponential backoff (1s, 2s, 4s, 8s, max 60s), retry next cycle, log warn |
| HTTP 403 (geo or auth) | Log error, alert Telegram, continue with other wallets — do NOT crash |
| Target wallet returns AUM = 0 | Skip the trade, log info "target inactive/cashed out" |
| Trade on already-resolved market | Skip, log warn |
| Computed `paper_size < $1` | Skip, log debug |
| `trade_pct > 0.50` | Clamp to 0.50, log warn (suspicious target behavior) |
| Paper SELL exceeds current paper size | Clamp to current size, set position to 0, log warn |
| Crash / kill | Systemd `Restart=always`, state.json/portfolio.json reloaded |
| Two trades with same hash | Idempotent — second one is a no-op (dedup) |
| Clock skew between local and Polymarket | `last_seen_ts` always uses Polymarket-side `t.timestamp`, never local time |

## Observability

### Dashboard
New Flask route `/api/copytrade` in `dashboard/app.py` returning:
```json
{
  "started_at": "2026-05-15T18:00:00Z",
  "capital_total": 1000,
  "wallets": [
    {"pseudonym": "RN1", "wallet": "0x...", "cash": 320, "equity": 354,
     "open_positions": 3, "pnl_pct_30d": 6.2, "last_trade_at": "..."},
    ...
  ],
  "recent_decisions": [...],  // last 50
  "equity_curve": [...]       // daily snapshots
}
```

New tab in `dashboard/templates/index.html` ("CopyTrade") showing per-wallet equity curves, current positions, last 20 decisions.

### Telegram
- Bot start/stop
- Each copy trade (formatted, with link to Polymarket market URL)
- Daily summary 18h UTC
- Error rate > 10/h → ping
- Heartbeat hourly (silent in logs, only ping if positions changed)

### Smoke test at boot
1. GET `/trades?user=<RN1>&limit=1` → must return 200 + JSON array. Else fail-fast.
2. Verify `logs/copytrade/` exists or create.
3. Verify `state.json` parses or initialize fresh.
4. Send Telegram start alert.

## Success criteria (30-day evaluation window)

After 30 days of paper trading, capitalization decision is made on:

| Criterion | Threshold |
|---|---|
| Final paper capital | > $1000 (positive PnL) |
| Sharpe ratio (daily returns annualized) | > 1.0 |
| Total trades copied | ≥ 20 |
| Max drawdown | < 20% |
| Trades on resolved markets (PnL realized) | ≥ 50% of total trades |

All five must pass. Otherwise: stop the bot, archive logs, document learnings in `docs/SESSIONS_HISTORY.md`.

If all five pass → next step is the "go live" project, separately scoped (geoblock, USDC funding, signed CLOB orders).

## Path to live (out of scope v1, but designed for)

The detection + sizing + state code is broker-agnostic. To go live in a v2:
1. Add `live_executor.py` that signs EIP-712 and POSTs to `clob.polymarket.com`
2. Solve geoblock: VPN (Mullvad on VPS) OR new VPS region (Vultr Tokyo, Hetzner Singapore)
3. Sign up via VPN to provision a Polymarket proxy wallet, fund USDC.e on Polygon
4. Replace `paper_portfolio` calls with `live_executor` calls behind a `BOT_CP_MODE=paper|live` env var

No code in v1 needs to change for v2. The v2 spec will be a separate document.

## Testing strategy

### Unit tests (pytest)
- `test_aum_estimator.py` — synthetic wallet data, verify AUM math
- `test_paper_portfolio.py` — buy/sell sequences, edge cases (sell more than owned, zero size, etc.)
- `test_runner_sizing.py` — given a fake trade + AUM, verify `paper_size` calculation + clamps

### Integration tests
- `test_data_api_smoke.py` — hit real Data API, verify response shape on the 3 target wallets

### Replay test
- `replay_30d.py` script: fetch last 30 days of trades from the 3 wallets, run the runner logic offline, output equity curve. Validates the strategy retroactively before paper running forward.

### Manual smoke after deploy
1. `systemctl status bot-cp` shows active
2. Telegram start alert received
3. After 5 minutes, `decisions.jsonl` either empty (no new trades from targets) or has ≥1 line
4. `/api/copytrade` returns valid JSON with 3 wallets

## File layout (post-implementation)

```
live/copytrade/
├── __init__.py
├── runner.py
├── targets.py
├── data_api.py
├── aum_estimator.py
├── paper_portfolio.py
├── state.py
└── README.md
tests/copytrade/
├── test_aum_estimator.py
├── test_paper_portfolio.py
├── test_runner_sizing.py
└── test_data_api_smoke.py
scripts/
└── replay_30d.py
deploy/
└── bot-cp.service
dashboard/
├── app.py                      ← add /api/copytrade route
└── templates/index.html        ← add CopyTrade tab
logs/copytrade/                 ← created at runtime
```

## Open questions for implementation plan

- Frequency of equity snapshot — daily 00h UTC, or rolling on each trade?
- Should the bot replay the last 24h of target trades on boot to catch up, or only watch from `now`?
- Heartbeat in Telegram — every hour silent / only when changes / configurable?
- Dashboard tab styling — reuse existing CSS or add new section?

These get resolved in the writing-plans phase.

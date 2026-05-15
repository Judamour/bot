# Bot CopyTrade Paper (bot-cp)

Paper-trading mirror of 3 Polymarket top wallets. See spec at
`docs/superpowers/specs/2026-05-15-polymarket-copytrade-bot-design.md`.

## Local run

```bash
python -m live.copytrade.runner
```

Env vars:
- `BOT_CP_CAPITAL_USD` — total paper capital (default 1000)
- `BOT_CP_POLL_S` — polling interval seconds (default 60)
- `BOT_CP_LOG_DIR` — output dir (default `logs/copytrade`)

## VPS deployment

```bash
sudo cp deploy/bot-cp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bot-cp
sudo systemctl status bot-cp
sudo tail -f /home/botuser/bot-trading/logs/copytrade/copytrade.log
```

## State files

```
logs/copytrade/
├── state.json          last_seen_ts per wallet
├── portfolio.json      cash + positions per wallet
├── decisions.jsonl     each detected trade + copy outcome
├── equity.jsonl        daily MTM snapshot
└── copytrade.log       stdout + stderr
```

## Reset

```bash
sudo systemctl stop bot-cp
sudo rm -f /home/botuser/bot-trading/logs/copytrade/{state,portfolio}.json \
           /home/botuser/bot-trading/logs/copytrade/{decisions,equity}.jsonl
sudo systemctl start bot-cp
```

## Tests

```bash
pytest tests/copytrade/ -v
```

Integration smoke test (hits real Polymarket API):
```bash
pytest tests/copytrade/test_data_api_smoke.py -v --run-integration
```

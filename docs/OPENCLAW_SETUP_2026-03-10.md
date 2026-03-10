# OpenClaw — Setup & Intégration Bot Trading (2026-03-10)

## Contexte

Session du 2026-03-10. Objectif : installer OpenClaw sur le VPS et le connecter au bot de trading en lecture seule via Telegram, pour permettre à Claude de surveiller et analyser le bot en temps réel.

## Ce qui a été fait

### 1. Installation Node.js 22 + OpenClaw sur le VPS

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install nodejs -y        # → v22.22.1
sudo npm install -g openclaw@latest  # → OpenClaw 2026.3.8
```

### 2. Onboarding non-interactif (SSH headless)

```bash
openclaw onboard --non-interactive --accept-risk \
  --auth-choice anthropic-api-key \
  --anthropic-api-key <clé> \
  --install-daemon
```

Installe un service systemd user : `~/.config/systemd/user/openclaw-gateway.service`

### 3. Ajout clé Anthropic dans le service systemd

L'onboarding ne configure pas la clé pour l'agent embedded. Fix : injection dans le service.

```bash
# Fichier : /home/ubuntu/.config/systemd/user/openclaw-gateway.service
Environment=ANTHROPIC_API_KEY=<clé>

systemctl --user daemon-reload
systemctl --user restart openclaw-gateway
```

### 4. Plugin Telegram + bot @Damoria_openclawbot

```bash
openclaw plugins enable telegram
openclaw channels add --channel telegram --token <token_bot>
systemctl --user restart openclaw-gateway

# Approbation du pairing (après 1er message sur Telegram)
openclaw pairing approve telegram <CODE>
```

- Token bot : créé via @BotFather → @Damoria_openclawbot
- Chat ID Justin : 1771388433

### 5. Endpoint /api/openclaw dans dashboard/app.py

Ajout d'un endpoint Flask dédié, lecture seule :

```python
@app.route("/api/openclaw")
def api_openclaw():
    # Retourne : bot_z (capital/engine/drawdown/vix/regime/warnings)
    #            bots A/B/C/G/H/I (capital/pnl/positions/win_rate)
    #            fear_greed
```

Accessible localement : `curl http://127.0.0.1:5000/api/openclaw`

### 6. Configuration workspace OpenClaw

**SOUL.md** — instruit l'agent de :
- Toujours répondre en français à Justin
- Exécuter `curl http://127.0.0.1:5000/api/openclaw` quand on parle du bot
- Donner son avis (pas juste lire les chiffres)

**TOOLS.md** — documente :
- L'endpoint API et les commandes curl
- L'architecture du bot (Bot Z, engines, sous-bots)
- Les règles de sécurité (lecture seule, pas d'accès aux clés)

## Architecture finale

```
[VPS 51.210.13.248]
├── bot-trading (botuser, port 5000)
│   ├── multi_runner.py
│   ├── dashboard Flask → GET /api/openclaw
│   └── @Damortrading_bot (notifications trades)
└── openclaw-gateway (ubuntu, port 18789)
    ├── @Damoria_openclawbot (interface IA)
    ├── Claude Opus via Anthropic
    └── curl http://127.0.0.1:5000/api/openclaw
```

## Décision architecture (confirmée par ChatGPT + Claude)

- OpenClaw **séparé** du bot (pas intégré dans multi_runner.py)
- Accès **lecture seule** via API interne
- Clés exchange (Kraken/Binance) **non exposées** à OpenClaw
- Evolution future : endpoints POST /actions/pause si besoin

## Résultat du premier test

OpenClaw a correctement :
1. Lu SOUL.md et TOOLS.md sur instruction
2. Exécuté `curl http://127.0.0.1:5000/api/openclaw`
3. Présenté un résumé structuré en français (table positions, alertes)
4. Donné un avis analytique sur le drift 70% et le Fear & Greed à 13

## Fichiers modifiés/créés

| Fichier | Action |
|---------|--------|
| `dashboard/app.py` | Ajout `GET /api/openclaw` |
| `/home/ubuntu/.openclaw/workspace/SOUL.md` | Personnalité + instruction curl bot |
| `/home/ubuntu/.openclaw/workspace/TOOLS.md` | API endpoint + architecture + sécurité |
| `/home/ubuntu/.config/systemd/user/openclaw-gateway.service` | `ANTHROPIC_API_KEY` injecté |
| `CLAUDE.md` | Section OpenClaw ajoutée |

## Commit

`ed3e369` — feat: add /api/openclaw endpoint for OpenClaw integration

## Évolutions possibles

1. **Heartbeat automatique** : OpenClaw envoie un résumé quotidien sans être sollicité
2. **Endpoint /api/risk** : exposer drawdown max, circuit breaker, VIX détaillé
3. **POST /actions/pause** : action limitée (phase 2, après validation)
4. **Revue 30/04** : OpenClaw pourra analyser 3 mois de shadow.jsonl avec vous

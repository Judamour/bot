#!/usr/bin/env bash
# Wrapper pour systemd --user (contourne les espaces dans le path projet).
set -euo pipefail
cd "/home/damoria/Developpement REACT/bot trading"
exec ./.venv-copytrade/bin/python -m local.copytrade_live.poller

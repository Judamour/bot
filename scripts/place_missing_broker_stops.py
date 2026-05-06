#!/usr/bin/env python3
"""
One-shot : place broker-side stops sur toutes les positions ouvertes
qui n'ont pas encore d'alpaca_stop_id (positions ouvertes avant le
déploiement du feature broker-stop).

⚠ Doit tourner BOT ARRÊTÉ pour éviter race sur state.json :
    sudo systemctl stop bot
    sudo -u botuser /home/botuser/bot-trading/venv/bin/python \
        /home/botuser/bot-trading/scripts/place_missing_broker_stops.py
    sudo systemctl start bot
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.order_executor import place_broker_stop
from live import alpaca_executor

STATE_FILES = [
    ("A", "logs/supertrend/state.json"),
    ("B", "logs/momentum/state.json"),
    ("C", "logs/breakout/state.json"),
    ("G", "logs/trend/state.json"),
    ("H", "logs/vcb/state.json"),
    ("I", "logs/rs_leaders/state.json"),
    ("J", "logs/mean_reversion/state.json"),
]


def main():
    placed = skipped = failed = 0

    for bot_id, path in STATE_FILES:
        if not os.path.exists(path):
            continue

        with open(path) as f:
            state = json.load(f)

        positions = state.get("positions") or {}
        if not positions:
            continue

        print(f"\n── Bot {bot_id} : {len(positions)} positions ──")
        modified = False

        for symbol, pos in positions.items():
            existing = pos.get("alpaca_stop_id")
            if existing:
                print(f"  [SKIP] {symbol:12} stop déjà présent ({existing[:8]}…)")
                skipped += 1
                continue

            if not alpaca_executor.is_alpaca_routed(symbol):
                print(f"  [SKIP] {symbol:12} non routé Alpaca")
                skipped += 1
                continue

            stop_price = pos.get("stop")
            size = pos.get("size")
            if not stop_price or not size:
                print(f"  [FAIL] {symbol:12} stop ou size manquant ({stop_price=}, {size=})")
                failed += 1
                continue

            print(f"  [PLACE] {symbol:12} qty={size:.6f} stop={stop_price:.4f} …", end=" ", flush=True)
            ids = place_broker_stop(symbol, size, stop_price)
            new_id = ids.get("stop_id") if ids else None
            if new_id:
                pos["alpaca_stop_id"] = new_id
                modified = True
                placed += 1
                print(f"OK ({new_id[:8]}…)")
            else:
                failed += 1
                print("ÉCHEC")

        if modified:
            with open(path, "w") as f:
                json.dump(state, f, indent=2, default=str)
            print(f"  → state.json mis à jour")

    print(f"\n=== Résumé ===")
    print(f"  Placés  : {placed}")
    print(f"  Skipped : {skipped}")
    print(f"  Failed  : {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

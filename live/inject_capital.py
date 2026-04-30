"""
inject_capital.py — Injecter ou retirer du capital sur Bot Z sans casser le PnL.

Usage :
  python live/inject_capital.py <montant> [label]

Exemples :
  python live/inject_capital.py 500            # ajoute 500€
  python live/inject_capital.py 500 "DCA mai"  # ajoute 500€ avec label
  python live/inject_capital.py -200 "retrait" # retire 200€

Mécanisme :
  - Update z_capital, cb_peak, initial_capital (les 3 augmentent du montant)
  - Re-scale last_bot_values au prorata pour éviter PnL artificiel au cycle suivant
  - Log dans logs/bot_z/injections.jsonl
  - Notification Telegram

Raison cb_peak augmente : sinon le circuit breaker calcule
DD = (z_capital - cb_peak) / cb_peak avec un cb_peak figé sur l'ancien capital
→ DD apparent augmente artificiellement → CB se déclenche pour rien.

Raison last_bot_values re-scaled : Bot Z calcule pnl_cycle =
sum(current_values) - sum(last_bot_values). Sans re-scale, l'injection serait
comptée comme un gain massif au prochain cycle.
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATE_FILE = "logs/bot_z/state.json"
LOG_FILE = "logs/bot_z/injections.jsonl"


def inject(amount: float, label: str = "") -> dict:
    """Inject (positif) ou retire (négatif) du capital. Retourne les détails."""
    if not os.path.exists(STATE_FILE):
        raise FileNotFoundError(f"State file introuvable : {STATE_FILE}")

    with open(STATE_FILE) as f:
        state = json.load(f)

    old_z    = float(state.get("z_capital", 0))
    old_cb   = float(state.get("cb_peak", old_z))
    old_init = float(state.get("initial_capital", old_z))

    if old_z <= 0:
        raise ValueError(f"z_capital invalide ({old_z}) — refus injection")

    new_z = old_z + amount
    if new_z <= 0:
        raise ValueError(f"Retrait trop grand : {old_z}€ - {abs(amount)}€ = {new_z}€ ≤ 0")

    state["z_capital"]        = new_z
    state["cb_peak"]          = old_cb + amount
    state["initial_capital"]  = old_init + amount

    # Re-scale last_bot_values au prorata (évite PnL artificiel cycle suivant)
    ratio = new_z / old_z
    lbv = state.get("last_bot_values", {})
    if isinstance(lbv, dict):
        state["last_bot_values"] = {k: float(v) * ratio for k, v in lbv.items()}

    entry = {
        "ts": datetime.now().isoformat(),
        "amount": amount,
        "label": label,
        "z_capital_before": old_z,
        "z_capital_after": new_z,
        "cb_peak_before": old_cb,
        "cb_peak_after": old_cb + amount,
        "ratio_applied": ratio,
    }

    injections = state.get("injections", [])
    if not isinstance(injections, list):
        injections = []
    injections.append(entry)
    state["injections"] = injections

    # Save atomique
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)

    # Append au log d'injections
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return entry


def main():
    p = argparse.ArgumentParser(description="Injecter/retirer du capital Bot Z")
    p.add_argument("amount", type=float, help="Montant en € (positif = ajout, négatif = retrait)")
    p.add_argument("label", nargs="?", default="", help="Label optionnel pour traçabilité")
    p.add_argument("--no-notify", action="store_true", help="Skip notification Telegram")
    args = p.parse_args()

    if abs(args.amount) < 0.01:
        print("ERREUR : montant trop faible (<0.01€)")
        sys.exit(1)

    label = args.label or f"Injection {args.amount:+.2f}€"

    try:
        entry = inject(args.amount, label)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERREUR : {e}")
        sys.exit(1)

    sign = "+" if args.amount > 0 else "-"
    action = "Injection" if args.amount > 0 else "Retrait"
    print(f"✓ {action} {sign}{abs(args.amount):.2f}€ enregistré")
    print(f"  z_capital   : {entry['z_capital_before']:>10.2f}€ → {entry['z_capital_after']:>10.2f}€")
    print(f"  cb_peak     : {entry['cb_peak_before']:>10.2f}€ → {entry['cb_peak_after']:>10.2f}€")
    print(f"  ratio appliqué aux last_bot_values : ×{entry['ratio_applied']:.4f}")
    print(f"  Label       : {label}")
    print(f"  Log         : {LOG_FILE}")

    if not args.no_notify:
        try:
            from live.notifier import notify
            notify(
                f"💰 <b>Capital {action.lower()}</b>\n"
                f"Montant : <b>{sign}{abs(args.amount):.2f}€</b>\n"
                f"Z capital : {entry['z_capital_before']:.2f}€ → <b>{entry['z_capital_after']:.2f}€</b>\n"
                f"Label : {label}"
            )
            print("  Telegram    : envoyé ✓")
        except Exception as e:
            print(f"  Telegram    : échec ({e})")


if __name__ == "__main__":
    main()

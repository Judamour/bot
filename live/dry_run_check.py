"""
dry_run_check.py — Vérifications obligatoires avant bascule live.

Tests effectués (AUCUN ordre réel n'est passé) :
  1. Imports et config
  2. Connexion authentifiée Kraken (lecture solde)
  3. Permissions clé API (peut-elle lister les ordres ouverts ?)
  4. Test de placement d'ordre INVALID (montant=0) pour détecter "permission refusée"
     vs "paramètre invalide" — confirme que la clé a "Trade" sans risquer un fill
  5. Vérification min order size BTC/EUR (Kraken renvoie info marché)
  6. State Bot Z accessible et cohérent
  7. Telegram notifier opérationnel

Usage : python live/dry_run_check.py
Exit code 0 si tout OK, 1 si bloquant détecté.
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg):    print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg):  print(f"  {RED}✗{RESET} {msg}")
def warn(msg):  print(f"  {YELLOW}⚠{RESET}  {msg}")
def info(msg):  print(f"    {msg}")


def main():
    blockers = []
    warnings = []

    print(f"\n{BOLD}=== DRY RUN CHECK — pré-bascule live ==={RESET}\n")

    # ── 1. Imports
    print(f"{BOLD}1. Imports et config{RESET}")
    try:
        import config
        from data.fetcher import get_exchange
        from live.order_executor import check_balance, check_total_value_eur, reconcile_positions
        from live.notifier import notify
        ok(f"Tous les imports OK")
        info(f"PAPER_TRADING = {config.PAPER_TRADING}")
        info(f"Symboles configurés : {len(config.SYMBOLS)}")
    except Exception as e:
        fail(f"Import : {e}")
        blockers.append("imports")
        print(f"\n{RED}BLOQUANT — arrêt prématuré{RESET}")
        return 1

    # ── 2. Connexion Kraken authentifiée
    print(f"\n{BOLD}2. Connexion Kraken authentifiée{RESET}")
    try:
        exchange = get_exchange(use_auth=True)
        balance = exchange.fetch_balance()
        eur_free = float(balance.get("EUR", {}).get("free", 0))
        eur_total = float(balance.get("EUR", {}).get("total", 0))
        ok(f"Connexion OK")
        info(f"EUR free  : {eur_free:.4f}€")
        info(f"EUR total : {eur_total:.4f}€")

        positions = []
        for asset, b in balance.items():
            if asset in ("EUR", "info", "free", "used", "total", "timestamp", "datetime"):
                continue
            if isinstance(b, dict):
                qty = float(b.get("total", 0))
                if qty > 0.0001:
                    positions.append((asset, qty))
        if positions:
            info(f"Positions détectées :")
            for a, q in positions:
                info(f"  {a}: {q:.6f}")
        else:
            info(f"Aucune position ouverte sur le compte")
    except Exception as e:
        fail(f"Connexion : {e}")
        blockers.append("kraken_connection")
        return 1

    # ── 3. Permissions clé API
    print(f"\n{BOLD}3. Permissions clé API{RESET}")
    try:
        open_orders = exchange.fetch_open_orders()
        ok(f"Lecture ordres ouverts : {len(open_orders)} ordre(s)")
    except Exception as e:
        fail(f"Lecture ordres : {e}")
        warnings.append("read_orders")

    # Test placement d'ordre invalide pour vérifier permission Trade
    # On envoie un ordre avec amount=0 → Kraken refuse pour "amount" si Trade OK,
    # ou pour "permission" si Trade absent. Distinguer les 2 erreurs.
    print(f"\n{BOLD}4. Test permission Trade (sans risque){RESET}")
    try:
        exchange.create_order("BTC/EUR", "limit", "buy", 0.0, 1.0)  # amount=0 invalide volontaire
        warn(f"Ordre amount=0 accepté ?? — anormal, vérifier manuellement")
    except Exception as e:
        err_msg = str(e).lower()
        if "permission" in err_msg or "denied" in err_msg or "not allowed" in err_msg:
            fail(f"Permission Trade ABSENTE : {e}")
            blockers.append("trade_permission")
        elif "amount" in err_msg or "volume" in err_msg or "minimum" in err_msg or "invalid" in err_msg:
            ok(f"Permission Trade présente (erreur attendue 'amount/volume/invalid')")
            info(f"Réponse Kraken : {str(e)[:120]}")
        else:
            warn(f"Erreur inattendue (à analyser) : {e}")
            warnings.append("permission_unclear")

    # ── 5. Min order size BTC/EUR
    print(f"\n{BOLD}5. Min order size BTC/EUR{RESET}")
    try:
        markets = exchange.load_markets()
        btc_market = markets.get("BTC/EUR", {})
        limits = btc_market.get("limits", {}) or {}
        amount_min = (limits.get("amount") or {}).get("min")
        cost_min = (limits.get("cost") or {}).get("min")
        ok(f"Marché BTC/EUR chargé")
        if amount_min:
            info(f"Min amount : {amount_min} BTC")
        if cost_min:
            info(f"Min cost   : {cost_min}€")
        ticker = exchange.fetch_ticker("BTC/EUR")
        price = float(ticker.get("last", 0))
        info(f"Prix BTC/EUR actuel : {price:.2f}€")
        if amount_min and price > 0:
            min_eur = amount_min * price
            info(f"Ordre minimum BTC en €: ~{min_eur:.2f}€")
            if min_eur > 50:
                warn(f"Min order BTC > 50€ — vérifier capital disponible vs taille minimale")
                warnings.append("btc_min_high")
    except Exception as e:
        warn(f"Marchés : {e}")
        warnings.append("markets")

    # ── 6. State Bot Z
    print(f"\n{BOLD}6. State Bot Z{RESET}")
    state_path = "logs/bot_z/state.json"
    if not os.path.exists(state_path):
        warn(f"State file absent ({state_path}) — sera créé au premier cycle")
        warnings.append("state_missing")
    else:
        try:
            import json
            with open(state_path) as f:
                state = json.load(f)
            z_cap = state.get("z_capital", 0)
            engine = state.get("current_engine", "?")
            ok(f"State chargé")
            info(f"z_capital      : {z_cap:.2f}€")
            info(f"current_engine : {engine}")
            info(f"cb_peak        : {state.get('cb_peak', 0):.2f}€")
            info(f"injections     : {len(state.get('injections', []))}")
        except Exception as e:
            fail(f"State corrompu : {e}")
            blockers.append("state_corrupt")

    # ── 7. Telegram
    print(f"\n{BOLD}7. Telegram notifier{RESET}")
    try:
        notify("🧪 <b>Dry run check</b> — Bot Trading\nTest de notification (aucun ordre passé)")
        ok(f"Notification envoyée")
    except Exception as e:
        warn(f"Telegram : {e}")
        warnings.append("telegram")

    # ── Résumé
    print(f"\n{BOLD}=== RÉSUMÉ ==={RESET}")
    if blockers:
        print(f"{RED}{BOLD}BLOQUANTS ({len(blockers)}) :{RESET}")
        for b in blockers:
            print(f"  - {b}")
        print(f"\n{RED}NE PAS BASCULER EN LIVE{RESET}")
        return 1
    if warnings:
        print(f"{YELLOW}Warnings ({len(warnings)}) :{RESET}")
        for w in warnings:
            print(f"  - {w}")
        print(f"\n{YELLOW}Vérifier les warnings, mais bascule possible{RESET}")
    else:
        print(f"{GREEN}{BOLD}TOUT OK — bascule live possible{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

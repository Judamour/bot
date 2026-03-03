#!/usr/bin/env python3
"""
Bot Trading — Point d'entrée principal

Usage:
  python main.py backtest     → Lance le backtest sur tous les symboles
  python main.py live         → Lance le bot en paper trading
  python main.py data         → Télécharge et affiche les données
  python main.py dashboard    → Lance le dashboard sur http://localhost:5000
"""
import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "backtest":
        from backtest.engine import run_backtest
        import config
        for symbol in config.SYMBOLS:
            run_backtest(symbol)

    elif cmd == "live":
        from live.bot import run
        run()

    elif cmd == "data":
        from data.fetcher import fetch_ohlcv
        import config
        for symbol in config.SYMBOLS:
            df = fetch_ohlcv(symbol, days=30)
            print(f"\n{symbol} — 5 dernières bougies:")
            print(df[["open", "high", "low", "close", "volume"]].tail(5))

    elif cmd == "dashboard":
        from dashboard.app import run
        run()

    else:
        print(f"Commande inconnue: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

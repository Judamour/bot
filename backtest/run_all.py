"""
Runner backtest multi-symboles — parallèle, rapport global.

Usage:
    python backtest/run_all.py                    # Tous les symboles, 3 ans
    python backtest/run_all.py --days 730         # 2 ans
    python backtest/run_all.py --symbols BTC/EUR  # Un symbole
    python backtest/run_all.py --workers 4        # 4 threads parallèles
"""
import argparse
import os
import sys
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # Headless — pas de GUI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from backtest.engine import run_backtest


def run_all(symbols=None, days=config.BACKTEST_DAYS, workers=4):
    symbols = symbols or config.SYMBOLS
    os.makedirs("backtest/results", exist_ok=True)

    print(f"\n{'='*65}")
    print(f"  BACKTEST GLOBAL — {len(symbols)} symboles | {days}j | {config.TIMEFRAME}")
    print(f"  Position: {config.POSITION_SIZE_EUR}€ | SL: 3×ATR | TP: {config.TAKE_PROFIT_RATIO}R")
    print(f"  Filtres: ADX>{config.ADX_THRESHOLD} | RSI<{config.RSI_OVERBOUGHT} | Supertrend + EMA")
    print(f"{'='*65}")

    results = {}
    errors = []

    def backtest_one(symbol):
        try:
            return symbol, run_backtest(symbol, config.TIMEFRAME, days)
        except Exception as e:
            return symbol, {"error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(backtest_one, s): s for s in symbols}
        for future in as_completed(futures):
            symbol, metrics = future.result()
            results[symbol] = metrics
            if "error" in metrics:
                errors.append(f"{symbol}: {metrics['error']}")

    # ── Rapport global ─────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("  RÉSUMÉ GLOBAL")
    print(f"{'='*65}")
    print(f"{'Symbole':<16} {'Trades':>6} {'Win%':>6} {'Rdt%':>7} {'PF':>5} {'Sharpe':>7} {'MaxDD%':>7}")
    print("-" * 65)

    valid = {s: m for s, m in results.items() if "error" not in m and m.get("total_trades", 0) > 0}
    invalid = {s: m for s, m in results.items() if "error" in m or m.get("total_trades", 0) == 0}

    for symbol, m in sorted(valid.items(), key=lambda x: x[1]["total_return_pct"], reverse=True):
        wr = m["win_rate"]
        rdt = m["total_return_pct"]
        pf = m["profit_factor"]
        sh = m["sharpe_ratio"]
        dd = m["max_drawdown_pct"]
        wr_str  = f"{wr:.1f}%"
        rdt_str = f"{rdt:+.1f}%"
        print(f"{symbol:<16} {m['total_trades']:>6} {wr_str:>6} {rdt_str:>7} {pf:>5.2f} {sh:>7.2f} {dd:>6.1f}%")

    for symbol in invalid:
        err = results[symbol].get("error", "0 trades")
        print(f"{symbol:<16}  — {err}")

    if valid:
        all_returns = [m["total_return_pct"] for m in valid.values()]
        all_wr      = [m["win_rate"] for m in valid.values()]
        all_pf      = [m["profit_factor"] for m in valid.values() if m["profit_factor"] != float("inf")]
        profitable  = sum(1 for r in all_returns if r > 0)

        print("-" * 65)
        print(f"{'MOYENNE':<16} {'':>6} {sum(all_wr)/len(all_wr):>5.1f}% "
              f"{sum(all_returns)/len(all_returns):>+6.1f}%  "
              f"{sum(all_pf)/len(all_pf) if all_pf else 0:>5.2f}")
        print(f"\n  Symboles profitables : {profitable}/{len(valid)}")
        print(f"  Win rate moyen       : {sum(all_wr)/len(all_wr):.1f}%")
        print(f"  Rendement moyen      : {sum(all_returns)/len(all_returns):+.1f}%")

    # ── Sauvegarde CSV ─────────────────────────────────────────────────────────
    out = f"backtest/results/summary_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    with open(out, "w", newline="") as f:
        fields = ["symbol", "total_trades", "win_rate", "total_return_pct",
                  "profit_factor", "sharpe_ratio", "max_drawdown_pct",
                  "avg_win_eur", "avg_loss_eur", "final_capital"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for symbol, m in results.items():
            if "error" not in m:
                writer.writerow({"symbol": symbol, **m})
    print(f"\n  Résumé CSV : {out}")
    print(f"{'='*65}\n")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--days",    type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    run_all(args.symbols, args.days, args.workers)

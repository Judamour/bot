import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from colorama import Fore, Style, init
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import fetch_ohlcv
from strategies.supertrend import generate_signals, calculate_position_size

init(autoreset=True)


def run_backtest(symbol: str, timeframe: str = config.TIMEFRAME, days: int = config.BACKTEST_DAYS) -> dict:
    """
    Exécute un backtest complet et retourne les métriques de performance.
    """
    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"  BACKTEST : {symbol} | {timeframe} | {days} jours")
    print(f"{'='*60}{Style.RESET_ALL}")

    # 1. Données
    df = fetch_ohlcv(symbol, timeframe, days)
    df = generate_signals(df)

    # 2. Simulation
    capital = config.PAPER_CAPITAL
    initial_capital = capital
    position = None  # {"entry": price, "size": qty, "stop": price, "tp": price}
    trades = []
    equity_curve = [capital]
    equity_dates = [df.index[0]]

    for i, (ts, row) in enumerate(df.iterrows()):
        # Vérifier stop-loss / take-profit si position ouverte
        if position:
            exit_reason = None
            exit_price = row["close"]

            if row["low"] <= position["stop"]:
                exit_price = position["stop"]
                exit_reason = "stop_loss"
            elif row["signal"] == -1:
                exit_reason = "signal"

            if exit_reason:
                exit_price_eff = exit_price * (1 - config.SLIPPAGE)
                fee_exit = exit_price_eff * position["size"] * config.EXCHANGE_FEE
                proceeds = exit_price_eff * position["size"] - fee_exit
                pnl = proceeds - (position["entry"] * position["size"] + position["fee_entry"])
                capital += pnl
                trades.append({
                    "entry_date": position["date"],
                    "exit_date": ts,
                    "entry_price": position["entry"],
                    "exit_price": exit_price_eff,
                    "size": position["size"],
                    "pnl": pnl,
                    "pnl_pct": pnl / (position["entry"] * position["size"]) * 100,
                    "reason": exit_reason,
                    "result": "win" if pnl > 0 else "loss",
                })
                position = None
            else:
                # ATR trailing stop : monte le stop si le prix progresse
                new_stop = row["close"] - config.ATR_MULTIPLIER * row["atr"]
                if new_stop > position["stop"]:
                    position["stop"] = new_stop

        # Ouvrir position sur signal achat (si pas déjà en position)
        if row["signal"] == 1 and position is None and capital > 0:
            entry_price = row["close"] * (1 + config.SLIPPAGE)
            position_eur = max(config.POSITION_MIN_EUR, capital * config.POSITION_SIZE_PCT)
            pos = calculate_position_size(position_eur, entry_price, row["atr"])
            fee_entry = entry_price * pos["size"] * config.EXCHANGE_FEE
            cost = pos["size"] * entry_price + fee_entry
            if cost <= capital:
                position = {
                    "entry": entry_price,
                    "size": pos["size"],
                    "stop": pos["stop_loss"],
                    "tp": pos["take_profit"],
                    "date": ts,
                    "fee_entry": fee_entry,
                }

        equity_curve.append(capital)
        equity_dates.append(ts)

    # Clôturer position ouverte en fin de période
    if position:
        last = df.iloc[-1]
        exit_price_eff = last["close"] * (1 - config.SLIPPAGE)
        fee_exit = exit_price_eff * position["size"] * config.EXCHANGE_FEE
        proceeds = exit_price_eff * position["size"] - fee_exit
        pnl = proceeds - (position["entry"] * position["size"] + position["fee_entry"])
        capital += pnl
        trades.append({
            "entry_date": position["date"],
            "exit_date": df.index[-1],
            "entry_price": position["entry"],
            "exit_price": exit_price_eff,
            "size": position["size"],
            "pnl": pnl,
            "pnl_pct": pnl / (position["entry"] * position["size"]) * 100,
            "reason": "end_of_period",
            "result": "win" if pnl > 0 else "loss",
        })

    # 3. Métriques
    metrics = _calculate_metrics(trades, equity_curve, initial_capital, capital)

    # 4. Affichage
    _print_report(symbol, timeframe, days, metrics, trades)

    # 5. Graphique
    _plot_results(df, trades, equity_curve, equity_dates, symbol, timeframe)

    return metrics


def _calculate_metrics(trades, equity_curve, initial_capital, final_capital):
    if not trades:
        return {"error": "Aucun trade généré"}

    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["result"] == "win"]
    losses = df_trades[df_trades["result"] == "loss"]

    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak * 100
    max_drawdown = drawdown.min()

    total_return = (final_capital - initial_capital) / initial_capital * 100
    win_rate = len(wins) / len(df_trades) * 100 if len(df_trades) > 0 else 0
    avg_win = wins["pnl"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl"].mean() if len(losses) > 0 else 0
    profit_factor = abs(wins["pnl"].sum() / losses["pnl"].sum()) if losses["pnl"].sum() != 0 else float("inf")

    # Sharpe ratio (approximatif)
    returns = pd.Series(equity).pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

    return {
        "total_trades": len(df_trades),
        "win_rate": round(win_rate, 1),
        "total_return_pct": round(total_return, 2),
        "final_capital": round(final_capital, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe_ratio": round(sharpe, 2),
        "avg_win_eur": round(avg_win, 2),
        "avg_loss_eur": round(avg_loss, 2),
        "best_trade_eur": round(df_trades["pnl"].max(), 2),
        "worst_trade_eur": round(df_trades["pnl"].min(), 2),
    }


def _print_report(symbol, timeframe, days, metrics, trades):
    if "error" in metrics:
        print(f"{Fore.RED}  {metrics['error']}")
        return

    def color(val, good_if_positive=True):
        if (val > 0 and good_if_positive) or (val < 0 and not good_if_positive):
            return f"{Fore.GREEN}{val}{Style.RESET_ALL}"
        return f"{Fore.RED}{val}{Style.RESET_ALL}"

    print(f"\n{Fore.YELLOW}  RÉSULTATS{Style.RESET_ALL}")
    print(f"  Trades totaux     : {metrics['total_trades']}")
    print(f"  Win rate          : {color(metrics['win_rate'])}%")
    print(f"  Rendement total   : {color(metrics['total_return_pct'])}%")
    print(f"  Capital final     : {metrics['final_capital']} €")
    print(f"  Max drawdown      : {color(metrics['max_drawdown_pct'], False)}%")
    print(f"  Profit factor     : {metrics['profit_factor']}")
    print(f"  Sharpe ratio      : {metrics['sharpe_ratio']}")
    print(f"  Gain moyen        : {color(metrics['avg_win_eur'])} €")
    print(f"  Perte moyenne     : {color(metrics['avg_loss_eur'])} €")

    # Verdict
    print(f"\n{Fore.CYAN}  VERDICT{Style.RESET_ALL}")
    score = 0
    if metrics["win_rate"] >= 50: score += 1
    if metrics["total_return_pct"] > 0: score += 1
    if metrics["max_drawdown_pct"] > -20: score += 1
    if metrics["profit_factor"] >= 1.5: score += 1
    if metrics["sharpe_ratio"] >= 1.0: score += 1

    verdicts = {
        5: f"{Fore.GREEN}  ★★★★★ Excellente stratégie — déployable en paper trading",
        4: f"{Fore.GREEN}  ★★★★☆ Bonne stratégie — à valider sur plus de données",
        3: f"{Fore.YELLOW}  ★★★☆☆ Stratégie correcte — optimiser les paramètres",
        2: f"{Fore.RED}  ★★☆☆☆ Stratégie faible — ne pas déployer",
        1: f"{Fore.RED}  ★☆☆☆☆ Stratégie perdante — revoir les paramètres",
        0: f"{Fore.RED}  ☆☆☆☆☆ Stratégie très mauvaise",
    }
    print(verdicts.get(score, verdicts[0]))


def _plot_results(df, trades, equity_curve, equity_dates, symbol, timeframe):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=False)
    fig.suptitle(f"Backtest {symbol} — {timeframe}", fontsize=14, fontweight="bold")

    # Graphique 1 : Prix + indicateurs + trades
    ax1.plot(df.index, df["close"], color="#aaaaaa", linewidth=0.8, label="Prix")

    if "ema_fast" in df.columns:
        ax1.plot(df.index, df["ema_fast"], color="#2196F3", linewidth=1.2, label=f"EMA {config.EMA_FAST}")
        ax1.plot(df.index, df["ema_slow"], color="#FF9800", linewidth=1.2, label=f"EMA {config.EMA_SLOW}")
    if "ema200" in df.columns:
        ax1.plot(df.index, df["ema200"], color="#9C27B0", linewidth=1.5, label="EMA 200")
    if "supertrend" in df.columns:
        bull = df[df["supertrend_dir"] == 1]
        bear = df[df["supertrend_dir"] == -1]
        ax1.plot(bull.index, bull["supertrend"], color="green", linewidth=1.2, label="Supertrend ↑")
        ax1.plot(bear.index, bear["supertrend"], color="red", linewidth=1.2, label="Supertrend ↓")

    for t in trades:
        ax1.axvline(t["entry_date"], color="green", alpha=0.3, linewidth=0.5)
        ax1.axvline(t["exit_date"], color="red", alpha=0.3, linewidth=0.5)

    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_ylabel("Prix (EUR)")
    ax1.grid(alpha=0.3)

    # Graphique 2 : Courbe d'equity
    ax2.plot(equity_dates, equity_curve, color="#4CAF50", linewidth=1.5)
    ax2.fill_between(equity_dates, equity_curve, config.PAPER_CAPITAL, alpha=0.2, color="#4CAF50")
    ax2.axhline(config.PAPER_CAPITAL, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_ylabel("Capital (EUR)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    os.makedirs("backtest/results", exist_ok=True)
    filename = f"backtest/results/backtest_{symbol.replace('/', '_')}_{timeframe}.png"
    plt.savefig(filename, dpi=150, bbox_inches="tight")
    print(f"\n  Graphique sauvegardé : {filename}")
    plt.close(fig)


if __name__ == "__main__":
    for symbol in config.SYMBOLS:
        run_backtest(symbol)

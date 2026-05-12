#!/usr/bin/env python3
"""Backtest 3 ans du Shadow Bot (moteur unifié single-loop, daily granularity).

Réutilise EXACTEMENT les détecteurs et scorer du shadow runner :
  - shadow.strategies.ALL_DETECTORS (5 détecteurs)
  - shadow.scorer.compute_score (score composite 0-100)
  - Sizing risk parity 1% per trade, max 10% per position
  - Trailing ATR×4

Sortie : CAGR, Sharpe, MaxDD, trades, win rate, profit factor.
Comparaison avec backtests prod : Bot A solo (CAGR 49%, Sharpe 2.43) et Bot Z (CAGR 38%).
"""
import os
import sys
import math
import warnings
import json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf

warnings.filterwarnings("ignore")
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shadow.strategies import ALL_DETECTORS
from shadow.scorer import compute_score
from strategies.supertrend import compute_atr
from backtest.multi_backtest import compute_metrics, INITIAL

# ── Config ───────────────────────────────────────────────────────────────────
START = "2023-04-29"
END = "2026-04-29"
INITIAL_CAPITAL = INITIAL  # 100K
MIN_SCORE = 55.0
TOP_N_SIGNALS = 5
MAX_OPEN_POSITIONS = 10
RISK_PER_TRADE_PCT = 0.01
MAX_POSITION_PCT = 0.10
ATR_MULT_TRAIL = 4.0
FEE = 0.0026
SLIPPAGE = 0.001
WARMUP_DAYS = 220  # need 200 days for SMA200 in detectors

# Univers identique au prod (21 actifs Alpaca crisis-alpha)
CRYPTO = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD",
          "AVAX/USD": "AVAX-USD", "LINK/USD": "LINK-USD"}
STOCKS = ["NVDA", "GOOGL", "META", "PLTR", "CRWD", "LLY", "ABBV", "XOM", "CVX",
          "JPM", "BAC", "KO", "PG", "SPY", "QQQ", "GLD"]
ALL_SYMBOLS = list(CRYPTO.keys()) + STOCKS


def fetch_daily(symbol_internal: str) -> pd.DataFrame | None:
    """Fetch daily OHLCV via yfinance, normalize columns."""
    yf_ticker = CRYPTO.get(symbol_internal, symbol_internal)
    try:
        df = yf.download(yf_ticker, start=START, end=END, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < WARMUP_DAYS:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        return df
    except Exception as e:
        print(f"  [skip] {symbol_internal}: {e}")
        return None


def main():
    print(f"=== SHADOW BACKTEST {START} → {END} ===\n")
    print("Chargement OHLCV daily…")
    cache = {}
    for sym in ALL_SYMBOLS:
        df = fetch_daily(sym)
        if df is not None:
            cache[sym] = df
            print(f"  ✓ {sym:10} : {len(df)} jours ({df.index[0].date()} → {df.index[-1].date()})")
    if not cache:
        print("Aucune data chargée")
        return

    # Date range unifiée (intersection)
    common_dates = sorted(set.intersection(*[set(df.index) for df in cache.values()]))
    print(f"\n{len(common_dates)} jours communs à tous les symboles\n")

    # ── State ────────────────────────────────────────────────────────────────
    capital = INITIAL_CAPITAL
    positions = {}  # symbol -> dict
    trades = []
    equity_curve = []
    equity_dates = []

    ctx = {"vix": 18.0, "btc_trend": "bull", "qqq_ok": True}

    # ── Simulation jour par jour ─────────────────────────────────────────────
    for i, day in enumerate(common_dates[WARMUP_DAYS:], start=WARMUP_DAYS):
        # 1. Update macro context (approximation simple basée sur SPY trend)
        if "SPY" in cache:
            spy_df = cache["SPY"]
            spy_today = spy_df.loc[:day].iloc[-1]
            sma_200 = spy_df.loc[:day].iloc[-200:]["close"].mean()
            ctx["qqq_ok"] = bool(spy_today["close"] > sma_200)

        # 2. Sur positions ouvertes : check stop touché (low du jour) + trailing update
        for sym in list(positions.keys()):
            df = cache[sym]
            if day not in df.index:
                continue
            bar = df.loc[day]
            low = float(bar["low"])
            close = float(bar["close"])
            pos = positions[sym]

            # Stop touché
            if low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - SLIPPAGE)
                proceeds = exit_price * pos["size"]
                fee = proceeds * FEE
                capital += proceeds - fee
                pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee
                trades.append({
                    "symbol": sym, "strategy": pos["strategy"],
                    "entry": pos["entry"], "exit": exit_price,
                    "entry_date": pos["entry_date"], "exit_date": day,
                    "pnl": round(pnl, 2),
                    "reason": "stop_loss", "score": pos["score"],
                })
                del positions[sym]
                continue

            # Trailing update
            df_slice = df.loc[:day]
            if len(df_slice) >= 15:
                atr = float(compute_atr(df_slice["high"], df_slice["low"], df_slice["close"], 14).iloc[-1] or 0)
                if atr > 0:
                    new_stop = close - ATR_MULT_TRAIL * atr
                    if new_stop > pos["stop"]:
                        pos["stop"] = new_stop

        # 3. Scan signaux (sur tous les symboles sans position)
        candidates = []
        for sym in ALL_SYMBOLS:
            if sym in positions or sym not in cache:
                continue
            df = cache[sym]
            if day not in df.index:
                continue
            df_history = df.loc[:day]
            if len(df_history) < WARMUP_DAYS:
                continue
            # En backtest daily, on passe le même df aux deux slots (4h et 1d)
            # Les détecteurs travaillent sur les données dispo jusqu'au jour t
            for detector in ALL_DETECTORS:
                try:
                    sig = detector(sym, df_history, df_history)
                    if sig is None:
                        continue
                    sig.score = compute_score(sig, ctx)
                    if sig.score >= MIN_SCORE:
                        candidates.append(sig)
                except Exception:
                    pass

        # 4. Dédup par symbole (plusieurs détecteurs peuvent firer sur le même actif)
        # Sans ça, le dict positions[sym] = {...} écrase la position précédente
        # en gardant le capital déduit mais en perdant la trace de la 1re position.
        best_by_symbol: dict[str, "Signal"] = {}
        for sig in candidates:
            if sig.symbol not in best_by_symbol or sig.score > best_by_symbol[sig.symbol].score:
                best_by_symbol[sig.symbol] = sig
        candidates = sorted(best_by_symbol.values(), key=lambda s: s.score, reverse=True)

        # 5. Top N → ouvrir positions
        for sig in candidates[:TOP_N_SIGNALS]:
            if len(positions) >= MAX_OPEN_POSITIONS:
                break
            # Sizing risk parity
            stop_dist = abs(sig.entry_price - sig.stop_price)
            if stop_dist <= 0:
                continue
            # Equity = cash + positions (recalcule pour sizing)
            eq = capital
            for s, p in positions.items():
                if day in cache[s].index:
                    eq += float(cache[s].loc[day]["close"]) * p["size"]
            risk_eur = eq * RISK_PER_TRADE_PCT
            size = risk_eur / stop_dist
            max_size = (eq * MAX_POSITION_PCT) / sig.entry_price
            size = min(size, max_size)
            if size <= 0:
                continue
            entry_eff = sig.entry_price * (1 + SLIPPAGE)
            cost = entry_eff * size
            fee = cost * FEE
            total = cost + fee
            if total > capital:
                continue
            capital -= total
            positions[sig.symbol] = {
                "strategy": sig.strategy, "score": sig.score,
                "entry": entry_eff, "size": size,
                "stop": sig.stop_price, "initial_stop": sig.stop_price,
                "entry_date": day, "fee_entry": fee,
            }

        # 5. Equity du jour
        eq = capital
        for sym, pos in positions.items():
            if day in cache[sym].index:
                eq += float(cache[sym].loc[day]["close"]) * pos["size"]
        equity_curve.append(eq)
        equity_dates.append(day)

    # Force close all à la fin pour comparer fairement
    last_day = common_dates[-1]
    for sym in list(positions.keys()):
        if last_day in cache[sym].index:
            exit_price = float(cache[sym].loc[last_day]["close"]) * (1 - SLIPPAGE)
            pos = positions[sym]
            proceeds = exit_price * pos["size"]
            fee = proceeds * FEE
            capital += proceeds - fee
            pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee
            trades.append({
                "symbol": sym, "strategy": pos["strategy"],
                "entry": pos["entry"], "exit": exit_price,
                "entry_date": pos["entry_date"], "exit_date": last_day,
                "pnl": round(pnl, 2), "reason": "end_of_backtest",
                "score": pos["score"],
            })
        del positions[sym]

    # ── Résultats ────────────────────────────────────────────────────────────
    metrics = compute_metrics(trades, equity_curve, initial=INITIAL_CAPITAL)

    print(f"\n{'='*60}")
    print(f"  SHADOW BOT — Backtest {START} → {END}")
    print(f"{'='*60}")
    print(f"  Capital final  : ${metrics['final']:,.0f}")
    print(f"  CAGR           : {metrics['cagr']:>5.1f} %")
    print(f"  Sharpe         : {metrics['sharpe']:>5.2f}")
    print(f"  Max Drawdown   : {metrics['max_dd']:>5.1f} %")
    print(f"  Profit Factor  : {metrics['profit_factor']:.2f}")
    print(f"  Trades         : {metrics['trades']}")
    print(f"  Win rate       : {metrics['win_rate']:.1f} %")
    print(f"{'='*60}\n")

    # Breakdown par stratégie
    by_strat = {}
    for t in trades:
        s = t.get("strategy", "?")
        by_strat.setdefault(s, []).append(t)
    print("Trades par stratégie :")
    for s, ts in sorted(by_strat.items(), key=lambda x: -len(x[1])):
        wins = sum(1 for t in ts if t["pnl"] > 0)
        total_pnl = sum(t["pnl"] for t in ts)
        print(f"  {s:18} : {len(ts):4} trades | win {wins/len(ts)*100:.1f}% | PnL ${total_pnl:+,.0f}")

    # Comparaison références
    print(f"\n📊 Références prod :")
    print(f"  Bot A solo (3y)         : +33% CAGR, Sharpe 2.24")
    print(f"  Bot Z PROD Meta v2 (3y) : +27% CAGR, Sharpe 1.71")
    print(f"  Bot Z régime pur (3y)   : +40% CAGR, Sharpe 1.04")
    print(f"  Z v2 QualityScore (4y)  : +43% CAGR, Sharpe 1.37")

    # Sauvegarde
    os.makedirs("backtest/results", exist_ok=True)
    out = {
        "params": {
            "start": START, "end": END,
            "min_score": MIN_SCORE, "top_n": TOP_N_SIGNALS,
            "max_open": MAX_OPEN_POSITIONS,
            "risk_per_trade": RISK_PER_TRADE_PCT,
            "max_position": MAX_POSITION_PCT,
        },
        "metrics": metrics,
        "n_trades": len(trades),
        "by_strategy": {s: {"n": len(ts), "pnl": round(sum(t["pnl"] for t in ts), 2)}
                        for s, ts in by_strat.items()},
        "equity_dates": [str(d.date()) for d in equity_dates[::30]],  # un point/mois
        "equity_curve_monthly": [round(equity_curve[i], 0) for i in range(0, len(equity_curve), 30)],
    }
    with open("backtest/results/shadow_3y.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n💾 Sauvegardé : backtest/results/shadow_3y.json")


if __name__ == "__main__":
    main()

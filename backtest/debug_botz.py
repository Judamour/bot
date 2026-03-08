#!/usr/bin/env python3
"""Debug script to trace Bot Z Enhanced bug."""
import warnings; warnings.filterwarnings("ignore")
import sys, os, math, time
import pandas as pd, numpy as np
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from backtest.multi_backtest import (
    backtest_bot_a, backtest_bot_b, backtest_bot_c, backtest_bot_g,
    INITIAL, VALID_BOTS_Z, REGIME_WEIGHTS_Z, _get_regime_at_dt, _metrics_portfolio,
)
import yfinance as yf

XSTOCK_MAP = {
    "NVDAx/EUR":"NVDA","AAPLx/EUR":"AAPL","MSFTx/EUR":"MSFT","METAx/EUR":"META",
    "GOOGx/EUR":"GOOGL","PLTRx/EUR":"PLTR","AMDx/EUR":"AMD","AVGOx/EUR":"AVGO",
    "GLDx/EUR":"GLD","NFLXx/EUR":"NFLX","CRWDx/EUR":"CRWD",
}
CRYPTO_MAP = {"BTC/EUR":"BTC-EUR","ETH/EUR":"ETH-EUR","SOL/EUR":"SOL-EUR","BNB/EUR":"BNB-EUR"}
EURUSD = 1.1608; START = "2016-01-01"

def fetch(ticker, to_eur=False):
    raw = yf.Ticker(ticker).history(period="10y", interval="1d")
    if raw.empty: return None
    df = raw[["Open","High","Low","Close","Volume"]].rename(columns=str.lower)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None: df.index = df.index.tz_localize(None)
    df = df[df.index >= START].dropna(subset=["close"])
    if to_eur:
        for c in ["open","high","low","close"]: df[c] /= EURUSD
    return df if len(df) > 200 else None

print("Fetching data...")
daily = {}
for sym, tk in XSTOCK_MAP.items():
    if sym not in config.SYMBOLS: continue
    df = fetch(tk, True)
    if df is not None: daily[sym] = df
for sym, tk in CRYPTO_MAP.items():
    if sym not in config.SYMBOLS: continue
    df = fetch(tk)
    if df is not None: daily[sym] = df
if "BTC/EUR" in daily:
    daily["BTC/EUR"]["ema200"] = daily["BTC/EUR"]["close"].ewm(span=200).mean()

print("Running individual bots...")
results = {}
for key, fn in [("a", backtest_bot_a), ("b", backtest_bot_b),
                ("c", backtest_bot_c), ("g", backtest_bot_g)]:
    r = fn(daily)
    results[key] = r
    print(f"  Bot {key}: {len(r['dates'])} dates, equity[0]={r['equity'][0]:.2f}, equity[-1]={r['equity'][-1]:.2f}")
    print(f"    dates[0]={r['dates'][0]} (type={type(r['dates'][0]).__name__})")

valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
ks = list(valid.keys())
bot_norm = {k: {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])} for k, r in valid.items()}

# Check common dates
date_sets = [set(r["dates"]) for r in valid.values()]
common_dates = sorted(set.intersection(*date_sets))
print(f"\nCommon dates: {len(common_dates)} dates")
print(f"  First: {common_dates[0]} (type={type(common_dates[0]).__name__})")
print(f"  Last:  {common_dates[-1]}")

# Check if first date is in bot_norm
for k in ks:
    cd0 = common_dates[0]
    val = bot_norm[k].get(cd0, "MISSING")
    print(f"  bot_norm[{k}][{cd0}] = {val}")

# Simulate first 10 days
print("\nFirst 10 daily returns (Bot Z Enhanced simulation):")
eq = [INITIAL * len(ks)]
for i in range(1, min(11, len(common_dates))):
    dt = common_dates[i]
    prev_dt = common_dates[i-1]

    raw_w = REGIME_WEIGHTS_Z.get("RANGE")
    total_w = sum(raw_w.get(k, 0) for k in ks) or 1.0
    w = {k: raw_w.get(k, 0) / total_w for k in ks}

    bot_r = {}
    for k in ks:
        p = bot_norm[k].get(prev_dt, 1.0)
        c = bot_norm[k].get(dt, p)
        bot_r[k] = (c / p - 1) if p > 0 else 0.0

    r_port = sum(w[k] * bot_r[k] for k in ks)
    eq.append(eq[-1] * (1 + r_port))
    bot_r_str = " ".join(f"{k}:{bot_r[k]*100:+.2f}%" for k in ks)
    print(f"  {dt}: r_port={r_port*100:+.3f}% | {bot_r_str} | equity={eq[-1]:.2f}")

# Find the day with maximum daily return
print("\nSearching for anomalous daily returns...")
max_r = 0
max_dt = None
eq2 = [INITIAL * len(ks)]
for i in range(1, len(common_dates)):
    dt = common_dates[i]
    prev_dt = common_dates[i-1]
    raw_w = REGIME_WEIGHTS_Z.get("RANGE")
    total_w = sum(raw_w.get(k, 0) for k in ks) or 1.0
    w = {k: raw_w.get(k, 0) / total_w for k in ks}
    bot_r = {}
    for k in ks:
        p = bot_norm[k].get(prev_dt, 1.0)
        c = bot_norm[k].get(dt, p)
        bot_r[k] = (c / p - 1) if p > 0 else 0.0
    r_port = sum(w[k] * bot_r[k] for k in ks)
    eq2.append(eq2[-1] * (1 + r_port))
    if r_port > max_r:
        max_r = r_port
        max_dt = dt
        max_bots = dict(bot_r)

print(f"Max daily return: {max_r*100:.1f}% on {max_dt}")
if max_dt:
    print(f"  Bot returns: {max_bots}")
print(f"Final equity (RANGE weights always): {eq2[-1]:.0f}")

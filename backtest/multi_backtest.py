#!/usr/bin/env python3
"""
backtest/multi_backtest.py
==========================
Backtest 3 ans — 6 bots actifs (A, B, C, G, H, I)

Produit :
  - Tableau comparatif console (CAGR, Sharpe, MaxDD, PF, Trades, WinRate)
  - Performance par année (2022 / 2023 / 2024)
  - Performance par régime (BULL / RANGE / BEAR / HIGH_VOL)
  - backtest/results/multi_summary.csv
  - backtest/results/multi_equity.png

Usage :
    python backtest/multi_backtest.py
"""
import os
import sys
import math
import warnings
import time

import numpy as np
import pandas as pd
from colorama import Fore, Style, init

warnings.filterwarnings("ignore")
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import generate_signals, compute_atr, compute_adx

init(autoreset=True)

# ── Constantes ────────────────────────────────────────────────────────────────
INITIAL      = 1000.0
FEE          = config.EXCHANGE_FEE
SLIP         = config.SLIPPAGE
DAYS         = 365 * 3 + 60          # ~3 ans + buffer warm-up
RESULTS_DIR  = "backtest/results"
os.makedirs(RESULTS_DIR, exist_ok=True)

BREAKOUT_SYMS = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]
VCB_SYMS      = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "NVDAx", "AMDx", "METAx", "PLTRx"]


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, color=Fore.CYAN):
    print(f"{color}[BACKTEST]{Style.RESET_ALL} {msg}")


# ── 1. DATA FETCH ─────────────────────────────────────────────────────────────

def fetch_all_data():
    """Fetch 3 ans daily OHLCV pour tous les symboles + VIX + QQQ."""
    from data.fetcher import fetch_ohlcv

    log("Fetching données daily 3 ans...")
    daily = {}
    for sym in config.SYMBOLS:
        try:
            df = fetch_ohlcv(sym, "1d", DAYS)
            if df is not None and len(df) > 250:
                daily[sym] = df
                log(f"  {sym}: {len(df)} barres", Fore.GREEN)
            else:
                log(f"  {sym}: données insuffisantes ({len(df) if df is not None else 0} barres)", Fore.YELLOW)
        except Exception as e:
            log(f"  {sym} ERREUR: {e}", Fore.RED)
        time.sleep(0.3)

    log("Fetching VIX + QQQ (régime)...")
    import yfinance as yf
    try:
        vix_raw = yf.Ticker("^VIX").history(period="4y", interval="1d")["Close"]
        vix_raw.index = vix_raw.index.tz_localize(None)
    except Exception:
        vix_raw = pd.Series(dtype=float)

    try:
        qqq_raw = yf.Ticker("QQQ").history(period="4y", interval="1d")[["Close"]]
        qqq_raw.index = qqq_raw.index.tz_localize(None)
        qqq_raw["sma200"] = qqq_raw["Close"].rolling(200).mean()
    except Exception:
        qqq_raw = pd.DataFrame(columns=["Close", "sma200"])

    log(f"Données chargées : {len(daily)}/{len(config.SYMBOLS)} symboles | VIX: {len(vix_raw)} barres | QQQ: {len(qqq_raw)} barres")
    return daily, vix_raw, qqq_raw


# ── 2. RÉGIME ─────────────────────────────────────────────────────────────────

def classify_regime(vix, qqq_price, qqq_sma200):
    if pd.isna(vix) or pd.isna(qqq_price) or pd.isna(qqq_sma200):
        return "UNKNOWN"
    if vix > 30:
        return "HIGH_VOL"
    if qqq_price < qqq_sma200:
        return "BEAR"
    if vix < 18:
        return "BULL"
    return "RANGE"


def get_regime_at(dt, vix_s, qqq_df):
    try:
        if hasattr(dt, 'date'):
            dt = pd.Timestamp(dt).normalize()
        v = vix_s.asof(dt)
        q = qqq_df["Close"].asof(dt)
        s = qqq_df["sma200"].asof(dt)
        return classify_regime(v, q, s)
    except Exception:
        return "UNKNOWN"


# ── 3. MÉTRIQUES ──────────────────────────────────────────────────────────────

def compute_metrics(trades, equity_list, initial=INITIAL):
    if not equity_list:
        return _empty()
    eq = np.array(equity_list, dtype=float)
    final = eq[-1]
    n_days = len(eq)
    years = max(n_days / 365, 0.1)
    cagr = ((final / initial) ** (1 / years) - 1) * 100

    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / (peak + 1e-10) * 100
    max_dd = float(dd.min())

    ret = pd.Series(eq).pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * math.sqrt(252)) if ret.std() > 0 else 0

    if not trades:
        return {"cagr": round(cagr, 1), "sharpe": round(sharpe, 2), "max_dd": round(max_dd, 1),
                "profit_factor": 0, "trades": 0, "win_rate": 0, "final": round(final, 0)}

    pnls = [t["pnl"] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) * 100
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0
    pf = round(gp / gl, 2) if gl > 0 else (99.0 if gp > 0 else 0)

    return {
        "cagr": round(cagr, 1),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 1),
        "profit_factor": pf,
        "trades": len(trades),
        "win_rate": round(win_rate, 1),
        "final": round(final, 0),
    }


def _empty():
    return {"cagr": 0, "sharpe": 0, "max_dd": 0, "profit_factor": 0,
            "trades": 0, "win_rate": 0, "final": INITIAL}


def annual_returns(equity_list, dates):
    """Retourne {2022: pct, 2023: pct, 2024: pct, ...}"""
    s = pd.Series(equity_list, index=dates)
    results = {}
    for year in sorted(s.index.year.unique()):
        yearly = s[s.index.year == year]
        if len(yearly) < 2:
            continue
        r = (yearly.iloc[-1] / yearly.iloc[0] - 1) * 100
        results[year] = round(r, 1)
    return results


def regime_returns(trades, vix_s, qqq_df):
    """Retourne {BULL: pnl, RANGE: pnl, BEAR: pnl, HIGH_VOL: pnl}"""
    buckets = {"BULL": 0.0, "RANGE": 0.0, "BEAR": 0.0, "HIGH_VOL": 0.0, "UNKNOWN": 0.0}
    for t in trades:
        r = get_regime_at(t.get("exit_date", t.get("entry_date")), vix_s, qqq_df)
        buckets[r] = buckets.get(r, 0) + t["pnl"]
    return {k: round(v, 1) for k, v in buckets.items() if k != "UNKNOWN"}


# ── 4. HELPERS INDICATEURS ────────────────────────────────────────────────────

def _entry(price): return price * (1 + SLIP)
def _exit(price):  return price * (1 - SLIP)
def _cost(size, price): return size * price * (1 + FEE)
def _proceeds(size, price): return size * price * (1 - FEE)

def _close_pos(pos, price, reason, dt):
    eff = _exit(price)
    proceeds = _proceeds(pos["size"], eff)
    pnl = proceeds - pos["cost"]
    return proceeds, {"symbol": pos.get("sym","?"), "entry_date": pos["date"],
                      "exit_date": dt, "pnl": round(pnl, 4), "reason": reason}


# ── 5. BOT A — Supertrend + MR (daily approximation) ─────────────────────────

def backtest_bot_a(daily_cache):
    log("Bot A — Supertrend+MR...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    MAX_POS, SIZE_PCT = 6, 0.15

    # Precompute signals
    dfs = {}
    for sym in config.SYMBOLS:
        df = daily_cache.get(sym)
        if df is not None and len(df) > 220:
            try:
                dfs[sym] = generate_signals(df.copy())
            except Exception:
                pass

    if not dfs:
        return {"trades": [], "equity": [INITIAL], "dates": [], "name": "Bot A"}

    all_dates = sorted(set.union(*[set(df.index) for df in dfs.values()]))

    for dt in all_dates:
        # Trailing stops + exits
        for sym in list(positions.keys()):
            pos = positions[sym]
            df = dfs.get(sym)
            if df is None or dt not in df.index:
                continue
            row = df.loc[dt]
            atr = float(row.get("atr", row["close"] * 0.02))
            stype = pos.get("stype", "trend")
            mult = 3.0 if stype == "trend" else 1.0
            new_stop = row["close"] - mult * atr
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop

            exit_reason = None
            ep = row["close"]
            if row["close"] <= pos["stop"]:
                exit_reason, ep = "trailing_stop", pos["stop"]
            elif row.get("signal", 0) == -1 and stype == "trend":
                exit_reason = "signal_exit"
            elif row.get("mr_signal", 0) == -1 and stype == "mr":
                exit_reason = "mr_exit"

            if exit_reason:
                proceeds, tr = _close_pos(pos, ep, exit_reason, dt)
                capital += proceeds
                trades.append(tr)
                del positions[sym]

        # Entries
        for sym, df in dfs.items():
            if sym in positions or dt not in df.index or len(positions) >= MAX_POS:
                continue
            row = df.loc[dt]
            atr = float(row.get("atr", row["close"] * 0.02))
            stype, buy = None, False
            if row.get("signal", 0) == 1:
                stype, buy = "trend", True
            elif row.get("mr_signal", 0) == 1:
                stype, buy = "mr", True
            if buy and capital > 10:
                ep = _entry(row["close"])
                size = (capital * SIZE_PCT) / ep
                cost = _cost(size, ep)
                if cost <= capital:
                    mult = 3.0 if stype == "trend" else 1.0
                    capital -= cost
                    positions[sym] = {"sym": sym, "size": size, "cost": cost,
                                      "stop": ep - mult * atr, "date": dt, "stype": stype}

        pv = capital + sum(dfs[s].loc[dt, "close"] * p["size"]
                           for s, p in positions.items() if s in dfs and dt in dfs[s].index)
        equity.append(pv)
        dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot A — Supertrend+MR"}


# ── 6. BOT B — Momentum Rotation ──────────────────────────────────────────────

def backtest_bot_b(daily_cache):
    log("Bot B — Momentum Rotation...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    TOP_N, REBAL_DAYS, STOP_PCT = 4, 6, 0.12
    last_rebal = None

    syms = [s for s in config.SYMBOLS if s in daily_cache and len(daily_cache[s]) > 130]
    if not syms:
        return {"trades": [], "equity": [INITIAL], "dates": [], "name": "Bot B"}

    all_dates = sorted(set.union(*[set(daily_cache[s].index) for s in syms]))

    for dt in all_dates:
        prices = {s: float(daily_cache[s].loc[dt, "close"])
                  for s in syms if dt in daily_cache[s].index}

        # Hard stop -12%
        for sym in list(positions.keys()):
            pos = positions[sym]
            px = prices.get(sym)
            if px and (px - pos["entry"]) / pos["entry"] <= -STOP_PCT:
                proceeds, tr = _close_pos(pos, px, "hard_stop", dt)
                capital += proceeds; trades.append(tr); del positions[sym]

        # Rebalance every 6+ days
        if last_rebal is None or (dt - last_rebal).days >= REBAL_DAYS:
            # Compute scores
            scores = {}
            for sym in syms:
                df = daily_cache[sym]
                idx = df.index.get_loc(dt) if dt in df.index else -1
                if idx < 130:
                    continue
                c = df["close"]
                p1m = float(c.iloc[max(0, idx - 22)])
                p3m = float(c.iloc[max(0, idx - 66)])
                p6m = float(c.iloc[max(0, idx - 130)])
                px  = float(c.iloc[idx])
                if p1m > 0 and p3m > 0 and p6m > 0:
                    sc = 0.4 * (px/p1m - 1) + 0.4 * (px/p3m - 1) + 0.2 * (px/p6m - 1)
                    if sc > 0:
                        scores[sym] = sc

            top = sorted(scores, key=scores.get, reverse=True)[:TOP_N]

            # Sell non-top
            for sym in list(positions.keys()):
                if sym not in top:
                    px = prices.get(sym, positions[sym]["entry"])
                    proceeds, tr = _close_pos(positions[sym], px, "rotation", dt)
                    capital += proceeds; trades.append(tr); del positions[sym]

            # Buy new entries
            n_to_buy = [s for s in top if s not in positions]
            n_slots = TOP_N - len(positions)
            if n_slots > 0 and n_to_buy and capital > 10:
                alloc = capital / max(n_slots, len(n_to_buy))
                for sym in n_to_buy[:n_slots]:
                    px = prices.get(sym)
                    if not px:
                        continue
                    ep = _entry(px)
                    size = alloc / ep
                    cost = _cost(size, ep)
                    if cost <= capital:
                        capital -= cost
                        positions[sym] = {"sym": sym, "size": size, "cost": cost,
                                          "entry": ep, "date": dt}
            last_rebal = dt

        pv = capital + sum(prices.get(s, p["entry"]) * p["size"] for s, p in positions.items())
        equity.append(pv); dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot B — Momentum"}


# ── 7. BOT C — Donchian Breakout (BTC/ETH/SOL) ───────────────────────────────

def backtest_bot_c(daily_cache):
    log("Bot C — Donchian Breakout...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    RISK_PCT = 0.01

    syms = [s for s in BREAKOUT_SYMS if s in daily_cache and len(daily_cache[s]) > 70]
    if not syms:
        return {"trades": [], "equity": [INITIAL], "dates": [], "name": "Bot C"}

    # Precompute signals
    sigs = {}
    for sym in syms:
        df = daily_cache[sym].copy()
        df["don_high"] = df["high"].rolling(55).max().shift(1)
        df["don_low"]  = df["low"].rolling(20).min().shift(1)
        df["atr20"]    = compute_atr(df["high"], df["low"], df["close"], 20)
        df["adx"]      = compute_adx(df["high"], df["low"], df["close"], 14)
        sigs[sym] = df

    all_dates = sorted(set.union(*[set(df.index) for df in sigs.values()]))

    for dt in all_dates:
        # Trailing stop + exit
        for sym in list(positions.keys()):
            pos = positions[sym]
            df = sigs[sym]
            if dt not in df.index:
                continue
            row = df.loc[dt]
            new_stop = row["close"] - 2 * row["atr20"]
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
            exit_reason = None
            ep = row["close"]
            if row["close"] <= pos["stop"]:
                exit_reason, ep = "atr_stop", pos["stop"]
            elif row["close"] < row["don_low"]:
                exit_reason = "don_exit"
            if exit_reason:
                proceeds, tr = _close_pos(pos, ep, exit_reason, dt)
                capital += proceeds; trades.append(tr); del positions[sym]

        # Entry
        for sym, df in sigs.items():
            if sym in positions or dt not in df.index:
                continue
            row = df.loc[dt]
            if pd.isna(row["don_high"]) or pd.isna(row["atr20"]):
                continue
            vix_ok = True  # simplified (no live VIX in backtest)
            if (row["close"] > row["don_high"] and row["adx"] > 20 and vix_ok and capital > 10):
                ep = _entry(row["close"])
                atr_n = float(row["atr20"])
                stop = ep - 2 * atr_n
                risk_eur = capital * RISK_PCT
                size = risk_eur / (ep - stop) if (ep - stop) > 0 else 0
                size = min(size, capital * 0.33 / ep)
                cost = _cost(size, ep)
                if size > 0 and cost <= capital:
                    capital -= cost
                    positions[sym] = {"sym": sym, "size": size, "cost": cost,
                                      "stop": stop, "date": dt}

        pv = capital + sum(sigs[s].loc[dt, "close"] * p["size"]
                           for s, p in positions.items() if dt in sigs[s].index)
        equity.append(pv); dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot C — Breakout"}


# ── 8. BOT G — Trend Following Multi-Asset ────────────────────────────────────

def backtest_bot_g(daily_cache):
    log("Bot G — Trend Following Multi-Asset...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    TARGET_VOL, MAX_POS_PCT, MAX_POS = 0.15, 0.10, 8

    syms = [s for s in config.SYMBOLS if s in daily_cache and len(daily_cache[s]) > 230]

    sigs = {}
    for sym in syms:
        df = daily_cache[sym].copy()
        df["sma200"]  = df["close"].rolling(200).mean()
        df["sma50"]   = df["close"].rolling(50).mean()
        df["high50"]  = df["high"].rolling(50).max().shift(1)
        df["atr20"]   = compute_atr(df["high"], df["low"], df["close"], 20)
        df["adx"]     = compute_adx(df["high"], df["low"], df["close"], 14)
        df["vol20"]   = df["close"].pct_change().rolling(20).std() * math.sqrt(252)
        sigs[sym] = df

    all_dates = sorted(set.union(*[set(df.index) for df in sigs.values()]))

    for dt in all_dates:
        prices = {s: float(sigs[s].loc[dt, "close"]) for s in sigs if dt in sigs[s].index}

        # Trailing stop + exits
        for sym in list(positions.keys()):
            pos = positions[sym]
            df = sigs.get(sym)
            if df is None or dt not in df.index:
                continue
            row = df.loc[dt]
            px = row["close"]
            new_stop = px - 3 * float(row["atr20"])
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
            exit_reason = None
            ep = px
            if px <= pos["stop"]:
                exit_reason, ep = "trailing_stop", pos["stop"]
            elif not pd.isna(row["sma200"]) and px < row["sma200"]:
                exit_reason = "sma200_break"
            if exit_reason:
                proceeds, tr = _close_pos(pos, ep, exit_reason, dt)
                capital += proceeds; trades.append(tr); del positions[sym]

        # Entries
        if len(positions) < MAX_POS:
            for sym, df in sigs.items():
                if sym in positions or dt not in df.index or len(positions) >= MAX_POS:
                    continue
                row = df.loc[dt]
                if pd.isna(row["sma200"]) or pd.isna(row["high50"]) or pd.isna(row["vol20"]):
                    continue
                cond = (row["close"] > row["sma200"] and row["close"] > row["sma50"]
                        and row["close"] > row["high50"] and row["adx"] > 20
                        and row["vol20"] > 0)
                if cond and capital > 10:
                    vol = float(row["vol20"])
                    ep = _entry(row["close"])
                    size_pct = min(TARGET_VOL / vol, MAX_POS_PCT)
                    size = (capital * size_pct) / ep
                    cost = _cost(size, ep)
                    atr = float(row["atr20"])
                    if cost <= capital and size > 0:
                        capital -= cost
                        positions[sym] = {"sym": sym, "size": size, "cost": cost,
                                          "stop": ep - 3 * atr, "date": dt}

        pv = capital + sum(prices.get(s, p["cost"] / p["size"]) * p["size"]
                           for s, p in positions.items())
        equity.append(pv); dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot G — Trend Multi-Asset"}


# ── 9. BOT H — VCB Breakout ───────────────────────────────────────────────────

def backtest_bot_h(daily_cache):
    log("Bot H — VCB Breakout (daily approx.)...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    SIZE_PCT, MAX_POS = 0.20, 5

    syms = [s for s in VCB_SYMS if s in daily_cache and len(daily_cache[s]) > 120]

    sigs = {}
    for sym in syms:
        df = daily_cache[sym].copy()
        df["sma200"]  = df["close"].rolling(200).mean()
        df["sma50"]   = df["close"].rolling(50).mean()
        df["atr14"]   = compute_atr(df["high"], df["low"], df["close"], 14)
        df["high20"]  = df["high"].rolling(20).max().shift(1)
        # BB width percentile
        bb_mid   = df["close"].rolling(20).mean()
        bb_std   = df["close"].rolling(20).std()
        bb_w     = (2 * bb_std * 2) / (bb_mid + 1e-10)
        df["bb_pct"] = (bb_w - bb_w.rolling(100).min()) / (bb_w.rolling(100).max() - bb_w.rolling(100).min() + 1e-10)
        # ATR compression: 5 bars décroissants
        atr_d    = (df["atr14"].diff() < 0).astype(int)
        df["compressed"] = atr_d.rolling(5).sum() >= 5
        sigs[sym] = df

    all_dates = sorted(set.union(*[set(df.index) for df in sigs.values()]))

    for dt in all_dates:
        prices = {s: float(sigs[s].loc[dt, "close"]) for s in sigs if dt in sigs[s].index}

        # Trailing stop
        for sym in list(positions.keys()):
            pos = positions[sym]
            df = sigs.get(sym)
            if df is None or dt not in df.index:
                continue
            row = df.loc[dt]
            px = row["close"]
            new_stop = px - 3 * float(row["atr14"])
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
            if px <= pos["stop"]:
                proceeds, tr = _close_pos(pos, pos["stop"], "trailing_stop", dt)
                capital += proceeds; trades.append(tr); del positions[sym]

        # Entry: compression + breakout + above SMA200 + above SMA50
        if len(positions) < MAX_POS:
            for sym, df in sigs.items():
                if sym in positions or dt not in df.index or len(positions) >= MAX_POS:
                    continue
                row = df.loc[dt]
                if pd.isna(row["sma200"]) or pd.isna(row["bb_pct"]) or pd.isna(row["high20"]):
                    continue
                cond = (row["close"] > row["sma200"] and row["close"] > row["sma50"]
                        and row["compressed"] and row["bb_pct"] < 0.20
                        and row["close"] > row["high20"] and capital > 10)
                if cond:
                    ep = _entry(row["close"])
                    size = (capital * SIZE_PCT) / ep
                    atr = float(row["atr14"])
                    stop = ep - 1.5 * atr
                    cost = _cost(size, ep)
                    if cost <= capital and size > 0:
                        capital -= cost
                        positions[sym] = {"sym": sym, "size": size, "cost": cost,
                                          "stop": stop, "date": dt}

        pv = capital + sum(prices.get(s, p["cost"] / p["size"]) * p["size"]
                           for s, p in positions.items())
        equity.append(pv); dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot H — VCB Breakout"}


# ── 10. BOT I — RS Leaders ────────────────────────────────────────────────────

def backtest_bot_i(daily_cache):
    log("Bot I — RS Leaders...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    TOP_N, EXIT_RANK = 3, 5
    REBAL_DAYS, ADX_MIN, VOL_MAX, EXT_MAX = 5, 18, 0.90, 0.15
    TARGET_VOL, MAX_POS_PCT = 0.15, 0.30
    ATR_TRAIL, HARD_STOP = 2.5, 0.10
    last_rebal = None

    syms = [s for s in config.SYMBOLS if s in daily_cache and len(daily_cache[s]) > 220]

    sigs = {}
    for sym in syms:
        df = daily_cache[sym].copy()
        df["sma200"] = df["close"].rolling(200).mean()
        df["sma50"]  = df["close"].rolling(50).mean()
        df["atr14"]  = compute_atr(df["high"], df["low"], df["close"], 14)
        df["adx"]    = compute_adx(df["high"], df["low"], df["close"], 14)
        df["vol20"]  = df["close"].pct_change().rolling(20).std() * math.sqrt(252)
        sigs[sym] = df

    all_dates = sorted(set.union(*[set(df.index) for df in sigs.values()]))

    for dt in all_dates:
        prices = {s: float(sigs[s].loc[dt, "close"]) for s in sigs if dt in sigs[s].index}

        # Trailing stop + hard stop + SMA50 break
        for sym in list(positions.keys()):
            pos = positions[sym]
            df = sigs.get(sym)
            if df is None or dt not in df.index:
                continue
            row = df.loc[dt]
            px = row["close"]
            atr = float(row["atr14"])
            new_stop = px - ATR_TRAIL * atr
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
            exit_reason = None
            ep = px
            if px <= pos["stop"]:
                exit_reason, ep = "trailing_stop", pos["stop"]
            elif (px - pos["entry"]) / pos["entry"] <= -HARD_STOP:
                exit_reason = "hard_stop"
            elif not pd.isna(row["sma50"]) and px < row["sma50"]:
                exit_reason = "sma50_break"
            if exit_reason:
                proceeds, tr = _close_pos(pos, ep, exit_reason, dt)
                capital += proceeds; trades.append(tr); del positions[sym]

        # RS Scores
        scores = {}
        for sym in syms:
            df = sigs[sym]
            if dt not in df.index:
                continue
            idx = df.index.get_loc(dt)
            if idx < 200:
                continue
            row = df.loc[dt]
            c = df["close"]
            p1m = float(c.iloc[max(0, idx - 22)])
            p3m = float(c.iloc[max(0, idx - 66)])
            p6m = float(c.iloc[max(0, idx - 130)])
            px  = float(c.iloc[idx])
            if p1m <= 0 or p3m <= 0 or p6m <= 0:
                continue
            sma200 = float(row["sma200"]) if not pd.isna(row["sma200"]) else 0
            dist200 = (px - sma200) / sma200 if sma200 > 0 else 0
            sc = (0.35 * (px/p1m-1) + 0.35 * (px/p3m-1)
                  + 0.20 * (px/p6m-1) + 0.10 * dist200)
            if sc > 0:
                scores[sym] = sc

        ranked = sorted(scores, key=scores.get, reverse=True)

        # Rotate out if rank > EXIT_RANK
        for sym in list(positions.keys()):
            rank = ranked.index(sym) + 1 if sym in ranked else 999
            df = sigs.get(sym)
            row = df.loc[dt] if df is not None and dt in df.index else None
            # Quality check for retention
            passes = (row is not None and not pd.isna(row["sma200"])
                      and prices.get(sym, 0) > float(row["sma200"]))
            if rank > EXIT_RANK or not passes:
                px = prices.get(sym, positions[sym]["entry"])
                proceeds, tr = _close_pos(positions[sym], px, f"rs_exit_rank{rank}", dt)
                capital += proceeds; trades.append(tr); del positions[sym]

        # Rebalance
        if last_rebal is None or (dt - last_rebal).days >= REBAL_DAYS:
            # Filter qualified top symbols
            qualified = []
            for sym in ranked:
                if len(qualified) >= TOP_N:
                    break
                df = sigs.get(sym)
                if df is None or dt not in df.index:
                    continue
                row = df.loc[dt]
                if pd.isna(row["sma200"]) or pd.isna(row["sma50"]):
                    continue
                px = prices.get(sym, 0)
                sma200 = float(row["sma200"])
                sma50  = float(row["sma50"])
                adx    = float(row["adx"]) if not pd.isna(row["adx"]) else 0
                vol    = float(row["vol20"]) if not pd.isna(row["vol20"]) else 1
                ext    = (px - sma50) / sma50 if sma50 > 0 else 0
                if (px > sma200 and sma50 > sma200 and px > sma50
                        and adx > ADX_MIN and vol < VOL_MAX and ext < EXT_MAX):
                    qualified.append(sym)

            to_buy = [s for s in qualified if s not in positions]
            if to_buy and capital > 10:
                for sym in to_buy:
                    if len(positions) >= TOP_N:
                        break
                    df = sigs.get(sym)
                    if df is None or dt not in df.index:
                        continue
                    row = df.loc[dt]
                    vol = float(row["vol20"]) if not pd.isna(row["vol20"]) else 0.5
                    atr = float(row["atr14"])
                    ep  = _entry(prices.get(sym, row["close"]))
                    size_pct = min(TARGET_VOL / max(vol, 0.01), MAX_POS_PCT)
                    size = (capital * size_pct) / ep
                    cost = _cost(size, ep)
                    if cost <= capital and size > 0:
                        capital -= cost
                        positions[sym] = {"sym": sym, "size": size, "cost": cost,
                                          "entry": ep, "stop": ep - ATR_TRAIL * atr, "date": dt}
            last_rebal = dt

        pv = capital + sum(prices.get(s, p["entry"]) * p["size"] for s, p in positions.items())
        equity.append(pv); dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot I — RS Leaders"}


# ── 11. RAPPORT ───────────────────────────────────────────────────────────────

def print_report(results, vix_s, qqq_df):
    bots = list(results.values())
    print(f"\n{Fore.CYAN}{'='*100}")
    print(f"  BACKTEST 3 ANS — COMPARAISON MULTI-BOTS")
    print(f"{'='*100}{Style.RESET_ALL}")

    # Header
    hdr = f"{'Bot':<26} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7} {'PF':>6} {'Trades':>7} {'WinRate':>8} {'Final€':>8}"
    print(hdr)
    print("-" * 100)

    rows = []
    for r in bots:
        m = r["metrics"]
        color = Fore.GREEN if m["cagr"] > 0 else Fore.RED
        row = (f"  {r['name']:<24} "
               f"{color}{m['cagr']:>+6.1f}%{Style.RESET_ALL}  "
               f"{m['sharpe']:>6.2f}  "
               f"{Fore.RED if m['max_dd'] < -15 else Fore.YELLOW}{m['max_dd']:>6.1f}%{Style.RESET_ALL}  "
               f"{m['profit_factor']:>5.2f}  "
               f"{m['trades']:>6}  "
               f"{m['win_rate']:>7.1f}%  "
               f"{m['final']:>7.0f}€")
        print(row)
        rows.append({
            "bot": r["name"], "cagr": m["cagr"], "sharpe": m["sharpe"],
            "max_dd": m["max_dd"], "profit_factor": m["profit_factor"],
            "trades": m["trades"], "win_rate": m["win_rate"], "final": m["final"],
        })

    # Annual returns
    print(f"\n{Fore.CYAN}  PERFORMANCE ANNUELLE{Style.RESET_ALL}")
    years = sorted({y for r in bots for y in r.get("annual", {}).keys()})
    hdr2 = f"  {'Bot':<24}" + "".join(f"  {y:>7}" for y in years)
    print(hdr2)
    print("  " + "-" * (24 + 9 * len(years)))
    for r in bots:
        line = f"  {r['name']:<24}"
        for y in years:
            pct = r.get("annual", {}).get(y)
            if pct is not None:
                c = Fore.GREEN if pct > 0 else Fore.RED
                line += f"  {c}{pct:>+6.1f}%{Style.RESET_ALL}"
            else:
                line += "       —  "
        print(line)

    # Regime breakdown
    print(f"\n{Fore.CYAN}  PNL PAR RÉGIME (€ cumulé){Style.RESET_ALL}")
    regimes = ["BULL", "RANGE", "BEAR", "HIGH_VOL"]
    hdr3 = f"  {'Bot':<24}" + "".join(f"  {rg:>10}" for rg in regimes)
    print(hdr3)
    print("  " + "-" * (24 + 12 * len(regimes)))
    for r in bots:
        rb = r.get("regime", {})
        line = f"  {r['name']:<24}"
        for rg in regimes:
            v = rb.get(rg, 0)
            c = Fore.GREEN if v > 0 else (Fore.RED if v < 0 else Fore.WHITE)
            line += f"  {c}{v:>+9.1f}{Style.RESET_ALL}"
        print(line)

    print(f"\n{Fore.CYAN}{'='*100}{Style.RESET_ALL}")

    # CSV
    pd.DataFrame(rows).to_csv(f"{RESULTS_DIR}/multi_summary.csv", index=False)
    log(f"CSV sauvegardé : {RESULTS_DIR}/multi_summary.csv", Fore.GREEN)


def plot_equity_curves(results):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        colors = {"Bot A": "#58a6ff", "Bot B": "#3fb950", "Bot C": "#ffa657",
                  "Bot G": "#39d353", "Bot H": "#e06c75", "Bot I": "#79c0ff"}

        fig, ax = plt.subplots(figsize=(14, 7))
        fig.patch.set_facecolor("#0d1117")
        ax.set_facecolor("#161b22")

        for r in results.values():
            if not r["equity"] or not r["dates"]:
                continue
            eq_pct = [(v / INITIAL - 1) * 100 for v in r["equity"]]
            key = r["name"].split(" — ")[0]
            color = colors.get(key, "#8b949e")
            ax.plot(r["dates"], eq_pct, label=r["name"], color=color, linewidth=1.5)

        ax.axhline(0, color="#8b949e", linewidth=0.8, linestyle="--")
        ax.set_title("Backtest 3 ans — Equity curves (%)", color="#e6edf3", fontsize=13, fontweight="bold")
        ax.set_ylabel("Performance (%)", color="#8b949e")
        ax.tick_params(colors="#8b949e")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.xticks(rotation=35)
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363d")
        ax.grid(alpha=0.15, color="#30363d")
        ax.legend(loc="upper left", fontsize=9, facecolor="#161b22", labelcolor="#e6edf3")

        plt.tight_layout()
        path = f"{RESULTS_DIR}/multi_equity.png"
        plt.savefig(path, dpi=150, bbox_inches="tight", facecolor="#0d1117")
        plt.close()
        log(f"Graphique sauvegardé : {path}", Fore.GREEN)
    except Exception as e:
        log(f"Graphique ignoré: {e}", Fore.YELLOW)


# ── 12. MAIN ──────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    log(f"Démarrage backtest 3 ans — {len(config.SYMBOLS)} symboles")

    daily, vix_s, qqq_df = fetch_all_data()

    if not daily:
        log("Aucune donnée disponible — abandon.", Fore.RED)
        return

    bot_funcs = {
        "a": backtest_bot_a,
        "b": backtest_bot_b,
        "c": backtest_bot_c,
        "g": backtest_bot_g,
        "h": backtest_bot_h,
        "i": backtest_bot_i,
    }

    results = {}
    for key, fn in bot_funcs.items():
        try:
            r = fn(daily)
            r["metrics"] = compute_metrics(r["trades"], r["equity"])
            r["annual"]  = annual_returns(r["equity"], r["dates"]) if r["dates"] else {}
            r["regime"]  = regime_returns(r["trades"], vix_s, qqq_df)
            results[key] = r
            m = r["metrics"]
            log(f"  {r['name']}: CAGR={m['cagr']:+.1f}% | Sharpe={m['sharpe']:.2f} | "
                f"MaxDD={m['max_dd']:.1f}% | Trades={m['trades']}", Fore.GREEN)
        except Exception as e:
            import traceback
            log(f"Bot {key} ERREUR: {e}", Fore.RED)
            traceback.print_exc()

    if results:
        print_report(results, vix_s, qqq_df)
        plot_equity_curves(results)

    log(f"Terminé en {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

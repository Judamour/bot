#!/usr/bin/env python3
"""
backtest/multi_backtest.py
==========================
Backtest multi-période — 6 bots actifs (A, B, C, G, H, I) + Bot Z portfolio

Produit :
  - Tableau comparatif console (CAGR, Sharpe, MaxDD, PF, Trades, WinRate)
  - Performance par année (2020→2026)
  - Performance par régime (BULL / RANGE / BEAR / HIGH_VOL)
  - Simulation Bot Z : equal-weight vs régime pondéré vs hybride 70/30
  - backtest/results/multi_summary.csv
  - backtest/results/bot_z_comparison.csv
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
DAYS_CRYPTO  = 365 * 6 + 60          # ~6 ans crypto (Binance depuis 2020)
DAYS_STOCKS  = 365 * 4 + 60          # ~4 ans xStocks (yfinance)
DAYS         = DAYS_CRYPTO            # garde compat pour les fonctions bot
RESULTS_DIR  = "backtest/results"
os.makedirs(RESULTS_DIR, exist_ok=True)

# ── Constantes portefeuille hybride ───────────────────────────────────────────
# Base stable : G=30% (pilier), C=20% (défensif), A=20%, B=20%, cash=10%
BASE_WEIGHTS = {"g": 0.30, "a": 0.20, "b": 0.20, "c": 0.20, "cash": 0.10}
BASE_PCT     = 0.70    # 70% du capital en base stable
OVERLAY_PCT  = 0.30    # 30% en overlay Bot Z dynamique
MAX_BOT_WEIGHT = 0.40  # cap max par bot (base + overlay combiné)

BREAKOUT_SYMS = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]
VCB_SYMS      = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "NVDAx", "AMDx", "METAx", "PLTRx"]


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, color=Fore.CYAN):
    print(f"{color}[BACKTEST]{Style.RESET_ALL} {msg}")


# ── 1. DATA FETCH ─────────────────────────────────────────────────────────────

def fetch_all_data():
    """
    Fetch daily OHLCV pour tous les symboles + VIX + QQQ.
    Crypto : ~6 ans (depuis 2020, Binance)
    xStocks : ~4 ans (yfinance)
    """
    from data.fetcher import fetch_ohlcv

    log(f"Fetching données daily — crypto {DAYS_CRYPTO//365}ans / xStocks {DAYS_STOCKS//365}ans...")
    daily = {}
    for sym in config.SYMBOLS:
        is_crypto = sym in config.CRYPTO
        days = DAYS_CRYPTO if is_crypto else DAYS_STOCKS
        try:
            df = fetch_ohlcv(sym, "1d", days)
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
        vix_raw = yf.Ticker("^VIX").history(period="7y", interval="1d")["Close"]
        vix_raw.index = vix_raw.index.tz_localize(None)
    except Exception:
        vix_raw = pd.Series(dtype=float)

    try:
        qqq_raw = yf.Ticker("QQQ").history(period="7y", interval="1d")[["Close"]]
        qqq_raw.index = qqq_raw.index.tz_localize(None)
        qqq_raw["sma200"] = qqq_raw["Close"].rolling(200).mean()
    except Exception:
        qqq_raw = pd.DataFrame(columns=["Close", "sma200"])

    # BTC EMA200 pour le momentum overlay
    btc_df = daily.get("BTC/EUR")
    if btc_df is not None:
        btc_df = btc_df.copy()
        btc_df["ema200"] = btc_df["close"].ewm(span=200, adjust=False).mean()
        daily["BTC/EUR"] = btc_df

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

    # Sharpe corrigé : sur les returns actifs uniquement (filtre les jours plats)
    ret = pd.Series(eq).pct_change().dropna()
    active_ret = ret[ret.abs() > 1e-8]   # exclut les jours sans position (equity plate)
    if len(active_ret) > 10 and active_ret.std() > 0:
        sharpe = float(active_ret.mean() / active_ret.std() * math.sqrt(252))
    elif ret.std() > 0:
        sharpe = float(ret.mean() / ret.std() * math.sqrt(252))
    else:
        sharpe = 0

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
            if row["low"] <= pos["stop"]:
                exit_reason, ep = "atr_stop", pos["stop"]
            elif row["low"] < row["don_low"]:
                exit_reason, ep = "don_exit", float(row["don_low"])
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
    log("Bot I — RS Leaders (v2 : REBAL_DAYS=12, cooldown re-entry 10j)...")
    trades, equity, dates = [], [], []
    capital = INITIAL
    positions = {}
    TOP_N, EXIT_RANK = 3, 5
    REBAL_DAYS, ADX_MIN, VOL_MAX, EXT_MAX = 12, 18, 0.90, 0.15   # fix: 5→12 jours
    REENTRY_COOLDOWN = 10                                           # fix: cooldown re-entrée
    TARGET_VOL, MAX_POS_PCT = 0.15, 0.30
    ATR_TRAIL, HARD_STOP = 2.5, 0.10
    last_rebal = None
    recent_exits = {}   # {sym: date_exit} pour le cooldown

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
                capital += proceeds; trades.append(tr)
                del positions[sym]
                recent_exits[sym] = dt   # enregistre la date de sortie pour cooldown

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

            # Filtre cooldown : ne pas re-rentrer sur un actif sorti depuis < REENTRY_COOLDOWN jours
            to_buy = [
                s for s in qualified
                if s not in positions
                and (s not in recent_exits or (dt - recent_exits[s]).days >= REENTRY_COOLDOWN)
            ]
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


# ── 10b. BOT J — MEAN REVERSION (RSI(2) + Bollinger + SMA200) ────────────────

def backtest_bot_j_mean_reversion(daily_cache: dict) -> dict:
    """
    Bot J — Mean Reversion : stratégie anti-tendance, complémentaire aux bots trend.
    Profil : faible corrélation avec A/B/C/G, gagne quand le marché est choppy/range.

    Entrée LONG :
      - RSI(2) < 5        — survente extrême sur 2 barres
      - Close < Bollinger Lower (20j, 2σ) — extension vers le bas
      - Close > SMA200    — filtre : ne pas acheter contre une tendance baissière majeure

    Sortie :
      - RSI(2) > 60 (retour à la normale)
      - OU close > SMA20 (milieu Bollinger)

    Stop : 1.5 × ATR14 sous l'entrée
    Sizing : 0.5% du capital par trade, max 10% par position
    """
    log("Bot J — Mean Reversion (RSI2 + Bollinger + SMA200)...")

    RISK_PCT    = 0.005   # 0.5% du capital par trade
    ATR_MULT    = 1.5     # stop = 1.5 × ATR14
    MAX_POS_PCT = 0.10    # max 10% du capital par position

    trades  = []
    equity  = []
    dates   = []
    capital = INITIAL

    # Universel : tous les symboles valides avec assez de données
    syms = sorted(daily_cache.keys())

    # Pré-calcul des indicateurs par symbole
    sigs = {}
    for sym, df in daily_cache.items():
        if df is None or len(df) < 210:   # SMA200 + warmup
            continue
        c = df["close"]
        h = df["high"]
        lo = df["low"]

        # RSI(2)
        delta = c.diff()
        gain = delta.clip(lower=0).rolling(2).mean()
        loss = (-delta.clip(upper=0)).rolling(2).mean()
        rs   = gain / (loss + 1e-10)
        rsi2 = 100 - 100 / (1 + rs)

        # Bollinger Bands (20, 2σ)
        bb_mid   = c.rolling(20).mean()
        bb_std   = c.rolling(20).std()
        bb_lower = bb_mid - 2 * bb_std

        # SMA200
        sma200 = c.rolling(200).mean()

        # ATR14
        atr14 = compute_atr(h, lo, c, 14)

        sig = pd.DataFrame({
            "close": c, "rsi2": rsi2, "bb_lower": bb_lower,
            "bb_mid": bb_mid, "sma200": sma200, "atr14": atr14,
        })
        sig.dropna(inplace=True)
        if len(sig) > 0:
            sigs[sym] = sig

    if not sigs:
        return {"trades": [], "equity": [INITIAL], "dates": [], "name": "Bot J — Mean Reversion"}

    # Dates communes (tous les jours où au moins un symbole est disponible)
    all_dates = sorted({d for sig in sigs.values() for d in sig.index})

    positions = {}   # {sym: {"entry": price, "stop": price, "size": size, "cost": cost}}

    for dt in all_dates:
        prices = {sym: float(sigs[sym].loc[dt, "close"])
                  for sym in sigs if dt in sigs[sym].index}

        # ── Gérer les stops et les sorties ──────────────────────────────────
        to_close = []
        for sym, pos in positions.items():
            if sym not in prices:
                continue
            px = prices[sym]
            # Stop hit
            if px <= pos["stop"]:
                pnl = (pos["stop"] - pos["entry"]) * pos["size"]  # négatif
                cost_close = _cost(pos["size"], pos["stop"]) - pos["cost"]
                capital += pos["cost"] + pnl - abs(cost_close) * FEE
                trades.append({"pnl": pnl - abs(cost_close) * FEE, "exit": "stop"})
                to_close.append(sym)
                continue
            # RSI exit ou retour au milieu Bollinger
            if sym in sigs and dt in sigs[sym].index:
                row = sigs[sym].loc[dt]
                if row["rsi2"] > 60 or px > row["bb_mid"]:
                    pnl = (px - pos["entry"]) * pos["size"]
                    cost_close = pos["size"] * px * FEE
                    net_pnl = pnl - cost_close
                    capital += pos["cost"] + net_pnl
                    trades.append({"pnl": net_pnl, "exit": "rsi_exit"})
                    to_close.append(sym)
        for sym in to_close:
            del positions[sym]

        # ── Chercher des signaux d'entrée ────────────────────────────────────
        for sym, px in prices.items():
            if sym in positions:
                continue
            if sym not in sigs or dt not in sigs[sym].index:
                continue
            row = sigs[sym].loc[dt]
            if pd.isna(row["rsi2"]) or pd.isna(row["bb_lower"]) or pd.isna(row["sma200"]):
                continue
            # Signal MR : survente extrême + extension bas + au-dessus tendance
            if (row["rsi2"] < 5
                    and px < row["bb_lower"]
                    and px > row["sma200"]
                    and row["atr14"] > 0):
                ep   = _entry(px)
                stop = ep - ATR_MULT * row["atr14"]
                risk = ep - stop
                if risk <= 0:
                    continue
                size = min(capital * RISK_PCT / risk,
                           capital * MAX_POS_PCT / ep)
                size = max(size, 0)
                cost = _cost(size, ep)
                if cost <= capital and size > 0:
                    capital -= cost
                    positions[sym] = {"entry": ep, "stop": stop,
                                      "size": size, "cost": cost}

        pv = capital + sum(prices.get(s, p["entry"]) * p["size"] for s, p in positions.items())
        equity.append(pv)
        dates.append(dt)

    return {"trades": trades, "equity": equity, "dates": dates, "name": "Bot J — Mean Reversion"}


# ── 11. BOT Z — PORTFOLIO SIMULÉ ─────────────────────────────────────────────

# Poids Bot Z pur par régime — CALIBRATION V2 (validée backtest 2020-2026)
# BEAR corrigé : C=1.5 et G=1.2 (seuls défensifs prouvés en 2022 : -2.5% et -3.4%)
REGIME_WEIGHTS_Z = {
    "BULL":     {"a": 0.8,  "b": 1.0, "c": 0.5, "g": 1.2},
    "RANGE":    {"a": 1.0,  "b": 0.8, "c": 0.7, "g": 0.8},
    "BEAR":     {"a": 0.3,  "b": 0.0, "c": 1.5, "g": 1.2},
    "HIGH_VOL": {"a": 0.5,  "b": 0.3, "c": 1.0, "g": 0.8},
}

# Bots valides pour le portefeuille (H=0 trades, I=bug)
VALID_BOTS_Z = ["a", "b", "c", "g"]


def _get_regime_at_dt(dt, vix_s, qqq_df) -> str:
    """Détecte le régime pour une date donnée (normalise tz)."""
    try:
        dt_norm = pd.Timestamp(dt).normalize()
        if dt_norm.tzinfo is not None:
            dt_norm = dt_norm.tz_localize(None)
        vix = float(vix_s.asof(dt_norm)) if not vix_s.empty else 15.0
        # Support Close ou close dans le DataFrame
        col_close = "Close" if "Close" in qqq_df.columns else "close"
        col_sma   = "sma200"
        qqq_close  = float(qqq_df[col_close].asof(dt_norm))
        qqq_sma200 = float(qqq_df[col_sma].asof(dt_norm))
        return classify_regime(vix, qqq_close, qqq_sma200)
    except Exception:
        return "RANGE"


def _resample_weekly(dates, equity):
    """Resample a daily equity curve to weekly (last day of each week).
    Eliminates daily mark-to-market noise from open crypto positions.
    """
    if not dates or not equity:
        return dates, equity
    s = pd.Series(equity, index=pd.DatetimeIndex(dates))
    ws = s.resample("W").last().dropna()
    return list(ws.index), list(ws.values)


def _metrics_portfolio(equity_list, dates_list, init, weekly=False):
    """Métriques sur une equity curve (CAGR, Sharpe, MaxDD)."""
    if not equity_list or len(equity_list) < 2:
        return {"cagr": 0, "sharpe": 0, "max_dd": 0, "final": init,
                "profit_factor": 0, "trades": 0, "win_rate": 0}
    eq = np.array(equity_list, dtype=float)
    n_years = (dates_list[-1] - dates_list[0]).days / 365.25
    cagr = ((eq[-1] / init) ** (1 / n_years) - 1) * 100 if n_years > 0.1 else 0
    ret = pd.Series(eq).pct_change().dropna()
    ann_factor = math.sqrt(52) if weekly else math.sqrt(252)
    sharpe = float(ret.mean() / ret.std() * ann_factor) if ret.std() > 0 else 0
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / (peak + 1e-10) * 100).min())
    return {"cagr": round(cagr, 2), "sharpe": round(sharpe, 2),
            "max_dd": round(max_dd, 2), "final": round(float(eq[-1]), 2),
            "profit_factor": 0, "trades": 0, "win_rate": 0}


def backtest_bot_z_portfolio(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame) -> dict:
    """
    Simule 3 structures de portefeuille pour les bots valides (A, B, C, G) :

    1. Equal-Weight      : 25% sur chaque bot, rebalancé daily
    2. Bot Z pur         : 100% allocation régime dynamique (BULL/RANGE/BEAR/HIGH_VOL)
    3. Hybride 70/30     : 70% base fixe (G=30%,A=20%,B=20%,C=15%,cash=15%)
                           + 30% overlay Bot Z dynamique (±10-15% ajustement)
                           avec cap par bot à MAX_BOT_WEIGHT

    Chaque structure part du même capital total = INITIAL × n_bots.
    """
    log("Bot Z — 3 structures portfolio (equal / régime / hybride 70-30)...")

    # Equity curves des bots valides uniquement
    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if len(valid) < 2:
        log("Bot Z: pas assez de bots valides.", Fore.YELLOW)
        return {}

    # Rééchantillonner à fréquence hebdomadaire (élimine le bruit MtM journalier crypto)
    weekly_valid = {}
    for k, r in valid.items():
        wd, we = _resample_weekly(r["dates"], r["equity"])
        weekly_valid[k] = {"dates": wd, "equity": we}

    # Intersection des dates communes (hebdomadaires)
    date_sets = [set(r["dates"]) for r in weekly_valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        log("Bot Z: pas de dates communes.", Fore.YELLOW)
        return {}

    # Normalisé : returns hebdomadaires de chaque bot (base 1.0 = départ)
    bot_norm = {}
    for k, r in weekly_valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    n_bots       = len(valid)
    initial_total = INITIAL * n_bots   # 4000€

    # ── Simulation par retours quotidiens (rebalancing correct) ───────────────
    # Chaque jour : retour pondéré des bots → composé sur le capital total.
    # Évite le biais des ratios cumulés (qui donne des résultats aberrants
    # quand les bots ont des niveaux de performance très différents).
    eq_equal  = [initial_total]
    eq_z      = [initial_total]
    eq_hybrid = [initial_total]
    dates_out = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]
        regime  = _get_regime_at_dt(dt, vix_s, qqq_df)

        # Retour quotidien de chaque bot (ratio t / ratio t-1 - 1)
        bot_r = {}
        for k in valid:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k][dt]
            bot_r[k] = (c / p - 1) if p > 0 else 0.0

        # ── 1. Equal-Weight (rebalancing quotidien) ──────────────────────
        r_eq = sum(bot_r[k] / n_bots for k in valid)
        eq_equal.append(eq_equal[-1] * (1 + r_eq))

        # ── 2. Bot Z pur — allocation régime ────────────────────────────
        raw_w   = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])
        total_w = sum(raw_w.get(k, 0) for k in valid) or 1.0
        w_z     = {k: raw_w.get(k, 0) / total_w for k in valid}
        r_z     = sum(w_z[k] * bot_r[k] for k in valid)
        eq_z.append(eq_z[-1] * (1 + r_z))

        # ── 3. Hybride 70/30 ────────────────────────────────────────────
        # Base stable (70%) : poids fixes G/A/B/C
        base_vals = {k: BASE_WEIGHTS.get(k, 0.0) for k in valid}
        cash_frac = BASE_WEIGHTS.get("cash", 0.10)
        base_bot_sum = sum(base_vals.values())
        if base_bot_sum > 0:
            base_scaled = {k: base_vals[k] / base_bot_sum * (1 - cash_frac) for k in valid}
        else:
            base_scaled = {k: (1 - cash_frac) / n_bots for k in valid}

        # Overlay (30%) : régime dynamique
        overlay_sum = sum(raw_w.get(k, 0) for k in valid) or 1.0
        overlay_scaled = {k: raw_w.get(k, 0) / overlay_sum for k in valid}

        # Poids final : 70% base + 30% overlay, cap par bot
        current_cash = cash_frac
        w_hybrid = {}
        for k in valid:
            w = BASE_PCT * base_scaled[k] + OVERLAY_PCT * overlay_scaled[k]
            if w > MAX_BOT_WEIGHT:
                current_cash += w - MAX_BOT_WEIGHT
                w = MAX_BOT_WEIGHT
            w_hybrid[k] = w

        # Retour portefeuille (cash = 0% rendement)
        r_hybrid = sum(w_hybrid[k] * bot_r[k] for k in valid)
        eq_hybrid.append(eq_hybrid[-1] * (1 + r_hybrid))

        dates_out.append(dt)

    # Métriques (weekly=True car equity curves resamplées hebdomadairement)
    m_equal  = _metrics_portfolio(eq_equal,  dates_out, initial_total, weekly=True)
    m_z      = _metrics_portfolio(eq_z,      dates_out, initial_total, weekly=True)
    m_hybrid = _metrics_portfolio(eq_hybrid, dates_out, initial_total, weekly=True)

    # Retours annuels
    ann_equal  = annual_returns(eq_equal,  dates_out)
    ann_z      = annual_returns(eq_z,      dates_out)
    ann_hybrid = annual_returns(eq_hybrid, dates_out)

    return {
        "equal": {
            "name": "Equal-Weight (A+B+C+G)",
            "equity": eq_equal, "dates": dates_out, "trades": [],
            "metrics": m_equal, "annual": ann_equal, "regime": {},
        },
        "z": {
            "name": "Bot Z — Régime pur",
            "equity": eq_z, "dates": dates_out, "trades": [],
            "metrics": m_z, "annual": ann_z, "regime": {},
        },
        "hybrid": {
            "name": "Hybride 70/30 (Base+Bot Z)",
            "equity": eq_hybrid, "dates": dates_out, "trades": [],
            "metrics": m_hybrid, "annual": ann_hybrid, "regime": {},
        },
    }


# ── 12. BOT Z AMÉLIORÉ — Momentum Overlay + Circuit Breaker ──────────────────

def backtest_bot_z_enhanced(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                             daily_cache: dict) -> dict:
    """
    Bot Z enhanced : toutes les couches de protection.
      - Momentum Overlay  : si BTC < EMA200 ET QQQ < SMA200 → force BEAR weights
      - Circuit Breaker   : si portfolio DD > 25% → réduit exposure 50%
      - Volatility scaling: overlay modulé par vol récente (déjà dans les poids)
    Retourne equity/dates/metrics compatibles avec les autres structures.
    """
    log("Bot Z Enhanced — Momentum Overlay + Circuit Breaker...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if len(valid) < 2:
        return {}

    # Rééchantillonner à fréquence hebdomadaire (élimine le bruit MtM journalier crypto)
    weekly_valid = {}
    for k, r in valid.items():
        wd, we = _resample_weekly(r["dates"], r["equity"])
        weekly_valid[k] = {"dates": wd, "equity": we}

    date_sets = [set(r["dates"]) for r in weekly_valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots = len(valid)
    initial_total = INITIAL * n_bots

    bot_norm = {}
    for k, r in weekly_valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    # BTC EMA200 series
    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    # Circuit breaker state
    cb_peak   = initial_total
    cb_factor = 1.0          # 1.0 = plein, 0.5 = réduit
    CB_THRESHOLD  = -0.25    # -25% DD
    CB_RECOVERY   = 0.005    # +0.5%/jour de récupération progressive

    eq_enhanced = [initial_total]
    dates_out   = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # Régime de base
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)

        # Momentum Overlay
        try:
            dt_norm = pd.Timestamp(dt).normalize().tz_localize(None)
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
        except Exception:
            qqq_bearish = False

        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])

        # Si les deux indicateurs macro sont baissiers → force BEAR
        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        # Si seulement un des deux → HIGH_VOL (prudence)
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        # Poids par régime
        raw_w   = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])
        total_w = sum(raw_w.get(k, 0) for k in valid) or 1.0
        w_z     = {k: raw_w.get(k, 0) / total_w for k in valid}

        # Retours quotidiens
        bot_r = {}
        for k in valid:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k][dt]
            bot_r[k] = (c / p - 1) if p > 0 else 0.0

        # Retour portefeuille brut
        r_port = sum(w_z[k] * bot_r[k] for k in valid)

        # Circuit Breaker : réduit l'exposition si DD > seuil
        current_pv = eq_enhanced[-1]
        if current_pv > cb_peak:
            cb_peak   = current_pv
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)  # récupération progressive
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0
        if port_dd < CB_THRESHOLD:
            cb_factor = max(0.3, cb_factor - 0.05)  # déclenche réduction rapide
        elif port_dd > -0.10:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)

        # Applique le CB : partie réduite reste en cash (0%)
        r_final = cb_factor * r_port

        eq_enhanced.append(eq_enhanced[-1] * (1 + r_final))
        dates_out.append(dt)

    m = _metrics_portfolio(eq_enhanced, dates_out, initial_total, weekly=True)
    ann = annual_returns(eq_enhanced, dates_out)
    return {
        "name": "Bot Z Enhanced (MO + CB)",
        "equity": eq_enhanced, "dates": dates_out, "trades": [],
        "metrics": m, "annual": ann, "regime": {},
    }


# ── 13b. BOT Z PRO — Volatility Targeting + Adaptive Score + Corr Spike ──────

def backtest_bot_z_pro(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                       daily_cache: dict) -> dict:
    """
    Bot Z Pro — architecture hedge fund (toutes les couches) :
      1. Momentum Overlay    : BTC EMA200 + QQQ SMA200 → force régime
      2. Volatility Targeting: pondère chaque bot pour une vol annuelle cible (20%)
                               → contributions au risque égales entre bots
      3. Adaptive Scoring    : rolling 90j Sharpe par bot → modifie les poids régime
                               (poids × [0.5, 2.0] selon performance récente)
      4. Correlation Spike   : si corrélation inter-bots moyenne > 70% sur 20j
                               → réduit exposition totale (bénéfice diversification ↓)
      5. Multi-tier CB       : DD>-10%→expo×0.80 | DD>-20%→×0.50 | DD>-30%→×0.30
    """
    log("Bot Z Pro — Vol Targeting + Adaptive Score + Corr Spike + Multi-tier CB...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if len(valid) < 2:
        return {}

    date_sets = [set(r["dates"]) for r in valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots        = len(valid)
    initial_total = INITIAL * n_bots

    bot_norm = {}
    for k, r in valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    # BTC EMA200 series (momentum overlay)
    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    TARGET_VOL  = 0.20   # vol annuelle cible pour chaque bot (20%)
    VOL_WIN     = 20     # fenêtre vol réalisée (jours)
    SHARPE_WIN  = 90     # fenêtre rolling Sharpe (jours)
    CORR_WIN    = 20     # fenêtre corrélation inter-bots (jours)
    CORR_THRESH = 0.70   # seuil corrélation → réduction expo
    CB_RECOVERY = 0.005  # récupération CB +0.5%/jour

    ret_history = {k: [] for k in valid}
    cb_peak     = initial_total
    cb_factor   = 1.0

    eq_pro    = [initial_total]
    dates_out = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # ── Régime de base + Momentum Overlay ───────────────────────────────
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)
        try:
            dt_norm    = pd.Timestamp(dt).normalize().tz_localize(None)
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
        except Exception:
            qqq_bearish = False

        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])

        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        raw_w = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])

        # Retours quotidiens
        bot_r = {}
        for k in valid:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k][dt]
            bot_r[k] = (c / p - 1) if p > 0 else 0.0
            ret_history[k].append(bot_r[k])

        # ── 1. Volatility Targeting ──────────────────────────────────────────
        vol_scale = {}
        for k in valid:
            if len(ret_history[k]) >= VOL_WIN:
                daily_vol  = float(np.std(ret_history[k][-VOL_WIN:]))
                annual_vol = daily_vol * math.sqrt(252) if daily_vol > 1e-8 else TARGET_VOL
                vol_scale[k] = min(TARGET_VOL / annual_vol, 3.0)
            else:
                vol_scale[k] = 1.0

        # ── 2. Adaptive Scoring (rolling Sharpe 90j) ─────────────────────────
        score = {}
        for k in valid:
            if len(ret_history[k]) >= SHARPE_WIN:
                s = np.array(ret_history[k][-SHARPE_WIN:])
                sharpe_r = float(s.mean() / s.std() * math.sqrt(252)) if s.std() > 1e-8 else 0.0
                # Convertit en multiplicateur [0.5, 2.0] autour de 1.0
                score[k] = max(0.5, min(2.0, 1.0 + sharpe_r / 4))
            else:
                score[k] = 1.0

        # ── Poids combinés : régime × vol_scale × score ─────────────────────
        w_raw   = {k: raw_w.get(k, 0) * vol_scale[k] * score[k] for k in valid}
        total_w = sum(w_raw.values()) or 1.0
        w_final = {k: w_raw[k] / total_w for k in valid}

        # ── 3. Correlation Spike ─────────────────────────────────────────────
        corr_factor = 1.0
        if len(ret_history[list(valid.keys())[0]]) >= CORR_WIN:
            rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in valid])
            if rets_mat.shape[0] > 1:
                try:
                    corr_m   = np.corrcoef(rets_mat)
                    n        = corr_m.shape[0]
                    off_diag = [corr_m[ii, jj] for ii in range(n) for jj in range(ii + 1, n)]
                    avg_corr = float(np.mean(off_diag)) if off_diag else 0.0
                    if avg_corr > CORR_THRESH:
                        excess      = (avg_corr - CORR_THRESH) / max(0.95 - CORR_THRESH, 1e-4)
                        corr_factor = 1.0 - 0.5 * min(excess, 1.0)
                except Exception:
                    corr_factor = 1.0

        # ── Retour portefeuille brut ─────────────────────────────────────────
        r_port = sum(w_final[k] * bot_r[k] for k in valid)

        # ── 4. Multi-tier Circuit Breaker ────────────────────────────────────
        current_pv = eq_pro[-1]
        if current_pv > cb_peak:
            cb_peak = current_pv
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0.0

        if port_dd < -0.30:
            cb_factor = max(0.30, cb_factor - 0.05)   # réduction agressive
        elif port_dd < -0.20:
            cb_factor = max(0.50, cb_factor - 0.03)
        elif port_dd < -0.10:
            cb_factor = max(0.80, cb_factor - 0.01)
        elif port_dd > -0.05:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)  # récupération progressive

        # Exposition finale : CB × corrélation × r_port
        r_final = cb_factor * corr_factor * r_port

        eq_pro.append(eq_pro[-1] * (1 + r_final))
        dates_out.append(dt)

    m   = _metrics_portfolio(eq_pro, dates_out, initial_total)
    ann = annual_returns(eq_pro, dates_out)
    return {
        "name":    "Bot Z Pro (VT+AS+CS+CB)",
        "equity":  eq_pro, "dates": dates_out, "trades": [],
        "metrics": m, "annual": ann, "regime": {},
    }


# ── 13c. BOT Z ADAPTIVE — Meta Regime Switch (Enhanced / Balanced / Pro) ──────

# Configs des 3 profils — seuls les paramètres de risk management diffèrent.
# Tous utilisent les mêmes REGIME_WEIGHTS_Z (calibration v2) et le MO.
ADAPTIVE_PROFILES = {
    "ENHANCED": {
        # Max croissance — vol targeting off, CB simple -25%, corr quasi-inactif
        "target_vol":   None,   # pas de vol targeting
        "corr_thresh":  0.90,   # seuil corrélation très haut (quasi-désactivé)
        "corr_reduce":  0.30,
        "cb_tiers":     [(-0.25, 0.30)],          # single tier comme Enhanced
    },
    "BALANCED": {
        # Compromis — vol targeting modéré, CB 2-tiers, corr intermédiaire
        "target_vol":   0.25,
        "corr_thresh":  0.75,
        "corr_reduce":  0.30,
        "cb_tiers":     [(-0.20, 0.50), (-0.30, 0.30)],
    },
    "PRO": {
        # Protection max — vol targeting strict, CB 3-tiers, corr sensible
        "target_vol":   0.20,
        "corr_thresh":  0.70,
        "corr_reduce":  0.50,
        "cb_tiers":     [(-0.10, 0.80), (-0.20, 0.50), (-0.30, 0.30)],
    },
}

# Jours de confirmation requis pour quitter chaque profil (hysteresis asymétrique)
# Bear→Bull : lent (évite les faux signaux) | Bull→Bear : rapide (protection)
ADAPTIVE_HYSTERESIS = {
    "ENHANCED": 7,   # 7 jours dans ENHANCED avant de pouvoir basculer
    "BALANCED": 5,
    "PRO":      3,   # 3 jours dans PRO — on sort vite si le marché rebondit
}


def _select_profile_raw(vix: float, btc_bearish: bool, qqq_bearish: bool,
                        port_dd: float, avg_corr: float = 0.0) -> str:
    """
    Profil brut selon market state (sans hysteresis).
    PRO      : dès que conditions défensives sont présentes
    ENHANCED : bull propre sur tous les critères
    BALANCED : tout le reste (transition, bull fragile)
    """
    # PRO : au moins une condition défensive forte
    if ((btc_bearish and qqq_bearish)
            or vix > 28
            or avg_corr > 0.70
            or port_dd < -0.12):
        return "PRO"
    # ENHANCED : bull propre sur tous les critères
    if (not btc_bearish and not qqq_bearish
            and vix < 22
            and port_dd > -0.05
            and avg_corr < 0.60):
        return "ENHANCED"
    # BALANCED : transition / bull fragile
    return "BALANCED"


def backtest_bot_z_adaptive(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                             daily_cache: dict) -> dict:
    """
    Bot Z Adaptive : méta-switch entre 3 profils selon régime + hysteresis.

    Profiles :
      ENHANCED → max CAGR (bull propre) : Enhanced parameters
      BALANCED → compromis (transition)  : risk management intermédiaire
      PRO      → protection (bear/stress): vol targeting + multi-tier CB + corr spike

    Hysteresis :
      Évite le flip-flop. Délai de confirmation avant switch :
        ENHANCED → switch : 7j | BALANCED : 5j | PRO → switch : 3j (protection rapide)

    Switch déclenché par : VIX, BTC trend, QQQ SMA200, corrélation inter-bots, DD port.
    """
    log("Bot Z Adaptive — Meta Regime Switch (Enhanced / Balanced / Pro)...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if len(valid) < 2:
        return {}

    date_sets = [set(r["dates"]) for r in valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots        = len(valid)
    initial_total = INITIAL * n_bots

    bot_norm = {}
    for k, r in valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    CB_RECOVERY = 0.005
    VOL_WIN     = 20
    CORR_WIN    = 20

    ret_history   = {k: [] for k in valid}
    cb_peak       = initial_total
    cb_factor     = 1.0

    # Hysteresis state
    current_profile = "ENHANCED"
    pending_profile = None
    days_pending    = 0

    eq_adaptive = [initial_total]
    dates_out   = [common_dates[0]]
    profile_log = []

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # ── Régime de base + Momentum Overlay ───────────────────────────────
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)
        try:
            dt_norm    = pd.Timestamp(dt).normalize().tz_localize(None)
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
            vix = float(vix_s.asof(dt_norm)) if not vix_s.empty else 15.0
        except Exception:
            qqq_bearish = False
            vix = 15.0

        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])

        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        # Retours quotidiens
        bot_r = {}
        for k in valid:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k][dt]
            bot_r[k] = (c / p - 1) if p > 0 else 0.0
            ret_history[k].append(bot_r[k])

        # ── Corrélation inter-bots (pour profile selector) ──────────────────
        avg_corr = 0.0
        if len(ret_history[list(valid.keys())[0]]) >= CORR_WIN:
            rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in valid])
            if rets_mat.shape[0] > 1:
                try:
                    corr_m = np.corrcoef(rets_mat)
                    n_c    = corr_m.shape[0]
                    off    = [corr_m[ii, jj] for ii in range(n_c) for jj in range(ii + 1, n_c)]
                    avg_corr = float(np.mean(off)) if off else 0.0
                except Exception:
                    avg_corr = 0.0

        # ── DD portefeuille ──────────────────────────────────────────────────
        current_pv = eq_adaptive[-1]
        if current_pv > cb_peak:
            cb_peak = current_pv
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0.0

        # ── Profile selector avec hysteresis ────────────────────────────────
        raw_profile = _select_profile_raw(vix, btc_bearish, qqq_bearish, port_dd, avg_corr)

        if raw_profile != current_profile:
            if pending_profile == raw_profile:
                days_pending += 1
            else:
                pending_profile = raw_profile
                days_pending    = 1
            min_days = ADAPTIVE_HYSTERESIS.get(current_profile, 5)
            if days_pending >= min_days:
                current_profile = pending_profile
                pending_profile = None
                days_pending    = 0
                cb_factor = min(1.0, cb_factor + 0.02)  # léger reset CB au switch
        else:
            pending_profile = None
            days_pending    = 0

        profile_log.append((dt, current_profile))
        cfg = ADAPTIVE_PROFILES[current_profile]

        # ── Poids régime (calibration v2, identique pour tous les profils) ──
        raw_w   = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])
        total_w = sum(raw_w.get(k, 0) for k in valid) or 1.0
        w_base  = {k: raw_w.get(k, 0) / total_w for k in valid}

        # ── Volatility Targeting (selon profil) ─────────────────────────────
        vol_scale = {k: 1.0 for k in valid}
        if cfg["target_vol"] is not None and len(ret_history[list(valid.keys())[0]]) >= VOL_WIN:
            for k in valid:
                dv  = float(np.std(ret_history[k][-VOL_WIN:]))
                av  = dv * math.sqrt(252) if dv > 1e-8 else cfg["target_vol"]
                vol_scale[k] = min(cfg["target_vol"] / av, 3.0)

        # ── Poids combinés ───────────────────────────────────────────────────
        w_raw    = {k: w_base[k] * vol_scale[k] for k in valid}
        total_w2 = sum(w_raw.values()) or 1.0
        w_final  = {k: w_raw[k] / total_w2 for k in valid}

        # ── Correlation Spike (selon profil) ─────────────────────────────────
        corr_factor = 1.0
        if avg_corr > cfg["corr_thresh"]:
            excess      = (avg_corr - cfg["corr_thresh"]) / max(0.95 - cfg["corr_thresh"], 1e-4)
            corr_factor = 1.0 - cfg["corr_reduce"] * min(excess, 1.0)

        # ── Retour portefeuille brut ─────────────────────────────────────────
        r_port = sum(w_final[k] * bot_r[k] for k in valid)

        # ── Multi-tier Circuit Breaker (selon profil) ────────────────────────
        tiers = sorted(cfg["cb_tiers"], key=lambda x: x[0])
        target_factor = 1.0
        for (dd_thresh, factor) in tiers:
            if port_dd < dd_thresh:
                target_factor = factor

        if target_factor < cb_factor:
            cb_factor = max(target_factor, cb_factor - 0.05)
        elif port_dd > -0.05:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)

        r_final = cb_factor * corr_factor * r_port
        eq_adaptive.append(eq_adaptive[-1] * (1 + r_final))
        dates_out.append(dt)

    m   = _metrics_portfolio(eq_adaptive, dates_out, initial_total)
    ann = annual_returns(eq_adaptive, dates_out)

    # Statistiques profils
    profile_counts = {}
    for _, p in profile_log:
        profile_counts[p] = profile_counts.get(p, 0) + 1
    total_days = len(profile_log) or 1
    profile_pct = {p: round(profile_counts.get(p, 0) / total_days * 100, 1)
                   for p in ["ENHANCED", "BALANCED", "PRO"]}

    return {
        "name":          "Bot Z Adaptive (E/B/P)",
        "equity":        eq_adaptive, "dates": dates_out, "trades": [],
        "metrics":       m, "annual": ann, "regime": {},
        "profile_stats": profile_pct,
    }


# ── 13d. BOT Z OMEGA — Dynamic Portfolio Optimizer (ER + Risk + Corr) ─────────

def backtest_bot_z_omega(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                         daily_cache: dict) -> dict:
    """
    Bot Z Omega — Portfolio Optimizer dynamique (remplace les poids régime fixes) :
      1. Expected Return Engine  : ER_score = 0.35×Sharpe_90d + 0.25×PF_90d
                                             + 0.20×equity_slope_60d + 0.20×regime_fit
      2. Risk Engine             : risk_score = 0.4×vol_20d + 0.3×downside_vol
                                               + 0.3×current_dd_abs
      3. Correlation Penalty     : pénalise les bots redondants (marginal corr)
      4. Softmax weights         : final_score = (ER - Risk) × corr_penalty → softmax
      5. Momentum Overlay        : BTC EMA200 + QQQ SMA200 → scale expo globale en BEAR
      6. Circuit Breaker         : DD > -25% → expo × 0.30 (single-tier comme Enhanced)
    """
    log("Bot Z Omega — Expected Return Engine + Risk Engine + Corr Penalty...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if len(valid) < 2:
        return {}

    # Rééchantillonner à fréquence hebdomadaire (élimine le bruit MtM journalier crypto)
    weekly_valid = {}
    for k, r in valid.items():
        wd, we = _resample_weekly(r["dates"], r["equity"])
        weekly_valid[k] = {"dates": wd, "equity": we}

    date_sets = [set(r["dates"]) for r in weekly_valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots        = len(valid)
    initial_total = INITIAL * n_bots

    bot_norm = {}
    for k, r in weekly_valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    # BTC EMA200 (momentum overlay)
    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    SHARPE_WIN   = 90
    VOL_WIN      = 20
    SLOPE_WIN    = 60
    CORR_WIN     = 20
    SOFTMAX_BETA = 3.0   # concentration des poids (plus élevé = plus concentré)
    CB_THRESHOLD = -0.25
    CB_MIN_FACTOR = 0.30
    CB_RECOVERY  = 0.005

    ret_history = {k: [] for k in valid}
    eq_history  = {k: [] for k in valid}   # equity normalisée pour le slope
    bot_peaks   = {k: 1.0 for k in valid}  # peak individuel pour dd par bot

    cb_peak   = initial_total
    cb_factor = 1.0

    eq_omega  = [initial_total]
    dates_out = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # ── Retours quotidiens + historique ─────────────────────────────────
        bot_r = {}
        for k in valid:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k].get(dt, p)
            bot_r[k] = (c / p - 1) if p > 0 else 0.0
            ret_history[k].append(bot_r[k])
            eq_val = bot_norm[k].get(dt, 1.0)
            eq_history[k].append(eq_val)
            if eq_val > bot_peaks[k]:
                bot_peaks[k] = eq_val

        # ── Régime + Momentum Overlay ────────────────────────────────────────
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)
        try:
            dt_norm    = pd.Timestamp(dt).normalize().tz_localize(None)
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
        except Exception:
            qqq_bearish = False

        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])

        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        raw_regime_w = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])

        # ── Calcul ER + Risk par bot ─────────────────────────────────────────
        warmup = min(len(ret_history[k]) for k in valid)
        ks = list(valid.keys())

        er_comp   = {k: {} for k in ks}
        risk_comp = {k: {} for k in ks}

        for k in ks:
            hist = ret_history[k]
            eq_h = eq_history[k]

            if warmup >= SHARPE_WIN:
                # Sharpe 90j
                r90 = np.array(hist[-SHARPE_WIN:])
                sharpe_90 = (float(r90.mean() / r90.std() * math.sqrt(252))
                             if r90.std() > 1e-8 else 0.0)

                # Profit Factor 90j
                pos_sum = sum(r for r in hist[-SHARPE_WIN:] if r > 0)
                neg_sum = abs(sum(r for r in hist[-SHARPE_WIN:] if r < 0))
                pf_90 = min((pos_sum / neg_sum) if neg_sum > 1e-8 else 3.0, 5.0)

                # Slope equity 60j (annualisé, normalisé par niveau de départ)
                if len(eq_h) >= SLOPE_WIN:
                    eq_s = np.array(eq_h[-SLOPE_WIN:])
                    x = np.arange(len(eq_s))
                    slope = (float(np.polyfit(x, eq_s / max(eq_s[0], 1e-8), 1)[0]) * 252
                             if eq_s.std() > 1e-8 else 0.0)
                else:
                    slope = 0.0

                # Regime fit : poids régime normalisé par moyenne equal-weight
                total_rw = sum(raw_regime_w.values()) or 1.0
                regime_fit = raw_regime_w.get(k, 0) / (total_rw / n_bots)

                er_comp[k] = {"sharpe": sharpe_90, "pf": pf_90,
                              "slope": slope, "regime_fit": regime_fit}

                # Risque 20j
                r20 = np.array(hist[-VOL_WIN:])
                vol_20 = float(r20.std() * math.sqrt(252)) if r20.std() > 1e-8 else 0.01
                down_r = r20[r20 < 0]
                down_vol = (float(down_r.std() * math.sqrt(252))
                            if len(down_r) > 1 and down_r.std() > 1e-8 else vol_20)
                dd_k = abs(eq_history[k][-1] / bot_peaks[k] - 1) if bot_peaks[k] > 0 else 0.0

                risk_comp[k] = {"vol": vol_20, "down_vol": down_vol, "dd": dd_k}
            else:
                # Warm-up : composantes neutres → equal-weight via softmax(0)
                er_comp[k]   = {"sharpe": 0.0, "pf": 1.0, "slope": 0.0, "regime_fit": 1.0}
                risk_comp[k] = {"vol": 0.3, "down_vol": 0.3, "dd": 0.0}

        # ── Normalisation cross-sectionnelle (z-score par composante) ────────
        def _z(vals):
            arr = np.array(vals, dtype=float)
            m, s = arr.mean(), arr.std()
            return list((arr - m) / s) if s > 1e-8 else [0.0] * len(vals)

        sharpe_z  = dict(zip(ks, _z([er_comp[k]["sharpe"]     for k in ks])))
        pf_z      = dict(zip(ks, _z([er_comp[k]["pf"]         for k in ks])))
        slope_z   = dict(zip(ks, _z([er_comp[k]["slope"]      for k in ks])))
        regime_z  = dict(zip(ks, _z([er_comp[k]["regime_fit"] for k in ks])))
        vol_z     = dict(zip(ks, _z([risk_comp[k]["vol"]      for k in ks])))
        dvol_z    = dict(zip(ks, _z([risk_comp[k]["down_vol"] for k in ks])))
        dd_z      = dict(zip(ks, _z([risk_comp[k]["dd"]       for k in ks])))

        er_score   = {k: 0.35*sharpe_z[k] + 0.25*pf_z[k]
                        + 0.20*slope_z[k] + 0.20*regime_z[k] for k in ks}
        risk_score = {k: 0.4*vol_z[k] + 0.3*dvol_z[k] + 0.3*dd_z[k] for k in ks}
        net_score  = {k: er_score[k] - risk_score[k] for k in ks}

        # ── Correlation Penalty ──────────────────────────────────────────────
        corr_penalty = {k: 1.0 for k in ks}
        if warmup >= CORR_WIN:
            rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in ks])
            try:
                corr_m = np.corrcoef(rets_mat)
                n = len(ks)
                for ii, k in enumerate(ks):
                    others = [corr_m[ii, jj] for jj in range(n) if jj != ii]
                    avg_c  = float(np.mean(others)) if others else 0.0
                    # Pénalité linéaire pour corrélation > 0.5
                    corr_penalty[k] = max(0.3, 1.0 - max(0.0, avg_c - 0.5) / 0.5)
            except Exception:
                pass

        final_scores = {k: net_score[k] * corr_penalty[k] for k in ks}

        # ── Softmax → poids ──────────────────────────────────────────────────
        max_s   = max(final_scores.values())
        exp_s   = {k: math.exp(SOFTMAX_BETA * (final_scores[k] - max_s)) for k in ks}
        total_e = sum(exp_s.values()) or 1.0
        weights = {k: exp_s[k] / total_e for k in ks}

        r_port = sum(weights[k] * bot_r[k] for k in ks)

        # ── Circuit Breaker ──────────────────────────────────────────────────
        current_pv = eq_omega[-1]
        if current_pv > cb_peak:
            cb_peak = current_pv
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0.0

        if port_dd < CB_THRESHOLD:
            cb_factor = max(CB_MIN_FACTOR, cb_factor - 0.05)
        elif port_dd > -0.05:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)

        eq_omega.append(eq_omega[-1] * (1 + cb_factor * r_port))
        dates_out.append(dt)

    m   = _metrics_portfolio(eq_omega, dates_out, initial_total, weekly=True)
    ann = annual_returns(eq_omega, dates_out)
    return {
        "name":    "Bot Z Omega (ER+Risk+Corr)",
        "equity":  eq_omega, "dates": dates_out, "trades": [],
        "metrics": m, "annual": ann, "regime": {},
    }


# ── 13e. BOT Z OMEGA V2 — Risk Parity + Meta-Learning ─────────────────────────

def backtest_bot_z_omega_v2(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                             daily_cache: dict) -> dict:
    """
    Bot Z Omega v2 — Toutes les couches Omega + 2 couches supplémentaires :

      6. Risk Parity (Equal Risk Contribution) :
         Ajuste les poids pour que chaque bot contribue également au risque total.
         Approximation inverse-vol : w_rp_i ∝ 1/vol_20d_i
         Poids final = blend 50% Omega + 50% Risk Parity

      7. Meta-Learning (strategy decay detection) :
         Détecte quand un bot sous-performe par rapport à ses attentes historiques.
         edge_score = return_30d_réel - return_30d_attendu (basé sur Sharpe long terme)
         confidence = clip(1 + edge_score / 0.05, 0.4, 1.5)
         → réduit l'allocation aux bots en perte d'edge avant que le DD explose
    """
    log("Bot Z Omega v2 — Risk Parity + Meta-Learning...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    # Inclure Bot J s'il est disponible (diversification factor)
    if "j" in results and results["j"]["equity"]:
        valid["j"] = results["j"]
    if len(valid) < 2:
        return {}

    # Rééchantillonner à fréquence hebdomadaire (élimine le bruit MtM journalier crypto)
    weekly_valid = {}
    for k, r in valid.items():
        wd, we = _resample_weekly(r["dates"], r["equity"])
        weekly_valid[k] = {"dates": wd, "equity": we}

    date_sets = [set(r["dates"]) for r in weekly_valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots        = len(valid)
    initial_total = INITIAL * len(VALID_BOTS_Z)  # toujours 4×1000€ comme base de comparaison

    bot_norm = {}
    for k, r in weekly_valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    # BTC EMA200 (momentum overlay)
    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    SHARPE_WIN    = 90
    VOL_WIN       = 20
    SLOPE_WIN     = 60
    CORR_WIN      = 20
    META_WIN      = 30    # fenêtre meta-learning (30j)
    SOFTMAX_BETA  = 3.0
    CB_THRESHOLD  = -0.25
    CB_MIN_FACTOR = 0.30
    CB_RECOVERY   = 0.005
    RP_BLEND      = 0.5   # 50% Omega + 50% Risk Parity

    ret_history  = {k: [] for k in valid}
    eq_history   = {k: [] for k in valid}
    bot_peaks    = {k: 1.0 for k in valid}

    cb_peak   = initial_total
    cb_factor = 1.0

    eq_v2     = [initial_total]
    dates_out = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # ── Retours + historique ─────────────────────────────────────────────
        bot_r = {}
        for k in valid:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k].get(dt, p)
            bot_r[k] = (c / p - 1) if p > 0 else 0.0
            ret_history[k].append(bot_r[k])
            eq_val = bot_norm[k].get(dt, 1.0)
            eq_history[k].append(eq_val)
            if eq_val > bot_peaks[k]:
                bot_peaks[k] = eq_val

        # ── Régime + Momentum Overlay ────────────────────────────────────────
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)
        try:
            dt_norm    = pd.Timestamp(dt).normalize().tz_localize(None)
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
        except Exception:
            qqq_bearish = False
        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])
        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        raw_regime_w = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])

        # ── ER + Risk scores (identique Omega) ──────────────────────────────
        warmup = min(len(ret_history[k]) for k in valid)
        ks = list(valid.keys())

        er_comp   = {k: {} for k in ks}
        risk_comp = {k: {} for k in ks}

        for k in ks:
            hist = ret_history[k]
            eq_h = eq_history[k]
            if warmup >= SHARPE_WIN:
                r90 = np.array(hist[-SHARPE_WIN:])
                sharpe_90 = (float(r90.mean() / r90.std() * math.sqrt(252))
                             if r90.std() > 1e-8 else 0.0)
                pos_sum = sum(r for r in hist[-SHARPE_WIN:] if r > 0)
                neg_sum = abs(sum(r for r in hist[-SHARPE_WIN:] if r < 0))
                pf_90 = min((pos_sum / neg_sum) if neg_sum > 1e-8 else 3.0, 5.0)
                if len(eq_h) >= SLOPE_WIN:
                    eq_s = np.array(eq_h[-SLOPE_WIN:])
                    x = np.arange(len(eq_s))
                    slope = (float(np.polyfit(x, eq_s / max(eq_s[0], 1e-8), 1)[0]) * 252
                             if eq_s.std() > 1e-8 else 0.0)
                else:
                    slope = 0.0
                total_rw = sum(raw_regime_w.values()) or 1.0
                regime_fit = raw_regime_w.get(k, 0) / (total_rw / max(len(VALID_BOTS_Z), 1))
                er_comp[k] = {"sharpe": sharpe_90, "pf": pf_90,
                              "slope": slope, "regime_fit": regime_fit}
                r20 = np.array(hist[-VOL_WIN:])
                vol_20 = float(r20.std() * math.sqrt(252)) if r20.std() > 1e-8 else 0.01
                down_r = r20[r20 < 0]
                down_vol = (float(down_r.std() * math.sqrt(252))
                            if len(down_r) > 1 and down_r.std() > 1e-8 else vol_20)
                dd_k = abs(eq_history[k][-1] / bot_peaks[k] - 1) if bot_peaks[k] > 0 else 0.0
                risk_comp[k] = {"vol": vol_20, "down_vol": down_vol, "dd": dd_k}
            else:
                er_comp[k]   = {"sharpe": 0.0, "pf": 1.0, "slope": 0.0, "regime_fit": 1.0}
                risk_comp[k] = {"vol": 0.3, "down_vol": 0.3, "dd": 0.0}

        def _z(vals):
            arr = np.array(vals, dtype=float)
            m, s = arr.mean(), arr.std()
            return list((arr - m) / s) if s > 1e-8 else [0.0] * len(vals)

        sharpe_z = dict(zip(ks, _z([er_comp[k]["sharpe"]     for k in ks])))
        pf_z     = dict(zip(ks, _z([er_comp[k]["pf"]         for k in ks])))
        slope_z  = dict(zip(ks, _z([er_comp[k]["slope"]      for k in ks])))
        regime_z = dict(zip(ks, _z([er_comp[k]["regime_fit"] for k in ks])))
        vol_z    = dict(zip(ks, _z([risk_comp[k]["vol"]      for k in ks])))
        dvol_z   = dict(zip(ks, _z([risk_comp[k]["down_vol"] for k in ks])))
        dd_z     = dict(zip(ks, _z([risk_comp[k]["dd"]       for k in ks])))

        er_score   = {k: 0.35*sharpe_z[k] + 0.25*pf_z[k]
                        + 0.20*slope_z[k] + 0.20*regime_z[k] for k in ks}
        risk_score = {k: 0.4*vol_z[k] + 0.3*dvol_z[k] + 0.3*dd_z[k] for k in ks}
        net_score  = {k: er_score[k] - risk_score[k] for k in ks}

        corr_penalty = {k: 1.0 for k in ks}
        if warmup >= CORR_WIN:
            rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in ks])
            try:
                corr_m = np.corrcoef(rets_mat)
                n = len(ks)
                for ii, k in enumerate(ks):
                    others = [corr_m[ii, jj] for jj in range(n) if jj != ii]
                    avg_c  = float(np.mean(others)) if others else 0.0
                    corr_penalty[k] = max(0.3, 1.0 - max(0.0, avg_c - 0.5) / 0.5)
            except Exception:
                pass

        final_scores = {k: net_score[k] * corr_penalty[k] for k in ks}
        max_s   = max(final_scores.values())
        exp_s   = {k: math.exp(SOFTMAX_BETA * (final_scores[k] - max_s)) for k in ks}
        total_e = sum(exp_s.values()) or 1.0
        omega_weights = {k: exp_s[k] / total_e for k in ks}

        # ── Couche 6 : Risk Parity (inverse-vol) ────────────────────────────
        vols_k = {k: max(risk_comp[k]["vol"], 0.01) for k in ks}
        inv_vol = {k: 1.0 / vols_k[k] for k in ks}
        total_iv = sum(inv_vol.values()) or 1.0
        rp_weights = {k: inv_vol[k] / total_iv for k in ks}

        # Blend Omega + Risk Parity
        blended = {k: (1 - RP_BLEND) * omega_weights[k] + RP_BLEND * rp_weights[k] for k in ks}

        # ── Couche 7 : Meta-Learning (strategy decay detection) ──────────────
        meta_confidence = {k: 1.0 for k in ks}
        if warmup >= max(SHARPE_WIN, META_WIN + 1):
            # Sharpe long-terme de chaque bot (sur toute l'historique disponible)
            for k in ks:
                full_hist = np.array(ret_history[k])
                long_sharpe = (float(full_hist.mean() / full_hist.std() * math.sqrt(252))
                               if full_hist.std() > 1e-8 else 0.0)
                # Retour attendu sur 30j selon Sharpe long terme
                expected_daily = long_sharpe / math.sqrt(252)
                expected_30d   = (1 + expected_daily) ** META_WIN - 1

                # Retour réel des 30 derniers jours
                r30 = np.array(ret_history[k][-META_WIN:])
                actual_30d = float(np.prod(1 + r30) - 1)

                edge_score = actual_30d - expected_30d
                meta_confidence[k] = max(0.4, min(1.5, 1.0 + edge_score / 0.05))

        # Appliquer la confidence meta-learning sur les poids blendés
        adjusted = {k: blended[k] * meta_confidence[k] for k in ks}
        total_adj = sum(adjusted.values()) or 1.0
        final_weights = {k: adjusted[k] / total_adj for k in ks}

        r_port = sum(final_weights[k] * bot_r[k] for k in ks)

        # ── Circuit Breaker ──────────────────────────────────────────────────
        current_pv = eq_v2[-1]
        if current_pv > cb_peak:
            cb_peak = current_pv
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0.0
        if port_dd < CB_THRESHOLD:
            cb_factor = max(CB_MIN_FACTOR, cb_factor - 0.05)
        elif port_dd > -0.05:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)

        eq_v2.append(eq_v2[-1] * (1 + cb_factor * r_port))
        dates_out.append(dt)

    m   = _metrics_portfolio(eq_v2, dates_out, initial_total, weekly=True)
    ann = annual_returns(eq_v2, dates_out)
    return {
        "name":    "Bot Z Omega v2 (RP+ML)",
        "equity":  eq_v2, "dates": dates_out, "trades": [],
        "metrics": m, "annual": ann, "regime": {},
    }


# ── 13f. BOT Z META — Méta-sélecteur dynamique des 4 engines ─────────────────

# Seuils de sélection d'engine
META_ENGINE_HYSTERESIS = {
    "ENHANCED": 7,   # bull → on attend 7 jours avant de confirmer
    "OMEGA":    5,
    "OMEGA_V2": 4,
    "PRO":      3,   # bear → on sort vite (protection rapide)
}


def _select_engine_raw(vix: float, btc_bearish: bool, qqq_bearish: bool,
                       port_dd: float, avg_corr: float = 0.0) -> str:
    """Engine brut sans hysteresis — logique de sélection."""
    # PRO : conditions défensives sévères
    if (btc_bearish and qqq_bearish) or vix > 30 or port_dd < -0.15:
        return "PRO"
    # OMEGA_V2 : stress modéré — risk parity requis
    if vix > 24 or port_dd < -0.08 or avg_corr > 0.65:
        return "OMEGA_V2"
    # ENHANCED : bull propre sur tous les critères
    if not btc_bearish and not qqq_bearish and vix < 20 and port_dd > -0.03:
        return "ENHANCED"
    # OMEGA : default — ER/Risk engine standard
    return "OMEGA"


def backtest_bot_z_meta(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                        daily_cache: dict) -> dict:
    """
    Bot Z Meta — Méta-sélecteur dynamique entre 4 engines selon le régime :

      ENHANCED  : bull propre (VIX<20, BTC+QQQ bull, DD>-3%)
                  → poids régime purs v2 (max CAGR)
      OMEGA     : conditions normales
                  → ER Engine + Risk Engine + Corr Penalty + softmax
      OMEGA_V2  : stress modéré (VIX>24, DD>-8%, corr>65%)
                  → Omega + Risk Parity 50% + Meta-Learning
      PRO       : bear/crise (BTC+QQQ all bearish, VIX>30, DD>-15%)
                  → Omega + Vol Targeting + multi-CB

    Hysteresis : 7/5/4/3 jours de confirmation avant switch d'engine.
    """
    log("Bot Z Meta — Méta-sélecteur ENHANCED/OMEGA/OMEGA_V2/PRO...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if "j" in results and results["j"]["equity"]:
        valid["j"] = results["j"]
    if len(valid) < 2:
        return {}

    date_sets = [set(r["dates"]) for r in valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots_base   = len(VALID_BOTS_Z)
    initial_total = INITIAL * n_bots_base

    bot_norm = {}
    for k, r in valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    SHARPE_WIN = 90; VOL_WIN = 20; SLOPE_WIN = 60; CORR_WIN = 20; META_WIN = 30
    SOFTMAX_BETA = 3.0; CB_RECOVERY = 0.005
    TARGET_VOL = 0.20  # pour le mode PRO

    # CB par engine (tiers différents)
    CB_TIERS = {
        "ENHANCED": [(-0.25, 0.30)],
        "OMEGA":    [(-0.25, 0.30)],
        "OMEGA_V2": [(-0.20, 0.50), (-0.30, 0.30)],
        "PRO":      [(-0.10, 0.80), (-0.20, 0.50), (-0.30, 0.30)],
    }

    ks = list(valid.keys())
    ret_history = {k: [] for k in ks}
    eq_history  = {k: [] for k in ks}
    bot_peaks   = {k: 1.0 for k in ks}

    cb_peak   = initial_total
    cb_factor = 1.0
    current_engine   = "OMEGA"
    pending_engine   = "OMEGA"
    days_pending     = 0
    engine_log       = []

    eq_meta   = [initial_total]
    dates_out = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # ── Retours + historique ─────────────────────────────────────────────
        bot_r = {}
        for k in ks:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k].get(dt, p)
            bot_r[k] = (c / p - 1) if p > 0 else 0.0
            ret_history[k].append(bot_r[k])
            eq_val = bot_norm[k].get(dt, 1.0)
            eq_history[k].append(eq_val)
            if eq_val > bot_peaks[k]:
                bot_peaks[k] = eq_val

        # ── Régime + Momentum Overlay ────────────────────────────────────────
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)
        try:
            dt_norm    = pd.Timestamp(dt).normalize().tz_localize(None)
            vix_val    = float(vix_s.asof(dt_norm)) if not vix_s.empty else 15.0
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
        except Exception:
            vix_val = 15.0; qqq_bearish = False

        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])

        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        raw_regime_w = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])

        warmup = min(len(ret_history[k]) for k in ks)

        # ── Corrélation inter-bots ───────────────────────────────────────────
        avg_corr = 0.0
        if warmup >= CORR_WIN:
            rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in ks])
            try:
                corr_m = np.corrcoef(rets_mat)
                n = len(ks)
                off_d = [corr_m[ii, jj] for ii in range(n) for jj in range(ii+1, n)]
                avg_corr = float(np.mean(off_d)) if off_d else 0.0
            except Exception:
                pass

        # ── Drawdown portefeuille ────────────────────────────────────────────
        current_pv = eq_meta[-1]
        if current_pv > cb_peak:
            cb_peak = current_pv
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0.0

        # ── Sélection d'engine avec hysteresis ───────────────────────────────
        raw_engine = _select_engine_raw(vix_val, btc_bearish, qqq_bearish, port_dd, avg_corr)
        if raw_engine != pending_engine:
            pending_engine = raw_engine
            days_pending   = 0
        else:
            days_pending += 1

        if days_pending >= META_ENGINE_HYSTERESIS.get(pending_engine, 5):
            current_engine = pending_engine
        engine_log.append((dt, current_engine))

        # ── Calcul des poids selon l'engine actif ────────────────────────────
        # Composantes communes ER + Risk
        er_comp   = {k: {} for k in ks}
        risk_comp = {k: {} for k in ks}
        for k in ks:
            hist = ret_history[k]
            eq_h = eq_history[k]
            if warmup >= SHARPE_WIN:
                r90 = np.array(hist[-SHARPE_WIN:])
                sharpe_90 = (float(r90.mean() / r90.std() * math.sqrt(252))
                             if r90.std() > 1e-8 else 0.0)
                pos_s = sum(r for r in hist[-SHARPE_WIN:] if r > 0)
                neg_s = abs(sum(r for r in hist[-SHARPE_WIN:] if r < 0))
                pf_90 = min((pos_s / neg_s) if neg_s > 1e-8 else 3.0, 5.0)
                if len(eq_h) >= SLOPE_WIN:
                    eq_s = np.array(eq_h[-SLOPE_WIN:])
                    x = np.arange(len(eq_s))
                    slope = (float(np.polyfit(x, eq_s / max(eq_s[0], 1e-8), 1)[0]) * 252
                             if eq_s.std() > 1e-8 else 0.0)
                else:
                    slope = 0.0
                total_rw = sum(raw_regime_w.values()) or 1.0
                regime_fit = raw_regime_w.get(k, 0) / (total_rw / max(len(VALID_BOTS_Z), 1))
                er_comp[k] = {"sharpe": sharpe_90, "pf": pf_90,
                              "slope": slope, "regime_fit": regime_fit}
                r20 = np.array(hist[-VOL_WIN:])
                vol_20 = float(r20.std() * math.sqrt(252)) if r20.std() > 1e-8 else 0.01
                down_r = r20[r20 < 0]
                down_vol = (float(down_r.std() * math.sqrt(252))
                            if len(down_r) > 1 and down_r.std() > 1e-8 else vol_20)
                dd_k = abs(eq_history[k][-1] / bot_peaks[k] - 1) if bot_peaks[k] > 0 else 0.0
                risk_comp[k] = {"vol": vol_20, "down_vol": down_vol, "dd": dd_k}
            else:
                er_comp[k]   = {"sharpe": 0.0, "pf": 1.0, "slope": 0.0, "regime_fit": 1.0}
                risk_comp[k] = {"vol": 0.3, "down_vol": 0.3, "dd": 0.0}

        def _z(vals):
            arr = np.array(vals, dtype=float)
            m, s = arr.mean(), arr.std()
            return list((arr - m) / s) if s > 1e-8 else [0.0] * len(vals)

        if current_engine == "ENHANCED":
            # Poids régime purs v2 (même logique que backtest_bot_z_enhanced)
            total_w = sum(raw_regime_w.get(k, 0) for k in ks) or 1.0
            weights = {k: raw_regime_w.get(k, 0) / total_w for k in ks}
            corr_factor = 1.0

        else:
            # ER/Risk softmax commun à OMEGA, OMEGA_V2, PRO
            sharpe_z = dict(zip(ks, _z([er_comp[k]["sharpe"]     for k in ks])))
            pf_z     = dict(zip(ks, _z([er_comp[k]["pf"]         for k in ks])))
            slope_z  = dict(zip(ks, _z([er_comp[k]["slope"]      for k in ks])))
            regime_z = dict(zip(ks, _z([er_comp[k]["regime_fit"] for k in ks])))
            vol_z    = dict(zip(ks, _z([risk_comp[k]["vol"]      for k in ks])))
            dvol_z   = dict(zip(ks, _z([risk_comp[k]["down_vol"] for k in ks])))
            dd_z     = dict(zip(ks, _z([risk_comp[k]["dd"]       for k in ks])))

            er_s   = {k: 0.35*sharpe_z[k] + 0.25*pf_z[k]
                        + 0.20*slope_z[k] + 0.20*regime_z[k] for k in ks}
            risk_s = {k: 0.4*vol_z[k] + 0.3*dvol_z[k] + 0.3*dd_z[k] for k in ks}

            # Corr penalty
            corr_penalty = {k: 1.0 for k in ks}
            if warmup >= CORR_WIN:
                rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in ks])
                try:
                    corr_m = np.corrcoef(rets_mat)
                    n = len(ks)
                    for ii, k in enumerate(ks):
                        others = [corr_m[ii, jj] for jj in range(n) if jj != ii]
                        ac = float(np.mean(others)) if others else 0.0
                        corr_penalty[k] = max(0.3, 1.0 - max(0.0, ac - 0.5) / 0.5)
                except Exception:
                    pass

            net_score = {k: (er_s[k] - risk_s[k]) * corr_penalty[k] for k in ks}

            # Vol targeting (PRO uniquement)
            if current_engine == "PRO":
                vol_scale = {}
                for k in ks:
                    if len(ret_history[k]) >= VOL_WIN:
                        v = float(np.std(ret_history[k][-VOL_WIN:]) * math.sqrt(252))
                        vol_scale[k] = min(TARGET_VOL / max(v, 1e-8), 3.0)
                    else:
                        vol_scale[k] = 1.0
                net_score = {k: net_score[k] * vol_scale[k] for k in ks}

            # Softmax
            max_s = max(net_score.values())
            exp_s = {k: math.exp(SOFTMAX_BETA * (net_score[k] - max_s)) for k in ks}
            tot_e = sum(exp_s.values()) or 1.0
            omega_w = {k: exp_s[k] / tot_e for k in ks}

            # Risk Parity blend (OMEGA_V2 50%, PRO 30%)
            rp_blend = 0.5 if current_engine == "OMEGA_V2" else (0.3 if current_engine == "PRO" else 0.0)
            if rp_blend > 0:
                inv_v = {k: 1.0 / max(risk_comp[k]["vol"], 0.01) for k in ks}
                tot_iv = sum(inv_v.values()) or 1.0
                rp_w = {k: inv_v[k] / tot_iv for k in ks}
                blended = {k: (1-rp_blend) * omega_w[k] + rp_blend * rp_w[k] for k in ks}
            else:
                blended = omega_w

            # Meta-Learning (OMEGA_V2 + PRO)
            if current_engine in ("OMEGA_V2", "PRO") and warmup >= max(SHARPE_WIN, META_WIN+1):
                confidence = {}
                for k in ks:
                    fh = np.array(ret_history[k])
                    ls = (float(fh.mean() / fh.std() * math.sqrt(252))
                          if fh.std() > 1e-8 else 0.0)
                    exp_daily = ls / math.sqrt(252)
                    exp_30d   = (1 + exp_daily) ** META_WIN - 1
                    r30 = np.array(ret_history[k][-META_WIN:])
                    act_30d = float(np.prod(1 + r30) - 1)
                    confidence[k] = max(0.4, min(1.5, 1.0 + (act_30d - exp_30d) / 0.05))
                adj = {k: blended[k] * confidence[k] for k in ks}
                tot_adj = sum(adj.values()) or 1.0
                weights = {k: adj[k] / tot_adj for k in ks}
            else:
                weights = blended

            corr_factor = 1.0  # corr déjà géré dans corr_penalty

        r_port = sum(weights[k] * bot_r[k] for k in ks)

        # ── Circuit Breaker selon l'engine actif ─────────────────────────────
        tiers = CB_TIERS.get(current_engine, [(-0.25, 0.30)])
        target_factor = 1.0
        for threshold, floor in sorted(tiers, key=lambda x: x[0]):
            if port_dd < threshold:
                target_factor = floor

        if target_factor < cb_factor:
            cb_factor = max(target_factor, cb_factor - 0.05)
        elif port_dd > -0.05:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)

        eq_meta.append(eq_meta[-1] * (1 + cb_factor * r_port))
        dates_out.append(dt)

    # Stats engines
    engine_counts = {}
    for _, e in engine_log:
        engine_counts[e] = engine_counts.get(e, 0) + 1
    total_days = len(engine_log) or 1
    engine_pct = {e: round(engine_counts.get(e, 0) / total_days * 100, 1)
                  for e in ["ENHANCED", "OMEGA", "OMEGA_V2", "PRO"]}

    m   = _metrics_portfolio(eq_meta, dates_out, initial_total, weekly=True)
    ann = annual_returns(eq_meta, dates_out)
    return {
        "name":         "Bot Z Meta (E/Ω/Ω2/P)",
        "equity":       eq_meta, "dates": dates_out, "trades": [],
        "metrics":      m, "annual": ann, "regime": {},
        "engine_stats": engine_pct,
    }


# ── 13g. BOT Z META V2 — Engine Scoring + Calibration affinée ─────────────────

# Régime-fit par engine : dans quel régime chaque engine performe le mieux
ENGINE_REGIME_FIT = {
    "ENHANCED": {"BULL": 1.0, "RANGE": 0.6, "HIGH_VOL": 0.3, "BEAR": 0.1},
    "OMEGA":    {"BULL": 0.8, "RANGE": 0.8, "HIGH_VOL": 0.7, "BEAR": 0.5},
    "OMEGA_V2": {"BULL": 0.5, "RANGE": 0.7, "HIGH_VOL": 0.9, "BEAR": 0.8},
    "PRO":      {"BULL": 0.3, "RANGE": 0.5, "HIGH_VOL": 0.8, "BEAR": 1.0},
}


def backtest_bot_z_meta_v2(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                            daily_cache: dict) -> dict:
    """
    Bot Z Meta v2 — Sélection d'engine data-driven (vs règles statiques en v1) :

      engine_score = 0.40 × regime_fit
                   + 0.30 × perf_rolling_60d   (shadow equity tracking)
                   + 0.20 × inverse_risk         (1/vol récente de l'engine)
                   + 0.10 × diversification      (corr engine vs portefeuille)

      Hard rules (non-négociables) :
        PRO forcé si (BTC+QQQ both bearish ET VIX>26) OU DD<-12%
        ENHANCED bloqué si BTC ou QQQ bearish

      Seuils recalibrés vs v1 :
        OMEGA_V2 : VIX>26 (était 24) + DD>-10% (était -8%)
        PRO      : VIX>32 (était 30) + DD>-12% (était -15%)
    """
    log("Bot Z Meta v2 — Engine Scoring data-driven...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if "j" in results and results["j"]["equity"]:
        valid["j"] = results["j"]
    if len(valid) < 2:
        return {}

    # Rééchantillonner à fréquence hebdomadaire (élimine le bruit MtM journalier crypto)
    weekly_valid = {}
    for k, r in valid.items():
        wd, we = _resample_weekly(r["dates"], r["equity"])
        weekly_valid[k] = {"dates": wd, "equity": we}

    date_sets = [set(r["dates"]) for r in weekly_valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    initial_total = INITIAL * len(VALID_BOTS_Z)

    bot_norm = {}
    for k, r in weekly_valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    btc_df = daily_cache.get("BTC/EUR")
    btc_norm = {}
    if btc_df is not None and "ema200" in btc_df.columns:
        for dt, row in btc_df.iterrows():
            btc_norm[dt] = {"close": row["close"], "ema200": row["ema200"]}

    ENGINE_NAMES = ["ENHANCED", "OMEGA", "OMEGA_V2", "PRO"]
    SHARPE_WIN = 90; VOL_WIN = 20; SLOPE_WIN = 60; CORR_WIN = 20; META_WIN = 30
    PERF_WIN = 60    # fenêtre rolling performance par engine
    SOFTMAX_BETA = 3.0; CB_RECOVERY = 0.005; TARGET_VOL = 0.20
    CB_TIERS = {
        "ENHANCED": [(-0.25, 0.30)],
        "OMEGA":    [(-0.25, 0.30)],
        "OMEGA_V2": [(-0.20, 0.50), (-0.30, 0.30)],
        "PRO":      [(-0.10, 0.80), (-0.20, 0.50), (-0.30, 0.30)],
    }
    HYSTERESIS = {"ENHANCED": 7, "OMEGA": 5, "OMEGA_V2": 4, "PRO": 3}

    ks = list(valid.keys())
    ret_history  = {k: [] for k in ks}
    eq_history   = {k: [] for k in ks}
    bot_peaks    = {k: 1.0 for k in ks}

    # Shadow equity tracking : chaque engine tourne en parallèle (sans CB)
    shadow_eq = {e: [initial_total] for e in ENGINE_NAMES}
    shadow_ret_hist = {e: [] for e in ENGINE_NAMES}

    cb_peak   = initial_total
    cb_factor = 1.0
    current_engine = "OMEGA"
    pending_engine = "OMEGA"
    days_pending   = 0
    engine_log     = []

    eq_meta2  = [initial_total]
    dates_out = [common_dates[0]]

    for i in range(1, len(common_dates)):
        dt      = common_dates[i]
        prev_dt = common_dates[i - 1]

        # ── Retours + historique ─────────────────────────────────────────────
        bot_r = {}
        for k in ks:
            p = bot_norm[k].get(prev_dt, 1.0)
            c = bot_norm[k].get(dt, p)
            bot_r[k] = (c / p - 1) if p > 0 else 0.0
            ret_history[k].append(bot_r[k])
            ev = bot_norm[k].get(dt, 1.0)
            eq_history[k].append(ev)
            if ev > bot_peaks[k]:
                bot_peaks[k] = ev

        # ── Régime + MO ──────────────────────────────────────────────────────
        regime = _get_regime_at_dt(dt, vix_s, qqq_df)
        try:
            dt_norm    = pd.Timestamp(dt).normalize().tz_localize(None)
            vix_val    = float(vix_s.asof(dt_norm)) if not vix_s.empty else 15.0
            qqq_close  = float(qqq_df["Close"].asof(dt_norm))
            qqq_sma200 = float(qqq_df["sma200"].asof(dt_norm))
            qqq_bearish = qqq_close < qqq_sma200
        except Exception:
            vix_val = 15.0; qqq_bearish = False

        btc_row = btc_norm.get(dt) or btc_norm.get(prev_dt)
        btc_bearish = (btc_row is not None and btc_row["ema200"] > 0
                       and btc_row["close"] < btc_row["ema200"])

        if btc_bearish and qqq_bearish:
            regime = "BEAR"
        elif btc_bearish or qqq_bearish:
            regime = "HIGH_VOL" if regime in ("BULL", "RANGE") else regime

        raw_regime_w = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])

        warmup = min(len(ret_history[k]) for k in ks)

        # ── Corrélation ──────────────────────────────────────────────────────
        avg_corr = 0.0
        if warmup >= CORR_WIN:
            rets_mat = np.array([ret_history[k][-CORR_WIN:] for k in ks])
            try:
                corr_m = np.corrcoef(rets_mat)
                n = len(ks)
                off_d = [corr_m[ii, jj] for ii in range(n) for jj in range(ii+1, n)]
                avg_corr = float(np.mean(off_d)) if off_d else 0.0
            except Exception:
                pass

        # ── Drawdown ─────────────────────────────────────────────────────────
        current_pv = eq_meta2[-1]
        if current_pv > cb_peak:
            cb_peak = current_pv
        port_dd = (current_pv - cb_peak) / cb_peak if cb_peak > 0 else 0.0

        # ── ER/Risk scores (communs) ─────────────────────────────────────────
        er_comp   = {k: {} for k in ks}
        risk_comp = {k: {} for k in ks}
        for k in ks:
            hist = ret_history[k]
            eq_h = eq_history[k]
            if warmup >= SHARPE_WIN:
                r90 = np.array(hist[-SHARPE_WIN:])
                s90 = (float(r90.mean() / r90.std() * math.sqrt(252))
                       if r90.std() > 1e-8 else 0.0)
                ps = sum(r for r in hist[-SHARPE_WIN:] if r > 0)
                ns = abs(sum(r for r in hist[-SHARPE_WIN:] if r < 0))
                pf = min((ps / ns) if ns > 1e-8 else 3.0, 5.0)
                slope = 0.0
                if len(eq_h) >= SLOPE_WIN:
                    eq_s = np.array(eq_h[-SLOPE_WIN:])
                    x = np.arange(len(eq_s))
                    if eq_s.std() > 1e-8:
                        slope = float(np.polyfit(x, eq_s / max(eq_s[0], 1e-8), 1)[0]) * 252
                total_rw = sum(raw_regime_w.values()) or 1.0
                rf = raw_regime_w.get(k, 0) / (total_rw / max(len(VALID_BOTS_Z), 1))
                er_comp[k] = {"sharpe": s90, "pf": pf, "slope": slope, "regime_fit": rf}
                r20 = np.array(hist[-VOL_WIN:])
                v20 = float(r20.std() * math.sqrt(252)) if r20.std() > 1e-8 else 0.01
                dr = r20[r20 < 0]
                dv = (float(dr.std() * math.sqrt(252)) if len(dr) > 1 and dr.std() > 1e-8 else v20)
                ddv = abs(eq_history[k][-1] / bot_peaks[k] - 1) if bot_peaks[k] > 0 else 0.0
                risk_comp[k] = {"vol": v20, "down_vol": dv, "dd": ddv}
            else:
                er_comp[k]   = {"sharpe": 0.0, "pf": 1.0, "slope": 0.0, "regime_fit": 1.0}
                risk_comp[k] = {"vol": 0.3, "down_vol": 0.3, "dd": 0.0}

        def _z(vals):
            arr = np.array(vals, dtype=float)
            m, s = arr.mean(), arr.std()
            return list((arr - m) / s) if s > 1e-8 else [0.0] * len(vals)

        # ── Poids par engine ─────────────────────────────────────────────────
        def _omega_weights(net_score):
            max_s = max(net_score.values())
            exp_s = {k: math.exp(SOFTMAX_BETA * (net_score[k] - max_s)) for k in ks}
            tot   = sum(exp_s.values()) or 1.0
            return {k: exp_s[k] / tot for k in ks}

        def _er_risk_net():
            sharpe_z = dict(zip(ks, _z([er_comp[k]["sharpe"]     for k in ks])))
            pf_z     = dict(zip(ks, _z([er_comp[k]["pf"]         for k in ks])))
            slope_z  = dict(zip(ks, _z([er_comp[k]["slope"]      for k in ks])))
            regime_z = dict(zip(ks, _z([er_comp[k]["regime_fit"] for k in ks])))
            vol_z    = dict(zip(ks, _z([risk_comp[k]["vol"]      for k in ks])))
            dvol_z   = dict(zip(ks, _z([risk_comp[k]["down_vol"] for k in ks])))
            dd_z     = dict(zip(ks, _z([risk_comp[k]["dd"]       for k in ks])))
            corr_pen = {k: 1.0 for k in ks}
            if warmup >= CORR_WIN:
                rets_m = np.array([ret_history[k][-CORR_WIN:] for k in ks])
                try:
                    cm = np.corrcoef(rets_m); n = len(ks)
                    for ii, k in enumerate(ks):
                        oth = [cm[ii, jj] for jj in range(n) if jj != ii]
                        ac  = float(np.mean(oth)) if oth else 0.0
                        corr_pen[k] = max(0.3, 1.0 - max(0.0, ac - 0.5) / 0.5)
                except Exception:
                    pass
            er_s   = {k: 0.35*sharpe_z[k] + 0.25*pf_z[k] + 0.20*slope_z[k] + 0.20*regime_z[k] for k in ks}
            risk_s = {k: 0.4*vol_z[k] + 0.3*dvol_z[k] + 0.3*dd_z[k] for k in ks}
            return {k: (er_s[k] - risk_s[k]) * corr_pen[k] for k in ks}

        def _rp_weights(blend=0.5, omega_w=None):
            inv_v = {k: 1.0 / max(risk_comp[k]["vol"], 0.01) for k in ks}
            tot   = sum(inv_v.values()) or 1.0
            rp_w  = {k: inv_v[k] / tot for k in ks}
            if omega_w:
                return {k: (1-blend) * omega_w[k] + blend * rp_w[k] for k in ks}
            return rp_w

        def _meta_learning_confidence():
            if warmup < max(SHARPE_WIN, META_WIN + 1):
                return {k: 1.0 for k in ks}
            conf = {}
            for k in ks:
                fh = np.array(ret_history[k])
                ls = (float(fh.mean() / fh.std() * math.sqrt(252)) if fh.std() > 1e-8 else 0.0)
                exp_30d = (1 + ls / math.sqrt(252)) ** META_WIN - 1
                act_30d = float(np.prod(1 + np.array(ret_history[k][-META_WIN:])) - 1)
                conf[k] = max(0.4, min(1.5, 1.0 + (act_30d - exp_30d) / 0.05))
            return conf

        def _engine_weights(engine):
            if engine == "ENHANCED":
                total_w = sum(raw_regime_w.get(k, 0) for k in ks) or 1.0
                return {k: raw_regime_w.get(k, 0) / total_w for k in ks}
            net = _er_risk_net()
            if engine == "OMEGA":
                return _omega_weights(net)
            omega_w = _omega_weights(net)
            if engine == "OMEGA_V2":
                blended = _rp_weights(0.5, omega_w)
                conf = _meta_learning_confidence()
                adj  = {k: blended[k] * conf[k] for k in ks}
                tot  = sum(adj.values()) or 1.0
                return {k: adj[k] / tot for k in ks}
            # PRO : vol targeting + RP 30% + meta
            vs = {}
            for k in ks:
                if len(ret_history[k]) >= VOL_WIN:
                    v = float(np.std(ret_history[k][-VOL_WIN:]) * math.sqrt(252))
                    vs[k] = min(TARGET_VOL / max(v, 1e-8), 3.0)
                else:
                    vs[k] = 1.0
            net_pro  = {k: net[k] * vs[k] for k in ks}
            omega_pro = _omega_weights(net_pro)
            blended  = _rp_weights(0.3, omega_pro)
            conf     = _meta_learning_confidence()
            adj      = {k: blended[k] * conf[k] for k in ks}
            tot      = sum(adj.values()) or 1.0
            return {k: adj[k] / tot for k in ks}

        # ── Shadow tracking (tous les engines en parallèle) ──────────────────
        for eng in ENGINE_NAMES:
            w_eng  = _engine_weights(eng)
            r_eng  = sum(w_eng[k] * bot_r[k] for k in ks)
            shadow_eq[eng].append(shadow_eq[eng][-1] * (1 + r_eng))
            shadow_ret_hist[eng].append(r_eng)

        # ── Engine Scoring data-driven ───────────────────────────────────────
        # Hard rules (priorité absolue)
        force_pro = ((btc_bearish and qqq_bearish and vix_val > 26)
                     or vix_val > 32 or port_dd < -0.12)
        block_enhanced = btc_bearish or qqq_bearish

        eng_scores = {}
        for eng in ENGINE_NAMES:
            # 1. Regime fit (table ENGINE_REGIME_FIT)
            rf = ENGINE_REGIME_FIT[eng].get(regime, 0.5)

            # 2. Performance rolling 60j du shadow engine (normalisée)
            if len(shadow_eq[eng]) > PERF_WIN:
                perf_60d = shadow_eq[eng][-1] / shadow_eq[eng][-PERF_WIN-1] - 1
            else:
                perf_60d = 0.0

            # 3. Inverse risk (vol récente du shadow engine)
            if len(shadow_ret_hist[eng]) >= VOL_WIN:
                eng_vol = float(np.std(shadow_ret_hist[eng][-VOL_WIN:]) * math.sqrt(252))
                inv_risk = 1.0 / max(eng_vol, 0.01)
            else:
                inv_risk = 1.0

            # 4. Diversification (diversif du shadow engine vs portefeuille)
            div_score = 0.5  # neutre par défaut
            if len(shadow_ret_hist[eng]) >= CORR_WIN and len(shadow_ret_hist["OMEGA"]) >= CORR_WIN:
                try:
                    r_eng_arr = np.array(shadow_ret_hist[eng][-CORR_WIN:])
                    r_ref_arr = np.array(shadow_ret_hist["OMEGA"][-CORR_WIN:])
                    c = float(np.corrcoef(r_eng_arr, r_ref_arr)[0, 1])
                    div_score = max(0.0, 1.0 - abs(c))
                except Exception:
                    pass

            eng_scores[eng] = 0.40 * rf + 0.30 * perf_60d + 0.20 * inv_risk + 0.10 * div_score

        # Normalise inv_risk across engines for comparability
        inv_risks = {e: (1.0 / max(float(np.std(shadow_ret_hist[e][-VOL_WIN:]) * math.sqrt(252)), 0.01)
                         if len(shadow_ret_hist[e]) >= VOL_WIN else 1.0) for e in ENGINE_NAMES}
        max_ir = max(inv_risks.values()) or 1.0
        for eng in ENGINE_NAMES:
            # Recalculate with normalized inv_risk
            rf = ENGINE_REGIME_FIT[eng].get(regime, 0.5)
            perf_60d = (shadow_eq[eng][-1] / shadow_eq[eng][-PERF_WIN-1] - 1
                        if len(shadow_eq[eng]) > PERF_WIN else 0.0)
            inv_risk_norm = inv_risks[eng] / max_ir
            div_score = 0.5
            if len(shadow_ret_hist[eng]) >= CORR_WIN and len(shadow_ret_hist["OMEGA"]) >= CORR_WIN:
                try:
                    c = float(np.corrcoef(shadow_ret_hist[eng][-CORR_WIN:],
                                          shadow_ret_hist["OMEGA"][-CORR_WIN:])[0, 1])
                    div_score = max(0.0, 1.0 - abs(c))
                except Exception:
                    pass
            eng_scores[eng] = 0.40 * rf + 0.30 * perf_60d + 0.20 * inv_risk_norm + 0.10 * div_score

        # Appliquer hard rules + choisir l'engine avec le meilleur score
        if force_pro:
            raw_engine = "PRO"
        elif block_enhanced:
            # Choisir parmi OMEGA, OMEGA_V2, PRO selon les scores
            scores_no_enh = {e: eng_scores[e] for e in ["OMEGA", "OMEGA_V2", "PRO"]}
            raw_engine = max(scores_no_enh, key=scores_no_enh.get)
        else:
            raw_engine = max(eng_scores, key=eng_scores.get)

        # Hysteresis
        if raw_engine != pending_engine:
            pending_engine = raw_engine
            days_pending   = 0
        else:
            days_pending += 1

        if days_pending >= HYSTERESIS.get(pending_engine, 5):
            current_engine = pending_engine
        engine_log.append((dt, current_engine))

        # ── Appliquer l'engine sélectionné ───────────────────────────────────
        weights = _engine_weights(current_engine)
        r_port  = sum(weights[k] * bot_r[k] for k in ks)

        # ── Circuit Breaker ──────────────────────────────────────────────────
        tiers = CB_TIERS.get(current_engine, [(-0.25, 0.30)])
        target_factor = 1.0
        for threshold, floor in sorted(tiers, key=lambda x: x[0]):
            if port_dd < threshold:
                target_factor = floor
        if target_factor < cb_factor:
            cb_factor = max(target_factor, cb_factor - 0.05)
        elif port_dd > -0.05:
            cb_factor = min(1.0, cb_factor + CB_RECOVERY)

        eq_meta2.append(eq_meta2[-1] * (1 + cb_factor * r_port))
        dates_out.append(dt)

    engine_counts = {}
    for _, e in engine_log:
        engine_counts[e] = engine_counts.get(e, 0) + 1
    total_days = len(engine_log) or 1
    engine_pct = {e: round(engine_counts.get(e, 0) / total_days * 100, 1)
                  for e in ENGINE_NAMES}

    m   = _metrics_portfolio(eq_meta2, dates_out, initial_total, weekly=True)
    ann = annual_returns(eq_meta2, dates_out)
    return {
        "name":         "Bot Z Meta v2 (scored)",
        "equity":       eq_meta2, "dates": dates_out, "trades": [],
        "metrics":      m, "annual": ann, "regime": {},
        "engine_stats": engine_pct,
    }


# ── 14. WALK-FORWARD TEST ─────────────────────────────────────────────────────

def walk_forward_test(results: dict, vix_s: pd.Series, qqq_df: pd.DataFrame,
                      daily_cache: dict, split_year: int = 2023) -> dict:
    """
    Walk-forward : évalue si l'edge observé en in-sample se maintient en out-of-sample.
      In-sample  : jusqu'au 31/12/(split_year-1)
      Out-of-sample : à partir du 01/01/split_year

    Pour chaque structure (equal-weight, Bot Z, Enhanced) :
      - Calcule les métriques sur chaque période séparément
      - Un bon système doit maintenir un CAGR positif en OOS
    """
    log(f"Walk-Forward Test — In-Sample <{split_year} / Out-of-Sample >={split_year}...")

    valid = {k: results[k] for k in VALID_BOTS_Z if k in results and results[k]["equity"]}
    if len(valid) < 2:
        return {}

    date_sets = [set(r["dates"]) for r in valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    split_dt = pd.Timestamp(f"{split_year}-01-01").tz_localize(None)
    is_dates  = [d for d in common_dates if pd.Timestamp(d).tz_localize(None) < split_dt]
    oos_dates = [d for d in common_dates if pd.Timestamp(d).tz_localize(None) >= split_dt]

    if not is_dates or not oos_dates:
        log("Walk-forward : pas assez de données pour les deux périodes.", Fore.YELLOW)
        return {}

    n_bots = len(valid)
    initial_total = INITIAL * n_bots

    bot_norm = {}
    for k, r in valid.items():
        bot_norm[k] = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}

    def _simulate(date_list):
        """Simule equal-weight + Bot Z pur sur une liste de dates."""
        if len(date_list) < 2:
            return None, None
        eq_eq = [initial_total]
        eq_z  = [initial_total]
        for i in range(1, len(date_list)):
            dt, prev_dt = date_list[i], date_list[i-1]
            bot_r = {}
            for k in valid:
                p = bot_norm[k].get(prev_dt, 1.0)
                c = bot_norm[k].get(dt, bot_norm[k].get(prev_dt, 1.0))
                bot_r[k] = (c / p - 1) if p > 0 else 0.0
            r_eq = sum(bot_r[k] / n_bots for k in valid)
            eq_eq.append(eq_eq[-1] * (1 + r_eq))
            regime  = _get_regime_at_dt(dt, vix_s, qqq_df)
            raw_w   = REGIME_WEIGHTS_Z.get(regime, REGIME_WEIGHTS_Z["RANGE"])
            total_w = sum(raw_w.get(k, 0) for k in valid) or 1.0
            w_z     = {k: raw_w.get(k, 0) / total_w for k in valid}
            r_z     = sum(w_z[k] * bot_r[k] for k in valid)
            eq_z.append(eq_z[-1] * (1 + r_z))
        return eq_eq, eq_z

    eq_eq_is,  eq_z_is  = _simulate(is_dates)
    eq_eq_oos, eq_z_oos = _simulate(oos_dates)

    def _m(eq, dates):
        if eq and dates:
            return _metrics_portfolio(eq, dates, eq[0])
        return {}

    return {
        "split_year": split_year,
        "is_period":  f"2020 → {split_year-1}",
        "oos_period": f"{split_year} → 2026",
        "is_dates":   is_dates,  "oos_dates":  oos_dates,
        "equal": {
            "is":  _m(eq_eq_is,  is_dates),
            "oos": _m(eq_eq_oos, oos_dates),
        },
        "z": {
            "is":  _m(eq_z_is,  is_dates),
            "oos": _m(eq_z_oos, oos_dates),
        },
    }


# ── 14. MONTE CARLO ───────────────────────────────────────────────────────────

def monte_carlo_test(results: dict, n_simulations: int = 1000) -> dict:
    """
    Monte Carlo : simule N fois les bots en randomisant l'ordre des trades.
    Si le système est profitable même avec ordre aléatoire →
      l'edge n'est pas dépendant de la séquence (edge réel, pas de chance).

    Pour chaque bot valide :
      - Récupère les trade PnL
      - Shuffle N fois
      - Calcule CAGR et MaxDD pour chaque simulation
      - Retourne p5/p50/p95 percentiles
    """
    log(f"Monte Carlo — {n_simulations} simulations par bot...")

    mc_results = {}
    for bot_id in VALID_BOTS_Z:
        r = results.get(bot_id)
        if not r or not r["trades"]:
            continue
        trades = r["trades"]
        if len(trades) < 10:
            continue

        capital_start = INITIAL
        pnls = [t["pnl"] for t in trades]
        n_trades = len(pnls)

        sim_cagrs = []
        sim_maxdds = []

        for _ in range(n_simulations):
            shuffled = list(pnls)
            import random
            random.shuffle(shuffled)

            # Simule l'equity curve avec les PnL dans cet ordre
            cap = capital_start
            equity_sim = [cap]
            for pnl in shuffled:
                cap += pnl
                cap = max(cap, 1.0)  # floor à 1€
                equity_sim.append(cap)

            # Métriques
            eq = np.array(equity_sim)
            n_years = n_trades / 50   # ~50 trades/an approximation
            n_years = max(n_years, 0.1)
            cagr = ((eq[-1] / capital_start) ** (1 / n_years) - 1) * 100

            peak = np.maximum.accumulate(eq)
            max_dd = float(((eq - peak) / (peak + 1e-10) * 100).min())

            sim_cagrs.append(cagr)
            sim_maxdds.append(max_dd)

        sim_cagrs  = sorted(sim_cagrs)
        sim_maxdds = sorted(sim_maxdds)
        n = len(sim_cagrs)

        mc_results[bot_id] = {
            "bot_name":     r["name"],
            "n_simulations": n_simulations,
            "n_trades":     n_trades,
            "real_cagr":  r["metrics"]["cagr"],
            "real_maxdd": r["metrics"]["max_dd"],
            "cagr_p5":    round(sim_cagrs[int(n * 0.05)], 1),
            "cagr_p50":   round(sim_cagrs[int(n * 0.50)], 1),
            "cagr_p95":   round(sim_cagrs[int(n * 0.95)], 1),
            "dd_p5":      round(sim_maxdds[int(n * 0.05)], 1),   # worst case
            "dd_p95":     round(sim_maxdds[int(n * 0.95)], 1),   # best case
            "pct_positive": round(sum(1 for c in sim_cagrs if c > 0) / n * 100, 1),
        }

    return mc_results


# ── 15. RAPPORT ───────────────────────────────────────────────────────────────

def print_walk_forward(wf: dict):
    """Affiche les résultats du walk-forward test."""
    if not wf:
        return
    print(f"\n{Fore.CYAN}{'='*100}")
    print(f"  WALK-FORWARD TEST — In-Sample {wf['is_period']} / Out-of-Sample {wf['oos_period']}")
    print(f"  (Un bon système doit rester profitable en OOS — sinon l'edge est sur-ajusté)")
    print(f"{'='*100}{Style.RESET_ALL}")
    print(f"  {'Stratégie':<28} {'IS CAGR':>9} {'IS Sharpe':>10} {'IS MaxDD':>9}  |  {'OOS CAGR':>9} {'OOS Sharpe':>11} {'OOS MaxDD':>9}  {'Verdict'}")
    print("  " + "-" * 100)

    for key, label in [("equal", "Equal-Weight (A+B+C+G)"), ("z", "Bot Z — Régime pur")]:
        d = wf.get(key, {})
        is_m  = d.get("is",  {})
        oos_m = d.get("oos", {})
        if not is_m or not oos_m:
            continue

        oos_ok = oos_m.get("cagr", 0) > 0
        verdict_color = Fore.GREEN if oos_ok else Fore.RED
        verdict = "EDGE RÉEL" if oos_ok else "OVER-FIT?"

        dd_c_is  = Fore.RED if is_m.get("max_dd", 0) < -30 else Fore.YELLOW
        dd_c_oos = Fore.RED if oos_m.get("max_dd", 0) < -30 else Fore.YELLOW
        cagr_c_oos = Fore.GREEN if oos_ok else Fore.RED

        print(f"  {label:<28} "
              f"{Fore.GREEN}{is_m.get('cagr', 0):>+8.1f}%{Style.RESET_ALL}  "
              f"{is_m.get('sharpe', 0):>9.2f}  "
              f"{dd_c_is}{is_m.get('max_dd', 0):>8.1f}%{Style.RESET_ALL}  |  "
              f"{cagr_c_oos}{oos_m.get('cagr', 0):>+8.1f}%{Style.RESET_ALL}  "
              f"{oos_m.get('sharpe', 0):>10.2f}  "
              f"{dd_c_oos}{oos_m.get('max_dd', 0):>8.1f}%{Style.RESET_ALL}  "
              f"{verdict_color}{verdict}{Style.RESET_ALL}")

    print(f"{Fore.CYAN}{'='*100}{Style.RESET_ALL}")


def print_monte_carlo(mc: dict):
    """Affiche les résultats du Monte Carlo."""
    if not mc:
        return
    print(f"\n{Fore.CYAN}{'='*100}")
    n_sims = next(iter(mc.values())).get("n_simulations", 5000) if mc else 5000
    print(f"  MONTE CARLO — Robustesse statistique ({n_sims} simulations / ordre des trades randomisé)")
    print(f"  (Si p5 CAGR > 0 → l'edge est réel et indépendant de la séquence)")
    print(f"{'='*100}{Style.RESET_ALL}")
    print(f"  {'Bot':<28} {'Trades':>7} {'CAGR réel':>10} {'p5 CAGR':>9} {'p50 CAGR':>9} {'p95 CAGR':>9} {'%Positif':>9} {'DD p5':>8}")
    print("  " + "-" * 100)

    for bot_id in VALID_BOTS_Z:
        mc_r = mc.get(bot_id)
        if not mc_r:
            continue
        edge_ok = mc_r["cagr_p5"] > 0
        color   = Fore.GREEN if edge_ok else Fore.YELLOW
        verdict = "✓ EDGE" if edge_ok else "~ MIXTE"
        print(f"  {mc_r['bot_name']:<28} "
              f"{mc_r['n_trades']:>7}  "
              f"{Fore.CYAN}{mc_r['real_cagr']:>+9.1f}%{Style.RESET_ALL}  "
              f"{color}{mc_r['cagr_p5']:>+8.1f}%{Style.RESET_ALL}  "
              f"{mc_r['cagr_p50']:>+8.1f}%  "
              f"{mc_r['cagr_p95']:>+8.1f}%  "
              f"{mc_r['pct_positive']:>8.0f}%  "
              f"{mc_r['dd_p5']:>7.1f}%  {color}{verdict}{Style.RESET_ALL}")

    print(f"{Fore.CYAN}{'='*100}{Style.RESET_ALL}")


def print_report(results, vix_s, qqq_df, z_results=None, wf=None, mc=None):
    bots = list(results.values())
    # Déterminer la période réelle
    all_dates = [d for r in bots for d in r.get("dates", [])]
    if all_dates:
        d0 = min(all_dates).strftime("%Y-%m")
        d1 = max(all_dates).strftime("%Y-%m")
        period_str = f"{d0} → {d1}"
    else:
        period_str = "?"

    print(f"\n{Fore.CYAN}{'='*100}")
    print(f"  BACKTEST — COMPARAISON MULTI-BOTS  [{period_str}]")
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

    # Bot Z comparison
    if z_results:
        print(f"\n{Fore.CYAN}{'='*100}")
        print(f"  BOT Z — 6 STRUCTURES PORTFOLIO (4 bots valides A+B+C+G | capital 4×1000€)")
        print(f"{'='*100}{Style.RESET_ALL}")
        print(f"  {'Stratégie':<32} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>8} {'Final€':>9}  Notes")
        print("  " + "-" * 90)

        # Best individual bot (scaled ×4 for comparison)
        valid_bots = [r for r in results.values()
                      if r["metrics"]["cagr"] > 0 and r["name"].split(" — ")[0].replace("Bot ", "").lower()
                      in VALID_BOTS_Z]
        best = max(valid_bots, key=lambda r: r["metrics"]["cagr"], default=None)
        if best:
            m = best["metrics"]
            name_short = best["name"].split(" — ")[0]
            print(f"  {'REF: '+name_short+' seul (×4)':<32} {Fore.YELLOW}{m['cagr']:>+6.1f}%{Style.RESET_ALL}  "
                  f"{m['sharpe']:>6.2f}  {m['max_dd']:>7.1f}%  {m['final']*4:>8.0f}€  ← référence (1 bot ×4)")

        strategy_order = [
            ("equal",    Fore.CYAN,    "← diversification pure"),
            ("z",        Fore.GREEN,   "← régime dynamique 100%"),
            ("hybrid",   Fore.WHITE,   "← 70% base stable + 30% overlay dynamique"),
            ("enhanced", Fore.MAGENTA, "← régime + momentum overlay + circuit breaker"),
            ("pro",      Fore.YELLOW,  "← VT + adaptive score + corr spike + multi-tier CB"),
            ("adaptive", Fore.CYAN,    "← meta-switch E/B/P + hysteresis 7/5/3j"),
            ("omega",    Fore.WHITE,   "← ER Engine + Risk Engine + Corr Penalty + softmax"),
            ("omega_v2", Fore.GREEN,   "← Omega + Risk Parity + Meta-Learning"),
            ("meta",     Fore.WHITE,   "← Méta-sélecteur ENHANCED/OMEGA/OMEGA_V2/PRO"),
            ("meta_v2",  Fore.GREEN,   "← Meta v2 : engine scoring data-driven"),
        ]
        for key, color, note in strategy_order:
            r = z_results.get(key)
            if not r:
                continue
            m = r["metrics"]
            dd_color = Fore.RED if m["max_dd"] < -30 else (Fore.YELLOW if m["max_dd"] < -15 else Fore.GREEN)
            print(f"  {r['name']:<32} {color}{m['cagr']:>+6.1f}%{Style.RESET_ALL}  "
                  f"{m['sharpe']:>6.2f}  {dd_color}{m['max_dd']:>7.1f}%{Style.RESET_ALL}  "
                  f"{m['final']:>8.0f}€  {note}")

        # Annual comparison
        print(f"\n  Performance annuelle :")
        years = sorted({y for r in z_results.values() for y in r.get("annual", {}).keys()})
        hdr_z = f"  {'Stratégie':<32}" + "".join(f"  {y:>8}" for y in years)
        print(hdr_z)
        print("  " + "-" * (32 + 10 * len(years)))
        for key in ["equal", "z", "hybrid", "enhanced", "pro", "adaptive", "omega", "omega_v2", "meta", "meta_v2"]:
            r = z_results.get(key)
            if not r:
                continue
            line = f"  {r['name']:<32}"
            for y in years:
                pct = r.get("annual", {}).get(y)
                if pct is not None:
                    c = Fore.GREEN if pct > 0 else Fore.RED
                    line += f"  {c}{pct:>+7.1f}%{Style.RESET_ALL}"
                else:
                    line += "         —"
            print(line)

        # Verdict
        enhanced_cagr = z_results.get("enhanced", {}).get("metrics", {}).get("cagr", 0) if z_results.get("enhanced") else 0
        enhanced_dd   = z_results.get("enhanced", {}).get("metrics", {}).get("max_dd", 0) if z_results.get("enhanced") else 0
        pro_cagr      = z_results.get("pro",      {}).get("metrics", {}).get("cagr",   0) if z_results.get("pro") else 0
        pro_dd        = z_results.get("pro",      {}).get("metrics", {}).get("max_dd", 0) if z_results.get("pro") else 0
        eq_cagr       = z_results.get("equal",  {}).get("metrics", {}).get("cagr", 0)
        z_cagr        = z_results.get("z",      {}).get("metrics", {}).get("cagr", 0)
        print(f"\n  Verdict :")
        print(f"  • Bot Z pur vs Equal-weight  : {Fore.GREEN if z_cagr > eq_cagr else Fore.RED}"
              f"{z_cagr - eq_cagr:+.1f}%/an{Style.RESET_ALL} CAGR")
        if enhanced_cagr:
            print(f"  • Enhanced vs Equal-weight   : {Fore.GREEN if enhanced_cagr > eq_cagr else Fore.RED}"
                  f"{enhanced_cagr - eq_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | MaxDD Enhanced = {enhanced_dd:.1f}%")
        if pro_cagr:
            print(f"  • Pro vs Enhanced            : {Fore.GREEN if pro_cagr > enhanced_cagr else Fore.RED}"
                  f"{pro_cagr - enhanced_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | MaxDD Pro = {pro_dd:.1f}%")
        adaptive_r = z_results.get("adaptive")
        if adaptive_r:
            ad_cagr = adaptive_r["metrics"].get("cagr", 0)
            ad_dd   = adaptive_r["metrics"].get("max_dd", 0)
            ad_sharpe = adaptive_r["metrics"].get("sharpe", 0)
            ps = adaptive_r.get("profile_stats", {})
            ps_str = " | ".join(f"{p}:{v:.0f}%" for p, v in ps.items() if v > 0)
            print(f"  • Adaptive vs Enhanced       : {Fore.GREEN if ad_cagr > enhanced_cagr else Fore.RED}"
                  f"{ad_cagr - enhanced_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | "
                  f"MaxDD={ad_dd:.1f}% | Sharpe={ad_sharpe:.2f}")
            print(f"    Profils utilisés : {ps_str}")
        omega_r = z_results.get("omega")
        if omega_r:
            om_cagr   = omega_r["metrics"].get("cagr", 0)
            om_dd     = omega_r["metrics"].get("max_dd", 0)
            om_sharpe = omega_r["metrics"].get("sharpe", 0)
            print(f"  • Omega vs Enhanced          : {Fore.GREEN if om_cagr > enhanced_cagr else Fore.RED}"
                  f"{om_cagr - enhanced_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | "
                  f"MaxDD={om_dd:.1f}% | Sharpe={om_sharpe:.2f}")
        omega_v2_r = z_results.get("omega_v2")
        if omega_v2_r:
            ov2_cagr   = omega_v2_r["metrics"].get("cagr", 0)
            ov2_dd     = omega_v2_r["metrics"].get("max_dd", 0)
            ov2_sharpe = omega_v2_r["metrics"].get("sharpe", 0)
            print(f"  • Omega v2 vs Omega          : {Fore.GREEN if ov2_cagr > (om_cagr if om_cagr else 0) else Fore.RED}"
                  f"{ov2_cagr - (om_cagr if om_cagr else 0):+.1f}%/an{Style.RESET_ALL} CAGR | "
                  f"MaxDD={ov2_dd:.1f}% | Sharpe={ov2_sharpe:.2f}")
        meta_r = z_results.get("meta")
        if meta_r:
            mt_cagr   = meta_r["metrics"].get("cagr", 0)
            mt_dd     = meta_r["metrics"].get("max_dd", 0)
            mt_sharpe = meta_r["metrics"].get("sharpe", 0)
            es = meta_r.get("engine_stats", {})
            es_str = " | ".join(f"{e}:{v:.0f}%" for e, v in es.items() if v > 0)
            print(f"  • Meta vs Enhanced           : {Fore.GREEN if mt_cagr > enhanced_cagr else Fore.RED}"
                  f"{mt_cagr - enhanced_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | "
                  f"MaxDD={mt_dd:.1f}% | Sharpe={mt_sharpe:.2f}")
            print(f"    Engines utilisés : {es_str}")
        meta_v2_r = z_results.get("meta_v2")
        if meta_v2_r:
            mv2_cagr   = meta_v2_r["metrics"].get("cagr", 0)
            mv2_dd     = meta_v2_r["metrics"].get("max_dd", 0)
            mv2_sharpe = meta_v2_r["metrics"].get("sharpe", 0)
            es2 = meta_v2_r.get("engine_stats", {})
            es2_str = " | ".join(f"{e}:{v:.0f}%" for e, v in es2.items() if v > 0)
            print(f"  • Meta v2 vs Enhanced        : {Fore.GREEN if mv2_cagr > enhanced_cagr else Fore.RED}"
                  f"{mv2_cagr - enhanced_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | "
                  f"MaxDD={mv2_dd:.1f}% | Sharpe={mv2_sharpe:.2f}")
            print(f"    Engines utilisés : {es2_str}")
        print(f"{Fore.CYAN}{'='*100}{Style.RESET_ALL}")

        # Walk-forward et Monte Carlo
        if wf:
            print_walk_forward(wf)
        if mc:
            print_monte_carlo(mc)

        # Save Z comparison to CSV
        z_rows = []
        for key in ["equal", "z", "hybrid", "enhanced", "pro", "adaptive", "omega", "omega_v2", "meta", "meta_v2"]:
            r = z_results.get(key)
            if r:
                m = r["metrics"]
                z_row = {"strategie": r["name"], "cagr": m["cagr"], "sharpe": m["sharpe"],
                         "max_dd": m["max_dd"], "final": m["final"]}
                for y in years:
                    z_row[f"annual_{y}"] = r.get("annual", {}).get(y, None)
                z_rows.append(z_row)
        if z_rows:
            pd.DataFrame(z_rows).to_csv(f"{RESULTS_DIR}/bot_z_comparison.csv", index=False)
            log(f"CSV Bot Z sauvegardé : {RESULTS_DIR}/bot_z_comparison.csv", Fore.GREEN)

    # CSV
    pd.DataFrame(rows).to_csv(f"{RESULTS_DIR}/multi_summary.csv", index=False)
    log(f"CSV sauvegardé : {RESULTS_DIR}/multi_summary.csv", Fore.GREEN)


def plot_equity_curves(results, z_results=None):
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
            ax.plot(r["dates"], eq_pct, label=r["name"], color=color, linewidth=1.2, alpha=0.6)

        # Bot Z 3 structures (use 4×INITIAL as base since it's 4 bots)
        if z_results:
            init4 = INITIAL * len(VALID_BOTS_Z)
            z_styles = [
                ("equal",    "--",  1.5, "#f0883e"),
                ("z",        "-.",  2.0, "#a371f7"),
                ("hybrid",   ":",   1.5, "#8b949e"),
                ("enhanced", "-",   2.8, "#ffffff"),
                ("pro",      "-",   2.5, "#ffd700"),
                ("adaptive", "-",   3.2, "#00d9ff"),
                ("omega",    "-",   3.0, "#ff6e96"),
                ("omega_v2", "-",   3.5, "#00ff88"),
                ("meta",     "-",   4.0, "#ff00ff"),
            ]
            for key, style, lw, color in z_styles:
                r = z_results.get(key)
                if r and r["equity"]:
                    eq_pct = [(v / init4 - 1) * 100 for v in r["equity"]]
                    ax.plot(r["dates"], eq_pct, label=r["name"],
                            color=color, linewidth=lw, linestyle=style, zorder=5)

        ax.axhline(0, color="#8b949e", linewidth=0.8, linestyle="--")
        ax.set_title("Backtest multi-période — Equity curves + 3 structures Bot Z", color="#e6edf3", fontsize=13, fontweight="bold")
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
        "j": backtest_bot_j_mean_reversion,
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
        # Structures portfolio
        z_results = backtest_bot_z_portfolio(results, vix_s, qqq_df)

        # Bot Z Enhanced (momentum overlay + circuit breaker)
        z_enhanced = backtest_bot_z_enhanced(results, vix_s, qqq_df, daily)
        if z_enhanced:
            z_results["enhanced"] = z_enhanced

        # Bot Z Pro (vol targeting + adaptive score + corr spike + multi-tier CB)
        z_pro = backtest_bot_z_pro(results, vix_s, qqq_df, daily)
        if z_pro:
            z_results["pro"] = z_pro

        # Bot Z Adaptive (meta-switch Enhanced/Balanced/Pro + hysteresis)
        z_adaptive = backtest_bot_z_adaptive(results, vix_s, qqq_df, daily)
        if z_adaptive:
            z_results["adaptive"] = z_adaptive

        log("Bot Z Omega — Expected Return Engine + Risk Engine + Corr Penalty...")
        z_omega = backtest_bot_z_omega(results, vix_s, qqq_df, daily)
        if z_omega:
            z_results["omega"] = z_omega

        log("Bot Z Omega v2 — Risk Parity + Meta-Learning...")
        z_omega_v2 = backtest_bot_z_omega_v2(results, vix_s, qqq_df, daily)
        if z_omega_v2:
            z_results["omega_v2"] = z_omega_v2

        log("Bot Z Meta — Méta-sélecteur ENHANCED/OMEGA/OMEGA_V2/PRO...")
        z_meta = backtest_bot_z_meta(results, vix_s, qqq_df, daily)
        if z_meta:
            z_results["meta"] = z_meta

        log("Bot Z Meta v2 — Engine Scoring data-driven...")
        z_meta_v2 = backtest_bot_z_meta_v2(results, vix_s, qqq_df, daily)
        if z_meta_v2:
            z_results["meta_v2"] = z_meta_v2

        # Walk-forward test
        wf = walk_forward_test(results, vix_s, qqq_df, daily, split_year=2023)

        # Monte Carlo (5000 simulations — plus robuste statistiquement)
        mc = monte_carlo_test(results, n_simulations=5000)

        print_report(results, vix_s, qqq_df, z_results, wf=wf, mc=mc)
        plot_equity_curves(results, z_results)

    log(f"Terminé en {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

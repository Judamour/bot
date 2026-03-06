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


def _metrics_portfolio(equity_list, dates_list, init):
    """Métriques sur une equity curve (CAGR, Sharpe, MaxDD)."""
    if not equity_list or len(equity_list) < 2:
        return {"cagr": 0, "sharpe": 0, "max_dd": 0, "final": init,
                "profit_factor": 0, "trades": 0, "win_rate": 0}
    eq = np.array(equity_list, dtype=float)
    n_years = (dates_list[-1] - dates_list[0]).days / 365.25
    cagr = ((eq[-1] / init) ** (1 / n_years) - 1) * 100 if n_years > 0.1 else 0
    ret = pd.Series(eq).pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * math.sqrt(252)) if ret.std() > 0 else 0
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

    # Intersection des dates communes
    date_sets = [set(r["dates"]) for r in valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        log("Bot Z: pas de dates communes.", Fore.YELLOW)
        return {}

    # Normalisé : returns quotidiens de chaque bot (base 1.0 = départ)
    bot_norm = {}
    for k, r in valid.items():
        idx = {d: v / INITIAL for d, v in zip(r["dates"], r["equity"])}
        bot_norm[k] = idx

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

    # Métriques
    m_equal  = _metrics_portfolio(eq_equal,  dates_out, initial_total)
    m_z      = _metrics_portfolio(eq_z,      dates_out, initial_total)
    m_hybrid = _metrics_portfolio(eq_hybrid, dates_out, initial_total)

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

    date_sets = [set(r["dates"]) for r in valid.values()]
    common_dates = sorted(set.intersection(*date_sets))
    if not common_dates:
        return {}

    n_bots = len(valid)
    initial_total = INITIAL * n_bots

    bot_norm = {}
    for k, r in valid.items():
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

    m = _metrics_portfolio(eq_enhanced, dates_out, initial_total)
    ann = annual_returns(eq_enhanced, dates_out)
    return {
        "name": "Bot Z Enhanced (MO + CB)",
        "equity": eq_enhanced, "dates": dates_out, "trades": [],
        "metrics": m, "annual": ann, "regime": {},
    }


# ── 13. WALK-FORWARD TEST ─────────────────────────────────────────────────────

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
            "bot_name":   r["name"],
            "n_trades":   n_trades,
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
    print(f"  MONTE CARLO — Robustesse statistique (1000 simulations / ordre des trades randomisé)")
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
        print(f"  BOT Z — 3 STRUCTURES PORTFOLIO (4 bots valides A+B+C+G | capital 4×1000€)")
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
        for key in ["equal", "z", "hybrid", "enhanced"]:
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
        eq_cagr       = z_results.get("equal",  {}).get("metrics", {}).get("cagr", 0)
        z_cagr        = z_results.get("z",      {}).get("metrics", {}).get("cagr", 0)
        print(f"\n  Verdict :")
        print(f"  • Bot Z pur vs Equal-weight  : {Fore.GREEN if z_cagr > eq_cagr else Fore.RED}"
              f"{z_cagr - eq_cagr:+.1f}%/an{Style.RESET_ALL} CAGR")
        if enhanced_cagr:
            print(f"  • Enhanced vs Equal-weight   : {Fore.GREEN if enhanced_cagr > eq_cagr else Fore.RED}"
                  f"{enhanced_cagr - eq_cagr:+.1f}%/an{Style.RESET_ALL} CAGR | MaxDD Enhanced = {enhanced_dd:.1f}%")
        print(f"{Fore.CYAN}{'='*100}{Style.RESET_ALL}")

        # Walk-forward et Monte Carlo
        if wf:
            print_walk_forward(wf)
        if mc:
            print_monte_carlo(mc)

        # Save Z comparison to CSV
        z_rows = []
        for key in ["equal", "z", "hybrid", "enhanced"]:
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

        # Walk-forward test
        wf = walk_forward_test(results, vix_s, qqq_df, daily, split_year=2023)

        # Monte Carlo (1000 simulations)
        mc = monte_carlo_test(results, n_simulations=1000)

        print_report(results, vix_s, qqq_df, z_results, wf=wf, mc=mc)
        plot_equity_curves(results, z_results)

    log(f"Terminé en {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

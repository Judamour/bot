#!/usr/bin/env python3
"""Backtest 3 ans du Shadow Bot v2 (moteur unifié single-loop, 4h granularity).

Réutilise EXACTEMENT les détecteurs et scorer du shadow runner :
  - shadow.strategies.ALL_DETECTORS (5 détecteurs)
  - shadow.scorer.compute_score (score composite 0-100)
  - shadow.sizing.compute_size (score-weighted, top-3 par cycle)
  - shadow.quality_gate.passes (4 gates méchaniques)
  - shadow.regime.shield_active (SHIELD VIX/BTC/QQQ)
  - shadow.risk_guard.RiskGuard (MaxDD halt + cooldowns)
  - Trailing ATR adaptatif (tight 1.5× → loose 3.0× au-delà +5%)

Sortie : CAGR, Sharpe, MaxDD, trades, win rate, profit factor.
Comparaison avec backtests prod : Bot A solo (CAGR 49%, Sharpe 2.43) et Bot Z (CAGR 38%).
"""
import os
import sys
import warnings
import json
from datetime import datetime, timezone

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shadow.strategies import ALL_DETECTORS
from shadow.scorer import compute_score, Signal
from shadow.constants_v2 import (
    SCORE_FLOOR, TOP_N_SIGNALS, MAX_OPEN_POSITIONS,
    ATR_MULT_STOP_INIT, ATR_MULT_TRAIL, PROFIT_LOOSEN_PCT,
    ACTIVE_DETECTORS,
)

# Filter detectors to the active subset (drops noisy 4h detectors per v2 iter-1)
ALL_DETECTORS = [d for d in ALL_DETECTORS if d.__name__.replace("detect_", "") in ACTIVE_DETECTORS]
from shadow.regime import shield_active
from shadow.quality_gate import passes as gate_passes
from shadow.risk_guard import RiskGuard
from shadow.sizing import compute_size
from strategies.supertrend import compute_atr
from backtest.multi_backtest import compute_metrics, INITIAL

# ── Config ───────────────────────────────────────────────────────────────────
START = "2023-04-29"
END = "2026-04-29"
INITIAL_CAPITAL = INITIAL  # 1000 from multi_backtest
FEE = 0.0026
SLIPPAGE = 0.001

DAYS_4H = 365 * 3 + 60          # 3 ans 4h + warmup
DAYS_1D = 365 * 3 + 220         # 3 ans + 220 jours pour SMA200

# Univers identique au prod (21 actifs Alpaca crisis-alpha)
CRYPTO = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD",
          "AVAX/USD": "AVAX-USD", "LINK/USD": "LINK-USD"}
STOCKS = ["NVDA", "GOOGL", "META", "PLTR", "CRWD", "LLY", "ABBV", "XOM", "CVX",
          "JPM", "BAC", "KO", "PG", "SPY", "QQQ", "GLD"]
ALL_SYMBOLS = list(CRYPTO.keys()) + STOCKS


def fetch_bars(symbol_internal: str, timeframe: str, days: int) -> pd.DataFrame | None:
    """Fetch OHLCV via prod data.fetcher (Binance crypto / Alpaca-or-yf stocks)."""
    from data.fetcher import fetch_ohlcv
    try:
        df = fetch_ohlcv(symbol_internal, timeframe, days)
        if df is None or len(df) < 50:
            return None
        df.columns = [c.lower() for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception as e:
        print(f"  [skip] {symbol_internal} {timeframe}: {e}")
        return None


def main():
    print(f"=== SHADOW BACKTEST v2 {START} → {END} (4h) ===\n")

    print("Chargement OHLCV 4h (signaux) + 1d (MTF + régime QQQ)…")
    cache_4h, cache_1d = {}, {}
    for sym in ALL_SYMBOLS:
        df_4h = fetch_bars(sym, "4h", days=DAYS_4H)
        df_1d = fetch_bars(sym, "1d", days=DAYS_1D)
        if df_4h is not None and df_1d is not None:
            cache_4h[sym] = df_4h
            cache_1d[sym] = df_1d
            print(f"  ✓ {sym:10}: {len(df_4h):4} bars 4h / {len(df_1d):4} bars 1d")
    if not cache_4h:
        print("Aucune donnée chargée")
        return

    # Date timeline: intersection of all 4h indices
    common_bars = sorted(set.intersection(*[set(df.index) for df in cache_4h.values()]))
    print(f"\n{len(common_bars)} barres 4h communes\n")

    # Warmup : need 220 1d bars for SMA200 in detectors → skip first ~330 4h bars
    WARMUP_BARS_4H = 330
    if len(common_bars) <= WARMUP_BARS_4H:
        print(f"Pas assez d'historique ({len(common_bars)} bars)")
        return

    capital = INITIAL_CAPITAL
    positions = {}                       # sym → dict
    trades = []
    equity_curve = []
    equity_ts = []

    # Risk guard state (in-memory only for backtest — no persistence)
    rg = RiskGuard(state_path="/tmp/__backtest_risk_state__.json",
                   peak_equity=INITIAL_CAPITAL,
                   peak_date=common_bars[WARMUP_BARS_4H])

    ctx_default = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": True}

    for i, bar_ts in enumerate(common_bars[WARMUP_BARS_4H:], start=WARMUP_BARS_4H):
        # 1. Macro context (approx via SPY trend on 1d cache)
        ctx = dict(ctx_default)
        if "SPY" in cache_1d:
            spy = cache_1d["SPY"]
            spy_slice = spy.loc[:bar_ts.normalize()] if hasattr(bar_ts, "normalize") else spy.loc[:bar_ts]
            if len(spy_slice) >= 200:
                ctx["qqq_regime_ok"] = bool(spy_slice["close"].iloc[-1] > spy_slice["close"].tail(200).mean())

        # 2. Check halt / SHIELD
        halted = rg.is_halted(now=bar_ts)
        shielded = shield_active(ctx)
        skip_new_entries = halted or shielded

        # 3. Update trailing stops + check stops for open positions
        for sym in list(positions.keys()):
            df = cache_4h[sym]
            if bar_ts not in df.index:
                continue
            bar = df.loc[bar_ts]
            pos = positions[sym]
            low, close = float(bar["low"]), float(bar["close"])
            # Stop hit?
            if low <= pos["stop"]:
                exit_price = pos["stop"] * (1 - SLIPPAGE)
                proceeds = exit_price * pos["size"]
                fee_exit = proceeds * FEE
                capital += proceeds - fee_exit
                pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee_exit
                trades.append({
                    "symbol": sym, "strategy": pos["strategy"],
                    "entry": pos["entry"], "exit": exit_price,
                    "entry_ts": pos["entry_ts"], "exit_ts": bar_ts,
                    "pnl": round(pnl, 2), "reason": "stop_loss", "score": pos["score"],
                })
                rg.register_stop(sym, pnl, now=bar_ts)
                del positions[sym]
                continue
            # Trailing update (adaptive)
            df_slice = df.loc[:bar_ts]
            if len(df_slice) < 15:
                continue
            atr = float(compute_atr(df_slice["high"], df_slice["low"], df_slice["close"], 14).iloc[-1] or 0)
            if atr <= 0:
                continue
            pnl_pct = (close - pos["entry"]) / pos["entry"]
            atr_mult = ATR_MULT_TRAIL if pnl_pct >= PROFIT_LOOSEN_PCT else ATR_MULT_STOP_INIT
            new_stop = close - atr_mult * atr
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop

        if skip_new_entries:
            eq = capital + sum(float(cache_4h[s].loc[bar_ts]["close"]) * p["size"]
                               for s, p in positions.items() if bar_ts in cache_4h[s].index)
            equity_curve.append(eq)
            equity_ts.append(bar_ts)
            rg.update_equity(eq, now=bar_ts)
            continue

        # 4. Scan signals
        candidates = []
        for sym in ALL_SYMBOLS:
            if sym in positions or sym not in cache_4h:
                continue
            df_4h = cache_4h[sym]
            df_1d = cache_1d.get(sym)
            if bar_ts not in df_4h.index:
                continue
            df_4h_hist = df_4h.loc[:bar_ts]
            if len(df_4h_hist) < 60:
                continue
            df_1d_hist = df_1d.loc[:bar_ts.normalize()] if (df_1d is not None and hasattr(bar_ts, "normalize")) else df_1d
            if df_1d_hist is None or len(df_1d_hist) < 220:
                continue
            for detector in ALL_DETECTORS:
                try:
                    sig = detector(sym, df_4h_hist, df_1d_hist)
                    if sig is None:
                        continue
                    sig.score = compute_score(sig, ctx)
                    if sig.score >= SCORE_FLOOR:
                        candidates.append(sig)
                except Exception:
                    pass

        # 5. Dédup by symbol
        best_by_symbol: dict[str, Signal] = {}
        for sig in candidates:
            if sig.symbol not in best_by_symbol or sig.score > best_by_symbol[sig.symbol].score:
                best_by_symbol[sig.symbol] = sig
        sorted_cands = sorted(best_by_symbol.values(), key=lambda s: s.score, reverse=True)

        # 6. Quality gate + size by rank
        accepted = [s for s in sorted_cands if gate_passes(s, rg, now=bar_ts)][:TOP_N_SIGNALS]
        for rank, sig in enumerate(accepted):
            if len(positions) >= MAX_OPEN_POSITIONS:
                break
            size_res = compute_size(rank=rank, cash=capital, entry_price=sig.entry_price)
            if size_res.qty <= 0:
                continue
            entry_eff = sig.entry_price * (1 + SLIPPAGE)
            cost = entry_eff * size_res.qty
            fee = cost * FEE
            total = cost + fee
            if total > capital:
                continue
            capital -= total
            stop_initial = entry_eff - ATR_MULT_STOP_INIT * sig.atr
            positions[sig.symbol] = {
                "strategy": sig.strategy, "score": sig.score,
                "entry": entry_eff, "size": size_res.qty,
                "stop": stop_initial, "atr": sig.atr,
                "entry_ts": bar_ts, "fee_entry": fee,
            }

        # 7. Equity snapshot
        eq = capital + sum(float(cache_4h[s].loc[bar_ts]["close"]) * p["size"]
                           for s, p in positions.items() if bar_ts in cache_4h[s].index)
        equity_curve.append(eq)
        equity_ts.append(bar_ts)
        rg.update_equity(eq, now=bar_ts)

    # Force close all open positions at the last bar. If a position's symbol has no
    # bar at last_bar (data hole), use its last known close from earlier in cache
    # instead of silently dropping it (which would corrupt G4 accounting).
    last_bar = common_bars[-1]
    for sym in list(positions.keys()):
        df = cache_4h[sym]
        pos = positions[sym]
        if last_bar in df.index:
            exit_price_raw = float(df.loc[last_bar]["close"])
        else:
            if len(df) == 0:
                print(f"  [warn] {sym} has no data at end-of-backtest, recovering at entry price (zero P&L)")
                exit_price_raw = pos["entry"]
            else:
                exit_price_raw = float(df["close"].iloc[-1])
                print(f"  [warn] {sym} missing last_bar, using last available close {exit_price_raw}")
        exit_price = exit_price_raw * (1 - SLIPPAGE)
        proceeds = exit_price * pos["size"]
        fee_exit = proceeds * FEE
        capital += proceeds - fee_exit
        pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee_exit
        trades.append({
            "symbol": sym, "strategy": pos["strategy"],
            "entry": pos["entry"], "exit": exit_price,
            "entry_ts": pos["entry_ts"], "exit_ts": last_bar,
            "pnl": round(pnl, 2), "reason": "end_of_backtest", "score": pos["score"],
        })
        del positions[sym]

    # G4 invariant: sum(trade_pnl) ≈ final - initial
    sum_pnl = sum(t["pnl"] for t in trades)
    delta_capital = capital - INITIAL_CAPITAL
    accounting_gap = abs(sum_pnl - delta_capital)
    gap_pct = (accounting_gap / INITIAL_CAPITAL) * 100
    assert gap_pct < 1.0, (
        f"COMPTABILITÉ INCOHÉRENTE: sum(pnl)={sum_pnl:.2f} vs delta_capital={delta_capital:.2f} "
        f"écart={accounting_gap:.2f} ({gap_pct:.2f}%) — anti-régression bug 7803182"
    )

    from backtest.multi_backtest import compute_metrics
    metrics = compute_metrics(trades, equity_curve, initial=INITIAL_CAPITAL)

    # ── Résultats ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  SHADOW BOT v2 — Backtest {START} → {END} (4h)")
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
    print(f"\n Références prod :")
    print(f"  Bot A solo (3y)         : +33% CAGR, Sharpe 2.24")
    print(f"  Bot Z PROD Meta v2 (3y) : +27% CAGR, Sharpe 1.71")
    print(f"  Bot Z régime pur (3y)   : +40% CAGR, Sharpe 1.04")
    print(f"  Z v2 QualityScore (4y)  : +43% CAGR, Sharpe 1.37")

    # Sauvegarde
    os.makedirs("backtest/results", exist_ok=True)
    out = {
        "params": {
            "start": START, "end": END,
            "min_score": SCORE_FLOOR, "top_n": TOP_N_SIGNALS,
            "max_open": MAX_OPEN_POSITIONS,
            "atr_mult_stop_init": ATR_MULT_STOP_INIT,
            "atr_mult_trail": ATR_MULT_TRAIL,
            "profit_loosen_pct": PROFIT_LOOSEN_PCT,
        },
        "metrics": metrics,
        "n_trades": len(trades),
        "by_strategy": {s: {"n": len(ts), "pnl": round(sum(t["pnl"] for t in ts), 2)}
                        for s, ts in by_strat.items()},
        "equity_dates": [str(d.date()) if hasattr(d, "date") else str(d) for d in equity_ts[::30]],
        "equity_curve_monthly": [round(equity_curve[i], 0) for i in range(0, len(equity_curve), 30)],
    }
    with open("backtest/results/shadow_3y.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n Sauvegardé : backtest/results/shadow_3y.json")


if __name__ == "__main__":
    main()

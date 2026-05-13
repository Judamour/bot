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
import pickle
import time
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
    ACTIVE_DETECTORS, DEFENSIVE_SYMBOLS, DEFENSIVE_AND_INVERSE,
    INVERSE_ETFS, EQUITY_BEAR_SIZE_FACTOR,
    SECTOR_MAP, MAX_PER_SECTOR, MACRO_EXIT_PROFIT_PCT,
)

# Filter detectors to the active subset (drops noisy 4h detectors per v2 iter-1)
ALL_DETECTORS = [d for d in ALL_DETECTORS if d.__name__.replace("detect_", "") in ACTIVE_DETECTORS]
from shadow.regime import shield_active, equity_bear_active
from shadow.quality_gate import passes as gate_passes
from shadow.risk_guard import RiskGuard
from shadow.sizing import compute_size
from strategies.supertrend import compute_atr
from backtest.multi_backtest import compute_metrics, INITIAL

# ── Config ───────────────────────────────────────────────────────────────────
START = "2022-01-03"            # extended to include 2022 tech bear (QQQ -33%)
END = "2026-04-29"
INITIAL_CAPITAL = INITIAL  # 1000 from multi_backtest
FEE = 0.0026
SLIPPAGE = 0.001

DAYS_4H = 365 * 5               # 5 ans 4h + warmup pour couvrir 2022-01 → 2026-04
DAYS_1D = 365 * 5 + 220         # 5 ans + 220 jours pour SMA200 warmup

# Univers identique au prod (21 actifs Alpaca crisis-alpha)
CRYPTO = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD",
          "AVAX/USD": "AVAX-USD", "LINK/USD": "LINK-USD"}
STOCKS = ["NVDA", "GOOGL", "META", "PLTR", "CRWD", "LLY", "ABBV", "XOM", "CVX",
          "JPM", "BAC", "KO", "PG", "SPY", "QQQ", "GLD"]
ALL_SYMBOLS = list(CRYPTO.keys()) + STOCKS


# ── OHLCV cache (iter-5 #16) ────────────────────────────────────────────────
# Pickle-based local cache to skip re-fetching the same data across runs.
# Speeds backtest iteration from ~40s → ~3s when cache is warm.
# Invalidation: delete backtest/cache/ to force fresh fetch.
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CACHE_TTL_SECONDS = 6 * 3600  # 6h — long enough for iterative tuning sessions


def _cache_key(symbol: str, timeframe: str, days: int) -> str:
    """Safe filename: replace / with _ for crypto pairs."""
    safe = symbol.replace("/", "_").replace("^", "")
    return f"{safe}_{timeframe}_{days}"


def _cache_load(key: str):
    """Return cached DataFrame if fresh (< CACHE_TTL_SECONDS old), else None."""
    path = os.path.join(CACHE_DIR, f"{key}.pkl")
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age >= CACHE_TTL_SECONDS:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _cache_save(key: str, value) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{key}.pkl")
    try:
        with open(path, "wb") as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        print(f"  [cache-warn] failed to save {key}: {e}")


def fetch_bars(symbol_internal: str, timeframe: str, days: int) -> pd.DataFrame | None:
    """Fetch OHLCV via prod data.fetcher with local pickle cache."""
    key = _cache_key(symbol_internal, timeframe, days)
    cached = _cache_load(key)
    if cached is not None:
        return cached
    from data.fetcher import fetch_ohlcv
    try:
        df = fetch_ohlcv(symbol_internal, timeframe, days)
        if df is None or len(df) < 50:
            return None
        df.columns = [c.lower() for c in df.columns]
        out = df[["open", "high", "low", "close", "volume"]].dropna()
        _cache_save(key, out)
        return out
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

    # VIX cache for real SHIELD activation in backtest (pickle-cached too)
    print("Chargement VIX (régime SHIELD)…")
    vix_key = _cache_key("VIX", "1d", DAYS_1D)
    vix_cached = _cache_load(vix_key)
    if vix_cached is not None:
        cache_1d["VIX"] = vix_cached
        print(f"  ✓ VIX       : {len(vix_cached)} bars 1d (from cache)")
    else:
        try:
            import yfinance as yf
            vix_df = yf.download("^VIX", period=f"{int(DAYS_1D)}d", interval="1d",
                                 progress=False, auto_adjust=False)
            if vix_df is not None and len(vix_df) > 0:
                if hasattr(vix_df.columns, 'levels'):
                    vix_df.columns = vix_df.columns.get_level_values(0)
                vix_df.columns = [c.lower() for c in vix_df.columns]
                vix_df.index = vix_df.index.tz_localize(None) if vix_df.index.tz else vix_df.index
                vix_out = vix_df[["close"]]
                cache_1d["VIX"] = vix_out
                _cache_save(vix_key, vix_out)
                print(f"  ✓ VIX       : {len(vix_df)} bars 1d ({vix_df.index[0].date()} → {vix_df.index[-1].date()})")
        except Exception as e:
            print(f"  [skip] VIX fetch failed: {e} — backtest will use vix=18 stub")

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

    # Diagnostic counters (iter-4)
    n_cycles_total = 0
    n_cycles_shielded = 0
    n_cycles_rotation = 0
    n_trades_in_rotation = 0

    # Risk guard state (in-memory only for backtest — no persistence)
    rg = RiskGuard(state_path="/tmp/__backtest_risk_state__.json",
                   peak_equity=INITIAL_CAPITAL,
                   peak_date=common_bars[WARMUP_BARS_4H])

    ctx_default = {"vix": 18.0, "btc_trend": "bull", "qqq_regime_ok": True}

    for i, bar_ts in enumerate(common_bars[WARMUP_BARS_4H:], start=WARMUP_BARS_4H):
        # 1. Macro context (real VIX + BTC trend + SPY full uptrend)
        ctx = dict(ctx_default)
        if "SPY" in cache_1d:
            spy = cache_1d["SPY"]
            spy_slice = spy.loc[:bar_ts.normalize()] if hasattr(bar_ts, "normalize") else spy.loc[:bar_ts]
            if len(spy_slice) >= 200:
                spy_close = float(spy_slice["close"].iloc[-1])
                sma_200 = float(spy_slice["close"].tail(200).mean())
                sma_50 = float(spy_slice["close"].tail(50).mean())
                ctx["qqq_regime_ok"] = spy_close > sma_200
                # Full uptrend: required to exit equity_bear (hysteresis)
                ctx["qqq_full_uptrend"] = spy_close > sma_50 > sma_200
        # Real BTC trend (drives SHIELD)
        if "BTC/USD" in cache_1d:
            btc = cache_1d["BTC/USD"]
            btc_slice = btc.loc[:bar_ts.normalize()] if hasattr(bar_ts, "normalize") else btc.loc[:bar_ts]
            if len(btc_slice) >= 200:
                btc_close = float(btc_slice["close"].iloc[-1])
                btc_sma_200 = float(btc_slice["close"].tail(200).mean())
                ctx["btc_trend"] = "bull" if btc_close > btc_sma_200 else "bear"
        # Real VIX (drives SHIELD) — fetched from cache_1d_vix if available
        if "VIX" in cache_1d:
            vix_df = cache_1d["VIX"]
            # Normalize timezone — both sides must be tz-naive
            bar_ts_naive = bar_ts.tz_localize(None) if getattr(bar_ts, "tz", None) else bar_ts
            cutoff = bar_ts_naive.normalize() if hasattr(bar_ts_naive, "normalize") else bar_ts_naive
            try:
                vix_slice = vix_df.loc[:cutoff]
                if len(vix_slice) >= 1:
                    ctx["vix"] = float(vix_slice["close"].iloc[-1])
            except (KeyError, TypeError):
                pass  # fall back to default vix=18

        # 2. Check halt / SHIELD / equity_bear rotation
        halted = rg.is_halted(now=bar_ts)
        shielded = shield_active(ctx)
        equity_bear = equity_bear_active(ctx)
        skip_new_entries = halted or shielded
        # When in equity bear (and not in full SHIELD), rotate to defensives only
        # with reduced sizing — instead of going fully dormant.
        rotate_defensives = equity_bear and not skip_new_entries
        size_factor = EQUITY_BEAR_SIZE_FACTOR if rotate_defensives else 1.0
        n_cycles_total += 1
        if shielded:
            n_cycles_shielded += 1
        if rotate_defensives:
            n_cycles_rotation += 1

        # 3. Update trailing stops + check stops for open positions
        # Macro-aware exit (iter-6 #3): when SHIELD/HALT activates AND position
        # is already at +MACRO_EXIT_PROFIT_PCT (15%), lock in the gain. Higher
        # threshold than iter-5's +5% (which cut bull rallies). Only protects
        # already-strong winners.
        for sym in list(positions.keys()):
            df = cache_4h[sym]
            if bar_ts not in df.index:
                continue
            bar = df.loc[bar_ts]
            pos = positions[sym]
            low, close = float(bar["low"]), float(bar["close"])
            pnl_pct = (close - pos["entry"]) / pos["entry"]
            if (shielded or halted) and pnl_pct >= MACRO_EXIT_PROFIT_PCT:
                exit_price = close * (1 - SLIPPAGE)
                proceeds = exit_price * pos["size"]
                fee_exit = proceeds * FEE
                capital += proceeds - fee_exit
                pnl = (exit_price - pos["entry"]) * pos["size"] - pos["fee_entry"] - fee_exit
                trades.append({
                    "symbol": sym, "strategy": pos["strategy"],
                    "entry": pos["entry"], "exit": exit_price,
                    "entry_ts": pos["entry_ts"], "exit_ts": bar_ts,
                    "pnl": round(pnl, 2), "reason": "macro_take_profit", "score": pos["score"],
                })
                rg.register_stop(sym, pnl, now=bar_ts)
                del positions[sym]
                continue
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
        # In equity_bear regime, restrict scan to DEFENSIVE_SYMBOLS subset
        # (gold, healthcare, defensive consumer, energy) — these historically
        # perform when broad equity is in bear.
        scan_universe = (
            [s for s in ALL_SYMBOLS if s in DEFENSIVE_SYMBOLS]
            if rotate_defensives else ALL_SYMBOLS
        )
        candidates = []
        for sym in scan_universe:
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

        # 6. Quality gate + sector diversification (max 1 per sector per cycle)
        # Already-open positions count against their sector's quota.
        gate_passed = [s for s in sorted_cands if gate_passes(s, rg, now=bar_ts)]
        sector_count: dict[str, int] = {}
        for sym in positions.keys():
            sec = SECTOR_MAP.get(sym, "other")
            sector_count[sec] = sector_count.get(sec, 0) + 1
        accepted = []
        for sig in gate_passed:
            sec = SECTOR_MAP.get(sig.symbol, "other")
            if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
                continue
            accepted.append(sig)
            sector_count[sec] = sector_count.get(sec, 0) + 1
            if len(accepted) >= TOP_N_SIGNALS:
                break
        for rank, sig in enumerate(accepted):
            if len(positions) >= MAX_OPEN_POSITIONS:
                break
            # Score-weighted sizing (vol-adjust disabled: dampened bull CAGR -21pt)
            size_res = compute_size(rank=rank, cash=capital,
                                    entry_price=sig.entry_price)
            if size_factor != 1.0:
                size_res = type(size_res)(qty=size_res.qty * size_factor,
                                          notional=size_res.notional * size_factor)
            if size_res.qty <= 0:
                continue
            entry_eff = sig.entry_price * (1 + SLIPPAGE)
            cost = entry_eff * size_res.qty
            fee = cost * FEE
            total = cost + fee
            if total > capital:
                continue
            capital -= total
            if rotate_defensives:
                n_trades_in_rotation += 1
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

    # Breakdown par symbole
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    print("\nTrades par symbole (TOUS, triés par PnL) :")
    sym_pnl = [(sym, sum(t["pnl"] for t in ts), len(ts),
                sum(1 for t in ts if t["pnl"] > 0))
               for sym, ts in by_sym.items()]
    for sym, pnl, n, wins in sorted(sym_pnl, key=lambda x: -x[1]):
        tag = " [DEF]" if sym in ("GLD", "KO", "PG", "LLY", "ABBV", "XOM", "CVX") else ""
        print(f"  {sym:10} : {n:3} trades | win {wins/n*100:5.1f}% | PnL ${pnl:+,.0f}{tag}")

    # Régime diagnostics
    print(f"\nRégime cycles :")
    print(f"  Total cycles   : {n_cycles_total}")
    print(f"  SHIELD active  : {n_cycles_shielded} ({n_cycles_shielded/max(n_cycles_total,1)*100:.1f}%)")
    print(f"  Rotation déf.  : {n_cycles_rotation} ({n_cycles_rotation/max(n_cycles_total,1)*100:.1f}%)")
    print(f"  Trades en rotation : {n_trades_in_rotation} sur {len(trades)} total")

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

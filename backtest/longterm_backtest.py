#!/usr/bin/env python3
"""
backtest/longterm_backtest.py
==============================
Backtest long-terme — maximum de données disponibles par symbole.

Couverture cible :
  xStocks (yfinance period=max) : NVDA 1999, AAPL 1980, MSFT 1986, META 2012, GOOGL 2004...
  BTC/EUR (yfinance)            : ~2014
  ETH/EUR (yfinance)            : ~2017
  SOL, BNB, TON                 : données limitées (2020+)
  VIX + QQQ                     : period=max (~1993)

Stratégie : réutilise les fonctions backtest de multi_backtest.py.
            Seule la couche DATA est remplacée pour aller chercher le max.

Produit :
  - Rapport console complet
  - backtest/results/longterm_summary.csv
  - backtest/results/longterm_equity.png
  - backtest/results/longterm_annual.csv

Usage :
    python backtest/longterm_backtest.py
"""
import os
import sys
import math
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf
from colorama import Fore, Style, init

warnings.filterwarnings("ignore")
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Import de toutes les fonctions backtest existantes
from backtest.multi_backtest import (
    compute_metrics, annual_returns, regime_returns, log,
    backtest_bot_a, backtest_bot_b, backtest_bot_c, backtest_bot_g,
    backtest_bot_h, backtest_bot_i, backtest_bot_j_mean_reversion,
    backtest_bot_z_portfolio, backtest_bot_z_enhanced, backtest_bot_z_pro,
    backtest_bot_z_omega, backtest_bot_z_omega_v2, backtest_bot_z_meta_v2,
    INITIAL, classify_regime, get_regime_at,
)

init(autoreset=True)

RESULTS_DIR = "backtest/results"
os.makedirs(RESULTS_DIR, exist_ok=True)

# ── Mapping crypto → symbole yfinance ─────────────────────────────────────────
CRYPTO_YF_MAP = {
    "BTC/EUR": "BTC-EUR",
    "ETH/EUR": "ETH-EUR",
    "SOL/EUR": "SOL-EUR",
    "BNB/EUR": "BNB-EUR",
    "TON/EUR": None,   # indisponible sur yfinance
}

# ── Mapping xStocks → ticker yfinance (identique à fetcher.py) ────────────────
XSTOCK_YF_MAP = {
    "NVDAx/EUR":  "NVDA",
    "AAPLx/EUR":  "AAPL",
    "MSFTx/EUR":  "MSFT",
    "METAx/EUR":  "META",
    "AMZNx/EUR":  "AMZN",
    "GOOGx/EUR":  "GOOGL",
    "PLTRx/EUR":  "PLTR",
    "AMDx/EUR":   "AMD",
    "AVGOx/EUR":  "AVGO",
    "GLDx/EUR":   "GLD",
    "NFLXx/EUR":  "NFLX",
    "CRWDx/EUR":  "CRWD",
    "TSLAx/EUR":  "TSLA",
    "AMZNx/EUR":  "AMZN",
}


def _get_eurusd_rate() -> float:
    try:
        raw = yf.Ticker("EURUSD=X").history(period="5d", interval="1d")
        if not raw.empty:
            return float(raw["Close"].iloc[-1])
    except Exception:
        pass
    return 1.08


def fetch_yf_ohlcv(ticker: str, to_eur: bool = False, eurusd: float = 1.08) -> pd.DataFrame | None:
    """
    Télécharge l'historique maximum disponible pour un ticker yfinance.
    Retourne un DataFrame daily avec colonnes open/high/low/close/volume.
    """
    try:
        raw = yf.Ticker(ticker).history(period="max", interval="1d")
        if raw.empty:
            return None
        df = raw[["Open", "High", "Low", "Close", "Volume"]].rename(columns=str.lower)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["close"])
        if to_eur:
            for col in ["open", "high", "low", "close"]:
                df[col] = df[col] / eurusd
        return df
    except Exception as e:
        log(f"  {ticker} ERREUR: {e}", Fore.RED)
        return None


def fetch_extended_data():
    """
    Télécharge le maximum de données disponibles pour chaque symbole.
    - xStocks : yfinance period=max (USD→EUR)
    - Crypto  : yfinance BTC-EUR, ETH-EUR... (déjà en EUR)
    - VIX + QQQ : period=max
    """
    log("=" * 65)
    log("  BACKTEST LONG-TERME — MAXIMUM DE DONNÉES DISPONIBLES")
    log("=" * 65)

    eurusd = _get_eurusd_rate()
    log(f"Taux EUR/USD : {eurusd:.4f}")

    daily = {}

    # ── 1. xStocks via yfinance ────────────────────────────────────────────────
    log("\nxStocks (yfinance period=max, USD→EUR) :")
    for sym, ticker in XSTOCK_YF_MAP.items():
        if sym not in config.SYMBOLS:
            continue
        df = fetch_yf_ohlcv(ticker, to_eur=True, eurusd=eurusd)
        if df is not None and len(df) > 200:
            daily[sym] = df
            years = len(df) / 252
            log(f"  {sym} ({ticker}): {len(df)} barres | {df.index[0].date()} → {df.index[-1].date()} ({years:.1f} ans)", Fore.GREEN)
        else:
            log(f"  {sym} ({ticker}): données insuffisantes", Fore.YELLOW)
        time.sleep(0.3)

    # ── 2. Crypto via yfinance (BTC-EUR, ETH-EUR...) ─────────────────────────
    log("\nCrypto (yfinance period=max, directement en EUR) :")
    for sym, yf_ticker in CRYPTO_YF_MAP.items():
        if sym not in config.SYMBOLS:
            continue
        if yf_ticker is None:
            log(f"  {sym}: non disponible sur yfinance", Fore.YELLOW)
            continue
        df = fetch_yf_ohlcv(yf_ticker, to_eur=False)
        if df is not None and len(df) > 200:
            daily[sym] = df
            years = len(df) / 365
            log(f"  {sym} ({yf_ticker}): {len(df)} barres | {df.index[0].date()} → {df.index[-1].date()} ({years:.1f} ans)", Fore.GREEN)
        else:
            log(f"  {sym} ({yf_ticker}): données insuffisantes", Fore.YELLOW)
        time.sleep(0.3)

    # ── 3. VIX + QQQ (régime) period=max ──────────────────────────────────────
    log("\nVIX + QQQ (régime, period=max) :")
    try:
        vix_raw = yf.Ticker("^VIX").history(period="max", interval="1d")["Close"]
        vix_raw.index = pd.to_datetime(vix_raw.index)
        if vix_raw.index.tz is not None:
            vix_raw.index = vix_raw.index.tz_localize(None)
        log(f"  VIX: {len(vix_raw)} barres | {vix_raw.index[0].date()} → {vix_raw.index[-1].date()}", Fore.GREEN)
    except Exception as e:
        log(f"  VIX ERREUR: {e}", Fore.RED)
        vix_raw = pd.Series(dtype=float)

    try:
        qqq_raw = yf.Ticker("QQQ").history(period="max", interval="1d")[["Close"]]
        qqq_raw.index = pd.to_datetime(qqq_raw.index)
        if qqq_raw.index.tz is not None:
            qqq_raw.index = qqq_raw.index.tz_localize(None)
        qqq_raw["sma200"] = qqq_raw["Close"].rolling(200).mean()
        log(f"  QQQ: {len(qqq_raw)} barres | {qqq_raw.index[0].date()} → {qqq_raw.index[-1].date()}", Fore.GREEN)
    except Exception as e:
        log(f"  QQQ ERREUR: {e}", Fore.RED)
        qqq_raw = pd.DataFrame(columns=["Close", "sma200"])

    # ── 4. BTC EMA200 pour momentum overlay ───────────────────────────────────
    btc_df = daily.get("BTC/EUR")
    if btc_df is not None:
        btc_df = btc_df.copy()
        btc_df["ema200"] = btc_df["close"].ewm(span=200, adjust=False).mean()
        daily["BTC/EUR"] = btc_df

    # ── 5. Rapport couverture ─────────────────────────────────────────────────
    log(f"\nDonnées chargées : {len(daily)}/{len(config.SYMBOLS)} symboles")
    log("Couverture par symbole :")
    for sym, df in sorted(daily.items(), key=lambda x: x[1].index[0]):
        years = (df.index[-1] - df.index[0]).days / 365
        log(f"  {sym:<20} {df.index[0].date()} → {df.index[-1].date()}  ({years:.1f} ans  {len(df)} barres)")

    return daily, vix_raw, qqq_raw


def print_coverage_table(daily: dict):
    """Affiche un tableau de disponibilité des données par symbole et par année."""
    log("\n── Disponibilité des données par symbole ──")
    print(f"\n{'Symbole':<22} {'Début':>12} {'Fin':>12} {'Barres':>8} {'Années':>8}")
    print("-" * 65)
    for sym, df in sorted(daily.items(), key=lambda x: x[1].index[0]):
        years = (df.index[-1] - df.index[0]).days / 365
        print(f"  {sym:<20} {str(df.index[0].date()):>12} {str(df.index[-1].date()):>12} {len(df):>8} {years:>7.1f}")
    print()


def print_results_table(results: dict, z_results: dict):
    """Tableau récapitulatif avec CAGR, Sharpe, MaxDD, Trades, WinRate, Capital final."""
    print(f"\n{Fore.CYAN}{'='*85}{Style.RESET_ALL}")
    print(f"  BACKTEST LONG-TERME — RÉSULTATS (capital initial : {INITIAL:.0f}€ par bot)")
    print(f"{Fore.CYAN}{'='*85}{Style.RESET_ALL}")

    print(f"\n{'Stratégie':<35} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>8} {'WR%':>7} {'Final':>10}")
    print("-" * 85)

    bot_labels = {
        "a": "Bot A — Supertrend+MR",
        "b": "Bot B — Momentum Rotation",
        "c": "Bot C — Donchian Breakout",
        "g": "Bot G — Trend Multi-Asset",
        "h": "Bot H — VCB Breakout",
        "i": "Bot I — RS Leaders",
        "j": "Bot J — Mean Reversion",
    }
    for key, label in bot_labels.items():
        if key not in results:
            continue
        m = results[key]["metrics"]
        date_range = ""
        c = Fore.GREEN if m["cagr"] > 0 else Fore.RED
        print(
            f"  {label:<33} {c}{m['cagr']:>+7.1f}%{Style.RESET_ALL}"
            f" {m['sharpe']:>8.2f} {m['max_dd']:>7.1f}%"
            f" {m['trades']:>8} {m['win_rate']:>6.1f}%"
            f" {m['final']:>9.0f}€"
        )

    print(f"\n{Fore.YELLOW}  ── Bot Z (portefeuilles sur A+B+C+G) ──{Style.RESET_ALL}")
    z_labels = {
        "equal_weight":  "Equal-Weight A+B+C+G",
        "regime_pure":   "Bot Z Régime pur",
        "enhanced":      "Bot Z Enhanced",
        "omega":         "Bot Z Omega",
        "omega_v2":      "Bot Z Omega v2",
        "meta_v2":       "Bot Z Meta v2 (PROD)",
    }
    for key, label in z_labels.items():
        z = z_results.get(key)
        if z is None:
            continue
        m = compute_metrics(z.get("trades", []), z.get("equity", []), initial=INITIAL * 4)
        c = Fore.GREEN if m["cagr"] > 0 else Fore.RED
        print(
            f"  {label:<33} {c}{m['cagr']:>+7.1f}%{Style.RESET_ALL}"
            f" {m['sharpe']:>8.2f} {m['max_dd']:>7.1f}%"
            f" {m['trades']:>8} {m['win_rate']:>6.1f}%"
            f" {m['final']:>9.0f}€"
        )
    print(f"{Fore.CYAN}{'='*85}{Style.RESET_ALL}\n")


def print_annual_table(results: dict, z_results: dict):
    """Table des rendements annuels par bot et par Bot Z."""
    all_years = set()
    for r in results.values():
        all_years.update(r.get("annual", {}).keys())
    for z in z_results.values():
        if z and z.get("dates"):
            dates = pd.DatetimeIndex(z["dates"])
            all_years.update(dates.year.unique().tolist())
    if not all_years:
        return

    years = sorted(all_years)
    header = f"{'Bot':<20}" + "".join(f"{y:>9}" for y in years)
    print(f"\n{Fore.CYAN}── Rendements annuels ──{Style.RESET_ALL}")
    print(header)
    print("-" * (20 + 9 * len(years)))

    def fmt(val):
        if val is None:
            return f"{'—':>9}"
        c = Fore.GREEN if val > 0 else Fore.RED if val < 0 else Fore.WHITE
        return f"{c}{val:>+8.1f}%{Style.RESET_ALL}"

    labels = {
        "a": "A-Supertrend", "b": "B-Momentum",
        "c": "C-Breakout",   "g": "G-Trend",
        "j": "J-MeanRev",
    }
    for key, label in labels.items():
        if key not in results:
            continue
        ann = results[key].get("annual", {})
        row = f"  {label:<18}" + "".join(fmt(ann.get(y)) for y in years)
        print(row)

    # Bot Z Meta v2
    z = z_results.get("meta_v2")
    if z and z.get("dates") and z.get("equity"):
        ann_z = annual_returns(z["equity"], z["dates"])
        row = f"  {'Z-Meta v2':<18}" + "".join(fmt(ann_z.get(y)) for y in years)
        print(f"{Fore.YELLOW}{row}{Style.RESET_ALL}")

    print()


def save_csv(results: dict, z_results: dict):
    """Sauvegarde résumé CSV."""
    rows = []
    for key, r in results.items():
        m = r["metrics"]
        df_sym = None
        rows.append({
            "bot": key.upper(),
            "strategy": r.get("name", key),
            "cagr_pct": m["cagr"],
            "sharpe": m["sharpe"],
            "max_dd_pct": m["max_dd"],
            "trades": m["trades"],
            "win_rate_pct": m["win_rate"],
            "final_eur": m["final"],
            "profit_factor": m["profit_factor"],
        })
    pd.DataFrame(rows).to_csv(f"{RESULTS_DIR}/longterm_summary.csv", index=False)
    log(f"Sauvegardé : {RESULTS_DIR}/longterm_summary.csv", Fore.GREEN)

    # Annual returns
    all_years = set()
    for r in results.values():
        all_years.update(r.get("annual", {}).keys())
    years = sorted(all_years)
    ann_rows = []
    for key, r in results.items():
        row = {"bot": key.upper()}
        for y in years:
            row[str(y)] = r.get("annual", {}).get(y)
        ann_rows.append(row)
    pd.DataFrame(ann_rows).to_csv(f"{RESULTS_DIR}/longterm_annual.csv", index=False)
    log(f"Sauvegardé : {RESULTS_DIR}/longterm_annual.csv", Fore.GREEN)


def plot_equity(results: dict, z_results: dict, daily: dict):
    """Graphique equity curves long-terme."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        fig, axes = plt.subplots(2, 1, figsize=(18, 12))
        fig.suptitle("Backtest Long-Terme — Maximum de données disponibles", fontsize=14, fontweight="bold")

        # ── Panel 1 : bots individuels ───────────────────────────────────────
        ax1 = axes[0]
        colors = {"a": "#2ecc71", "b": "#3498db", "c": "#e74c3c", "g": "#f39c12", "j": "#9b59b6"}
        for key, r in results.items():
            if not r.get("dates") or not r.get("equity"):
                continue
            dates = pd.DatetimeIndex(r["dates"])
            eq = np.array(r["equity"])
            m = r["metrics"]
            label = f"{r['name']} — CAGR {m['cagr']:+.1f}% | Sharpe {m['sharpe']:.2f} | MaxDD {m['max_dd']:.1f}%"
            ax1.plot(dates, eq, label=label, color=colors.get(key, "#95a5a6"), linewidth=1.2, alpha=0.85)

        ax1.set_title("Performance individuelle des bots (1 000€ initial chacun)")
        ax1.set_ylabel("Capital (€)")
        ax1.legend(fontsize=7, loc="upper left")
        ax1.grid(alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax1.xaxis.set_major_locator(mdates.YearLocator(2))

        # ── Panel 2 : Bot Z Meta v2 vs benchmarks ────────────────────────────
        ax2 = axes[1]
        z_colors = {
            "equal_weight": "#95a5a6",
            "enhanced":     "#2ecc71",
            "omega":        "#3498db",
            "meta_v2":      "#e74c3c",
        }
        z_labels_plot = {
            "equal_weight": "Equal-Weight A+B+C+G",
            "enhanced":     "Bot Z Enhanced",
            "omega":        "Bot Z Omega",
            "meta_v2":      "Bot Z Meta v2 (PROD)",
        }
        for key, label in z_labels_plot.items():
            z = z_results.get(key)
            if z is None or not z.get("dates") or not z.get("equity"):
                continue
            dates = pd.DatetimeIndex(z["dates"])
            eq = np.array(z["equity"])
            m = compute_metrics(z.get("trades", []), z.get("equity", []), initial=INITIAL * 4)
            lw = 2.5 if key == "meta_v2" else 1.2
            ax2.plot(dates, eq, label=f"{label} — CAGR {m['cagr']:+.1f}% | Sharpe {m['sharpe']:.2f} | MaxDD {m['max_dd']:.1f}%",
                     color=z_colors.get(key, "#7f8c8d"), linewidth=lw, alpha=0.9)

        ax2.set_title("Bot Z Portfolio (4 000€ initial — A+B+C+G)")
        ax2.set_ylabel("Capital (€)")
        ax2.legend(fontsize=8, loc="upper left")
        ax2.grid(alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax2.xaxis.set_major_locator(mdates.YearLocator(2))

        plt.tight_layout()
        out = f"{RESULTS_DIR}/longterm_equity.png"
        plt.savefig(out, dpi=150, bbox_inches="tight")
        plt.close()
        log(f"Graphique sauvegardé : {out}", Fore.GREEN)

    except Exception as e:
        log(f"Graphique ignoré: {e}", Fore.YELLOW)


def main():
    t0 = time.time()

    # ── 1. Données max ────────────────────────────────────────────────────────
    daily, vix_s, qqq_df = fetch_extended_data()

    if not daily:
        log("Aucune donnée disponible — abandon.", Fore.RED)
        return

    print_coverage_table(daily)

    # ── 2. Bots individuels ───────────────────────────────────────────────────
    log("Exécution des stratégies individuelles...")
    bot_funcs = {
        "a": backtest_bot_a,
        "b": backtest_bot_b,
        "c": backtest_bot_c,
        "g": backtest_bot_g,
        "j": backtest_bot_j_mean_reversion,
    }

    results = {}
    for key, fn in bot_funcs.items():
        try:
            log(f"  Bot {key.upper()}...")
            r = fn(daily)
            r["metrics"] = compute_metrics(r["trades"], r["equity"])
            r["annual"]  = annual_returns(r["equity"], r["dates"]) if r.get("dates") else {}
            r["regime"]  = regime_returns(r["trades"], vix_s, qqq_df)
            results[key] = r
            m = r["metrics"]
            start = pd.DatetimeIndex(r["dates"])[0].date() if r.get("dates") else "?"
            log(f"    {r['name']}: CAGR={m['cagr']:+.1f}% | Sharpe={m['sharpe']:.2f} | "
                f"MaxDD={m['max_dd']:.1f}% | Trades={m['trades']} | Début={start}", Fore.GREEN)
        except Exception as e:
            import traceback
            log(f"  Bot {key} ERREUR: {e}", Fore.RED)
            traceback.print_exc()

    # ── 3. Bot Z portfolio ────────────────────────────────────────────────────
    z_results = {}
    if results:
        log("\nSimulation Bot Z portfolio (A+B+C+G)...")
        try:
            z_results = backtest_bot_z_portfolio(results, vix_s, qqq_df)
            log("  Equal-weight + Régime pur calculés", Fore.GREEN)
        except Exception as e:
            log(f"  Bot Z portfolio ERREUR: {e}", Fore.RED)

        try:
            z_enhanced = backtest_bot_z_enhanced(results, vix_s, qqq_df, daily)
            if z_enhanced:
                z_results["enhanced"] = z_enhanced
            log("  Bot Z Enhanced calculé", Fore.GREEN)
        except Exception as e:
            log(f"  Enhanced ERREUR: {e}", Fore.RED)

        try:
            z_omega = backtest_bot_z_omega(results, vix_s, qqq_df, daily)
            if z_omega:
                z_results["omega"] = z_omega
            log("  Bot Z Omega calculé", Fore.GREEN)
        except Exception as e:
            log(f"  Omega ERREUR: {e}", Fore.RED)

        try:
            z_omega_v2 = backtest_bot_z_omega_v2(results, vix_s, qqq_df, daily)
            if z_omega_v2:
                z_results["omega_v2"] = z_omega_v2
            log("  Bot Z Omega v2 calculé", Fore.GREEN)
        except Exception as e:
            log(f"  Omega v2 ERREUR: {e}", Fore.RED)

        try:
            z_meta_v2 = backtest_bot_z_meta_v2(results, vix_s, qqq_df, daily)
            if z_meta_v2:
                z_results["meta_v2"] = z_meta_v2
            log("  Bot Z Meta v2 calculé", Fore.GREEN)
        except Exception as e:
            log(f"  Meta v2 ERREUR: {e}", Fore.RED)

    # ── 4. Affichage résultats ────────────────────────────────────────────────
    print_results_table(results, z_results)
    print_annual_table(results, z_results)

    # ── 5. Benchmarks de référence ────────────────────────────────────────────
    log("Téléchargement benchmarks de référence (SP500, NASDAQ, BTC)...")
    print(f"\n{Fore.CYAN}── Benchmarks (period=max, dividendes inclus) ──{Style.RESET_ALL}")
    for ticker, label in [("^GSPC", "S&P 500"), ("QQQ", "NASDAQ-100"), ("BTC-EUR", "BTC/EUR (buy&hold)")]:
        try:
            raw = yf.Ticker(ticker).history(period="max", interval="1d")["Close"]
            raw.index = pd.to_datetime(raw.index)
            if raw.index.tz is not None:
                raw.index = raw.index.tz_localize(None)
            raw = raw.dropna()
            years = (raw.index[-1] - raw.index[0]).days / 365
            total_ret = (raw.iloc[-1] / raw.iloc[0] - 1) * 100
            cagr = ((raw.iloc[-1] / raw.iloc[0]) ** (1 / years) - 1) * 100

            ret = raw.pct_change().dropna()
            sharpe = float(ret.mean() / ret.std() * math.sqrt(252)) if ret.std() > 0 else 0
            peak = raw.expanding().max()
            dd = ((raw - peak) / peak * 100).min()

            print(f"  {label:<22} CAGR {cagr:>+6.1f}% | Sharpe {sharpe:>5.2f} | MaxDD {dd:>7.1f}% "
                  f"| {raw.index[0].date()} → {raw.index[-1].date()} ({years:.0f} ans)")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {label}: erreur ({e})")

    # ── 6. Sauvegarde ─────────────────────────────────────────────────────────
    save_csv(results, z_results)
    plot_equity(results, z_results, daily)

    elapsed = time.time() - t0
    log(f"\nTerminé en {elapsed:.1f}s", Fore.GREEN)


if __name__ == "__main__":
    main()

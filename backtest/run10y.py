#!/usr/bin/env python3
"""Backtest 10 ans (2016-2026) — script standalone."""
import os, sys, time, math, warnings
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from backtest.multi_backtest import (
    compute_metrics, annual_returns, log,
    backtest_bot_a, backtest_bot_b, backtest_bot_c, backtest_bot_g,
    backtest_bot_j_mean_reversion,
    backtest_bot_z_portfolio, backtest_bot_z_enhanced,
    backtest_bot_z_omega, backtest_bot_z_omega_v2, backtest_bot_z_meta_v2,
    INITIAL,
)
from colorama import Fore, Style, init
init(autoreset=True)

START = "2016-01-01"
EURUSD = 1.1608
RESULTS = "backtest/results"
os.makedirs(RESULTS, exist_ok=True)

XSTOCK_MAP = {
    "NVDAx/EUR":"NVDA","AAPLx/EUR":"AAPL","MSFTx/EUR":"MSFT",
    "METAx/EUR":"META","GOOGx/EUR":"GOOGL","PLTRx/EUR":"PLTR",
    "AMDx/EUR":"AMD","AVGOx/EUR":"AVGO","GLDx/EUR":"GLD",
    "NFLXx/EUR":"NFLX","CRWDx/EUR":"CRWD",
}
CRYPTO_MAP = {
    "BTC/EUR":"BTC-EUR","ETH/EUR":"ETH-EUR",
    "SOL/EUR":"SOL-EUR","BNB/EUR":"BNB-EUR",
}

def fetch(ticker, to_eur=False):
    try:
        raw = yf.Ticker(ticker).history(period="10y", interval="1d")
        if raw.empty:
            return None
        df = raw[["Open","High","Low","Close","Volume"]].rename(columns=str.lower)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[df.index >= START].dropna(subset=["close"])
        if to_eur:
            for c in ["open","high","low","close"]:
                df[c] = df[c] / EURUSD
        return df if len(df) > 200 else None
    except Exception as e:
        print(f"  ERREUR {ticker}: {e}")
        return None

# ── Données ───────────────────────────────────────────────────────────────────
print(f"\n{Fore.CYAN}{'='*65}")
print(f"  BACKTEST 10 ANS — {START} → 2026")
print(f"{'='*65}{Style.RESET_ALL}\n")

daily = {}
for sym, tk in XSTOCK_MAP.items():
    if sym not in config.SYMBOLS:
        continue
    df = fetch(tk, to_eur=True)
    if df is not None:
        daily[sym] = df
        print(f"  {sym:<20} {df.index[0].date()} → {df.index[-1].date()}  {len(df)} barres")
    time.sleep(0.3)

for sym, tk in CRYPTO_MAP.items():
    if sym not in config.SYMBOLS:
        continue
    df = fetch(tk)
    if df is not None:
        daily[sym] = df
        print(f"  {sym:<20} {df.index[0].date()} → {df.index[-1].date()}  {len(df)} barres")
    time.sleep(0.3)

if "BTC/EUR" in daily:
    daily["BTC/EUR"]["ema200"] = daily["BTC/EUR"]["close"].ewm(span=200).mean()

vix_raw = yf.Ticker("^VIX").history(period="10y", interval="1d")["Close"]
vix_raw.index = pd.to_datetime(vix_raw.index)
if vix_raw.index.tz is not None:
    vix_raw.index = vix_raw.index.tz_localize(None)
vix_raw = vix_raw[vix_raw.index >= START]

qqq_raw = yf.Ticker("QQQ").history(period="10y", interval="1d")[["Close"]]
qqq_raw.index = pd.to_datetime(qqq_raw.index)
if qqq_raw.index.tz is not None:
    qqq_raw.index = qqq_raw.index.tz_localize(None)
qqq_raw = qqq_raw[qqq_raw.index >= START]
qqq_raw["sma200"] = qqq_raw["Close"].rolling(200).mean()

print(f"\n{len(daily)}/{len(config.SYMBOLS)} symboles | VIX: {len(vix_raw)} | QQQ: {len(qqq_raw)}\n")

# ── Bots individuels ──────────────────────────────────────────────────────────
results = {}
BOT_FUNCS = {
    "a": backtest_bot_a, "b": backtest_bot_b,
    "c": backtest_bot_c, "g": backtest_bot_g,
    "j": backtest_bot_j_mean_reversion,
}
BOT_NAMES = {
    "a":"Bot A — Supertrend+MR", "b":"Bot B — Momentum",
    "c":"Bot C — Breakout",      "g":"Bot G — Trend Multi-Asset",
    "j":"Bot J — Mean Reversion",
}

for key, fn in BOT_FUNCS.items():
    try:
        r = fn(daily)
        r["metrics"] = compute_metrics(r["trades"], r["equity"])
        r["annual"]  = annual_returns(r["equity"], r["dates"]) if r.get("dates") else {}
        results[key] = r
    except Exception as e:
        import traceback
        print(f"Bot {key} ERREUR: {e}")
        traceback.print_exc()

# ── Bot Z ─────────────────────────────────────────────────────────────────────
z_results = {}
if results:
    try:
        zp = backtest_bot_z_portfolio(results, vix_raw, qqq_raw)
        # Extraire equal_weight et regime_pure du résultat de portfolio
        if zp.get("equal"):
            z_results["equal_weight"] = zp["equal"]
        if zp.get("z"):
            z_results["regime_pure"] = zp["z"]
    except Exception as e:
        print(f"Z portfolio: {e}")
    for label, fn in [
        ("enhanced", backtest_bot_z_enhanced),
        ("omega",    backtest_bot_z_omega),
        ("omega_v2", backtest_bot_z_omega_v2),
        ("meta_v2",  backtest_bot_z_meta_v2),
    ]:
        try:
            z = fn(results, vix_raw, qqq_raw, daily)
            if z and z.get("equity"):
                z_results[label] = z
        except Exception as e:
            print(f"Z {label}: {e}")
    # ── Variantes test levier ──────────────────────────────────────────────
    # A : BULL hysteresis 1 semaine (au lieu de 2) → BULL s'active plus vite
    try:
        z = backtest_bot_z_meta_v2(results, vix_raw, qqq_raw, daily,
                                   cfg={"bull_hyst": 1, "lev_engines": {"BULL"}})
        if z and z.get("equity"):
            z_results["meta_v2_bull1w"] = z
    except Exception as e:
        print(f"Z meta_v2_bull1w: {e}")
    # B : levier sur BULL + BALANCED (vol targeting plus large)
    try:
        z = backtest_bot_z_meta_v2(results, vix_raw, qqq_raw, daily,
                                   cfg={"lev_engines": {"BULL", "BALANCED"}})
        if z and z.get("equity"):
            z_results["meta_v2_lev_bal"] = z
    except Exception as e:
        print(f"Z meta_v2_lev_bal: {e}")

# ── Affichage ─────────────────────────────────────────────────────────────────
print(f"\n{Fore.CYAN}{'='*90}{Style.RESET_ALL}")
print(f"  RÉSULTATS — BACKTEST 10 ANS ({START} → 2026) | Capital initial : {INITIAL:.0f}€")
print(f"{Fore.CYAN}{'='*90}{Style.RESET_ALL}")
print(f"{'Stratégie':<36} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>8} {'WR%':>7} {'Final':>10} {'PF':>6}")
print("-" * 90)

for key, name in BOT_NAMES.items():
    if key not in results:
        continue
    m = results[key]["metrics"]
    c = Fore.GREEN if m["cagr"] > 0 else Fore.RED
    print(f"  {name:<34} {c}{m['cagr']:>+7.1f}%{Style.RESET_ALL}"
          f" {m['sharpe']:>8.2f} {m['max_dd']:>7.1f}%"
          f" {m['trades']:>8} {m['win_rate']:>6.1f}%"
          f" {m['final']:>9.0f}€ {m['profit_factor']:>5.2f}")

print(f"\n{Fore.YELLOW}  ── Bot Z (A+B+C+G, 4 000€ initial) ──{Style.RESET_ALL}")
Z_LABELS = {
    "equal_weight":    "Equal-Weight A+B+C+G",
    "regime_pure":     "Bot Z Régime pur",
    "enhanced":        "Bot Z v1 — MO+CB",
    "omega":           "Bot Z v2 — QualityScore",
    "omega_v2":        "Bot Z v3 — RiskParity",
    "meta_v2":         "Bot Z PROD — Meta v2",
    "meta_v2_bull1w":  "Test A — BULL hyst 1w + lev×1.3",
    "meta_v2_lev_bal": "Test B — lev×1.3 BULL+BALANCED",
}
for key, name in Z_LABELS.items():
    z = z_results.get(key)
    if z is None or not z.get("equity"):
        continue
    # Utiliser les métriques pré-calculées par _metrics_portfolio (CAGR correct sur dates réelles)
    m = z.get("metrics") or {}
    if not m:
        continue
    c = Fore.GREEN if m.get("cagr", 0) > 0 else Fore.RED
    print(f"  {name:<34} {c}{m.get('cagr', 0):>+7.1f}%{Style.RESET_ALL}"
          f" {m.get('sharpe', 0):>8.2f} {m.get('max_dd', 0):>7.1f}%"
          f" {m.get('trades', 0):>8} {m.get('win_rate', 0):>6.1f}%"
          f" {m.get('final', 0):>9.0f}€")

print(f"{Fore.CYAN}{'='*90}{Style.RESET_ALL}")

# ── Rendements annuels ────────────────────────────────────────────────────────
all_years = set()
for r in results.values():
    all_years.update(r.get("annual", {}).keys())
years = sorted(all_years)

print(f"\n{Fore.CYAN}── Rendements annuels ──{Style.RESET_ALL}")
header = f"  {'Bot':<22}" + "".join(f"{y:>9}" for y in years)
print(header)
print("-" * (24 + 9 * len(years)))

def fmt(v):
    if v is None:
        return f"{'—':>9}"
    c = Fore.GREEN if v > 0 else Fore.RED if v < 0 else ""
    return f"{c}{v:>+8.1f}%{Style.RESET_ALL}"

for key, name in BOT_NAMES.items():
    if key not in results:
        continue
    ann = results[key].get("annual", {})
    print(f"  {name:<22}" + "".join(fmt(ann.get(y)) for y in years))

# Bot Z Meta v2
zmv2 = z_results.get("meta_v2")
if zmv2 and zmv2.get("dates") and zmv2.get("equity"):
    ann_z = annual_returns(zmv2["equity"], zmv2["dates"])
    row = f"  {'Z Meta v2':<22}" + "".join(fmt(ann_z.get(y)) for y in years)
    print(f"{Fore.YELLOW}{row}{Style.RESET_ALL}")

# ── Benchmarks ────────────────────────────────────────────────────────────────
print(f"\n{Fore.CYAN}── Benchmarks (2016 → 2026) ──{Style.RESET_ALL}")
for tk, label in [("^GSPC","S&P 500"), ("QQQ","NASDAQ-100"), ("BTC-EUR","BTC/EUR buy&hold")]:
    try:
        raw = yf.Ticker(tk).history(period="10y", interval="1d")["Close"]
        raw.index = pd.to_datetime(raw.index)
        if raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)
        raw = raw[raw.index >= START].dropna()
        years_n = (raw.index[-1] - raw.index[0]).days / 365
        cagr = ((raw.iloc[-1] / raw.iloc[0]) ** (1 / years_n) - 1) * 100
        ret = raw.pct_change().dropna()
        sharpe = float(ret.mean() / ret.std() * math.sqrt(252)) if ret.std() > 0 else 0
        dd = ((raw - raw.expanding().max()) / raw.expanding().max() * 100).min()
        print(f"  {label:<22} CAGR {cagr:>+6.1f}% | Sharpe {sharpe:>5.2f} | MaxDD {dd:>7.1f}%")
        time.sleep(0.3)
    except Exception as e:
        print(f"  {label}: {e}")

# ── CSV ───────────────────────────────────────────────────────────────────────
rows = []
for key in BOT_FUNCS:
    if key not in results:
        continue
    m = results[key]["metrics"]
    rows.append({"bot": key.upper(), "strategy": BOT_NAMES[key],
                 "period": "2016-2026", "cagr_pct": m["cagr"],
                 "sharpe": m["sharpe"], "max_dd_pct": m["max_dd"],
                 "trades": m["trades"], "win_rate_pct": m["win_rate"],
                 "final_eur": m["final"], "profit_factor": m["profit_factor"]})
for key, name in Z_LABELS.items():
    z = z_results.get(key)
    if z and z.get("equity") and z.get("metrics"):
        m = z["metrics"]
        rows.append({"bot": f"Z-{key.upper()}", "strategy": name,
                     "period": "2016-2026", "cagr_pct": m.get("cagr", 0),
                     "sharpe": m.get("sharpe", 0), "max_dd_pct": m.get("max_dd", 0),
                     "trades": 0, "win_rate_pct": 0,
                     "final_eur": m.get("final", 0), "profit_factor": 0})
pd.DataFrame(rows).to_csv(f"{RESULTS}/run10y_summary.csv", index=False)
print(f"\nSauvegardé : {RESULTS}/run10y_summary.csv")

# ── Graphique ─────────────────────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt, matplotlib.dates as mdates
    import numpy as np

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10))
    fig.suptitle(f"Backtest 10 ans — {START} → 2026", fontsize=13, fontweight="bold")

    colors_bot = {"a":"#2ecc71","b":"#3498db","c":"#e74c3c","g":"#f39c12","j":"#9b59b6"}
    for key, name in BOT_NAMES.items():
        r = results.get(key)
        if not r or not r.get("dates"): continue
        m = r["metrics"]
        ax1.plot(pd.DatetimeIndex(r["dates"]), r["equity"],
                 label=f"{name} | CAGR {m['cagr']:+.1f}% | Sharpe {m['sharpe']:.2f} | MaxDD {m['max_dd']:.1f}%",
                 color=colors_bot.get(key,"#7f8c8d"), linewidth=1.3, alpha=0.9)
    ax1.set_title("Bots individuels (1 000€)")
    ax1.set_ylabel("Capital (€)")
    ax1.legend(fontsize=7, loc="upper left")
    ax1.grid(alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    colors_z = {"equal_weight":"#95a5a6","enhanced":"#27ae60","omega":"#2980b9","omega_v2":"#8e44ad","meta_v2":"#e74c3c"}
    for key, name in Z_LABELS.items():
        z = z_results.get(key)
        if not z or not z.get("equity") or not z.get("dates"): continue
        m = z.get("metrics") or {}
        lw = 2.5 if key == "meta_v2" else 1.2
        ax2.plot(pd.DatetimeIndex(z["dates"]), z["equity"],
                 label=f"{name} | CAGR {m.get('cagr',0):+.1f}% | Sharpe {m.get('sharpe',0):.2f} | MaxDD {m.get('max_dd',0):.1f}%",
                 color=colors_z.get(key,"#7f8c8d"), linewidth=lw, alpha=0.9)
    ax2.set_title("Bot Z Portfolio (4 000€ — A+B+C+G)")
    ax2.set_ylabel("Capital (€)")
    ax2.legend(fontsize=7, loc="upper left")
    ax2.grid(alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    plt.tight_layout()
    out = f"{RESULTS}/run10y_equity.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Graphique : {out}")
except Exception as e:
    print(f"Graphique ignoré: {e}")

print("\nTerminé.")

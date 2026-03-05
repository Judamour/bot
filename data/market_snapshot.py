"""
Shared Market Data Hub
Fetches all market data once per cycle and shares it across all strategies.
Avoids redundant API calls when running multiple bots simultaneously.
"""
import time
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import (
    fetch_ohlcv, fetch_fear_greed, fetch_funding_rates,
    fetch_news_macro_rss, fetch_qqq_regime,
)


def fetch_btc_context() -> dict:
    """BTC macro context — EMA200 trend filter. Shared by all bots."""
    try:
        from strategies.supertrend import add_indicators
        df = fetch_ohlcv("BTC/EUR", config.TIMEFRAME, days=45)
        df = add_indicators(df)
        last = df.iloc[-1]
        btc_price = float(last["close"])
        btc_ema200 = float(last["ema200"])
        above = btc_price > btc_ema200
        return {
            "btc_price": round(btc_price, 2),
            "btc_above_ema200": above,
            "btc_trend": "bull" if above else "bear",
        }
    except Exception as e:
        print(f"[SNAPSHOT] BTC context unavailable: {e}")
        return {}


def fetch_vix_value() -> float:
    """Current VIX value. Returns 0.0 if unavailable."""
    try:
        import yfinance as yf
        df = yf.Ticker("^VIX").history(period="2d", interval="1h")
        if not df.empty:
            return round(float(df["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return 0.0


def fetch_macro_context() -> dict:
    """
    Fetch all shared macro data once per cycle.

    Returns dict with:
      btc_context, vix, vix_factor, fear_greed,
      funding_rates, macro_news, qqq_regime_ok, qqq_description
    """
    btc_context = fetch_btc_context()

    vix = fetch_vix_value()
    # Scaling linéaire : VIX 15 → ×1.0, VIX 25 → ×0.625, VIX 35+ → ×0.25
    vix_factor = round(max(0.25, 1.0 - max(0.0, vix - 15) * 0.0375), 2) if vix > 0 else 1.0

    fear_greed = fetch_fear_greed()
    funding_rates = fetch_funding_rates(config.CRYPTO)
    macro_news = fetch_news_macro_rss(limit=4)
    qqq_regime_ok, qqq_description = fetch_qqq_regime()

    return {
        "btc_context": btc_context,
        "vix": vix,
        "vix_factor": vix_factor,
        "fear_greed": fear_greed,
        "funding_rates": funding_rates,
        "macro_news": macro_news,
        "qqq_regime_ok": qqq_regime_ok,
        "qqq_description": qqq_description,
    }


def fetch_ohlcv_cache(
    symbols: list,
    timeframe: str = config.TIMEFRAME,
    days: int = 45,
    sleep_between: float = 0.5,
) -> dict:
    """
    Pre-fetch OHLCV for all symbols.
    Returns {symbol: DataFrame} — missing symbols are simply absent.
    """
    cache = {}
    for symbol in symbols:
        try:
            df = fetch_ohlcv(symbol, timeframe, days)
            if df is not None and len(df) > 10:
                cache[symbol] = df
        except Exception as e:
            print(f"[SNAPSHOT] {symbol} fetch failed ({timeframe}): {e}")
        time.sleep(sleep_between)
    return cache

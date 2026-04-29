import ccxt
import pandas as pd
from datetime import datetime, timedelta, timezone
import time
import sys
import os
import logging

logger = logging.getLogger(__name__)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _check_data_freshness(df: pd.DataFrame, timeframe: str) -> tuple:
    """
    Check if OHLCV data is fresh enough for the given timeframe.
    Returns (is_fresh: bool, age_hours: float).
    For 4h timeframe: last candle should be within 5 hours of now.
    For 1d timeframe: last candle should be within 2 days of now.
    """
    if df is None or df.empty:
        return False, float("inf")

    now = datetime.now(timezone.utc)
    last_ts = df.index[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")

    age = now - last_ts
    age_hours = age.total_seconds() / 3600

    max_age_hours = {"4h": 5.0, "1d": 48.0}.get(timeframe, 8.0)
    is_fresh = age_hours <= max_age_hours

    return is_fresh, round(age_hours, 1)


def _is_xstock(symbol: str) -> bool:
    """True si le symbole est une action US (données via yfinance)."""
    return symbol in config.XSTOCKS


def _xstock_ticker(symbol: str) -> str:
    """Convertit 'NVDAx/EUR' → 'NVDA' (format yfinance/Alpaca, sans le suffixe x Kraken)."""
    base = symbol.split("/")[0]
    return base[:-1] if base.endswith("x") else base


def get_exchange(use_auth: bool = False) -> ccxt.kraken:
    """Initialise la connexion à Kraken (pour les ordres live)."""
    params = {"enableRateLimit": True}
    if use_auth:
        params["apiKey"] = config.API_KEY
        params["secret"] = config.API_SECRET
    return ccxt.kraken(params)


def _get_eurusd_rate() -> float:
    """Taux EUR/USD actuel via yfinance (fallback 1.08 si indisponible)."""
    try:
        import yfinance as yf
        df = yf.Ticker("EURUSD=X").history(period="1d", interval="5m")
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    import logging
    logging.warning("[fetcher] Taux EUR/USD indisponible — fallback 1.08 appliqué (sizing xStocks approximatif)")
    return 1.08


def fetch_yfinance_ohlcv(
    symbol: str,
    timeframe: str = config.TIMEFRAME,
    days: int = config.BACKTEST_DAYS,
) -> pd.DataFrame:
    """
    OHLCV depuis yfinance pour les xStocks (NYSE/NASDAQ).
    Prix USD convertis en EUR. Données 1h resamplées en 4h pour ≤60 jours,
    sinon données journalières.
    """
    import yfinance as yf

    ticker_sym = _xstock_ticker(symbol)  # "NVDAx/EUR" → "NVDA"
    end = datetime.utcnow()
    start = end - timedelta(days=days)

    # yfinance : 1h max ~730j, sinon utiliser 1d
    interval = "1h" if days <= 60 else "1d"
    print(f"  Téléchargement {ticker_sym} [{interval}→{timeframe}] — {days} jours (via yfinance)...")

    raw = yf.Ticker(ticker_sym).history(start=start, end=end, interval=interval)
    if raw.empty:
        raise ValueError(f"Aucune donnée yfinance pour {ticker_sym}")

    df = raw[["Open", "High", "Low", "Close", "Volume"]].rename(columns=str.lower)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Resample 1h → 4h si nécessaire
    if interval == "1h" and timeframe == "4h":
        df = df.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()

    # Conversion USD → EUR
    eurusd = _get_eurusd_rate()
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col] / eurusd

    print(f"  ✓ {len(df)} bougies {ticker_sym} USD→EUR @{eurusd:.4f} ({df.index[0].date()} → {df.index[-1].date()})")

    is_fresh, age_hours = _check_data_freshness(df, timeframe)
    if not is_fresh:
        logger.warning(f"[fetcher] STALE DATA: {symbol} last candle is {age_hours}h old (timeframe={timeframe})")
    df.attrs["is_fresh"] = is_fresh
    df.attrs["age_hours"] = age_hours

    return df


def _get_binance_symbol(symbol: str) -> str:
    """Convertit un symbole Kraken en symbole Binance. Ex: BTC/EUR → BTC/USDT."""
    base = symbol.split("/")[0]
    return f"{base}/USDT"


def fetch_ohlcv(
    symbol: str,
    timeframe: str = config.TIMEFRAME,
    days: int = config.BACKTEST_DAYS,
) -> pd.DataFrame:
    """
    Télécharge les données OHLCV.
    - xStocks → yfinance NYSE/NASDAQ (USD converti en EUR)
    - Crypto   → Binance (API publique, converti en USDT)

    Args:
        symbol: Ex: "BTC/EUR" ou "NVDAx/EUR"
        timeframe: Ex: "4h"
        days: Nombre de jours d'historique

    Returns:
        DataFrame avec colonnes: open, high, low, close, volume
    """
    if _is_xstock(symbol):
        return fetch_yfinance_ohlcv(symbol, timeframe, days)

    # Binance pour les cryptos (API publique, pas de clé nécessaire)
    exchange = ccxt.binance({"enableRateLimit": True})
    binance_symbol = _get_binance_symbol(symbol)

    since = exchange.parse8601(
        (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    all_candles = []
    print(f"  Téléchargement {binance_symbol} [{timeframe}] — {days} jours (via Binance)...")
    now = exchange.milliseconds()

    while since < now:
        try:
            candles = exchange.fetch_ohlcv(binance_symbol, timeframe, since=since, limit=1000)
        except ccxt.RateLimitExceeded:
            time.sleep(5)
            continue
        except Exception as e:
            print(f"  Erreur: {e}")
            break

        if not candles:
            break

        all_candles.extend(candles)
        last_ts = candles[-1][0]

        if last_ts >= now - _timeframe_to_ms(timeframe):
            break

        since = last_ts + _timeframe_to_ms(timeframe)
        time.sleep(exchange.rateLimit / 1000)

    if not all_candles:
        raise ValueError(f"Aucune donnée reçue pour {binance_symbol}")

    df = pd.DataFrame(
        all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    print(f"  ✓ {len(df)} bougies récupérées ({df.index[0].date()} → {df.index[-1].date()})")

    is_fresh, age_hours = _check_data_freshness(df, timeframe)
    if not is_fresh:
        logger.warning(f"[fetcher] STALE DATA: {symbol} last candle is {age_hours}h old (timeframe={timeframe})")
    df.attrs["is_fresh"] = is_fresh
    df.attrs["age_hours"] = age_hours

    return df


def _timeframe_to_ms(timeframe: str) -> int:
    """Convertit un timeframe en millisecondes."""
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(timeframe[:-1]) * units[timeframe[-1]]


def fetch_news_yfinance(ticker: str, limit: int = 4, hours: int = 48) -> list:
    """
    Fetch les dernières news via yfinance (Yahoo Finance) — aucune clé requise.
    ticker : symbole Yahoo ex: "NVDA", "^GSPC" (S&P), "^NDX" (Nasdaq), "BTC-USD"
    Retourne [{"title": str, "source": str, "age_h": float}]
    """
    try:
        import yfinance as yf
        from datetime import timezone

        now = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
        cutoff = now - hours * 3600
        raw = yf.Ticker(ticker).news or []
        result = []
        for n in raw:
            pub = n.get("providerPublishTime", 0)
            if pub < cutoff:
                continue
            result.append({
                "title": n.get("title", ""),
                "source": n.get("publisher", ""),
                "age_h": round((now - pub) / 3600, 1),
            })
            if len(result) >= limit:
                break
        return result
    except Exception:
        return []


def fetch_news_macro_rss(limit: int = 6) -> list:
    """
    Fetch les headlines macro depuis Yahoo Finance RSS (public, aucune clé).
    Combine S&P 500 (^GSPC) + Nasdaq (^NDX) pour une couverture macro + tech.
    Retourne [{"title": str, "source": str}]
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    results = []
    for index in ["%5EGSPC", "%5ENDX"]:   # S&P 500 + Nasdaq 100
        try:
            url = f"https://finance.yahoo.com/rss/2.0/headline?s={index}&region=US&lang=en-US"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                root = ET.fromstring(r.read())
            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                if title and title not in {n["title"] for n in results}:
                    results.append({"title": title, "source": "Yahoo Finance"})
        except Exception:
            continue
    return results[:limit]


def fetch_qqq_regime() -> tuple:
    """
    Régime de marché actions US : QQQ > SMA200 = Risk-ON, sinon Risk-OFF.
    Retourne (ok: bool, description: str).
    Permissif (True) si données indisponibles.
    """
    try:
        import yfinance as yf
        df = yf.Ticker("QQQ").history(period="1y", interval="1d")
        if df.empty or len(df) < 200:
            return True, "N/A (historique insuffisant)"
        price = float(df["Close"].iloc[-1])
        sma200 = float(df["Close"].rolling(200).mean().iloc[-1])
        ok = price > sma200
        pct = (price - sma200) / sma200 * 100
        return ok, f"QQQ {'>' if ok else '<'} SMA200 ({pct:+.1f}%)"
    except Exception as e:
        return True, f"N/A ({e})"


def fetch_fear_greed() -> dict:
    """
    Fetch le Crypto Fear & Greed Index (alternative.me, API publique).
    Retourne {'score': int 0-100, 'label': str}
    Scores : 0-24 Peur extrême | 25-49 Peur | 50-74 Avidité | 75-100 Avidité extrême
    """
    try:
        import requests
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = r.json()["data"][0]
        return {"score": int(data["value"]), "label": data["value_classification"]}
    except Exception:
        return {"score": 50, "label": "Neutral"}


def fetch_funding_rates(symbols: list) -> dict:
    """
    Fetch les taux de financement des futures perpétuels Binance (API publique).
    Retourne {symbol: rate} ex: {"BTC/EUR": 0.0001}  (rate par 8h)
    Interprétation :
      < -0.01% : shorts surexposés (signal haussier contrarian)
       0-0.03% : neutre
       0.03-0.10% : longs surexposés, risque de squeeze
      > 0.10% : danger — liquidation de masse probable
    """
    try:
        import requests
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        data = requests.get(url, timeout=5).json()
        ticker_map = {s.split("/")[0] + "USDT": s for s in symbols if "/" in s}
        return {
            ticker_map[item["symbol"]]: float(item.get("lastFundingRate", 0))
            for item in data
            if item.get("symbol") in ticker_map
        }
    except Exception:
        return {}


_BTC_DOM_CACHE = {"value": None, "ts": 0, "trend_up": True}


def fetch_btc_dominance() -> dict:
    """
    BTC dominance via CoinGecko (gratuit, no key). Cache 1h.
    Returns: {dominance: float (%), trend_up: bool, sma20: float}.
    Si BTC.D en hausse → flux vers BTC, altcoins sous-performent.
    """
    import time as _t
    now = _t.time()
    if _BTC_DOM_CACHE["value"] is not None and now - _BTC_DOM_CACHE["ts"] < 3600:
        return {
            "dominance": _BTC_DOM_CACHE["value"],
            "trend_up": _BTC_DOM_CACHE["trend_up"],
            "sma20": _BTC_DOM_CACHE.get("sma20", _BTC_DOM_CACHE["value"]),
        }
    try:
        import requests
        # 30j de cap historique pour SMA20
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=5,
        ).json()
        current = float(r["data"]["market_cap_percentage"]["btc"])
        # Pas d'historique direct → on stocke et on fait SMA glissante en cache
        history = _BTC_DOM_CACHE.get("history", [])
        history.append(current)
        history = history[-20:]
        sma20 = sum(history) / len(history)
        _BTC_DOM_CACHE.update({
            "value": current,
            "ts": now,
            "trend_up": current > sma20,
            "sma20": sma20,
            "history": history,
        })
        return {"dominance": current, "trend_up": current > sma20, "sma20": sma20}
    except Exception:
        return {"dominance": 50.0, "trend_up": False, "sma20": 50.0}


def save_data(df: pd.DataFrame, symbol: str, timeframe: str) -> str:
    """Sauvegarde les données en CSV."""
    os.makedirs("data/cache", exist_ok=True)
    filename = f"data/cache/{symbol.replace('/', '_')}_{timeframe}.csv"
    df.to_csv(filename)
    return filename


def load_data(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Charge les données depuis le cache CSV si disponible."""
    filename = f"data/cache/{symbol.replace('/', '_')}_{timeframe}.csv"
    if not os.path.exists(filename):
        return None
    df = pd.read_csv(filename, index_col="timestamp", parse_dates=True)
    # Rafraîchir si données de plus de 1h
    age = datetime.now().timestamp() - os.path.getmtime(filename)
    if age > 3600:
        return None
    return df


if __name__ == "__main__":
    print("=== Test de connexion Kraken ===")
    for symbol in config.SYMBOLS:
        df = fetch_ohlcv(symbol, days=30)
        print(df.tail(3))
        print()

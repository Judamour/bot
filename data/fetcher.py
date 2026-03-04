import ccxt
import pandas as pd
from datetime import datetime, timedelta
import time
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _is_xstock(symbol: str) -> bool:
    """True si le symbole est une action US (données via Alpaca ou yfinance)."""
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
    - xStocks → yfinance NYSE/NASDAQ (USD converti en EUR, paper trading)
    - Crypto   → Binance (API publique, converti en USDT)

    Args:
        symbol: Ex: "BTC/EUR" ou "NVDA/EUR"
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
    return df


def _timeframe_to_ms(timeframe: str) -> int:
    """Convertit un timeframe en millisecondes."""
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(timeframe[:-1]) * units[timeframe[-1]]


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

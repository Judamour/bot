import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def compute_supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
                       length: int = 14, multiplier: float = 4.5):
    atr = compute_atr(high, low, close, length)
    hl2 = (high + low) / 2

    upper_base = hl2 + multiplier * atr
    lower_base = hl2 - multiplier * atr

    upper = upper_base.values.copy()
    lower = lower_base.values.copy()
    close_arr = close.values
    direction = np.ones(len(close), dtype=int)
    supertrend = np.zeros(len(close))

    for i in range(1, len(close)):
        lower[i] = lower[i] if lower[i] > lower[i - 1] or close_arr[i - 1] < lower[i - 1] else lower[i - 1]
        upper[i] = upper[i] if upper[i] < upper[i - 1] or close_arr[i - 1] > upper[i - 1] else upper[i - 1]

        if direction[i - 1] == -1 and close_arr[i] > upper[i - 1]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close_arr[i] < lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

        supertrend[i] = lower[i] if direction[i] == 1 else upper[i]

    return pd.Series(supertrend, index=close.index), pd.Series(direction, index=close.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule les indicateurs pour la stratégie Supertrend + Golden Cross.

    Indicateurs :
    - Supertrend (ATR 14, mult 4.5) : signal principal
    - EMA 50 et EMA 200 : filtre de tendance longue (Golden Cross)
    - RSI 14 : filtre d'entrée (évite les zones de surachat)
    - ATR 14 : stop-loss dynamique
    """
    df = df.copy()

    df["supertrend"], df["supertrend_dir"] = compute_supertrend(
        df["high"], df["low"], df["close"], length=14, multiplier=4.5
    )
    df["ema50"] = compute_ema(df["close"], span=50)
    df["ema200"] = compute_ema(df["close"], span=200)
    df["rsi"] = compute_rsi(df["close"], length=14)
    df["atr"] = compute_atr(df["high"], df["low"], df["close"], length=14)

    return df.dropna()


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Génère les signaux avec la stratégie Supertrend + Golden Cross.

    Signal LONG si :
      1. Supertrend passe en haussier (direction change de -1 à 1)
      2. Prix au-dessus de l'EMA 200 (tendance longue confirmée)
      3. EMA 50 > EMA 200 (structure de marché haussière)
      4. RSI < 75 (pas de surachat extrême)

    Signal EXIT si :
      1. Supertrend passe en baissier (direction change de 1 à -1)
    """
    df = add_indicators(df)

    supertrend_up = (df["supertrend_dir"] == 1) & (df["supertrend_dir"].shift(1) == -1)
    supertrend_down = (df["supertrend_dir"] == -1) & (df["supertrend_dir"].shift(1) == 1)

    above_ema200 = df["close"] > df["ema200"]
    bullish_structure = df["ema50"] > df["ema200"]
    not_overbought = df["rsi"] < 75

    df["signal"] = 0
    df.loc[supertrend_up & above_ema200 & bullish_structure & not_overbought, "signal"] = 1
    df.loc[supertrend_down, "signal"] = -1

    return df


def calculate_position_size(capital: float, entry_price: float, atr: float) -> dict:
    """Calcule la taille de position avec gestion du risque."""
    risk_eur = capital * config.RISK_PER_TRADE
    stop_distance = atr * config.ATR_MULTIPLIER
    size = risk_eur / stop_distance

    stop_loss = entry_price - stop_distance
    take_profit = entry_price + (stop_distance * config.TAKE_PROFIT_RATIO)

    return {
        "size": round(size, 6),
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "risk_eur": round(risk_eur, 2),
        "stop_distance": round(stop_distance, 2),
    }


if __name__ == "__main__":
    from data.fetcher import fetch_ohlcv

    print("=== Test Supertrend ===")
    df = fetch_ohlcv("BTC/EUR", days=365)
    df = generate_signals(df)

    signals = df[df["signal"] != 0]
    print(f"Signaux : {len(signals)} ({len(df[df['signal']==1])} achats, {len(df[df['signal']==-1])} ventes)")
    print(signals[["close", "supertrend_dir", "rsi", "signal"]].tail(10))

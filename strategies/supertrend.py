import pandas as pd
import pandas_ta as ta
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule les indicateurs pour la stratégie Supertrend + Golden Cross.

    Indicateurs :
    - Supertrend (ATR 10, mult 3.0) : signal principal
    - EMA 50 et EMA 200 : filtre de tendance longue (Golden Cross)
    - RSI 14 : filtre d'entrée (évite les zones de surachat)
    - ATR 14 : stop-loss dynamique
    """
    df = df.copy()

    # Supertrend — signal principal de direction
    # length=14, mult=4.5 : moins sensible, évite les faux signaux
    st = ta.supertrend(df["high"], df["low"], df["close"], length=14, multiplier=4.5)
    df["supertrend"] = st["SUPERT_14_4.5"]
    df["supertrend_dir"] = st["SUPERTd_14_4.5"]  # 1 = haussier, -1 = baissier

    # Golden Cross — filtre tendance longue
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)

    # RSI — filtre surachat
    df["rsi"] = ta.rsi(df["close"], length=14)

    # ATR — stop-loss dynamique
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

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

    # Supertrend passe en haussier ET confirmation : 2 bougies consécutives haussières
    supertrend_up = (
        (df["supertrend_dir"] == 1) &
        (df["supertrend_dir"].shift(1) == -1)
    )

    # Supertrend passe en baissier
    supertrend_down = (df["supertrend_dir"] == -1) & (df["supertrend_dir"].shift(1) == 1)

    # Filtres de qualité
    above_ema200 = df["close"] > df["ema200"]
    bullish_structure = df["ema50"] > df["ema200"]  # Golden cross actif
    not_overbought = df["rsi"] < 75

    # Signaux
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

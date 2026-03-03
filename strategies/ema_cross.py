import pandas as pd
import pandas_ta as ta
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule tous les indicateurs techniques sur le DataFrame.

    Indicateurs :
    - EMA 20, 50 (signal principal), EMA 200 (filtre tendance)
    - RSI 14 (filtre de confirmation)
    - ATR 14 (calcul du stop-loss dynamique)
    - ADX 14 (filtre de force de tendance)
    - Volume moyen 20 (filtre de liquidité)
    """
    df = df.copy()

    # EMAs
    df["ema_fast"] = ta.ema(df["close"], length=config.EMA_FAST)
    df["ema_slow"] = ta.ema(df["close"], length=config.EMA_SLOW)
    df["ema_trend"] = ta.ema(df["close"], length=config.EMA_TREND)

    # RSI
    df["rsi"] = ta.rsi(df["close"], length=config.RSI_PERIOD)

    # ATR (Average True Range) pour stop-loss dynamique
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ADX (force de la tendance)
    adx_data = ta.adx(df["high"], df["low"], df["close"], length=config.ADX_PERIOD)
    df["adx"] = adx_data[f"ADX_{config.ADX_PERIOD}"]

    # Volume moyen sur 20 périodes (filtre de liquidité)
    df["vol_ma"] = df["volume"].rolling(20).mean()

    return df.dropna()


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Génère les signaux d'achat/vente avec 5 filtres de confirmation.

    Signal LONG (achat) si TOUTES les conditions sont remplies :
      1. EMA rapide croise EMA lente vers le haut (golden cross)
      2. Prix au-dessus de l'EMA 200 (tendance longue haussière)
      3. ADX > 25 (tendance forte, évite les marchés plats)
      4. RSI entre 40 et 75 (ni survendu, ni suracheté)
      5. Volume supérieur à 70% de la moyenne

    Signal de sortie si :
      1. EMA rapide croise EMA lente vers le bas (death cross)
      2. RSI < 60
    """
    df = add_indicators(df)

    # Croisements EMA
    cross_up = (df["ema_fast"] > df["ema_slow"]) & (
        df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)
    )
    cross_down = (df["ema_fast"] < df["ema_slow"]) & (
        df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)
    )

    # Filtre tendance longue (EMA 200) — le filtre le plus important
    above_trend = df["close"] > df["ema_trend"]

    # Filtre ADX — ne trader que quand la tendance est forte
    strong_trend = df["adx"] > config.ADX_THRESHOLD

    # Filtre RSI
    rsi_ok_long = (df["rsi"] > 40) & (df["rsi"] < config.RSI_OVERBOUGHT)
    rsi_ok_short = df["rsi"] < 60

    # Filtre volume
    vol_ok = df["volume"] > df["vol_ma"] * 0.7

    # Signaux finaux
    df["signal"] = 0
    df.loc[cross_up & above_trend & strong_trend & rsi_ok_long & vol_ok, "signal"] = 1
    df.loc[cross_down & rsi_ok_short, "signal"] = -1

    return df


def calculate_position_size(capital: float, entry_price: float, atr: float) -> dict:
    """
    Calcule la taille de position avec gestion du risque stricte.

    Logique :
    - On risque max RISK_PER_TRADE % du capital
    - Stop-loss = ATR_MULTIPLIER x ATR sous le prix d'entrée
    - Taille = (Capital x Risque%) / Stop-loss en EUR

    Returns:
        dict avec: size (quantité), stop_loss, take_profit, risk_eur
    """
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

    print("=== Test de la stratégie ===")
    df = fetch_ohlcv("BTC/EUR", days=90)
    df = generate_signals(df)

    signals = df[df["signal"] != 0]
    print(f"\nSignaux générés : {len(signals)}")
    print(f"  Achats  : {len(df[df['signal'] == 1])}")
    print(f"  Ventes  : {len(df[df['signal'] == -1])}")
    print(signals[["close", "ema_fast", "ema_slow", "rsi", "signal"]].tail(10))

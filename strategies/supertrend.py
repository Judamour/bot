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


def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Calcule l'ADX — mesure la force de la tendance (> 20 = tendance, > 25 = forte)."""
    high_diff = high.diff()
    low_diff = -low.diff()

    plus_dm = pd.Series(
        np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0),
        index=low.index,
    )

    atr = compute_atr(high, low, close, length)
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr

    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)) * 100
    return dx.ewm(alpha=1 / length, adjust=False).mean()


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
    Calcule tous les indicateurs pour la stratégie.

    Indicateurs :
    - Supertrend (ATR 14, mult 4.5) : signal principal de retournement
    - EMA 9 / EMA 21 : momentum court terme (Golden Cross rapide)
    - EMA 50 / EMA 200 : filtre de tendance longue (Golden Cross lent)
    - RSI 14 : filtre surachat/survente
    - ADX 14 : force de la tendance (filtre marché en range)
    - ATR 14 : volatilité pour le sizing
    - Volume MA 20 + ratio : confirmation de volume sur le signal
    """
    df = df.copy()

    df["supertrend"], df["supertrend_dir"] = compute_supertrend(
        df["high"], df["low"], df["close"], length=14, multiplier=4.5
    )
    df["ema9"] = compute_ema(df["close"], span=config.EMA_FAST)
    df["ema21"] = compute_ema(df["close"], span=config.EMA_SLOW)
    df["ema50"] = compute_ema(df["close"], span=50)
    df["ema200"] = compute_ema(df["close"], span=200)
    df["rsi"] = compute_rsi(df["close"], length=14)
    df["atr"] = compute_atr(df["high"], df["low"], df["close"], length=14)
    df["adx"] = compute_adx(df["high"], df["low"], df["close"], length=config.ADX_PERIOD)
    # Volume : remplacer 0 (hors heures marché xStocks) par NaN puis ffill
    vol = df["volume"].replace(0, float("nan"))
    df["volume_ma"] = vol.rolling(window=20, min_periods=5).mean()
    df["volume_ratio"] = (vol / df["volume_ma"]).ffill().fillna(1.0)

    return df.dropna()


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Génère les signaux avec la stratégie Supertrend + filtres multiples.

    Signal LONG si TOUS ces critères sont réunis :
      1. Supertrend passe en haussier (direction -1 → 1)
      2. ADX > seuil (marché en tendance, pas en range)
      3. Prix > EMA 200 (tendance longue confirmée)
      4. EMA 50 > EMA 200 (structure de marché haussière)
      5. EMA 9 > EMA 21 (momentum court terme aligné)
      6. RSI < 75 (pas de surachat extrême)
      7. Volume > 110% de la moyenne 20 périodes (confirmation institutionnelle)

    Signal EXIT si :
      1. Supertrend passe en baissier (direction 1 → -1)
    """
    df = add_indicators(df)

    supertrend_up   = (df["supertrend_dir"] == 1) & (df["supertrend_dir"].shift(1) == -1)
    supertrend_down = (df["supertrend_dir"] == -1) & (df["supertrend_dir"].shift(1) == 1)

    trending_market  = df["adx"] > config.ADX_THRESHOLD      # Pas de range
    above_ema200     = df["close"] > df["ema200"]             # Tendance longue
    bullish_structure = df["ema50"] > df["ema200"]            # Golden Cross lent
    momentum_up      = df["ema9"] > df["ema21"]               # Golden Cross rapide
    not_overbought   = df["rsi"] < config.RSI_OVERBOUGHT      # Pas de surachat
    strong_volume    = df["volume_ratio"] > 1.1               # Volume > 110% moyenne

    # ── Colonnes booléennes pour analyse contrefactuelle ──
    df["f_supertrend_up"] = supertrend_up
    df["f_trending"]      = trending_market
    df["f_above_ema200"]  = above_ema200
    df["f_structure"]     = bullish_structure
    df["f_momentum"]      = momentum_up
    df["f_rsi"]           = not_overbought
    df["f_volume"]        = strong_volume

    df["signal"] = 0
    # ── 3 conditions hard (non négociables) ──────────────────────────────────
    # Supertrend flip + pas de surachat RSI + prix au-dessus EMA200
    # Les 4 autres filtres (ADX, volume, structure, momentum) sont passés à
    # Claude comme contexte — il est le décideur final.
    df.loc[
        supertrend_up
        & not_overbought
        & above_ema200,
        "signal",
    ] = 1
    df.loc[supertrend_down, "signal"] = -1

    return df


def calculate_position_size(position_eur: float, entry_price: float, atr: float) -> dict:
    """Calcule la taille de position avec montant fixe en EUR."""
    size = position_eur / entry_price
    stop_distance = atr * config.ATR_MULTIPLIER

    stop_loss = entry_price - stop_distance
    take_profit = entry_price + (stop_distance * config.TAKE_PROFIT_RATIO)
    risk_eur = size * stop_distance  # risque réel selon ATR

    return {
        "size": round(size, 6),
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        "risk_eur": round(risk_eur, 2),
        "stop_distance": round(stop_distance, 4),
    }


if __name__ == "__main__":
    from data.fetcher import fetch_ohlcv

    print("=== Test Supertrend + filtres ===")
    df = fetch_ohlcv("BTC/EUR", days=365)
    df = generate_signals(df)

    signals = df[df["signal"] != 0]
    print(f"Signaux : {len(signals)} ({len(df[df['signal']==1])} achats, {len(df[df['signal']==-1])} ventes)")
    print(signals[["close", "supertrend_dir", "adx", "rsi", "volume_ratio", "signal"]].tail(10))

"""Détecteurs de signaux pour le shadow bot.

Chaque fonction reçoit un DataFrame OHLCV avec indicateurs et retourne
soit un Signal soit None. Les fonctions s'inspirent des stratégies prod
mais sont auto-contenues pour rester indépendantes.
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.supertrend import add_indicators
from strategies.breakout_strategy import add_donchian_indicators
from shadow.scorer import Signal


def detect_supertrend(symbol: str, df_4h, df_1d=None) -> Signal | None:
    """Supertrend long signal : trend bullish + ADX + RSI sain."""
    if df_4h is None or len(df_4h) < 50:
        return None
    df = add_indicators(df_4h.copy())
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    # Conditions
    trend_bull = bool(last.get("supertrend_dir", 0) == 1)
    crossed_up = (prev.get("supertrend_dir", 0) <= 0) and (last.get("supertrend_dir", 0) == 1)
    adx = float(last.get("adx", 0) or 0)
    rsi = float(last.get("rsi", 50) or 50)
    close = float(last["close"])

    # Signal : trend bull + ADX assez forte + RSI pas overbought
    if not trend_bull or adx < 18 or rsi > 75:
        return None

    # Bonus si on vient de cross
    atr = float(last.get("atr", 0) or 0)
    if atr <= 0:
        return None

    # MTF check
    mtf_aligned = False
    if df_1d is not None and len(df_1d) >= 50:
        df_1d_ind = add_indicators(df_1d.copy())
        last_1d = df_1d_ind.iloc[-1]
        mtf_aligned = bool(last_1d.get("supertrend_dir", 0) == 1)

    # Volume
    vol_avg = df["volume"].tail(20).mean()
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0

    return Signal(
        symbol=symbol,
        strategy="supertrend",
        side="long",
        entry_price=close,
        atr=atr,
        stop_price=close - 4 * atr,
        rationale={
            "adx": adx, "rsi": rsi, "volume_ratio": vol_ratio,
            "mtf_aligned": mtf_aligned,
            "crossed_up": crossed_up,
        },
    )


def detect_donchian(symbol: str, df_4h, df_1d=None) -> Signal | None:
    """Donchian breakout 50-period high."""
    if df_4h is None or len(df_4h) < 60:
        return None
    df = add_donchian_indicators(df_4h.copy())
    df = add_indicators(df)
    last = df.iloc[-1]
    close = float(last["close"])

    # Need 50-period high
    high_50 = float(df["high"].tail(50).max())
    if close < high_50 * 0.995:  # pas dans les 0.5% du high
        return None

    breakout_pct = (close - high_50) / high_50 if high_50 > 0 else 0
    atr = float(last.get("atr", 0) or 0)
    adx = float(last.get("adx", 0) or 0)
    rsi = float(last.get("rsi", 50) or 50)

    if atr <= 0 or adx < 22:
        return None

    vol_avg = df["volume"].tail(20).mean()
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0

    mtf_aligned = False
    if df_1d is not None and len(df_1d) >= 200:
        last_1d_close = float(df_1d["close"].iloc[-1])
        sma_200 = float(df_1d["close"].tail(200).mean())
        mtf_aligned = last_1d_close > sma_200

    return Signal(
        symbol=symbol,
        strategy="donchian",
        side="long",
        entry_price=close,
        atr=atr,
        stop_price=close - 4 * atr,
        rationale={
            "adx": adx, "rsi": rsi, "volume_ratio": vol_ratio,
            "mtf_aligned": mtf_aligned, "breakout_pct": breakout_pct,
            "high_50": high_50,
        },
    )


def detect_mean_reversion(symbol: str, df_4h, df_1d=None) -> Signal | None:
    """RSI(2) < 10 sur uptrend long-terme."""
    if df_4h is None or len(df_4h) < 50:
        return None
    df = add_indicators(df_4h.copy())
    last = df.iloc[-1]
    close = float(last["close"])

    # RSI(2) — recompute (add_indicators fait RSI(14))
    rsi2_series = _rsi(df["close"], length=2)
    rsi2 = float(rsi2_series.iloc[-1])
    if rsi2 > 10:
        return None

    # Trend long-terme bullish (SMA200 si dispo)
    if df_1d is None or len(df_1d) < 200:
        return None
    sma_200_1d = float(df_1d["close"].tail(200).mean())
    last_1d_close = float(df_1d["close"].iloc[-1])
    if last_1d_close < sma_200_1d:
        return None  # pas en uptrend macro

    atr = float(last.get("atr", 0) or 0)
    if atr <= 0:
        return None

    adx = float(last.get("adx", 0) or 0)
    vol_avg = df["volume"].tail(20).mean()
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0

    return Signal(
        symbol=symbol,
        strategy="mean_reversion",
        side="long",
        entry_price=close,
        atr=atr,
        stop_price=close - 1.0 * atr,  # MR stop serré
        rationale={
            "adx": adx, "rsi": rsi2, "volume_ratio": vol_ratio,
            "mtf_aligned": True,  # déjà checké
        },
    )


def detect_momentum(symbol: str, df_4h, df_1d=None) -> Signal | None:
    """Momentum Antonacci-style : 90-day return positive et top quartile."""
    if df_1d is None or len(df_1d) < 90:
        return None
    last_close = float(df_1d["close"].iloc[-1])
    close_90d_ago = float(df_1d["close"].iloc[-90])
    momentum_90d = (last_close - close_90d_ago) / close_90d_ago

    if momentum_90d < 0.10:  # < 10% sur 90j → pas dans le top
        return None

    # Cross-check : current price proche du high récent
    high_30d = float(df_1d["high"].tail(30).max())
    if last_close < high_30d * 0.95:  # > 5% sous le high récent → pas en momentum frais
        return None

    if df_4h is None or len(df_4h) < 20:
        return None
    df = add_indicators(df_4h.copy())
    last = df.iloc[-1]
    atr = float(last.get("atr", 0) or 0)
    adx = float(last.get("adx", 0) or 0)
    rsi = float(last.get("rsi", 50) or 50)
    if atr <= 0:
        return None

    vol_avg = df["volume"].tail(20).mean()
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0

    return Signal(
        symbol=symbol,
        strategy="momentum",
        side="long",
        entry_price=float(last["close"]),
        atr=atr,
        stop_price=float(last["close"]) - 4 * atr,
        rationale={
            "adx": adx, "rsi": rsi, "volume_ratio": vol_ratio,
            "mtf_aligned": True, "momentum_90d": momentum_90d,
        },
    )


def detect_trend_multi_asset(symbol: str, df_4h, df_1d=None) -> Signal | None:
    """Trend long-terme : price > SMA(50) > SMA(200) sur daily."""
    if df_1d is None or len(df_1d) < 200:
        return None
    sma_50 = float(df_1d["close"].tail(50).mean())
    sma_200 = float(df_1d["close"].tail(200).mean())
    last_1d = float(df_1d["close"].iloc[-1])

    if not (last_1d > sma_50 > sma_200):
        return None

    if df_4h is None or len(df_4h) < 20:
        return None
    df = add_indicators(df_4h.copy())
    last = df.iloc[-1]
    atr = float(last.get("atr", 0) or 0)
    adx = float(last.get("adx", 0) or 0)
    rsi = float(last.get("rsi", 50) or 50)

    if atr <= 0 or adx < 20:
        return None

    vol_avg = df["volume"].tail(20).mean()
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0

    return Signal(
        symbol=symbol,
        strategy="trend_multi_asset",
        side="long",
        entry_price=float(last["close"]),
        atr=atr,
        stop_price=float(last["close"]) - 4 * atr,
        rationale={
            "adx": adx, "rsi": rsi, "volume_ratio": vol_ratio,
            "mtf_aligned": True,
        },
    )


def detect_inverse_bear(symbol: str, df_4h, df_1d=None) -> Signal | None:
    """Inverse ETF signal (iter-6 #4) — fires ONLY on SQQQ/SH and only when
    the inverse itself is trending up (= underlying in confirmed downtrend).

    Same structure as detect_trend_multi_asset (price > SMA50 > SMA200 on 1d)
    but restricted to SQQQ/SH. The cycle's equity_bear gate further protects:
    these symbols only end up in the scan universe when equity_bear is active.

    Volatility decay risk: trailing stops at 4×/5× ATR limit hold time when
    underlying recovers (inverse ETF would drop fast on QQQ/SPY bounce).
    """
    if symbol not in ("SQQQ", "SH"):
        return None
    if df_1d is None or len(df_1d) < 200:
        return None
    sma_50 = float(df_1d["close"].tail(50).mean())
    sma_200 = float(df_1d["close"].tail(200).mean())
    last_1d = float(df_1d["close"].iloc[-1])

    # Inverse must itself be in uptrend (= underlying breaking down)
    if not (last_1d > sma_50 > sma_200):
        return None

    if df_4h is None or len(df_4h) < 20:
        return None
    df = add_indicators(df_4h.copy())
    last = df.iloc[-1]
    atr = float(last.get("atr", 0) or 0)
    adx = float(last.get("adx", 0) or 0)
    rsi = float(last.get("rsi", 50) or 50)

    if atr <= 0 or adx < 22:  # require stronger trend than regular trend_multi (20)
        return None

    vol_avg = df["volume"].tail(20).mean()
    vol_ratio = float(last["volume"]) / vol_avg if vol_avg > 0 else 1.0

    return Signal(
        symbol=symbol,
        strategy="inverse_bear",
        side="long",  # buying SQQQ/SH (which is conceptually short underlying)
        entry_price=float(last["close"]),
        atr=atr,
        stop_price=float(last["close"]) - 4 * atr,
        rationale={
            "adx": adx, "rsi": rsi, "volume_ratio": vol_ratio,
            "mtf_aligned": True,
            "inverse_underlying": "QQQ" if symbol == "SQQQ" else "SPY",
        },
    )


def _rsi(series, length: int = 14):
    """RSI standard."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(length).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


ALL_DETECTORS = [
    detect_supertrend,
    detect_donchian,
    detect_mean_reversion,
    detect_momentum,
    detect_trend_multi_asset,
    detect_inverse_bear,
]

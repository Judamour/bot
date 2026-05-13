"""Score composite pour chaque signal détecté.

Combine plusieurs facteurs en un score 0-100 :
  - Trend strength (ADX)
  - Volume confirmation
  - RSI position (ni overbought ni oversold)
  - MTF alignment (4h + 1d)
  - Breakout magnitude (si applicable)
  - VIX context (penalty si > 25 sauf mean reversion)

Le score sert au top-N selection : les signaux les plus forts gagnent le capital.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Signal:
    """Signal détecté par une stratégie sur un symbole."""
    symbol: str
    strategy: str
    side: str  # "long" (pour l'instant que long)
    entry_price: float
    atr: float
    stop_price: float
    rationale: dict = field(default_factory=dict)
    score: float = 0.0


def compute_score(sig: Signal, ctx: dict) -> float:
    """Calcule le score composite d'un signal. 0-100.

    Args:
        sig: signal détecté avec ses metadata (ADX, RSI, volume_ratio, …)
        ctx: contexte marché (VIX, BTC trend, breadth, …)
    """
    r = sig.rationale
    score = 0.0

    # 1. Trend strength (ADX) — 0-30 pts
    adx = r.get("adx", 0)
    if adx >= 30:
        score += 30
    elif adx >= 22:
        score += 20
    elif adx >= 18:
        score += 10

    # 2. Volume confirmation — 0-15 pts
    vol_ratio = r.get("volume_ratio", 1.0)
    if vol_ratio >= 1.5:
        score += 15
    elif vol_ratio >= 1.2:
        score += 10
    elif vol_ratio >= 1.0:
        score += 5

    # 3. RSI position — 0-15 pts
    rsi = r.get("rsi", 50)
    if sig.strategy == "mean_reversion":
        # MR aime RSI extrêmement bas
        if rsi < 10:
            score += 15
        elif rsi < 20:
            score += 10
    else:
        # Trend strategies : RSI 40-65 = momentum sain
        if 45 <= rsi <= 65:
            score += 15
        elif 35 <= rsi <= 70:
            score += 10
        elif rsi > 75:
            score -= 5  # overbought, risque de retournement

    # 4. MTF alignment — 0-15 pts
    if r.get("mtf_aligned", False):
        score += 15

    # 5. Breakout magnitude — 0-15 pts
    breakout_pct = r.get("breakout_pct", 0)
    if breakout_pct > 0:
        score += min(15, breakout_pct * 100)  # ex: +2% breakout = 2 pts

    # 6. Strategy historical edge — 0-10 pts (bonus statique par stratégie)
    edge_bonus = {
        "supertrend": 10,        # backtest 10y CAGR 49% — le plus fort
        "trend_multi_asset": 8,  # CAGR 23% — solide
        "donchian": 6,           # peu de signaux mais bons
        "momentum": 4,           # rotation lente
        "mean_reversion": 5,     # défensif
        "inverse_bear": 7,       # iter-6 #4: ne fire qu'en bear, profit asymétrique
    }
    score += edge_bonus.get(sig.strategy, 0)

    # 7. Context penalties / bonuses
    vix = ctx.get("vix", 18)
    if vix > 28:
        if sig.strategy == "mean_reversion":
            score += 5  # MR aime la peur
        else:
            score -= 10  # crise → moins de trend reliability

    btc_trend = ctx.get("btc_trend", "bull")
    if btc_trend == "bear" and "/" in sig.symbol:
        score -= 15  # crypto en bear → skip ou cap

    qqq_ok = ctx.get("qqq_ok", True)
    if not qqq_ok and sig.symbol not in ("GLD",):
        score -= 8  # marché stocks défavorable

    return max(0.0, min(100.0, score))

import os
import anthropic


def ask_claude(
    symbol: str,
    price: float,
    rsi: float,
    ema50: float,
    ema200: float,
    atr: float,
    adx: float,
    volume_ratio: float,
    capital: float,
) -> tuple[bool, str]:
    """
    Demande à Claude de valider un signal d'achat.
    Retourne (confirme: bool, raison: str)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return True, "Clé API manquante — signal accepté par défaut"

    client = anthropic.Anthropic(api_key=api_key)

    trend = "HAUSSIER (Golden Cross actif)" if ema50 > ema200 else "BAISSIER"
    distance_ema200 = ((price - ema200) / ema200) * 100
    adx_label = "forte tendance ✓" if adx > 25 else "tendance modérée" if adx > 20 else "marché en range ⚠"
    vol_label = "volume fort ✓" if volume_ratio > 1.3 else "volume normal" if volume_ratio > 1.1 else "volume faible ⚠"

    prompt = f"""Tu es un trader algorithmique spécialisé en crypto.
Signal BUY validé techniquement sur {symbol} (timeframe 4h).

INDICATEURS CONFIRMÉS:
- Prix: {price:.4f}€
- Supertrend: retournement HAUSSIER ✓
- ADX(14): {adx:.1f} ({adx_label})
- Volume: {volume_ratio:.2f}× la moyenne 20 bougies ({vol_label})
- RSI(14): {rsi:.1f} (< 75 ✓)
- EMA9 > EMA21 (momentum court terme haussier ✓)
- EMA50 > EMA200 (Golden Cross long terme ✓)
- Tendance: {trend}
- Distance EMA200: +{distance_ema200:.1f}%
- ATR(14): {atr:.4f}€ | Capital: {capital:.2f}€

RÈGLES TRADE: Risk 2% | SL = 3×ATR | TP = 2.5×ATR (R:R 1:2.5)

Évalue ce trade en tenant compte du contexte crypto actuel.
Si la plupart des indicateurs sont alignés, confirme.
Ignore seulement si tu identifies un risque macro majeur spécifique.

Réponds EXACTEMENT:
DÉCISION: CONFIRME ou IGNORE
RAISON: [1 phrase concise]"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        response = message.content[0].text.strip()

        confirme = "CONFIRME" in response.upper()
        lines = response.split("\n")
        raison = next(
            (l.replace("RAISON:", "").strip() for l in lines if "RAISON:" in l),
            response,
        )
        return confirme, raison

    except Exception as e:
        return True, f"Erreur API Claude ({e}) — signal accepté"

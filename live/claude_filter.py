import os
import anthropic


def ask_claude(symbol: str, price: float, rsi: float, ema50: float,
               ema200: float, atr: float, capital: float) -> tuple[bool, str]:
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

    prompt = f"""Tu es un analyste crypto spécialisé dans le trading algorithmique.
Un signal d'achat technique vient de se déclencher sur {symbol}.

DONNÉES TECHNIQUES:
- Prix: {price:.2f}€
- RSI(14): {rsi:.1f}/100 {"(zone neutre ✓)" if 40 < rsi < 70 else "(attention)"}
- EMA50: {ema50:.2f}€ | EMA200: {ema200:.2f}€
- Tendance: {trend}
- Distance au-dessus EMA200: {distance_ema200:.1f}%
- ATR(14): {atr:.2f}€ (volatilité)
- Capital disponible: {capital:.2f}€

CONTEXTE:
- Stratégie: Supertrend + Golden Cross + RSI sur timeframe 4h
- Risk per trade: 2% du capital
- Mode: Paper trading (simulation)

Évalue si ce trade est pertinent dans le contexte macro actuel des cryptos.
Considère: tendances du marché crypto, dominance BTC, sentiment général.

Réponds EXACTEMENT dans ce format:
DÉCISION: CONFIRME ou IGNORE
RAISON: [1-2 phrases concises]"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}]
        )
        response = message.content[0].text.strip()

        confirme = "CONFIRME" in response.upper()
        lines = response.split("\n")
        raison = next((l.replace("RAISON:", "").strip() for l in lines if "RAISON:" in l), response)

        return confirme, raison

    except Exception as e:
        return True, f"Erreur API Claude ({e}) — signal accepté"

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
    # Contexte macro
    btc_context: dict = None,
    vix: float = 0.0,
    # Sentiment et dérivés
    fear_greed: dict = None,
    funding_rate: float = 0.0,
    # Contexte portfolio
    open_positions: int = 0,
    max_positions: int = 3,
    recent_win_rate: float = None,
    rotation_factor: float = 1.0,
    daily_trend_reason: str = "",
    news: list = None,
) -> tuple[bool, str]:
    """
    Demande à Claude de valider un signal d'achat.
    Retourne (confirme: bool, raison: str)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return True, "Clé API manquante — signal accepté par défaut"

    client = anthropic.Anthropic(api_key=api_key)

    # ── Indicateurs techniques ──
    trend = "HAUSSIER (Golden Cross)" if ema50 > ema200 else "BAISSIER (Death Cross)"
    dist_ema200 = ((price - ema200) / ema200) * 100
    adx_label = "forte tendance ✓" if adx > 25 else "tendance modérée" if adx > 20 else "range ⚠"
    vol_label = "fort ✓" if volume_ratio > 1.3 else "normal" if volume_ratio > 1.1 else "faible ⚠"
    category = "xStock US (actions tokenisées)" if symbol.endswith("x/EUR") else "Crypto 24/7"

    # ── Contexte macro BTC + VIX ──
    macro_parts = []
    if btc_context:
        bt = btc_context.get("btc_trend", "?").upper()
        bp = btc_context.get("btc_price", 0)
        be = btc_context.get("btc_above_ema200", False)
        macro_parts.append(f"BTC {bt} ({bp:.0f}€ {'>' if be else '<'} EMA200)")
    if vix > 0:
        vix_label = "PEUR ÉLEVÉE ⚠" if vix > 25 else "élevé" if vix > 20 else "normal"
        macro_parts.append(f"VIX {vix:.1f} ({vix_label})")
    macro_str = " | ".join(macro_parts) if macro_parts else "Non disponible"

    # ── Sentiment Fear & Greed ──
    fg_str = "N/A"
    fg_alert = ""
    if fear_greed:
        score = fear_greed.get("score", 50)
        label = fear_greed.get("label", "Neutral")
        fg_str = f"{score}/100 ({label})"
        if score <= 20:
            fg_alert = " ⚠ PEUR EXTRÊME — possible capitulation ou achat contrarian"
        elif score >= 80:
            fg_alert = " ⚠ AVIDITÉ EXTRÊME — risque de retournement imminent"

    # ── Funding rate (crypto uniquement) ──
    funding_str = ""
    if funding_rate != 0.0:
        pct = funding_rate * 100
        if funding_rate > 0.001:
            funding_label = "DANGER squeeze" if funding_rate > 0.001 else "longs surexposés ⚠"
            # 0.001 = 0.1%, 0.0003 = 0.03%
            funding_label = "DANGER squeeze ⚠" if funding_rate > 0.001 else "longs surexposés" if funding_rate > 0.0003 else "neutre"
        elif funding_rate < -0.0001:
            funding_label = "shorts surexposés (signal haussier contrarian)"
        else:
            funding_label = "neutre"
        funding_str = f"\n• Funding rate: {pct:+.4f}%/8h ({funding_label})"

    # ── Portfolio ──
    slots_left = max_positions - open_positions
    wr_str = f"{recent_win_rate:.0f}%" if recent_win_rate is not None else "N/A"
    rot_str = f"×{rotation_factor:.2f} ({'surpondéré' if rotation_factor > 1.0 else 'souspondéré' if rotation_factor < 1.0 else 'neutre'})"

    daily_str = f"✓ {daily_trend_reason}" if daily_trend_reason else "✓ confirmé"

    # ── Actualités récentes ──
    news_str = ""
    if news:
        lines = []
        for n in news[:6]:
            age = f"{n['age_h']}h" if n.get("age_h") else ""
            src = n.get("source", "")
            title = n.get("title", "")
            lines.append(f"• [{src}] {title}" + (f" ({age})" if age else ""))
        news_str = "\nACTUALITÉS RÉCENTES (24-48h) :\n" + "\n".join(lines) + "\n"

    prompt = f"""Tu es un trader algorithmique. Signal BUY technique validé sur {symbol} ({category}).

INDICATEURS 4H (7/7 filtres passés) :
• Prix: {price:.4f}€ | ATR: {atr:.4f}€ | Distance EMA200: {dist_ema200:+.1f}%
• Supertrend: HAUSSIER ✓ | Tendance 1d: {daily_str}
• ADX: {adx:.1f} ({adx_label}) | RSI: {rsi:.1f} (<75 ✓) | Volume: ×{volume_ratio:.2f} ({vol_label})
• EMA9>EMA21 ✓ | EMA50/EMA200: {trend}

CONTEXTE MACRO & SENTIMENT :
• Macro: {macro_str}
• Fear & Greed: {fg_str}{fg_alert}{funding_str}
• Portfolio: {slots_left}/{max_positions} slots libres | Capital: {capital:.0f}€
• Win rate récent: {wr_str} | Facteur taille: {rot_str}
{news_str}
TRADE : Risk 2% | SL=3×ATR | TP=2.5×ATR (R:R 1:2.5)

Confirme si les indicateurs sont alignés. Les actualités peuvent faire pencher la balance si elles révèlent un risque sectoriel direct (annonce tarifaire, résultat décevant, crise macro) — sinon, les indicateurs techniques priment.

Réponds EXACTEMENT :
DÉCISION: CONFIRME ou IGNORE
RAISON: [1-2 phrases : facteur décisif + impact du sentiment/macro]"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=160,
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

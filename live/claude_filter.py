import os
import json
import subprocess
import anthropic


# ── Token management ─────────────────────────────────────────────────────────

_CREDS_PATHS = [
    os.path.expanduser("~/.claude/.credentials.json"),
    "/home/botuser/.claude/.credentials.json",
    "/home/ubuntu/.claude/.credentials.json",
]


def _get_token() -> str:
    """Read OAuth access token from Claude CLI credentials file."""
    for path in _CREDS_PATHS:
        try:
            with open(path) as f:
                creds = json.load(f)
            return creds["claudeAiOauth"]["accessToken"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            continue
    return ""


def _refresh_token_via_cli():
    """Force Claude CLI to refresh the OAuth token (runs in background)."""
    try:
        subprocess.run(
            ["claude", "-p", "hi", "--output-format", "text", "--effort", "low"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        pass


def _get_client() -> anthropic.Anthropic:
    """Get Anthropic client with fresh OAuth token."""
    token = _get_token()
    if not token:
        _refresh_token_via_cli()
        token = _get_token()
    return anthropic.Anthropic(api_key=token)


# ── Claude filter ────────────────────────────────────────────────────────────

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
    soft_filters: dict = None,
) -> tuple[bool, str]:
    """
    Demande à Claude de valider un signal d'achat.
    Utilise le SDK Anthropic directement (pas le CLI) pour éviter le system prompt
    et les problèmes d'auth.
    Retourne (confirme: bool, raison: str)
    """

    # ── Indicateurs techniques ──
    trend = "Golden Cross (bullish)" if ema50 > ema200 else "Death Cross (bearish)"
    dist_ema200 = ((price - ema200) / ema200) * 100
    category = "xStock US" if symbol.endswith("x/EUR") else "Crypto"

    # ── Contexte macro BTC + VIX ──
    macro_parts = []
    if btc_context:
        bt = btc_context.get("btc_trend", "?").upper()
        bp = btc_context.get("btc_price", 0)
        be = btc_context.get("btc_above_ema200", False)
        macro_parts.append(f"BTC {bt} ({bp:.0f}EUR {'>' if be else '<'} EMA200)")
    if vix > 0:
        vix_label = "HIGH FEAR" if vix > 25 else "elevated" if vix > 20 else "normal"
        macro_parts.append(f"VIX {vix:.1f} ({vix_label})")
    macro_str = " | ".join(macro_parts) if macro_parts else "N/A"

    # ── Sentiment Fear & Greed ──
    fg_str = "N/A"
    fg_alert = ""
    if fear_greed:
        score = fear_greed.get("score", 50)
        label = fear_greed.get("label", "Neutral")
        fg_str = f"{score}/100 ({label})"
        if score <= 20:
            fg_alert = " — EXTREME FEAR (possible capitulation)"
        elif score >= 80:
            fg_alert = " — EXTREME GREED (reversal risk)"

    # ── Funding rate (crypto) ──
    funding_str = ""
    if funding_rate != 0.0:
        pct = funding_rate * 100
        if funding_rate > 0.001:
            funding_label = "DANGER squeeze"
        elif funding_rate > 0.0003:
            funding_label = "longs overexposed"
        elif funding_rate < -0.0001:
            funding_label = "shorts overexposed (contrarian bullish)"
        else:
            funding_label = "neutral"
        funding_str = f"\n- Funding rate: {pct:+.4f}%/8h ({funding_label})"

    # ── Portfolio ──
    slots_left = max_positions - open_positions
    wr_str = f"{recent_win_rate:.0f}%" if recent_win_rate is not None else "N/A"

    # ── Soft filters ──
    soft_str = ""
    if soft_filters is not None:
        sf_items = [
            ("adx_trending", "ADX>20 (trending)"),
            ("volume_strong", "Volume>110% MA"),
            ("structure",     "EMA50>EMA200 (bullish structure)"),
            ("momentum",      "EMA9>EMA21 (short-term momentum)"),
            ("mtf_1d",        "Daily trend (ST up + >EMA200)"),
            ("qqq_regime",    "QQQ > SMA200 (Risk-ON)"),
            ("no_rsi_div",    "No RSI bearish divergence"),
        ]
        ok_count = sum(1 for k, _ in sf_items if soft_filters.get(k, True))
        sf_lines = []
        for k, label in sf_items:
            ok = soft_filters.get(k, True)
            line = f"  {'OK' if ok else 'WARN'} {label}"
            if k == "mtf_1d" and not ok and daily_trend_reason:
                line += f" ({daily_trend_reason})"
            sf_lines.append(line)
        soft_str = f"\nSoft filters ({ok_count}/7 passed):\n" + "\n".join(sf_lines)

    # ── News ──
    news_str = ""
    if news:
        news_lines = []
        for n in news[:6]:
            age = f"{n['age_h']}h" if n.get("age_h") else ""
            src = n.get("source", "")
            title = n.get("title", "")
            news_lines.append(f"- [{src}] {title}" + (f" ({age})" if age else ""))
        news_str = "\nRecent news (24-48h):\n" + "\n".join(news_lines)

    prompt = f"""Evaluate this paper trading signal. Respond with exactly 2 lines.

Signal: BUY {symbol} ({category})
Price: {price:.4f}EUR | EMA50/200: {trend} | Dist EMA200: {dist_ema200:+.1f}%
RSI: {rsi:.1f} | ADX: {adx:.1f} | Volume: x{volume_ratio:.2f} | ATR: {atr:.4f}EUR
{soft_str}
Macro: {macro_str}
Fear&Greed: {fg_str}{fg_alert}{funding_str}
Portfolio: {slots_left}/{max_positions} slots | Capital: {capital:.0f}EUR | WR: {wr_str}
{news_str}
Risk: 2% | SL=3xATR | TP=2.5xATR

CONFIRM if broadly favorable (even with 2-3 soft warnings).
IGNORE only if direct macro risk (tariffs, crisis, earnings miss).

DECISION: CONFIRM or IGNORE
REASON: one sentence"""

    try:
        client = _get_client()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=160,
            messages=[{"role": "user", "content": prompt}],
        )
        response = resp.content[0].text.strip()

        upper = response.upper()
        confirme = "CONFIRM" in upper and "IGNORE" not in upper
        lines = response.split("\n")
        raison = next(
            (l.split(":", 1)[1].strip() for l in lines if "REASON:" in l.upper()),
            response[:200],
        )
        from live.notifier import clear_api_alert
        clear_api_alert("anthropic")
        return confirme, raison

    except anthropic.AuthenticationError:
        # Token expiré — tenter un refresh via CLI puis retry
        _refresh_token_via_cli()
        try:
            client = _get_client()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=160,
                messages=[{"role": "user", "content": prompt}],
            )
            response = resp.content[0].text.strip()
            upper = response.upper()
            confirme = "CONFIRM" in upper and "IGNORE" not in upper
            lines = response.split("\n")
            raison = next(
                (l.split(":", 1)[1].strip() for l in lines if "REASON:" in l.upper()),
                response[:200],
            )
            return confirme, raison
        except Exception as e2:
            from live.notifier import set_api_alert
            set_api_alert("anthropic", f"Auth expired + refresh failed: {e2}")
            return False, f"Claude indisponible (auth expired) — trade bloqué par sécurité"

    except Exception as e:
        from live.notifier import set_api_alert
        set_api_alert("anthropic", str(e))
        return False, f"Claude indisponible ({e}) — trade bloqué par sécurité"

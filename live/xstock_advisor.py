"""Analyse pré-marché Claude pour toutes les xStocks."""
import os
import json
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import fetch_ohlcv, fetch_news_yfinance, fetch_news_macro_rss, _xstock_ticker
from strategies.supertrend import generate_signals
from live.notifier import notify

SIGNALS_FILE = "logs/signals.jsonl"


def _log_signal(event: str, symbol: str, data: dict):
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "symbol": symbol,
        **data,
    }
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def run_premarket_analysis(
    state: dict,
    btc_context: dict = None,
    vix: float = 0.0,
    fear_greed: dict = None,
):
    """Analyse technique de toutes les xStocks + recommandations Claude → Telegram."""
    summaries = []
    for symbol in config.XSTOCKS:
        try:
            df = fetch_ohlcv(symbol, config.TIMEFRAME, days=45)
            df = generate_signals(df)
            last = df.iloc[-1]
            ticker = _xstock_ticker(symbol)
            sym_news = fetch_news_yfinance(ticker, limit=2, hours=48)
            summaries.append({
                "symbol": symbol,
                "price": round(float(last["close"]), 2),
                "supertrend": "▲" if last["supertrend_dir"] == 1 else "▼",
                "adx": round(float(last["adx"]), 1),
                "rsi": round(float(last["rsi"]), 1),
                "ema_cross": "EMA9>21" if last["f_momentum"] else "EMA9<21",
                "above_ema200": bool(last["f_above_ema200"]),
                "structure": bool(last["f_structure"]),
                "volume_ratio": round(float(last["volume_ratio"]), 2),
                "signal": int(last["signal"]),
                "filters_ok": sum([
                    bool(last["f_trending"]),
                    bool(last["f_above_ema200"]),
                    bool(last["f_structure"]),
                    bool(last["f_momentum"]),
                    bool(last["f_rsi"]),
                    bool(last["f_volume"]),
                ]),
                "news": sym_news,
            })
        except Exception as e:
            summaries.append({"symbol": symbol, "error": str(e)})

    prompt = _build_prompt(summaries, state["capital"], state.get("trades", []), btc_context, vix, fear_greed)
    try:
        from live.claude_filter import _get_client
        client = _get_client()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = resp.content[0].text.strip()
    except Exception as e:
        analysis = f"Erreur Claude SDK — analyse indisponible ({e})"

    _log_signal("PREMARKET_ANALYSIS", "ALL", {
        "summaries": summaries,
        "analysis": analysis,
        "btc_trend": btc_context.get("btc_trend") if btc_context else None,
        "vix": vix,
        "fear_greed_score": fear_greed.get("score") if fear_greed else None,
    })
    notify(f"📈 <b>Analyse pré-marché US</b>\n{analysis}")


def _build_prompt(
    summaries: list,
    capital: float,
    trades: list,
    btc_context: dict = None,
    vix: float = 0.0,
    fear_greed: dict = None,
) -> str:
    # ── News macro (S&P 500 headlines) ──
    macro_news = fetch_news_macro_rss(limit=3)
    macro_str_lines = [f"  • [{n.get('source','')}] {n['title']}" for n in macro_news if n.get("title")]
    macro_section = ("ACTUALITÉS MARCHÉ (S&P 500) :\n" + "\n".join(macro_str_lines)) if macro_str_lines else ""

    # ── Lignes techniques par symbole ──
    lines = []
    for s in summaries:
        if "error" in s:
            lines.append(f"- {s['symbol']} : erreur données ({s['error']})")
            continue
        sig_str = "🟢 BUY" if s["signal"] == 1 else "🔴 SELL" if s["signal"] == -1 else "⚪ neutre"
        line = (
            f"- {s['symbol']} | {s['price']}€ | ST:{s['supertrend']} | ADX:{s['adx']} | "
            f"RSI:{s['rsi']} | {s['ema_cross']} | >EMA200:{s['above_ema200']} | "
            f"Vol×{s['volume_ratio']} | {s['filters_ok']}/6 filtres | {sig_str}"
        )
        sym_news = s.get("news", [])
        if sym_news:
            news_parts = " | ".join(f'"{n["title"][:80]}" ({n.get("age_h","?")}h)' for n in sym_news)
            line += f"\n  News: {news_parts}"
        lines.append(line)

    # ── Contexte BTC ──
    btc_str = "Non disponible"
    if btc_context:
        bt = btc_context.get("btc_trend", "?").upper()
        bp = btc_context.get("btc_price", 0)
        be = btc_context.get("btc_above_ema200", False)
        btc_str = f"BTC {bt} ({bp:.0f}€, {'>' if be else '<'} EMA200)"

    # ── VIX ──
    if vix > 0:
        vix_label = "⚠ PEUR ÉLEVÉE — positions réduites automatiquement" if vix > 25 else "élevé" if vix > 20 else "normal"
        vix_str = f"{vix:.1f} ({vix_label})"
    else:
        vix_str = "N/A"

    # ── Fear & Greed ──
    fg_str = "N/A"
    if fear_greed:
        score = fear_greed.get("score", 50)
        label = fear_greed.get("label", "Neutral")
        fg_str = f"{score}/100 — {label}"
        if score <= 20:
            fg_str += " ⚠ (peur extrême : surveiller les rebonds techniques)"
        elif score >= 80:
            fg_str += " ⚠ (avidité extrême : risque de retournement)"

    # ── Performance récente ──
    recent = trades[-20:] if trades else []
    if recent:
        wins = sum(1 for t in recent if t.get("pnl", 0) > 0)
        wr = f"{wins/len(recent)*100:.0f}% ({wins}/{len(recent)})"
        avg_pnl = f"{sum(t.get('pnl',0) for t in recent)/len(recent):+.2f}€/trade"
    else:
        wr = "N/A (aucun trade fermé)"
        avg_pnl = "N/A"

    return f"""This is an automated pre-market analysis function for a paper trading bot (educational/simulation).
Analyze the technical data below and produce a structured pre-market report.

MACRO CONTEXT:
- {btc_str}
- VIX: {vix_str}
- Crypto Fear & Greed: {fg_str}
- Available capital: {capital:.0f}EUR | Max 3 simultaneous positions
- Recent bot performance: win rate {wr} | Average {avg_pnl} (last 20 trades)
{macro_section}

TECHNICAL DATA (timeframe {config.TIMEFRAME}):
{chr(10).join(lines)}

Respond in French with this exact structure:

TOP OPPORTUNITES (max 3, only if >=4/6 filters AND ST up):
SYMBOL | Entry: X.XX€ | SL: X.XX€ (-X%) | TP: X.XX€ (+X%) | Confidence: high/medium/low
→ Reason (technical catalyst + macro context, 1-2 sentences)

A EVITER:
SYMBOL — short reason (overbought RSI/ST down/low volume/extreme fear)

MARKET CONTEXT:
2-3 sentences on overall tech/AI momentum considering VIX, Fear & Greed, BTC trend.
"""

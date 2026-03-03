"""Analyse pré-marché Claude pour toutes les xStocks."""
import os
import json
import sys
from datetime import datetime

import anthropic

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetcher import fetch_ohlcv
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


def run_premarket_analysis(state: dict):
    """Analyse technique de toutes les xStocks + recommandations Claude → Telegram."""
    summaries = []
    for symbol in config.XSTOCKS:
        try:
            df = fetch_ohlcv(symbol, config.TIMEFRAME, days=45)
            df = generate_signals(df)
            last = df.iloc[-1]
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
            })
        except Exception as e:
            summaries.append({"symbol": symbol, "error": str(e)})

    prompt = _build_prompt(summaries, state["capital"])
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    analysis = response.content[0].text

    _log_signal("PREMARKET_ANALYSIS", "ALL", {"summaries": summaries, "analysis": analysis})
    notify(f"📈 <b>Analyse pré-marché US</b>\n{analysis}")


def _build_prompt(summaries: list, capital: float) -> str:
    lines = []
    for s in summaries:
        if "error" in s:
            lines.append(f"- {s['symbol']} : erreur données ({s['error']})")
            continue
        lines.append(
            f"- {s['symbol']} | {s['price']}€ | Supertrend {s['supertrend']} | "
            f"ADX {s['adx']} | RSI {s['rsi']} | {s['ema_cross']} | "
            f">EMA200:{s['above_ema200']} | Structure:{s['structure']} | "
            f"Vol×{s['volume_ratio']} | {s['filters_ok']}/6 filtres OK"
        )

    return f"""Tu es un analyste technique senior. Le marché US ouvre dans 30 minutes.
Capital disponible : {capital:.0f}€ | Max 3 positions simultanées.

DONNÉES TECHNIQUES xStocks (timeframe 4h, indicateurs Supertrend + EMA + ADX) :
{chr(10).join(lines)}

Réponds en français, structure ta réponse ainsi :

TOP OPPORTUNITÉS (max 3) :
Pour chaque action retenue : niveau d'entrée, SL suggéré, TP, raison principale, confiance (haute/moyenne/faible)

À ÉVITER aujourd'hui :
Actions avec signal faible/range/surachat — raison courte

CONTEXTE GÉNÉRAL :
1-2 phrases sur le momentum global des actions tech/IA aujourd'hui.
"""

"""
Bot D: LLM-Driven Strategy — DeepSeek V3.2 Reasoner (R1)

Améliorations v2:
- Stop loss ATR (2×ATR) + trailing stop automatique
- Pre-filter: n'appelle le LLM que si signal potentiel ou position ouverte
- Filtre heures de marché US pour les xStocks
- 20 bougies dans le prompt (vs 5 avant)
- ATR + stop courant dans le contexte du prompt

Capital: 1000€ | Position size: 100€ fixed | Max positions: 6
"""
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from strategies.supertrend import generate_signals

STATE_FILE = "logs/llm/state.json"
INITIAL_CAPITAL = 1000.0
POSITION_SIZE = 100.0
MAX_POSITIONS = 6
ATR_STOP_MULT = 2.0       # Stop = 2×ATR sous l'entrée
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")
# Pricing DeepSeek Reasoner V3.2
_INPUT_PRICE_PER_M  = 0.28  # $0.28 / 1M tokens
_OUTPUT_PRICE_PER_M = 0.42  # $0.42 / 1M tokens (inclut reasoning tokens)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": INITIAL_CAPITAL,
        "positions": {},
        "trades": [],
        "initial_capital": INITIAL_CAPITAL,
    }


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [BOT-D][{level}] {msg}")
    os.makedirs("logs", exist_ok=True)
    with open("logs/llm.log", "a") as f:
        f.write(f"{ts} [{level}] {msg}\n")


def _is_us_market_open() -> bool:
    try:
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York"))
        return et.weekday() < 5 and 9 * 60 + 30 <= et.hour * 60 + et.minute <= 16 * 60
    except Exception:
        return True


def _build_prompt(symbol: str, df_signals, state: dict, macro: dict) -> str:
    last = df_signals.iloc[-1]
    current_price = float(last["close"])
    st_dir = "UP" if float(last["supertrend_dir"]) == 1 else "DOWN"
    rsi = round(float(last["rsi"]), 1)
    adx = round(float(last["adx"]), 1)
    atr = round(float(last["atr"]), 4)
    ema50 = round(float(last["ema50"]), 4)
    ema200 = round(float(last["ema200"]), 4)
    ema_vs = "EMA50>EMA200 (bullish)" if ema50 > ema200 else "EMA50<EMA200 (bearish)"

    # Last 20 candles (vs 5 before — better trend context)
    recent = df_signals.tail(20)[["open", "high", "low", "close"]].round(4)
    candles_txt = "\n".join(
        f"  {i+1}. O:{row['open']} H:{row['high']} L:{row['low']} C:{row['close']}"
        for i, (_, row) in enumerate(recent.iterrows())
    )

    # Macro
    btc = macro.get("btc_context", {})
    btc_trend = btc.get("btc_trend", "unknown")
    btc_price = btc.get("btc_price", "?")
    vix = macro.get("vix", 0.0)
    fg = macro.get("fear_greed", {"score": 50, "label": "Neutral"})
    fg_score = fg.get("score", 50)
    fg_label = fg.get("label", "Neutral")
    qqq_ok = macro.get("qqq_regime_ok", True)
    qqq_desc = macro.get("qqq_description", "N/A")

    # Portfolio context
    capital = state.get("capital", INITIAL_CAPITAL)
    positions = state.get("positions", {})
    n_positions = len(positions)
    slots_free = MAX_POSITIONS - n_positions
    has_position = symbol in positions
    if has_position:
        pos = positions[symbol]
        entry = pos.get("entry", 0)
        stop = pos.get("stop", 0)
        pnl_pct = (current_price - entry) / entry * 100 if entry > 0 else 0
        position_txt = (
            f"CURRENT POSITION: entry={entry:.4f}€, "
            f"unrealized PnL={pnl_pct:+.1f}%, "
            f"ATR stop={stop:.4f}€ (auto-managed)"
        )
    else:
        position_txt = "No open position"

    trades = state.get("trades", [])
    recent_trades = trades[-20:]
    wins = sum(1 for t in recent_trades if t.get("pnl", 0) > 0)
    win_rate = wins / len(recent_trades) * 100 if recent_trades else 0

    prompt = f"""You are a trading algorithm. Analyze this asset and decide: BUY, SELL, or HOLD.

ASSET: {symbol}
PRICE: {current_price:.4f}€

TECHNICAL INDICATORS:
- Supertrend: {st_dir}
- RSI(14): {rsi}
- ADX(14): {adx}
- ATR(14): {atr:.4f}€
- EMA structure: {ema_vs}

LAST 20 CANDLES (4h):
{candles_txt}

MACRO CONTEXT:
- BTC: {btc_price}€ ({btc_trend})
- VIX: {vix:.1f}
- Fear & Greed: {fg_score}/100 ({fg_label})
- QQQ regime: {'OK' if qqq_ok else 'BEARISH'} ({qqq_desc})

PORTFOLIO:
- Free capital: {capital:.2f}€
- Open positions: {n_positions}/{MAX_POSITIONS} (free slots: {slots_free})
- Win rate (last 20): {win_rate:.0f}%
- {position_txt}

RULES:
- BUY only if: no position AND capital >= 100€ AND free slots > 0 AND Supertrend=UP AND ADX>18
- SELL only if: position exists AND conditions deteriorating (Supertrend DOWN or RSI>75 or macro bearish)
- ATR stop is automatic — do NOT sell just because price dropped slightly
- HOLD in all other cases

Respond ONLY with valid JSON (no markdown):
{{"action":"BUY or SELL or HOLD","confidence":0-100,"reason":"brief explanation"}}"""
    return prompt


def _call_deepseek(prompt: str) -> tuple:
    """Returns (decision_dict, usage_dict)."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
        )
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip("`").strip()
        usage = {
            "input":  response.usage.prompt_tokens,
            "output": response.usage.completion_tokens,
        }
        return json.loads(raw), usage
    except Exception as e:
        log(f"DeepSeek API error: {e}", "WARN")
        from live.notifier import is_credit_error, set_api_alert, clear_api_alert
        if is_credit_error(e):
            set_api_alert("deepseek", str(e))
        else:
            clear_api_alert("deepseek")
        return {"action": "HOLD", "confidence": 0, "reason": f"error: {e}"}, {"input": 0, "output": 0}


def run_llm_cycle(state: dict, ohlcv_4h: dict, macro: dict) -> dict:
    """Run one cycle of the LLM-driven strategy."""
    for symbol in config.SYMBOLS:
        df = ohlcv_4h.get(symbol)
        if df is None or len(df) < 50:
            log(f"{symbol} — Insufficient data, skipping", "WARN")
            continue

        try:
            df_signals = generate_signals(df)
            if df_signals.empty:
                continue
        except Exception as e:
            log(f"{symbol} — generate_signals error: {e}", "WARN")
            continue

        last = df_signals.iloc[-1]
        current_price = float(last["close"])
        atr = float(last["atr"])
        position = state["positions"].get(symbol)

        # ── 1. Trailing stop update ──
        if position and "stop" in position:
            new_stop = round(current_price - ATR_STOP_MULT * atr, 4)
            if new_stop > position["stop"]:
                position["stop"] = new_stop
                state["positions"][symbol] = position

        # ── 2. Stop loss check (avant d'appeler le LLM) ──
        if position:
            stop = position.get("stop", 0)
            if stop > 0 and current_price <= stop:
                exit_price = stop * (1 - config.SLIPPAGE)
                fee = exit_price * position["size"] * config.EXCHANGE_FEE
                proceeds = exit_price * position["size"] - fee
                pnl = proceeds - position["cost"]
                state["capital"] += proceeds
                state["trades"].append({
                    "symbol": symbol,
                    "entry_date": position["date"],
                    "exit_date": str(datetime.now()),
                    "entry_price": position["entry"],
                    "exit_price": round(exit_price, 4),
                    "pnl": round(pnl, 2),
                    "reason": "stop_loss_atr",
                    "result": "win" if pnl > 0 else "loss",
                })
                state["positions"].pop(symbol)
                log(
                    f"{'✓' if pnl > 0 else '✗'} STOP {symbol} | "
                    f"{position['entry']:.4f}€ → {exit_price:.4f}€ | PnL: {pnl:+.2f}€",
                    "SELL",
                )
                continue

        # ── 3. Pre-filter: n'appelle le LLM que si pertinent ──
        supertrend_up = float(last["supertrend_dir"]) == 1
        adx_ok = float(last["adx"]) > 18
        has_position = symbol in state["positions"]

        if not has_position and not (supertrend_up and adx_ok):
            continue  # HOLD implicite — pas d'appel API

        # ── 4. Filtre heures de marché US pour les xStocks ──
        if not has_position and symbol in config.XSTOCKS and not _is_us_market_open():
            log(f"{symbol} — Marché US fermé, BUY ignoré")
            continue

        prompt = _build_prompt(symbol, df_signals, state, macro)
        decision, usage = _call_deepseek(prompt)

        # ── Token tracking ──
        stats = state.setdefault("token_stats", {
            "total_input": 0, "total_output": 0, "total_calls": 0, "est_cost_usd": 0.0
        })
        stats["total_input"]  += usage["input"]
        stats["total_output"] += usage["output"]
        stats["total_calls"]  += 1
        stats["est_cost_usd"] = round(
            stats["est_cost_usd"]
            + usage["input"]  / 1_000_000 * _INPUT_PRICE_PER_M
            + usage["output"] / 1_000_000 * _OUTPUT_PRICE_PER_M,
            4
        )

        action = decision.get("action", "HOLD").upper()
        confidence = decision.get("confidence", 0)
        reason = decision.get("reason", "")

        log(
            f"{symbol} | {current_price:.4f}€ | {action} (conf={confidence}) | {reason} "
            f"[↑{usage['input']}+{usage['output']} tok | cumul ${stats['est_cost_usd']:.3f}]"
        )

        # ── SELL ──
        if action == "SELL" and symbol in state["positions"]:
            pos = state["positions"][symbol]
            exit_price = current_price * (1 - config.SLIPPAGE)
            fee = exit_price * pos["size"] * config.EXCHANGE_FEE
            proceeds = exit_price * pos["size"] - fee
            pnl = proceeds - pos["cost"]
            state["capital"] += proceeds
            state["trades"].append({
                "symbol": symbol,
                "entry_date": pos["date"],
                "exit_date": str(datetime.now()),
                "entry_price": pos["entry"],
                "exit_price": round(exit_price, 4),
                "pnl": round(pnl, 2),
                "reason": f"llm_sell | {reason[:80]}",
                "result": "win" if pnl > 0 else "loss",
                "confidence": confidence,
            })
            state["positions"].pop(symbol)
            log(
                f"{'✓' if pnl > 0 else '✗'} CLOSE {symbol} | "
                f"{pos['entry']:.4f}€ → {exit_price:.4f}€ | PnL: {pnl:+.2f}€",
                "SELL",
            )

        # ── BUY ──
        elif action == "BUY" and symbol not in state["positions"]:
            if len(state["positions"]) >= MAX_POSITIONS:
                log(f"{symbol} — BUY blocked: max positions ({MAX_POSITIONS}) reached")
            elif state["capital"] < POSITION_SIZE:
                log(f"{symbol} — BUY blocked: insufficient capital ({state['capital']:.2f}€)")
            else:
                entry_price = current_price * (1 + config.SLIPPAGE)
                size = POSITION_SIZE / (entry_price * (1 + config.EXCHANGE_FEE))
                fee = entry_price * size * config.EXCHANGE_FEE
                total_cost = size * entry_price + fee
                stop_loss = round(entry_price - ATR_STOP_MULT * atr, 4)
                state["capital"] -= total_cost
                state["positions"][symbol] = {
                    "entry": round(entry_price, 4),
                    "size": round(size, 6),
                    "cost": round(total_cost, 4),
                    "date": str(datetime.now()),
                    "confidence": confidence,
                    "stop": stop_loss,
                    "atr": round(atr, 4),
                }
                log(
                    f"▲ BUY {symbol} | {entry_price:.4f}€ | {size:.6f} units | "
                    f"SL: {stop_loss:.4f}€ (2×ATR) | Cost: {total_cost:.2f}€ | conf={confidence}",
                    "BUY",
                )

        time.sleep(2)

    return state

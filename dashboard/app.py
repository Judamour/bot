"""
Dashboard Flask — Bot Trading
Sert le dashboard sur http://localhost:5000
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, "logs", "paper_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs", "bot.log")

app = Flask(__name__)
app.config["SECRET_KEY"] = "bot-trading-dashboard"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "capital": config.PAPER_CAPITAL,
        "positions": {},
        "trades": [],
        "initial_capital": config.PAPER_CAPITAL,
    }


def compute_metrics(state: dict, live_prices: dict) -> dict:
    """Calcule les métriques du portfolio."""
    capital = state.get("capital", config.PAPER_CAPITAL)
    initial = state.get("initial_capital", config.PAPER_CAPITAL)
    positions = state.get("positions", {})
    trades = state.get("trades", [])

    # Valeur totale (capital libre + valeur positions au prix live)
    positions_value = 0.0
    positions_detail = []
    for symbol, pos in positions.items():
        price = live_prices.get(symbol, pos.get("entry", 0))
        value = price * pos.get("size", 0)
        cost = pos.get("entry", 0) * pos.get("size", 0)
        pnl_pct = ((price - pos["entry"]) / pos["entry"] * 100) if pos.get("entry") else 0
        positions_value += value
        positions_detail.append({
            "symbol": symbol,
            "entry": pos.get("entry", 0),
            "current_price": price,
            "size": pos.get("size", 0),
            "value": round(value, 2),
            "pnl_eur": round(value - cost, 2),
            "pnl_pct": round(pnl_pct, 2),
            "stop": pos.get("stop", 0),
            "tp": pos.get("tp", 0),
            "date": pos.get("date", ""),
        })

    total_value = capital + positions_value
    pnl_pct = ((total_value - initial) / initial * 100) if initial else 0
    pnl_eur = total_value - initial

    # Win rate
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0

    # Drawdown max (sur l'equity curve des trades fermés)
    max_dd = 0.0
    if trades:
        equity = initial
        peak = initial
        for t in trades:
            equity += t.get("pnl", 0)
            if equity > peak:
                peak = equity
            dd = (equity - peak) / peak * 100 if peak else 0
            if dd < max_dd:
                max_dd = dd

    return {
        "capital": round(capital, 2),
        "total_value": round(total_value, 2),
        "initial_capital": round(initial, 2),
        "pnl_pct": round(pnl_pct, 2),
        "pnl_eur": round(pnl_eur, 2),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_dd, 2),
        "open_trades": len(positions),
        "total_trades": len(trades),
        "positions": positions_detail,
        "paper_mode": config.PAPER_TRADING,
    }


def compute_equity_curve(state: dict) -> list:
    """Construit la courbe d'equity à partir des trades fermés."""
    trades = state.get("trades", [])
    initial = state.get("initial_capital", config.PAPER_CAPITAL)
    equity = initial
    curve = [{"date": "", "value": initial}]
    for t in sorted(trades, key=lambda x: x.get("exit_date", "")):
        equity += t.get("pnl", 0)
        curve.append({"date": t.get("exit_date", ""), "value": round(equity, 2)})
    return curve


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", symbols=config.SYMBOLS, timeframe=config.TIMEFRAME)


@app.route("/api/state")
def api_state():
    state = load_state()
    metrics = compute_metrics(state, _live_prices)
    metrics["equity_curve"] = compute_equity_curve(state)
    metrics["trades"] = list(reversed(state.get("trades", [])))[:50]
    return jsonify(metrics)


@app.route("/api/prices/<path:symbol>")
def api_prices(symbol):
    """Retourne les données OHLCV + indicateurs pour le graphique."""
    try:
        import ccxt
        from strategies.supertrend import generate_signals

        symbol_decoded = symbol.replace("-", "/")
        binance_symbol = symbol_decoded.split("/")[0] + "/USDT"

        exchange = ccxt.binance({"enableRateLimit": True})
        since = exchange.parse8601(
            (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        candles = exchange.fetch_ohlcv(binance_symbol, config.TIMEFRAME, since=since, limit=500)

        import pandas as pd
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)

        df = generate_signals(df)

        result = []
        for ts, row in df.iterrows():
            result.append({
                "time": int(ts.timestamp()),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
                "volume": round(float(row["volume"]), 4),
                "supertrend": round(float(row["supertrend"]), 4) if "supertrend" in row.index and not pd.isna(row["supertrend"]) else None,
                "ema_fast": round(float(row["ema50"]), 4) if "ema50" in row.index and not pd.isna(row["ema50"]) else None,
                "ema_slow": None,
                "ema_trend": round(float(row["ema200"]), 4) if "ema200" in row.index and not pd.isna(row["ema200"]) else None,
                "signal": int(row["signal"]) if "signal" in row else 0,
            })

        return jsonify({"symbol": symbol_decoded, "timeframe": config.TIMEFRAME, "data": result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
def api_log():
    lines = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                lines = f.readlines()[-50:]
        except Exception:
            pass
    return jsonify({"lines": [l.rstrip() for l in lines]})


# ── Background Thread ─────────────────────────────────────────────────────────

_live_prices: dict = {}
_state_mtime: float = 0.0
_log_size: int = 0


def background_thread():
    """Surveille les fichiers et émet les mises à jour WebSocket."""
    global _state_mtime, _log_size, _live_prices

    import ccxt
    exchange = ccxt.binance({"enableRateLimit": True})

    while True:
        try:
            # ── Prix live (toutes les 10s) ──
            prices = {}
            for symbol in config.SYMBOLS:
                binance_sym = symbol.split("/")[0] + "/USDT"
                try:
                    ticker = exchange.fetch_ticker(binance_sym)
                    prices[symbol] = ticker["last"]
                except Exception:
                    pass

            if prices:
                _live_prices.update(prices)
                socketio.emit("price_update", prices)

            # ── État du portfolio ──
            if os.path.exists(STATE_FILE):
                mtime = os.path.getmtime(STATE_FILE)
                if mtime != _state_mtime:
                    _state_mtime = mtime
                    state = load_state()
                    metrics = compute_metrics(state, _live_prices)
                    metrics["equity_curve"] = compute_equity_curve(state)
                    socketio.emit("state_update", metrics)

            # ── Nouvelles lignes de log ──
            if os.path.exists(LOG_FILE):
                size = os.path.getsize(LOG_FILE)
                if size != _log_size:
                    _log_size = size
                    with open(LOG_FILE) as f:
                        lines = f.readlines()[-10:]
                    socketio.emit("log_update", {"lines": [l.rstrip() for l in lines]})

        except Exception as e:
            pass

        time.sleep(10)


def run(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """Lance le serveur Flask."""
    t = threading.Thread(target=background_thread, daemon=True)
    t.start()

    print(f"\n  Dashboard démarré → http://localhost:{port}\n")
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)

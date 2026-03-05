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

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE   = os.path.join(BASE_DIR, "logs", "paper_state.json")
LOG_FILE     = os.path.join(BASE_DIR, "logs", "bot.log")
SIGNALS_FILE = os.path.join(BASE_DIR, "logs", "signals.jsonl")

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

    # Profit factor
    gross_profit = sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    # Avg trade
    avg_trade = round(sum(t.get("pnl", 0) for t in trades) / len(trades), 2) if trades else 0.0

    # Sharpe ratio (annualisé, approximatif sur les PnL des trades)
    pnls = [t.get("pnl", 0) for t in trades]
    if len(pnls) > 1:
        avg_pnl = sum(pnls) / len(pnls)
        std_pnl = (sum((p - avg_pnl) ** 2 for p in pnls) / (len(pnls) - 1)) ** 0.5
        sharpe = round(avg_pnl / std_pnl * (252 ** 0.5), 2) if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "capital": round(capital, 2),
        "total_value": round(total_value, 2),
        "initial_capital": round(initial, 2),
        "pnl_pct": round(pnl_pct, 2),
        "pnl_eur": round(pnl_eur, 2),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": profit_factor,
        "avg_trade": avg_trade,
        "open_trades": len(positions),
        "total_trades": len(trades),
        "positions": positions_detail,
        "paper_mode": config.PAPER_TRADING,
        "sharpe_ratio": sharpe,
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
    return render_template("index.html", symbols=config.SYMBOLS, xstocks=config.XSTOCKS, timeframe=config.TIMEFRAME)


@app.route("/api/state")
def api_state():
    state = load_state()
    metrics = compute_metrics(state, _live_prices)
    metrics["equity_curve"] = compute_equity_curve(state)
    metrics["trades"] = list(reversed(state.get("trades", [])))[:50]
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo("America/New_York"))
    metrics["us_market_open"] = et.weekday() < 5 and 9 * 60 + 30 <= et.hour * 60 + et.minute <= 16 * 60
    metrics["fear_greed"] = _fear_greed_cache
    metrics["vix"] = _vix_cache
    return jsonify(metrics)


@app.route("/api/health")
def api_health():
    import subprocess
    try:
        bot_active = subprocess.run(
            ["systemctl", "is-active", "bot"], capture_output=True, text=True
        ).returncode == 0
    except Exception:
        bot_active = os.path.exists(LOG_FILE)
    last_analysis = None
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                for line in reversed(f.readlines()):
                    if "Analyse en cours" in line:
                        last_analysis = line.strip()[:19]
                        break
        except Exception:
            pass
    # Prochain cycle (heures UTC fixes : 03 07 11 15 19 23)
    CYCLE_HOURS_UTC = [3, 7, 11, 15, 19, 23]
    now_utc = datetime.utcnow()
    next_run = None
    for h in CYCLE_HOURS_UTC:
        candidate = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now_utc:
            next_run = candidate
            break
    if next_run is None:
        next_run = (now_utc + timedelta(days=1)).replace(
            hour=CYCLE_HOURS_UTC[0], minute=0, second=0, microsecond=0
        )
    seconds_until = max(0, int((next_run - now_utc).total_seconds()))

    state = load_state()
    return jsonify({
        "bot_running": bot_active,
        "last_analysis": last_analysis,
        "next_analysis_utc": next_run.isoformat(),
        "seconds_until_next": seconds_until,
        "open_positions": len(state.get("positions", {})),
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/api/prices/<path:symbol>")
def api_prices(symbol):
    """Retourne les données OHLCV + indicateurs pour le graphique (cache 5 min)."""
    try:
        import pandas as pd
        from strategies.supertrend import generate_signals

        symbol_decoded = symbol.replace("-", "/")

        cached = _chart_cache.get(symbol_decoded)
        if cached and (time.time() - cached[0]) < CHART_CACHE_TTL:
            return jsonify(cached[1])

        if symbol_decoded in config.XSTOCKS:
            # xStocks via yfinance
            from data.fetcher import fetch_yfinance_ohlcv
            df = fetch_yfinance_ohlcv(symbol_decoded, config.TIMEFRAME, days=60)
            df = generate_signals(df)
        else:
            # Crypto via Binance
            import ccxt
            exchange = ccxt.binance({"enableRateLimit": True})
            binance_symbol = symbol_decoded.split("/")[0] + "/USDT"
            since = exchange.parse8601(
                (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            candles = exchange.fetch_ohlcv(binance_symbol, config.TIMEFRAME, since=since, limit=500)
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

        payload = {"symbol": symbol_decoded, "timeframe": config.TIMEFRAME, "data": result}
        _chart_cache[symbol_decoded] = (time.time(), payload)
        return jsonify(payload)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/system")
def api_system():
    """Retourne les métriques système du serveur."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return jsonify({
            "cpu_pct": round(cpu, 1),
            "mem_used_mb": round(mem.used / 1024 / 1024),
            "mem_total_mb": round(mem.total / 1024 / 1024),
            "mem_pct": round(mem.percent, 1),
            "disk_pct": round(disk.percent, 1),
        })
    except ImportError:
        return jsonify({"error": "psutil non installé"}), 500


@app.route("/api/signals")
def api_signals():
    """Retourne les derniers événements du signals.jsonl pour analyse."""
    records = []
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE) as f:
                lines = f.readlines()[-200:]
            for line in lines:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
        except Exception:
            pass
    return jsonify({"signals": records})


@app.route("/api/claude")
def api_claude():
    """Retourne les derniers événements Claude (filtres BUY + analyses pré-marché)."""
    events = []
    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE) as f:
                lines = f.readlines()[-500:]
            for line in lines:
                try:
                    rec = json.loads(line)
                    if rec.get("event") in ("CLAUDE_FILTER", "PREMARKET_ANALYSIS"):
                        events.append(rec)
                except Exception:
                    pass
        except Exception:
            pass
    return jsonify({"events": list(reversed(events))[:50]})


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
_chart_cache: dict = {}          # {symbol: (timestamp, payload)}
_fear_greed_cache: dict = {"score": None, "label": "—"}
_vix_cache: float = 0.0
CHART_CACHE_TTL = 300            # 5 minutes
POLL_INTERVAL = 30               # secondes entre chaque poll prix/état
FG_POLL_INTERVAL = 300           # Fear & Greed toutes les 5 min


def background_thread():
    """Surveille les fichiers et émet les mises à jour WebSocket."""
    global _state_mtime, _log_size, _live_prices, _fear_greed_cache, _vix_cache
    _fg_last_poll = 0

    import ccxt
    binance = ccxt.binance({"enableRateLimit": True})
    kraken  = ccxt.kraken({"enableRateLimit": True})

    while True:
        try:
            # ── Prix live (toutes les 30s) ──
            prices = {}
            # Crypto via Binance
            for symbol in config.CRYPTO:
                try:
                    ticker = binance.fetch_ticker(symbol.split("/")[0] + "/USDT")
                    prices[symbol] = ticker["last"]
                except Exception:
                    pass
            # xStocks via Kraken (NVDAx/EUR...)
            for symbol in config.XSTOCKS:
                try:
                    ticker = kraken.fetch_ticker(symbol)
                    prices[symbol] = ticker["last"]
                except Exception:
                    pass

            if prices:
                _live_prices.update(prices)
                socketio.emit("price_update", prices)

            # ── Fear & Greed + VIX (toutes les 5 min) ──
            now = time.time()
            if now - _fg_last_poll >= FG_POLL_INTERVAL:
                _fg_last_poll = now
                try:
                    import requests as _req
                    r = _req.get("https://api.alternative.me/fng/?limit=1", timeout=5)
                    d = r.json()["data"][0]
                    _fear_greed_cache.update({"score": int(d["value"]), "label": d["value_classification"]})
                except Exception:
                    pass
                try:
                    import yfinance as _yf
                    vix_df = _yf.Ticker("^VIX").history(period="2d", interval="1h")
                    if not vix_df.empty:
                        _vix_cache = round(float(vix_df["Close"].iloc[-1]), 1)
                except Exception:
                    pass
                socketio.emit("sentiment_update", {"fear_greed": _fear_greed_cache, "vix": _vix_cache})

            # ── État du portfolio ──
            if os.path.exists(STATE_FILE):
                mtime = os.path.getmtime(STATE_FILE)
                if mtime != _state_mtime:
                    _state_mtime = mtime
                    state = load_state()
                    metrics = compute_metrics(state, _live_prices)
                    metrics["equity_curve"] = compute_equity_curve(state)
                    metrics["trades"] = list(reversed(state.get("trades", [])))[:50]
                    socketio.emit("state_update", metrics)

            # ── Nouvelles lignes de log ──
            if os.path.exists(LOG_FILE):
                size = os.path.getsize(LOG_FILE)
                if size != _log_size:
                    _log_size = size
                    with open(LOG_FILE) as f:
                        lines = f.readlines()[-10:]
                    socketio.emit("log_update", {"lines": [l.rstrip() for l in lines]})

        except Exception:
            pass

        time.sleep(POLL_INTERVAL)


def run(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """Lance le serveur Flask."""
    t = threading.Thread(target=background_thread, daemon=True)
    t.start()

    print(f"\n  Dashboard démarré → http://localhost:{port}\n")
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    run()

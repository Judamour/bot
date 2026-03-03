import os
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ────────────────────────────────────────────────────────────────
EXCHANGE = "kraken"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ── Paires tradées ──────────────────────────────────────────────────────────
# Format Kraken : "BTC/EUR", "ETH/EUR", "SOL/EUR"
# BTC/ETH exclus car en bear market prolongé (2025) — réactiver lors du prochain bull run
SYMBOLS = ["SOL/EUR", "BNB/EUR"]

# ── Timeframe ───────────────────────────────────────────────────────────────
# Options : "1m", "5m", "15m", "1h", "4h", "1d"
TIMEFRAME = "4h"  # 4h = équilibre qualité/quantité pour Supertrend

# ── Stratégie EMA Cross ─────────────────────────────────────────────────────
EMA_FAST = 9        # EMA rapide
EMA_SLOW = 21       # EMA lente
EMA_TREND = 200     # EMA tendance longue — on achète seulement si prix > EMA200
RSI_PERIOD = 14     # Période RSI
RSI_OVERBOUGHT = 75 # RSI > 75 → on n'achète pas
RSI_OVERSOLD = 25   # RSI < 25 → seuil de survente
ADX_PERIOD = 14     # Période ADX
ADX_THRESHOLD = 20  # ADX > 20 = tendance suffisante

# ── Gestion du risque ───────────────────────────────────────────────────────
RISK_PER_TRADE = 0.02    # 2% du capital max par trade
ATR_MULTIPLIER = 3.0     # Stop-loss = 3x ATR sous le prix d'entrée (adapté au 4h)
TAKE_PROFIT_RATIO = 2.5  # Take-profit = 2.5x le stop-loss (ratio R:R = 1:2.5)
MAX_OPEN_TRADES = 3      # Maximum de trades ouverts simultanément

# ── Paper trading ───────────────────────────────────────────────────────────
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_CAPITAL = float(os.getenv("PAPER_CAPITAL", "1000"))

# ── Données historiques pour backtest ──────────────────────────────────────
BACKTEST_DAYS = 1095  # 3 ans d'historique pour une validation statistique solide

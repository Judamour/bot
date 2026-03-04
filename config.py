import os
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ────────────────────────────────────────────────────────────────
EXCHANGE = "kraken"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ── Paires tradées ──────────────────────────────────────────────────────────
# Format Kraken : "BTC/EUR", "ETH/EUR", "SOL/EUR"
CRYPTO = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "BNB/EUR", "ADA/EUR", "DOT/EUR", "LINK/EUR", "AVAX/EUR"]

# xStocks : actions tokenisées sur Kraken, tradées en EUR 24/7
# Symboles Kraken : suffixe "x" (ex: NVDAx/EUR)
XSTOCKS = [
    "NVDAx/EUR", "AAPLx/EUR", "TSLAx/EUR", "MSFTx/EUR",
    "METAx/EUR", "AMZNx/EUR", "GOOGx/EUR",
]

SYMBOLS = CRYPTO + XSTOCKS

# ── Heures marché US (Eastern Time — gère automatiquement EST/EDT) ──────────
XSTOCK_MARKET_OPEN_ET  = (9, 30)    # NYSE/NASDAQ ouverture (9h30 ET)
XSTOCK_MARKET_CLOSE_ET = (16,  0)   # Fermeture (16h00 ET)
XSTOCK_PREMARKET_ET    = (8,   0)   # Analyse pré-marché (8h00 ET = 14h CET = 15h CEST)

# ── Gestion du risque portefeuille ──────────────────────────────────────────
MAX_DRAWDOWN = -0.15    # Coupe-circuit si capital chute de -15% depuis le départ

# ── Secteurs (corrélation positions — max 1 par secteur) ────────────────────
SECTORS = {
    "NVDAx/EUR": "tech",     "AAPLx/EUR": "tech",
    "MSFTx/EUR": "tech",     "METAx/EUR": "tech",
    "GOOGx/EUR": "tech",     "AMZNx/EUR": "ecommerce",
    "TSLAx/EUR": "auto",
    "BTC/EUR":   "crypto",   "ETH/EUR":   "crypto",
    "SOL/EUR":   "crypto",   "BNB/EUR":   "crypto",
    "ADA/EUR":   "crypto",   "DOT/EUR":   "crypto",
    "LINK/EUR":  "crypto",   "AVAX/EUR":  "crypto",
}

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
POSITION_SIZE_EUR = 100  # Montant fixe investi par position (en EUR)
ATR_MULTIPLIER = 3.0     # Stop-loss = 3x ATR sous le prix d'entrée (adapté au 4h)
TAKE_PROFIT_RATIO = 2.5  # Take-profit = 2.5x le stop-loss (ratio R:R = 1:2.5)
MAX_OPEN_TRADES = 3      # Maximum de trades ouverts simultanément

# ── Coûts de transaction ────────────────────────────────────────────────────
EXCHANGE_FEE = 0.0026   # 0.26% taker Kraken (pire cas)
SLIPPAGE     = 0.001    # 0.10% slippage moyen estimé

# ── Paper trading ───────────────────────────────────────────────────────────
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_CAPITAL = float(os.getenv("PAPER_CAPITAL", "1000"))

# ── Données historiques pour backtest ──────────────────────────────────────
BACKTEST_DAYS = 1095  # 3 ans d'historique pour une validation statistique solide

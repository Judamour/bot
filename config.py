import os
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ────────────────────────────────────────────────────────────────
EXCHANGE = "kraken"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ── Paires tradées ──────────────────────────────────────────────────────────
# Format Kraken : "BTC/EUR", "ETH/EUR", "SOL/EUR"
CRYPTO = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "BNB/EUR", "TON/EUR"]
# Retirés : LINK/EUR (PF<1 sur 1 et 3 ans), AVAX/EUR (idem)

# xStocks : actions tokenisées sur Kraken, tradées en EUR 24/7
# Symboles Kraken : suffixe "x" (ex: NVDAx/EUR)
XSTOCKS = [
    "NVDAx/EUR", "AAPLx/EUR", "MSFTx/EUR",
    "METAx/EUR", "GOOGx/EUR",
    "PLTRx/EUR", "AMDx/EUR", "AVGOx/EUR",
    "GLDx/EUR", "NFLXx/EUR", "CRWDx/EUR",
]
# Retirés : TSLAx/EUR (WR=20%, Musk volatility), AMZNx/EUR (PF=0.07)

SYMBOLS = CRYPTO + XSTOCKS

# ── Heures marché US (Eastern Time — gère automatiquement EST/EDT) ──────────
XSTOCK_MARKET_OPEN_ET  = (9, 30)    # NYSE/NASDAQ ouverture (9h30 ET)
XSTOCK_MARKET_CLOSE_ET = (16,  0)   # Fermeture (16h00 ET)
XSTOCK_PREMARKET_ET    = (8,   0)   # Analyse pré-marché (8h00 ET = 14h CET = 15h CEST)

# ── Gestion du risque portefeuille ──────────────────────────────────────────
MAX_DRAWDOWN = -0.15    # Coupe-circuit si capital chute de -15% depuis le départ

# ── Secteurs (corrélation positions — max 1 par secteur) ────────────────────
SECTORS = {
    "NVDAx/EUR": "tech",       "AAPLx/EUR": "tech",
    "MSFTx/EUR": "tech",       "METAx/EUR": "tech",
    "GOOGx/EUR": "tech",       "AMZNx/EUR": "ecommerce",
    "TSLAx/EUR": "auto",
    "PLTRx/EUR": "ai_data",
    "AMDx/EUR":  "semis",      "AVGOx/EUR": "semis",
    "GLDx/EUR":  "gold",       "NFLXx/EUR": "media",
    "CRWDx/EUR": "cybersec",
    "BTC/EUR":   "crypto",     "ETH/EUR":   "crypto",
    "SOL/EUR":   "crypto",     "BNB/EUR":   "crypto",
    "TON/EUR":   "crypto",
}

# ── Timeframe ───────────────────────────────────────────────────────────────
# Options : "1m", "5m", "15m", "1h", "4h", "1d"
TIMEFRAME = "4h"  # 4h = équilibre qualité/quantité pour Supertrend

# ── Stratégie EMA Cross ─────────────────────────────────────────────────────
EMA_FAST = 9        # EMA rapide
EMA_SLOW = 21       # EMA lente
EMA_TREND = 200     # EMA tendance longue — on achète seulement si prix > EMA200
RSI_PERIOD = 14     # Période RSI
RSI_OVERBOUGHT = 70 # RSI > 70 → on n'achète pas
RSI_OVERSOLD = 25   # RSI < 25 → seuil de survente
ADX_PERIOD = 14     # Période ADX
ADX_THRESHOLD = 22  # ADX > 22 = tendance suffisante

# ── Gestion du risque ───────────────────────────────────────────────────────
POSITION_SIZE_PCT = 0.15  # 15% du capital disponible par position
POSITION_MIN_EUR  = 20    # Plancher absolu (évite des positions ridicules)
ATR_MULTIPLIER = 3.0     # Stop-loss = 3x ATR (trend following)
TAKE_PROFIT_RATIO = 3.0  # (référence calcul, non utilisé en live — trailing stop)
MAX_OPEN_TRADES = 6      # Maximum de trades ouverts simultanément
TARGET_VOL   = 0.15     # Volatilité annualisée cible (15%) pour le vol targeting
MAX_LEVERAGE = 1.3      # Exposition max (×1.3 position de base)

# ── Mean Reversion (RSI 2 — marché en range) ────────────────────────────────
MR_RSI_ENTRY       = 10   # RSI(2) < 10 → signal d'achat mean reversion
MR_RSI_EXIT        = 90   # RSI(2) > 90 → sortie mean reversion
MR_ATR_MULTIPLIER  = 1.0  # Stop serré : 1×ATR (vs 3×ATR pour trend)

# ── Coûts de transaction ────────────────────────────────────────────────────
EXCHANGE_FEE = 0.0026   # 0.26% taker Kraken (pire cas)
SLIPPAGE     = 0.001    # 0.10% slippage moyen estimé

# ── Paper trading ───────────────────────────────────────────────────────────
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_CAPITAL = float(os.getenv("PAPER_CAPITAL", "1000"))

# ── Données historiques pour backtest ──────────────────────────────────────
BACKTEST_DAYS = 1095  # 3 ans d'historique pour une validation statistique solide

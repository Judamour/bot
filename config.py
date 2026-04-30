import os
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ────────────────────────────────────────────────────────────────
EXCHANGE = "kraken"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ── Paires tradées ──────────────────────────────────────────────────────────
# MIGRATION 2026-04-30 : EUR → USD pour pouvoir trader les xStocks via API Kraken.
# xStocks Kraken n'existent QU'EN /USD via l'API spot (vérifié AssetPairs?aclass=tokenized_asset).
# Cryptos : on passe aussi en /USD pour cohérence (Binance source = USDT ≈ USD).
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "TON/USD"]

# xStocks Kraken : suffixe "x" minuscule, tradés 24/5 en USD
# Sur Kraken API : NVDAxUSD (sans slash), wsname NVDAx/USD
XSTOCKS = [
    "NVDAx/USD", "AAPLx/USD", "MSFTx/USD",
    "METAx/USD", "GOOGLx/USD",
    "PLTRx/USD", "AMDx/USD", "AVGOx/USD",
    "GLDx/USD", "NFLXx/USD", "CRWDx/USD",
]

SYMBOLS = CRYPTO + XSTOCKS

# ── Heures marché US (Eastern Time — gère automatiquement EST/EDT) ──────────
XSTOCK_MARKET_OPEN_ET  = (9, 30)    # NYSE/NASDAQ ouverture (9h30 ET)
XSTOCK_MARKET_CLOSE_ET = (16,  0)   # Fermeture (16h00 ET)
XSTOCK_PREMARKET_ET    = (8,   0)   # Analyse pré-marché (8h00 ET = 14h CET = 15h CEST)

# ── Gestion du risque portefeuille ──────────────────────────────────────────
MAX_DRAWDOWN = -0.15    # Coupe-circuit si capital chute de -15% depuis le départ

# ── Secteurs (corrélation positions — max MAX_PER_SECTOR par secteur) ───────
# 1 → 2 : on bloquait NVDA quand AAPL ouvert, BTC quand ETH ouvert. Rate les
# rallies sectoriels groupés (NVDA +25%, BTC +20% avril 2026).
MAX_PER_SECTOR = 2
SECTORS = {
    "NVDAx/USD": "tech",       "AAPLx/USD": "tech",
    "MSFTx/USD": "tech",       "METAx/USD": "tech",
    "GOOGLx/USD": "tech",
    "PLTRx/USD": "ai_data",
    "AMDx/USD":  "semis",      "AVGOx/USD": "semis",
    "GLDx/USD":  "gold",       "NFLXx/USD": "media",
    "CRWDx/USD": "cybersec",
    "BTC/USD":   "crypto",     "ETH/USD":   "crypto",
    "SOL/USD":   "crypto",     "BNB/USD":   "crypto",
    "TON/USD":   "crypto",
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
POSITION_SIZE_PCT = 0.20  # 20% du total portfolio (cash + positions MTM) par position
# Plancher position en % du capital (au lieu d'un montant € hardcodé)
# Avec 91€ : floor = 4.5€ ; avec 10000€ : floor = 500€
POSITION_MIN_PCT  = 0.05  # 5% du capital initial = floor relatif
ATR_MULTIPLIER = 3.0     # Stop-loss = 3x ATR (trend following)
TAKE_PROFIT_RATIO = 3.0  # (référence calcul, non utilisé en live — trailing stop)
MAX_OPEN_TRADES = 10     # Maximum de trades ouverts simultanément (8 → 10 pour exploiter 16 symboles + sector=2)
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

# ── Capital total bot (capital-agnostic) ────────────────────────────────────
# Bot Z dispatche INITIAL_CAPITAL aux 4 sub-bots (A/B/C/G) selon engine actif.
# En paper : 10000€ par défaut. En live : matche ton solde Kraken (.env).
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000"))
INITIAL_CAPITAL_PER_BOT = float(os.getenv("INITIAL_CAPITAL_PER_BOT", str(INITIAL_CAPITAL / 10)))

# ── Kill switch global (sécurité absolue, ferme tout au-delà) ───────────────
# -10% par défaut. Active une fermeture forcée + freeze quand DD ≤ ce seuil.
KILL_SWITCH_PCT = float(os.getenv("KILL_SWITCH_PCT", "-0.10"))

# ── Min order Kraken (skip tentative si budget < seuil) ─────────────────────
# Évite "insufficient funds" loops sur small capital
MIN_ORDER_EUR = float(os.getenv("MIN_ORDER_EUR", "5.0"))

# ── Bots actifs (autres = désactivés, ne tradent pas) ───────────────────────
# Audit 55 jours paper : Bot A 33 trades, B 4, C/G/H/I/J 0 trades.
# On garde les bots avec preuve d'activité ou utilité défensive (J = mean rev).
# Bot G désactivé temporairement (à investiguer — backtest 70+ trades, live 0).
# Override via env : ACTIVE_BOTS="a,j" ou "a,b,c,g,h,i,j" pour tous.
ACTIVE_BOTS = [b.strip().lower() for b in os.getenv("ACTIVE_BOTS", "a,j").split(",") if b.strip()]

# ── Données historiques pour backtest ──────────────────────────────────────
BACKTEST_DAYS = 1095  # 3 ans d'historique pour une validation statistique solide

import os
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ────────────────────────────────────────────────────────────────
EXCHANGE = "kraken"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")

# ── Paires tradées ──────────────────────────────────────────────────────────
# UNIVERS 2026-05-01 v4 : profil agressif crypto-tilted + stocks via Alpaca.
# Crypto live → Kraken (déjà fonctionnel, frais 0.26%).
# Stocks US (vrais, pas tokenisés) → Alpaca (paper/live, frais $0).
# xStocks Kraken abandonnés (réponse support 2026-04-30 : EEA = Convert only,
# pas d'order book API).

# Cryptos (5) — top liquidité, trend persistence — routés Kraken
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

# Stocks US (16) — diversifiés sur 7 secteurs — routés Alpaca, fractionnables
STOCKS = [
    # Tech mega-cap (3) — moteur bull
    "NVDA", "GOOGL", "META",
    # AI/Cloud high beta (2)
    "PLTR", "CRWD",
    # Healthcare défensif (2)
    "LLY", "ABBV",
    # Energy (2)
    "XOM", "CVX",
    # Financials (2)
    "JPM", "BAC",
    # Defensive consumer (2)
    "KO", "PG",
    # Index ETF (2)
    "SPY", "QQQ",
    # Gold ETF (1)
    "GLD",
]

# xStocks Kraken : conservé pour rétro-compat / référence — non utilisé.
# Le support Kraken (2026-04-30) confirme : pas de trading xStocks via REST API
# pour clients EEA (uniquement via la feature Convert dans Kraken Pro UI).
XSTOCKS = []
XSTOCKS_ENABLED = False

# Flag activation Alpaca (default: True dès qu'on a les clés)
ALPACA_ENABLED = os.getenv("ALPACA_ENABLED", "true").lower() == "true" \
                 and bool(os.getenv("ALPACA_API_KEY"))

SYMBOLS = CRYPTO + (STOCKS if ALPACA_ENABLED else [])  # 21 actifs si Alpaca ON, sinon 5 cryptos

# ── Heures marché US (Eastern Time — gère automatiquement EST/EDT) ──────────
XSTOCK_MARKET_OPEN_ET  = (9, 30)    # NYSE/NASDAQ ouverture (9h30 ET)
XSTOCK_MARKET_CLOSE_ET = (16,  0)   # Fermeture (16h00 ET)
XSTOCK_PREMARKET_ET    = (8,   0)   # Analyse pré-marché (8h00 ET = 14h CET = 15h CEST)

# ── Gestion du risque portefeuille ──────────────────────────────────────────
# Profil A agressif (2026-04-30) : viser 25-40% CAGR, accepter MaxDD -40%.
MAX_DRAWDOWN = -0.35    # Coupe-circuit si capital chute de -35% depuis le départ (vs -15% prudent)

# ── Secteurs (corrélation positions — max MAX_PER_SECTOR par secteur) ───────
# 1 → 2 : on bloquait NVDA quand AAPL ouvert, BTC quand ETH ouvert. Rate les
# rallies sectoriels groupés (NVDA +25%, BTC +20% avril 2026).
MAX_PER_SECTOR = 2
SECTORS = {
    # Tech mega-cap (Alpaca)
    "NVDA": "tech",       "GOOGL": "tech",      "META": "tech",
    # AI/Cloud (Alpaca)
    "PLTR": "ai_data",    "CRWD": "cybersec",
    # Healthcare (Alpaca)
    "LLY":  "healthcare", "ABBV": "healthcare",
    # Energy (Alpaca)
    "XOM":  "energy",     "CVX":  "energy",
    # Financials (Alpaca)
    "JPM":  "financials", "BAC":  "financials",
    # Consumer defensive (Alpaca)
    "KO":   "defensive",  "PG":   "defensive",
    # Index ETF (Alpaca)
    "SPY":  "index",      "QQQ":  "index",
    # Gold ETF (Alpaca)
    "GLD":  "gold",
    # Crypto (Kraken)
    "BTC/USD":   "crypto",     "ETH/USD":   "crypto",
    "SOL/USD":   "crypto",     "AVAX/USD":  "crypto",
    "LINK/USD":  "crypto",
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
# Profil A agressif : sizing plus large, stops plus larges, vol cible plus haute.
POSITION_SIZE_PCT = 0.25  # 25% du total portfolio par position (vs 20% prudent)
POSITION_MIN_PCT  = 0.05  # 5% du capital initial = floor relatif
ATR_MULTIPLIER = 4.0     # Stop-loss = 4x ATR — laisse plus de room aux trends crypto/tech (vs 3x)
TAKE_PROFIT_RATIO = 3.0  # (référence calcul, non utilisé en live — trailing stop)
MAX_OPEN_TRADES = 6      # Max 6 positions simultanées (capital 109 USD → 18 USD/position)
TARGET_VOL   = 0.25     # Volatilité annualisée cible 25% (vs 15% prudent)
MAX_LEVERAGE = 1.3      # Exposition max ×1.3 (préservé)

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
# Profil A agressif : -25% (accepte plus de DD pour viser 25-40% CAGR).
KILL_SWITCH_PCT = float(os.getenv("KILL_SWITCH_PCT", "-0.25"))

# ── Min order Kraken (skip tentative si budget < seuil) ─────────────────────
# Kraken min cost réel BTC = 0.5 USD, xStocks ~0.5 USD
# Avec 4 bots × 27 USD × 25% sizing = 6.75 USD typique, descendre à 1.0 pour
# permettre aux petits trades de passer (small capital live).
MIN_ORDER_EUR = float(os.getenv("MIN_ORDER_EUR", "1.0"))

# ── Bots actifs (autres = désactivés, ne tradent pas) ───────────────────────
# Audit 55 jours paper : Bot A 33 trades, B 4, C/G/H/I/J 0 trades.
# On garde les bots avec preuve d'activité ou utilité défensive (J = mean rev).
# Bot G désactivé temporairement (à investiguer — backtest 70+ trades, live 0).
# Override via env : ACTIVE_BOTS="a,j" ou "a,b,c,g,h,i,j" pour tous.
ACTIVE_BOTS = [b.strip().lower() for b in os.getenv("ACTIVE_BOTS", "a,j").split(",") if b.strip()]

# ── Données historiques pour backtest ──────────────────────────────────────
BACKTEST_DAYS = 1095  # 3 ans d'historique pour une validation statistique solide

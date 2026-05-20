"""Configuration centralisée — lit env (passé par docker-compose) et expose constantes."""
import os
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("COPYTRADE_DATA_DIR", "/data"))
LOGS_DIR = DATA_DIR / "logs"
STATE_PATH = DATA_DIR / "state.json"
POSITIONS_PATH = DATA_DIR / "positions.json"

# Source des décisions : lecture directe du fichier monté (pas de SSH)
DECISIONS_PATH = Path(os.getenv(
    "COPYTRADE_DECISIONS_PATH",
    "/decisions/decisions.jsonl",
))
POLL_INTERVAL_SEC = int(os.getenv("COPYTRADE_POLL_INTERVAL_SEC", "60"))

POLYMARKET_HOST = "https://clob.polymarket.com"
POLYMARKET_CHAIN_ID = 137
# v2 default: 3 (POLY_1271, EIP-1271 smart contract signature) — used by
# Polymarket "deposit wallet flow" since CLOB v2 migration (end of April 2026).
# Legacy values: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE. Override via env if
# the wallet was provisioned through a non-default Polymarket flow.
POLYMARKET_SIG_TYPE = int(os.getenv("POLYMARKET_SIG_TYPE", "3"))

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
API_KEY = os.getenv("POLYMARKET_API_KEY")
API_SECRET = os.getenv("POLYMARKET_API_SECRET")
API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS")
SIGNER = os.getenv("POLYMARKET_SIGNER_ADDRESS")

TARGET_WALLET = os.getenv("COPYTRADE_TARGET_WALLET", "surfandturf")
FIXED_SIZE_USD = float(os.getenv("COPYTRADE_FIXED_SIZE_USD", "1.5"))
MAX_POSITIONS = int(os.getenv("COPYTRADE_MAX_POSITIONS", "20"))
KILL_EQUITY_USD = float(os.getenv("COPYTRADE_KILL_EQUITY_USD", "20.0"))
MIN_TARGET_SIZE_USD = float(os.getenv("COPYTRADE_MIN_TARGET_SIZE_USD", "5.0"))
# Bias underdog confirmé sur surfandturf (Pistons 0.39, Svitolina 0.42, Spurs 0.33, Chennai 0.49)
# Min: skip "lottery tickets" extrêmes (entries <0.06) qui finissent à $0 ~90% du temps
MIN_ENTRY_PRICE = float(os.getenv("COPYTRADE_MIN_ENTRY_PRICE", "0.06"))
MAX_ENTRY_PRICE = float(os.getenv("COPYTRADE_MAX_ENTRY_PRICE", "0.55"))
# Skip si current ask > his_entry × drift (évite de chasser quand le book a bougé)
MAX_PRICE_DRIFT = float(os.getenv("COPYTRADE_MAX_PRICE_DRIFT", "1.05"))
# Cap d'exposition par marché (anti-concentration, surfandturf empile $250K mais nous max $5)
MAX_USD_PER_MARKET = float(os.getenv("COPYTRADE_MAX_USD_PER_MARKET", "5.0"))
# --- Sizing mode (fixed | tiered) ---
SIZING_MODE = os.getenv("COPYTRADE_SIZING_MODE", "fixed").strip().lower()
# Tiered grid — defaults tuned for surfandturf on $40 wallet
TIER_PENNY_MAX = float(os.getenv("COPYTRADE_TIER_PENNY_MAX", "0.20"))
# SKIP zone for absband mode: prices in [TIER_PENNY_MAX, TIER_SKIP_HIGH) are
# skipped. Default = TIER_PENNY_MAX (no skip zone, surfandturf-tuned). Set to
# 0.45 for RN1 to skip his losing mid_low bucket.
TIER_SKIP_HIGH = float(os.getenv("COPYTRADE_TIER_SKIP_HIGH", str(os.getenv("COPYTRADE_TIER_PENNY_MAX", "0.20"))))
TIER_PENNY_MIN_CONVICTION = float(os.getenv("COPYTRADE_TIER_PENNY_MIN_CONVICTION", "0.03"))
TIER_PENNY_SIZE = float(os.getenv("COPYTRADE_TIER_PENNY_SIZE", "1.0"))
TIER_MID_MAX = float(os.getenv("COPYTRADE_TIER_MID_MAX", "0.65"))
TIER_MID_MIN_CONVICTION = float(os.getenv("COPYTRADE_TIER_MID_MIN_CONVICTION", "0.05"))
TIER_MID_MAX_CONVICTION = float(os.getenv("COPYTRADE_TIER_MID_MAX_CONVICTION", "0.50"))
TIER_MID_MIN_SIZE = float(os.getenv("COPYTRADE_TIER_MID_MIN_SIZE", "1.5"))
TIER_MID_MAX_SIZE = float(os.getenv("COPYTRADE_TIER_MID_MAX_SIZE", "5.0"))
TIER_FAV_MIN_CONVICTION = float(os.getenv("COPYTRADE_TIER_FAV_MIN_CONVICTION", "0.15"))
TIER_FAV_SIZE = float(os.getenv("COPYTRADE_TIER_FAV_SIZE", "4.5"))
TIER_NORMAL_SIZE = float(os.getenv("COPYTRADE_TIER_NORMAL_SIZE", "4.5"))
# Option B filters — RN1-specific exclusions (bad hours, bad mtypes, whales).
# Disabled by default to preserve Option A semantics for non-RN1 wallets.
OPTIONB_FILTERS = os.getenv("COPYTRADE_OPTIONB_FILTERS", "false").lower() == "true"
DRY_RUN = os.getenv("COPYTRADE_DRY_RUN", "true").lower() == "true"

GAMMA_API = "https://gamma-api.polymarket.com"


def validate():
    missing = []
    for name, val in [
        ("POLYMARKET_PRIVATE_KEY", PRIVATE_KEY),
        ("POLYMARKET_API_KEY", API_KEY),
        ("POLYMARKET_API_SECRET", API_SECRET),
        ("POLYMARKET_API_PASSPHRASE", API_PASSPHRASE),
        ("POLYMARKET_FUNDER_ADDRESS", FUNDER),
    ]:
        if not val:
            missing.append(name)
    if missing:
        raise RuntimeError(f"Missing env vars: {missing}")
    if PRIVATE_KEY and len(PRIVATE_KEY) != 66:
        raise RuntimeError(f"POLYMARKET_PRIVATE_KEY wrong length ({len(PRIVATE_KEY)})")
    if not DECISIONS_PATH.exists():
        raise RuntimeError(f"Decisions file not mounted: {DECISIONS_PATH}")

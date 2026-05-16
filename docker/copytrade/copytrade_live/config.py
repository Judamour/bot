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
POLYMARKET_SIG_TYPE = 1

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

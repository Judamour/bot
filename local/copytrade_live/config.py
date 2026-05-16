"""Configuration centralisée — lit .env et expose constantes."""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

MODULE_DIR = Path(__file__).resolve().parent
LOGS_DIR = MODULE_DIR / "logs"
STATE_PATH = MODULE_DIR / "state.json"
POSITIONS_PATH = MODULE_DIR / "positions.json"

VPS_HOST = "ubuntu@51.210.13.248"
VPS_DECISIONS_PATH = "/home/botuser/bot-trading/logs/copytrade/decisions.jsonl"
POLL_INTERVAL_SEC = 60

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
MIN_ENTRY_PRICE = float(os.getenv("COPYTRADE_MIN_ENTRY_PRICE", "0.06"))
MAX_ENTRY_PRICE = float(os.getenv("COPYTRADE_MAX_ENTRY_PRICE", "0.55"))
MAX_PRICE_DRIFT = float(os.getenv("COPYTRADE_MAX_PRICE_DRIFT", "1.05"))
MAX_USD_PER_MARKET = float(os.getenv("COPYTRADE_MAX_USD_PER_MARKET", "5.0"))
DRY_RUN = os.getenv("COPYTRADE_DRY_RUN", "true").lower() == "true"

GAMMA_API = "https://gamma-api.polymarket.com"

def validate():
    """Raise if any required env var is missing. Call before doing anything live."""
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
        raise RuntimeError(f"POLYMARKET_PRIVATE_KEY wrong length ({len(PRIVATE_KEY)}, expected 66)")

import os
from datetime import datetime, timezone

# ─────────────────────────────────────────
# Deriv API
# ─────────────────────────────────────────
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DEMO_TOKEN   = os.getenv("DEMO_TOKEN", "")
LIVE_TOKEN   = os.getenv("LIVE_TOKEN", "")
MODE         = os.getenv("MODE", "demo").lower()

def get_active_token():
    if MODE == "live":
        if not LIVE_TOKEN:
            raise ValueError("LIVE_TOKEN not set")
        return LIVE_TOKEN
    if not DEMO_TOKEN:
        raise ValueError("DEMO_TOKEN not set")
    return DEMO_TOKEN

ACTIVE_TOKEN = get_active_token()

# ─────────────────────────────────────────
# Scalping markets
# ─────────────────────────────────────────
SCALP_MARKETS = [
    "frxEURUSD",
    "frxGBPUSD",
    "frxUSDJPY",
    "frxAUDUSD",
    "frxUSDCHF",
    "frxXAUUSD",
]

MARKETS = SCALP_MARKETS  # For compatibility

# ─────────────────────────────────────────
# Candle settings
# ─────────────────────────────────────────
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", 30))
CANDLE_GRANULARITY = 60
CANDLE_COUNT       = 100
HTF_GRANULARITY    = 3600
HTF_COUNT          = 200

# ─────────────────────────────────────────
# Risk management
# ─────────────────────────────────────────
STAKE_PERCENT        = float(os.getenv("STAKE_PERCENT", 1.0))
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 5.0))
DAILY_PROFIT_TARGET  = float(os.getenv("DAILY_PROFIT_TARGET", 8.0))
MAX_OPEN_POSITIONS   = int(os.getenv("MAX_OPEN_POSITIONS", 3))

# ─────────────────────────────────────────
# Account
# ─────────────────────────────────────────
CURRENCY = os.getenv("CURRENCY", "USD")

# ─────────────────────────────────────────
# Admin
# ─────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
PORT           = int(os.environ.get("PORT") or 10000)

# ─────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO")
STAKING_STRATEGY = os.getenv("STAKING_STRATEGY", "flat").lower()

# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def get_current_session() -> str:
    hour = datetime.now(timezone.utc).hour
    if 13 <= hour < 17: return "LONDON_NY_OVERLAP"
    if 8  <= hour < 13: return "LONDON"
    if 17 <= hour < 22: return "NEW_YORK"
    if 0  <= hour < 7:  return "ASIAN"
    return "DEAD_ZONE"

def is_weekend() -> bool:
    day  = datetime.now(timezone.utc).weekday()
    hour = datetime.now(timezone.utc).hour
    if day == 4 and hour >= 21: return True
    if day == 5: return True
    if day == 6 and hour < 21: return True
    return False

def get_active_markets() -> list:
    if is_weekend():
        return ["frxXAUUSD"]  # Gold trades on weekends
    return SCALP_MARKETS

def is_forex(market: str) -> bool:
    return market.startswith("frx") and market not in ("frxXAUUSD","frxXAGUSD")

def is_commodity(market: str) -> bool:
    return market in ("frxXAUUSD", "frxXAGUSD")

def validate_config():
    errors = []
    if not DERIV_APP_ID: errors.append("DERIV_APP_ID not set")
    if MODE not in ("demo","live"): errors.append(f"Invalid MODE: {MODE}")
    if not ACTIVE_TOKEN: errors.append("No active token")
    for e in errors: print(f"[CONFIG ERROR] {e}")
    if errors: raise SystemExit("Fix config errors")
    print(f"[CONFIG] OK | Mode: {MODE.upper()} | "
          f"Markets: {len(MARKETS)} | Interval: {SCAN_INTERVAL}s")

validate_config()

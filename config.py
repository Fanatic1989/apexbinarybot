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
            raise ValueError("LIVE_TOKEN not set but MODE=live")
        return LIVE_TOKEN
    if not DEMO_TOKEN:
        raise ValueError("DEMO_TOKEN not set but MODE=demo")
    return DEMO_TOKEN

ACTIVE_TOKEN = get_active_token()

# ─────────────────────────────────────────
# Scan & candle settings
# ─────────────────────────────────────────
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", 60))
CANDLE_GRANULARITY = 60
CANDLE_COUNT       = 120
HTF_GRANULARITY    = 3600
HTF_COUNT          = 50

# ─────────────────────────────────────────
# Markets
# ─────────────────────────────────────────
SYNTHETIC_MARKETS = [
    "R_50", "R_75", "R_100",
    "1HZ50V", "1HZ75V", "1HZ100V",
    "JD50", "JD75", "JD100",
    # BOOM/CRASH removed — don't support Rise/Fall binary contracts
    # Use Deriv's dedicated Boom/Crash product instead
]

FOREX_MARKETS = [
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY",
    "frxAUDUSD", "frxUSDCAD", "frxUSDCHF",
    "frxEURGBP", "frxEURJPY", "frxGBPJPY",
]

ASIAN_FOREX = ["frxAUDUSD", "frxUSDJPY", "frxUSDCAD", "frxEURJPY"]

MARKETS = list(dict.fromkeys(FOREX_MARKETS + SYNTHETIC_MARKETS))

# ─────────────────────────────────────────
# Session detection
# ─────────────────────────────────────────
def get_current_session() -> str:
    hour = datetime.now(timezone.utc).hour
    if 13 <= hour < 17:  return "LONDON_NY_OVERLAP"
    if 8  <= hour < 17:  return "LONDON"
    if 17 <= hour < 20:  return "NEW_YORK"
    if 0  <= hour < 7:   return "ASIAN"
    return "DEAD_ZONE"

def is_weekend() -> bool:
    day  = datetime.now(timezone.utc).weekday()
    hour = datetime.now(timezone.utc).hour
    if day == 4 and hour >= 21: return True
    if day == 5:                return True
    if day == 6 and hour < 21:  return True
    return False

def get_active_markets() -> list:
    if is_weekend():
        return SYNTHETIC_MARKETS
    session = get_current_session()
    if session in ("LONDON_NY_OVERLAP", "LONDON", "NEW_YORK"):
        return FOREX_MARKETS + SYNTHETIC_MARKETS
    if session == "ASIAN":
        return ASIAN_FOREX + SYNTHETIC_MARKETS
    return SYNTHETIC_MARKETS

# ─────────────────────────────────────────
# Expiry (minutes)
# ─────────────────────────────────────────
SYNTHETIC_EXPIRY = {
    "R_50": 3,   "R_75": 3,   "R_100": 2,
    "1HZ50V": 1, "1HZ75V": 1, "1HZ100V": 1,
    "JD50": 1,   "JD75": 1,   "JD100": 1,
    "BOOM500": 1,  "BOOM1000": 1,
    "CRASH500": 1, "CRASH1000": 1,
}

FOREX_EXPIRY_OPTIONS = [15, 30, 60, 120]

def get_expiry(market: str) -> int:
    if is_forex(market):
        return FOREX_EXPIRY_OPTIONS[0]
    return SYNTHETIC_EXPIRY.get(market, 3)

def is_forex(market: str) -> bool:
    return market.startswith("frx")

# ─────────────────────────────────────────
# Risk management
# ─────────────────────────────────────────
STAKE_PERCENT        = float(os.getenv("STAKE_PERCENT", 1.0))
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 10.0))
MAX_CONSECUTIVE_LOSS = int(os.getenv("MAX_CONSECUTIVE_LOSS", 3))
DAILY_PROFIT_TARGET  = float(os.getenv("DAILY_PROFIT_TARGET", 5.0))
PAUSE_DURATION       = int(os.getenv("PAUSE_DURATION", 1800))

# ─────────────────────────────────────────
# Account
# ─────────────────────────────────────────
CURRENCY         = os.getenv("CURRENCY", "USD")
COMPOUND         = os.getenv("COMPOUND", "false").lower() == "true"
COMPOUND_PERCENT = float(os.getenv("COMPOUND_PERCENT", 50.0))

# ─────────────────────────────────────────
# Admin / server
# ─────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
PORT           = int(os.environ.get("PORT") or os.environ.get("port") or 10000)

# ─────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─────────────────────────────────────────
# Staking
# ─────────────────────────────────────────
STAKING_STRATEGY = os.getenv("STAKING_STRATEGY", "flat").lower()

# ─────────────────────────────────────────
# Validation
# ─────────────────────────────────────────
def validate_config():
    errors = []
    if not DERIV_APP_ID:
        errors.append("DERIV_APP_ID not set")
    if MODE not in ("demo", "live"):
        errors.append(f"MODE must be demo or live, got: {MODE}")
    if not ACTIVE_TOKEN:
        errors.append("No active token available")
    if SCAN_INTERVAL < 15:
        errors.append("SCAN_INTERVAL must be at least 15 seconds")
    for e in errors:
        print(f"[CONFIG ERROR] {e}")
    if errors:
        raise SystemExit("Fix config errors before starting.")
    print(f"[CONFIG] OK | Mode: {MODE.upper()} | "
          f"Markets: {len(MARKETS)} | Interval: {SCAN_INTERVAL}s | "
          f"Compound: {COMPOUND} | Staking: {STAKING_STRATEGY.upper()}")

validate_config()

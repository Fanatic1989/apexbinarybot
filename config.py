import os
from datetime import datetime, timezone

# ─────────────────────────────────────────
# Deriv Application ID
# ─────────────────────────────────────────
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

# ─────────────────────────────────────────
# Tokens
# ─────────────────────────────────────────
DEMO_TOKEN = os.getenv("DEMO_TOKEN", "")
LIVE_TOKEN = os.getenv("LIVE_TOKEN", "")
MODE       = os.getenv("MODE", "demo").lower()

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
HTF_GRANULARITY    = 3600   # 1-hour candles for trend filter
HTF_COUNT          = 50

# ─────────────────────────────────────────
# Synthetic indices — always available
# ─────────────────────────────────────────
SYNTHETIC_MARKETS = [
    "R_50", "R_75", "R_100",
    "1HZ50V", "1HZ75V", "1HZ100V",
    "JD50", "JD75", "JD100",
    "BOOM500", "BOOM1000",
    "CRASH500", "CRASH1000",
]

# ─────────────────────────────────────────
# Forex pairs — session aware
# Deriv binary options uses frx prefix
# ─────────────────────────────────────────
FOREX_MARKETS = [
    "frxEURUSD",   # Euro / US Dollar        — most liquid
    "frxGBPUSD",   # British Pound / US Dollar
    "frxUSDJPY",   # US Dollar / Japanese Yen
    "frxAUDUSD",   # Australian Dollar / US Dollar
    "frxUSDCAD",   # US Dollar / Canadian Dollar
    "frxUSDCHF",   # US Dollar / Swiss Franc
    "frxEURGBP",   # Euro / British Pound
    "frxEURJPY",   # Euro / Japanese Yen
    "frxGBPJPY",   # British Pound / Japanese Yen
]

# Asian session only pairs
ASIAN_FOREX = ["frxAUDUSD", "frxUSDJPY", "frxUSDCAD", "frxEURJPY"]

# ─────────────────────────────────────────
# Session windows (UTC hours)
# ─────────────────────────────────────────
LONDON_OPEN    = 8
LONDON_CLOSE   = 17
NY_OPEN        = 13
NY_CLOSE       = 20
ASIAN_OPEN     = 0
ASIAN_CLOSE    = 7
DEAD_ZONE_START= 20
DEAD_ZONE_END  = 24

def get_current_session() -> str:
    """Return current trading session name."""
    hour = datetime.now(timezone.utc).hour
    if NY_OPEN <= hour < NY_CLOSE and LONDON_OPEN <= hour < LONDON_CLOSE:
        return "LONDON_NY_OVERLAP"
    elif LONDON_OPEN <= hour < LONDON_CLOSE:
        return "LONDON"
    elif NY_OPEN <= hour < NY_CLOSE:
        return "NEW_YORK"
    elif ASIAN_OPEN <= hour < ASIAN_CLOSE:
        return "ASIAN"
    else:
        return "DEAD_ZONE"

def get_active_markets() -> list:
    """
    Return markets to scan based on current session.
    Forex during market hours, synthetics during off-hours.
    Forex completely disabled on weekends.
    """
    # Weekends — synthetics only, forex closed
    if is_weekend():
        return SYNTHETIC_MARKETS

    session = get_current_session()
    if session in ("LONDON_NY_OVERLAP", "LONDON", "NEW_YORK"):
        return FOREX_MARKETS + SYNTHETIC_MARKETS
    elif session == "ASIAN":
        return ASIAN_FOREX + SYNTHETIC_MARKETS
    else:
        # Dead zone — synthetics only
        return SYNTHETIC_MARKETS

# Combined full market list
MARKETS = list(dict.fromkeys(FOREX_MARKETS + SYNTHETIC_MARKETS))

# ─────────────────────────────────────────
# Expiry map (minutes)
# ─────────────────────────────────────────
EXPIRY_MAP = {
    # Forex
    "frxEURUSD": 5, "frxGBPUSD": 5, "frxUSDJPY": 3,
    "frxGBPJPY": 5, "frxEURGBP": 5, "frxAUDUSD": 5,
    # Synthetics
    "R_50": 3,  "R_75": 3,   "R_100": 2,
    "1HZ50V": 1,"1HZ75V": 1, "1HZ100V": 1,
    "JD50": 1,  "JD75": 1,   "JD100": 1,
    "BOOM500": 1,  "BOOM1000": 1,
    "CRASH500": 1, "CRASH1000": 1,
}

def get_expiry(market: str) -> int:
    return EXPIRY_MAP.get(market, 3)

def is_forex(market: str) -> bool:
    return market.startswith("frx")

def is_weekend() -> bool:
    """Forex markets closed Saturday and Sunday UTC."""
    day = datetime.now(timezone.utc).weekday()
    hour = datetime.now(timezone.utc).hour
    # Friday after 21:00 UTC to Sunday 21:00 UTC
    if day == 4 and hour >= 21:  return True  # Friday evening
    if day == 5:                  return True  # Saturday
    if day == 6 and hour < 21:   return True  # Sunday until open
    return False

# ─────────────────────────────────────────
# Risk management
# ─────────────────────────────────────────
STAKE_PERCENT        = float(os.getenv("STAKE_PERCENT", 1.0))
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 10.0))
MAX_CONSECUTIVE_LOSS = int(os.getenv("MAX_CONSECUTIVE_LOSS", 3))
DAILY_PROFIT_TARGET  = float(os.getenv("DAILY_PROFIT_TARGET", 5.0))
PAUSE_DURATION       = int(os.getenv("PAUSE_DURATION", 1800))

# ─────────────────────────────────────────
# Account settings
# ─────────────────────────────────────────
CURRENCY         = os.getenv("CURRENCY", "USD")
COMPOUND         = os.getenv("COMPOUND", "false").lower() == "true"
COMPOUND_PERCENT = float(os.getenv("COMPOUND_PERCENT", 50.0))

# ─────────────────────────────────────────
# Admin / server
# ─────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
HOST           = os.getenv("host", "0.0.0.0")
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
# Validation
# ─────────────────────────────────────────
def validate_config():
    errors = []
    if not DERIV_APP_ID:
        errors.append("DERIV_APP_ID not set")
    if MODE not in ("demo", "live"):
        errors.append(f"MODE must be demo or live, got {MODE}")
    if not ACTIVE_TOKEN:
        errors.append("No active token")
    if SCAN_INTERVAL < 15:
        errors.append("SCAN_INTERVAL must be >= 15s")
    for e in errors:
        print(f"[CONFIG ERROR] {e}")
    if errors:
        raise SystemExit("Fix config errors before starting.")
    print(f"[CONFIG] OK | Mode: {MODE.upper()} | "
          f"Markets: {len(MARKETS)} | Interval: {SCAN_INTERVAL}s | "
          f"Currency: {CURRENCY} | Compound: {COMPOUND}")

validate_config()

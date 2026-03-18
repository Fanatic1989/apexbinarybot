import os

# ─────────────────────────────────────────
# Deriv Application ID
# ─────────────────────────────────────────
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

# ─────────────────────────────────────────
# Tokens
# ─────────────────────────────────────────
DEMO_TOKEN = os.getenv("DEMO_TOKEN", "")
LIVE_TOKEN = os.getenv("LIVE_TOKEN", "")

# ─────────────────────────────────────────
# Mode: "demo" or "live"
# ─────────────────────────────────────────
MODE = os.getenv("MODE", "demo").lower()

# Auto-select token based on mode
def get_active_token():
    if MODE == "live":
        if not LIVE_TOKEN:
            raise ValueError("LIVE_TOKEN is not set but MODE=live")
        return LIVE_TOKEN
    else:
        if not DEMO_TOKEN:
            raise ValueError("DEMO_TOKEN is not set but MODE=demo")
        return DEMO_TOKEN

ACTIVE_TOKEN = get_active_token()

# ─────────────────────────────────────────
# Scan interval (seconds)
# ─────────────────────────────────────────
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60))

# ─────────────────────────────────────────
# Candle settings
# ─────────────────────────────────────────
CANDLE_GRANULARITY = 60   # 1-minute candles
CANDLE_COUNT       = 120  # number of candles to fetch

# ─────────────────────────────────────────
# Markets to scan — all 21 instruments
# ─────────────────────────────────────────
MARKETS = [
    # Standard Volatility
    "R_10",
    "R_25",
    "R_50",
    "R_75",
    "R_100",

    # Fast Tick (1-second)
    "1HZ10V",
    "1HZ25V",
    "1HZ50V",
    "1HZ75V",
    "1HZ100V",

    # Jump Indices
    "JD10",
    "JD25",
    "JD50",
    "JD75",
    "JD100",

    # Boom & Crash
    "BOOM500",
    "BOOM1000",
    "CRASH500",
    "CRASH1000",
]

# ─────────────────────────────────────────
# Expiry rules per market (in minutes)
# ─────────────────────────────────────────
EXPIRY_MAP = {
    "R_10":      10,
    "R_25":      10,
    "R_50":       3,
    "R_75":       3,
    "R_100":      2,
    "1HZ10V":     2,
    "1HZ25V":     2,
    "1HZ50V":     1,
    "1HZ75V":     1,
    "1HZ100V":    1,
    "JD10":       2,
    "JD25":       2,
    "JD50":       1,
    "JD75":       1,
    "JD100":      1,
    "BOOM500":    1,
    "BOOM1000":   1,
    "CRASH500":   1,
    "CRASH1000":  1,
}

def get_expiry(market: str) -> int:
    """Return expiry duration in minutes for a given market."""
    return EXPIRY_MAP.get(market, 3)

# ─────────────────────────────────────────
# Risk management
# ─────────────────────────────────────────
STAKE_PERCENT        = float(os.getenv("STAKE_PERCENT", 1.0))   # % of balance per trade
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 10.0))  # halt at 10% daily loss
MAX_CONSECUTIVE_LOSS = int(os.getenv("MAX_CONSECUTIVE_LOSS", 3))     # pause after 3 losses
DAILY_PROFIT_TARGET  = float(os.getenv("DAILY_PROFIT_TARGET", 5.0))  # stop at 5% daily gain
PAUSE_DURATION       = int(os.getenv("PAUSE_DURATION", 1800))        # 30-min pause in seconds

# ─────────────────────────────────────────
# Telegram (optional)
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─────────────────────────────────────────
# Startup validation
# ─────────────────────────────────────────
def validate_config():
    errors = []
    if not DERIV_APP_ID:
        errors.append("DERIV_APP_ID is not set")
    if MODE not in ("demo", "live"):
        errors.append(f"MODE must be 'demo' or 'live', got '{MODE}'")
    if not ACTIVE_TOKEN:
        errors.append("No active token available")
    if SCAN_INTERVAL < 15:
        errors.append("SCAN_INTERVAL must be at least 15 seconds")
    if errors:
        for e in errors:
            print(f"[CONFIG ERROR] {e}")
        raise SystemExit("Fix config errors before starting the bot.")
    print(f"[CONFIG] Mode: {MODE.upper()} | Markets: {len(MARKETS)} | Interval: {SCAN_INTERVAL}s")

validate_config()

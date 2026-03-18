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
# Markets to scan
# ─────────────────────────────────────────
MARKETS = [
    # Standard Volatility
    "R_10", "R_25", "R_50", "R_75", "R_100",
    # Fast Tick (1-second)
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    # Jump Indices
    "JD10", "JD25", "JD50", "JD75", "JD100",
    # Boom & Crash
    "BOOM500", "BOOM1000", "CRASH500", "CRASH1000",
]

# ─────────────────────────────────────────
# Expiry rules per market (in minutes)
# ─────────────────────────────────────────
EXPIRY_MAP = {
    "R_10": 10, "R_25": 10, "R_50": 3, "R_75": 3, "R_100": 2,
    "1HZ10V": 2, "1HZ25V": 2, "1HZ50V": 1, "1HZ75V": 1, "1HZ100V": 1,
    "JD10": 2, "JD25": 2, "JD50": 1, "JD75": 1, "JD100": 1,
    "BOOM500": 1, "BOOM1000": 1, "CRASH500": 1, "CRASH1000": 1,
}

def get_expiry(market: str) -> int:
    return EXPIRY_MAP.get(market, 3)

# ─────────────────────────────────────────
# Risk management
# ─────────────────────────────────────────
STAKE_PERCENT        = float(os.getenv("STAKE_PERCENT", 1.0))
MAX_DAILY_LOSS_PCT   = float(os.getenv("MAX_DAILY_LOSS_PCT", 10.0))
MAX_CONSECUTIVE_LOSS = int(os.getenv("MAX_CONSECUTIVE_LOSS", 3))
DAILY_PROFIT_TARGET  = float(os.getenv("DAILY_PROFIT_TARGET", 5.0))
PAUSE_DURATION       = int(os.getenv("PAUSE_DURATION", 1800))

# ─────────────────────────────────────────
# Account / trading settings
# ─────────────────────────────────────────
CURRENCY = os.getenv("CURRENCY", "USD")

# Compounding — reinvest profits into stake sizing
# When True, COMPOUND_PERCENT% of each win is added
# back into the balance used for stake calculation.
# e.g. Win $10, COMPOUND_PERCENT=50 -> $5 added to stake base
COMPOUND         = os.getenv("COMPOUND", "false").lower() == "true"
COMPOUND_PERCENT = float(os.getenv("COMPOUND_PERCENT", 50.0))

# ─────────────────────────────────────────
# Dashboard admin credentials
# ─────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# ─────────────────────────────────────────
# Server / hosting
# ─────────────────────────────────────────
HOST = os.getenv("host", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("port") or 10000)

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
    errors   = []
    warnings = []

    if not DERIV_APP_ID:
        errors.append("DERIV_APP_ID is not set")
    if MODE not in ("demo", "live"):
        errors.append(f"MODE must be 'demo' or 'live', got '{MODE}'")
    if not ACTIVE_TOKEN:
        errors.append("No active token available")
    if SCAN_INTERVAL < 15:
        errors.append("SCAN_INTERVAL must be at least 15 seconds")
    if not ADMIN_PASSWORD:
        warnings.append("ADMIN_PASSWORD is not set — dashboard login is unprotected")

    for w in warnings:
        print(f"[CONFIG WARNING] {w}")
    for e in errors:
        print(f"[CONFIG ERROR]   {e}")

    if errors:
        raise SystemExit("Fix config errors before starting the bot.")

    print(
        f"[CONFIG] OK | Mode: {MODE.upper()} | "
        f"Markets: {len(MARKETS)} | Interval: {SCAN_INTERVAL}s | "
        f"Currency: {CURRENCY} | Compound: {COMPOUND}"
    )

validate_config()

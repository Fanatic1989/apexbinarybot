import os

# ==========================
# ADMIN LOGIN
# ==========================

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme123")


# ==========================
# DERIV TOKENS
# ==========================

DEMO_TOKEN = os.getenv("DEMO_TOKEN")
LIVE_TOKEN = os.getenv("LIVE_TOKEN")


# ==========================
# BOT MODE
# ==========================

MODE = "demo"


# ==========================
# TRADING SETTINGS
# ==========================

START_BALANCE = 100
RISK_PERCENT = 2
COMPOUND = True


# ==========================
# BOT SPEED
# ==========================

SCAN_INTERVAL = 30


# ==========================
# MARKETS
# ==========================

MARKETS = [
"R_10","R_25","R_50","R_75","R_100",
"1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
"JD10","JD25","JD50","JD75","JD100",
"BOOM500","BOOM1000",
"CRASH500","CRASH1000"
]

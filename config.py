import os

# Deriv Application ID
DERIV_APP_ID = os.getenv("DERIV_APP_ID")

# Tokens
DEMO_TOKEN = os.getenv("DEMO_TOKEN")
LIVE_TOKEN = os.getenv("LIVE_TOKEN")

# Mode (demo or live)
MODE = os.getenv("MODE", "demo")

# Scan interval (seconds)
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60))

# Markets to scan
MARKETS = [
    "R_10",
    "R_25",
    "R_50",
    "R_75",
    "R_100",
    "R_255"
]

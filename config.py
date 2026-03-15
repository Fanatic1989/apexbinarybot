import os
from dotenv import load_dotenv

load_dotenv()

# MODE
MODE = os.getenv("MODE", "demo")

# TOKENS
DEMO_TOKEN = os.getenv("DEMO_TOKEN")
LIVE_TOKEN = os.getenv("LIVE_TOKEN")

# TELEGRAM
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# TRADE SETTINGS
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", 1))
DURATION = int(os.getenv("DURATION", 1))
CURRENCY = os.getenv("CURRENCY", "USD")

# COMPOUND SETTINGS
COMPOUND = os.getenv("COMPOUND", "true").lower() == "true"
COMPOUND_PERCENT = float(os.getenv("COMPOUND_PERCENT", 0.20))

# MARKETS
MARKETS = [
"R_10",
"R_25",
"R_50",
"R_75",
"R_100",
"1HZ10V",
"1HZ25V",
"1HZ50V",
"1HZ75V",
"1HZ100V",
"JD10",
"JD25",
"JD50",
"JD75",
"JD100",
"BOOM500",
"BOOM1000",
"CRASH500",
"CRASH1000"
]

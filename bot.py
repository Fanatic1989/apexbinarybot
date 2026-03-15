import time
import random
from market_scanner import scan_markets
from config import BOT_NAME, SCAN_INTERVAL_MIN, SCAN_INTERVAL_MAX

print(f"{BOT_NAME} started...")

while True:
    scan_markets()
    sleep_time = random.randint(SCAN_INTERVAL_MIN, SCAN_INTERVAL_MAX)
    print(f"Next scan in {sleep_time} seconds")
    time.sleep(sleep_time)
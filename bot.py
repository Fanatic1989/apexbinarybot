import time
import random
from market_scanner import scan_markets
from config import BOT_NAME, SCAN_INTERVAL_MIN, SCAN_INTERVAL_MAX


def main():

    print(f"{BOT_NAME} started...\n")

    while True:

        try:
            # Run market scanner
            scan_markets()

        except Exception as e:
            print("Bot error:", e)
            print("Continuing bot loop...\n")

        # Random wait time between scans
        wait_time = random.randint(SCAN_INTERVAL_MIN, SCAN_INTERVAL_MAX)

        print(f"Next scan in {wait_time} seconds\n")

        time.sleep(wait_time)


# Allows bot to run directly OR from server.py
if __name__ == "__main__":
    main()

from config import MARKETS

def scan_markets():
    for market in MARKETS:
        print(f"Scanning market: {market}")
        # Future: pull candles, run strategy, execute trade
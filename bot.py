import time
from concurrent.futures import ThreadPoolExecutor
from strategy import analyze_market
from trade_manager import place_trade
import config

running = False

def scan_market(market):

    signal = analyze_market(market)

    if signal:
        print(f"🚨 SIGNAL {market}: {signal}")
        place_trade(market, signal)

def run_bot():

    global running
    running = True

    print("APEXBINARYBOT started...")

    while running:

        with ThreadPoolExecutor(max_workers=25) as executor:
            executor.map(scan_market, config.MARKETS)

        print("Next scan in", config.SCAN_INTERVAL)

        time.sleep(config.SCAN_INTERVAL)

def stop_bot():

    global running
    running = False

import time
from concurrent.futures import ThreadPoolExecutor
from strategy import analyze_market
from trade_manager import place_trade
import config

MARKETS = [
"R_10","R_25","R_50","R_75","R_100",
"1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
"JD10","JD25","JD50","JD75","JD100",
"BOOM500","BOOM1000",
"CRASH500","CRASH1000"
]

running = False

def scan_market(market):

    signal = analyze_market(market)

    if signal:
        place_trade(market,signal)

def run_bot():

    global running

    running = True

    print("BOT STARTED")

    while running:

        with ThreadPoolExecutor(max_workers=20) as executor:
            executor.map(scan_market,MARKETS)

        time.sleep(config.SCAN_INTERVAL)

def stop_bot():

    global running

    running = False

import time
import config
from strategy import analyze_market
from trade_executor import execute_trade


def run_bot():

    print("BOT STARTED")

    while True:

        for market in config.MARKETS:

            signal = analyze_market(market)

            if signal:

                result = execute_trade(market, signal)

                print(market, signal, result)

        time.sleep(20)

import json
import time
import random

history_file = "trade_history.json"


def save_trade(symbol, direction, result):

    with open(history_file, "r") as f:
        data = json.load(f)

    trade = {
        "symbol": symbol,
        "direction": direction,
        "result": result,
        "time": int(time.time())
    }

    data["trades"].append(trade)

    with open(history_file, "w") as f:
        json.dump(data, f, indent=4)


def execute_trade(symbol, direction):

    print(f"TRADE {symbol} {direction}")

    time.sleep(3)

    result = random.choice(["W", "L", "D"])

    save_trade(symbol, direction, result)

    return result

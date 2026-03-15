import json
import random
import config

balance = config.START_BALANCE

def calculate_trade():

    global balance

    trade = balance * (config.RISK_PERCENT/100)

    return round(trade,2)

def place_trade(market,direction):

    global balance

    size = calculate_trade()

    result = random.choice(["W","L","D"])

    if result=="W":
        balance += size*0.8

    elif result=="L":
        balance -= size

    trade = {
        "market":market,
        "direction":direction,
        "size":size,
        "result":result
    }

    save_trade(trade)

def save_trade(trade):

    try:
        with open("trade_history.json") as f:
            data=json.load(f)
    except:
        data=[]

    data.append(trade)

    with open("trade_history.json","w") as f:
        json.dump(data,f)

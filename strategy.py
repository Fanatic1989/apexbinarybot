import random

# -------------------------------
# SIMPLE PRICE GENERATOR
# (placeholder until real Deriv API candles are connected)
# -------------------------------

def generate_prices():

    prices = []

    base = random.uniform(100, 200)

    for i in range(50):

        change = random.uniform(-1, 1)

        base = base + change

        prices.append(round(base, 2))

    return prices


# -------------------------------
# EMA CALCULATION
# -------------------------------

def calculate_ema(prices, period):

    k = 2 / (period + 1)

    ema = prices[0]

    for price in prices[1:]:

        ema = price * k + ema * (1 - k)

    return ema


# -------------------------------
# MOMENTUM CHECK
# -------------------------------

def momentum(prices):

    recent = prices[-1]

    past = prices[-5]

    return recent - past


# -------------------------------
# TREND DETECTION
# -------------------------------

def trend_direction(prices):

    ema_fast = calculate_ema(prices, 5)

    ema_slow = calculate_ema(prices, 15)

    if ema_fast > ema_slow:
        return "UP"

    elif ema_fast < ema_slow:
        return "DOWN"

    else:
        return "SIDEWAYS"


# -------------------------------
# MAIN SIGNAL FUNCTION
# -------------------------------

def analyze_market(market):

    prices = generate_prices()

    trend = trend_direction(prices)

    mom = momentum(prices)

    last_price = prices[-1]

    previous_price = prices[-2]


    # -------------------------------
    # BUY SIGNAL
    # -------------------------------

    if trend == "UP":

        if mom > 0 and last_price > previous_price:

            return "CALL"


    # -------------------------------
    # SELL SIGNAL
    # -------------------------------

    if trend == "DOWN":

        if mom < 0 and last_price < previous_price:

            return "PUT"


    # -------------------------------
    # NO TRADE
    # -------------------------------

    return None

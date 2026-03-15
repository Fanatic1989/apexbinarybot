import random


# --------------------------------------------------
# GENERATE SAMPLE PRICES (temporary until API added)
# --------------------------------------------------

def generate_prices():

    prices = []

    base = random.uniform(100,200)

    for i in range(100):

        move = random.uniform(-1.2,1.2)

        base = base + move

        prices.append(round(base,2))

    return prices


# --------------------------------------------------
# EMA CALCULATION
# --------------------------------------------------

def calculate_ema(prices, period):

    multiplier = 2 / (period + 1)

    ema = prices[0]

    for price in prices[1:]:

        ema = (price - ema) * multiplier + ema

    return ema


# --------------------------------------------------
# RSI CALCULATION
# --------------------------------------------------

def calculate_rsi(prices, period=14):

    gains = []
    losses = []

    for i in range(1, len(prices)):

        change = prices[i] - prices[i-1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi


# --------------------------------------------------
# CANDLE MOMENTUM
# --------------------------------------------------

def candle_strength(prices):

    last = prices[-1]
    prev = prices[-2]

    if last > prev:
        return "BULL"

    elif last < prev:
        return "BEAR"

    else:
        return "FLAT"


# --------------------------------------------------
# TREND DETECTION
# --------------------------------------------------

def trend_direction(prices):

    ema_fast = calculate_ema(prices, 9)
    ema_slow = calculate_ema(prices, 21)

    if ema_fast > ema_slow:
        return "UP"

    elif ema_fast < ema_slow:
        return "DOWN"

    else:
        return "SIDEWAYS"


# --------------------------------------------------
# MAIN SIGNAL ENGINE
# --------------------------------------------------

def analyze_market(market):

    prices = generate_prices()

    trend = trend_direction(prices)

    rsi = calculate_rsi(prices)

    candle = candle_strength(prices)


    # ----------------------------
    # CALL SIGNAL
    # ----------------------------

    if trend == "UP":

        if rsi > 55 and candle == "BULL":

            return "CALL"


    # ----------------------------
    # PUT SIGNAL
    # ----------------------------

    if trend == "DOWN":

        if rsi < 45 and candle == "BEAR":

            return "PUT"


    # ----------------------------
    # NO TRADE
    # ----------------------------

    return None

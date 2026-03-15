import pandas as pd
import numpy as np
from deriv_api import get_candles


# -----------------------------------
# EMA
# -----------------------------------

def ema(series, period):

    return series.ewm(span=period, adjust=False).mean()


# -----------------------------------
# RSI
# -----------------------------------

def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)

    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()

    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# -----------------------------------
# MAIN SIGNAL ENGINE
# -----------------------------------

def analyze_market(symbol):

    prices = get_candles(symbol)

    df = pd.DataFrame(prices, columns=["close"])

    df["ema_fast"] = ema(df["close"], 9)

    df["ema_slow"] = ema(df["close"], 21)

    df["rsi"] = rsi(df["close"])

    last = df.iloc[-1]

    prev = df.iloc[-2]


    # -----------------------
    # CALL SIGNAL
    # -----------------------

    if last["ema_fast"] > last["ema_slow"]:

        if last["rsi"] > 55:

            if last["close"] > prev["close"]:

                return "CALL"


    # -----------------------
    # PUT SIGNAL
    # -----------------------

    if last["ema_fast"] < last["ema_slow"]:

        if last["rsi"] < 45:

            if last["close"] < prev["close"]:

                return "PUT"


    return None

import pandas as pd
import numpy as np
from deriv_api import get_candles


# -----------------------------
# EMA
# -----------------------------

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


# -----------------------------
# RSI
# -----------------------------

def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# -----------------------------
# Bollinger Bands
# -----------------------------

def bollinger(df):

    df["ma"] = df["close"].rolling(20).mean()

    df["std"] = df["close"].rolling(20).std()

    df["upper"] = df["ma"] + (df["std"] * 2)
    df["lower"] = df["ma"] - (df["std"] * 2)

    df["width"] = df["upper"] - df["lower"]

    return df


# -----------------------------
# Strategy Engine
# -----------------------------

def analyze_market(symbol):

    candles = get_candles(symbol)

    if len(candles) < 30:
        return None

    df = pd.DataFrame(candles)

    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)

    df["rsi"] = rsi(df["close"])

    df = bollinger(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # --------------------------
    # RSI EXTREME FILTER
    # --------------------------

    if last["rsi"] > 70 or last["rsi"] < 30:
        return None

    # --------------------------
    # EMA CROSS
    # --------------------------

    ema_cross_up = prev["ema9"] < prev["ema21"] and last["ema9"] > last["ema21"]

    ema_cross_down = prev["ema9"] > prev["ema21"] and last["ema9"] < last["ema21"]

    # --------------------------
    # Candle Direction
    # --------------------------

    bullish = last["close"] > last["open"]

    bearish = last["close"] < last["open"]

    # --------------------------
    # Bollinger Squeeze
    # --------------------------

    avg_width = df["width"].mean()

    recent_width = df["width"].iloc[-5:].mean()

    squeeze = recent_width < avg_width * 0.8

    # --------------------------
    # CALL
    # --------------------------

    if ema_cross_up and 45 <= last["rsi"] <= 65 and bullish:

        if squeeze:
            print(symbol, "HIGH CONFIDENCE CALL")

        return "CALL"

    # --------------------------
    # PUT
    # --------------------------

    if ema_cross_down and 35 <= last["rsi"] <= 55 and bearish:

        if squeeze:
            print(symbol, "HIGH CONFIDENCE PUT")

        return "PUT"

    return None

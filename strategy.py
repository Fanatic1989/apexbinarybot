import pandas as pd
from deriv_api import get_candles


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def analyze_market(symbol):

    prices = get_candles(symbol)

    df = pd.DataFrame(prices, columns=["close"])

    df["ema_fast"] = ema(df["close"], 9)
    df["ema_slow"] = ema(df["close"], 21)
    df["rsi"] = rsi(df["close"])

    last = df.iloc[-1]

    if last["ema_fast"] > last["ema_slow"] and last["rsi"] > 52:
        return "CALL"

    if last["ema_fast"] < last["ema_slow"] and last["rsi"] < 48:
        return "PUT"

    return None

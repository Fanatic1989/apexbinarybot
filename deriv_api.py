import websocket
import json
import config
import pandas as pd

DERIV_WS = "wss://ws.derivws.com/websockets/v3?app_id=1089"


def get_candles(symbol, count=100):

    ws = websocket.create_connection(DERIV_WS)

    auth = {
        "authorize": config.DEMO_TOKEN if config.MODE == "demo" else config.LIVE_TOKEN
    }

    ws.send(json.dumps(auth))
    ws.recv()

    request = {
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": count,
        "end": "latest",
        "style": "candles",
        "granularity": 60
    }

    ws.send(json.dumps(request))

    result = json.loads(ws.recv())

    ws.close()

    candles = result["candles"]

    prices = [c["close"] for c in candles]

    return prices

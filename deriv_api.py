import websocket
import json
import config

DERIV_WS = "wss://ws.derivws.com/websockets/v3?app_id=1089"


def get_candles(symbol, count=100):

    ws = websocket.create_connection(DERIV_WS)

    token = config.DEMO_TOKEN if config.MODE == "demo" else config.LIVE_TOKEN

    ws.send(json.dumps({
        "authorize": token
    }))

    ws.recv()

    ws.send(json.dumps({
        "ticks_history": symbol,
        "count": count,
        "end": "latest",
        "style": "candles",
        "granularity": 60
    }))

    result = json.loads(ws.recv())

    ws.close()

    candles = result["candles"]

    return [c["close"] for c in candles]

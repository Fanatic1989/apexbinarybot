import websocket
import json
import config

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}"


def get_candles(symbol, count=120):

    ws = websocket.create_connection(DERIV_WS)

    token = config.DEMO_TOKEN if config.MODE == "demo" else config.LIVE_TOKEN

    # Authorize session
    ws.send(json.dumps({
        "authorize": token
    }))

    ws.recv()

    # Request candles
    ws.send(json.dumps({
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": count,
        "end": "latest",
        "style": "candles",
        "granularity": 60
    }))

    result = json.loads(ws.recv())

    ws.close()

    if "candles" not in result:
        print("No candle data:", result)
        return []

    candles = result["candles"]

    formatted = []

    for c in candles:
        formatted.append({
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"])
        })

    return formatted

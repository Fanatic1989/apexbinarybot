import websocket
import json
from config import MODE, DEMO_API_TOKEN, LIVE_API_TOKEN

def get_token():
    if MODE == "DEMO":
        return DEMO_API_TOKEN
    else:
        return LIVE_API_TOKEN

def connect():
    ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3")
    token = get_token()

    auth_request = {
        "authorize": token
    }

    ws.send(json.dumps(auth_request))
    result = ws.recv()
    print("Connected to Deriv API")

    return ws
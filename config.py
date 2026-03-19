import websocket
import json
import time
import logging
import config

log = logging.getLogger(__name__)
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}"

def _open_ws(timeout=15):
    ws = websocket.create_connection(DERIV_WS, timeout=timeout)
    ws.send(json.dumps({"authorize": config.ACTIVE_TOKEN}))
    resp = json.loads(ws.recv())
    if "error" in resp:
        ws.close()
        raise ConnectionError(f"Auth failed: {resp['error']['message']}")
    return ws

def get_candles(symbol, count=None, granularity=None, retries=3):
    count       = count       or config.CANDLE_COUNT
    granularity = granularity or config.CANDLE_GRANULARITY
    for attempt in range(1, retries + 1):
        ws = None
        try:
            ws = _open_ws()
            ws.send(json.dumps({
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count": count,
                "end": "latest",
                "style": "candles",
                "granularity": granularity
            }))
            result = json.loads(ws.recv())
            if "error" in result:
                log.warning(f"[DERIV] {symbol} candle error: {result['error']['message']}")
                return []
            if "candles" not in result:
                return []
            return [{
                "open":  float(c["open"]),
                "high":  float(c["high"]),
                "low":   float(c["low"]),
                "close": float(c["close"]),
                "epoch": int(c["epoch"])
            } for c in result["candles"]]
        except Exception as e:
            log.error(f"[DERIV] Attempt {attempt}/{retries} for {symbol}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
        finally:
            if ws:
                try: ws.close()
                except: pass
    return []

def get_htf_candles(symbol, retries=2):
    """Fetch 1-hour candles for higher timeframe trend filter."""
    return get_candles(
        symbol,
        count=config.HTF_COUNT,
        granularity=config.HTF_GRANULARITY,
        retries=retries
    )

def get_balance():
    ws = None
    try:
        ws = _open_ws()
        ws.send(json.dumps({"balance": 1, "subscribe": 0}))
        result = json.loads(ws.recv())
        if "error" in result:
            log.error(f"[DERIV] Balance error: {result['error']['message']}")
            return 0.0
        return float(result["balance"]["balance"])
    except Exception as e:
        log.error(f"[DERIV] Balance fetch failed: {e}")
        return 0.0
    finally:
        if ws:
            try: ws.close()
            except: pass

def place_trade(symbol, direction, stake, duration_minutes):
    """
    Place a binary options trade.
    For forex pairs, automatically tries multiple durations
    until one is accepted by Deriv.
    """
    # Build list of durations to try
    if symbol.startswith("frx"):
        # Try different durations for forex
        durations_to_try = [(d, "m") for d in [15, 30, 60, 120]]
    else:
        durations_to_try = [(duration_minutes, "m")]

    ws = None
    try:
        ws = _open_ws()

        for dur, unit in durations_to_try:
            ws.send(json.dumps({
                "proposal": 1,
                "amount": round(stake, 2),
                "basis": "stake",
                "contract_type": "CALL" if direction == "CALL" else "PUT",
                "currency": "USD",
                "duration": dur,
                "duration_unit": unit,
                "symbol": symbol
            }))
            proposal = json.loads(ws.recv())

            if "error" in proposal:
                err_msg = proposal['error']['message']
                if "duration" in err_msg.lower() or "trading is not offered" in err_msg.lower():
                    log.warning(f"[DERIV] {symbol} duration {dur}{unit} not valid, trying next...")
                    continue
                log.error(f"[DERIV] Proposal error {symbol}: {err_msg}")
                return {}

            # Valid proposal found
            proposal_id  = proposal["proposal"]["id"]
            payout       = proposal["proposal"]["payout"]
            actual_dur   = dur
            log.info(f"[DERIV] Proposal OK {symbol} {direction} "
                     f"${stake} | {dur}{unit} | payout ${payout:.2f}")

            ws.send(json.dumps({"buy": proposal_id, "price": round(stake, 2)}))
            result = json.loads(ws.recv())

            if "error" in result:
                log.error(f"[DERIV] Buy error {symbol}: {result['error']['message']}")
                return {}

            return {
                "contract_id":  result["buy"]["contract_id"],
                "symbol":       symbol,
                "direction":    direction,
                "stake":        stake,
                "payout":       payout,
                "duration_min": actual_dur
            }

        log.error(f"[DERIV] No valid duration found for {symbol}")
        return {}

    except Exception as e:
        log.error(f"[DERIV] place_trade exception {symbol}: {e}")
        return {}
    finally:
        if ws:
            try: ws.close()
            except: pass

def get_contract_result(contract_id):
    ws = None
    try:
        ws = _open_ws()
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id
        }))
        result = json.loads(ws.recv())
        if "error" in result:
            log.error(f"[DERIV] Contract result error: {result['error']['message']}")
            return {}
        contract = result.get("proposal_open_contract", {})
        return {
            "status":      contract.get("status", "open"),
            "profit":      float(contract.get("profit", 0)),
            "entry_spot":  contract.get("entry_spot"),
            "exit_spot":   contract.get("exit_spot"),
            "contract_id": contract_id
        }
    except Exception as e:
        log.error(f"[DERIV] get_contract_result exception: {e}")
        return {}
    finally:
        if ws:
            try: ws.close()
            except: pass

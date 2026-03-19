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
    # Build list of durations to try based on instrument type
    if symbol.startswith("frx"):
        # Forex: try minutes
        durations_to_try = [(d, "m") for d in [15, 30, 60, 120]]
    elif any(symbol.startswith(p) for p in ("BOOM","CRASH")):
        # Boom/Crash on Deriv use specific minute durations only
        # Rise/Fall contracts: try 1m through 10m
        durations_to_try = [(1,"m"),(2,"m"),(3,"m"),(5,"m"),(10,"m")]
    elif any(symbol.startswith(p) for p in ("JD",)):
        # Jump indices
        durations_to_try = [(1,"m"),(2,"m"),(3,"m")]
    elif symbol.startswith("1HZ"):
        # Fast tick indices
        durations_to_try = [(1,"m"),(2,"m"),(3,"m")]
    else:
        # Standard volatility indices R_10 etc
        durations_to_try = [(duration_minutes,"m"),(2,"m"),(3,"m"),(5,"m")]

    ws = None
    try:
        ws = _open_ws()

        # Cap stake to avoid max payout errors
        # Deriv caps payouts at ~$100-200 depending on instrument
        actual_stake = stake
        if symbol.startswith("frx"):
            # Forex binary max payout ~$100, typical ratio 1.8x → max stake $50
            actual_stake = min(stake, 50.00)
        elif any(symbol.startswith(p) for p in ("BOOM","CRASH")):
            # Boom/Crash max stake ~$50 for safe payout
            actual_stake = min(stake, 50.00)
        else:
            # Synthetics — allow up to $100 stake
            actual_stake = min(stake, 100.00)

        if actual_stake != stake:
            log.info(f"[DERIV] Stake capped: ${stake:.2f} → ${actual_stake:.2f} "
                     f"for {symbol}")

        for dur, unit in durations_to_try:
            ws.send(json.dumps({
                "proposal": 1,
                "amount": round(actual_stake, 2),
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
                if "payout" in err_msg.lower() or "maximum payout" in err_msg.lower():
                    # Reduce stake further and retry same duration
                    actual_stake = round(actual_stake * 0.7, 2)
                    actual_stake = max(actual_stake, 0.50)
                    log.warning(f"[DERIV] Payout too high, reducing stake to ${actual_stake:.2f}")
                    ws.send(json.dumps({
                        "proposal": 1,
                        "amount": actual_stake,
                        "basis": "stake",
                        "contract_type": "CALL" if direction == "CALL" else "PUT",
                        "currency": "USD",
                        "duration": dur,
                        "duration_unit": unit,
                        "symbol": symbol
                    }))
                    proposal = json.loads(ws.recv())
                    if "error" in proposal:
                        log.error(f"[DERIV] Still failing after stake reduction: {proposal['error']['message']}")
                        return {}
                else:
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
                "stake":        actual_stake,
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
    """
    Fetch contract result. Returns dict with status won/lost/open.
    Also tries profit_table as fallback for settled contracts.
    """
    ws = None
    try:
        ws = _open_ws()

        # Primary: proposal_open_contract
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id
        }))
        result = json.loads(ws.recv())

        if "error" not in result:
            contract = result.get("proposal_open_contract", {})
            status   = contract.get("status", "open")
            profit   = float(contract.get("profit", 0))

            if status in ("won", "lost"):
                log.info(f"[DERIV] Contract #{contract_id} → "
                         f"{status.upper()} profit=${profit:.2f}")
                return {
                    "status":      status,
                    "profit":      profit,
                    "entry_spot":  contract.get("entry_spot"),
                    "exit_spot":   contract.get("exit_spot"),
                    "contract_id": contract_id
                }

        # Fallback: profit_table — fetch more records and match by contract_id
        ws.send(json.dumps({
            "profit_table": 1,
            "description":  1,
            "sort":         "DESC",
            "limit":        50
        }))
        pt = json.loads(ws.recv())

        if "profit_table" in pt:
            txns = pt["profit_table"].get("transactions", [])
            log.debug(f"[DERIV] profit_table has {len(txns)} transactions")
            for txn in txns:
                txn_cid = str(txn.get("contract_id","")).strip()
                our_cid = str(contract_id).strip()
                if txn_cid == our_cid:
                    sell_price = float(txn.get("sell_price", 0))
                    buy_price  = float(txn.get("buy_price",  0))
                    profit     = round(sell_price - buy_price, 2)
                    status     = "won" if profit > 0 else "lost"
                    log.info(f"[DERIV] Contract #{contract_id} found in profit_table → "
                             f"{status.upper()} profit=${profit:.2f}")
                    return {
                        "status":      status,
                        "profit":      abs(profit) if status=="won" else profit,
                        "entry_spot":  txn.get("purchase_time"),
                        "exit_spot":   txn.get("sell_time"),
                        "contract_id": contract_id
                    }

        # Still open
        log.debug(f"[DERIV] Contract #{contract_id} not settled yet")
        return {"status": "open", "profit": 0, "contract_id": contract_id}

    except Exception as e:
        log.error(f"[DERIV] get_contract_result exception: {e}")
        return {}
    finally:
        if ws:
            try: ws.close()
            except: pass

import websocket
import json
import time
import logging
import config

log = logging.getLogger(__name__)

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}"

# ─────────────────────────────────────────
# Internal helper — open an authorised WS
# ─────────────────────────────────────────
def _open_ws(timeout: int = 15) -> websocket.WebSocket:
    """Open a WebSocket connection and authorise it. Returns the ws object."""
    ws = websocket.create_connection(DERIV_WS, timeout=timeout)

    # Authorise
    ws.send(json.dumps({"authorize": config.ACTIVE_TOKEN}))
    response = json.loads(ws.recv())

    if "error" in response:
        ws.close()
        raise ConnectionError(f"[DERIV] Auth failed: {response['error']['message']}")

    log.debug("[DERIV] Authorised successfully.")
    return ws


# ─────────────────────────────────────────
# Get candles with retry
# ─────────────────────────────────────────
def get_candles(symbol: str,
                count: int = None,
                granularity: int = None,
                retries: int = 3) -> list:
    """
    Fetch historical candles for a symbol.
    Returns a list of dicts: {open, high, low, close, epoch}
    Returns [] on failure after all retries.
    """
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
                log.warning(f"[DERIV] Candle error for {symbol}: {result['error']['message']}")
                return []

            if "candles" not in result:
                log.warning(f"[DERIV] No candle key in response for {symbol}: {result}")
                return []

            candles = result["candles"]
            formatted = []
            for c in candles:
                formatted.append({
                    "open":  float(c["open"]),
                    "high":  float(c["high"]),
                    "low":   float(c["low"]),
                    "close": float(c["close"]),
                    "epoch": int(c["epoch"])
                })

            log.info(f"[DERIV] {symbol} — {len(formatted)} candles fetched.")
            return formatted

        except Exception as e:
            log.error(f"[DERIV] Attempt {attempt}/{retries} failed for {symbol}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)   # back-off: 3s, 6s, 9s

        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    log.error(f"[DERIV] All {retries} attempts failed for {symbol}. Returning empty.")
    return []


# ─────────────────────────────────────────
# Get account balance
# ─────────────────────────────────────────
def get_balance() -> float:
    """Fetch current account balance. Returns 0.0 on failure."""
    ws = None
    try:
        ws = _open_ws()
        ws.send(json.dumps({"balance": 1, "subscribe": 0}))
        result = json.loads(ws.recv())

        if "error" in result:
            log.error(f"[DERIV] Balance error: {result['error']['message']}")
            return 0.0

        balance = float(result["balance"]["balance"])
        log.info(f"[DERIV] Balance: {balance} {result['balance']['currency']}")
        return balance

    except Exception as e:
        log.error(f"[DERIV] Failed to fetch balance: {e}")
        return 0.0

    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


# ─────────────────────────────────────────
# Place a binary options contract
# ─────────────────────────────────────────
def place_trade(symbol: str,
                direction: str,
                stake: float,
                duration_minutes: int) -> dict:
    """
    Place a Rise/Fall binary options contract.

    direction : "CALL" or "PUT"
    stake     : amount in account currency
    duration  : contract duration in minutes

    Returns the full proposal/buy response dict, or {} on failure.
    """
    contract_type = "CALL" if direction == "CALL" else "PUT"
    ws = None

    try:
        ws = _open_ws()

        # Step 1 — get a proposal
        ws.send(json.dumps({
            "proposal": 1,
            "amount": round(stake, 2),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": duration_minutes,
            "duration_unit": "m",
            "symbol": symbol
        }))

        proposal = json.loads(ws.recv())

        if "error" in proposal:
            log.error(f"[DERIV] Proposal error for {symbol}: {proposal['error']['message']}")
            return {}

        proposal_id = proposal["proposal"]["id"]
        payout      = proposal["proposal"]["payout"]
        log.info(f"[DERIV] Proposal OK — {symbol} {direction} ${stake} → payout ${payout:.2f}")

        # Step 2 — buy the contract
        ws.send(json.dumps({
            "buy": proposal_id,
            "price": round(stake, 2)
        }))

        result = json.loads(ws.recv())

        if "error" in result:
            log.error(f"[DERIV] Buy error for {symbol}: {result['error']['message']}")
            return {}

        contract_id = result["buy"]["contract_id"]
        log.info(f"[DERIV] Trade placed — contract_id: {contract_id} | {symbol} {direction} ${stake}")

        return {
            "contract_id":  contract_id,
            "symbol":       symbol,
            "direction":    direction,
            "stake":        stake,
            "payout":       payout,
            "duration_min": duration_minutes
        }

    except Exception as e:
        log.error(f"[DERIV] place_trade exception for {symbol}: {e}")
        return {}

    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


# ─────────────────────────────────────────
# Check contract result
# ─────────────────────────────────────────
def get_contract_result(contract_id: int) -> dict:
    """
    Poll a contract by ID and return its outcome.
    Returns dict with keys: status, profit, entry_spot, exit_spot
    """
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
        status   = contract.get("status", "open")
        profit   = float(contract.get("profit", 0))

        return {
            "status":      status,
            "profit":      profit,
            "entry_spot":  contract.get("entry_spot"),
            "exit_spot":   contract.get("exit_spot"),
            "contract_id": contract_id
        }

    except Exception as e:
        log.error(f"[DERIV] get_contract_result exception: {e}")
        return {}

    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass

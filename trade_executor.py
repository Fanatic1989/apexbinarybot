import json
import time
import logging
import os
from datetime import datetime

import config
from deriv_api import place_trade, get_contract_result

log = logging.getLogger(__name__)

HISTORY_FILE = "trade_history.json"


# ─────────────────────────────────────────
# Initialise history file if missing
# ─────────────────────────────────────────
def _init_history():
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w") as f:
            json.dump({
                "trades":       [],
                "total_trades": 0,
                "total_wins":   0,
                "total_losses": 0,
                "net_pnl":      0.0
            }, f, indent=4)
        log.info(f"[HISTORY] Created {HISTORY_FILE}")


# ─────────────────────────────────────────
# Save a completed trade to history
# ─────────────────────────────────────────
def save_trade(trade: dict):
    """
    Append a completed trade record to trade_history.json.

    trade dict keys:
        symbol, direction, stake, payout,
        result (won/lost), profit, expiry,
        confidence, contract_id, time
    """
    _init_history()

    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        log.warning("[HISTORY] Could not read history file — resetting.")
        data = {
            "trades": [], "total_trades": 0,
            "total_wins": 0, "total_losses": 0, "net_pnl": 0.0
        }

    # Update summary counters
    data["total_trades"] += 1
    if trade.get("result") == "won":
        data["total_wins"] += 1
        data["net_pnl"]    = round(data["net_pnl"] + trade.get("profit", 0), 2)
    else:
        data["total_losses"] += 1
        data["net_pnl"]      = round(data["net_pnl"] - trade.get("stake", 0), 2)

    # Format the trade record
    record = {
        "contract_id": trade.get("contract_id", "—"),
        "symbol":      trade.get("symbol", "—"),
        "direction":   trade.get("direction", "—"),
        "stake":       round(float(trade.get("stake", 0)), 2),
        "payout":      round(float(trade.get("payout", 0)), 2),
        "result":      trade.get("result", "unknown"),
        "profit":      round(float(trade.get("profit", 0)), 2),
        "expiry":      trade.get("expiry", "—"),
        "confidence":  trade.get("confidence", "normal"),
        "time":        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    }

    data["trades"].append(record)

    # Keep only last 500 trades in file
    if len(data["trades"]) > 500:
        data["trades"] = data["trades"][-500:]

    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=4)
        log.debug(f"[HISTORY] Trade saved — {record['symbol']} {record['direction']} {record['result']}")
    except Exception as e:
        log.error(f"[HISTORY] Failed to write history: {e}")


# ─────────────────────────────────────────
# Execute a trade end-to-end
# ─────────────────────────────────────────
def execute_trade(symbol: str,
                  signal: dict,
                  stake: float) -> dict:
    """
    Place a real trade on Deriv, wait for settlement,
    fetch the result and save it to history.

    Args:
        symbol  : market symbol e.g. "R_75"
        signal  : signal dict from strategy.py
                  must contain: direction, expiry, confidence
        stake   : dollar amount to trade

    Returns:
        result dict:
        {
            "result":   "won" | "lost" | "error",
            "profit":   float,
            "stake":    float,
            "payout":   float,
            "contract_id": int | None
        }
    """
    direction  = signal.get("direction", "NONE")
    expiry     = signal.get("expiry", config.get_expiry(symbol))
    confidence = signal.get("confidence", "normal")

    if direction not in ("CALL", "PUT"):
        log.error(f"[EXECUTOR] Invalid direction '{direction}' for {symbol}")
        return {"result": "error", "profit": 0, "stake": stake, "payout": 0, "contract_id": None}

    log.info(f"[EXECUTOR] Placing trade — {symbol} {direction} "
             f"${stake:.2f} | Expiry: {expiry}m | Confidence: {confidence}")

    # ── Step 1: Place the trade ───────────
    trade = place_trade(
        symbol=symbol,
        direction=direction,
        stake=stake,
        duration_minutes=expiry
    )

    if not trade:
        log.error(f"[EXECUTOR] Trade placement failed for {symbol}")
        return {"result": "error", "profit": 0, "stake": stake, "payout": 0, "contract_id": None}

    contract_id = trade["contract_id"]
    payout      = trade.get("payout", 0)

    log.info(f"[EXECUTOR] Contract placed — ID: {contract_id} | "
             f"Potential payout: ${payout:.2f}")

    # ── Step 2: Wait for settlement ───────
    wait_seconds = (expiry * 60) + 8   # expiry + 8s buffer
    log.info(f"[EXECUTOR] Waiting {wait_seconds}s for contract #{contract_id} to settle...")
    time.sleep(wait_seconds)

    # ── Step 3: Fetch result ──────────────
    outcome = get_contract_result(contract_id)

    if not outcome:
        log.error(f"[EXECUTOR] Could not fetch result for contract #{contract_id}")
        # Save as unknown so it still appears in history
        save_trade({
            "contract_id": contract_id,
            "symbol":      symbol,
            "direction":   direction,
            "stake":       stake,
            "payout":      payout,
            "result":      "unknown",
            "profit":      0,
            "expiry":      expiry,
            "confidence":  confidence
        })
        return {"result": "error", "profit": 0, "stake": stake, "payout": payout, "contract_id": contract_id}

    result = outcome.get("status", "unknown")   # "won" or "lost"
    profit = outcome.get("profit", 0)

    if result == "won":
        log.info(f"[EXECUTOR] ✅ WON — Contract #{contract_id} | "
                 f"+${profit:.2f}")
    elif result == "lost":
        log.info(f"[EXECUTOR] ❌ LOST — Contract #{contract_id} | "
                 f"-${stake:.2f}")
    else:
        log.warning(f"[EXECUTOR] ⚠️ Unknown result '{result}' for #{contract_id}")

    # ── Step 4: Save to history ───────────
    save_trade({
        "contract_id": contract_id,
        "symbol":      symbol,
        "direction":   direction,
        "stake":       stake,
        "payout":      payout,
        "result":      result,
        "profit":      profit,
        "expiry":      expiry,
        "confidence":  confidence
    })

    return {
        "result":      result,
        "profit":      profit,
        "stake":       stake,
        "payout":      payout,
        "contract_id": contract_id
    }


# ─────────────────────────────────────────
# Read trade history
# ─────────────────────────────────────────
def get_history(limit: int = 100) -> dict:
    """
    Return trade history dict for dashboard/API.
    Returns last `limit` trades, most recent first.
    """
    _init_history()
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
        data["trades"] = list(reversed(data["trades"]))[:limit]
        return data
    except Exception as e:
        log.error(f"[HISTORY] Failed to read history: {e}")
        return {"trades": [], "total_trades": 0, "total_wins": 0,
                "total_losses": 0, "net_pnl": 0.0}


# ─────────────────────────────────────────
# Clear trade history
# ─────────────────────────────────────────
def clear_history():
    """Wipe trade history — useful for fresh demo sessions."""
    with open(HISTORY_FILE, "w") as f:
        json.dump({
            "trades": [], "total_trades": 0,
            "total_wins": 0, "total_losses": 0, "net_pnl": 0.0
        }, f, indent=4)
    log.info("[HISTORY] Trade history cleared.")

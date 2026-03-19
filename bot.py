import time
import logging
import json
import os
from datetime import datetime

import config
from deriv_api import get_candles, get_balance, place_trade, get_contract_result
from strategy import analyze_market
from risk_manager import RiskManager
from telegram_bot import send_signal, send_alert

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# Exposed for server.py /status route
risk_manager = None
last_signals  = []

# ─────────────────────────────────────────
# Schedule constants
# ─────────────────────────────────────────
# Session 1: 12 hours trading
# Rest:        1 hour pause
# Session 2:  11 hours trading
# Target:     100 trades per 24 hours
# That means ~4.2 trades per hour = 1 trade every ~14 minutes
# With 19 markets scanning every 60s we easily hit this

SESSION_1_HOURS  = 12
REST_HOURS       = 1
SESSION_2_HOURS  = 11
TARGET_TRADES    = 100
MAX_TRADES_S1    = 55   # ~55 trades in session 1
MAX_TRADES_S2    = 45   # ~45 trades in session 2


# ─────────────────────────────────────────
# Main bot loop
# ─────────────────────────────────────────
def run_bot():
    global risk_manager

    log.info("=" * 55)
    log.info("  APEX BINARY BOT — 24HR SCHEDULE MODE")
    log.info(f"  Mode      : {config.MODE.upper()}")
    log.info(f"  Markets   : {len(config.MARKETS)}")
    log.info(f"  Schedule  : {SESSION_1_HOURS}h trade → {REST_HOURS}h rest → {SESSION_2_HOURS}h trade")
    log.info(f"  Target    : {TARGET_TRADES} trades/day")
    log.info("=" * 55)

    # ── Connect with retry ───────────────
    balance = 0.0
    for attempt in range(1, 6):
        log.info(f"[BOT] Connecting to Deriv (attempt {attempt}/5) | "
                 f"App ID: {config.DERIV_APP_ID} | Mode: {config.MODE.upper()}")
        balance = get_balance()
        if balance > 0:
            break
        log.warning("[BOT] Connection failed. Retrying in 10s...")
        time.sleep(10)

    if balance <= 0:
        log.error("=" * 55)
        log.error("  COULD NOT CONNECT TO DERIV AFTER 5 ATTEMPTS")
        log.error(f"  App ID : {config.DERIV_APP_ID}")
        log.error(f"  Token  : {'SET' if config.ACTIVE_TOKEN else 'MISSING'}")
        log.error("  Fix: Update DERIV_APP_ID + DEMO_TOKEN in Render")
        log.error("=" * 55)
        return

    risk_manager = RiskManager(starting_balance=balance)
    log.info(f"[BOT] Connected | Balance: ${balance:.2f}")
    send_alert(f"🚀 Apex Bot started\nMode: {config.MODE.upper()}\nBalance: ${balance:.2f}")

    # ── Run 24hr schedule ────────────────
    while True:
        try:
            _run_session("SESSION 1", MAX_TRADES_S1, SESSION_1_HOURS)

            log.info(f"[BOT] 💤 REST period — {REST_HOURS} hour(s)")
            send_alert(f"💤 Rest period started ({REST_HOURS}h)\n"
                       f"Balance: ${risk_manager.current_balance:.2f}")
            time.sleep(REST_HOURS * 3600)

            fresh = get_balance()
            if fresh > 0:
                risk_manager.current_balance = fresh
                log.info(f"[BOT] Balance refreshed after rest: ${fresh:.2f}")

            _run_session("SESSION 2", MAX_TRADES_S2, SESSION_2_HOURS)

        except Exception as _e:
            log.error(f"[BOT] Session error: {_e} — restarting in 60s")
            time.sleep(60)
            continue

                # Daily reset
        log.info("[BOT] 🔄 24hr cycle complete — resetting daily counters")
        fresh = get_balance()
        risk_manager.reset_daily(fresh if fresh > 0 else None)
        send_alert(f"📋 24hr cycle complete\n"
                   f"Trades: {risk_manager.total_trades}\n"
                   f"Wins: {risk_manager.total_wins}\n"
                   f"Losses: {risk_manager.total_losses}\n"
                   f"Net P&L: ${risk_manager.net_pnl:.2f}\n"
                   f"Balance: ${risk_manager.current_balance:.2f}")


# ─────────────────────────────────────────
# Trading session
# ─────────────────────────────────────────
def _run_session(name: str, max_trades: int, max_hours: int):
    """Run a trading session until trade target or time limit is hit."""
    log.info(f"[BOT] ▶ {name} started | Target: {max_trades} trades | "
             f"Max duration: {max_hours}h")
    send_alert(f"▶ {name} started\nTarget: {max_trades} trades\n"
               f"Balance: ${risk_manager.current_balance:.2f}")

    session_trades  = 0
    session_start   = time.time()
    max_seconds     = max_hours * 3600
    scan_count      = 0

    while True:
        elapsed = time.time() - session_start

        # ── Time limit ───────────────────
        if elapsed >= max_seconds:
            log.info(f"[BOT] {name} time limit reached ({max_hours}h)")
            break

        # ── Trade target ─────────────────
        if session_trades >= max_trades:
            log.info(f"[BOT] {name} trade target reached ({max_trades} trades)")
            break

        # ── Daily loss limit ─────────────
        if risk_manager.daily_loss_limit_hit():
            msg = f"🛑 Daily loss limit hit. Stopping all sessions."
            log.warning(msg)
            send_alert(msg)
            _sleep_until_midnight()
            fresh = get_balance()
            risk_manager.reset_daily(fresh if fresh > 0 else None)
            break

        # ── Pause check ──────────────────
        if risk_manager.is_paused():
            remaining = risk_manager.pause_remaining()
            log.info(f"[BOT] ⏸ Paused — resuming in {remaining:.0f}s")
            time.sleep(min(remaining, 30))
            continue

        # ── Scan markets ─────────────────
        scan_count += 1
        remaining_trades = max_trades - session_trades
        time_left_min    = (max_seconds - elapsed) / 60

        log.info(f"[BOT] {name} Scan #{scan_count} | "
                 f"Trades: {session_trades}/{max_trades} | "
                 f"Time left: {time_left_min:.0f}m | "
                 f"Balance: ${risk_manager.current_balance:.2f}")

        active = config.get_active_markets()
        for market in active:
            # Stop mid-scan if target hit
            if session_trades >= max_trades:
                break

            try:
                candles = get_candles(market)
                if not candles or len(candles) < 30:
                    continue

                signal = analyze_market(candles, market)
                # Track last signal per market for dashboard
                import bot as _b
                _b.last_signals = [s for s in _b.last_signals if s.get("market") != market]
                if signal:
                    _b.last_signals.append({
                        "market": market,
                        "direction": signal.get("direction","NONE"),
                        "confidence": signal.get("confidence","low"),
                        "timestamp": __import__("datetime").datetime.utcnow().strftime("%H:%M:%S")
                    })
                    if len(_b.last_signals) > 30:
                        _b.last_signals = _b.last_signals[-30:]

                if not signal or signal.get("direction") == "NONE":
                    continue
                if not signal.get("confirmed", False):
                    continue

                direction  = signal["direction"]
                confidence = signal.get("confidence", "normal")
                expiry     = config.get_expiry(market)

                # ── Compounding stake ─────
                stake = _calculate_stake()

                log.info(f"[{market}] ⚡ {direction} | "
                         f"Conf: {confidence} | "
                         f"Expiry: {expiry}m | "
                         f"Stake: ${stake:.2f}")

                send_signal(
                    market=market,
                    direction=direction,
                    expiry=expiry,
                    confidence=confidence,
                    stake=stake
                )

                # ── Signal only mode ─────
                # Set SIGNAL_ONLY=true in Render env to
                # watch signals without placing real trades
                if os.getenv("SIGNAL_ONLY","false").lower() == "true":
                    log.info(f"[{market}] 📡 SIGNAL ONLY — {direction} "
                             f"(no trade placed)")
                    session_trades += 1
                    continue

                # ── Place trade ───────────
                trade = place_trade(
                    symbol=market,
                    direction=direction,
                    stake=stake,
                    duration_minutes=expiry
                )

                if not trade:
                    log.error(f"[{market}] Trade placement failed")
                    continue

                session_trades += 1
                contract_id = trade["contract_id"]
                log.info(f"[{market}] Trade #{session_trades} placed — "
                         f"contract #{contract_id}")

                # ── Wait for settlement ───
                _wait_for_settlement(expiry)

                # ── Get result ────────────
                outcome = get_contract_result(contract_id)
                if outcome:
                    _handle_outcome(market, direction, stake,
                                    outcome, risk_manager, trade, signal)

            except Exception as e:
                log.error(f"[{market}] Error: {e}", exc_info=True)
                continue

        time.sleep(config.SCAN_INTERVAL)

    # ── Session summary ──────────────────
    log.info(f"[BOT] {name} complete | "
             f"Trades: {session_trades} | "
             f"Balance: ${risk_manager.current_balance:.2f}")


# ─────────────────────────────────────────
# Compounding stake calculation
# ─────────────────────────────────────────
def _calculate_stake() -> float:
    """
    Calculate stake with optional compounding.

    If COMPOUND=true:
        Stakes grow as balance grows.
        Uses current_balance * STAKE_PERCENT.

    If COMPOUND=false:
        Fixed stake based on starting balance.
        Safer — losses don't shrink future stakes.
    """
    if config.COMPOUND:
        # Compound: stake grows with balance
        balance = risk_manager.current_balance
    else:
        # Fixed: always use starting balance as base
        balance = risk_manager.starting_balance

    stake = balance * (config.STAKE_PERCENT / 100)
    stake = max(stake, 0.35)            # Deriv minimum
    stake = min(stake, balance * 0.02)  # hard cap 2%
    return round(stake, 2)


# ─────────────────────────────────────────
# Handle trade outcome
# ─────────────────────────────────────────
def _handle_outcome(market, direction, stake, outcome,
                    risk: RiskManager,
                    trade: dict = None,
                    signal: dict = None):
    status      = outcome.get("status", "unknown")
    profit      = float(outcome.get("profit", 0))
    confidence  = signal.get("confidence", "normal") if signal else "normal"
    expiry      = signal.get("expiry", config.get_expiry(market)) if signal else config.get_expiry(market)
    payout      = trade.get("payout", 0) if trade else 0
    contract_id = trade.get("contract_id", "—") if trade else "—"

    # Save to history first
    _save_trade({
        "contract_id": contract_id,
        "symbol":      market,
        "direction":   direction,
        "stake":       round(stake, 2),
        "payout":      round(float(payout), 2),
        "result":      status,
        "profit":      round(profit if status == "won" else -stake, 2),
        "expiry":      expiry,
        "confidence":  confidence,
    })

    if status == "won":
        log.info(f"[{market}] ✅ WON +${profit:.2f} | "
                 f"Balance: ${risk.current_balance + profit:.2f}")
        risk.record_win(profit)
        send_alert(f"✅ {market} {direction} WON +${profit:.2f}\n"
                   f"Balance: ${risk.current_balance:.2f}")

    elif status == "lost":
        log.info(f"[{market}] ❌ LOST -${stake:.2f} | "
                 f"Balance: ${risk.current_balance - stake:.2f}")
        risk.record_loss(stake)
        send_alert(f"❌ {market} {direction} LOST -${stake:.2f}\n"
                   f"Balance: ${risk.current_balance:.2f}")

        if risk.consecutive_losses >= config.MAX_CONSECUTIVE_LOSS:
            risk.trigger_pause()
            msg = (f"⏸ {config.MAX_CONSECUTIVE_LOSS} losses in a row.\n"
                   f"Pausing {config.PAUSE_DURATION // 60} minutes.")
            log.warning(msg)
            send_alert(msg)
    else:
        log.warning(f"[{market}] Unknown result: {status}")


# ─────────────────────────────────────────
# Save trade to history
# ─────────────────────────────────────────
def _save_trade(trade: dict):
    history_file = "trade_history.json"
    empty = {"trades": [], "total_trades": 0,
             "total_wins": 0, "total_losses": 0, "net_pnl": 0.0}
    try:
        if os.path.exists(history_file):
            with open(history_file, "r") as f:
                data = json.load(f)
        else:
            data = empty

        for k, v in empty.items():
            if k not in data:
                data[k] = v

        trade["time"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        data["trades"].append(trade)
        data["total_trades"] += 1

        if trade.get("result") == "won":
            data["total_wins"] += 1
            data["net_pnl"]     = round(data["net_pnl"] + abs(float(trade.get("profit", 0))), 2)
        else:
            data["total_losses"] += 1
            data["net_pnl"]      = round(data["net_pnl"] - float(trade.get("stake", 0)), 2)

        if len(data["trades"]) > 500:
            data["trades"] = data["trades"][-500:]

        with open(history_file, "w") as f:
            json.dump(data, f, indent=2)

        log.info(f"[HISTORY] {trade['symbol']} {trade['direction']} "
                 f"{trade['result'].upper()} saved.")
    except Exception as e:
        log.error(f"[HISTORY] Failed to save: {e}")


# ─────────────────────────────────────────
# Wait for contract settlement
# ─────────────────────────────────────────
def _wait_for_settlement(expiry_minutes: int):
    wait = (expiry_minutes * 60) + 8
    log.info(f"[BOT] Waiting {wait}s for settlement...")
    time.sleep(wait)


# ─────────────────────────────────────────
# Sleep until midnight
# ─────────────────────────────────────────
def _sleep_until_midnight():
    from datetime import timedelta
    now      = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=5, microsecond=0)
    if midnight <= now:
        midnight += timedelta(days=1)
    seconds = (midnight - now).total_seconds()
    log.info(f"[BOT] Sleeping {seconds/3600:.1f}h until midnight...")
    time.sleep(seconds)


if __name__ == "__main__":
    run_bot()

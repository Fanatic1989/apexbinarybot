import time
import logging
from datetime import datetime

import config
from deriv_api import get_candles, get_balance, place_trade, get_contract_result
from strategy import analyze_market
from risk_manager import RiskManager
from telegram_bot import send_signal, send_alert

# ─────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# Exposed for server.py /status route
risk_manager = None


# ─────────────────────────────────────────
# Main bot loop
# ─────────────────────────────────────────
def run_bot():
    log.info("=" * 50)
    log.info(f"  DERIV SIGNAL BOT STARTED")
    log.info(f"  Mode     : {config.MODE.upper()}")
    log.info(f"  Markets  : {len(config.MARKETS)}")
    log.info(f"  Interval : {config.SCAN_INTERVAL}s")
    log.info("=" * 50)

    # Initialise risk manager — retry 5 times before giving up
    balance = 0.0
    for attempt in range(1, 6):
        log.info(f"[BOT] Connecting to Deriv (attempt {attempt}/5) | "
                 f"App ID: {config.DERIV_APP_ID} | Mode: {config.MODE.upper()}")
        balance = get_balance()
        if balance > 0:
            break
        log.warning(f"[BOT] Connection failed. Retrying in 10s...")
        time.sleep(10)

    if balance <= 0:
        log.error("=" * 50)
        log.error("  COULD NOT CONNECT TO DERIV AFTER 5 ATTEMPTS")
        log.error(f"  App ID used : {config.DERIV_APP_ID}")
        log.error(f"  Mode        : {config.MODE.upper()}")
        log.error(f"  Token set   : {'YES' if config.ACTIVE_TOKEN else 'NO'}")
        log.error("  Fix:")
        log.error("  1. https://api.deriv.com  -> Register app -> copy App ID")
        log.error("  2. https://app.deriv.com/account/api-token -> copy token")
        log.error("  3. Update DERIV_APP_ID + DEMO_TOKEN in Render env vars")
        log.error("=" * 50)
        return

    risk = RiskManager(starting_balance=balance)

    # Expose risk manager so server.py /status can read it
    import bot as _self
    _self.risk_manager = risk
    log.info(f"  Balance  : ${balance:.2f}")
    send_alert(f"🤖 Bot started | Mode: {config.MODE.upper()} | Balance: ${balance:.2f}")

    scan_count = 0

    while True:
        # ── Daily limit checks ──────────────────
        if risk.daily_loss_limit_hit():
            msg = f"🛑 Daily loss limit hit ({config.MAX_DAILY_LOSS_PCT}%). Bot stopped for today."
            log.warning(msg)
            send_alert(msg)
            # Sleep until midnight then reset
            _sleep_until_midnight()
            balance = get_balance()
            risk.reset_daily(balance)
            continue

        if risk.daily_profit_target_hit():
            msg = f"✅ Daily profit target hit ({config.DAILY_PROFIT_TARGET}%). Bot stopped for today."
            log.info(msg)
            send_alert(msg)
            _sleep_until_midnight()
            balance = get_balance()
            risk.reset_daily(balance)
            continue

        # ── Consecutive loss pause ──────────────
        if risk.is_paused():
            remaining = risk.pause_remaining()
            log.info(f"[BOT] Paused after {config.MAX_CONSECUTIVE_LOSS} losses. "
                     f"Resuming in {remaining:.0f}s...")
            time.sleep(min(remaining, 30))
            continue

        # ── Scan all markets ────────────────────
        scan_count += 1
        log.info(f"[SCAN #{scan_count}] Scanning {len(config.MARKETS)} markets...")

        signals_found = 0

        for market in config.MARKETS:
            try:
                # Fetch candles
                candles = get_candles(market)
                if not candles or len(candles) < 30:
                    log.warning(f"[{market}] Not enough candle data ({len(candles)} candles). Skipping.")
                    continue

                # Analyse market
                signal = analyze_market(candles, market)

                if not signal or signal["direction"] == "NONE":
                    log.debug(f"[{market}] No signal.")
                    continue

                signals_found += 1
                direction  = signal["direction"]
                confidence = signal.get("confidence", "normal")
                expiry     = config.get_expiry(market)
                stake      = risk.calculate_stake()

                log.info(f"[{market}] ⚡ Signal: {direction} | "
                         f"Confidence: {confidence} | Expiry: {expiry}m | Stake: ${stake:.2f}")

                # Send Telegram signal regardless of trade mode
                send_signal(
                    market=market,
                    direction=direction,
                    expiry=expiry,
                    confidence=confidence,
                    stake=stake
                )

                # ── Auto-trade if enabled ───────────
                if config.MODE in ("demo", "live"):
                    trade = place_trade(
                        symbol=market,
                        direction=direction,
                        stake=stake,
                        duration_minutes=expiry
                    )

                    if not trade:
                        log.error(f"[{market}] Trade placement failed.")
                        continue

                    contract_id = trade["contract_id"]
                    log.info(f"[{market}] Trade placed — contract #{contract_id}")

                    # Wait for contract to settle
                    _wait_for_settlement(expiry)

                    # Check outcome
                    outcome = get_contract_result(contract_id)
                    if outcome:
                        _handle_outcome(market, direction, stake, outcome, risk)

            except Exception as e:
                log.error(f"[{market}] Unexpected error during scan: {e}", exc_info=True)
                continue

        log.info(f"[SCAN #{scan_count}] Complete. Signals found: {signals_found}. "
                 f"Sleeping {config.SCAN_INTERVAL}s...")
        time.sleep(config.SCAN_INTERVAL)


# ─────────────────────────────────────────
# Handle trade outcome
# ─────────────────────────────────────────
def _handle_outcome(market, direction, stake, outcome, risk: "RiskManager"):
    status = outcome.get("status", "unknown")
    profit = outcome.get("profit", 0)

    if status == "won":
        log.info(f"[{market}] ✅ WON +${profit:.2f}")
        risk.record_win(profit)
        send_alert(f"✅ {market} {direction} WON +${profit:.2f}")

    elif status == "lost":
        log.info(f"[{market}] ❌ LOST -${stake:.2f}")
        risk.record_loss(stake)
        send_alert(f"❌ {market} {direction} LOST -${stake:.2f}")

        if risk.consecutive_losses >= config.MAX_CONSECUTIVE_LOSS:
            risk.trigger_pause()
            msg = (f"⏸ {config.MAX_CONSECUTIVE_LOSS} consecutive losses. "
                   f"Pausing for {config.PAUSE_DURATION // 60} minutes.")
            log.warning(msg)
            send_alert(msg)

    else:
        log.warning(f"[{market}] Contract status unknown: {status}")


# ─────────────────────────────────────────
# Wait for contract to expire
# ─────────────────────────────────────────
def _wait_for_settlement(expiry_minutes: int):
    """Sleep slightly longer than the contract duration to allow settlement."""
    wait = expiry_minutes * 60 + 5
    log.info(f"[BOT] Waiting {wait}s for contract settlement...")
    time.sleep(wait)


# ─────────────────────────────────────────
# Sleep until midnight for daily reset
# ─────────────────────────────────────────
def _sleep_until_midnight():
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=5, microsecond=0)
    # If already past midnight, go to next day
    from datetime import timedelta
    if midnight <= now:
        midnight += timedelta(days=1)
    seconds = (midnight - now).total_seconds()
    log.info(f"[BOT] Sleeping {seconds / 3600:.1f}h until midnight reset...")
    time.sleep(seconds)


# ─────────────────────────────────────────
# Entry point (when run directly)
# ─────────────────────────────────────────
if __name__ == "__main__":
    run_bot()

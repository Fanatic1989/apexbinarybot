import logging
import requests
from datetime import datetime

import config

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Internal sender
# ─────────────────────────────────────────
def _send(message: str, parse_mode: str = "HTML") -> bool:
    """
    Core Telegram message sender.
    Returns True on success, False on failure.
    Silently skips if token/chat ID are not configured.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("[TELEGRAM] Not configured — skipping message.")
        return False

    url  = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": parse_mode
    }

    try:
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code == 200:
            log.debug("[TELEGRAM] Message sent.")
            return True
        else:
            log.warning(f"[TELEGRAM] Send failed: {resp.status_code} — {resp.text}")
            return False
    except requests.exceptions.Timeout:
        log.warning("[TELEGRAM] Request timed out.")
        return False
    except Exception as e:
        log.error(f"[TELEGRAM] Unexpected error: {e}")
        return False


# ─────────────────────────────────────────
# Signal message
# ─────────────────────────────────────────
def send_signal(market: str,
                direction: str,
                expiry: int,
                confidence: str = "normal",
                stake: float = None) -> bool:
    """
    Send a formatted trading signal to Telegram.

    Example output:
    ⚡ TRADING SIGNAL

    📊 Market    : R_75
    📈 Direction : CALL ▲
    ⏱ Expiry    : 3 minutes
    🎯 Confidence: HIGH
    💰 Stake     : $5.00

    🕐 2024-01-15 14:32:01 UTC
    ─────────────────────────
    ⚠️ Trade at your own risk
    """
    direction_icon = "▲ CALL" if direction == "CALL" else "▼ PUT"
    conf_upper     = confidence.upper()

    if confidence == "high":
        conf_line = f"🔥 <b>{conf_upper}</b>"
        header    = "🔥 <b>HIGH CONFIDENCE SIGNAL</b>"
    else:
        conf_line = f"⚡ {conf_upper}"
        header    = "⚡ <b>TRADING SIGNAL</b>"

    stake_line = f"\n💰 <b>Stake</b>     : ${stake:.2f}" if stake else ""
    timestamp  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    message = (
        f"{header}\n"
        f"{'─' * 26}\n"
        f"📊 <b>Market</b>     : <code>{market}</code>\n"
        f"📈 <b>Direction</b>  : <b>{direction_icon}</b>\n"
        f"⏱ <b>Expiry</b>     : {expiry} minute{'s' if expiry > 1 else ''}\n"
        f"🎯 <b>Confidence</b> : {conf_line}"
        f"{stake_line}\n"
        f"{'─' * 26}\n"
        f"🕐 {timestamp}\n"
        f"⚠️ <i>Trade at your own risk</i>"
    )

    return _send(message)


# ─────────────────────────────────────────
# Alert message (bot status, errors, limits)
# ─────────────────────────────────────────
def send_alert(message: str) -> bool:
    """
    Send a plain alert message — bot start/stop, daily limits,
    pause events, errors, etc.
    """
    timestamp = datetime.utcnow().strftime("%H:%M:%S UTC")
    formatted = (
        f"🤖 <b>APEX BOT ALERT</b>\n"
        f"{'─' * 26}\n"
        f"{message}\n"
        f"{'─' * 26}\n"
        f"🕐 {timestamp}"
    )
    return _send(formatted)


# ─────────────────────────────────────────
# Trade result message
# ─────────────────────────────────────────
def send_trade_result(market: str,
                      direction: str,
                      stake: float,
                      result: str,
                      profit: float) -> bool:
    """
    Send win/loss result after a trade settles.

    result: "won" or "lost"
    profit: positive for win, stake amount for loss
    """
    if result == "won":
        icon   = "✅"
        label  = "WIN"
        pnl    = f"+${profit:.2f}"
    else:
        icon   = "❌"
        label  = "LOSS"
        pnl    = f"-${stake:.2f}"

    direction_icon = "▲ CALL" if direction == "CALL" else "▼ PUT"
    timestamp      = datetime.utcnow().strftime("%H:%M:%S UTC")

    message = (
        f"{icon} <b>TRADE {label}</b>\n"
        f"{'─' * 26}\n"
        f"📊 <b>Market</b>    : <code>{market}</code>\n"
        f"📈 <b>Direction</b> : {direction_icon}\n"
        f"💰 <b>Stake</b>     : ${stake:.2f}\n"
        f"💵 <b>P&amp;L</b>       : <b>{pnl}</b>\n"
        f"{'─' * 26}\n"
        f"🕐 {timestamp}"
    )
    return _send(message)


# ─────────────────────────────────────────
# Daily summary message
# ─────────────────────────────────────────
def send_daily_summary(summary: dict) -> bool:
    """
    Send end-of-day performance summary.
    Expects the dict returned by RiskManager.get_summary()
    """
    win_rate  = summary.get("win_rate", 0)
    net_pnl   = summary.get("net_pnl", 0)
    pnl_icon  = "📈" if net_pnl >= 0 else "📉"
    pnl_str   = f"+${net_pnl:.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):.2f}"
    timestamp = datetime.utcnow().strftime("%Y-%m-%d UTC")

    message = (
        f"📋 <b>DAILY SUMMARY — {timestamp}</b>\n"
        f"{'─' * 26}\n"
        f"💼 <b>Balance</b>      : ${summary.get('balance', 0):.2f}\n"
        f"📊 <b>Total trades</b> : {summary.get('total_trades', 0)}\n"
        f"✅ <b>Wins</b>         : {summary.get('wins', 0)}\n"
        f"❌ <b>Losses</b>       : {summary.get('losses', 0)}\n"
        f"🎯 <b>Win rate</b>     : {win_rate:.1f}%\n"
        f"{pnl_icon} <b>Net P&amp;L</b>      : <b>{pnl_str}</b>\n"
        f"📅 <b>Daily profit</b> : +${summary.get('daily_profit', 0):.2f}\n"
        f"📅 <b>Daily loss</b>   : -${summary.get('daily_loss', 0):.2f}\n"
        f"{'─' * 26}\n"
        f"🤖 Apex Binary Bot"
    )
    return _send(message)


# ─────────────────────────────────────────
# Startup message
# ─────────────────────────────────────────
def send_startup(mode: str, balance: float, markets: int) -> bool:
    """Send a notification when the bot starts up."""
    message = (
        f"🚀 <b>APEX BOT STARTED</b>\n"
        f"{'─' * 26}\n"
        f"⚙️ <b>Mode</b>     : {mode.upper()}\n"
        f"💰 <b>Balance</b>  : ${balance:.2f}\n"
        f"📡 <b>Markets</b>  : {markets} instruments\n"
        f"{'─' * 26}\n"
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    return _send(message)

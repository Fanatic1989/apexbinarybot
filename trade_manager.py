import json
import logging
import os
from datetime import datetime

import config
from deriv_api import get_balance as fetch_live_balance

log = logging.getLogger(__name__)

HISTORY_FILE = "trade_history.json"


# ─────────────────────────────────────────
# Trade Manager Class
# ─────────────────────────────────────────
class TradeManager:
    """
    Manages balance tracking, stake calculation,
    trade recording and history persistence.

    This is the single source of truth for all
    trade data in the bot. Both bot.py and
    server.py read from this class.
    """

    def __init__(self):
        self.balance          = 0.0
        self.starting_balance = 0.0
        self.total_trades     = 0
        self.total_wins       = 0
        self.total_losses     = 0
        self.net_pnl          = 0.0
        self.daily_profit     = 0.0
        self.daily_loss       = 0.0
        self._initialised     = False

    # ─────────────────────────────────────
    # Initialise with live balance
    # ─────────────────────────────────────
    def initialise(self):
        """
        Fetch real account balance from Deriv
        and set up the history file.
        Must be called once before trading starts.
        """
        live = fetch_live_balance()
        if live > 0:
            self.balance          = live
            self.starting_balance = live
            self._initialised     = True
            log.info(f"[MANAGER] Initialised | Balance: ${self.balance:.2f}")
        else:
            log.error("[MANAGER] Could not fetch live balance. Check token.")

        _ensure_history_file()

    # ─────────────────────────────────────
    # Stake calculation
    # ─────────────────────────────────────
    def calculate_stake(self) -> float:
        """
        Calculate stake as a percentage of current balance.
        Uses STAKE_PERCENT from config.
        Enforces Deriv minimum of $0.35.
        Hard caps at 2% regardless of config.
        """
        if self.balance <= 0:
            return 0.35

        raw   = self.balance * (config.STAKE_PERCENT / 100)
        stake = max(raw, 0.35)               # Deriv minimum
        stake = min(stake, self.balance * 0.02)  # hard cap at 2%
        return round(stake, 2)

    # ─────────────────────────────────────
    # Record a win
    # ─────────────────────────────────────
    def record_win(self, profit: float, stake: float):
        self.total_trades  += 1
        self.total_wins    += 1
        self.balance       += profit
        self.net_pnl       += profit
        self.daily_profit  += profit
        log.info(f"[MANAGER] ✅ WIN  +${profit:.2f} | "
                 f"Balance: ${self.balance:.2f} | "
                 f"Net P&L: ${self.net_pnl:.2f}")

    # ─────────────────────────────────────
    # Record a loss
    # ─────────────────────────────────────
    def record_loss(self, stake: float):
        self.total_trades  += 1
        self.total_losses  += 1
        self.balance       -= stake
        self.net_pnl       -= stake
        self.daily_loss    += stake
        log.info(f"[MANAGER] ❌ LOSS -${stake:.2f} | "
                 f"Balance: ${self.balance:.2f} | "
                 f"Net P&L: ${self.net_pnl:.2f}")

    # ─────────────────────────────────────
    # Save trade to history file
    # ─────────────────────────────────────
    def save_trade(self, trade: dict):
        """
        Persist a completed trade record to
        trade_history.json.

        Expected trade keys:
            contract_id, symbol, direction,
            stake, payout, result, profit,
            expiry, confidence
        """
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = _empty_history()

        record = {
            "contract_id": trade.get("contract_id", "—"),
            "symbol":      trade.get("symbol",      "—"),
            "direction":   trade.get("direction",   "—"),
            "stake":       round(float(trade.get("stake",  0)), 2),
            "payout":      round(float(trade.get("payout", 0)), 2),
            "result":      trade.get("result",      "unknown"),
            "profit":      round(float(trade.get("profit", 0)), 2),
            "expiry":      trade.get("expiry",      "—"),
            "confidence":  trade.get("confidence",  "normal"),
            "time":        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        }

        data["trades"].append(record)

        # Update summary counters in file
        data["total_trades"] = self.total_trades
        data["total_wins"]   = self.total_wins
        data["total_losses"] = self.total_losses
        data["net_pnl"]      = round(self.net_pnl, 2)

        # Cap at 500 records
        if len(data["trades"]) > 500:
            data["trades"] = data["trades"][-500:]

        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            log.error(f"[MANAGER] Failed to write history: {e}")

    # ─────────────────────────────────────
    # Read history for dashboard
    # ─────────────────────────────────────
    def get_history(self, limit: int = 100) -> dict:
        """Return trade history for the dashboard API."""
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
            trades = list(reversed(data.get("trades", [])))[:limit]
            return {
                "trades":       trades,
                "total_trades": data.get("total_trades", self.total_trades),
                "total_wins":   data.get("total_wins",   self.total_wins),
                "total_losses": data.get("total_losses", self.total_losses),
                "net_pnl":      data.get("net_pnl",      round(self.net_pnl, 2))
            }
        except Exception as e:
            log.error(f"[MANAGER] Failed to read history: {e}")
            return _empty_history()

    # ─────────────────────────────────────
    # Win rate
    # ─────────────────────────────────────
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return round((self.total_wins / self.total_trades) * 100, 1)

    # ─────────────────────────────────────
    # Summary dict for status API
    # ─────────────────────────────────────
    def get_summary(self) -> dict:
        return {
            "balance":      round(self.balance, 2),
            "starting_bal": round(self.starting_balance, 2),
            "total_trades": self.total_trades,
            "wins":         self.total_wins,
            "losses":       self.total_losses,
            "win_rate":     self.win_rate,
            "net_pnl":      round(self.net_pnl, 2),
            "daily_profit": round(self.daily_profit, 2),
            "daily_loss":   round(self.daily_loss, 2),
        }

    # ─────────────────────────────────────
    # Daily reset
    # ─────────────────────────────────────
    def reset_daily(self):
        self.daily_profit = 0.0
        self.daily_loss   = 0.0
        # Re-sync balance from Deriv
        live = fetch_live_balance()
        if live > 0:
            self.balance = live
        log.info(f"[MANAGER] Daily reset | Balance: ${self.balance:.2f}")

    # ─────────────────────────────────────
    # Clear history
    # ─────────────────────────────────────
    def clear_history(self):
        with open(HISTORY_FILE, "w") as f:
            json.dump(_empty_history(), f, indent=4)
        log.info("[MANAGER] Trade history cleared.")


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def _empty_history() -> dict:
    return {
        "trades":       [],
        "total_trades": 0,
        "total_wins":   0,
        "total_losses": 0,
        "net_pnl":      0.0
    }


def _ensure_history_file():
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w") as f:
            json.dump(_empty_history(), f, indent=4)
        log.info(f"[MANAGER] Created {HISTORY_FILE}")


# ─────────────────────────────────────────
# Singleton instance
# ─────────────────────────────────────────
# Import this in bot.py and server.py:
# from trade_manager import trade_manager
trade_manager = TradeManager()

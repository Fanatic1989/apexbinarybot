import time
import logging
from datetime import datetime

import config

log = logging.getLogger(__name__)


class RiskManager:
    """
    Centralised risk management for the Deriv bot.

    Tracks:
      - Account balance and daily P&L
      - Consecutive losses and pause state
      - Daily loss limit and profit target
      - Per-session trade statistics
    """

    def __init__(self, starting_balance: float):
        # ── Balance ──────────────────────────
        self.starting_balance  = starting_balance
        self.current_balance   = starting_balance
        self.daily_start_bal   = starting_balance

        # ── Session stats ────────────────────
        self.total_trades      = 0
        self.total_wins        = 0
        self.total_losses      = 0
        self.total_profit      = 0.0
        self.total_loss_amount = 0.0

        # ── Daily stats ──────────────────────
        self.daily_profit      = 0.0
        self.daily_loss        = 0.0

        # ── Consecutive loss tracking ────────
        self.consecutive_losses = 0

        # ── Pause state ──────────────────────
        self._paused_until     = None   # epoch timestamp

        # ── Session start ────────────────────
        self.session_start     = datetime.utcnow()

        log.info(f"[RISK] Initialised | Balance: ${starting_balance:.2f} | "
                 f"Stake: {config.STAKE_PERCENT}% | "
                 f"Daily loss limit: {config.MAX_DAILY_LOSS_PCT}% | "
                 f"Daily target: {config.DAILY_PROFIT_TARGET}%")

    # ─────────────────────────────────────────
    # Stake calculation
    # ─────────────────────────────────────────
    def calculate_stake(self) -> float:
        """
        Return stake amount based on current balance and STAKE_PERCENT.
        Enforces a minimum stake of $0.35 (Deriv's minimum).
        """
        stake = round(self.current_balance * (config.STAKE_PERCENT / 100), 2)
        stake = max(stake, 0.35)   # Deriv minimum stake
        stake = min(stake, self.current_balance * 0.02)  # hard cap at 2% even if config says more
        return round(stake, 2)

    # ─────────────────────────────────────────
    # Record outcomes
    # ─────────────────────────────────────────
    def record_win(self, profit: float):
        """Call this after a winning trade."""
        self.total_trades      += 1
        self.total_wins        += 1
        self.total_profit      += profit
        self.daily_profit      += profit
        self.current_balance   += profit
        self.consecutive_losses = 0   # reset on win

        log.info(f"[RISK] ✅ WIN  +${profit:.2f} | "
                 f"Balance: ${self.current_balance:.2f} | "
                 f"Daily P&L: +${self.daily_profit:.2f}")
        self._log_stats()

    def record_loss(self, stake: float):
        """Call this after a losing trade."""
        self.total_trades       += 1
        self.total_losses       += 1
        self.total_loss_amount  += stake
        self.daily_loss         += stake
        self.current_balance    -= stake
        self.consecutive_losses += 1

        log.warning(f"[RISK] ❌ LOSS -${stake:.2f} | "
                    f"Balance: ${self.current_balance:.2f} | "
                    f"Consecutive: {self.consecutive_losses} | "
                    f"Daily loss: -${self.daily_loss:.2f}")
        self._log_stats()

    # ─────────────────────────────────────────
    # Pause logic
    # ─────────────────────────────────────────
    def trigger_pause(self):
        """Pause trading for PAUSE_DURATION seconds."""
        self._paused_until = time.time() + config.PAUSE_DURATION
        resume_at = datetime.utcfromtimestamp(self._paused_until).strftime("%H:%M:%S UTC")
        log.warning(f"[RISK] ⏸  Bot paused for "
                    f"{config.PAUSE_DURATION // 60} minutes. "
                    f"Resuming at {resume_at}")
        self.consecutive_losses = 0   # reset counter after pause

    def is_paused(self) -> bool:
        """Return True if the bot is currently in a pause window."""
        if self._paused_until is None:
            return False
        if time.time() < self._paused_until:
            return True
        # Pause expired — clear it
        self._paused_until = None
        log.info("[RISK] ▶️  Pause expired. Resuming trading.")
        return False

    def pause_remaining(self) -> float:
        """Return seconds remaining in current pause. 0 if not paused."""
        if self._paused_until is None:
            return 0.0
        remaining = self._paused_until - time.time()
        return max(remaining, 0.0)

    # ─────────────────────────────────────────
    # Daily limit checks
    # ─────────────────────────────────────────
    def daily_loss_limit_hit(self) -> bool:
        """Return True if daily loss has exceeded MAX_DAILY_LOSS_PCT."""
        if self.daily_start_bal <= 0:
            return False
        loss_pct = (self.daily_loss / self.daily_start_bal) * 100
        if loss_pct >= config.MAX_DAILY_LOSS_PCT:
            log.warning(f"[RISK] 🛑 Daily loss limit hit: "
                        f"-${self.daily_loss:.2f} ({loss_pct:.1f}%)")
            return True
        return False

    def daily_profit_target_hit(self) -> bool:
        """Return True if daily profit has reached DAILY_PROFIT_TARGET."""
        if self.daily_start_bal <= 0:
            return False
        profit_pct = (self.daily_profit / self.daily_start_bal) * 100
        if profit_pct >= config.DAILY_PROFIT_TARGET:
            log.info(f"[RISK] 🎯 Daily profit target hit: "
                     f"+${self.daily_profit:.2f} ({profit_pct:.1f}%)")
            return True
        return False

    # ─────────────────────────────────────────
    # Status check (replaces old check_risk())
    # ─────────────────────────────────────────
    def status(self) -> str:
        """
        Returns one of: "TRADE", "PAUSE", "STOP"

        TRADE — safe to place next trade
        PAUSE — temporarily halted (consecutive losses)
        STOP  — halted for the day (daily limits)
        """
        if self.daily_loss_limit_hit() or self.daily_profit_target_hit():
            return "STOP"
        if self.is_paused():
            return "PAUSE"
        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSS:
            self.trigger_pause()
            return "PAUSE"
        return "TRADE"

    # ─────────────────────────────────────────
    # Daily reset (called at midnight)
    # ─────────────────────────────────────────
    def reset_daily(self, new_balance: float = None):
        """Reset daily counters. Called at midnight or start of new session."""
        self.daily_profit    = 0.0
        self.daily_loss      = 0.0
        self.daily_start_bal = new_balance or self.current_balance
        self.current_balance = self.daily_start_bal
        self._paused_until   = None
        self.consecutive_losses = 0
        log.info(f"[RISK] 🔄 Daily reset | New balance: ${self.daily_start_bal:.2f}")

    # ─────────────────────────────────────────
    # Session summary
    # ─────────────────────────────────────────
    def get_summary(self) -> dict:
        """Return a full session summary dict for dashboard/Telegram reporting."""
        win_rate = (
            round((self.total_wins / self.total_trades) * 100, 1)
            if self.total_trades > 0 else 0.0
        )
        net_pnl = self.total_profit - self.total_loss_amount
        return {
            "balance":        round(self.current_balance, 2),
            "starting_bal":   round(self.starting_balance, 2),
            "total_trades":   self.total_trades,
            "wins":           self.total_wins,
            "losses":         self.total_losses,
            "win_rate":       win_rate,
            "net_pnl":        round(net_pnl, 2),
            "daily_profit":   round(self.daily_profit, 2),
            "daily_loss":     round(self.daily_loss, 2),
            "consec_losses":  self.consecutive_losses,
            "status":         self.status(),
            "session_start":  self.session_start.strftime("%Y-%m-%d %H:%M UTC")
        }

    # ─────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────
    def _log_stats(self):
        win_rate = (
            round((self.total_wins / self.total_trades) * 100, 1)
            if self.total_trades > 0 else 0.0
        )
        log.info(f"[RISK] Stats — Trades: {self.total_trades} | "
                 f"W: {self.total_wins} L: {self.total_losses} | "
                 f"Win rate: {win_rate}% | "
                 f"Net P&L: ${self.total_profit - self.total_loss_amount:.2f}")

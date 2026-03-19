import logging
import os

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Staking Strategy Engine
# ─────────────────────────────────────────
# Set STAKING_STRATEGY in Render env vars:
#   flat        — fixed % of balance (default, safest)
#   dalembert   — +1 unit after loss, -1 unit after win
#   oscar       — increment only after a win that completes a profit cycle
#   strategy1326— 1→3→2→6 unit progression on wins, reset on loss
#   martingale  — double after loss (DANGEROUS — use only on demo)
# ─────────────────────────────────────────

STRATEGY = os.getenv("STAKING_STRATEGY", "flat").lower()


class StakingEngine:
    """
    Manages stake sizing based on selected strategy.
    All strategies respect a hard max cap to protect balance.
    """

    def __init__(self, base_stake: float, balance: float):
        self.base_stake      = base_stake      # starting unit size
        self.current_stake   = base_stake
        self.balance         = balance
        self.strategy        = STRATEGY

        # D'Alembert state
        self.dalembert_unit  = base_stake
        self.dalembert_level = 1              # units to bet

        # Oscar's Grind state
        self.oscar_session_profit = 0.0
        self.oscar_level          = 1

        # 1-3-2-6 state
        self.sequence_1326   = [1, 3, 2, 6]
        self.seq_position    = 0

        # Martingale state
        self.martingale_stake = base_stake

        log.info(f"[STAKING] Strategy: {self.strategy.upper()} | "
                 f"Base stake: ${base_stake:.2f}")

    def get_stake(self) -> float:
        """Return current stake for next trade."""
        stake = self._raw_stake()
        # Hard cap: never risk more than 3% of balance
        max_stake = self.balance * 0.03
        stake = min(stake, max_stake)
        stake = max(stake, 0.35)   # Deriv minimum
        return round(stake, 2)

    def record_win(self, profit: float):
        """Update staking state after a win."""
        self.balance += profit
        self._update_balance(profit)

        if self.strategy == "dalembert":
            # Decrease by 1 unit after win
            self.dalembert_level = max(1, self.dalembert_level - 1)
            self.current_stake = self.dalembert_unit * self.dalembert_level
            log.info(f"[STAKING] D'Alembert WIN → level {self.dalembert_level} "
                     f"→ stake ${self.current_stake:.2f}")

        elif self.strategy == "oscar":
            self.oscar_session_profit += profit
            if self.oscar_session_profit >= self.base_stake:
                # Completed a profit unit — reset
                self.oscar_session_profit = 0
                self.oscar_level = 1
                log.info(f"[STAKING] Oscar's Grind WIN cycle complete → reset")
            else:
                # Increment for next win
                self.oscar_level = min(self.oscar_level + 1, 8)
                log.info(f"[STAKING] Oscar's Grind WIN → level {self.oscar_level}")

        elif self.strategy == "strategy1326":
            self.seq_position += 1
            if self.seq_position >= len(self.sequence_1326):
                self.seq_position = 0
                log.info(f"[STAKING] 1-3-2-6 WIN cycle complete → reset to 1")
            else:
                log.info(f"[STAKING] 1-3-2-6 WIN → position {self.seq_position} "
                         f"(×{self.sequence_1326[self.seq_position]})")

        elif self.strategy == "martingale":
            # Reset to base after win
            self.martingale_stake = self.base_stake
            log.info(f"[STAKING] Martingale WIN → reset to ${self.base_stake:.2f}")

        elif self.strategy == "flat":
            # Recalculate base stake from updated balance if compounding
            import config
            if config.COMPOUND:
                self.base_stake  = self.balance * (config.STAKE_PERCENT / 100)
                self.current_stake = self.base_stake

    def record_loss(self, stake: float):
        """Update staking state after a loss."""
        self.balance -= stake
        self._update_balance(-stake)

        if self.strategy == "dalembert":
            # Increase by 1 unit after loss
            self.dalembert_level += 1
            self.current_stake = self.dalembert_unit * self.dalembert_level
            log.info(f"[STAKING] D'Alembert LOSS → level {self.dalembert_level} "
                     f"→ stake ${self.current_stake:.2f}")

        elif self.strategy == "oscar":
            # Keep same level on loss — never increase on loss
            self.oscar_session_profit -= stake
            log.info(f"[STAKING] Oscar's Grind LOSS → keep level {self.oscar_level} "
                     f"→ session P&L ${self.oscar_session_profit:.2f}")

        elif self.strategy == "strategy1326":
            # Reset to start on any loss
            self.seq_position = 0
            log.info(f"[STAKING] 1-3-2-6 LOSS → reset to position 0 (×1)")

        elif self.strategy == "martingale":
            # Double stake after loss
            self.martingale_stake = min(
                self.martingale_stake * 2,
                self.balance * 0.03   # hard cap
            )
            log.info(f"[STAKING] Martingale LOSS → double to ${self.martingale_stake:.2f}")

        elif self.strategy == "flat":
            import config
            if config.COMPOUND:
                self.base_stake   = self.balance * (config.STAKE_PERCENT / 100)
                self.current_stake = self.base_stake

    def update_balance(self, new_balance: float):
        """Sync balance from live account."""
        self.balance = new_balance
        if self.strategy == "flat":
            import config
            self.base_stake    = new_balance * (config.STAKE_PERCENT / 100)
            self.current_stake = self.base_stake

    def _raw_stake(self) -> float:
        if self.strategy == "dalembert":
            return self.dalembert_unit * self.dalembert_level
        elif self.strategy == "oscar":
            return self.base_stake * self.oscar_level
        elif self.strategy == "strategy1326":
            mult = self.sequence_1326[self.seq_position]
            return self.base_stake * mult
        elif self.strategy == "martingale":
            return self.martingale_stake
        else:
            return self.current_stake

    def _update_balance(self, delta: float):
        """Keep base stake in sync with balance for flat/compound."""
        pass

    def get_info(self) -> dict:
        """Return current staking state for dashboard."""
        return {
            "strategy":      self.strategy,
            "base_stake":    round(self.base_stake, 2),
            "current_stake": round(self.get_stake(), 2),
            "balance":       round(self.balance, 2),
        }

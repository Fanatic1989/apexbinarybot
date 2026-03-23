"""
Risk State Persistence — saves and restores RiskManager state across restarts.
Prevents the bot from resetting loss counts and daily P&L on restart.
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)
STATE_FILE = "risk_state.json"


def save_state(risk_manager, staking_engine=None):
    """Save current risk state to disk."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = {
            "date":               today,
            "current_balance":    float(risk_manager.current_balance),
            "starting_balance":   float(risk_manager.starting_balance),
            "total_trades":       int(risk_manager.total_trades),
            "total_wins":         int(risk_manager.total_wins),
            "total_losses":       int(risk_manager.total_losses),
            "net_pnl":            float(risk_manager.net_pnl),
            "consecutive_losses": int(risk_manager.consecutive_losses),
            "daily_loss":         float(risk_manager.daily_loss),
            "daily_profit":       float(getattr(risk_manager, "daily_profit", 0)),
            "paused_until":       getattr(risk_manager, "_paused_until", 0),
        }
        if staking_engine:
            state["staking"] = {
                "base_stake":    float(staking_engine.base_stake),
                "current_stake": float(staking_engine.current_stake),
                "balance":       float(staking_engine.balance),
            }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log.debug(f"[STATE] Saved — Balance: ${state['current_balance']:.2f} | "
                  f"W:{state['total_wins']} L:{state['total_losses']} | "
                  f"Consec: {state['consecutive_losses']}")
    except Exception as e:
        log.error(f"[STATE] Save failed: {e}")


def load_state() -> dict:
    """Load persisted risk state. Returns None if no state or stale (different day)."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("date") != today:
            log.info(f"[STATE] New day — resetting daily stats (was {state.get('date')})")
            # Keep cumulative stats but reset daily ones
            state["daily_loss"]         = 0.0
            state["daily_profit"]       = 0.0
            state["consecutive_losses"] = 0
            state["paused_until"]       = 0
            state["date"]               = today
        log.info(f"[STATE] Restored — Balance: ${state['current_balance']:.2f} | "
                 f"W:{state['total_wins']} L:{state['total_losses']} | "
                 f"Consec: {state['consecutive_losses']} | "
                 f"Daily loss: ${state['daily_loss']:.2f}")
        return state
    except FileNotFoundError:
        log.info("[STATE] No saved state — starting fresh")
        return None
    except Exception as e:
        log.error(f"[STATE] Load failed: {e}")
        return None


def restore_risk_manager(risk_manager, state: dict):
    """Apply saved state to a RiskManager instance."""
    if not state:
        return
    try:
        risk_manager.current_balance    = float(state["current_balance"])
        risk_manager.starting_balance   = float(state["starting_balance"])
        risk_manager.total_trades       = int(state["total_trades"])
        risk_manager.total_wins         = int(state["total_wins"])
        risk_manager.total_losses       = int(state["total_losses"])
        risk_manager.net_pnl            = float(state["net_pnl"])
        risk_manager.consecutive_losses = int(state["consecutive_losses"])
        risk_manager.daily_loss         = float(state["daily_loss"])
        if hasattr(risk_manager, "daily_profit"):
            risk_manager.daily_profit   = float(state.get("daily_profit", 0))
        # Restore pause if it was active
        paused_until = float(state.get("paused_until", 0))
        if paused_until > 0:
            risk_manager._paused_until  = paused_until
        log.info(f"[STATE] RiskManager restored — "
                 f"Consec losses: {risk_manager.consecutive_losses} | "
                 f"Daily loss: ${risk_manager.daily_loss:.2f}")
    except Exception as e:
        log.error(f"[STATE] RiskManager restore failed: {e}")


def restore_staking(staking_engine, state: dict):
    """Apply saved staking state."""
    if not state or "staking" not in state:
        return
    try:
        s = state["staking"]
        staking_engine.base_stake    = float(s["base_stake"])
        staking_engine.current_stake = float(s["current_stake"])
        staking_engine.balance       = float(s["balance"])
        log.info(f"[STATE] Staking restored — "
                 f"Stake: ${staking_engine.current_stake:.2f}")
    except Exception as e:
        log.error(f"[STATE] Staking restore failed: {e}")

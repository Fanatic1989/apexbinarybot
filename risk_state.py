"""
Risk State Persistence — saves and restores RiskManager state across restarts.
Prevents the bot from resetting loss counts and daily P&L on restart.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)
STATE_FILE = "risk_state.json"


def _sf(val, default=0.0) -> float:
    """Safe float — returns default if val is None or unconvertible."""
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _si(val, default=0) -> int:
    """Safe int — returns default if val is None or unconvertible."""
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _get(obj, *attrs, default=0):
    """Try multiple attribute names, return first that exists and isn't None."""
    for attr in attrs:
        v = getattr(obj, attr, None)
        if v is not None:
            return v
    return default


def save_state(risk_manager, staking_engine=None):
    """Save current risk state to disk after every trade."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = {
            "date":               today,
            "current_balance":    _sf(_get(risk_manager, "current_balance", "balance")),
            "starting_balance":   _sf(_get(risk_manager, "starting_balance")),
            "total_trades":       _si(_get(risk_manager, "total_trades")),
            "total_wins":         _si(_get(risk_manager, "total_wins", "wins")),
            "total_losses":       _si(_get(risk_manager, "total_losses", "losses")),
            "net_pnl":            _sf(_get(risk_manager, "net_pnl")),
            "consecutive_losses": _si(_get(risk_manager, "consecutive_losses", "consec_losses")),
            "daily_loss":         _sf(_get(risk_manager, "daily_loss")),
            "daily_profit":       _sf(_get(risk_manager, "daily_profit")),
            "paused_until":       _sf(_get(risk_manager, "_paused_until", "paused_until")),
        }
        if staking_engine:
            state["staking"] = {
                "base_stake":    _sf(_get(staking_engine, "base_stake")),
                "current_stake": _sf(_get(staking_engine, "current_stake")),
                "balance":       _sf(_get(staking_engine, "balance")),
            }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log.debug(f"[STATE] Saved | Balance:${state['current_balance']:.2f} "
                  f"W:{state['total_wins']} L:{state['total_losses']} "
                  f"Consec:{state['consecutive_losses']} "
                  f"DailyLoss:${state['daily_loss']:.2f}")
    except Exception as e:
        log.error(f"[STATE] Save failed: {e}")


def load_state() -> dict:
    """Load saved state. Resets daily stats if it's a new day."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("date") != today:
            log.info(f"[STATE] New day — resetting daily stats "
                     f"(was {state.get('date')}, now {today})")
            state["daily_loss"]         = 0.0
            state["daily_profit"]       = 0.0
            state["consecutive_losses"] = 0
            state["paused_until"]       = 0.0
            state["date"]               = today

        log.info(f"[STATE] Restored | Balance:${state.get('current_balance',0):.2f} "
                 f"W:{state.get('total_wins',0)} L:{state.get('total_losses',0)} "
                 f"Consec:{state.get('consecutive_losses',0)} "
                 f"DailyLoss:${state.get('daily_loss',0):.2f}")
        return state

    except FileNotFoundError:
        log.info("[STATE] No saved state — starting fresh")
        return None
    except Exception as e:
        log.error(f"[STATE] Load failed: {e}")
        return None


def restore_risk_manager(risk_manager, state: dict):
    """Apply saved state fields to RiskManager instance."""
    if not state:
        return
    try:
        fields = [
            ("current_balance",    "current_balance",    float),
            ("starting_balance",   "starting_balance",   float),
            ("total_trades",       "total_trades",       int),
            ("total_wins",         "total_wins",         int),
            ("total_losses",       "total_losses",       int),
            ("net_pnl",            "net_pnl",            float),
            ("consecutive_losses", "consecutive_losses", int),
            ("daily_loss",         "daily_loss",         float),
            ("daily_profit",       "daily_profit",       float),
        ]
        for state_key, attr, cast in fields:
            val = state.get(state_key)
            if val is not None and hasattr(risk_manager, attr):
                try:
                    setattr(risk_manager, attr, cast(val))
                except: pass

        # Restore pause if still active
        paused_until = _sf(state.get("paused_until", 0))
        if paused_until > 0 and hasattr(risk_manager, "_paused_until"):
            risk_manager._paused_until = paused_until

        log.info(f"[STATE] RiskManager restored | "
                 f"Consec:{risk_manager.consecutive_losses} "
                 f"DailyLoss:${risk_manager.daily_loss:.2f}")
    except Exception as e:
        log.error(f"[STATE] RiskManager restore failed: {e}")


def restore_staking(staking_engine, state: dict):
    """Apply saved staking state."""
    if not state or "staking" not in state:
        return
    try:
        s = state["staking"]
        for attr in ("base_stake", "current_stake", "balance"):
            if attr in s and hasattr(staking_engine, attr):
                setattr(staking_engine, attr, _sf(s[attr]))
        log.info(f"[STATE] Staking restored | "
                 f"Stake:${staking_engine.current_stake:.2f}")
    except Exception as e:
        log.error(f"[STATE] Staking restore failed: {e}")

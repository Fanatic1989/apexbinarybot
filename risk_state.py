"""
Risk State Persistence — saves and restores RiskManager state across restarts.
Prevents loss counts and daily P&L from resetting on bot restart.
Uses getattr/setattr safely to handle different RiskManager implementations.
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)
STATE_FILE = "risk_state.json"


def _rm_get(risk_manager, *attrs, default=0):
    """Try multiple attribute names on risk_manager, return first found."""
    # Try get_summary() dict first
    summary = {}
    if hasattr(risk_manager, "get_summary"):
        try:
            summary = risk_manager.get_summary() or {}
        except: pass

    for attr in attrs:
        # Direct attribute
        val = getattr(risk_manager, attr, None)
        if val is not None:
            return val
        # In summary dict
        val = summary.get(attr)
        if val is not None:
            return val
    return default


def save_state(risk_manager, staking_engine=None):
    """Save current risk state to disk."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = {
            "date":               today,
            "current_balance":    float(_rm_get(risk_manager, "current_balance", "balance", default=0)),
            "starting_balance":   float(_rm_get(risk_manager, "starting_balance", default=0)),
            "total_trades":       int(_rm_get(risk_manager,   "total_trades",     default=0)),
            "total_wins":         int(_rm_get(risk_manager,   "total_wins", "wins", default=0)),
            "total_losses":       int(_rm_get(risk_manager,   "total_losses", "losses", default=0)),
            "net_pnl":            float(_rm_get(risk_manager, "net_pnl",           default=0)),
            "consecutive_losses": int(_rm_get(risk_manager,   "consecutive_losses", "consec_losses", default=0)),
            "daily_loss":         float(_rm_get(risk_manager, "daily_loss",         default=0)),
            "daily_profit":       float(_rm_get(risk_manager, "daily_profit",       default=0)),
            "paused_until":       float(getattr(risk_manager, "_paused_until", 0)),
        }
        if staking_engine:
            state["staking"] = {
                "base_stake":    float(getattr(staking_engine, "base_stake",    0)),
                "current_stake": float(getattr(staking_engine, "current_stake", 0)),
                "balance":       float(getattr(staking_engine, "balance",       0)),
            }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log.debug(f"[STATE] Saved — Bal:${state['current_balance']:.2f} "
                  f"W:{state['total_wins']} L:{state['total_losses']} "
                  f"Consec:{state['consecutive_losses']}")
    except Exception as e:
        log.error(f"[STATE] Save failed: {e}")


def load_state() -> dict:
    """Load persisted state. Resets daily stats on new day."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("date") != today:
            log.info(f"[STATE] New day — resetting daily stats")
            state["daily_loss"]         = 0.0
            state["daily_profit"]       = 0.0
            state["consecutive_losses"] = 0
            state["paused_until"]       = 0
            state["date"]               = today
        log.info(f"[STATE] Restored — Bal:${state['current_balance']:.2f} "
                 f"W:{state['total_wins']} L:{state['total_losses']} "
                 f"Consec:{state['consecutive_losses']} "
                 f"DailyLoss:${state['daily_loss']:.2f}")
        return state
    except FileNotFoundError:
        log.info("[STATE] No saved state — starting fresh")
        return None
    except Exception as e:
        log.error(f"[STATE] Load failed: {e}")
        return None


def restore_risk_manager(risk_manager, state: dict):
    """Restore RiskManager from saved state using safe setattr."""
    if not state:
        return
    try:
        def _set(attr, key, cast=float):
            if key in state and hasattr(risk_manager, attr):
                try:
                    setattr(risk_manager, attr, cast(state[key]))
                except: pass

        _set("current_balance",    "current_balance")
        _set("starting_balance",   "starting_balance")
        _set("total_trades",       "total_trades",       int)
        _set("total_wins",         "total_wins",         int)
        _set("total_losses",       "total_losses",       int)
        _set("net_pnl",            "net_pnl")
        _set("consecutive_losses", "consecutive_losses", int)
        _set("daily_loss",         "daily_loss")
        _set("daily_profit",       "daily_profit")

        # Restore pause if still active
        paused_until = float(state.get("paused_until", 0))
        import time
        if paused_until > time.time():
            if hasattr(risk_manager, "_paused_until"):
                risk_manager._paused_until = paused_until
                log.info(f"[STATE] Pause restored — "
                         f"{(paused_until - time.time())/60:.1f}min remaining")

        log.info(f"[STATE] RiskManager restored — "
                 f"Consec:{getattr(risk_manager,'consecutive_losses',0)} "
                 f"DailyLoss:${getattr(risk_manager,'daily_loss',0):.2f}")
    except Exception as e:
        log.error(f"[STATE] Restore failed: {e}")


def restore_staking(staking_engine, state: dict):
    """Restore staking engine from saved state."""
    if not state or "staking" not in state:
        return
    try:
        s = state["staking"]
        for attr in ("base_stake", "current_stake", "balance"):
            if attr in s and hasattr(staking_engine, attr):
                setattr(staking_engine, attr, float(s[attr]))
        log.info(f"[STATE] Staking restored — "
                 f"Stake:${getattr(staking_engine,'current_stake',0):.2f}")
    except Exception as e:
        log.error(f"[STATE] Staking restore failed: {e}")

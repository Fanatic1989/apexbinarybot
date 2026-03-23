"""
User Manager — handles registration, authentication,
subscription status and per-user bot instances.
"""
import json
import os
import hashlib
import secrets
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

USERS_FILE = "users.json"
TRIAL_DAYS = 7
SUB_PRICE  = 79.00  # USD


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()


def _load() -> dict:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except:
        return {"users": {}}


def _save(data: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Registration ──────────────────────────────────────────────

def register_user(username: str, password: str,
                  email: str = "") -> dict:
    data = _load()
    username = username.strip().lower()

    if not username or len(username) < 3:
        return {"ok": False, "error": "Username must be at least 3 characters"}
    if not password or len(password) < 6:
        return {"ok": False, "error": "Password must be at least 6 characters"}
    if username in data["users"]:
        return {"ok": False, "error": "Username already taken"}

    # Check email uniqueness if provided
    if email:
        for u in data["users"].values():
            if u.get("email", "").lower() == email.lower():
                return {"ok": False, "error": "Email already registered"}

    salt  = secrets.token_hex(16)
    now   = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=TRIAL_DAYS)

    data["users"][username] = {
        "username":        username,
        "email":           email.strip().lower(),
        "password_hash":   _hash_password(password, salt),
        "salt":            salt,
        "role":            "user",
        "created_at":      now.isoformat(),
        "trial_ends":      trial_end.isoformat(),
        "subscription_end": None,
        "active":          True,
        "suspended":       False,
        # Bot settings
        "demo_token":      "",
        "live_token":      "",
        "mode":            "demo",
        "risk_pct":        1,
        "bot_running":     False,
        "account_type":    "paid",   # paid | free
        # Stats
        "total_trades":    0,
        "total_wins":      0,
        "total_losses":    0,
        "net_pnl":         0.0,
    }

    _save(data)
    log.info(f"[USERS] Registered: {username} (trial until {trial_end.date()})")
    return {"ok": True, "username": username, "trial_ends": trial_end.isoformat()}


# ── Authentication ────────────────────────────────────────────

def authenticate(username: str, password: str) -> Optional[dict]:
    data  = _load()
    uname = username.strip().lower()
    user  = data["users"].get(uname)
    if not user:
        return None
    if user.get("suspended"):
        return None
    expected = _hash_password(password, user["salt"])
    if expected != user["password_hash"]:
        return None
    return user


# ── Subscription status ───────────────────────────────────────

def get_subscription_status(username: str) -> dict:
    data = _load()
    user = data["users"].get(username.lower())
    if not user:
        return {"status": "not_found"}

    if user.get("suspended"):
        return {"status": "suspended"}

    now = datetime.now(timezone.utc)

    # Check active subscription first
    sub_end = user.get("subscription_end")
    if sub_end:
        sub_dt = datetime.fromisoformat(sub_end)
        if sub_dt > now:
            days_left = (sub_dt - now).days
            return {
                "status":    "active",
                "days_left": days_left,
                "ends":      sub_end,
                "plan":      "monthly",
            }

    # Check trial
    trial_end = user.get("trial_ends")
    if trial_end:
        trial_dt = datetime.fromisoformat(trial_end)
        if trial_dt > now:
            days_left = (trial_dt - now).days
            hours_left = int((trial_dt - now).total_seconds() / 3600)
            return {
                "status":     "trial",
                "days_left":  days_left,
                "hours_left": hours_left,
                "ends":       trial_end,
            }

    # Expired
    return {
        "status":   "expired",
        "days_left": 0,
        "price":    SUB_PRICE,
    }


def is_allowed_to_trade(username: str) -> bool:
    status = get_subscription_status(username)
    return status["status"] in ("active", "trial")


# ── User settings ─────────────────────────────────────────────

def update_user_settings(username: str, **kwargs) -> dict:
    data  = _load()
    uname = username.lower()
    user  = data["users"].get(uname)
    if not user:
        return {"ok": False, "error": "User not found"}

    allowed = {
        "demo_token", "live_token", "mode",
        "risk_pct", "email", "bot_running",
        "total_trades", "total_wins",
        "total_losses", "net_pnl",
        "account_type",
    }
    for k, v in kwargs.items():
        if k in allowed:
            user[k] = v

    _save(data)
    return {"ok": True}


def get_user(username: str) -> Optional[dict]:
    data = _load()
    return data["users"].get(username.lower())


def get_all_users() -> list:
    data = _load()
    users = []
    for u in data["users"].values():
        status = get_subscription_status(u["username"])
        users.append({**u, "sub_status": status})
    return users


# ── Admin controls ────────────────────────────────────────────

def admin_extend_subscription(username: str, days: int = 30) -> dict:
    data  = _load()
    uname = username.lower()
    user  = data["users"].get(uname)
    if not user:
        return {"ok": False, "error": "User not found"}

    now     = datetime.now(timezone.utc)
    current = user.get("subscription_end")

    if current:
        base = max(datetime.fromisoformat(current), now)
    else:
        base = now

    new_end = base + timedelta(days=days)
    user["subscription_end"] = new_end.isoformat()
    _save(data)
    log.info(f"[USERS] Extended {username} subscription to {new_end.date()}")
    return {"ok": True, "new_end": new_end.isoformat()}


def admin_suspend_user(username: str, suspend: bool = True) -> dict:
    data  = _load()
    uname = username.lower()
    user  = data["users"].get(uname)
    if not user:
        return {"ok": False, "error": "User not found"}
    user["suspended"] = suspend
    _save(data)
    return {"ok": True}


def admin_set_role(username: str, role: str) -> dict:
    data  = _load()
    uname = username.lower()
    user  = data["users"].get(uname)
    if not user:
        return {"ok": False, "error": "User not found"}
    user["role"] = role
    _save(data)
    return {"ok": True}

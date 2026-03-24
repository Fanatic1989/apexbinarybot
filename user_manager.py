"""
User-facing routes — add these to server.py
Handles registration, login, user dashboard, bot control and payments.
"""
from flask import (Blueprint, render_template, request, jsonify,
                   redirect, url_for, session)
import logging
import json

import user_manager as um
import bot_manager  as bm
import payments

log = logging.getLogger(__name__)
user_bp = Blueprint("user", __name__, url_prefix="/user")


def user_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_logged_in"):
            return redirect(url_for("user.login"))
        return f(*args, **kwargs)
    return decorated


# ── Auth ──────────────────────────────────────────────────────

@user_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm",  "").strip()
        email    = request.form.get("email",    "").strip()

        if password != confirm:
            return render_template("register.html",
                                   error="Passwords do not match")

        result = um.register_user(username, password, email)
        if result["ok"]:
            session["user_logged_in"] = True
            session["username"]       = username
            log.info(f"[USER] Registered and logged in: {username}")
            return redirect(url_for("user.dashboard"))
        else:
            return render_template("register.html", error=result["error"])

    return render_template("register.html")


@user_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user     = um.authenticate(username, password)
        if user:
            session["user_logged_in"] = True
            session["username"]       = user["username"]
            log.info(f"[USER] Login: {username}")
            return redirect(url_for("user.dashboard"))
        else:
            return render_template("user_login.html",
                                   error="Invalid username or password")

    return render_template("user_login.html")


@user_bp.route("/logout")
def logout():
    username = session.get("username")
    session.clear()
    log.info(f"[USER] Logout: {username}")
    return redirect(url_for("user.login"))


# ── Dashboard ─────────────────────────────────────────────────

@user_bp.route("/")
@user_login_required
def dashboard():
    return render_template("user_dashboard.html")


# ── Status ────────────────────────────────────────────────────

@user_bp.route("/status")
@user_login_required
def status():
    username = session["username"]
    user     = um.get_user(username)
    sub      = um.get_subscription_status(username)
    state    = bm.get_user_state(username)

    # News events
    news = []
    try:
        from server import _get_upcoming_news
        news = _get_upcoming_news()
    except: pass

    risk = None
    if state.get("risk"):
        risk = state["risk"]
    elif user:
        risk = {
            "balance":       "—",
            "total_trades":  user.get("total_trades", 0),
            "wins":          user.get("total_wins",   0),
            "losses":        user.get("total_losses", 0),
            "win_rate":      round(user["total_wins"] / user["total_trades"] * 100, 1)
                             if user.get("total_trades", 0) > 0 else 0,
            "net_pnl":       user.get("net_pnl", 0),
            "consec_losses": 0,
            "daily_profit":  0,
            "daily_loss":    0,
        }

    return jsonify({
        "username":     username,
        "bot_running":  bm.is_running(username),
        "mode":         user.get("mode", "demo") if user else "demo",
        "risk_pct":     user.get("risk_pct", 1)  if user else 1,
        "risk":         risk,
        "subscription": sub,
        "last_signals": bm.get_last_signals(username),
        "news_events":  news,
    })


# ── Bot controls ──────────────────────────────────────────────

@user_bp.route("/start", methods=["POST"])
@user_login_required
def start_bot():
    username = session["username"]

    if not um.is_allowed_to_trade(username):
        return jsonify({"ok": False,
                        "error": "Subscription expired. Please renew."})

    user = um.get_user(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found"})

    mode  = user.get("mode", "demo")
    token = user.get("demo_token") if mode == "demo" \
            else user.get("live_token")

    if not token:
        return jsonify({"ok": False,
                        "error": f"No {mode} API token set. Add your token first."})

    result = bm.start_user_bot(username, user)
    return jsonify(result)


@user_bp.route("/stop", methods=["POST"])
@user_login_required
def stop_bot():
    username = session["username"]
    result   = bm.stop_user_bot(username)
    return jsonify(result)


@user_bp.route("/set-mode/<mode>")
@user_login_required
def set_mode(mode):
    if mode not in ("demo", "live"):
        return jsonify({"error": "Invalid mode"}), 400
    username = session["username"]
    bm.stop_user_bot(username)
    um.update_user_settings(username, mode=mode)
    return jsonify({"ok": True, "mode": mode})


@user_bp.route("/set-risk/<int:pct>")
@user_login_required
def set_risk(pct):
    if pct not in (1, 2, 3):
        return jsonify({"error": "Risk must be 1, 2 or 3"}), 400
    username = session["username"]
    um.update_user_settings(username, risk_pct=pct)
    return jsonify({"ok": True, "risk_pct": pct})


@user_bp.route("/save-tokens", methods=["POST"])
@user_login_required
def save_tokens():
    username   = session["username"]
    data       = request.get_json() or {}
    demo_token = data.get("demo_token", "").strip()
    live_token = data.get("live_token", "").strip()

    updates = {}
    if demo_token: updates["demo_token"] = demo_token
    if live_token: updates["live_token"] = live_token

    if not updates:
        return jsonify({"ok": False, "error": "No tokens provided"})

    result = um.update_user_settings(username, **updates)
    log.info(f"[USER] {username} updated tokens")
    return jsonify(result)


@user_bp.route("/change-password", methods=["POST"])
@user_login_required
def change_password():
    username = session["username"]
    data     = request.get_json() or {}
    current  = data.get("current", "").strip()
    new_pw   = data.get("new_password", "").strip()
    result   = um.change_password(username, current, new_pw)
    if result["ok"]:
        log.info(f"[USER] {username} changed password")
    return jsonify(result)



@user_bp.route("/close-all", methods=["POST"])
@user_login_required
def close_all():
    """Emergency close all open positions for this user."""
    username = session["username"]
    try:
        import scalp_bot as sb
        result = sb.close_all()
        log.info(f"[USER] {username} closed all positions")
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Trades ────────────────────────────────────────────────────

@user_bp.route("/trades")
@user_login_required
def trades():
    username = session["username"]
    t        = bm.get_user_trades(username)
    return jsonify({"trades": t})


# ── Payments ──────────────────────────────────────────────────

@user_bp.route("/create-payment", methods=["POST"])
@user_login_required
def create_payment():
    username    = session["username"]
    base_url    = request.host_url.rstrip("/")
    success_url = base_url + "/user/?payment=success"
    cancel_url  = base_url + "/user/?payment=cancelled"

    result = payments.create_payment(username, success_url, cancel_url)
    return jsonify(result)


# ── NOWPayments webhook (no auth required) ────────────────────



# ── AJAX endpoints for SPA (index.html) ──────────────────────

@user_bp.route("/login-ajax", methods=["POST"])
def login_ajax():
    data     = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    user     = um.authenticate(username, password)
    if user:
        session["user_logged_in"] = True
        session["username"]       = user["username"]
        return jsonify({"ok": True, "username": user["username"]})
    return jsonify({"ok": False, "error": "Invalid username or password"})


@user_bp.route("/register-ajax", methods=["POST"])
def register_ajax():
    data     = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    email    = data.get("email",    "").strip()
    result   = um.register_user(username, password, email)
    if result["ok"]:
        session["user_logged_in"] = True
        session["username"]       = username
        return jsonify({"ok": True, "username": username})
    return jsonify(result)


@user_bp.route("/logout-ajax", methods=["POST"])
def logout_ajax():
    username = session.get("username")
    if username:
        bm.stop_user_bot(username)
    session.clear()
    return jsonify({"ok": True})


@user_bp.route("/whoami")
def whoami():
    if session.get("user_logged_in"):
        return jsonify({"username": session.get("username")})
    return jsonify({"username": None})

def register_webhook(app):
    @app.route("/webhook/nowpayments", methods=["POST"])
    def nowpayments_webhook():
        payload_bytes = request.get_data()
        sig           = request.headers.get("x-nowpayments-sig", "")

        if not payments.verify_webhook(payload_bytes, sig):
            log.warning("[WEBHOOK] Invalid signature")
            return jsonify({"error": "Invalid signature"}), 400

        try:
            payload = request.get_json(force=True)
        except:
            return jsonify({"error": "Bad JSON"}), 400

        result = payments.handle_webhook(payload)
        return jsonify(result)

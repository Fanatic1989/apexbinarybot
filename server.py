import os
import json
import threading
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, jsonify, request, render_template, redirect, url_for, session

import config
import scalp_bot as bot
from user_routes import user_bp, register_webhook
import user_manager as um
import scalp_bot as bm

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Flask app (FIXED TEMPLATE PATH)
# ─────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", config.ADMIN_PASSWORD or "apex-secret-key-change-me")

app.register_blueprint(user_bp)
register_webhook(app)

# ─────────────────────────────────────────
# Bot thread state
# ─────────────────────────────────────────
bot_thread    = None
bot_running   = False
bot_stop_flag = threading.Event()

TRADE_HISTORY_FILE = "trade_history.json"

# ─────────────────────────────────────────
# Login required decorator
# ─────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────
# Route: Login
# ─────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
            session.clear()
            session["logged_in"] = True
            session["username"]  = username
            log.info(f"[SERVER] Login successful for '{username}'")
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid username or password"
            log.warning(f"[SERVER] Failed login attempt for '{username}'")

    return render_template("login.html", error=error)


# ─────────────────────────────────────────
# Route: Logout
# ─────────────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────
# 🔥 FIXED DASHBOARD ROUTE
# ─────────────────────────────────────────
@app.route("/")
def admin_dashboard():
    if session.get("logged_in"):
        try:
            return render_template("dashboard.html")
        except Exception as e:
            log.error(f"[SERVER] Dashboard load failed: {e}")
            return "<h1>Dashboard missing</h1><p>Check templates folder</p>"
    return redirect(url_for("login"))


# ─────────────────────────────────────────
# Route: Status
# ─────────────────────────────────────────
@app.route("/status")
@login_required
def status():
    scalp_status = bot.get_status() if hasattr(bot, "get_status") else {}
    risk_summary = scalp_status.get("risk")

    return jsonify({
        "bot_running":     bot_running,
        "mode":            config.MODE,
        "markets":         len(config.MARKETS),
        "active_markets":  len(config.get_active_markets()),
        "interval":        config.SCAN_INTERVAL,
        "session":         config.get_current_session(),
        "risk":            risk_summary,
        "risk_pct":        int(config.STAKE_PERCENT),
        "positions":       scalp_status.get("positions", []),
        "open_positions":  scalp_status.get("open_positions", 0),
        "compound":        scalp_status.get("compound", {}),
    })


# ─────────────────────────────────────────
# Start Bot
# ─────────────────────────────────────────
@app.route("/start")
@login_required
def start_bot():
    global bot_thread, bot_running, bot_stop_flag

    if bot_running and bot_thread and bot_thread.is_alive():
        return jsonify({"status": "bot already running"})

    bot_stop_flag.clear()
    bot_running = True

    bot_thread = threading.Thread(target=_run_bot_safe, daemon=True)
    bot_thread.start()

    return jsonify({"status": "bot started"})


# ─────────────────────────────────────────
# Stop Bot
# ─────────────────────────────────────────
@app.route("/stop")
@login_required
def stop_bot():
    global bot_running
    bot_running = False
    bot_stop_flag.set()
    return jsonify({"status": "bot stopped"})


# ─────────────────────────────────────────
# Health + Ping (for UptimeRobot)
# ─────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({
        "status": "ok",
        "bot_running": bot_running,
        "timestamp": datetime.utcnow().isoformat()
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat()
    })


# ─────────────────────────────────────────
# Bot runner
# ─────────────────────────────────────────
def _run_bot_safe():
    global bot_running
    try:
        bot.run_bot()
    except Exception as e:
        log.error(f"[SERVER] Bot crashed: {e}", exc_info=True)
    finally:
        bot_running = False


# ─────────────────────────────────────────
# Self-ping (keeps Render awake)
# ─────────────────────────────────────────
def _self_ping_loop():
    import requests, time

    time.sleep(60)
    app_url = os.getenv("APP_URL", "").rstrip("/")

    if not app_url:
        log.info("[PING] No APP_URL set")
        return

    while True:
        try:
            requests.get(f"{app_url}/ping", timeout=10)
            log.debug("[PING] OK")
        except Exception as e:
            log.debug(f"[PING] Failed: {e}")

        time.sleep(120)


threading.Thread(target=_self_ping_loop, daemon=True).start()


# ─────────────────────────────────────────
# Run
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)

# Gunicorn
application = app

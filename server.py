import os
import json
import threading
import logging
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request, render_template, redirect, url_for, session

import config
import bot

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
# Flask app
# ─────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", config.ADMIN_PASSWORD or "apex-secret-key-change-me")

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
            session["logged_in"] = True
            session["username"]  = username
            log.info(f"[SERVER] Login successful for '{username}'")
            return redirect(url_for("dashboard"))
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
# Route: Dashboard
# ─────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ─────────────────────────────────────────
# Route: Status
# ─────────────────────────────────────────
@app.route("/status")
@login_required
def status():
    risk_summary = None
    if hasattr(bot, "risk_manager") and bot.risk_manager:
        risk_summary = bot.risk_manager.get_summary()
    return jsonify({
        "bot_running":     bot_running,
        "mode":            config.MODE,
        "markets":         len(config.MARKETS),
        "active_markets":  len(config.get_active_markets()),
        "interval":        config.SCAN_INTERVAL,
        "session":         config.get_current_session(),
        "risk":            risk_summary,
        "last_signals":    getattr(bot, "last_signals", [])
    })


# ─────────────────────────────────────────
# Route: Start bot
# ─────────────────────────────────────────
@app.route("/start")
@login_required
def start_bot():
    global bot_thread, bot_running, bot_stop_flag

    if bot_running and bot_thread and bot_thread.is_alive():
        return jsonify({"status": "bot already running"})

    bot_stop_flag.clear()
    bot_running = True

    bot_thread = threading.Thread(target=_run_bot_safe, daemon=True, name="BotThread")
    bot_thread.start()

    log.info("[SERVER] Bot thread started.")
    return jsonify({"status": "bot started"})


# ─────────────────────────────────────────
# Route: Stop bot
# ─────────────────────────────────────────
@app.route("/stop")
@login_required
def stop_bot():
    global bot_running
    bot_running = False
    bot_stop_flag.set()
    log.info("[SERVER] Bot stop requested.")
    return jsonify({"status": "bot stopped"})


# ─────────────────────────────────────────
# Route: Switch mode
# ─────────────────────────────────────────
@app.route("/mode/<mode>")
@login_required
def change_mode(mode):
    if mode not in ("demo", "live"):
        return jsonify({"error": "Mode must be 'demo' or 'live'"}), 400

    global bot_running
    bot_running = False
    bot_stop_flag.set()

    config.MODE         = mode
    config.ACTIVE_TOKEN = config.get_active_token()

    log.info(f"[SERVER] Mode switched to {mode.upper()}")
    return jsonify({
        "mode":   config.MODE,
        "status": "bot stopped for mode switch — restart manually"
    })


# ─────────────────────────────────────────
# Route: Trade history
# ─────────────────────────────────────────
@app.route("/trades")
@login_required
def trades():
    try:
        with open(TRADE_HISTORY_FILE) as f:
            data = json.load(f)
        return jsonify(data)
    except FileNotFoundError:
        return jsonify({"trades": []})
    except Exception as e:
        log.error(f"[SERVER] Error reading trade history: {e}")
        return jsonify({"trades": [], "error": str(e)})


# ─────────────────────────────────────────
# Route: Log lines
# ─────────────────────────────────────────
_log_lines = []

class _LogHandler(logging.Handler):
    def emit(self, record):
        _log_lines.append(self.format(record))
        if len(_log_lines) > 200:
            _log_lines.pop(0)

_handler = _LogHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_handler)

@app.route("/log")
@login_required
def get_log():
    return jsonify({"lines": list(reversed(_log_lines[-50:]))})


# ─────────────────────────────────────────
# Route: Health check — NO login required
# Render pings this to keep container alive
# ─────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})



# ─────────────────────────────────────────
# Route: Connection test (debug)
# ─────────────────────────────────────────
@app.route("/test-connection")
@login_required
def test_connection():
    """Test Deriv API connection and show exactly what is failing."""
    import websocket, json
    results = {
        "app_id":     config.DERIV_APP_ID,
        "mode":       config.MODE,
        "token_set":  bool(config.ACTIVE_TOKEN),
        "token_preview": config.ACTIVE_TOKEN[:6] + "..." if config.ACTIVE_TOKEN else "NOT SET",
        "ws_url":     f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
        "auth_result": None,
        "balance":    None,
        "error":      None
    }
    try:
        ws = websocket.create_connection(results["ws_url"], timeout=10)
        ws.send(json.dumps({"authorize": config.ACTIVE_TOKEN}))
        resp = json.loads(ws.recv())
        if "error" in resp:
            results["auth_result"] = "FAILED"
            results["error"] = resp["error"]["message"]
        else:
            results["auth_result"] = "SUCCESS"
            results["balance"] = resp.get("authorize", {}).get("balance")
        ws.close()
    except Exception as e:
        results["auth_result"] = "EXCEPTION"
        results["error"] = str(e)
    return jsonify(results)

# ─────────────────────────────────────────
# Bot runner wrapper
# ─────────────────────────────────────────
def _run_bot_safe():
    global bot_running
    try:
        bot.run_bot()
    except Exception as e:
        log.error(f"[SERVER] Bot thread crashed: {e}", exc_info=True)
    finally:
        bot_running = False
        log.info("[SERVER] Bot thread exited.")


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("port") or 10000)
    log.info(f"[SERVER] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

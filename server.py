import os
import json
import threading
import logging
import sys
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, jsonify, request, render_template, redirect, url_for, session

import config

# ✅ SAFE IMPORT (CRITICAL FIX)
try:
    import scalp_bot as bot
except Exception as e:
    print(f"[CRITICAL] Failed to import scalp_bot: {e}")
    bot = None

from user_routes import user_bp, register_webhook
import user_manager as um

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Flask app
# ─────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this")
app.register_blueprint(user_bp)
register_webhook(app)

bot_thread = None
bot_running = False
bot_stop_flag = threading.Event()

# ─────────────────────────────────────────
# SAFE BOT RUNNER (CRASH PROOF)
# ─────────────────────────────────────────
def _run_bot_safe():
    global bot_running
    try:
        if bot:
            bot.run_bot()
        else:
            log.error("[SERVER] Bot module not available")
    except Exception as e:
        log.error(f"[SERVER] Bot crashed: {e}", exc_info=True)
    finally:
        bot_running = False
        log.info("[SERVER] Bot thread exited (server still alive)")

# ─────────────────────────────────────────
# BASIC ROUTES
# ─────────────────────────────────────────
@app.route("/")
def home():
    return "Bot is running"

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_running": bot_running,
        "time": datetime.utcnow().isoformat()
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "alive"})

# ─────────────────────────────────────────
# START / STOP BOT
# ─────────────────────────────────────────
@app.route("/start")
def start():
    global bot_thread, bot_running

    if bot_running:
        return jsonify({"status": "already running"})

    bot_running = True
    bot_thread = threading.Thread(target=_run_bot_safe, daemon=True)
    bot_thread.start()

    return jsonify({"status": "started"})

@app.route("/stop")
def stop():
    global bot_running
    bot_running = False
    return jsonify({"status": "stopped"})

# ─────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────
@app.route("/status")
def status():
    if bot and hasattr(bot, "get_status"):
        return jsonify(bot.get_status())
    return jsonify({"status": "bot unavailable"})

# ─────────────────────────────────────────
# SELF PING (KEEP RENDER AWAKE)
# ─────────────────────────────────────────
def _self_ping_loop():
    import requests, time
    time.sleep(60)

    url = os.getenv("APP_URL", "").rstrip("/")
    if not url:
        log.info("[PING] No APP_URL set")
        return

    while True:
        try:
            requests.get(f"{url}/health", timeout=10)
            log.debug("[PING] success")
        except Exception as e:
            log.debug(f"[PING] failed: {e}")
        time.sleep(240)

threading.Thread(target=_self_ping_loop, daemon=True).start()

# ─────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Gunicorn
application = app

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
# Flask app
# ─────────────────────────────────────────
app = Flask(__name__)
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
# Forex market hours helper
# ─────────────────────────────────────────
def is_forex_hours() -> bool:
    """
    Returns True if current UTC time is within forex trading hours.
    Forex is open from Sunday 22:00 UTC to Friday 22:00 UTC.
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday=0, Sunday=6
    # Sunday after 22:00 UTC (weekday=6, hour>=22) -> open
    if weekday == 6 and now.hour >= 22:
        return True
    # Monday to Thursday all day
    if 0 <= weekday <= 3:
        return True
    # Friday until 22:00 UTC
    if weekday == 4 and now.hour < 22:
        return True
    return False

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
            session.clear()                      # wipe any user session first
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
# Route: Dashboard
# ─────────────────────────────────────────
@app.route("/")
def admin_dashboard():
    if session.get("logged_in"):
        return render_template("dashboard.html")
    return render_template("index.html")


# ─────────────────────────────────────────
# Route: Status
# ─────────────────────────────────────────
@app.route("/status")
@login_required
def status():
    # Get scalp bot status
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
        "news_events":     _get_upcoming_news(),
        "positions":       scalp_status.get("positions", []),
        "open_positions":  scalp_status.get("open_positions", 0),
        "compound":        scalp_status.get("compound", {}),
        "config": {
            "daily_profit_target": getattr(config, "DAILY_PROFIT_TARGET", 8.0),
            "max_daily_loss_pct":  getattr(config, "MAX_DAILY_LOSS_PCT", 5.0),
            "stake_percent":       config.STAKE_PERCENT,
        }
    })


def _get_upcoming_news():
    """
    Normalise news events for the dashboard buildNewsPanel() function.
    Looks ahead 72h on weekends so Monday events always show.
    """
    try:
        from news_filter import news_filter

        now     = datetime.now(timezone.utc)
        weekday = now.weekday()  # 5=Sat, 6=Sun
        hours   = 72 if weekday >= 5 else 8

        raw = news_filter.get_upcoming_events(hours=hours)
        out = []
        for e in raw:
            mins = e.get("mins_away", 0)
            eta  = now + timedelta(minutes=mins)
            out.append({
                "title":     e.get("title") or e.get("event") or "—",
                "currency":  e.get("currency", ""),
                "impact":    (e.get("impact") or "").lower(),
                "time_utc":  eta.strftime("%H:%M UTC"),
                "date_utc":  eta.strftime("%a %d %b"),
                "mins_away": mins,
                "source":    e.get("source", ""),
            })

        out.sort(key=lambda x: x["mins_away"])
        return out[:10]

    except Exception as ex:
        log.debug(f"[SERVER] _get_upcoming_news error: {ex}")
        return []


# ─────────────────────────────────────────
# Route: Debug news
# ─────────────────────────────────────────
@app.route("/debug-news")
@login_required
def debug_news():
    import requests as _req
    results = {}

    FF_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/json",
        "Referer":    "https://www.forexfactory.com/",
    }

    for key, url in [
        ("ff_thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"),
        ("ff_nextweek", "https://nfs.faireconomy.media/ff_calendar_nextweek.json"),
    ]:
        try:
            r = _req.get(url, headers=FF_HEADERS, timeout=10)
            data = r.json() if r.status_code == 200 else []
            results[key] = {
                "status":  r.status_code,
                "count":   len(data) if isinstance(data, list) else "not a list",
                "sample":  data[0] if isinstance(data, list) and data else None,
            }
        except Exception as e:
            results[key] = {"error": str(e)}

    try:
        results["filter_dynamic_count"] = len(news_filter._dynamic_events)
        results["filter_last_update"]   = str(news_filter._last_update)
        results["source_summary"]       = news_filter.get_source_summary()
        results["upcoming_4h"]          = news_filter.get_upcoming_events(hours=4)
        results["upcoming_72h"]         = news_filter.get_upcoming_events(hours=72)
        results["raw_sample"]           = [
            {k: str(v) for k, v in e.items()}
            for e in news_filter._dynamic_events[:3]
        ]
    except Exception as e:
        results["filter_error"] = str(e)

    results["server_utc"] = datetime.now(timezone.utc).isoformat()
    results["weekday"]    = datetime.now(timezone.utc).weekday()
    return jsonify(results)


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
# Route: Set risk percentage
# ─────────────────────────────────────────
@app.route("/set-risk/<int:pct>")
@login_required
def set_risk(pct):
    if pct not in (1, 2, 3):
        return jsonify({"error": "Risk must be 1, 2 or 3"}), 400

    config.STAKE_PERCENT = float(pct)

    balance = 0.0
    if hasattr(bot, "risk_manager") and bot.risk_manager:
        balance = float(bot.risk_manager.current_balance or 0)

    if balance <= 0:
        try:
            from deriv_api import get_balance
            balance = get_balance()
        except:
            pass

    new_stake = round(max(balance * (pct/100), 0.35), 2) if balance > 0 else 0.35

    if hasattr(bot, "staking_engine") and bot.staking_engine:
        bot.staking_engine.base_stake    = new_stake
        bot.staking_engine.current_stake = new_stake
        if balance > 0:
            bot.staking_engine.balance = balance

    log.info(f"[SERVER] Risk set to {pct}% | Balance: ${balance:.2f} | "
             f"Stake: ${new_stake:.2f} per trade")

    return jsonify({
        "risk_pct": pct,
        "stake":    new_stake,
        "balance":  round(balance, 2)
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
# Route: Test forex symbols
# ─────────────────────────────────────────
@app.route("/test-forex")
@login_required
def test_forex():
    import websocket, json
    results = {}
    symbols = [
        "frxEURUSD","frxGBPUSD","frxUSDJPY",
        "frxGBPJPY","frxEURGBP","frxAUDUSD",
        "frxEURJPY","frxUSDCAD","frxUSDCHF",
    ]
    ws = None
    try:
        ws = websocket.create_connection(
            f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
            timeout=15
        )
        ws.send(json.dumps({"authorize": config.ACTIVE_TOKEN}))
        auth = json.loads(ws.recv())
        if "error" in auth:
            return jsonify({"error": auth["error"]["message"]})

        for sym in symbols:
            try:
                ws.send(json.dumps({
                    "ticks_history": sym,
                    "adjust_start_time": 1,
                    "count": 3,
                    "end": "latest",
                    "style": "candles",
                    "granularity": 60
                }))
                r = json.loads(ws.recv())
                if "candles" in r:
                    results[sym] = "✓ WORKS"
                elif "error" in r:
                    results[sym] = f"✗ {r['error']['message']}"
                else:
                    results[sym] = "✗ No data"
            except Exception as e:
                results[sym] = f"✗ Exception: {e}"
    except Exception as e:
        return jsonify({"error": str(e), "results": results})
    finally:
        if ws:
            try: ws.close()
            except: pass

    return jsonify({"results": results})


# ─────────────────────────────────────────
# Route: Test valid durations for forex
# ─────────────────────────────────────────
@app.route("/test-durations")
@login_required
def test_durations():
    import websocket as _ws, json as _json
    symbol = request.args.get("symbol", "frxEURUSD")
    durations = [
        (1,"m"),(2,"m"),(3,"m"),(5,"m"),(10,"m"),(15,"m"),(30,"m"),
        (60,"m"),(1,"h"),(1,"d"),
        (15,"s"),(30,"s"),(60,"s"),(90,"s"),(120,"s"),(300,"s"),
    ]
    results = {}
    ws = None
    try:
        ws = _ws.create_connection(
            f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
            timeout=15
        )
        ws.send(_json.dumps({"authorize": config.ACTIVE_TOKEN}))
        auth = _json.loads(ws.recv())
        if "error" in auth:
            return jsonify({"error": auth["error"]["message"]})
        for dur, unit in durations:
            try:
                ws.send(_json.dumps({
                    "proposal": 1, "amount": 1, "basis": "stake",
                    "contract_type": "CALL", "currency": "USD",
                    "duration": dur, "duration_unit": unit,
                    "symbol": symbol
                }))
                r = _json.loads(ws.recv())
                key = f"{dur}{unit}"
                if "proposal" in r:
                    results[key] = f"✓ payout ${r['proposal']['payout']:.2f}"
                else:
                    results[key] = f"✗ {r.get('error',{}).get('message','No data')}"
            except Exception as e:
                results[f"{dur}{unit}"] = f"✗ {e}"
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        if ws:
            try: ws.close()
            except: pass
    working = {k:v for k,v in results.items() if v.startswith("✓")}
    return jsonify({"symbol": symbol, "working": working, "all": results})



# ─────────────────────────────────────────
# Route: Admin panel
# ─────────────────────────────────────────
@app.route("/admin")
@login_required
def admin_panel():
    return render_template("admin_panel.html")

@app.route("/admin/users")
@login_required
def admin_users():
    return jsonify({"users": um.get_all_users()})

@app.route("/admin/extend-subscription", methods=["POST"])
@login_required
def admin_extend():
    data     = request.get_json() or {}
    username = data.get("username", "")
    days     = int(data.get("days", 30))
    result   = um.admin_extend_subscription(username, days)
    return jsonify(result)

@app.route("/admin/suspend-user", methods=["POST"])
@login_required
def admin_suspend():
    data     = request.get_json() or {}
    username = data.get("username", "")
    suspend  = bool(data.get("suspend", True))
    result   = um.admin_suspend_user(username, suspend)
    return jsonify(result)


@app.route("/admin/payments")
@login_required
def admin_payments():
    try:
        import payments as pay
        history = pay.get_payment_history()
        summary = pay.get_revenue_summary()
        return jsonify({"payments": history, "summary": summary})
    except Exception as e:
        return jsonify({"payments": [], "summary": {}, "error": str(e)})


@app.route("/admin/add-free-member", methods=["POST"])
@login_required
def admin_add_free_member():
    data     = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    days     = int(data.get("days", 30))

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required"})

    # Register the user
    result = um.register_user(username, password, email="")
    if not result["ok"]:
        return jsonify(result)

    # Mark as free/influencer account
    um.update_user_settings(username, account_type="free")

    # Give them free access for specified days
    sub_result = um.admin_extend_subscription(username, days=days)

    log.info(f"[ADMIN] Free member added: {username} ({days} days)")
    return jsonify({
        "ok":      True,
        "username": username,
        "days":    days,
        "ends":    sub_result.get("new_end", ""),
    })


@app.route("/admin/delete-user", methods=["POST"])
@login_required
def admin_delete_user():
    data     = request.get_json() or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username required"})
    # Stop their bot first
    try:
        bm.stop_user_bot(username)
    except: pass
    result = um.delete_user(username)
    if result["ok"]:
        log.info(f"[ADMIN] Deleted user: {username}")
    return jsonify(result)


# ─────────────────────────────────────────
# Route: Set compounding
# ─────────────────────────────────────────
@app.route("/set-compound", methods=["POST"])
@login_required
def set_compound():
    data    = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    pct     = int(data.get("pct", 50))
    try:
        bot.set_compound(enabled, pct)
        return jsonify({"ok": True, "enabled": enabled, "pct": pct})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ─────────────────────────────────────────
# Route: Health check — NO login required
# ─────────────────────────────────────────

@app.route("/ping")
def ping():
    """Lightweight keep-alive endpoint for UptimeRobot and self-ping."""
    scalp_status = {}
    try:
        scalp_status = bot.get_status() if hasattr(bot, "get_status") else {}
    except: pass
    return jsonify({
        "status": "ok",
        "bot_running": bot_running,
        "open_positions": scalp_status.get("open_positions", 0),
        "timestamp": __import__("datetime").datetime.utcnow().isoformat()
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


# ─────────────────────────────────────────
# Route: Connection test (debug)
# ─────────────────────────────────────────
@app.route("/test-connection")
@login_required
def test_connection():
    results = {
        "app_id":        config.DERIV_APP_ID,
        "mode":          config.MODE,
        "token_set":     bool(config.ACTIVE_TOKEN),
        "token_preview": config.ACTIVE_TOKEN[:6] + "..." if config.ACTIVE_TOKEN else "NOT SET",
        "ws_url":        f"wss://ws.derivws.com/websockets/v3?app_id={config.DERIV_APP_ID}",
        "auth_result":   None,
        "balance":       None,
        "error":         None
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
# Watchdog — auto restart bot if it crashes
# ─────────────────────────────────────────
def _watchdog():
    import time as _time
    _time.sleep(60)
    while True:
        global bot_thread, bot_running
        if bot_running and (bot_thread is None or not bot_thread.is_alive()):
            log.warning("[WATCHDOG] Bot thread died — auto restarting...")
            bot_thread = threading.Thread(
                target=_run_bot_safe, daemon=True, name="BotThread"
            )
            bot_thread.start()
            log.info("[WATCHDOG] Bot restarted.")
        _time.sleep(30)

_watchdog_thread = threading.Thread(target=_watchdog, daemon=True, name="Watchdog")
_watchdog_thread.start()


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
# Self-ping — keeps Render free tier alive
# Improved: Sends heartbeat to UptimeRobot only when bot is running
# ─────────────────────────────────────────
def _self_ping_loop():
    import requests, time
    time.sleep(60)  # Wait 1 min after startup

    app_url = os.getenv("APP_URL", "").rstrip("/")
    uptime_url = os.getenv("UPTIMEROBOT_HEARTBEAT", "")

    if not app_url:
        log.info("[PING] APP_URL not set — self-ping disabled")
        return

    log.info(f"[PING] Self-ping active → {app_url}/ping every 2min")
    if uptime_url:
        log.info("[PING] UptimeRobot heartbeat configured (will only fire when bot is running)")

    while True:
        # 1. Self-ping to keep Render alive
        try:
            r = requests.get(f"{app_url}/ping", timeout=10)
            if r.status_code != 200:
                log.debug(f"[PING] Self-ping returned {r.status_code}")
            else:
                log.debug("[PING] Self-ping OK")
        except Exception as e:
            log.debug(f"[PING] Self-ping failed: {e}")

        # 2. UptimeRobot heartbeat – only if bot is actually running
        if uptime_url and bot_running:
            try:
                # Try up to 2 times
                for attempt in range(2):
                    r = requests.get(uptime_url, timeout=10)
                    if r.status_code in (200, 204):
                        log.debug("[PING] UptimeRobot heartbeat sent")
                        break
                    else:
                        log.debug(f"[PING] UptimeRobot heartbeat attempt {attempt+1} got {r.status_code}")
                        time.sleep(2)
                else:
                    log.warning("[PING] UptimeRobot heartbeat failed after retries")
            except Exception as e:
                log.debug(f"[PING] UptimeRobot heartbeat error: {e}")

        time.sleep(120)  # Every 2 minutes

_ping_thread = threading.Thread(target=_self_ping_loop, daemon=True, name="SelfPing")
_ping_thread.start()


# ─────────────────────────────────────────
# Auto-start/stop based on forex market hours
# ─────────────────────────────────────────
def _auto_manage_bot():
    """Periodically check if bot should be running based on forex hours."""
    import time
    time.sleep(30)  # initial delay

    while True:
        try:
            should_run = is_forex_hours()
            if should_run and not bot_running:
                log.info("[AUTO-MANAGE] Forex hours detected — starting bot...")
                # Start bot via the same mechanism as /start
                with app.test_request_context():
                    start_bot()
            elif not should_run and bot_running:
                log.info("[AUTO-MANAGE] Outside forex hours — stopping bot...")
                with app.test_request_context():
                    stop_bot()
        except Exception as e:
            log.error(f"[AUTO-MANAGE] Error: {e}")

        # Check every 5 minutes
        time.sleep(300)

# Start the auto-manage thread
_auto_manager = threading.Thread(target=_auto_manage_bot, daemon=True, name="AutoManager")
_auto_manager.start()


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("port") or 10000)
    log.info(f"[SERVER] Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)

# Gunicorn entry point
application = app

import os
import json
import threading
import logging
from datetime import datetime, timezone, timedelta
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
    staking_info = None
    if hasattr(bot, "staking_engine") and bot.staking_engine:
        staking_info = bot.staking_engine.get_info()

    ai_info = None
    try:
        from strategy_ai import tracker
        ai_info = tracker.get_summary()
    except: pass

    return jsonify({
        "bot_running":     bot_running,
        "mode":            config.MODE,
        "markets":         len(config.MARKETS),
        "active_markets":  len(config.get_active_markets()),
        "interval":        config.SCAN_INTERVAL,
        "session":         config.get_current_session(),
        "risk":            risk_summary,
        "staking":         staking_info,
        "last_signals":    getattr(bot, "last_signals", []),
        "ai_strategy":     ai_info,
        "risk_pct":        int(config.STAKE_PERCENT),
        "news_events":     _get_upcoming_news()
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
        from news_filter import news_filter
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
# Route: Health check — NO login required
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
    import websocket, json
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
# Entry point
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("port") or 10000)
    log.info(f"[SERVER] Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)

# Gunicorn entry point
application = app

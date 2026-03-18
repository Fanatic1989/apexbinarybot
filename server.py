import os
import json
import threading
import logging
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string

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

# ─────────────────────────────────────────
# Bot thread state
# ─────────────────────────────────────────
bot_thread    = None
bot_running   = False
bot_stop_flag = threading.Event()

TRADE_HISTORY_FILE = "trade_history.json"

# ─────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Apex Binary Bot</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #0d0d0d; color: #e0e0e0; padding: 24px; }
    h1 { color: #00e676; font-size: 22px; margin-bottom: 4px; }
    .subtitle { color: #888; font-size: 13px; margin-bottom: 24px; }
    .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px; padding: 16px 20px; margin-bottom: 16px; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .metric { background: #111; border-radius: 8px; padding: 12px; text-align: center; }
    .metric-val { font-size: 22px; font-weight: 600; color: #00e676; }
    .metric-lbl { font-size: 11px; color: #666; margin-top: 4px; }
    .btn { padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; margin-right: 8px; margin-bottom: 8px; }
    .btn-green  { background: #00e676; color: #000; }
    .btn-red    { background: #ff5252; color: #fff; }
    .btn-blue   { background: #2979ff; color: #fff; }
    .btn-orange { background: #ff9100; color: #000; }
    .btn-gray   { background: #333; color: #aaa; }
    .status-badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
    .status-running { background: #003320; color: #00e676; }
    .status-stopped { background: #330000; color: #ff5252; }
    .mode-badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; margin-left: 8px; }
    .mode-demo { background: #1a1a40; color: #7986cb; }
    .mode-live { background: #3a0000; color: #ff5252; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { text-align: left; padding: 8px 10px; color: #555; border-bottom: 1px solid #2a2a2a; font-weight: 500; }
    td { padding: 8px 10px; border-bottom: 1px solid #1f1f1f; }
    .win  { color: #00e676; }
    .loss { color: #ff5252; }
    .call { color: #00e676; }
    .put  { color: #ff5252; }
    #log-box { background: #111; border-radius: 6px; padding: 12px; font-family: monospace; font-size: 12px;
               height: 180px; overflow-y: auto; color: #aaa; white-space: pre-wrap; word-break: break-all; }
  </style>
</head>
<body>

<h1>⚡ APEX BINARY BOT</h1>
<div class="subtitle">
  Deriv Synthetic Indices — Automated Signal & Trading Engine
  <span class="status-badge" id="status-badge">...</span>
  <span class="mode-badge"   id="mode-badge">...</span>
</div>

<!-- Metrics -->
<div class="metrics">
  <div class="metric"><div class="metric-val" id="m-balance">—</div><div class="metric-lbl">Balance</div></div>
  <div class="metric"><div class="metric-val" id="m-trades">—</div><div class="metric-lbl">Total trades</div></div>
  <div class="metric"><div class="metric-val" id="m-winrate">—</div><div class="metric-lbl">Win rate</div></div>
  <div class="metric"><div class="metric-val" id="m-pnl">—</div><div class="metric-lbl">Net P&L</div></div>
  <div class="metric"><div class="metric-val" id="m-daily">—</div><div class="metric-lbl">Daily P&L</div></div>
  <div class="metric"><div class="metric-val" id="m-consec">—</div><div class="metric-lbl">Consec. losses</div></div>
</div>

<!-- Controls -->
<div class="card">
  <strong style="font-size:13px;color:#888;">BOT CONTROL</strong><br><br>
  <button class="btn btn-green"  onclick="startBot()">▶ START BOT</button>
  <button class="btn btn-red"    onclick="stopBot()">■ STOP BOT</button>
  <button class="btn btn-blue"   onclick="setMode('demo')">DEMO MODE</button>
  <button class="btn btn-orange" onclick="setMode('live')">LIVE MODE</button>
  <button class="btn btn-gray"   onclick="loadAll()">↻ REFRESH</button>
</div>

<!-- Trade History -->
<div class="card">
  <strong style="font-size:13px;color:#888;">TRADE HISTORY</strong>
  <table style="margin-top:12px">
    <thead>
      <tr>
        <th>Time</th><th>Market</th><th>Direction</th>
        <th>Stake</th><th>Payout</th><th>Result</th><th>P&L</th>
      </tr>
    </thead>
    <tbody id="trade-rows"><tr><td colspan="7" style="color:#555;text-align:center">No trades yet</td></tr></tbody>
  </table>
</div>

<!-- Activity log -->
<div class="card">
  <strong style="font-size:13px;color:#888;">ACTIVITY LOG</strong>
  <div id="log-box" style="margin-top:10px">Waiting for bot activity...</div>
</div>

<script>
function api(url, cb) {
  fetch(url).then(r => r.json()).then(cb).catch(e => console.error(url, e));
}

function startBot() {
  api('/start', d => showToast(d.status));
}
function stopBot() {
  api('/stop', d => showToast(d.status));
}
function setMode(m) {
  if (m === 'live' && !confirm('Switch to LIVE trading? Real money will be at risk.')) return;
  api('/mode/' + m, d => { showToast('Mode: ' + d.mode); loadStatus(); });
}

function loadStatus() {
  api('/status', d => {
    document.getElementById('status-badge').textContent = d.bot_running ? 'RUNNING' : 'STOPPED';
    document.getElementById('status-badge').className = 'status-badge ' + (d.bot_running ? 'status-running' : 'status-stopped');
    document.getElementById('mode-badge').textContent = d.mode.toUpperCase();
    document.getElementById('mode-badge').className = 'mode-badge mode-' + d.mode;
    if (d.risk) {
      document.getElementById('m-balance').textContent = '$' + d.risk.balance;
      document.getElementById('m-trades').textContent  = d.risk.total_trades;
      document.getElementById('m-winrate').textContent = d.risk.win_rate + '%';
      document.getElementById('m-pnl').textContent     = '$' + d.risk.net_pnl;
      document.getElementById('m-daily').textContent   = '$' + d.risk.daily_profit;
      document.getElementById('m-consec').textContent  = d.risk.consec_losses;
    }
  });
}

function loadTrades() {
  api('/trades', d => {
    const trades = (d.trades || []).slice().reverse().slice(0, 50);
    if (!trades.length) return;
    let html = '';
    trades.forEach(t => {
      const res = t.result === 'won'
        ? '<span class="win">✅ WIN</span>'
        : '<span class="loss">❌ LOSS</span>';
      const dir = t.direction === 'CALL'
        ? '<span class="call">▲ CALL</span>'
        : '<span class="put">▼ PUT</span>';
      const pnl = t.profit >= 0
        ? '<span class="win">+$' + t.profit + '</span>'
        : '<span class="loss">-$' + Math.abs(t.profit) + '</span>';
      html += `<tr>
        <td>${t.time || '—'}</td>
        <td>${t.symbol}</td>
        <td>${dir}</td>
        <td>$${t.stake}</td>
        <td>$${t.payout || '—'}</td>
        <td>${res}</td>
        <td>${pnl}</td>
      </tr>`;
    });
    document.getElementById('trade-rows').innerHTML = html;
  });
}

function loadLog() {
  api('/log', d => {
    const box = document.getElementById('log-box');
    box.textContent = (d.lines || []).join('\\n');
    box.scrollTop = box.scrollHeight;
  });
}

function loadAll() { loadStatus(); loadTrades(); loadLog(); }

function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;bottom:24px;right:24px;background:#00e676;color:#000;padding:10px 18px;border-radius:6px;font-size:13px;font-weight:600;z-index:999';
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2500);
}

loadAll();
setInterval(loadAll, 5000);
</script>
</body>
</html>
"""


# ─────────────────────────────────────────
# Route: Dashboard
# ─────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ─────────────────────────────────────────
# Route: Status
# ─────────────────────────────────────────
@app.route("/status")
def status():
    risk_summary = None
    if hasattr(bot, "risk_manager") and bot.risk_manager:
        risk_summary = bot.risk_manager.get_summary()
    return jsonify({
        "bot_running": bot_running,
        "mode":        config.MODE,
        "markets":     len(config.MARKETS),
        "interval":    config.SCAN_INTERVAL,
        "risk":        risk_summary
    })


# ─────────────────────────────────────────
# Route: Start bot
# ─────────────────────────────────────────
@app.route("/start")
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
def change_mode(mode):
    if mode not in ("demo", "live"):
        return jsonify({"error": "Mode must be 'demo' or 'live'"}), 400

    # Stop bot before switching mode
    global bot_running
    bot_running = False
    bot_stop_flag.set()

    config.MODE         = mode
    config.ACTIVE_TOKEN = config.get_active_token()

    log.info(f"[SERVER] Mode switched to {mode.upper()}")
    return jsonify({"mode": config.MODE, "status": "bot stopped for mode switch — restart manually"})


# ─────────────────────────────────────────
# Route: Trade history
# ─────────────────────────────────────────
@app.route("/trades")
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
# Route: Recent log lines
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
def get_log():
    return jsonify({"lines": list(reversed(_log_lines[-50:]))})


# ─────────────────────────────────────────
# Route: Health check (Render ping)
# ─────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


# ─────────────────────────────────────────
# Bot runner wrapper
# ─────────────────────────────────────────
def _run_bot_safe():
    """Wraps bot.run_bot() so exceptions don't silently kill the thread."""
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
    app.run(host="0.0.0.0", port=port, debug=False)

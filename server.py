import os
import json
import threading
from flask import Flask, jsonify, request, render_template_string
import bot
import config

app = Flask(__name__)

bot_running = False


# -----------------------------
# DASHBOARD PAGE
# -----------------------------

dashboard_html = """

<h2>APEX BINARY BOT DASHBOARD</h2>

<p>Mode: {{mode}}</p>

<button onclick="fetch('/start')">START BOT</button>
<button onclick="fetch('/stop')">STOP BOT</button>

<br><br>

<button onclick="fetch('/mode/demo')">DEMO</button>
<button onclick="fetch('/mode/live')">LIVE</button>

<h3>Trade History</h3>

<div id="trades"></div>

<script>

function loadTrades(){

fetch('/trades')
.then(r=>r.json())
.then(data=>{

let html=''

data.trades.reverse().forEach(t=>{

html += `<p>${t.symbol} | ${t.direction} | ${t.result}</p>`

})

document.getElementById("trades").innerHTML = html

})

}

setInterval(loadTrades,3000)

loadTrades()

</script>

"""


@app.route("/")
def dashboard():
    return render_template_string(dashboard_html, mode=config.MODE)


# -----------------------------
# TRADE HISTORY
# -----------------------------

@app.route("/trades")
def trades():

    with open("trade_history.json") as f:
        data = json.load(f)

    return jsonify(data)


# -----------------------------
# START BOT
# -----------------------------

@app.route("/start")
def start_bot():

    global bot_running

    if not bot_running:

        bot_running = True

        threading.Thread(target=bot.run_bot).start()

    return {"status":"bot started"}


# -----------------------------
# STOP BOT
# -----------------------------

@app.route("/stop")
def stop_bot():

    global bot_running

    bot_running = False

    return {"status":"bot stopped"}


# -----------------------------
# SWITCH MODE
# -----------------------------

@app.route("/mode/<mode>")
def change_mode(mode):

    config.MODE = mode

    return {"mode":config.MODE}


# -----------------------------
# SERVER START
# -----------------------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT",10000))

    app.run(host="0.0.0.0",port=port)

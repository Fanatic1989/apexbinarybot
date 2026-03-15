from flask import Flask, render_template, request, redirect, session
import threading
import json
import config
import bot

app = Flask(__name__)

app.secret_key = "supersecretkey"

bot_thread = None


@app.route("/", methods=["GET","POST"])
def login():

    if request.method == "POST":

        if request.form["username"] == config.ADMIN_USERNAME and request.form["password"] == config.ADMIN_PASSWORD:

            session["user"] = "admin"

            return redirect("/dashboard")

    return render_template("login.html")


@app.route("/dashboard")
def dashboard():

    if "user" not in session:
        return redirect("/")

    try:
        with open("trade_history.json") as f:
            trades = json.load(f)
    except:
        trades = []

    wins = sum(1 for t in trades if t["result"]=="W")
    losses = sum(1 for t in trades if t["result"]=="L")
    draws = sum(1 for t in trades if t["result"]=="D")

    return render_template(
        "dashboard.html",
        trades=trades[-20:],
        wins=wins,
        losses=losses,
        draws=draws,
        mode=config.MODE
    )


@app.route("/start")
def start():

    global bot_thread

    if bot_thread is None or not bot_thread.is_alive():

        bot_thread = threading.Thread(target=bot.run_bot)

        bot_thread.start()

    return redirect("/dashboard")


@app.route("/stop")
def stop():

    bot.stop_bot()

    return redirect("/dashboard")


@app.route("/mode/<mode>")
def mode(mode):

    config.MODE = mode

    return redirect("/dashboard")


@app.route("/add_member", methods=["POST"])
def add_member():

    name = request.form["name"]

    try:
        with open("members.json") as f:
            members = json.load(f)

    except:
        members = []

    members.append({
        "name": name
    })

    with open("members.json","w") as f:
        json.dump(members,f,indent=2)

    return redirect("/dashboard")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)

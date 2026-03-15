from flask import Flask, jsonify, request
import threading
import json
import bot
import config

app = Flask(__name__)

bot_running = False


@app.route("/")
def dashboard():

    with open("trade_history.json") as f:
        history = json.load(f)

    return jsonify(history)


@app.route("/start")
def start_bot():

    global bot_running

    if not bot_running:

        bot_running = True

        threading.Thread(target=bot.run_bot).start()

    return {"status": "bot started"}


@app.route("/stop")
def stop_bot():

    global bot_running
    bot_running = False

    return {"status": "bot stopped"}


@app.route("/mode/<mode>")
def change_mode(mode):

    config.MODE = mode

    return {"mode": config.MODE}


@app.route("/add_member", methods=["POST"])
def add_member():

    user = request.json["user"]

    with open("members.json", "r") as f:
        data = json.load(f)

    data["members"].append(user)

    with open("members.json", "w") as f:
        json.dump(data, f, indent=4)

    return {"status": "member added"}


if __name__ == "__main__":
    app.run()

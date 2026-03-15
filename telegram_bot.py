import requests

TOKEN = "PUT_TELEGRAM_BOT_TOKEN"
CHAT_ID = "PUT_CHANNEL_ID"

def send_signal(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": message
    }

    requests.post(url, data=data)
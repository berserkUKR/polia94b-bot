import os
import requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = -1002498237717
THREAD_ID = 162

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "text": text},
        timeout=30,
    )
    r.raise_for_status()

send("Доброго ранку! Бот працює.")

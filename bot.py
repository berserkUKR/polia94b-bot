import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
import xml.etree.ElementTree as ET
import requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = -1002498237717
THREAD_ID = 162
QUERIES = ['"Олександра Поля" Дніпро']


def window_hours():
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        h = datetime.now(timezone.utc).hour
        return 21 if h < 6 else 3.5
    return 24  # ручний запуск: за добу


def fetch(query):
    url = ("https://news.google.com/rss/search?q=" + quote(query)
           + "&hl=uk&gl=UA&ceid=UA:uk")
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for item in root.iter("item"):
        yield (
            item.findtext("title"),
            item.findtext("link"),
            parsedate_to_datetime(item.findtext("pubDate")),
        )


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "message_thread_id": THREAD_ID,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        },
        timeout=30,
    )
    r.raise_for_status()


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours())
    found = {}
    for q in QUERIES:
        try:
            for title, link, dt in fetch(q):
                if dt >= cutoff:
                    found[title] = (link, dt)
        except Exception as e:
            print("Помилка запиту:", q, e)

    items = sorted(found.items(), key=lambda x: x[1][1], reverse=True)[:10]

    if not items:
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            send("Нових згадок немає. Бот працює.")
        return

    lines = ["📰 Нове про проспект Поля:\n"]
    for title, (link, _) in items:
        lines.append(f"• {title}\n{link}\n")
    send("\n".join(lines))


main()

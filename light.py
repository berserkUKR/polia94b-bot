import html
import json
import os
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = -1002498237717
THREAD_ID = 162
GROUP = "5.2"
URL = ("https://app.yasno.ua/api/blackout-service/public/shutdowns/"
       "regions/3/dsos/303/planned-outages")
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
LOOKAHEAD_MIN = 35   # слати нагадування, якщо до події лишилось не більше хвилин
MORNING_HOUR = 6     # о котрій (за Києвом) слати графік на день
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

STATUS_TEXT = {
    "NoOutages": "За графіком відключень сьогодні немає 🎉",
    "WaitingForSchedule": "Графік на сьогодні ще не опубліковано.",
    "EmergencyShutdowns": "Діють аварійні відключення, графік не застосовується.",
}

# ---- Аварії за адресою: вода, газ, інше ----
# Адреса: проспект Олександра Поля, 94Б. Збігом вважаємо згадку вулиці
# з номером 94 поруч (щоб не слати сповіщення про весь проспект).
ADDRESS_RE = re.compile(
    r"(?:Олександра|Александра|О\.|пр\.?|просп\.?)\s*Поля\D{0,15}94(?!\d)",
    re.IGNORECASE,
)
OUTAGE_RE = re.compile(
    r"вод|газ|відключ|аварі|отключ|без (?:води|газу|світла)|нет воды|нет газа",
    re.IGNORECASE,
)
UA = {"User-Agent": "Mozilla/5.0"}
NEWS_LISTINGS = [
    "https://www.dnipro.media/novyny-dnipro/",
    "https://dnepr.info/uk/news-cat/dnipro-news",
]
NEWS_LINK_RE = re.compile(
    r'href="(https?://(?:www\.)?(?:dnipro\.media|dnepr\.info)/[^"#?\s]+)"'
)
TG_CHANNEL = "https://t.me/s/korbanofficial"
TG_MSG_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S
)
TAG_RE = re.compile(r"<[^>]+>")
MAX_NEW_NEWS_PER_RUN = 40


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"schedule_date": None, "reminders": []}


def save_state(state):
    state["reminders"] = state["reminders"][-60:]
    state["seen"] = state.get("seen", [])[-1000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "text": text},
        timeout=30,
    )
    r.raise_for_status()


def fetch_group():
    r = requests.get(URL, timeout=30, headers=UA)
    r.raise_for_status()
    data = r.json()
    if GROUP not in data:
        raise RuntimeError(f"Черги {GROUP} немає у відповіді: {list(data)[:20]}")
    return data[GROUP]


def day_outages(day):
    """Відключення за добу як список (початок, кінець)."""
    if not day or day.get("status") != "ScheduleApplies":
        return []
    d = datetime.fromisoformat(day["date"]).astimezone(TZ).date()
    midnight = datetime.combine(d, time(0, 0), TZ)
    out = []
    for s in sorted(day.get("slots", []), key=lambda s: s["start"]):
        if "definite" not in str(s.get("type", "")).lower():
            continue
        out.append((midnight + timedelta(minutes=s["start"]),
                    midnight + timedelta(minutes=s["end"])))
    return out


def merge(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def hhmm(dt):
    return dt.strftime("%H:%M")


def fmt_dur(a, b):
    h, m = divmod(int((b - a).total_seconds() // 60), 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def schedule_message(group_data, now):
    today = group_data.get("today")
    title = f"⚡ Графік на сьогодні, {now:%d.%m}, черга {GROUP} (ЦЕК)"
    status = (today or {}).get("status")
    if status != "ScheduleApplies":
        return f"{title}\n\n" + STATUS_TEXT.get(status, f"Статус: {status}")
    items = merge(day_outages(today))
    if not items:
        return f"{title}\n\n" + STATUS_TEXT["NoOutages"]
    lines = [title, ""]
    total = timedelta()
    for a, b in items:
        lines.append(f"🔴 {hhmm(a)}–{hhmm(b)} ({fmt_dur(a, b)})")
        total += b - a
    lines.append("")
    lines.append(f"Разом без світла: {fmt_dur(now, now + total)}")
    return "\n".join(lines)


def reminders(group_data, now, state):
    today, tomorrow = group_data.get("today"), group_data.get("tomorrow")
    intervals = merge(day_outages(today) + day_outages(tomorrow))
    end_today = datetime.combine(now.date() + timedelta(days=1), time(0, 0), TZ)
    tomorrow_known = (tomorrow or {}).get("status") == "ScheduleApplies"
    events = []
    for a, b in intervals:
        events.append(("off", a, b))
        if b == end_today and not tomorrow_known:
            continue  # невідомо, чи світло з'явиться опівночі
        events.append(("on", b, None))
    for kind, t, end in events:
        left = (t - now).total_seconds() / 60
        if not (0 < left <= LOOKAHEAD_MIN):
            continue
        key = f"{kind}-{t.isoformat()}"
        if key in state["reminders"]:
            continue
        mins = round(left)
        if kind == "off":
            text = f"⚠️ Через ~{mins} хв, о {hhmm(t)}, за графіком вимкнуть світло (до {hhmm(end)})."
        else:
            text = f"💡 Через ~{mins} хв, о {hhmm(t)}, за графіком має з'явитися світло."
        send(text)
        state["reminders"].append(key)


# ---- Аварії за адресою ----

def clean(text):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", text))).strip()


def fetch_telegram_posts():
    """Повертає список (ключ, посилання, текст) з публічного Telegram-каналу."""
    r = requests.get(TG_CHANNEL, timeout=30, headers=UA)
    r.raise_for_status()
    posts = []
    for chunk in r.text.split('data-post="')[1:]:
        post_id = chunk.split('"', 1)[0]
        m = TG_MSG_TEXT_RE.search(chunk)
        if not m:
            continue
        posts.append((f"tg:{post_id}", f"https://t.me/{post_id}", clean(m.group(1))))
    return posts


def fetch_news_links():
    links = []
    for listing in NEWS_LISTINGS:
        try:
            r = requests.get(listing, timeout=30, headers=UA)
            r.raise_for_status()
        except Exception as e:
            print("Не вдалося завантажити", listing, e)
            continue
        for link in NEWS_LINK_RE.findall(r.text):
            if link not in links:
                links.append(link)
    return links


def fetch_article(link):
    r = requests.get(link, timeout=30, headers=UA)
    r.raise_for_status()
    return clean(r.text)


def alert_kind(text):
    t = text.lower()
    if "газ" in t:
        return "🔥 Газ"
    if "вод" in t:
        return "💧 Вода"
    return "⚠️ Відключення"


def check_addresses(state):
    seen = set(state.get("seen", []))
    initialized = state.get("addr_init", False)
    found = []  # (ключ, посилання, текст)

    try:
        for key, url, text in fetch_telegram_posts():
            if key not in seen and ADDRESS_RE.search(text) and OUTAGE_RE.search(text):
                found.append((key, url, text))
            seen.add(key)
    except Exception as e:
        print("Telegram недоступний:", e)

    try:
        new_links = [l for l in fetch_news_links() if f"news:{l}" not in seen]
    except Exception as e:
        print("Новини недоступні:", e)
        new_links = []
    for link in new_links[:MAX_NEW_NEWS_PER_RUN]:
        key = f"news:{link}"
        try:
            text = fetch_article(link)
        except Exception as e:
            print("Не вдалося відкрити", link, e)
            continue
        if ADDRESS_RE.search(text) and OUTAGE_RE.search(text):
            found.append((key, link, text))
        seen.add(key)

    # Перший запуск: лише запам'ятовуємо наявні повідомлення, щоб не надіслати старі.
    if initialized:
        for key, url, text in found:
            snippet = text[:300] + ("…" if len(text) > 300 else "")
            send(f"{alert_kind(text)} за адресою проспект Олександра Поля, 94Б:\n"
                 f"{snippet}\n{url}")
    else:
        print("Ініціалізація: знайдено", len(found), "старих повідомлень, не надсилаємо")
        state["addr_init"] = True

    state["seen"] = sorted(seen)


def main():
    now = datetime.now(TZ)
    state = load_state()
    try:
        group_data = fetch_group()
    except Exception as e:
        print("Помилка отримання графіка:", e)
        group_data = None
    if group_data is not None:
        if MANUAL:
            print(json.dumps(group_data, ensure_ascii=False)[:1500])
        today_iso = now.date().isoformat()
        if MANUAL or (now.hour >= MORNING_HOUR and state.get("schedule_date") != today_iso):
            send(schedule_message(group_data, now))
            state["schedule_date"] = today_iso
        reminders(group_data, now, state)
    try:
        check_addresses(state)
    except Exception as e:
        print("Помилка перевірки адреси:", e)
    save_state(state)


main()

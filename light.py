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
MORNING_HOUR = 9     # о котрій (за Києвом) слати графік на день
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

STATUS_TEXT = {
    "NoOutages": "За графіком відключень сьогодні немає 🎉",
    "WaitingForSchedule": "Графік на сьогодні ще не опубліковано.",
    "EmergencyShutdowns": "Діють аварійні відключення, графік не застосовується.",
}

GAS_TEXT = (
    "🔥 Автоматичної перевірки газу за адресою немає: для газу не знайдено "
    "відкритого джерела з оголошеннями.\n"
    "Аварійна служба Дніпрогазу: 104.\n"
    "Контактний центр (з прес-релізу): (056) 78-78-104, (067) 62-63-104."
)

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
MAX_NEWS_ON_DEMAND = 20


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


def send(text, markup=None):
    payload = {"chat_id": CHAT_ID, "message_thread_id": THREAD_ID, "text": text}
    if markup:
        payload["reply_markup"] = markup
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["result"]["message_id"]


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


def timeline(day_date, intervals):
    """Добу як список (початок, кінець, чи є світло) з відключеннями і без них."""
    start = datetime.combine(day_date, time(0, 0), TZ)
    end = start + timedelta(days=1)
    items = []
    cur = start
    for a, b in merge(intervals):
        a, b = max(a, start), min(b, end)
        if a >= b:
            continue
        if cur < a:
            items.append((cur, a, True))
        items.append((a, b, False))
        cur = b
    if cur < end:
        items.append((cur, end, True))
    return items


def schedule_message(group_data, now):
    today = group_data.get("today")
    title = f"⚡ Графік на сьогодні, {now:%d.%m}, черга {GROUP} (ЦЕК)"
    status = (today or {}).get("status")
    if status != "ScheduleApplies":
        return f"{title}\n\n" + STATUS_TEXT.get(status, f"Статус: {status}")
    intervals = day_outages(today)
    if not intervals:
        return f"{title}\n\n" + STATUS_TEXT["NoOutages"]
    lines = [title, ""]
    total = timedelta()
    for a, b, has_light in timeline(now.date(), intervals):
        if has_light:
            lines.append(f"🟢 {hhmm(a)}–{hhmm(b)} світло є ({fmt_dur(a, b)})")
        else:
            lines.append(f"🔴 {hhmm(a)}–{hhmm(b)} світла немає ({fmt_dur(a, b)})")
            total += b - a
    lines.append("")
    h, m = divmod(int(total.total_seconds() // 60), 60)
    lines.append(f"Без світла всього: {h} год {m} хв")
    return "\n".join(lines)


def next_15_message(group_data, now):
    today, tomorrow = group_data.get("today"), group_data.get("tomorrow")
    if (today or {}).get("status") == "EmergencyShutdowns":
        return f"⏱ {now:%H:%M}. Діють аварійні відключення, за графіком час не визначено."
    intervals = merge(day_outages(today) + day_outages(tomorrow))
    tomorrow_known = (tomorrow or {}).get("status") == "ScheduleApplies"
    end_today = datetime.combine(now.date() + timedelta(days=1), time(0, 0), TZ)
    all_events = []
    for a, b in intervals:
        all_events.append((a, "🔴 вимкнуть світло"))
        if b == end_today and not tomorrow_known:
            continue  # невідомо, чи світло з'явиться опівночі
        all_events.append((b, "🟢 увімкнуть світло"))
    all_events.sort()
    is_off = any(a <= now < b for a, b in intervals)
    state = "🔴 зараз світла немає" if is_off else "🟢 зараз світло є"
    head = f"⏱ {now:%H:%M}, {state}."
    window_end = now + timedelta(minutes=15)
    in_window = [(t, label) for t, label in all_events if now < t <= window_end]
    if in_window:
        body = "\n".join(f"{hhmm(t)} — {label}" for t, label in in_window)
        return f"{head}\nЗа 15 хвилин (до {hhmm(window_end)}) за графіком:\n{body}"
    future = [(t, label) for t, label in all_events if t > now]
    if future:
        t, label = future[0]
        return (f"{head}\nЗа 15 хвилин змін за графіком немає. "
                f"Наступна зміна о {hhmm(t)}: {label}.")
    return f"{head}\nЗмін за графіком більше немає."


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


def current_address_matches(limit=5):
    """Збіги з усіх джерел зараз, без урахування вже надісланих (для кнопки)."""
    matches = []
    try:
        for _key, url, text in reversed(fetch_telegram_posts()):
            if ADDRESS_RE.search(text) and OUTAGE_RE.search(text):
                matches.append((url, text))
    except Exception as e:
        print("Telegram недоступний:", e)
    try:
        for link in fetch_news_links()[:MAX_NEWS_ON_DEMAND]:
            try:
                text = fetch_article(link)
            except Exception:
                continue
            if ADDRESS_RE.search(text) and OUTAGE_RE.search(text):
                matches.append((link, text))
    except Exception as e:
        print("Новини недоступні:", e)
    return matches[:limit]


def water_report():
    matches = current_address_matches()
    if not matches:
        return ("За адресою проспект Олександра Поля, 94 у перевірених джерелах "
                "зараз нічого не знайдено.")
    lines = ["Знайдено в джерелах (дати в них не завжди є, перевірте за посиланням):"]
    for url, text in matches:
        snippet = text[:200] + ("…" if len(text) > 200 else "")
        lines.append(f"{alert_kind(text)}: {snippet}\n{url}")
    return "\n\n".join(lines)


def status_message(group_data, now):
    """Який режим діє сьогодні і завтра: аварійні, графік чи без відключень."""
    today = group_data.get("today") or {}
    tomorrow = group_data.get("tomorrow") or {}

    def mode(day):
        s = day.get("status")
        if s == "EmergencyShutdowns":
            return "🚨 аварійні (стабілізаційні) відключення, графік не застосовується"
        if s == "ScheduleApplies":
            n = len(merge(day_outages(day)))
            if n == 0:
                return "🟢 за графіком відключень немає"
            return f"📅 за графіком {n} період(и) відключення"
        if s == "WaitingForSchedule":
            return "⏳ графік ще не опубліковано"
        return f"статус: {s}"

    return (f"ℹ️ Статус черги {GROUP} (ЦЕК), {now:%d.%m %H:%M}:\n\n"
            f"Сьогодні: {mode(today)}\n"
            f"Завтра: {mode(tomorrow)}")


# ---- Зміни графіка ----

def day_signature(date, day):
    outs = [[a.isoformat(), b.isoformat()] for a, b in day_outages(day)]
    return f"{date.isoformat()}|" + json.dumps([day.get("status"), outs])


def day_schedule_body(day, date):
    status = day.get("status")
    if status != "ScheduleApplies":
        return STATUS_TEXT.get(status, f"Статус: {status}")
    intervals = day_outages(day)
    if not intervals:
        return STATUS_TEXT["NoOutages"]
    lines = []
    total = timedelta()
    for a, b, has_light in timeline(date, intervals):
        if has_light:
            lines.append(f"🟢 {hhmm(a)}–{hhmm(b)} світло є ({fmt_dur(a, b)})")
        else:
            lines.append(f"🔴 {hhmm(a)}–{hhmm(b)} світла немає ({fmt_dur(a, b)})")
            total += b - a
    h, m = divmod(int(total.total_seconds() // 60), 60)
    lines.append("")
    lines.append(f"Без світла всього: {h} год {m} хв")
    return "\n".join(lines)


def check_schedule_changes(state, group_data, now):
    tomorrow_date = now.date() + timedelta(days=1)
    checks = [
        ("sig_today", group_data.get("today") or {}, now.date()),
        ("sig_tomorrow", group_data.get("tomorrow") or {}, tomorrow_date),
    ]
    for key, day, date in checks:
        sig = day_signature(date, day)
        old = state.get(key)
        state[key] = sig
        # Перший запуск або інша дата: лише запам'ятовуємо, без сповіщення
        if old is None or old.split("|", 1)[0] != date.isoformat() or old == sig:
            continue
        old_status = json.loads(old.split("|", 1)[1])[0]
        if key == "sig_tomorrow" and old_status != "ScheduleApplies" \
                and day.get("status") == "ScheduleApplies":
            header = f"📅 Опубліковано графік на завтра, {date:%d.%m}"
        else:
            which = "на сьогодні" if key == "sig_today" else "на завтра"
            header = f"⚠️ Графік {which} змінився, {date:%d.%m}"
        send(f"{header}\n\n{day_schedule_body(day, date)}")


# ---- Автооновлюване повідомлення-табло ----

def board_next_event(group_data, now):
    """(чи немає зараз світла, (час, дія) найближчої зміни або None)."""
    today = group_data.get("today") or {}
    tomorrow = group_data.get("tomorrow") or {}
    intervals = merge(day_outages(today) + day_outages(tomorrow))
    tomorrow_known = tomorrow.get("status") == "ScheduleApplies"
    end_today = datetime.combine(now.date() + timedelta(days=1), time(0, 0), TZ)
    is_off = any(a <= now < b for a, b in intervals)
    events = []
    for a, b in intervals:
        events.append((a, "вимкнення"))
        if b == end_today and not tomorrow_known:
            continue
        events.append((b, "увімкнення"))
    future = sorted((t, label) for t, label in events if t > now)
    return is_off, (future[0] if future else None)


def status_board_text(group_data, now):
    today = group_data.get("today") or {}
    status = today.get("status")
    lines = [f"⚡ Світло, черга {GROUP} (ЦЕК), {now:%d.%m}", ""]

    if status == "EmergencyShutdowns":
        lines.append("Тип відключень: 🚨 стабілізаційні / аварійні, графік не застосовується")
        lines.append("Світло зараз: час за графіком не визначено")
    else:
        is_off, nxt = board_next_event(group_data, now)
        type_text = "📅 графікові" if status == "ScheduleApplies" else f"статус: {status}"
        lines.append(f"Тип відключень: {type_text}")
        lines.append("Світло зараз: " + ("🔴 немає" if is_off else "🟢 є"))
        if nxt:
            t, label = nxt
            lines.append(f"Наступна зміна: о {hhmm(t)} — {label}")


    if status == "ScheduleApplies":
        intervals = day_outages(today)
        lines.append("")
        lines.append("Відключення на сьогодні:")
        outages = [(a, b) for a, b, has in timeline(now.date(), intervals) if not has]
        if outages:
            for a, b in outages:
                lines.append(f"🔴 {hhmm(a)}–{hhmm(b)}")
        else:
            lines.append("🟢 відключень немає")

    tomorrow = group_data.get("tomorrow") or {}
    lines.append("")
    if tomorrow.get("status") == "ScheduleApplies":
        t_date = now.date() + timedelta(days=1)
        lines.append(f"Відключення на завтра ({t_date:%d.%m}):")
        t_outs = [(a, b) for a, b, has in timeline(t_date, day_outages(tomorrow)) if not has]
        if t_outs:
            for a, b in t_outs:
                lines.append(f"🔴 {hhmm(a)}–{hhmm(b)}")
        else:
            lines.append("🟢 відключень немає")
    else:
        lines.append("Графік на завтра ще не опубліковано")

    lines.append("")
    lines.append("Проспект Олександра Поля, 94 (вода, газ):")
    matches = current_address_matches(limit=3)
    if matches:
        for url, text in matches:
            lines.append(f"• {alert_kind(text)}: {text[:120]}")
            lines.append(url)
    else:
        lines.append("щодо аварійних відключень у перевірених джерелах нічого не знайдено")

    lines.append("")
    lines.append(f"Оновлено: {now:%d.%m.%Y %H:%M}")
    lines.append("Дані можуть відображатися із затримкою і бути не зовсім точними.")
    return "\n".join(lines)


def edit_message(message_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/editMessageText",
        json={"chat_id": CHAT_ID, "message_id": message_id, "text": text},
        timeout=30,
    )
    if r.status_code == 400 and "not modified" in r.text:
        return
    r.raise_for_status()


def update_status_board(state, group_data, now):
    text = status_board_text(group_data, now)
    msg_id = state.get("board_msg_id")
    if msg_id:
        try:
            edit_message(msg_id, text)
            return
        except Exception as e:
            print("Не вдалося редагувати табло, надсилаю нове:", e)
    state["board_msg_id"] = send(text)


# ---- Кнопки в Telegram ----
# Бот перевіряє нові команди й натискання при кожному запуску (раз на 15 хв).

MENU_MARKUP = {"inline_keyboard": [
    [{"text": "📅 Графік на сьогодні", "callback_data": "schedule"}],
    [{"text": "⏱ Світло за 15 хв", "callback_data": "next15"}],
    [{"text": "ℹ️ Статус зараз", "callback_data": "status"}],
    [{"text": "💧 Вода за адресою", "callback_data": "water"}],
    [{"text": "🔥 Газ за адресою", "callback_data": "gas"}],
]}


def answer_callback(callback_id):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
        json={"callback_query_id": callback_id},
        timeout=30,
    )


def handle_callback(cb):
    answer_callback(cb["id"])
    if cb.get("message", {}).get("chat", {}).get("id") != CHAT_ID:
        return
    data = cb.get("data")
    if data == "schedule":
        send(schedule_message(fetch_group(), datetime.now(TZ)))
    elif data == "next15":
        send(next_15_message(fetch_group(), datetime.now(TZ)))
    elif data == "status":
        send(status_message(fetch_group(), datetime.now(TZ)))
    elif data == "water":
        send(water_report())
    elif data == "gas":
        send(GAS_TEXT)


def handle_message(msg):
    if msg.get("chat", {}).get("id") != CHAT_ID:
        return
    text = (msg.get("text") or "").strip().lower()
    if text.startswith("/menu") or text.startswith("/start"):
        send("Оберіть, що показати:", MENU_MARKUP)


def process_updates(state):
    params = {"timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])}
    if "update_offset" in state:
        params["offset"] = state["update_offset"]
    r = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    updates = r.json().get("result", [])
    print(f"Отримано оновлень від Telegram: {len(updates)}")
    now_ts = datetime.now(TZ).timestamp()
    for u in updates:
        state["update_offset"] = u["update_id"] + 1
        save_state(state)  # зберігаємо одразу, щоб команда не оброблялась повторно
        msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        if now_ts - msg.get("date", now_ts) > 3600:
            continue  # команди старші за годину ігноруємо
        try:
            if "callback_query" in u:
                handle_callback(u["callback_query"])
            elif "message" in u:
                handle_message(u["message"])
        except Exception as e:
            print("Помилка обробки оновлення:", e)


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
        if (group_data.get("today") or {}).get("status") == "EmergencyShutdowns" \
                and state.get("emergency_date") != today_iso:
            # Записуємо сиру відповідь у лог, щоб було видно формат даних
            print("Аварійні відключення:", json.dumps(group_data, ensure_ascii=False)[:1500])
            send("🚨 Діють аварійні (стабілізаційні) відключення для черги 5.2 (ЦЕК). "
                 "Планового графіка на сьогодні немає, слідкуйте за оновленнями.")
            state["emergency_date"] = today_iso
        if now.hour >= MORNING_HOUR and state.get("schedule_date") != today_iso:
            send(schedule_message(group_data, now))
            state["schedule_date"] = today_iso
        reminders(group_data, now, state)
    if group_data is not None:
        try:
            check_schedule_changes(state, group_data, now)
        except Exception as e:
            print("Помилка перевірки змін графіка:", e)
        try:
            update_status_board(state, group_data, now)
        except Exception as e:
            print("Помилка табло:", e)
    try:
        check_addresses(state)
    except Exception as e:
        print("Помилка перевірки адреси:", e)
    try:
        process_updates(state)
    except Exception as e:
        print("Помилка читання команд:", e)
    save_state(state)


main()

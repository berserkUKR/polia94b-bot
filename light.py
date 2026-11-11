import json
import os
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


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"schedule_date": None, "reminders": []}


def save_state(state):
    state["reminders"] = state["reminders"][-60:]
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
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
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


def main():
    now = datetime.now(TZ)
    state = load_state()
    try:
        group_data = fetch_group()
    except Exception as e:
        print("Помилка отримання графіка:", e)
        return
    if MANUAL:
        print(json.dumps(group_data, ensure_ascii=False)[:1500])
    today_iso = now.date().isoformat()
    if MANUAL or (now.hour >= MORNING_HOUR and state.get("schedule_date") != today_iso):
        send(schedule_message(group_data, now))
        state["schedule_date"] = today_iso
    reminders(group_data, now, state)
    save_state(state)


main()

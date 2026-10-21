#!/usr/bin/env python3
"""Sagban "disciplinator" Telegram bot.

Pure standard library (Python 3.9+), no pip packages required.

Task 1 — daily start-of-work report
-----------------------------------
Employees post a video in the "Кунлик режа" topic of the group before 09:00.
Every day except Sunday, at 09:00 (Asia/Tashkent, UTC+5), the bot posts in
that same topic:

    Кунингиз Барокатли булсин!
    Иш вактини бошлади:
    1. 07:56:28 - Муаззам
    2. 08:43:09 - Севарахон С12
    ...

Run:  python bot.py          (config in config.json next to this file)
"""

import difflib
import json
import os
import re
import sys
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):      # Windows console: never crash on Cyrillic/emoji output
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DATA_PATH = HERE / "data.json"

TZ = timezone(timedelta(hours=5))  # Uzbekistan, no DST

DEFAULT_CONFIG = {
    "token": "",
    "group_id": -1002318997025,
    "topic_id": 995,            # message_thread_id of "Кунлик режа"; change with /mavzu
    "admins": [1720865275],     # Telegram user IDs allowed to give commands
    "report_time": "09:00",
    "skip_weekdays": [6],       # 0=Mon ... 6=Sun
    "header": "Кунингиз Барокатли булсин!\nИш вактини бошлади:",
    "show_missing": True,
    "evening_time": "22:00",
    "evening_header": "Кунлик хисобот ёзмаганлар:",
    "evening_all_done": "Бугун ҳамма кунлик ҳисобот ёзди ✅",
    "reminder_text": "Хисобот ёзмаганизи эслатиб утаман",
    "report_min_chars": 10,
    "missing_header": "Ишга келмаганлар (09:00 гача видео ташламаганлар):",
    "employees": [
        "Дилшодахон Сгб",
        "Гулшода👨‍👩‍👧‍👧",
        "Абдулвосид Ку12",
        "Ёкутхон Т",
        "Севарахон С12",
        "Умидахон О2",
        "Жаннатой ВЧ",
        "Гавхарой ТП",
        "Зулайхо Ёз",
        "Муаззам",
        "🖤Farangizim🖤",
    ],
    "bindings": {}              # "user_id" -> employee name (learned or set with /biriktir)
}


# ----------------------------------------------------------------- storage

def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.loads(json.dumps(default))


def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
for k, v in DEFAULT_CONFIG.items():
    config.setdefault(k, v)
data = load_json(DATA_PATH, {"videos": {}, "reports_sent": {}, "offset": 0})
lock = threading.Lock()

TOKEN = os.environ.get("BOT_TOKEN") or config.get("token")
if not TOKEN:
    sys.exit("Token topilmadi: config.json ichida \"token\" ni to'ldiring yoki BOT_TOKEN muhit o'zgaruvchisini bering.")
API = f"https://api.telegram.org/bot{TOKEN}/"


# ----------------------------------------------------------------- telegram

def api(method, http_timeout=35, **params):
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API + method, data=body)
    with urllib.request.urlopen(req, timeout=http_timeout) as resp:
        out = json.load(resp)
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out}")
    return out["result"]


def send(chat_id, text, thread_id=None, reply_to=None, keyboard=None):
    return api("sendMessage", chat_id=chat_id, text=text,
               message_thread_id=thread_id, reply_to_message_id=reply_to,
               reply_markup=json.dumps({"inline_keyboard": keyboard}) if keyboard else None)


def edit(chat_id, message_id, text, keyboard=None):
    return api("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
               reply_markup=json.dumps({"inline_keyboard": keyboard}) if keyboard else None)


# ----------------------------------------------------------------- helpers

def now():
    return datetime.now(TZ)


def today_key(dt=None):
    return (dt or now()).strftime("%Y-%m-%d")


_LAT = [("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ye", "е"),
        ("o'", "ў"), ("o‘", "ў"), ("oʻ", "ў"), ("g'", "ғ"), ("g‘", "ғ"), ("gʻ", "ғ"),
        ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "ҳ"),
        ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
        ("p", "п"), ("q", "қ"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"),
        ("w", "в"), ("x", "х"), ("y", "й"), ("z", "з"), ("c", "к")]


def translit(s):
    s = (s or "").lower()
    for lat, cyr in _LAT:
        s = s.replace(lat, cyr)
    return s


def norm(s):
    """Cyrillic letters/digits only, Latin transliterated: 'Sevara' -> 'севара'."""
    s = translit(s)
    s = s.replace("ҳ", "х").replace("ў", "у").replace("қ", "к").replace("ғ", "г").replace("ё", "е")
    return re.sub(r"[^0-9а-я]+", "", s)


def similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def display_name(user):
    return " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p).strip()


def match_employee(user):
    """Return the canonical employee name for a Telegram user, or None."""
    uid = str(user["id"])
    if uid in config["bindings"]:
        return config["bindings"][uid]
    shown = display_name(user)
    nshown = norm(shown)
    if len(nshown) < 3:
        return None                      # emoji-only names: bind with /biriktir
    scored = []
    for emp in config["employees"]:
        nemp = norm(emp)
        first = norm(emp.split()[0]) if emp.split() else nemp
        score = max(similar(nshown, nemp), similar(nshown, first),
                    1.0 if (nshown.startswith(first) or first.startswith(nshown)) and min(len(first), len(nshown)) >= 5 else 0)
        scored.append((score, emp))
    scored.sort(reverse=True)
    best, second = scored[0], (scored[1] if len(scored) > 1 else (0, ""))
    if best[0] >= 0.8 and best[0] - second[0] >= 0.1:
        config["bindings"][uid] = best[1]
        save_json(CONFIG_PATH, config)
        return best[1]
    return None


def is_video(msg):
    if "video" in msg or "video_note" in msg:
        return True
    doc = msg.get("document")
    return bool(doc and str(doc.get("mime_type", "")).startswith("video/"))


def in_report_topic(msg):
    if config["topic_id"] is None:
        return True
    return msg.get("message_thread_id") == config["topic_id"]


def is_admin(user):
    return user and user["id"] in config["admins"]


# ----------------------------------------------------------------- report

def build_report(day):
    """Who posted a video before report_time (e.g. 09:00:00) and who did not."""
    cutoff = config["report_time"] + ":00"
    videos = data["videos"].get(day, {})
    rows = sorted((v for v in videos.values() if v["time"] < cutoff), key=lambda v: v["time"])
    lines = [config["header"]]
    for i, v in enumerate(rows, 1):
        lines.append(f"{i}. {v['time']} - {v['name']}")
    if not rows:
        lines.append("Ҳали ҳеч ким видео ташламади.")
    if config["show_missing"]:
        seen = {v["name"] for v in rows}
        missing = [e for e in config["employees"] if e not in seen]
        if missing:
            lines.append("")
            lines.append(config["missing_header"])
            lines.extend(f"- {e}" for e in missing)
    return "\n".join(lines)


MONTHS = ["Январ", "Феврал", "Март", "Апрел", "Май", "Июн",
          "Июл", "Август", "Сентябр", "Октябр", "Ноябр", "Декабр"]


def calendar_keyboard(year, month):
    """Inline keyboard with the days of a month; days with records are marked with a dot."""
    first = datetime(year, month, 1, tzinfo=TZ)
    days_in_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    rows = [[{"text": t, "callback_data": "noop"} for t in ["Ду", "Се", "Чо", "Па", "Жу", "Ша", "Як"]]]
    row = [{"text": " ", "callback_data": "noop"}] * first.weekday()
    for d in range(1, days_in_month.day + 1):
        key = f"{year:04d}-{month:02d}-{d:02d}"
        mark = "•" if data["videos"].get(key) else ""
        row.append({"text": f"{d}{mark}", "callback_data": f"day:{key}"})
        if len(row) == 7:
            rows.append(row)
            row = []
    if row:
        row += [{"text": " ", "callback_data": "noop"}] * (7 - len(row))
        rows.append(row)
    prev_m = (first - timedelta(days=1))
    next_m = (days_in_month + timedelta(days=1))
    rows.append([{"text": "◀ " + MONTHS[prev_m.month - 1], "callback_data": f"month:{prev_m:%Y-%m}"},
                 {"text": MONTHS[next_m.month - 1] + " ▶", "callback_data": f"month:{next_m:%Y-%m}"}])
    return rows


def calendar_text(year, month):
    return f"📅 {MONTHS[month - 1]} {year}\nКунни танланг (• — маълумот бор):"


def day_report(day):
    y, m, d = (int(x) for x in day.split("-"))
    title = f"📅 {d:02d}.{m:02d}.{y}"
    if day not in data["videos"] and day not in data.get("reports", {}):
        return f"{title}\nБу кун учун маълумот йўқ."
    reports = data.get("reports", {}).get(day, {})
    rows = sorted(reports.items(), key=lambda kv: kv[1]["time"])
    cutoff = config["evening_time"] + ":00"
    evening = ["Кунлик ҳисобот ёзганлар:"] + [f"{i}. {t['time']} - {n}{' (кеч)' if t['time'] >= cutoff else ''}"
                                              for i, (n, t) in enumerate(rows, 1)]
    if not rows:
        evening.append("ҳеч ким")
    evening += ["", build_evening(day)[0]]
    return f"{title}\n\n{build_report(day)}\n\n" + "\n".join(evening)


def build_evening(day):
    """Who has not written the daily text report in the topic before evening_time."""
    cutoff = config["evening_time"] + ":00"
    written = {n for n, r in data.get("reports", {}).get(day, {}).items() if r["time"] < cutoff}
    missing = [e for e in config["employees"] if e not in written]
    if not missing:
        return config["evening_all_done"], []
    lines = [config["evening_header"]]
    lines += [f"{i}. {e}" for i, e in enumerate(missing, 1)]
    return "\n".join(lines), missing


def employee_user_id(name):
    for uid, n in config["bindings"].items():
        if n == name:
            return int(uid)
    return None


def send_evening(day, manual=False):
    text, missing = build_evening(day)
    send(config["group_id"], text, thread_id=config["topic_id"])
    if manual:
        return
    data.setdefault("evening_sent", {})[day] = now().isoformat()
    save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] kechki ro'yxat yuborildi ({day}), yozmaganlar: {len(missing)}")
    unreachable = []
    for name in missing:
        uid = employee_user_id(name)
        if uid is None or str(uid) not in data.get("started", []):
            unreachable.append(name)
            continue
        try:
            send(uid, config["reminder_text"])
        except Exception as e:
            print(f"eslatma yuborilmadi ({name}): {e}")
            unreachable.append(name)
    if unreachable:
        note = ("Шахсий эслатма юборилмади (ҳали ботга Start босмаган ёки танилмаган):\n"
                + "\n".join(f"- {n}" for n in unreachable)
                + "\n\nУларга айтинг: @" + BOT_USERNAME + " га кириб Start босишсин.")
        for admin in config["admins"]:
            try:
                send(admin, note)
            except Exception as e:
                print("admin xabar xato:", e)


def send_report(day, manual=False):
    text = build_report(day)
    send(config["group_id"], text, thread_id=config["topic_id"])
    if not manual:
        data["reports_sent"][day] = now().isoformat()
        save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] hisobot yuborildi ({day}{', qo‘lda' if manual else ''})")


def _due(t, hhmm):
    hh, mm = (int(x) for x in hhmm.split(":"))
    minutes = t.hour * 60 + t.minute
    return (hh * 60 + mm) <= minutes < (hh * 60 + mm + 5)   # only in the 5 minutes after the set time


def scheduler():
    while True:
        try:
            t = now()
            day = today_key(t)
            if t.weekday() not in config["skip_weekdays"]:
                if _due(t, config["report_time"]) and day not in data["reports_sent"]:
                    with lock:
                        send_report(day)
                if _due(t, config["evening_time"]) and day not in data.setdefault("evening_sent", {}):
                    with lock:
                        send_evening(day)
        except Exception as e:  # keep running whatever happens
            print("scheduler xato:", e)
        time.sleep(20)


# ----------------------------------------------------------------- handlers

def record_video(msg):
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    name = match_employee(user)
    if not name:
        print(f"[{now():%H:%M:%S}] noma'lum foydalanuvchi video tashladi: {display_name(user)} ({user.get('id')})")
        notify_unknown(user, msg)
        return
    t = datetime.fromtimestamp(msg["date"], TZ)
    day = today_key(t)
    videos = data["videos"].setdefault(day, {})
    if name in videos:          # only the first video of the day counts
        return
    videos[name] = {"name": name, "time": t.strftime("%H:%M:%S"),
                    "user_id": user["id"], "message_id": msg["message_id"]}
    save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] {name} — {videos[name]['time']}")


def record_text_report(msg):
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if len(text) < config["report_min_chars"]:
        return
    name = match_employee(user)
    if not name:
        return
    t = datetime.fromtimestamp(msg["date"], TZ)
    day = today_key(t)
    reports = data.setdefault("reports", {}).setdefault(day, {})
    if name in reports:
        return
    reports[name] = {"time": t.strftime("%H:%M:%S"), "message_id": msg["message_id"]}
    save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] kunlik hisobot: {name} — {reports[name]['time']}")


def notify_unknown(user, msg):
    """Tell the admins (privately, once a day per user) about a video from someone not in the list."""
    key = f"{today_key()}:{user['id']}"
    seen = data.setdefault("unknown_notified", [])
    if key in seen:
        return
    seen.append(key)
    save_json(DATA_PATH, data)
    link = f"https://t.me/c/{str(config['group_id'])[4:]}/{msg['message_id']}"
    text = (f"«{display_name(user)}» видео ташлади, лекин рўйхатдан танимадим.\n"
            f"Хабар: {link}\n"
            f"Бириктириш учун ўша хабарга reply қилиб ёзинг: /biriktir <рўйхатдаги рақам ёки исм>\n"
            f"Рўйхат: /xodimlar")
    for admin in config["admins"]:
        try:
            send(admin, text)
        except Exception as e:
            print("admin xabar xato:", e)


def resolve_employee(arg):
    """'5' -> 5th employee; otherwise exact or fuzzy name."""
    arg = arg.strip()
    if arg.isdigit() and 1 <= int(arg) <= len(config["employees"]):
        return config["employees"][int(arg) - 1]
    if arg in config["employees"]:
        return arg
    scored = sorted(((similar(norm(arg), norm(e)), e) for e in config["employees"]), reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 0.8 else None


def handle_command(msg, cmd, arg):
    user = msg.get("from") or {}
    chat_id = msg["chat"]["id"]
    thread = msg.get("message_thread_id")
    if not is_admin(user):
        if cmd == "start" and msg["chat"]["type"] == "private":
            started = data.setdefault("started", [])
            if str(user["id"]) not in started:
                started.append(str(user["id"]))
                save_json(DATA_PATH, data)
            name = match_employee(user)
            send(chat_id, f"Салом{', ' + name if name else ''}! Эслатмаларни энди шу ерда оласиз.")
        return
    if cmd == "ping":
        send(chat_id, "Бот ишлаяпти ✅", thread_id=thread)
    elif cmd == "mavzu":
        if chat_id != config["group_id"]:
            send(chat_id, "Бу буйруқни гуруҳдаги «Кунлик режа» мавзусида ёзинг.", thread_id=thread)
            return
        config["topic_id"] = thread
        save_json(CONFIG_PATH, config)
        send(chat_id, f"Бу мавзу «Кунлик режа» сифатида сақланди (ID {thread}).", thread_id=thread)
    elif cmd == "hisobot":
        day = arg.strip() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg.strip()) else today_key()
        send(chat_id, build_report(day), thread_id=thread)
    elif cmd == "kechki":
        send(chat_id, build_evening(today_key())[0], thread_id=thread)
    elif cmd in ("kunlar", "sanalar", "kalendar"):
        t = now()
        send(chat_id, calendar_text(t.year, t.month), thread_id=thread, keyboard=calendar_keyboard(t.year, t.month))
    elif cmd == "biriktir":
        target = (msg.get("reply_to_message") or {}).get("from")
        if not target or not arg.strip():
            send(chat_id, "Ходимнинг хабарига жавоб (reply) қилиб ёзинг: /biriktir 5  ёки  /biriktir Исм", thread_id=thread)
            return
        name = resolve_employee(arg)
        if not name:
            send(chat_id, "Бундай исм рўйхатда йўқ. /xodimlar билан рўйхатни кўринг.", thread_id=thread)
            return
        config["bindings"][str(target["id"])] = name
        save_json(CONFIG_PATH, config)
        send(chat_id, f"{display_name(target)} → {name} бириктирилди.", thread_id=thread)
    elif cmd == "xodimlar":
        bound = {v: k for k, v in config["bindings"].items()}
        lines = ["Ходимлар рўйхати:"]
        for i, e in enumerate(config["employees"], 1):
            st = "📩" if e in bound and bound[e] in data.get("started", []) else ""
            lines.append(f"{i}. {e} {'✅' if e in bound else '⚠️ ҳали танилмаган'} {st}".rstrip())
        lines.append("\n✅ танилган, 📩 ботга Start босган (шахсий эслатма олади)")
        send(chat_id, "\n".join(lines), thread_id=thread)
    elif cmd in ("start", "help", "yordam"):
        send(chat_id, "Буйруқлар:\n/hisobot — бугунги рўйхат\n/kunlar — ойнинг кунлари, кунни танлаб ўша куннинг рўйхатини кўриш\n/kechki — бугун кунлик ҳисобот ёзмаганлар\n/mavzu — шу мавзуни «Кунлик режа» қилиб белгилаш\n"
                      "/biriktir Исм — (хабарга reply қилиб) ходимни бириктириш\n/xodimlar — ходимлар рўйхати\n/ping — бот ишлаяптими",
             thread_id=thread)


def handle_callback(cq):
    user = cq.get("from") or {}
    cq_id = cq["id"]
    msg = cq.get("message") or {}
    if not is_admin(user):
        api("answerCallbackQuery", callback_query_id=cq_id, text="Бу тугма фақат админ учун.")
        return
    payload = cq.get("data") or ""
    chat_id, mid = msg["chat"]["id"], msg["message_id"]
    try:
        if payload.startswith("day:"):
            day = payload[4:]
            y, m, _ = (int(x) for x in day.split("-"))
            edit(chat_id, mid, day_report(day),
                 keyboard=[[{"text": "◀ " + MONTHS[m - 1], "callback_data": f"month:{y:04d}-{m:02d}"}]])
        elif payload.startswith("month:"):
            y, m = (int(x) for x in payload[6:].split("-"))
            edit(chat_id, mid, calendar_text(y, m), keyboard=calendar_keyboard(y, m))
    except RuntimeError as e:
        if "not modified" not in str(e):
            raise
    api("answerCallbackQuery", callback_query_id=cq_id)


def handle_update(upd):
    if "callback_query" in upd:
        handle_callback(upd["callback_query"])
        return
    msg = upd.get("message")
    if not msg:
        return
    text = msg.get("text") or ""
    m = re.match(r"^/([A-Za-z_]+)(?:@\w+)?(?:\s+(.*))?$", text, re.S)
    if m:
        handle_command(msg, m.group(1).lower(), m.group(2) or "")
        return
    if msg["chat"]["id"] == config["group_id"] and in_report_topic(msg):
        if is_video(msg):
            record_video(msg)
        if msg.get("text") or msg.get("caption"):
            record_text_report(msg)


# ----------------------------------------------------------------- main

BOT_USERNAME = "sagban_yr_bot"


def main():
    global BOT_USERNAME
    me = api("getMe")
    BOT_USERNAME = me["username"]
    print(f"Bot ishga tushdi: @{me['username']}  (guruh {config['group_id']}, mavzu {config['topic_id']}, hisobot {config['report_time']})")
    api("deleteWebhook")
    threading.Thread(target=scheduler, daemon=True).start()
    offset = data.get("offset", 0)
    while True:
        try:
            updates = api("getUpdates", http_timeout=60, offset=offset, timeout=30)
        except Exception as e:
            print("getUpdates xato:", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                with lock:
                    handle_update(upd)
            except Exception as e:
                print("update xato:", e)
        if updates:
            data["offset"] = offset
            save_json(DATA_PATH, data)


if __name__ == "__main__":
    main()

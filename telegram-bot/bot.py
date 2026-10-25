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
import http.client
import json
import os
import re
import socket
import sys
import time
import threading
import traceback
import urllib.parse
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
LOG_PATH = HERE / "bot.log"


class _Tee:
    """Write console output to bot.log as well, so a hidden bot can still be inspected."""
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
                os.replace(LOG_PATH, LOG_PATH.with_suffix(".log.1"))
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


sys.stdout = _Tee(sys.stdout)
sys.stderr = _Tee(sys.stderr)

TZ = timezone(timedelta(hours=5))  # Uzbekistan, no DST

DEFAULT_CONFIG = {
    "token": "",
    "proxy": "",                # optional: "http://host:port" or "socks5://user:pass@host:port"
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
    "report_keywords": ["отчёт", "отчет", "otchet", "otchyot", "ҳисобот", "хисобот", "hisobot"],
    "heartbeat_times": ["07:00", "17:00"],   # private "bot is alive" messages to the admins
    "holidays": [],                          # "YYYY-MM-DD" days with no lists (/dam)
    "reminders": [                           # one-off reminders to the admins (/eslat)
        {"at": "2026-10-26 10:00", "text": "Эслатма: ботнинг маълумотларини GitHub'га юкланг (/eksport билан файлларни олинг)."},
        {"at": "2026-10-26 10:00", "text": "Эслатма: BotFather'да /revoke қилиб янги токен олинг ва config.json га ёзинг, кейин stop.bat → start_hidden.vbs."}
    ],
    "monthly_time": "10:00",                 # on the 1st: previous month's summary to the admins
    "photo_topic_name": "Дуконлар расми",   # topic where shop photos are posted (found by name)
    "photo_topic_id": None,     # set automatically when the topic is seen; may be set by hand
    "photo_report_time": "18:00",
    "photo_header": "Бугунги дўконлар расми",
    "photos_in_evening": False, # also append the photo counts to the 22:00 "Кунлик режа" message
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
    """Atomic write; retried because Windows (antivirus, a second bot window) may hold the file briefly."""
    tmp = path.with_suffix(".tmp")
    for attempt in range(5):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return
        except OSError as e:
            if attempt == 4:
                print(f"saqlash xato ({path.name}): {e}")
                return
            time.sleep(0.3)


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

API_HOST = "api.telegram.org"
_conns = {}          # http_timeout -> persistent HTTPS connection (keeps TLS handshakes rare)
_conn_locks = {}     # http_timeout -> lock, so a send never waits behind the long poll
_conn_locks_guard = threading.Lock()
backlog_drained = threading.Event()   # set once the first getUpdates batch after start is processed


def _conn_lock(timeout):
    with _conn_locks_guard:
        return _conn_locks.setdefault(timeout, threading.Lock())


def _new_connection(timeout):
    proxy = (config.get("proxy") or os.environ.get("BOT_PROXY")
             or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    if proxy.startswith("socks"):
        try:
            import socks  # PySocks:  pip install pysocks
        except ImportError:
            sys.exit("SOCKS proksi uchun PySocks kerak:  pip install pysocks")
        u = urllib.parse.urlparse(proxy)
        kind = socks.SOCKS5 if "5" in u.scheme else socks.SOCKS4
        socks.set_default_proxy(kind, u.hostname, u.port, username=u.username, password=u.password)
        socket.socket = socks.socksocket
        return http.client.HTTPSConnection(API_HOST, 443, timeout=timeout)
    if proxy.startswith("http"):
        u = urllib.parse.urlparse(proxy)
        c = http.client.HTTPSConnection(u.hostname, u.port, timeout=timeout)
        headers = {}
        if u.username:
            import base64
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(f"{u.username}:{u.password or ''}".encode()).decode()
        c.set_tunnel(API_HOST, 443, headers)
        return c
    return http.client.HTTPSConnection(API_HOST, 443, timeout=timeout)


def _drop_connection(timeout):
    c = _conns.pop(timeout, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def api(method, http_timeout=15, retries=3, **params):
    """Call a Bot API method over a persistent connection; retry on network errors."""
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Connection": "keep-alive"}
    last = None
    for attempt in range(retries):
        with _conn_lock(http_timeout):
            try:
                conn = _conns.get(http_timeout)
                if conn is None:
                    conn = _conns[http_timeout] = _new_connection(http_timeout)
                conn.request("POST", f"/bot{TOKEN}/{method}", body, headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as e:   # timeouts, resets, TLS, closed keep-alive
                _drop_connection(http_timeout)
                last = e
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                continue
        try:
            out = json.loads(raw)
        except ValueError:
            _drop_connection(http_timeout)
            raise RuntimeError(f"{method}: HTTP {resp.status}, javob JSON emas")
        if not out.get("ok"):
            raise RuntimeError(f"{method}: {out}")
        return out["result"]
    raise RuntimeError(f"{method}: tarmoq xatosi ({last})")


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
    words = [norm(w) for w in shown.split() if len(norm(w)) >= 4]
    scored = []
    for emp in config["employees"]:
        nemp = norm(emp)
        first = norm(emp.split()[0]) if emp.split() else nemp
        score = max(similar(nshown, nemp), similar(nshown, first),
                    1.0 if (nshown.startswith(first) or first.startswith(nshown)) and min(len(first), len(nshown)) >= 5 else 0,
                    *(similar(w, first) for w in words))
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


def is_photo(msg):
    if "photo" in msg:
        return True
    doc = msg.get("document")
    return bool(doc and str(doc.get("mime_type", "")).startswith("image/"))


def record_photo(msg):
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    name = match_employee(user)
    if not name:
        return
    t = datetime.fromtimestamp(msg["date"], TZ)
    day = today_key(t)
    rec = data.setdefault("photos", {}).setdefault(day, {}).setdefault(name, {"count": 0, "first": None, "last": None})
    group = msg.get("media_group_id")          # an album arrives as several messages sharing this id
    if group:
        groups = rec.setdefault("groups", [])
        if group in groups:
            return                                # same album: already counted once
        groups.append(group)
        del groups[:-50]
    rec["count"] += 1
    hhmmss = t.strftime("%H:%M:%S")
    rec["first"] = min(rec["first"] or hhmmss, hhmmss)
    rec["last"] = max(rec["last"] or hhmmss, hhmmss)
    save_json(DATA_PATH, data)


def build_photos(day):
    photos = data.get("photos", {}).get(day, {})
    rows = sorted(config["employees"], key=lambda e: -photos.get(e, {}).get("count", 0))
    total = sum(photos.get(e, {}).get("count", 0) for e in config["employees"])
    lines = [f"{config['photo_header']} (жами {total} та):"]
    for i, e in enumerate(rows, 1):
        n = photos.get(e, {}).get("count", 0)
        lines.append(f"{i}. {e} — {n} та" if n else f"{i}. {e} — йўқ")
    return "\n".join(lines)


def learn_topic(msg):
    """Forum messages carry the topic's name; remember thread_id -> name and resolve configured topics."""
    thread = msg.get("message_thread_id")
    created = (msg.get("reply_to_message") or {}).get("forum_topic_created")
    if thread is None or not created:
        return
    topics = data.setdefault("topics", {})
    if topics.get(str(thread)) != created["name"]:
        topics[str(thread)] = created["name"]
        save_json(DATA_PATH, data)
    if config["photo_topic_id"] is None and norm(created["name"]) == norm(config["photo_topic_name"]):
        config["photo_topic_id"] = thread
        save_json(CONFIG_PATH, config)
        print(f"[{now():%H:%M:%S}] «{created['name']}» mavzusi topildi (ID {thread})")


def photo_topic_id():
    if config["photo_topic_id"] is not None:
        return config["photo_topic_id"]
    for tid, name in data.get("topics", {}).items():
        if norm(name) == norm(config["photo_topic_name"]):
            return int(tid)
    return None


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
        mark = "•" if data["videos"].get(key) or data.get("reports", {}).get(key) or data.get("photos", {}).get(key) else ""
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
    if day not in data["videos"] and day not in data.get("reports", {}) and day not in data.get("photos", {}):
        return f"{title}\nБу кун учун маълумот йўқ."
    reports = data.get("reports", {}).get(day, {})
    rows = sorted(reports.items(), key=lambda kv: kv[1]["time"])
    cutoff = config["evening_time"] + ":00"
    evening = ["Кунлик ҳисобот ёзганлар:"] + [f"{i}. {t['time']} - {n}{' (кеч)' if t['time'] >= cutoff else ''}"
                                              for i, (n, t) in enumerate(rows, 1)]
    if not rows:
        evening.append("ҳеч ким")
    evening += ["", build_evening(day)[0], "", build_photos(day)]
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


def is_off_day(day):
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d).weekday() in config["skip_weekdays"] or day in config["holidays"]


def working_days(year, month, until=None):
    """Working days of the month up to today, starting from the first day the bot has data for."""
    recorded = [k for part in ("videos", "reports", "photos") for k in data.get(part, {})]
    first_data = min(recorded) if recorded else None
    days, d = [], datetime(year, month, 1, tzinfo=TZ)
    end = until or now()
    while d.month == month and d.date() <= end.date():
        key = d.strftime("%Y-%m-%d")
        if not is_off_day(key) and (first_data is None or key >= first_data):
            days.append(key)
        d += timedelta(days=1)
    return days


def build_monthly(year, month):
    days = working_days(year, month)
    cutoff_v = config["report_time"] + ":00"
    cutoff_r = config["evening_time"] + ":00"
    lines = [f"📊 {MONTHS[month - 1]} {year} — иш кунлари: {len(days)}",
             "(келди / келмади / ҳисобот йўқ / расмлар)"]
    rows = []
    for e in config["employees"]:
        came = sum(1 for d in days if e in data["videos"].get(d, {}) and data["videos"][d][e]["time"] < cutoff_v)
        wrote = sum(1 for d in days if e in data.get("reports", {}).get(d, {}) and data["reports"][d][e]["time"] < cutoff_r)
        photos = sum(data.get("photos", {}).get(d, {}).get(e, {}).get("count", 0) for d in days)
        rows.append((len(days) - came, e, came, wrote, photos))
    rows.sort()
    for i, (absent, e, came, wrote, photos) in enumerate(rows, 1):
        lines.append(f"{i}. {e} — {came} / {absent} / {len(days) - wrote} / {photos}")
    return "\n".join(lines)


def not_started():
    """Employees who cannot receive private messages yet."""
    bound = {v: k for k, v in config["bindings"].items()}
    started = set(data.get("started", []))
    out = []
    for e in config["employees"]:
        if e not in bound:
            out.append(f"{e} (ҳали танилмаган)")
        elif bound[e] not in started:
            out.append(e)
    return out


def send_admins(text):
    for admin in config["admins"]:
        try:
            send(admin, text)
        except Exception as e:
            print("admin xabar xato:", e)


def send_heartbeat(hhmm):
    text = f"Бот тайёр ✅ {now():%d.%m.%Y %H:%M}"
    ns = not_started()
    if ns and hhmm == config["heartbeat_times"][-1]:
        text += "\n\nБотга Start босмаганлар:\n" + "\n".join(f"- {n}" for n in ns)
    send_admins(text)
    data.setdefault("heartbeat_sent", {})[f"{today_key()} {hhmm}"] = now().isoformat()
    save_json(DATA_PATH, data)


def due_reminders():
    """Reminders whose time has come (within the last 12 hours) and not yet sent."""
    t = now()
    done = data.setdefault("reminders_done", [])
    out = []
    for r in config.get("reminders", []):
        try:
            at = datetime.strptime(r["at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            continue
        key = f"{r['at']}|{r['text']}"
        if key not in done and at <= t < at + timedelta(hours=12):
            out.append((key, r["text"]))
    return out


def api_document(chat_id, path, caption=None):
    """sendDocument with a multipart body (the only call that needs one)."""
    boundary = "----sagban" + str(int(time.time() * 1000))
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    with open(path, "rb") as f:
        content = f.read()
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{Path(path).name}\"\r\n"
                 f"Content-Type: application/json\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    conn = _new_connection(60)
    try:
        conn.request("POST", f"/bot{TOKEN}/sendDocument", body,
                     {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})
        out = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    if not out.get("ok"):
        raise RuntimeError(f"sendDocument: {out}")
    return out["result"]


def export_to(chat_id):
    exp = HERE / "config_export.json"
    safe = dict(config)
    safe["token"] = "***"
    save_json(exp, safe)
    api_document(chat_id, DATA_PATH, caption=f"data.json — {now():%d.%m.%Y %H:%M}")
    api_document(chat_id, exp, caption="config.json (токенсиз)")


def late_note():
    return f"\n\n⏰ Кечикиб юборилди (бот {now():%H:%M} да ишга тушди)"


def send_evening(day, manual=False, late=False):
    text, missing = build_evening(day)
    if config["photos_in_evening"]:
        text += "\n\n" + build_photos(day)
    send(config["group_id"], text + (late_note() if late else ""), thread_id=config["topic_id"])
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


def send_photos(day, late=False):
    thread = photo_topic_id()
    if thread is None:
        print("rasm ro'yxati yuborilmadi: «%s» mavzusi hali topilmadi" % config["photo_topic_name"])
        return
    send(config["group_id"], build_photos(day) + (late_note() if late else ""), thread_id=thread)
    data.setdefault("photos_sent", {})[day] = now().isoformat()
    save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] rasm ro'yxati yuborildi ({day})")


def send_report(day, manual=False, late=False):
    text = build_report(day)
    send(config["group_id"], text + (late_note() if late else ""), thread_id=config["topic_id"])
    if not manual:
        data["reports_sent"][day] = now().isoformat()
        save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] hisobot yuborildi ({day}{', qo‘lda' if manual else ''})")


def _minutes(hhmm):
    hh, mm = (int(x) for x in hhmm.split(":"))
    return hh * 60 + mm


def scheduler():
    """Send the two daily lists. If the bot was off at the set time, send them late the same day:
    the morning list until evening_time, the evening list until midnight."""
    backlog_drained.wait()           # first read everything that arrived while the bot was off
    while True:
        try:
            t = now()
            day = today_key(t)
            m = t.hour * 60 + t.minute
            rt, et = _minutes(config["report_time"]), _minutes(config["evening_time"])
            for hb in config["heartbeat_times"]:
                hm = _minutes(hb)
                if hm <= m < hm + 60 and f"{day} {hb}" not in data.setdefault("heartbeat_sent", {}):
                    with lock:
                        send_heartbeat(hb)
            for key, text in due_reminders():
                with lock:
                    send_admins("⏰ " + text)
                    data["reminders_done"].append(key)
                    save_json(DATA_PATH, data)
            if t.day == 1 and _minutes(config["monthly_time"]) <= m < _minutes(config["monthly_time"]) + 60 \
                    and data.setdefault("monthly_sent", {}).get(f"{t.year}-{t.month:02d}") is None:
                prev = t.replace(day=1) - timedelta(days=1)
                with lock:
                    send_admins(build_monthly(prev.year, prev.month))
                    data["monthly_sent"][f"{t.year}-{t.month:02d}"] = now().isoformat()
                    save_json(DATA_PATH, data)
            if not is_off_day(day):
                if rt <= m < et and day not in data["reports_sent"]:
                    with lock:
                        send_report(day, late=m >= rt + 5)
                if m >= et and day not in data.setdefault("evening_sent", {}):
                    with lock:
                        send_evening(day, late=m >= et + 5)
                pt = _minutes(config["photo_report_time"])
                if m >= pt and day not in data.setdefault("photos_sent", {}) and photo_topic_id() is not None:
                    with lock:
                        send_photos(day, late=m >= pt + 5)
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
        remember_unknown(user, "video", msg)
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
    low = text.lower()
    keywords = config.get("report_keywords") or []
    if keywords:
        if not any(k.lower() in low for k in keywords):
            return
    elif len(text) < config["report_min_chars"]:
        return
    name = match_employee(user)
    if not name:
        remember_unknown(user, "report", msg)
        return
    t = datetime.fromtimestamp(msg["date"], TZ)
    day = today_key(t)
    reports = data.setdefault("reports", {}).setdefault(day, {})
    if name in reports:
        return
    reports[name] = {"time": t.strftime("%H:%M:%S"), "message_id": msg["message_id"]}
    save_json(DATA_PATH, data)
    print(f"[{now():%H:%M:%S}] kunlik hisobot: {name} — {reports[name]['time']}")


def remember_unknown(user, kind, msg):
    t = datetime.fromtimestamp(msg["date"], TZ)
    ev = data.setdefault("unknown_events", {}).setdefault(str(user["id"]), [])
    ev.append({"kind": kind, "day": today_key(t), "time": t.strftime("%H:%M:%S"), "message_id": msg["message_id"]})
    del ev[:-30]
    save_json(DATA_PATH, data)


def apply_binding(uid, name):
    """Bind a Telegram user to an employee and replay what they posted while unknown."""
    config["bindings"][str(uid)] = name
    save_json(CONFIG_PATH, config)
    for ev in data.get("unknown_events", {}).pop(str(uid), []):
        if ev["kind"] == "video":
            bucket = data["videos"].setdefault(ev["day"], {})
            if name not in bucket or ev["time"] < bucket[name]["time"]:
                bucket[name] = {"name": name, "time": ev["time"], "user_id": int(uid), "message_id": ev["message_id"]}
        else:
            bucket = data.setdefault("reports", {}).setdefault(ev["day"], {})
            if name not in bucket or ev["time"] < bucket[name]["time"]:
                bucket[name] = {"time": ev["time"], "message_id": ev["message_id"]}
    save_json(DATA_PATH, data)


def bind_keyboard(uid):
    rows, row = [], []
    for i, e in enumerate(config["employees"], 1):
        row.append({"text": f"{i}. {e}", "callback_data": f"bind:{uid}:{i}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "❌ Рўйхатда йўқ", "callback_data": f"bindno:{uid}"}])
    return rows


def notify_unknown(user, msg):
    """Tell the admins (privately, once a day per user) about a video from someone not in the list."""
    if str(user["id"]) in data.get("ignored_users", []):
        return
    key = f"{today_key()}:{user['id']}"
    seen = data.setdefault("unknown_notified", [])
    if key in seen:
        return
    seen.append(key)
    save_json(DATA_PATH, data)
    link = f"https://t.me/c/{str(config['group_id'])[4:]}/{msg['message_id']}"
    text = (f"«{display_name(user)}» (ID {user['id']}) видео ташлади, лекин рўйхатдан танимадим.\n"
            f"Хабар: {link}\n\nБу ким? Тугмани босинг:")
    for admin in config["admins"]:
        try:
            send(admin, text, keyboard=bind_keyboard(user["id"]))
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
            send(chat_id, f"Салом{', ' + name if name else ''}! Эслатмаларни энди шу ерда оласиз. (ID: {user['id']})")
        return
    if cmd == "ping":
        send(chat_id, f"Бот ишлаяпти ✅\nБот вақти: {now():%H:%M:%S} (Тошкент)", thread_id=thread)
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
    elif cmd == "dam":
        days = re.findall(r"\d{4}-\d{2}-\d{2}", arg) or [today_key()]
        for d in days:
            if d not in config["holidays"]:
                config["holidays"].append(d)
        config["holidays"].sort()
        save_json(CONFIG_PATH, config)
        send(chat_id, "Дам кунлари деб белгиланди: " + ", ".join(days) + "\nБу кунларда рўйхатлар чиқмайди.", thread_id=thread)
    elif cmd == "dam_ochir":
        days = re.findall(r"\d{4}-\d{2}-\d{2}", arg)
        config["holidays"] = [d for d in config["holidays"] if d not in days]
        save_json(CONFIG_PATH, config)
        send(chat_id, "Ўчирилди: " + (", ".join(days) or "ҳеч нарса"), thread_id=thread)
    elif cmd == "damlar":
        send(chat_id, "Дам кунлари:\n" + ("\n".join(f"- {d}" for d in config["holidays"]) or "йўқ (фақат якшанба)"), thread_id=thread)
    elif cmd == "eslat":
        m_ = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)", arg.strip(), re.S)
        if not m_:
            send(chat_id, "Ёзинг: /eslat 2026-11-01 10:00 Матн", thread_id=thread)
        else:
            hh, mm = m_.group(2).split(":")
            config.setdefault("reminders", []).append({"at": f"{m_.group(1)} {int(hh):02d}:{mm}", "text": m_.group(3).strip()})
            save_json(CONFIG_PATH, config)
            send(chat_id, f"Эслатма қўйилди: {m_.group(1)} {int(hh):02d}:{mm}", thread_id=thread)
    elif cmd == "eslatmalar":
        done = set(data.get("reminders_done", []))
        items = [r for r in config.get("reminders", []) if f"{r['at']}|{r['text']}" not in done]
        send(chat_id, "Кутилаётган эслатмалар:\n" + ("\n".join(f"- {r['at']} — {r['text']}" for r in items) or "йўқ"), thread_id=thread)
    elif cmd == "oylik":
        m_ = re.fullmatch(r"(\d{4})-(\d{2})", arg.strip())
        y, mo = (int(m_.group(1)), int(m_.group(2))) if m_ else (now().year, now().month)
        send(chat_id, build_monthly(y, mo), thread_id=thread)
    elif cmd == "eksport":
        export_to(chat_id)
    elif cmd == "admin_qosh":
        if arg.strip().isdigit():
            uid = int(arg.strip())
            if uid not in config["admins"]:
                config["admins"].append(uid)
                save_json(CONFIG_PATH, config)
            send(chat_id, f"Админ қўшилди: {uid}", thread_id=thread)
        else:
            send(chat_id, "Ёзинг: /admin_qosh 123456789 (одамнинг Telegram ID си; у ботга /start ёзса ID сини /adminlar да кўрасиз)", thread_id=thread)
    elif cmd == "adminlar":
        started = data.get("started", [])
        send(chat_id, "Админлар: " + ", ".join(str(a) for a in config["admins"]) +
             "\nБотга Start босган ID лар: " + (", ".join(started) or "йўқ"), thread_id=thread)
    elif cmd == "mavzular":
        topics = data.get("topics", {})
        lines = ["Бот кўрган мавзулар:"] + [f"- {n} (ID {t})" for t, n in topics.items()] or ["ҳали йўқ"]
        lines.append(f"\nКунлик режа: {config['topic_id']}\nДўконлар расми: {photo_topic_id() or 'ҳали топилмади'}")
        send(chat_id, "\n".join(lines), thread_id=thread)
    elif cmd == "rasmlar":
        day = arg.strip() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg.strip()) else today_key()
        send(chat_id, build_photos(day), thread_id=thread)
    elif cmd == "kechki":
        send(chat_id, build_evening(today_key())[0], thread_id=thread)
    elif cmd in ("kunlar", "sanalar", "kalendar"):
        t = now()
        send(chat_id, calendar_text(t.year, t.month), thread_id=thread, keyboard=calendar_keyboard(t.year, t.month))
    elif cmd == "biriktir":
        target = (msg.get("reply_to_message") or {}).get("from")
        parts = arg.split(None, 1)
        if target is None and len(parts) == 2 and parts[0].isdigit() and len(parts[0]) >= 6:
            target = {"id": int(parts[0]), "first_name": f"ID {parts[0]}"}
            arg = parts[1]
        if not target or not arg.strip():
            send(chat_id, "Икки йўл бор:\n1) гуруҳда ходимнинг хабарига reply қилиб: /biriktir 5\n"
                          "2) шахсий чатда ID билан: /biriktir 8115418950 5\n(ID ни бот танимаган одам ҳақидаги хабарида ёзади)",
                 thread_id=thread)
            return
        name = resolve_employee(arg)
        if not name:
            send(chat_id, "Бундай исм рўйхатда йўқ. /xodimlar билан рўйхатни кўринг.", thread_id=thread)
            return
        apply_binding(target["id"], name)
        send(chat_id, f"{display_name(target)} → {name} бириктирилди.", thread_id=thread)
    elif cmd == "qosh":
        name = arg.strip()
        if not name:
            send(chat_id, "Ёзинг: /qosh Исм Фамилия", thread_id=thread)
        elif name in config["employees"]:
            send(chat_id, f"{name} аллақачон рўйхатда.", thread_id=thread)
        else:
            config["employees"].append(name)
            save_json(CONFIG_PATH, config)
            send(chat_id, f"Қўшилди: {name} (рўйхатда {len(config['employees'])}-чи).", thread_id=thread)
    elif cmd == "ochir":
        name = resolve_employee(arg) if arg.strip() else None
        if not name:
            send(chat_id, "Ёзинг: /ochir 5  ёки  /ochir Исм", thread_id=thread)
        else:
            config["employees"].remove(name)
            config["bindings"] = {k: v for k, v in config["bindings"].items() if v != name}
            save_json(CONFIG_PATH, config)
            send(chat_id, f"Ўчирилди: {name}.", thread_id=thread)
    elif cmd == "xodimlar":
        bound = {v: k for k, v in config["bindings"].items()}
        lines = ["Ходимлар рўйхати:"]
        for i, e in enumerate(config["employees"], 1):
            st = "📩" if e in bound and bound[e] in data.get("started", []) else ""
            lines.append(f"{i}. {e} {'✅' if e in bound else '⚠️ ҳали танилмаган'} {st}".rstrip())
        lines.append("\n✅ танилган, 📩 ботга Start босган (шахсий эслатма олади)")
        send(chat_id, "\n".join(lines), thread_id=thread)
    elif cmd in ("start", "help", "yordam"):
        send(chat_id, "Буйруқлар:\n/hisobot — бугунги рўйхат\n/kunlar — ойнинг кунлари, кунни танлаб ўша куннинг рўйхатини кўриш\n/kechki — бугун кунлик ҳисобот ёзмаганлар\n/rasmlar — бугун ким нечта расм ташлади\n/mavzu — шу мавзуни «Кунлик режа» қилиб белгилаш\n"
                      "/biriktir Исм — (хабарга reply қилиб) ходимни бириктириш\n/xodimlar — ходимлар рўйхати\n/qosh Исм — ходим қўшиш\n/ochir 5 — ходимни ўчириш\n/dam 2026-11-01 — дам куни\n/eslat 2026-11-01 10:00 Матн — эслатма\n/oylik — ойлик ҳисобот\n/eksport — маълумотларни файл қилиб олиш\n/ping — бот ишлаяптими",
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
        elif payload.startswith("bind:"):
            _, uid, idx = payload.split(":")
            name = config["employees"][int(idx) - 1]
            apply_binding(uid, name)
            edit(chat_id, mid, f"✅ ID {uid} → {name} бириктирилди. Бугунги видеоси/ҳисоботи ҳисобга олинди.")
        elif payload.startswith("bindno:"):
            uid = payload.split(":")[1]
            data.get("unknown_events", {}).pop(uid, None)
            data.setdefault("ignored_users", [])
            if uid not in data["ignored_users"]:
                data["ignored_users"].append(uid)
            save_json(DATA_PATH, data)
            edit(chat_id, mid, f"ID {uid} рўйхатда йўқ деб белгиланди, бошқа сўрамайман.")
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
    if msg["chat"]["id"] != config["group_id"]:
        return
    learn_topic(msg)
    if is_photo(msg) and msg.get("message_thread_id") == photo_topic_id():
        record_photo(msg)
    if in_report_topic(msg):
        if is_video(msg):
            record_video(msg)
        if msg.get("text") or msg.get("caption"):
            record_text_report(msg)


# ----------------------------------------------------------------- main

BOT_USERNAME = "sagban_yr_bot"


def main():
    global BOT_USERNAME
    print(f"\n===== {now():%Y-%m-%d %H:%M:%S} ishga tushmoqda =====")
    me = api("getMe")
    BOT_USERNAME = me["username"]
    print(f"Bot ishga tushdi: @{me['username']}  (guruh {config['group_id']}, mavzu {config['topic_id']}, rasm mavzusi {photo_topic_id()}, "
          f"ertalab {config['report_time']}, kechki {config['evening_time']}, bot vaqti {now():%H:%M})")
    api("deleteWebhook")
    backlog_drained.clear()
    threading.Thread(target=scheduler, daemon=True).start()
    offset = data.get("offset", 0)
    while True:
        try:
            updates = api("getUpdates", http_timeout=35, retries=1, offset=offset, timeout=25, limit=100)
        except Exception as e:
            msg = str(e)
            if "409" in msg or "Conflict" in msg:
                print("DIQQAT: bot boshqa joyda ham ishlayapti (ikkita oyna?). Faqat bittasini qoldiring.")
            else:
                print("getUpdates xato:", msg)
            time.sleep(3)
            continue
        if len(updates) < 100 and not backlog_drained.is_set():
            backlog_drained.set()
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                with lock:
                    handle_update(upd)
            except Exception as e:
                print("update xato:", repr(e))
                traceback.print_exc()
        if updates:
            data["offset"] = offset
            save_json(DATA_PATH, data)


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception:
            traceback.print_exc()
            print("Bot xatoga uchradi, 5 soniyadan keyin davom etadi...")
            time.sleep(5)

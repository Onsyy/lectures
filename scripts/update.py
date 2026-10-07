"""
Lecture timetable: merges RTU + LU (both fetched from their public timetable pages),
posts changes and an evening "tomorrow" message to Discord.

  python scripts/update.py check     # fetch, compare with last run, alert on changes
  python scripts/update.py evening   # send tomorrow's first lecture (once per evening)
"""
import glob, json, os, re, sys, urllib.parse, urllib.request
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------- settings (change these each semester) ----------
RTU_SEMESTER_PROGRAM_ID = 39327            # RDCP0, 2nd year, group 1, autumn 2026/27
SEMESTER_MONTHS = [(2026, 9), (2026, 10), (2026, 11), (2026, 12), (2027, 1)]
LU_URL = "https://lekciju-saraksts.lu.lv/grupa/26R-21922-PLK-3/hronologiski"   # LU group page (list view)
# -----------------------------------------------------------

TZ = ZoneInfo("Europe/Riga")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "timetable.json")
RTU_URL = "https://nodarbibas.rtu.lv/getSemesterProgEventList"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")


# ---------- RTU ----------
def fetch_rtu_month(year, month):
    body = urllib.parse.urlencode({"semesterProgramId": RTU_SEMESTER_PROGRAM_ID, "year": year, "month": month}).encode()
    req = urllib.request.Request(RTU_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (personal timetable checker)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("unexpected RTU response")
    return data


def split_rtu_name(name, lecturer):
    """'Lect, Pr. Banking Information Systems, I.Eriņš' -> ('Lect, Pr.', 'Banking Information Systems')"""
    name = (name or "").strip()
    if lecturer and name.endswith(", " + lecturer):
        name = name[: -len(", " + lecturer)]
    i = name.find(". ")
    if 0 < i <= 12:
        return name[: i + 1], name[i + 2:].strip()
    return "", name


def rtu_room(ev):
    txt = (ev.get("roomInfoTextEn") or ev.get("roomInfoText") or "").strip()
    if txt.startswith("Rem.") or txt.startswith("Att."):
        return "Remote"
    return txt or "Room TBA"


def norm_rtu(ev):
    d = datetime.fromtimestamp(ev["eventDate"] / 1000, TZ).date()
    s, e = ev["customStart"], ev["customEnd"]
    lect = ev.get("lecturerInfoTextEn") or ev.get("lecturerInfoText") or ""
    typ, subj = split_rtu_name(ev.get("eventTempNameEn") or ev.get("eventTempName"), lect)
    return {
        "id": "rtu-%s" % ev["eventDateId"], "src": "RTU",
        "subject": subj, "type": typ, "lecturer": lect,
        "date": d.isoformat(), "start": "%02d:%02d" % (s["hour"], s["minute"]), "end": "%02d:%02d" % (e["hour"], e["minute"]),
        "room": rtu_room(ev), "status": ev.get("statusId", 1),
    }


# ---------- LU ----------
from html.parser import HTMLParser


class _LURows(HTMLParser):
    """Collects the data-* attributes of every <li class="event-row"> on the LU page."""
    def __init__(self):
        super().__init__()
        self.rows = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li" and "event-row" in (a.get("class") or ""):
            self.rows.append(a)


def fetch_lu_html():
    req = urllib.request.Request(LU_URL, headers={"User-Agent": "Mozilla/5.0 (personal timetable checker)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def lu_place(building, room):
    room_short = re.split(r"\s+FT\.", room)[0].strip() if room else ""
    if not (building or room):
        return "Room TBA"
    return room_short + (" · " + building.replace("Kr. ", "") if building else "")


def norm_lu_html(html):
    p = _LURows()
    p.feed(html)
    if not p.rows:
        raise ValueError("no sessions found on the LU page")
    out = []
    for a in p.rows:
        g = lambda k: (a.get(k) or "").strip()
        d = datetime.strptime(g("data-date"), "%d.%m.%Y").date()
        subj = re.sub(r"^[A-Za-zĀ-ž]+[A-Z]\d+[A-Z]?\s+", "", g("data-title"))
        room = "Online" if g("data-online") else lu_place(g("data-room-building"), g("data-room"))
        state = g("data-state") or "Live"
        out.append({
            "src": "LU", "subject": subj, "type": g("data-event-type").split("|")[-1].strip(),
            "lecturer": g("data-staff"),
            "date": d.isoformat(), "start": g("data-time"), "end": g("data-time2"),
            "room": room, "status": 1 if state == "Live" else 3, "state": state,
        })
    out.sort(key=lambda x: (x["subject"], x["date"], x["start"]))
    seen = {}
    for x in out:
        k = (x["subject"], x["date"])
        seen[k] = seen.get(k, 0) + 1
        x["id"] = "lu|%s|%s|%d" % (x["subject"], x["date"], seen[k])
    return out


# ---------- diff ----------
FIELDS = [("start", "Time"), ("end", "Time"), ("room", "Room"), ("status", "Status"), ("type", "Type")]


def fmt_day(iso):
    d = date.fromisoformat(iso)
    return d.strftime("%a ") + str(d.day) + d.strftime(" %b")


def label(x):
    t = (" (" + x["type"].rstrip(".") + ")") if x.get("type") else ""
    return "**%s · %s** · %s%s" % (x["src"], fmt_day(x["date"]), x["subject"], t)


def status_name(s):
    return "normal" if s == 1 else "changed"


def diff(old, new, today):
    """Return human lines for upcoming changes only (today and later)."""
    lines = []
    o = {x["id"]: x for x in old}
    n = {x["id"]: x for x in new}
    for k in sorted(set(o) | set(n), key=lambda k: ((n.get(k) or o.get(k))["date"], (n.get(k) or o.get(k))["start"])):
        a, b = o.get(k), n.get(k)
        ref = b or a
        if ref["date"] < today and (a is None or a["date"] < today):
            continue
        if a and not b:
            lines.append("❌ Removed: %s, %s–%s" % (label(a), a["start"], a["end"]))
        elif b and not a:
            lines.append("➕ New: %s, %s–%s, %s" % (label(b), b["start"], b["end"], b["room"]))
        else:
            ch = []
            if a["date"] != b["date"]:
                ch.append("Date: %s → %s" % (fmt_day(a["date"]), fmt_day(b["date"])))
            if (a["start"], a["end"]) != (b["start"], b["end"]):
                ch.append("Time: %s–%s → %s–%s" % (a["start"], a["end"], b["start"], b["end"]))
            if a["room"] != b["room"]:
                ch.append("Room: %s → %s" % (a["room"], b["room"]))
            if a["status"] != b["status"]:
                ch.append("Marked as %s" % status_name(b["status"]))
            if a["subject"] != b["subject"]:
                ch.append("Subject: %s → %s" % (a["subject"], b["subject"]))
            if ch:
                lines.append("✏️ %s\n   %s" % (label(b), "\n   ".join(ch)))
    return lines


def diff_lu(old, new, today):
    """LU has no IDs, so compare per subject+day: drop exact matches, pair up what's left."""
    def groups(evs):
        g = {}
        for x in evs:
            if x["date"] >= today:
                g.setdefault((x["subject"], x["date"]), []).append(x)
        return g
    go, gn = groups(old), groups(new)
    lines = []
    for k in sorted(set(go) | set(gn), key=lambda k: (k[1], k[0])):
        a = sorted(go.get(k, []), key=lambda x: x["start"])
        b = sorted(gn.get(k, []), key=lambda x: x["start"])
        sig = lambda x: (x["start"], x["end"], x["room"], x["type"], x.get("status", 1))
        bs = [sig(x) for x in b]
        left_a = []
        for x in a:
            if sig(x) in bs: bs.remove(sig(x))
            else: left_a.append(x)
        asig = [sig(x) for x in a]
        left_b = []
        for x in b:
            if sig(x) in asig: asig.remove(sig(x))
            else: left_b.append(x)
        for x, y in zip(left_a, left_b):
            ch = []
            if (x["start"], x["end"]) != (y["start"], y["end"]):
                ch.append("Time: %s–%s → %s–%s" % (x["start"], x["end"], y["start"], y["end"]))
            if x["room"] != y["room"]:
                ch.append("Room: %s → %s" % (x["room"], y["room"]))
            if x["type"] != y["type"]:
                ch.append("Type: %s → %s" % (x["type"], y["type"]))
            if x.get("status", 1) != y.get("status", 1):
                ch.append("Status: %s" % (y.get("state") or status_name(y.get("status", 1))))
            lines.append("✏️ %s\n   %s" % (label(y), "\n   ".join(ch)))
        for x in left_a[len(left_b):]:
            lines.append("❌ Removed: %s, %s–%s" % (label(x), x["start"], x["end"]))
        for y in left_b[len(left_a):]:
            lines.append("➕ New: %s, %s–%s, %s" % (label(y), y["start"], y["end"], y["room"]))
    return lines


# ---------- discord ----------
def post(text):
    if not WEBHOOK:
        print("[no DISCORD_WEBHOOK set] would post:\n" + text)
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        req = urllib.request.Request(WEBHOOK, data=json.dumps({"content": c}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "timetable-bot"})
        urllib.request.urlopen(req, timeout=30).read()


# ---------- state ----------
def load_state():
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_state(state):
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def sort_events(evs):
    return sorted(evs, key=lambda x: (x["date"], x["start"], x["src"]))


# ---------- commands ----------
def check(fetch=fetch_rtu_month, now=None, fetch_lu=fetch_lu_html):
    now = now or datetime.now(TZ)
    today = now.date().isoformat()
    prev = load_state()
    first_run = prev is None
    prev = prev or {"events": [], "meta": {}}
    meta = prev.get("meta", {})
    old_rtu = [x for x in prev["events"] if x["src"] == "RTU"]
    old_lu = [x for x in prev["events"] if x["src"] == "LU"]

    # RTU: month by month; if a month fails, keep last known data for that month
    rtu, failed = [], []
    for (y, m) in SEMESTER_MONTHS:
        try:
            rtu += [norm_rtu(e) for e in fetch(y, m)]
        except Exception as e:  # network, block, bad JSON
            failed.append("%d-%02d" % (y, m))
            print("RTU %d-%02d failed: %s" % (y, m, e))
            rtu += [x for x in old_rtu if x["date"].startswith("%d-%02d" % (y, m))]
    rtu_lines = diff([x for x in old_rtu if x["date"][:7] not in failed], [x for x in rtu if x["date"][:7] not in failed], today)

    # LU: public group page; if it can't be read, keep the last known LU data
    lu_failed = False
    try:
        lu = norm_lu_html(fetch_lu())
    except Exception as e:
        print("LU failed: %s" % e)
        lu_failed, lu = True, old_lu
    if lu_failed:
        lu_lines = []
    elif old_lu:
        lu_lines = diff_lu(old_lu, lu, today)
    else:
        lu_lines = [] if first_run else ["📥 LU timetable added: %d sessions." % len(lu)]

    msgs = []
    if first_run:
        msgs.append("✅ **Timetable tracking is on.** Watching %d RTU and %d LU sessions. "
                    "You'll get a message here when something changes, and every evening at 20:00 for tomorrow."
                    % (len(rtu), len(lu)))
    else:
        if rtu_lines or lu_lines:
            msgs.append("📅 **Timetable changes**\n" + "\n".join(rtu_lines + lu_lines))
        if failed and not meta.get("rtu_failed"):
            msgs.append("⚠️ Couldn't reach the RTU timetable (%s). Showing the last known data; I'll keep trying."
                        % ", ".join(failed))
        if not failed and meta.get("rtu_failed"):
            msgs.append("✅ RTU timetable is reachable again.")
        if lu_failed and not meta.get("lu_failed"):
            msgs.append("⚠️ Couldn't read the LU timetable page. Showing the last known data; I'll keep trying.")
        if not lu_failed and meta.get("lu_failed"):
            msgs.append("✅ LU timetable is reachable again.")

    meta.pop("lu_file", None)
    meta.update({"rtu_failed": bool(failed), "lu_failed": lu_failed,
                 "last_check": now.isoformat(timespec="minutes")})
    save_state({"events": sort_events(rtu + lu), "meta": meta})
    for m in msgs:
        post(m)
    return msgs


def evening(now=None):
    """Announce the next day's first session. Runs whenever an evening attempt fires:
    noon-midnight -> tomorrow; midnight-noon (GitHub ran late) -> today, sessions not yet started.
    Each day is announced at most once."""
    now = now or datetime.now(TZ)
    state = load_state()
    if not state:
        print("no data yet"); return []
    meta = state.setdefault("meta", {})
    if now.hour >= 12:
        target, word, icon = (now.date() + timedelta(days=1)).isoformat(), "Tomorrow", "🌙"
    else:
        target, word, icon = now.date().isoformat(), "Today", "☀️"
    if meta.get("announced") == target:
        print("%s already announced" % target); return []
    evs = [x for x in state["events"] if x["date"] == target]
    if word == "Today":
        evs = [x for x in evs if x["start"] > now.strftime("%H:%M")]
    msgs = []
    if evs:
        f = sorted(evs, key=lambda x: x["start"])[0]
        t = (" (" + f["type"].rstrip(".") + ")") if f["type"] else ""
        flag = (" · ⚠️ %s" % (f.get("state") if f.get("state") not in (None, "Live") else "marked as changed")) if f["status"] != 1 else ""
        exam = "📝 " if "exam" in f["type"].lower() else ""
        msgs.append("%s **%s, %s:** first up at **%s** — %s%s%s at %s, %s%s"
                    % (icon, word, fmt_day(target), f["start"], exam, f["subject"], t, f["src"], f["room"], flag))
    elif word == "Tomorrow":
        msgs.append("%s **%s, %s:** no lectures 🎉" % (icon, word, fmt_day(target)))
    else:
        had = any(x["date"] == target for x in state["events"])
        msgs.append("%s **%s, %s:** %s" % (icon, word, fmt_day(target), "no more lectures today" if had else "no lectures 🎉"))
    meta["announced"] = target
    meta.pop("evening_sent", None)
    save_state(state)
    for m in msgs:
        post(m)
    return msgs


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": check, "evening": evening}[cmd]()

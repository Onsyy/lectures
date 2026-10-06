"""
Lecture timetable: merges RTU (fetched) + LU (uploaded Excel export),
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
EVENING_FROM = (19, 45)                    # send the "tomorrow" message from 19:45 Riga time...
EVENING_UNTIL = (23, 59)                   # ...until midnight, once per day (target: 20:00)
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
def lu_file():
    files = glob.glob(os.path.join(ROOT, "lu", "*.xlsx"))
    if not files:
        return None
    # prefer the newest export timestamp in the name (…-202610061408.xlsx), else the last name
    def key(p):
        m = re.search(r"(\d{12})", os.path.basename(p))
        return (m.group(1) if m else "0", os.path.basename(p))
    return max(files, key=key)


def norm_lu_rows(rows):
    out = []
    header = [str(h or "").strip() for h in rows[0]]
    col = {h: i for i, h in enumerate(header)}
    need = ["Summary", "Type", "Date", "Start Time", "End Time", "Building", "Room"]
    missing = [n for n in need if n not in col]
    if missing:
        raise ValueError("LU export is missing columns: %s" % ", ".join(missing))
    for r in rows[1:]:
        g = lambda n: ("" if r[col[n]] is None else str(r[col[n]])).strip()
        typ = g("Type")
        if not typ or typ == "Holiday" or not g("Start Time"):
            continue
        d = datetime.strptime(g("Date"), "%d/%m/%Y").date()
        subj = re.sub(r"^[A-Za-zĀ-ž]+[A-Z]\d+[A-Z]?\s+", "", g("Summary"))  # drop course code
        typ = typ.split("|")[-1].strip()
        building, room = g("Building"), g("Room")
        room_short = re.split(r"\s+FT\.", room)[0].strip() if room else ""
        place = "Room TBA" if not (building or room) else (room_short + (" · " + building.replace("Kr. ", "") if building else ""))
        out.append({
            "src": "LU", "subject": subj, "type": typ, "lecturer": "",
            "date": d.isoformat(), "start": g("Start Time"), "end": g("End Time"),
            "room": place, "status": 1,
        })
    # LU has no IDs: number sessions of the same subject on the same day in time order
    out.sort(key=lambda x: (x["subject"], x["date"], x["start"]))
    seen = {}
    for x in out:
        k = (x["subject"], x["date"])
        seen[k] = seen.get(k, 0) + 1
        x["id"] = "lu|%s|%s|%d" % (x["subject"], x["date"], seen[k])
    return out


def load_lu():
    path = lu_file()
    if not path:
        return None, None
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    return norm_lu_rows(rows), os.path.basename(path)


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
        sig = lambda x: (x["start"], x["end"], x["room"], x["type"])
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
def check(fetch=fetch_rtu_month, now=None):
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

    # LU: only compare when the uploaded file actually changed
    lu, lu_name = load_lu()
    lu_lines = []
    if lu is None:
        lu = old_lu
    elif old_lu:
        lu_lines = diff_lu(old_lu, lu, today)
    elif not first_run:
        lu_lines = ["📥 LU timetable added: %d sessions." % len(lu)]

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

    meta.update({"rtu_failed": bool(failed), "lu_file": lu_name or meta.get("lu_file"),
                 "last_check": now.isoformat(timespec="minutes")})
    save_state({"events": sort_events(rtu + lu), "meta": meta})
    for m in msgs:
        post(m)
    return msgs


def evening(now=None):
    now = now or datetime.now(TZ)
    state = load_state()
    if not state:
        print("no data yet"); return []
    meta = state.setdefault("meta", {})
    today = now.date().isoformat()
    if not (EVENING_FROM <= (now.hour, now.minute) <= EVENING_UNTIL):
        print("not evening yet in Riga (%s)" % now.strftime("%H:%M")); return []
    if meta.get("evening_sent") == today:
        print("already sent today"); return []
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    evs = [x for x in state["events"] if x["date"] == tomorrow]
    msgs = []
    if evs:
        f = sorted(evs, key=lambda x: x["start"])[0]
        t = (" (" + f["type"].rstrip(".") + ")") if f["type"] else ""
        flag = " · ⚠️ marked as changed" if f["status"] != 1 else ""
        exam = "📝 " if "exam" in f["type"].lower() else ""
        msgs.append("🌙 **Tomorrow, %s:** first up at **%s** — %s%s%s at %s, %s%s"
                    % (fmt_day(tomorrow), f["start"], exam, f["subject"], t, f["src"], f["room"], flag))
    meta["evening_sent"] = today
    save_state(state)
    for m in msgs:
        post(m)
    return msgs


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": check, "evening": evening}[cmd]()

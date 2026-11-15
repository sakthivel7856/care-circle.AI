#!/usr/bin/env python3
"""
CareCircle - a shared, passcode-based healthcare ecosystem for elderly care.

Zero third-party dependencies: Python 3.8+ standard library only.

Run:   python3 server.py            (then open http://localhost:8000)
Reset: python3 server.py --reset    (wipe the DB and re-seed demo data)

Email (optional). Without SMTP settings, every email is still generated and shown
in the in-app "Emails" log (dry-run), which is perfect for demos:
    SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com \
    SMTP_PASS=<app-password> SMTP_FROM="CareCircle <you@gmail.com>" python3 server.py
"""
import json, os, re, random, secrets, smtplib, sqlite3, sys, threading, time, traceback
from datetime import datetime, date, timedelta, time as dtime
from email.message import EmailMessage
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CARE_DB", os.path.join(BASE, "carecircle.db"))
PORT = int(os.environ.get("PORT", "8000"))
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "carecircle@localhost")

UPCOMING_MIN = int(os.environ.get("UPCOMING_MIN", "10"))   # heads-up before a dose
GRACE_MIN = int(os.environ.get("GRACE_MIN", "30"))         # missed -> elder + caretakers
ESCALATE_MIN = int(os.environ.get("ESCALATE_MIN", "60"))   # still missed -> family too
TICK_SECONDS = 30

# ----------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY, name TEXT, age INTEGER, conditions TEXT,
  allergies TEXT, doctor TEXT, blood_group TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY, profile_id INTEGER, name TEXT, email TEXT,
  role TEXT, relation TEXT, passcode TEXT UNIQUE, can_edit INTEGER DEFAULT 0, notify INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, member_id INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS meds(id INTEGER PRIMARY KEY, profile_id INTEGER, name TEXT, dosage TEXT,
  instructions TEXT, times TEXT, days TEXT, start_date TEXT, end_date TEXT, pills_left INTEGER,
  refill_at INTEGER DEFAULT 5, color TEXT, notes TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS dose_logs(id INTEGER PRIMARY KEY, med_id INTEGER, profile_id INTEGER, date TEXT,
  time TEXT, status TEXT, taken_at TEXT, by_member INTEGER, UNIQUE(med_id, date, time));
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, profile_id INTEGER, title TEXT, doctor TEXT,
  location TEXT, when_at TEXT, notes TEXT, questions TEXT, status TEXT DEFAULT 'scheduled');
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, profile_id INTEGER, member_name TEXT, text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, profile_id INTEGER, to_name TEXT, to_email TEXT,
  subject TEXT, body TEXT, kind TEXT, ts TEXT, status TEXT, dedupe TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS checkins(id INTEGER PRIMARY KEY, profile_id INTEGER, date TEXT, mood INTEGER,
  note TEXT, by_member TEXT, UNIQUE(profile_id, date));
"""

def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def rows(sql, args=()):
    c = conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()

def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None

def ex(sql, args=()):
    c = conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()

def init_db():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()
    c.close()

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def fmt12(t):
    h, m = map(int, t.split(":"))
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")

# ------------------------------------------------------------------- email
def deliver(row_id, to, name, subject, body):
    status = "logged (SMTP not configured - dry run)"
    if SMTP_HOST:
        if to.lower().endswith("@example.com"):
            status = "skipped (demo address)"
        else:
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, SMTP_FROM, to
                msg.set_content(body)
                html = "<div style='font-family:Arial,sans-serif;max-width:520px;margin:auto;padding:20px;border:1px solid #e5e5e5;border-radius:12px'>" \
                       + "<h2 style='color:#1f7a6d;margin-top:0'>CareCircle</h2>" \
                       + "".join("<p>%s</p>" % l for l in body.split("\n\n")).replace("\n", "<br>") + "</div>"
                msg.add_alternative(html, subtype="html")
                if SMTP_PORT == 465:
                    s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
                else:
                    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
                    s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
                s.quit()
                status = "sent"
            except Exception as e:  # noqa
                status = "failed: %s" % str(e)[:120]
    ex("UPDATE outbox SET status=? WHERE id=?", (status, row_id))

def mail(pid, recipients, subject, body, kind, key=None):
    """Queue + deliver an email to each recipient. `key` de-duplicates (one email per key per person)."""
    prof = one("SELECT * FROM profiles WHERE id=?", (pid,))
    sent = 0
    for m in recipients:
        if not m.get("email") or not m.get("notify"):
            continue
        full = "Hi %s,\n\n%s\n\n- CareCircle, caring together for %s" % (m["name"].split()[0], body, prof["name"])
        try:
            rid = ex("INSERT INTO outbox(profile_id,to_name,to_email,subject,body,kind,ts,status,dedupe) VALUES(?,?,?,?,?,?,?,?,?)",
                     (pid, m["name"], m["email"], subject, full, kind, now_str(), "queued", ("%s:%s" % (key, m["id"])) if key else None))
        except sqlite3.IntegrityError:
            continue
        deliver(rid, m["email"], m["name"], subject, full)
        sent += 1
    return sent

def circle(pid, *roles):
    if not roles:
        return rows("SELECT * FROM members WHERE profile_id=?", (pid,))
    q = ",".join("?" * len(roles))
    return rows("SELECT * FROM members WHERE profile_id=? AND role IN (%s)" % q, (pid,) + roles)

def record(pid, who, text):
    ex("INSERT INTO activity(profile_id,member_name,text,ts) VALUES(?,?,?,?)", (pid, who, text, now_str()))

# ------------------------------------------------------------- dose engine
def med_out(m):
    m = dict(m)
    m["times"] = json.loads(m["times"] or "[]")
    m["days"] = json.loads(m["days"] or "[]")
    per_day = max(1, len(m["times"]))
    m["days_left"] = (m["pills_left"] // per_day) if m["pills_left"] is not None else None
    return m

def doses_for(pid, day, now=None):
    now = now or datetime.now()
    ds = day.isoformat()
    names = {m["id"]: m["name"] for m in circle(pid)}
    logs = {(l["med_id"], l["time"]): l for l in rows("SELECT * FROM dose_logs WHERE profile_id=? AND date=?", (pid, ds))}
    out = []
    for m in rows("SELECT * FROM meds WHERE profile_id=? AND active=1", (pid,)):
        if m["start_date"] and ds < m["start_date"]:
            continue
        if m["end_date"] and ds > m["end_date"]:
            continue
        days = json.loads(m["days"] or "[]")
        if days and day.weekday() not in days:
            continue
        for t in sorted(json.loads(m["times"] or "[]")):
            h, mi = map(int, t.split(":"))
            sched = datetime.combine(day, dtime(h, mi))
            lg = logs.get((m["id"], t))
            mins = (now - sched).total_seconds() / 60
            late = False
            if lg:
                status = lg["status"]
                if status == "taken" and lg["taken_at"]:
                    late = (datetime.fromisoformat(lg["taken_at"].replace(" ", "T")) - sched).total_seconds() / 60 > GRACE_MIN
            elif mins >= GRACE_MIN:
                status = "missed"
            elif mins >= -UPCOMING_MIN:
                status = "due"
            else:
                status = "pending"
            out.append(dict(med_id=m["id"], name=m["name"], dosage=m["dosage"], instructions=m["instructions"],
                            color=m["color"], time=t, sched=sched.isoformat(), status=status, logged=bool(lg),
                            taken_at=lg["taken_at"] if lg else None, late=late,
                            by=names.get(lg["by_member"]) if lg and lg["by_member"] else None))
    out.sort(key=lambda d: (d["time"], d["name"]))
    return out

def low_pill_alerts(pid):
    alerts = []
    for m in map(med_out, rows("SELECT * FROM meds WHERE profile_id=? AND active=1", (pid,))):
        if m["pills_left"] is not None and m["pills_left"] <= (m["refill_at"] or 0):
            alerts.append("%s is running low: %d pills left (about %d days). Time to refill." % (m["name"], m["pills_left"], m["days_left"]))
    return alerts

def compute_insights(pid, days=30):
    today, now = date.today(), datetime.now()
    heat, per_med = [], {}
    slots = {k: [0, 0] for k in ("Morning", "Afternoon", "Evening", "Night")}
    last7, prev7 = [0, 0], [0, 0]
    late_count = 0
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        res = [x for x in doses_for(pid, d, now) if x["status"] in ("taken", "skipped", "missed")]
        tk = sum(1 for x in res if x["status"] == "taken")
        heat.append(dict(date=d.isoformat(), taken=tk, total=len(res), pct=round(100 * tk / len(res)) if res else None))
        for x in res:
            pm = per_med.setdefault(x["name"], [0, 0, x["color"]])
            pm[1] += 1
            hr = int(x["time"][:2])
            slot = "Morning" if hr < 12 else "Afternoon" if hr < 17 else "Evening" if hr < 21 else "Night"
            slots[slot][1] += 1
            if x["status"] == "taken":
                pm[0] += 1
                slots[slot][0] += 1
                late_count += 1 if x["late"] else 0
        if i < 7:
            last7[0] += tk; last7[1] += len(res)
        elif i < 14:
            prev7[0] += tk; prev7[1] += len(res)
    streak = 0
    for h in reversed(heat):
        if h["total"] == 0:
            continue
        if h["taken"] == h["total"]:
            streak += 1
        else:
            break
    pct = lambda a: round(100 * a[0] / a[1]) if a[1] else None
    tot = [sum(h["taken"] for h in heat), sum(h["total"] for h in heat)]
    tips = []
    worst = sorted([(1 - v[0] / v[1], k) for k, v in slots.items() if v[1] >= 3])
    if worst and worst[-1][0] > 0.1:
        tips.append("%s doses are missed most often (%d%% missed). Consider a louder reminder or moving that dose to a routine anchor such as a meal." % (worst[-1][1], round(worst[-1][0] * 100)))
    if pct(last7) is not None and pct(prev7) is not None:
        diff = pct(last7) - pct(prev7)
        if diff >= 5:
            tips.append("Great progress! Adherence is up %d points compared with the previous week." % diff)
        elif diff <= -5:
            tips.append("Adherence dropped %d points versus the previous week. A quick check-in call may help." % abs(diff))
    if late_count >= 3:
        tips.append("%d doses were taken more than %d minutes late recently. Shifting times closer to the actual routine may help." % (late_count, GRACE_MIN))
    tips += low_pill_alerts(pid)
    if streak >= 3:
        tips.append("%d perfect days in a row. Celebrate it together!" % streak)
    moods = rows("SELECT date, mood FROM checkins WHERE profile_id=? ORDER BY date DESC LIMIT 14", (pid,))[::-1]
    return dict(adherence7=pct(last7), adherence30=pct(tot), streak=streak, heat=heat, late=late_count,
                per_med=[dict(name=k, pct=round(100 * v[0] / v[1]), taken=v[0], total=v[1], color=v[2]) for k, v in per_med.items()],
                slots=[dict(slot=k, pct=round(100 * v[0] / v[1]) if v[1] else None, total=v[1]) for k, v in slots.items()],
                tips=tips, moods=moods)

# --------------------------------------------------------------- scheduler
def tick():
    """Runs every 30s. Sends upcoming / missed / escalated dose emails + appointment reminders."""
    now = datetime.now()
    today = now.date()
    before = one("SELECT COUNT(*) c FROM outbox")["c"]
    for p in rows("SELECT * FROM profiles"):
        pid = p["id"]
        el, ct, fam = circle(pid, "elder"), circle(pid, "caretaker"), circle(pid, "family")
        for d in doses_for(pid, today, now):
            if d["logged"]:
                continue
            mins = (now - datetime.fromisoformat(d["sched"])).total_seconds() / 60
            if mins > 360:
                continue
            tag = "%s:%s:%s" % (d["med_id"], today, d["time"])
            label = "%s %s at %s" % (d["name"], d["dosage"] or "", fmt12(d["time"]))
            how = (" (%s)" % d["instructions"]) if d["instructions"] else ""
            if -UPCOMING_MIN <= mins < 0:
                mail(pid, el, "Upcoming dose: %s" % d["name"], "A gentle reminder: %s is due soon%s.\nOpen CareCircle and tap 'Taken' once you have it." % (label, how), "upcoming", "up:" + tag)
            if mins >= GRACE_MIN:
                mail(pid, el + ct, "Missed dose: %s" % d["name"],
                     "%s has not been marked as taken yet (%d minutes ago).\n\nPlease check in with %s and record the dose, or mark it skipped if there is a reason." % (label, int(mins), p["name"]), "missed", "miss1:" + tag)
            if mins >= ESCALATE_MIN:
                mail(pid, fam, "Attention: %s missed %s" % (p["name"], d["name"]),
                     "%s's dose of %s is still not recorded after %d minutes. The elder and caretakers have already been notified.\n\nA quick call from the family could really help." % (p["name"], label, int(mins)), "escalation", "miss2:" + tag)
        for a in rows("SELECT * FROM appointments WHERE profile_id=? AND status='scheduled'", (pid,)):
            when = datetime.fromisoformat(a["when_at"])
            hrs = (when - now).total_seconds() / 3600
            if hrs <= 0:
                continue
            detail = "%s%s%s" % (a["title"], (" with " + a["doctor"]) if a["doctor"] else "", (" at " + a["location"]) if a["location"] else "")
            q = ("\n\nQuestions to ask:\n- " + "\n- ".join([x for x in (a["questions"] or "").split("\n") if x.strip()])) if (a["questions"] or "").strip() else ""
            if hrs <= 24:
                mail(pid, circle(pid), "Appointment tomorrow: %s" % a["title"], "Reminder: %s on %s at %s.%s" % (detail, when.strftime("%A, %d %b"), when.strftime("%I:%M %p"), q), "appointment", "apt24:%s" % a["id"])
            if hrs <= 2:
                mail(pid, circle(pid), "Appointment in 2 hours: %s" % a["title"], "Heads up: %s starts at %s. Leave early and carry prescriptions and reports." % (detail, when.strftime("%I:%M %p")), "appointment", "apt2:%s" % a["id"])
    return one("SELECT COUNT(*) c FROM outbox")["c"] - before

def scheduler_loop():
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(TICK_SECONDS)

# ------------------------------------------------------------------- seed
def seed_demo():
    rng = random.Random(7)
    now = datetime.now()
    pid = ex("INSERT INTO profiles(name,age,conditions,allergies,doctor,blood_group,created) VALUES(?,?,?,?,?,?,?)",
             ("Lakshmi Devi", 72, "Type 2 diabetes, hypertension, glaucoma", "Penicillin", "Dr. Ramesh Kumar (Salem Heart & Diabetes Clinic)", "B+", now_str()))
    for name, email, role, rel, code, edit in [
        ("Lakshmi Devi", "lakshmi@example.com", "elder", "Self", "1111", 1),
        ("Anitha S", "anitha.nurse@example.com", "caretaker", "Home nurse", "2222", 1),
        ("Rahul Kumar", "rahul@example.com", "family", "Son", "3333", 0),
        ("Priya Kumar", "priya@example.com", "family", "Daughter", "4444", 0)]:
        ex("INSERT INTO members(profile_id,name,email,role,relation,passcode,can_edit,notify) VALUES(?,?,?,?,?,?,?,1)", (pid, name, email, role, rel, code, edit))
    start = (date.today() - timedelta(days=14)).isoformat()
    t_missed = (now - timedelta(minutes=75)).strftime("%H:%M")
    t_soon = (now + timedelta(minutes=7)).strftime("%H:%M")
    if (now - timedelta(minutes=75)).date() != now.date() or (now + timedelta(minutes=7)).date() != now.date():
        t_missed, t_soon = "09:00", "18:00"
    meds = [
        ("Metformin", "500 mg", "After food", ["08:00", "20:00"], [], 24, 6, "#1f7a6d", "For blood sugar. Do not skip meals."),
        ("Amlodipine", "5 mg", "Morning, with water", ["09:00"], [], 6, 7, "#e8833a", "Blood pressure tablet."),
        ("Atorvastatin", "20 mg", "After dinner", ["21:30"], [], 30, 7, "#7b5ea7", "Cholesterol."),
        ("Vitamin D3", "60000 IU", "Once a week, with milk", ["10:00"], [date.today().weekday()], 8, 2, "#d9a400", "Weekly sachet."),
        ("Timolol eye drops", "1 drop each eye", "Wait 5 min between drops", [t_missed, t_soon], [], 40, 8, "#3b82c4", "For glaucoma."),
    ]
    for n, dose, ins, times, days, pills, refill, color, notes in meds:
        ex("INSERT INTO meds(profile_id,name,dosage,instructions,times,days,start_date,pills_left,refill_at,color,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (pid, n, dose, ins, json.dumps(times), json.dumps(days), start, pills, refill, color, notes))
    # 14 days of realistic history (evenings are missed a bit more often)
    el_id = one("SELECT id FROM members WHERE role='elder'")["id"]
    for i in range(14, 0, -1):
        d = date.today() - timedelta(days=i)
        for x in doses_for(pid, d, now):
            hr = int(x["time"][:2])
            r = rng.random()
            if r < (0.05 if hr < 12 else 0.12 if hr < 17 else 0.22):
                continue  # missed
            sched = datetime.fromisoformat(x["sched"])
            if r > 0.96:
                ex("INSERT OR IGNORE INTO dose_logs(med_id,profile_id,date,time,status,by_member) VALUES(?,?,?,?,?,?)", (x["med_id"], pid, d.isoformat(), x["time"], "skipped", el_id))
                continue
            at = sched + timedelta(minutes=rng.choice([1, 2, 4, 6, 9, 14, 22, 45]))
            ex("INSERT OR IGNORE INTO dose_logs(med_id,profile_id,date,time,status,taken_at,by_member) VALUES(?,?,?,?,?,?,?)",
               (x["med_id"], pid, d.isoformat(), x["time"], "taken", at.strftime("%Y-%m-%d %H:%M:%S"), el_id))
        ex("INSERT OR IGNORE INTO checkins(profile_id,date,mood,note,by_member) VALUES(?,?,?,?,?)", (pid, d.isoformat(), rng.choice([3, 4, 4, 5, 2, 4]), "", "Lakshmi Devi"))
    for x in doses_for(pid, date.today(), now):  # earlier doses today were taken, except the demo eye-drop dose
        if x["name"].startswith("Timolol"):
            continue
        if x["status"] == "missed":
            sched = datetime.fromisoformat(x["sched"])
            ex("INSERT OR IGNORE INTO dose_logs(med_id,profile_id,date,time,status,taken_at,by_member) VALUES(?,?,?,?,?,?,?)",
               (x["med_id"], pid, date.today().isoformat(), x["time"], "taken", (sched + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), el_id))
    appts = [
        ("Cardiology follow-up", "Dr. Ramesh Kumar", "Salem Heart & Diabetes Clinic", (now + timedelta(hours=20)).replace(second=0, microsecond=0), "Bring last 3 BP readings.", "Is my BP target still right?\nCan we reduce the number of tablets?", "scheduled"),
        ("Eye pressure check", "Dr. Meena Iyer", "Vasan Eye Care, Salem", (now + timedelta(days=9)).replace(hour=11, minute=30, second=0, microsecond=0), "", "Are the eye drops working?", "scheduled"),
        ("HbA1c blood test", "Lab", "Apollo Diagnostics", (now - timedelta(days=6)).replace(hour=8, minute=0, second=0, microsecond=0), "Fasting test. Result 6.9%.", "", "done"),
    ]
    for t, d, loc, when, notes, qs, st in appts:
        ex("INSERT INTO appointments(profile_id,title,doctor,location,when_at,notes,questions,status) VALUES(?,?,?,?,?,?,?,?)",
           (pid, t, d, loc, when.strftime("%Y-%m-%dT%H:%M"), notes, qs, st))
    record(pid, "Anitha S", "added Atorvastatin 20 mg to the schedule")
    record(pid, "Rahul Kumar", "added the Cardiology follow-up appointment")
    print("\nDemo passcodes -> Elder: 1111 | Caretaker: 2222 | Family: 3333 / 4444\n")

# -------------------------------------------------------------------- API
class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg

class Ctx:
    def __init__(self, m, body, q):
        self.m, self.body, self.q = m, body, q
        self.pid = m["profile_id"] if m else None

ROUTES = []
def route(method, pattern, public=False):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, public))
        return fn
    return deco

def need(cond, msg="Not allowed", code=403):
    if not cond:
        raise ApiError(code, msg)

def can_edit(m):
    return m["role"] == "elder" or bool(m["can_edit"])

def public_member(m, viewer):
    d = {k: m[k] for k in ("id", "name", "email", "role", "relation", "can_edit", "notify")}
    if viewer["role"] == "elder" or viewer["id"] == m["id"]:
        d["passcode"] = m["passcode"]
    return d

def new_session(member_id):
    tok = secrets.token_urlsafe(24)
    ex("INSERT INTO sessions(token,member_id,created) VALUES(?,?,?)", (tok, member_id, now_str()))
    return tok

def elder_of(pid):
    return circle(pid, "elder")

def tell_elder_if_other(ctx, text):
    """Transparency: whenever someone other than the elder changes something, the elder is emailed."""
    record(ctx.pid, ctx.m["name"], text)
    if ctx.m["role"] != "elder":
        mail(ctx.pid, elder_of(ctx.pid), "%s updated your care plan" % ctx.m["name"].split()[0], "%s %s.\n\nYou can review every change in the Activity tab, and you control who is allowed to edit in the Circle tab." % (ctx.m["name"], text), "change")

@route("POST", "/api/login", public=True)
def login(ctx):
    code = str(ctx.body.get("passcode", "")).strip()
    m = one("SELECT * FROM members WHERE passcode=?", (code,)) if code else None
    if not m:
        time.sleep(0.4)
        raise ApiError(401, "That passcode did not match anyone in a care circle.")
    return dict(token=new_session(m["id"]))

@route("POST", "/api/setup", public=True)
def setup(ctx):
    b = ctx.body
    name, code = (b.get("name") or "").strip(), str(b.get("passcode") or "").strip()
    need(name, "Please enter the elder's name", 400)
    need(re.fullmatch(r"\d{4,8}", code), "Passcode must be 4 to 8 digits", 400)
    need(not one("SELECT 1 FROM members WHERE passcode=?", (code,)), "That passcode is already taken", 400)
    pid = ex("INSERT INTO profiles(name,age,conditions,allergies,doctor,blood_group,created) VALUES(?,?,?,?,?,?,?)",
             (name, b.get("age") or None, b.get("conditions", ""), b.get("allergies", ""), b.get("doctor", ""), b.get("blood_group", ""), now_str()))
    mid = ex("INSERT INTO members(profile_id,name,email,role,relation,passcode,can_edit,notify) VALUES(?,?,?,?,?,?,1,1)",
             (pid, name, (b.get("email") or "").strip(), "elder", "Self", code))
    record(pid, name, "created this care circle")
    return dict(token=new_session(mid))

@route("POST", "/api/logout")
def logout(ctx):
    return dict(ok=True)

@route("GET", "/api/me")
def me(ctx):
    return dict(member=public_member(ctx.m, ctx.m), can_edit=can_edit(ctx.m), profile=one("SELECT * FROM profiles WHERE id=?", (ctx.pid,)))

@route("PUT", "/api/profile")
def update_profile(ctx):
    need(can_edit(ctx.m))
    b = ctx.body
    ex("UPDATE profiles SET age=?,conditions=?,allergies=?,doctor=?,blood_group=? WHERE id=?",
       (b.get("age") or None, b.get("conditions", ""), b.get("allergies", ""), b.get("doctor", ""), b.get("blood_group", ""), ctx.pid))
    tell_elder_if_other(ctx, "updated the health profile")
    return dict(ok=True)

@route("GET", "/api/config")
def config(ctx):
    return dict(grace=GRACE_MIN, escalate=ESCALATE_MIN, upcoming=UPCOMING_MIN, smtp=bool(SMTP_HOST))

@route("GET", "/api/today")
def today(ctx):
    now = datetime.now()
    ds = doses_for(ctx.pid, date.today(), now)
    resolved = [d for d in ds if d["status"] in ("taken", "skipped", "missed")]
    appts = [a for a in rows("SELECT * FROM appointments WHERE profile_id=? AND status='scheduled' ORDER BY when_at", (ctx.pid,)) if datetime.fromisoformat(a["when_at"]) >= now - timedelta(hours=1)]
    return dict(date=date.today().isoformat(), doses=ds,
                stats=dict(taken=sum(1 for d in ds if d["status"] == "taken"), total=len(ds), missed=sum(1 for d in ds if d["status"] == "missed"), resolved=len(resolved)),
                appointments=appts[:3], alerts=low_pill_alerts(ctx.pid),
                checkin=one("SELECT * FROM checkins WHERE profile_id=? AND date=?", (ctx.pid, date.today().isoformat())))

@route("POST", "/api/doses/log")
def log_dose(ctx):
    b = ctx.body
    day = date.fromisoformat(b.get("date") or date.today().isoformat())
    dose = next((d for d in doses_for(ctx.pid, day) if d["med_id"] == b.get("med_id") and d["time"] == b.get("time")), None)
    need(dose, "That dose is not on the schedule", 404)
    status = b.get("status")
    med = one("SELECT * FROM meds WHERE id=?", (dose["med_id"],))
    elder = elder_of(ctx.pid)[0]
    who = "" if ctx.m["role"] == "elder" else " for %s" % elder["name"].split()[0]
    label = "%s (%s)" % (dose["name"], fmt12(dose["time"]))
    prev = one("SELECT * FROM dose_logs WHERE med_id=? AND date=? AND time=?", (dose["med_id"], day.isoformat(), dose["time"]))
    if status in ("taken", "skipped"):
        ex("INSERT OR REPLACE INTO dose_logs(med_id,profile_id,date,time,status,taken_at,by_member) VALUES(?,?,?,?,?,?,?)",
           (dose["med_id"], ctx.pid, day.isoformat(), dose["time"], status, now_str() if status == "taken" else None, ctx.m["id"]))
        if status == "taken" and (not prev or prev["status"] != "taken") and med["pills_left"] is not None:
            left = max(0, med["pills_left"] - 1)
            ex("UPDATE meds SET pills_left=? WHERE id=?", (left, med["id"]))
            if left <= (med["refill_at"] or 0):
                mail(ctx.pid, circle(ctx.pid), "Refill needed: %s" % med["name"], "%s is running low with only %d pills left. Please arrange a refill soon." % (med["name"], left), "refill", "refill:%s:%s" % (med["id"], left))
        record(ctx.pid, ctx.m["name"], "marked %s as %s%s" % (label, status, who))
    elif status == "undo":
        if prev and prev["status"] == "taken" and med["pills_left"] is not None:
            ex("UPDATE meds SET pills_left=? WHERE id=?", (med["pills_left"] + 1, med["id"]))
        ex("DELETE FROM dose_logs WHERE med_id=? AND date=? AND time=?", (dose["med_id"], day.isoformat(), dose["time"]))
        record(ctx.pid, ctx.m["name"], "undid the log for %s" % label)
    else:
        raise ApiError(400, "Unknown status")
    return dict(ok=True)

def clean_med(b):
    name = (b.get("name") or "").strip()
    need(name, "Medicine name is required", 400)
    times = sorted({t for t in b.get("times", []) if re.fullmatch(r"\d{2}:\d{2}", t or "")})
    need(times, "Add at least one dose time", 400)
    days = sorted({int(d) for d in b.get("days", []) if str(d).isdigit() and 0 <= int(d) <= 6})
    if len(days) == 7:
        days = []
    pills = b.get("pills_left")
    return (name, (b.get("dosage") or "").strip(), (b.get("instructions") or "").strip(), json.dumps(times), json.dumps(days),
            (b.get("end_date") or None), int(pills) if str(pills or "").strip() != "" else None, int(b.get("refill_at") or 5),
            b.get("color") or "#1f7a6d", (b.get("notes") or "").strip())

@route("GET", "/api/meds")
def list_meds(ctx):
    return [med_out(m) for m in rows("SELECT * FROM meds WHERE profile_id=? AND active=1 ORDER BY name", (ctx.pid,))]

@route("POST", "/api/meds")
def add_med(ctx):
    need(can_edit(ctx.m))
    v = clean_med(ctx.body)
    mid = ex("INSERT INTO meds(profile_id,name,dosage,instructions,times,days,end_date,pills_left,refill_at,color,notes,start_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (ctx.pid,) + v + (date.today().isoformat(),))
    tell_elder_if_other(ctx, "added %s %s to the medication schedule" % (v[0], v[1]))
    return dict(id=mid)

@route("PUT", r"/api/meds/(\d+)")
def edit_med(ctx, mid):
    need(can_edit(ctx.m))
    need(one("SELECT 1 FROM meds WHERE id=? AND profile_id=?", (mid, ctx.pid)), "Not found", 404)
    v = clean_med(ctx.body)
    ex("UPDATE meds SET name=?,dosage=?,instructions=?,times=?,days=?,end_date=?,pills_left=?,refill_at=?,color=?,notes=? WHERE id=?", v + (mid,))
    tell_elder_if_other(ctx, "updated %s %s" % (v[0], v[1]))
    return dict(ok=True)

@route("DELETE", r"/api/meds/(\d+)")
def del_med(ctx, mid):
    need(can_edit(ctx.m))
    m = one("SELECT * FROM meds WHERE id=? AND profile_id=?", (mid, ctx.pid))
    need(m, "Not found", 404)
    ex("UPDATE meds SET active=0 WHERE id=?", (mid,))
    tell_elder_if_other(ctx, "removed %s from the schedule" % m["name"])
    return dict(ok=True)

def clean_appt(b):
    need((b.get("title") or "").strip(), "Give the appointment a title", 400)
    need(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", b.get("when_at") or ""), "Pick a date and time", 400)
    return ((b["title"]).strip(), (b.get("doctor") or "").strip(), (b.get("location") or "").strip(), b["when_at"], (b.get("notes") or "").strip(), (b.get("questions") or "").strip())

@route("GET", "/api/appointments")
def list_appts(ctx):
    return rows("SELECT * FROM appointments WHERE profile_id=? ORDER BY when_at", (ctx.pid,))

@route("POST", "/api/appointments")
def add_appt(ctx):
    need(can_edit(ctx.m))
    v = clean_appt(ctx.body)
    aid = ex("INSERT INTO appointments(profile_id,title,doctor,location,when_at,notes,questions) VALUES(?,?,?,?,?,?,?)", (ctx.pid,) + v)
    tell_elder_if_other(ctx, "scheduled the appointment '%s' on %s" % (v[0], v[3].replace("T", " ")))
    return dict(id=aid)

@route("PUT", r"/api/appointments/(\d+)")
def edit_appt(ctx, aid):
    a = one("SELECT * FROM appointments WHERE id=? AND profile_id=?", (aid, ctx.pid))
    need(a, "Not found", 404)
    if "status" in ctx.body and len(ctx.body) == 1:  # marking done / cancelled
        need(can_edit(ctx.m))
        ex("UPDATE appointments SET status=? WHERE id=?", (ctx.body["status"], aid))
        tell_elder_if_other(ctx, "marked '%s' as %s" % (a["title"], ctx.body["status"]))
        return dict(ok=True)
    need(can_edit(ctx.m))
    v = clean_appt(ctx.body)
    ex("UPDATE appointments SET title=?,doctor=?,location=?,when_at=?,notes=?,questions=? WHERE id=?", v + (aid,))
    tell_elder_if_other(ctx, "updated the appointment '%s'" % v[0])
    return dict(ok=True)

@route("DELETE", r"/api/appointments/(\d+)")
def del_appt(ctx, aid):
    need(can_edit(ctx.m))
    a = one("SELECT * FROM appointments WHERE id=? AND profile_id=?", (aid, ctx.pid))
    need(a, "Not found", 404)
    ex("DELETE FROM appointments WHERE id=?", (aid,))
    tell_elder_if_other(ctx, "deleted the appointment '%s'" % a["title"])
    return dict(ok=True)

@route("GET", "/api/insights")
def insights(ctx):
    return compute_insights(ctx.pid)

@route("GET", "/api/members")
def list_members(ctx):
    return [public_member(m, ctx.m) for m in sorted(circle(ctx.pid), key=lambda m: m["role"] != "elder")]

@route("POST", "/api/members")
def add_member(ctx):
    need(ctx.m["role"] == "elder", "Only the elder can invite people into the circle")
    b = ctx.body
    name, role = (b.get("name") or "").strip(), b.get("role")
    need(name and role in ("caretaker", "family"), "Name and role are required", 400)
    code = str(b.get("passcode") or "").strip() or "".join(secrets.choice("0123456789") for _ in range(4))
    need(re.fullmatch(r"\d{4,8}", code), "Passcode must be 4 to 8 digits", 400)
    for _ in range(20):
        if not one("SELECT 1 FROM members WHERE passcode=?", (code,)):
            break
        need(not b.get("passcode"), "That passcode is already used", 400)
        code = "".join(secrets.choice("0123456789") for _ in range(4))
    mid = ex("INSERT INTO members(profile_id,name,email,role,relation,passcode,can_edit,notify) VALUES(?,?,?,?,?,?,?,1)",
             (ctx.pid, name, (b.get("email") or "").strip(), role, (b.get("relation") or "").strip(), code, 1 if role == "caretaker" else 0))
    record(ctx.pid, ctx.m["name"], "invited %s (%s) to the circle" % (name, role))
    return dict(id=mid, passcode=code)

@route("PUT", r"/api/members/(\d+)")
def edit_member(ctx, mid):
    t = one("SELECT * FROM members WHERE id=? AND profile_id=?", (mid, ctx.pid))
    need(t, "Not found", 404)
    is_elder, is_self = ctx.m["role"] == "elder", ctx.m["id"] == mid
    need(is_elder or is_self)
    b = ctx.body
    name = (b.get("name") or t["name"]).strip()
    email = (b.get("email") if "email" in b else t["email"]) or ""
    notify = 1 if b.get("notify", t["notify"]) else 0
    ex("UPDATE members SET name=?,email=?,notify=? WHERE id=?", (name, email.strip(), notify, mid))
    if is_elder:
        if t["role"] != "elder":
            role = b.get("role", t["role"])
            need(role in ("caretaker", "family"), "Bad role", 400)
            ex("UPDATE members SET role=?, can_edit=?, relation=? WHERE id=?", (role, 1 if b.get("can_edit", t["can_edit"]) else 0, b.get("relation", t["relation"]), mid))
        code = str(b.get("passcode") or "").strip()
        if code and code != t["passcode"]:
            need(re.fullmatch(r"\d{4,8}", code), "Passcode must be 4 to 8 digits", 400)
            need(not one("SELECT 1 FROM members WHERE passcode=?", (code,)), "That passcode is already used", 400)
            ex("UPDATE members SET passcode=? WHERE id=?", (code, mid))
        if t["role"] != "elder" and bool(b.get("can_edit", t["can_edit"])) != bool(t["can_edit"]):
            record(ctx.pid, ctx.m["name"], "%s edit access for %s" % ("granted" if b.get("can_edit") else "removed", name))
    return dict(ok=True)

@route("DELETE", r"/api/members/(\d+)")
def del_member(ctx, mid):
    need(ctx.m["role"] == "elder")
    t = one("SELECT * FROM members WHERE id=? AND profile_id=?", (mid, ctx.pid))
    need(t and t["role"] != "elder", "Cannot remove this member", 400)
    ex("DELETE FROM sessions WHERE member_id=?", (mid,))
    ex("DELETE FROM members WHERE id=?", (mid,))
    record(ctx.pid, ctx.m["name"], "removed %s from the circle" % t["name"])
    return dict(ok=True)

@route("GET", "/api/activity")
def activity(ctx):
    return rows("SELECT * FROM activity WHERE profile_id=? ORDER BY id DESC LIMIT 60", (ctx.pid,))

@route("GET", "/api/outbox")
def outbox(ctx):
    return rows("SELECT id,to_name,to_email,subject,body,kind,ts,status FROM outbox WHERE profile_id=? ORDER BY id DESC LIMIT 60", (ctx.pid,))

@route("POST", "/api/tick")
def run_tick(ctx):
    need(ctx.m["role"] in ("elder", "caretaker"))
    return dict(new_emails=tick())

@route("POST", "/api/checkin")
def checkin(ctx):
    mood = int(ctx.body.get("mood", 3))
    need(1 <= mood <= 5, "Bad mood value", 400)
    ex("INSERT OR REPLACE INTO checkins(profile_id,date,mood,note,by_member) VALUES(?,?,?,?,?)", (ctx.pid, date.today().isoformat(), mood, ctx.body.get("note", ""), ctx.m["name"]))
    record(ctx.pid, ctx.m["name"], "logged today's wellbeing check-in (%d/5)" % mood)
    if mood <= 2:
        p = one("SELECT * FROM profiles WHERE id=?", (ctx.pid,))
        mail(ctx.pid, circle(ctx.pid, "caretaker", "family"), "%s is not feeling well today" % p["name"], "%s reported feeling low today (%d out of 5). A friendly call or visit could make a real difference." % (p["name"], mood), "wellbeing", "mood:%s" % date.today())
    return dict(ok=True)

@route("POST", "/api/sos")
def sos(ctx):
    p = one("SELECT * FROM profiles WHERE id=?", (ctx.pid,))
    last = [d for d in doses_for(ctx.pid, date.today()) if d["status"] == "taken"][-3:]
    body = ("%s has pressed the SOS button (sent by %s) at %s.\n\nPlease call them right away. If you cannot reach them, contact emergency services (112 in India).\n\n"
            "Medical snapshot\nConditions: %s\nAllergies: %s\nBlood group: %s\nDoctor: %s\nRecent doses today: %s") % (
        p["name"], ctx.m["name"], datetime.now().strftime("%I:%M %p"), p["conditions"] or "-", p["allergies"] or "-", p["blood_group"] or "-", p["doctor"] or "-",
        ", ".join("%s at %s" % (d["name"], fmt12(d["time"])) for d in last) or "none yet")
    n = mail(ctx.pid, circle(ctx.pid), "SOS: %s needs help" % p["name"], body, "sos")
    record(ctx.pid, ctx.m["name"], "pressed SOS and alerted the circle")
    return dict(notified=n)

@route("GET", "/api/summary")
def summary(ctx):
    ins = compute_insights(ctx.pid)
    return dict(profile=one("SELECT * FROM profiles WHERE id=?", (ctx.pid,)), meds=list_meds(ctx), insights=ins,
                appointments=rows("SELECT * FROM appointments WHERE profile_id=? ORDER BY when_at DESC LIMIT 6", (ctx.pid,)),
                generated=datetime.now().strftime("%d %b %Y, %I:%M %p"))

# ------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        if method == "GET" and not url.path.startswith("/api"):
            f = os.path.join(BASE, "index.html")
            return self._send(200, open(f, "rb").read(), "text/html; charset=utf-8")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._send(400, dict(error="Bad JSON"))
        try:
            for meth, rx, fn, public in ROUTES:
                mt = rx.match(url.path)
                if meth == method and mt:
                    member = None
                    if not public:
                        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                        s = one("SELECT member_id FROM sessions WHERE token=?", (tok,)) if tok else None
                        member = one("SELECT * FROM members WHERE id=?", (s["member_id"],)) if s else None
                        need(member, "Please sign in with your passcode", 401)
                    ctx = Ctx(member, body, parse_qs(url.query))
                    args = [int(g) if g.isdigit() else g for g in mt.groups()]
                    return self._send(200, fn(ctx, *args))
            raise ApiError(404, "Not found")
        except ApiError as e:
            self._send(e.code, dict(error=e.msg))
        except Exception as e:  # noqa
            traceback.print_exc()
            self._send(500, dict(error="Server error: %s" % e))

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")

def main():
    if "--reset" in sys.argv and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    if not one("SELECT 1 FROM profiles"):
        seed_demo()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("CareCircle running at http://localhost:%d  (email mode: %s)" % (PORT, "SMTP" if SMTP_HOST else "dry-run, see Emails tab"))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()

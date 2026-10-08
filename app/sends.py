"""Sending the customer form: a text, an email (from your mail app or from SimplyDoors), filled in on the spot, or an
installed customer form on a tablet. Each send carries a short random code, never the customer's details: opening it
fills in the name, and the lead arrives tagged "sent by Jose". Tracked as sent -> opened -> submitted.

Also the two lead alerts that run in the background: a lead nobody claimed in 2 business hours, and a busy spam day.
"""
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import alerts
from .db import audit, conn, get_setting, now_iso, set_setting
from .geo import TZ

CHANNELS = {"text": "Text", "email_app": "Email (my mail app)", "email_sent": "Email from SimplyDoors",
            "in_person": "Filled in here", "device": "Installed form"}
NUDGE_DAYS = 2                 # a sent link not used in 2 days is flagged
IN_PERSON_HOURS = 12           # a "fill it in here" code works this long
EMAILS_PER_DAY = 30            # "Send from SimplyDoors" per person per day
BUSINESS_START, BUSINESS_END = 8, 17     # Mon-Fri, Central
UNCLAIMED_BUSINESS_MINUTES = 120
SPAM_DAY_LIMIT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_RE = re.compile(r"[a-z0-9]{6,16}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_sends (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,              -- in the link (?s=code); never the customer's details
    channel TEXT NOT NULL,                  -- text | email_app | email_sent | in_person | device
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    first_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',         -- a device's name ("Showroom tablet")
    is_test INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,      -- an installed form can be turned off
    created_at TEXT NOT NULL,
    opened_at TEXT,
    submitted_at TEXT,
    lead_id INTEGER,                        -- the latest lead it brought in
    leads INTEGER NOT NULL DEFAULT 0        -- how many (an installed form brings in many)
);
CREATE INDEX IF NOT EXISTS intake_sends_staff ON intake_sends(staff_id, id);
"""


def init(c) -> None:
    c.executescript(SCHEMA)


def _first(name: str) -> str:
    return str(name or "").strip().split(" ")[0]


def create(staff, channel: str, first_name="", phone="", email="", label="", is_test=False) -> dict:
    code = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    c = conn()
    sid = c.execute("INSERT INTO intake_sends(code, channel, staff_id, first_name, phone, email, label, is_test, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (code, channel, staff["id"], " ".join(str(first_name).split())[:40], str(phone)[:30], str(email)[:120],
                     str(label)[:60], 1 if is_test else 0, now_iso())).lastrowid
    return c.execute("SELECT * FROM intake_sends WHERE id=?", (sid,)).fetchone()


def message(staff, first_name: str, link: str) -> str:
    me = _first(staff["name"])
    hi = f"Hi {first_name}" if first_name else "Hi"
    return f"{hi}, it's {me} from SimplyDoors. Here's the link to start your project: {link}"


def emails_today(staff_id: int) -> int:
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return conn().execute("SELECT COUNT(*) FROM intake_sends WHERE staff_id=? AND channel='email_sent' AND created_at>=?",
                          (staff_id, start.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]


def find(code: str):
    """The send behind a link code, if it still works (in-person codes last 12 hours; a turned-off device never)."""
    code = str(code or "").strip().lower()
    if not CODE_RE.fullmatch(code):
        return None
    r = conn().execute("SELECT s.*, st.name AS staff_name, st.active AS staff_active FROM intake_sends s"
                       " JOIN staff st ON st.id=s.staff_id WHERE s.code=?", (code,)).fetchone()
    if not r or not r["active"]:
        return None
    if r["channel"] == "in_person" and r["created_at"] < _iso(datetime.now(timezone.utc) - timedelta(hours=IN_PERSON_HOURS)):
        return None
    return r


def opened(r) -> None:
    if not r["opened_at"]:
        conn().execute("UPDATE intake_sends SET opened_at=? WHERE id=? AND opened_at IS NULL", (now_iso(), r["id"]))


def source(r) -> str:
    who = r["staff_name"]
    if r["channel"] == "device":
        return f"Installed form ({r['label'] or who + '’s device'})"
    if r["channel"] == "in_person":
        return f"Filled in with {who}"
    return f"Sent by {who} ({CHANNELS[r['channel']].lower()})"


def submitted(send_id: int, lead_id: int) -> None:
    conn().execute("UPDATE intake_sends SET submitted_at=COALESCE(submitted_at, ?), lead_id=?, leads=leads+1 WHERE id=?",
                   (now_iso(), lead_id, send_id))


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def out(r, with_lead: bool) -> dict:
    """One send for a list. People without Leads never get the lead itself, only whether it was sent in."""
    nudge = (r["channel"] in ("text", "email_app", "email_sent") and not r["submitted_at"]
             and r["created_at"] < _iso(datetime.now(timezone.utc) - timedelta(days=NUDGE_DAYS)))
    d = {"id": r["id"], "channel": r["channel"], "channel_label": CHANNELS[r["channel"]], "first_name": r["first_name"],
         "to": r["phone"] or r["email"], "label": r["label"], "created_at": r["created_at"], "opened_at": r["opened_at"],
         "submitted_at": r["submitted_at"], "leads": r["leads"], "active": bool(r["active"]), "nudge": nudge,
         "is_test": bool(r["is_test"]), "by": r["staff_name"] if "staff_name" in r.keys() else None}
    if with_lead and r["lead_id"]:
        d["lead_id"] = r["lead_id"]
    return d


def mine(staff) -> list[dict]:
    rows = conn().execute("SELECT s.*, st.name AS staff_name FROM intake_sends s JOIN staff st ON st.id=s.staff_id"
                          " WHERE s.staff_id=? AND s.channel!='in_person' ORDER BY s.id DESC LIMIT 60", (staff["id"],)).fetchall()
    return [out(r, False) for r in rows]


def waiting_all(include_test: bool) -> list[dict]:
    """For the Leads screen: everyone's sent links not used in 2 days."""
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=NUDGE_DAYS))
    rows = conn().execute("SELECT s.*, st.name AS staff_name FROM intake_sends s JOIN staff st ON st.id=s.staff_id"
                          " WHERE s.channel IN ('text','email_app','email_sent') AND s.submitted_at IS NULL AND s.created_at<?"
                          " AND s.created_at>=? AND (s.is_test=0 OR ?) ORDER BY s.id DESC LIMIT 50",
                          (cutoff, _iso(datetime.now(timezone.utc) - timedelta(days=30)), 1 if include_test else 0)).fetchall()
    return [out(r, True) for r in rows]


# ---------------------------------------------------------------- background alerts (run every 10 minutes)
def business_minutes(start: datetime, end: datetime) -> int:
    """Minutes between two moments that fall in business hours: Mon-Fri, 8 AM to 5 PM Central."""
    start, end = start.astimezone(TZ), end.astimezone(TZ)
    total, day = 0, start.date()
    while day <= end.date():
        if day.weekday() < 5:
            open_ = datetime(day.year, day.month, day.day, BUSINESS_START, tzinfo=TZ)
            close = datetime(day.year, day.month, day.day, BUSINESS_END, tzinfo=TZ)
            a, b = max(open_, start), min(close, end)
            if b > a:
                total += int((b - a).total_seconds() // 60)
        day += timedelta(days=1)
    return total


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_alerts(now: datetime | None = None) -> None:
    from .leads import OPS_URL
    now = now or datetime.now(timezone.utc)
    c = conn()
    # a lead nobody claimed in 2 business hours: one more alert to the owner, once per lead
    for r in c.execute("SELECT id, receipt, name, submitted_at, data FROM leads WHERE spam='' AND owner_id IS NULL"
                       " AND status='new' AND is_test=0 AND submitted_at>=?",
                       (_iso(now - timedelta(days=14)),)).fetchall():
        data = json.loads(r["data"])
        if data.get("unclaimed_alert") or business_minutes(_utc(r["submitted_at"]), now) < UNCLAIMED_BUSINESS_MINUTES:
            continue
        data["unclaimed_alert"] = _iso(now)
        c.execute("UPDATE leads SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), r["id"]))
        audit(None, "system", "lead_unclaimed_alert", f"lead:{r['id']}", {"receipt": r["receipt"]})
        alerts.push(f"Lead {r['receipt']} not claimed yet", f"{r['name']} has waited 2 business hours. Tap to claim it.",
                    "high", click=f"{OPS_URL}/leads#lead={r['id']}")
    # more than 10 caught or suspected spam in a day: one alert that day
    day = now.astimezone(TZ).strftime("%Y-%m-%d")
    if get_setting("spam_alert_day") == day:
        return
    start = _iso(now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc))
    n = (c.execute("SELECT COUNT(*) FROM intake_blocked WHERE at>=? AND is_test=0", (start,)).fetchone()[0]
         + c.execute("SELECT COUNT(*) FROM leads WHERE spam!='' AND submitted_at>=? AND is_test=0", (start,)).fetchone()[0])
    if n > SPAM_DAY_LIMIT:
        set_setting("spam_alert_day", day)
        audit(None, "system", "spam_day_alert", None, {"count": n})
        alerts.push("Customer form: lots of spam today",
                    f"{n} robots or suspected spam on the customer form today. Time to add Turnstile (ask Claude).", "default")

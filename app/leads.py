"""Customer intake (the public form at /start) and Leads (the office's screen for what comes in).

The form has no sign-in. Everything a customer can reach only ever ADDS: a new lead, or the "Tell us more"
answers for the lead that same phone just sent. Nothing is read back except that lead's receipt number.
A lead is saved before anyone is emailed; its emails go through the normal queue (mailer.py), so a failed
email never loses a lead.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import alerts
from .db import DATA_DIR, audit, conn, get_setting, now_iso, set_setting
from .pricelist import OUR_ADDRESS

INTAKE_PATH = "/start"         # customers reach the form at Studio's public address + this path
INTAKE_URL = os.environ.get("INTAKE_URL", "https://optiplex-ai.tailf0af63.ts.net" + INTAKE_PATH).rstrip("/")
OPS_URL = os.environ.get("OPS_URL", "https://optiplex-ai.tailf0af63.ts.net:10000/ops").rstrip("/")
RULE = "Customer Intake"       # its email list in Admin > Email lists
PREFIX = "INT"
FILE_DIR = os.path.join(DATA_DIR, "photos")   # lead-<id>/ next to the report photos, so the off-site copy takes them too

# Shown on the form and in the customer's email. Same lines as on purchase orders: change them in pricelist.OUR_ADDRESS.
OFFICE_STREET, OFFICE_CITY, OFFICE_PHONE = OUR_ADDRESS[0], OUR_ADDRESS[1], OUR_ADDRESS[2]

TYPES = ["Exterior door", "Interior doors", "Windows", "Millwork", "Other"]
HEARD = ["Google", "Facebook/Instagram", "Friend or family", "Builder or contractor", "Drove by / saw a truck", "Other"]
TIMELINES = ["ASAP", "1-3 months", "Just planning"]
WHO = ["Homeowner", "Builder", "Contractor"]
BEST_TIMES = ["Morning", "Afternoon", "Evening"]
STATUSES = {"new": "New", "called": "Called", "measure_booked": "Measure booked", "quoted": "Quoted",
            "won": "Won", "lost": "Lost"}
CLOSED = ("won", "lost")
SOURCES = ["Phone call", "Walk-in", "Email", "Text message", "Other"]   # how a lead added by hand came in

MAX_FILES = 5
MAX_PHOTO_BYTES = 15 * 1024 * 1024     # as sent; the phone shrinks photos first, and the server saves them smaller again
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 60 * 1024 * 1024
LOW_DISK_BYTES = 1_000_000_000         # under 1 GB free: the lead is still saved, its files aren't

# Spam rules. No outside service.
MIN_SECONDS = 3        # sent sooner than this after the page opened: a bot (fake "Got it", recorded, nobody told)
FAST_SECONDS = 10      # sooner than this: Suspected spam
PER_HOUR = 5           # forms from one connection in an hour; the 6th and on go to Suspected spam...
HARD_PER_HOUR = 20     # ...and past 20 they're treated as a bot
STALE_HOURS = 24       # a claimed lead with no claim, status change or note for this long is flagged
TEST_LINK_HOURS = 24
STEP2_DAYS = 2         # "Tell us more" answers are taken this long after the form was sent

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    receipt TEXT UNIQUE,
    submission_id TEXT NOT NULL UNIQUE,     -- made on the customer's phone; a retry never makes a second lead
    submitted_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0,
    spam TEXT NOT NULL DEFAULT '',          -- why it's in Suspected spam; '' = a lead
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL,                     -- JSON: project types, description, how they heard, "Tell us more"
    status TEXT NOT NULL DEFAULT 'new',
    owner_id INTEGER REFERENCES staff(id),  -- who claimed it
    claimed_at TEXT,
    touched_at TEXT,                        -- last claim, status change or note (the 24-hour flag)
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS leads_spam ON leads(spam, id);
CREATE TABLE IF NOT EXISTS lead_files (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    kind TEXT NOT NULL,                     -- photo | pdf
    name TEXT NOT NULL DEFAULT '',          -- the name it had on the customer's phone: shown, never used as a path
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_notes (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    at TEXT NOT NULL,
    text TEXT NOT NULL
);
-- bots that were stopped: kept 30 days so the office can see what was caught. Never emailed, never an alert.
CREATE TABLE IF NOT EXISTS intake_blocked (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    submission_id TEXT NOT NULL UNIQUE,
    receipt TEXT NOT NULL,                  -- the made-up number the bot was shown
    reason TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0,
    ip TEXT,
    name TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT ''
);
-- one row per form sent from a connection, for the per-hour limit (cleared after 2 days)
CREATE TABLE IF NOT EXISTS intake_hits (ip TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS intake_hits_ip ON intake_hits(ip, at);
"""


def init(c) -> None:
    c.executescript(SCHEMA)
    if not get_setting("intake_secret"):
        set_setting("intake_secret", secrets.token_hex(32))


def seed_owner_copy() -> None:
    """Once: the owner gets the Intake list as a private copy, like the other forms (the list itself shows admin@).
    So by default adem@ and admin@ both get every new lead, and the owner's address isn't on a shared list."""
    from .forms import owner_copies, owner_email, set_owner_copies
    if get_setting("intake_owner_copy") or not owner_email():
        return
    set_owner_copies(owner_copies() + [RULE])
    set_setting("intake_owner_copy", now_iso())


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(**kw) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(**kw))


# ---------------------------------------------------------------- who can see it
def allowed(staff) -> bool:
    """Admins always; everyone else only with "Can see Leads" switched on."""
    return bool(staff["is_admin"]) or bool("leads" in staff.keys() and staff["leads"])


def visible(staff, row) -> bool:
    """Test leads are the owner's own: nobody else sees them in their list."""
    return not row["is_test"] or bool(staff["is_owner"])


def people() -> list[dict]:
    return [{"id": r["id"], "name": r["name"]} for r in conn().execute(
        "SELECT id, name FROM staff WHERE active=1 AND (is_admin=1 OR leads=1) ORDER BY name")]


# ---------------------------------------------------------------- the page token (3-second rule)
def _secret() -> bytes:
    s = get_setting("intake_secret")
    if not s:
        s = secrets.token_hex(32)
        set_setting("intake_secret", s)
    return s.encode()


def form_token(now: float | None = None) -> str:
    """Printed into the form page when it's opened: when, signed, so a bot can't make one up."""
    ts = str(int(time.time() if now is None else now))
    return ts + "." + hmac.new(_secret(), b"intake|" + ts.encode(), hashlib.sha256).hexdigest()[:32]


def token_age(tok: str) -> float | None:
    """Seconds since the page carrying this token was opened, or None if this server didn't make it."""
    ts, _, sig = str(tok or "").partition(".")
    if not (ts.isascii() and ts.isdigit() and len(ts) <= 12 and len(sig) == 32):
        return None
    want = hmac.new(_secret(), b"intake|" + ts.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(want.encode(), sig.encode("utf-8", "replace")):
        return None
    age = time.time() - int(ts)
    return age if age > -60 else None


# ---------------------------------------------------------------- test links (the owner's test mode)
def make_test_link(owner) -> tuple[str, str]:
    """A link only the owner gets. Anything sent through it is a TEST lead. It ends after 24 hours, when the owner
    makes a new one, or when Test mode is switched off."""
    code = secrets.token_urlsafe(12)
    expires = _iso(datetime.now(timezone.utc) + timedelta(hours=TEST_LINK_HOURS))
    set_setting("intake_test_link", json.dumps({"hash": hashlib.sha256(code.encode()).hexdigest(),
                                                "expires": expires, "by": owner["id"]}))
    return code, expires


def add_by_staff(d: dict, source: str, staff, claim: bool, is_test: bool, ip: str, agent: str) -> int:
    """A lead someone typed in themselves (a phone call, a walk-in). Same numbering as the form. Nobody is emailed:
    the person adding it already has it, and it's claimed for them unless they untick that."""
    c = conn()
    now = now_iso()
    data = {"types": d["types"], "description": d["description"], "heard": d["heard"], "source": source,
            "added_by": staff["name"]}
    c.execute("BEGIN IMMEDIATE")
    try:
        lid = c.execute(
            "INSERT INTO leads(submission_id, submitted_at, is_test, name, phone, email, address, data, owner_id, claimed_at,"
            " touched_at, ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("staff-" + secrets.token_hex(12), now, 1 if is_test else 0, d["name"], d["phone"], d["email"], d["address"],
             json.dumps(data, ensure_ascii=False), staff["id"] if claim else None, now if claim else None,
             now if claim else None, ip, (agent or "")[:300])).lastrowid
        receipt = next_receipt(c, is_test)
        c.execute("UPDATE leads SET receipt=? WHERE id=?", (receipt, lid))
        audit(staff["id"], staff["name"], "lead_added", f"lead:{lid}",
              {"receipt": receipt, "source": source, "claimed": claim, **({"test": True} if is_test else {})}, ip, agent)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return lid


def test_link_ok(code: str) -> bool:
    code = str(code or "").strip()
    if not code or len(code) > 64 or get_setting("owner_test_mode") != "1":
        return False
    try:
        d = json.loads(get_setting("intake_test_link") or "{}")
    except ValueError:
        return False
    if not isinstance(d, dict) or not d.get("hash") or str(d.get("expires", "")) < now_iso():
        return False
    if not hmac.compare_digest(str(d["hash"]), hashlib.sha256(code.encode()).hexdigest()):
        return False
    return bool(conn().execute("SELECT 1 FROM staff WHERE id=? AND is_owner=1 AND active=1", (d.get("by"),)).fetchone())


# ---------------------------------------------------------------- spam
LINK_RE = re.compile(r"https?://|www\.|\[url", re.I)


def captcha_ok(raw: dict, ip: str) -> bool:
    """The one place to add Cloudflare Turnstile later: check raw.get("cf-turnstile-response") with Cloudflare here
    and return False for a bot (it then gets the same fake "Got it" as the checks below). No outside service yet."""
    return True


def bot_reason(raw: dict, age: float | None, ip: str) -> str:
    """Why this is certainly a bot, or ''. Bots are shown a normal-looking "Got it" and nothing else happens."""
    if str(raw.get("website") or "").strip():
        return "Filled in the hidden trap box"
    if age is None:
        return "Didn't come from the form page"
    if age < MIN_SECONDS:
        return f"Sent {age:.1f} seconds after the page opened"
    if not captcha_ok(raw, ip):
        return "Failed the robot check"
    return ""


def count_hit(ip: str) -> int:
    """Records one form from this connection and returns how many it sent in the last hour, this one included."""
    c = conn()
    c.execute("INSERT INTO intake_hits(ip, at) VALUES (?,?)", (ip, now_iso()))
    return c.execute("SELECT COUNT(*) FROM intake_hits WHERE ip=? AND at>=?", (ip, _ago(hours=1))).fetchone()[0]


def suspect_reasons(d: dict, age: float, hits: int, in_person: bool = False) -> list[str]:
    """Borderline: kept, but in Suspected spam (no emails, no alert) until someone moves it to Leads."""
    out = []
    # fast but complete: a phone filling in name and number for the person is quick, typing an address and a
    # description too in under 10 seconds isn't
    if age < FAST_SECONDS and d["address"] and d["description"] and not in_person:
        out.append(f"Filled in and sent {int(age)} seconds after the page opened (very fast for a person)")
    links = len(LINK_RE.findall(" ".join((d["name"], d["address"], d["description"]))))
    if LINK_RE.search(d["name"]):
        out.append("A link in the name")
    elif links >= 2:
        out.append(f"{links} links in the text")
    if hits > PER_HOUR:
        out.append(f"{hits} forms from the same connection in the last hour")
    return out


# ---------------------------------------------------------------- checking what was sent
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


def _one_line(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def clean(raw: dict, types: list) -> tuple[dict, list[str]]:
    """Step 1. Name, and a phone or an email, are needed; everything else is optional."""
    errors = []
    d = {"name": _one_line(raw.get("name"), 80), "phone": _one_line(raw.get("phone"), 30),
         "email": _one_line(raw.get("email"), 120), "address": _one_line(raw.get("address"), 200),
         "description": str(raw.get("description") or "").strip()[:4000]}
    if len(d["name"]) < 2:
        errors.append("Please type your name.")
    if d["phone"]:
        digits = re.sub(r"\D", "", d["phone"])
        if not 10 <= len(digits) <= 15:
            errors.append("That phone number doesn't look right. Type all 10 digits.")
    if d["email"] and not EMAIL_RE.match(d["email"]):
        errors.append("That email address doesn't look right.")
    if not d["phone"] and not d["email"]:
        errors.append("Give us a phone number or an email so we can reach you.")
    picked = {str(t) for t in types}
    d["types"] = [t for t in TYPES if t in picked]
    heard = str(raw.get("heard") or "")
    d["heard"] = heard if heard in HEARD else ""
    return d, errors


def clean_more(body: dict) -> tuple[dict, list[str]]:
    """Step 2, "Tell us more": all optional."""
    out, errors = {}, []
    for k in ("doors", "windows"):
        v = body.get(k)
        if v in (None, ""):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            n = -1
        if not 0 <= n <= 99:
            errors.append(f"Number of {k} should be 0 to 99.")
            continue
        out[k] = n
    for k, opts in (("timeline", TIMELINES), ("who", WHO), ("best_time", BEST_TIMES)):
        v = str(body.get(k) or "")
        if v in opts:
            out[k] = v
    return out, errors


def more_rows(data: dict) -> list[tuple[str, str]]:
    m = data.get("more") or {}
    rows = []
    if "doors" in m:
        rows.append(("Number of doors", str(m["doors"])))
    if "windows" in m:
        rows.append(("Number of windows", str(m["windows"])))
    for k, label in (("timeline", "Timeline"), ("who", "They are a"), ("best_time", "Best time to call")):
        if m.get(k):
            rows.append((label, m[k]))
    return rows


def greeting_name(name: str) -> str:
    """First name for "Hi Maria," in the customer's email; '' when it doesn't look like a plain name (a spammer's
    name never gets repeated back to whatever address they typed)."""
    first = str(name or "").strip().split(" ")[0]
    return first if re.fullmatch(r"[A-Za-zÀ-ÿ'’.-]{1,30}", first) else ""


# ---------------------------------------------------------------- saving
def received(sid: str) -> str | None:
    """The receipt this phone was already given for this submission (it retried), or None."""
    c = conn()
    r = c.execute("SELECT receipt FROM leads WHERE submission_id=?", (sid,)).fetchone()
    if r:
        return r["receipt"]
    r = c.execute("SELECT receipt FROM intake_blocked WHERE submission_id=?", (sid,)).fetchone()
    return r["receipt"] if r else None


def next_receipt(c, is_test: bool) -> str:
    # its own counter (INT-00001), carrying on from the highest so far; test leads count separately (TEST-INT-00001)
    pre = ("TEST-" if is_test else "") + PREFIX
    last = c.execute("SELECT MAX(CAST(substr(receipt, ?) AS INTEGER)) FROM leads WHERE receipt LIKE ?",
                     (len(pre) + 2, pre + "-%")).fetchone()[0]
    return f"{pre}-{(last or 0) + 1:05d}"


def record_blocked(sid: str, reason: str, is_test: bool, ip: str, raw: dict) -> str:
    """Keeps a short record of a caught bot and returns the made-up receipt it is shown."""
    c = conn()
    receipt = next_receipt(c, is_test)       # looks exactly like a real one; the number isn't used up
    contact = " / ".join(x for x in (_one_line(raw.get("phone"), 30), _one_line(raw.get("email"), 120)) if x)
    try:
        c.execute("INSERT INTO intake_blocked(at, submission_id, receipt, reason, is_test, ip, name, contact, text)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (now_iso(), sid, receipt, reason[:200], 1 if is_test else 0, ip, _one_line(raw.get("name"), 80),
                   contact[:160], _one_line(raw.get("description"), 300)))
    except sqlite3.IntegrityError:            # the same bot submission again
        return received(sid) or receipt
    return receipt


def folder(lid: int) -> str:
    return os.path.join(FILE_DIR, f"lead-{int(lid)}")


def _write_files(dest: str, files: list, save_photo) -> list:
    """files: [(kind, name, bytes)] -> [(kind, name, file name, size)]. Photos are saved again as plain JPEGs
    (no stamp, no hidden location or camera data); PDFs exactly as sent."""
    if not files:
        return []
    os.makedirs(dest, exist_ok=True)
    out = []
    for i, (kind, name, b) in enumerate(files, 1):
        fname = f"{i}.jpg" if kind == "photo" else f"{i}.pdf"
        p = os.path.join(dest, fname)
        if kind == "photo":
            size = save_photo(b, p)
        else:
            with open(p, "wb") as f:
                f.write(b)
            size = len(b)
        out.append((kind, name, fname, size))
    return out


def store(sid: str, d: dict, spam: list, is_test: bool, files: list, ip: str, agent: str, save_photo,
          extra: dict | None = None) -> dict:
    """Saves the files, then the lead, then queues its emails, all before the customer is told "Got it"."""
    c = conn()
    tmp = os.path.join(FILE_DIR, f"tmp-{secrets.token_hex(8)}")
    try:
        saved = _write_files(tmp, files, save_photo)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    data = {"types": d["types"], "description": d["description"], "heard": d["heard"], **(extra or {})}
    if d.get("files_not_saved"):
        data["files_not_saved"] = d["files_not_saved"]
    lid = None
    try:
        c.execute("BEGIN IMMEDIATE")
        lid = c.execute(
            "INSERT INTO leads(submission_id, submitted_at, is_test, spam, name, phone, email, address, data, ip, user_agent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, now_iso(), 1 if is_test else 0, "; ".join(spam), d["name"], d["phone"], d["email"], d["address"],
             json.dumps(data, ensure_ascii=False), ip, (agent or "")[:300])).lastrowid
        receipt = next_receipt(c, is_test)
        c.execute("UPDATE leads SET receipt=? WHERE id=?", (receipt, lid))
        dest = folder(lid)
        shutil.rmtree(dest, ignore_errors=True)       # only ever a leftover from a lead that never saved
        if saved:
            os.rename(tmp, dest)
        for kind, name, fname, size in saved:
            c.execute("INSERT INTO lead_files(lead_id, kind, name, path, bytes) VALUES (?,?,?,?,?)",
                      (lid, kind, name, os.path.join(dest, fname), size))
        details = {"receipt": receipt, "files": len(saved)}
        if spam:
            details["suspected_spam"] = spam
        if is_test:
            details["test"] = True
        audit(None, "Customer form", "lead_received", f"lead:{lid}", details, ip, agent)
        if not spam:
            queue_emails(lid)
        c.execute("COMMIT")
    except sqlite3.IntegrityError:
        c.execute("ROLLBACK")
        shutil.rmtree(tmp, ignore_errors=True)
        if lid:
            shutil.rmtree(folder(lid), ignore_errors=True)
        return {"receipt": received(sid), "duplicate": True}
    except Exception:
        c.execute("ROLLBACK")
        shutil.rmtree(tmp, ignore_errors=True)
        if lid:
            shutil.rmtree(folder(lid), ignore_errors=True)
        raise
    if not spam:
        alert(lid)
    from . import mailer
    mailer._wake.set()
    return {"receipt": receipt, "duplicate": False, "id": lid}


def save_more(sid: str, more: dict, ip: str, agent: str) -> None:
    """Step 2 onto the lead this phone sent. Unknown or caught submissions get the same quiet "ok"."""
    c = conn()
    r = c.execute("SELECT id, receipt, submitted_at, data FROM leads WHERE submission_id=?", (sid,)).fetchone()
    if not r or r["submitted_at"] < _ago(days=STEP2_DAYS):
        return
    data = json.loads(r["data"])
    data["more"] = more
    c.execute("UPDATE leads SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), r["id"]))
    audit(None, "Customer form", "lead_details_added", f"lead:{r['id']}", {"receipt": r["receipt"], "answers": len(more)},
          ip, agent)


# ---------------------------------------------------------------- emails + phone alert
def subject(lead, moved: bool = False) -> str:
    types = json.loads(lead["data"]).get("types") or []
    s = f"New lead: {lead['name']}" + (f" - {', '.join(types)}" if types else "") \
        + (" (moved from Suspected spam)" if moved else "")
    s = " ".join(f"{s} [{lead['receipt']}]".split())
    return ("TEST - " + s)[:200] if lead["is_test"] else s[:200]


def customer_subject(lead) -> str:
    s = f"We got your project request ({lead['receipt']})"
    return ("TEST - customer copy - " + s)[:200] if lead["is_test"] else s


def staff_recipients() -> tuple[list[str], list[str]]:
    """(to, private copies): the Intake list, plus the owner's private copy when it's ticked (Admin > Email lists)."""
    from .forms import _rule, owner_copies, owner_email
    to, seen = [], set()
    for e in _rule(RULE):
        if e.lower() not in seen:
            seen.add(e.lower())
            to.append(e)
    owner = owner_email()
    bcc = [owner] if owner and RULE in owner_copies() and owner.lower() not in seen else []
    return to, bcc


def queue_emails(lid: int, moved: bool = False) -> None:
    """The office's email and the customer's receipt. A test lead's emails all go to the owner only."""
    from . import mailer
    from .forms import owner_email
    lead = conn().execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    if lead["is_test"]:
        owner = owner_email()
        to, bcc = [e for e in [owner] if e], []
    else:
        to, bcc = staff_recipients()
    mailer.queue_lead_email(lid, to, subject(lead, moved), bcc)
    if lead["email"]:
        cust = owner_email() if lead["is_test"] else lead["email"]
        mailer.queue_lead_email(lid, [e for e in [cust] if e], customer_subject(lead), audience="customer")
        audit(None, "system", "customer_copy_queued", f"lead:{lid}",
              {"to": cust, "test": True} if lead["is_test"] else {"to": cust})


def alert(lid: int, moved: bool = False) -> None:
    lead = conn().execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    types = json.loads(lead["data"]).get("types") or []
    alerts.push(f"{'TEST ' if lead['is_test'] else ''}New lead {lead['receipt']}",
                f"{lead['name']}: {', '.join(types) or 'a project'}."
                + (" Moved from Suspected spam." if moved else "") + " Tap to open it and claim it.",
                "high", click=f"{OPS_URL}/leads#lead={lid}")


def bundle(lid: int):
    """(lead, data, files) for an email; None if the lead is gone."""
    c = conn()
    lead = c.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    if not lead:
        return None
    files = c.execute("SELECT * FROM lead_files WHERE lead_id=? ORDER BY id", (lid,)).fetchall()
    return lead, json.loads(lead["data"]), files


# ---------------------------------------------------------------- one customer across the app (Measure, Studio quotes)
ORDER = list(STATUSES)


def advance(lid: int, to: str) -> str | None:
    """Moves a lead forward to `to` (never back, never off Won/Lost). Returns the old status if it moved."""
    c = conn()
    r = c.execute("SELECT status FROM leads WHERE id=?", (lid,)).fetchone()
    if not r or r["status"] in CLOSED or ORDER.index(r["status"]) >= ORDER.index(to):
        return None
    c.execute("UPDATE leads SET status=?, touched_at=? WHERE id=?", (to, now_iso(), lid))
    return r["status"]


def search_open(q: str, include_test: bool, limit: int = 15) -> list[dict]:
    """Leads still in play (not spam, not Won/Lost), newest first, matching a name, phone, address or receipt."""
    q = " ".join(str(q or "").split())[:60]
    sql = ("SELECT id, receipt, name, address, data, status, is_test FROM leads WHERE spam='' AND status NOT IN ('won','lost')"
           " AND (is_test=0 OR ?)")
    args: list = [1 if include_test else 0]
    if q:
        digits = re.sub(r"\D", "", q)
        sql += " AND (name LIKE ? OR address LIKE ? OR receipt LIKE ?" + (" OR replace(replace(replace(replace(phone,'-',''),' ',''),'(',''),')','') LIKE ?" if len(digits) >= 3 else "") + ")"
        args += [f"%{q}%"] * 3 + ([f"%{digits}%"] if len(digits) >= 3 else [])
    rows = conn().execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    return [{"id": r["id"], "receipt": r["receipt"], "name": r["name"], "address": r["address"],
             "types": json.loads(r["data"]).get("types") or [], "status": STATUSES[r["status"]], "is_test": bool(r["is_test"])}
            for r in rows]


def contact(lid: int, include_test: bool):
    """Name, phone, email, address of one open lead (for filling in a measure or a Studio quote), or None."""
    r = conn().execute("SELECT * FROM leads WHERE id=? AND spam='' AND (is_test=0 OR ?)", (lid, 1 if include_test else 0)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"])
    return {"id": r["id"], "receipt": r["receipt"], "name": r["name"], "phone": r["phone"], "email": r["email"],
            "address": r["address"], "types": d.get("types") or [], "description": d.get("description") or "",
            "status": STATUSES[r["status"]], "is_test": bool(r["is_test"])}


def measured(lid: int, receipt: str, staff, ip: str, agent: str) -> None:
    """A measure was sent for this lead: it shows on the lead, and the lead moves to Measure booked."""
    r = conn().execute("SELECT receipt FROM leads WHERE id=?", (lid,)).fetchone()
    if not r:
        return
    was = advance(lid, "measure_booked")
    conn().execute("UPDATE leads SET touched_at=? WHERE id=?", (now_iso(), lid))
    audit(staff["id"], staff["name"], "lead_measured", f"lead:{lid}",
          {"receipt": r["receipt"], "measure": receipt, **({"status_from": was} if was else {})}, ip, agent)


def quoted(lid: int, ref: str, who: str, ip: str, agent: str) -> bool:
    """Studio made a quote for this lead."""
    c = conn()
    r = c.execute("SELECT receipt, data FROM leads WHERE id=? AND spam=''", (lid,)).fetchone()
    if not r:
        return False
    data = json.loads(r["data"])
    data["quotes"] = (data.get("quotes") or [])[-19:] + [{"ref": ref, "by": who, "at": now_iso()}]
    c.execute("UPDATE leads SET data=?, touched_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), now_iso(), lid))
    was = advance(lid, "quoted")
    audit(None, f"{who} (Studio)", "lead_quoted", f"lead:{lid}",
          {"receipt": r["receipt"], "quote": ref, **({"status_from": was} if was else {})}, ip, agent)
    return True


def sf_copy_text(r) -> str:
    """The customer's details in one block, to paste into Service Fusion (nothing is sent there from this app)."""
    d = json.loads(r["data"])
    lines = [r["name"], r["phone"], r["email"], r["address"], ", ".join(d.get("types") or []), d.get("description") or "",
             f"From SimplyDoors lead {r['receipt']}"]
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------- the Leads screen
def _stale(r, cutoff: str) -> bool:
    return bool(r["owner_id"]) and r["status"] not in CLOSED and (r["touched_at"] or r["claimed_at"] or "") < cutoff


def list_rows(staff, spam: bool) -> list[dict]:
    rows = conn().execute(
        "SELECT l.*, s.name AS owner_name, (SELECT COUNT(*) FROM lead_files f WHERE f.lead_id=l.id) AS nfiles"
        " FROM leads l LEFT JOIN staff s ON s.id=l.owner_id"
        f" WHERE l.spam {'!=' if spam else '='} '' AND (l.is_test=0 OR ?) ORDER BY l.id DESC LIMIT 300",
        (1 if staff["is_owner"] else 0,)).fetchall()
    cutoff = _ago(hours=STALE_HOURS)
    out = []
    for r in rows:
        d = json.loads(r["data"])
        out.append({"id": r["id"], "receipt": r["receipt"], "submitted_at": r["submitted_at"], "name": r["name"],
                    "address": r["address"], "types": d.get("types") or [], "status": r["status"],
                    "owner": r["owner_name"], "owner_id": r["owner_id"], "claimed_at": r["claimed_at"],
                    "stale": _stale(r, cutoff), "is_test": bool(r["is_test"]), "spam": r["spam"],
                    "files": r["nfiles"], "more": bool(d.get("more")), "source": d.get("source") or ""})
    return out


def counts(staff) -> dict:
    test = 1 if staff["is_owner"] else 0
    c = conn()
    return {"new": c.execute("SELECT COUNT(*) FROM leads WHERE spam='' AND owner_id IS NULL AND status='new'"
                             " AND (is_test=0 OR ?)", (test,)).fetchone()[0],
            "spam": c.execute("SELECT COUNT(*) FROM leads WHERE spam!='' AND (is_test=0 OR ?)", (test,)).fetchone()[0]}


def blocked_recent(staff) -> dict:
    test = 1 if staff["is_owner"] else 0
    since = _ago(days=7)
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM intake_blocked WHERE at>=? AND (is_test=0 OR ?)", (since, test)).fetchone()[0]
    rows = c.execute("SELECT at, receipt, reason, is_test, name, contact, text FROM intake_blocked"
                     " WHERE at>=? AND (is_test=0 OR ?) ORDER BY id DESC LIMIT 30", (since, test)).fetchall()
    return {"count": n, "rows": [dict(r) | {"is_test": bool(r["is_test"])} for r in rows]}


HISTORY = ("lead_measured", "lead_quoted", "lead_sf_job_set", "lead_added", "lead_received", "lead_details_added", "lead_claimed", "lead_reassigned", "lead_status_changed",
           "lead_note_added", "lead_moved_to_leads")


def _history_text(action: str, d: dict) -> str:
    if action == "lead_measured":
        return f"Measured: {d.get('measure')}" + (" (status moved to Measure booked)" if d.get("status_from") else "")
    if action == "lead_quoted":
        return f"Quoted in Studio: {d.get('quote')}" + (" (status moved to Quoted)" if d.get("status_from") else "")
    if action == "lead_sf_job_set":
        return f"Service Fusion job set: {d.get('to') or '(removed)'}"
    if action == "lead_added":
        return f"Added it by hand ({d.get('source', 'phone call')})" + (" and claimed it" if d.get("claimed") else "")
    if action == "lead_received":
        return "Sent the form" + (" (went to Suspected spam)" if d.get("suspected_spam") else "")
    if action == "lead_details_added":
        return "Answered “Tell us more”"
    if action == "lead_claimed":
        return "Claimed it"
    if action == "lead_reassigned":
        return f"Gave it to {d.get('to')}" + (f" (was {d['from']})" if d.get("from") else "")
    if action == "lead_status_changed":
        return f"Status: {STATUSES.get(d.get('from'), d.get('from'))} → {STATUSES.get(d.get('to'), d.get('to'))}"
    if action == "lead_note_added":
        return "Added a note"
    return "Moved it from Suspected spam to Leads"


def detail(r) -> dict:
    c = conn()
    d = json.loads(r["data"])
    owner = c.execute("SELECT name FROM staff WHERE id=?", (r["owner_id"],)).fetchone() if r["owner_id"] else None
    files = c.execute("SELECT id, kind, name FROM lead_files WHERE lead_id=? ORDER BY id", (r["id"],)).fetchall()
    notes = c.execute("SELECT n.at, n.text, s.name FROM lead_notes n JOIN staff s ON s.id=n.staff_id"
                      " WHERE n.lead_id=? ORDER BY n.id", (r["id"],)).fetchall()
    hist = []
    for a in c.execute(f"SELECT at, actor_name, action, details FROM audit WHERE target=? AND action IN"
                       f" ({','.join('?' * len(HISTORY))}) ORDER BY id", (f"lead:{r['id']}", *HISTORY)):
        try:
            det = json.loads(a["details"] or "{}")
        except ValueError:
            det = {}
        hist.append({"at": a["at"], "who": "Customer" if a["actor_name"] == "Customer form" else a["actor_name"],
                     "what": _history_text(a["action"], det if isinstance(det, dict) else {})})
    emails = c.execute("SELECT audience, status, sent_at FROM emails WHERE lead_id=? ORDER BY id", (r["id"],)).fetchall()
    measures = c.execute("SELECT r.id, r.receipt, r.submitted_at, json_extract(r.data, '$.measured_by') AS by FROM reports r"
                         " WHERE r.form_type='Measure Report' AND json_extract(r.data, '$.lead_id')=? ORDER BY r.id",
                         (r["id"],)).fetchall()
    return {"id": r["id"], "receipt": r["receipt"], "submitted_at": r["submitted_at"], "is_test": bool(r["is_test"]),
            "spam": r["spam"], "name": r["name"], "phone": r["phone"], "email": r["email"], "address": r["address"],
            "types": d.get("types") or [], "description": d.get("description") or "", "heard": d.get("heard") or "",
            "source": d.get("source") or "Customer form", "added_by": d.get("added_by"),
            "more": more_rows(d), "files_not_saved": d.get("files_not_saved", 0),
            "status": r["status"], "owner": owner["name"] if owner else None, "owner_id": r["owner_id"],
            "claimed_at": r["claimed_at"], "stale": _stale(r, _ago(hours=STALE_HOURS)),
            "files": [dict(f) for f in files],
            "notes": [{"at": n["at"], "by": n["name"], "text": n["text"]} for n in notes],
            "history": hist, "emails": [dict(e) for e in emails],
            "measures": [dict(m) for m in measures], "quotes": d.get("quotes") or [], "sf_job": d.get("sf_job") or "",
            "sf_copy": sf_copy_text(r)}


def tidy(c) -> None:
    """Nightly: the per-hour counts after 2 days, caught bots after 30."""
    c.execute("DELETE FROM intake_hits WHERE at < ?", (_ago(days=2),))
    c.execute("DELETE FROM intake_blocked WHERE at < ?", (_ago(days=30),))

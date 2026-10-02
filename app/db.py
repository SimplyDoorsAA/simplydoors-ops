"""Database: schema, seed data, and the append-only activity log.

SQLite file lives in DATA_DIR (a Docker volume on the OptiPlex).
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "ops.db")

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    dept TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0,
    sales_notify INTEGER NOT NULL DEFAULT 0,   -- appears in "Notify Sales Rep" list
    active INTEGER NOT NULL DEFAULT 1,
    pin_hash TEXT,
    pin_set_at TEXT,
    pin_source TEXT,                            -- 'import' | 'admin' | 'install'
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    lock_level INTEGER NOT NULL DEFAULT 0,      -- 1st lock 15 min, 2nd 1 hour, 3rd until an admin unlocks
    last_lock_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT, user_agent TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    code_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS ip_failures (
    ip TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ip_failures_ip ON ip_failures(ip, at);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    receipt TEXT UNIQUE,
    submission_id TEXT NOT NULL UNIQUE,         -- made on the phone; stops duplicates on retry
    form_type TEXT NOT NULL,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    submitted_at TEXT NOT NULL,                 -- server clock
    started_at TEXT,                            -- phone clock, when the form was opened
    queued_on_phone INTEGER NOT NULL DEFAULT 0, -- 1 = arrived from the phone's offline queue
    data TEXT NOT NULL                          -- JSON of every field
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    slot TEXT NOT NULL,                         -- e.g. ticket1, product2
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    taken_at TEXT,                              -- when it was added on the phone
    lat REAL, lon REAL, acc REAL,               -- where (acc = accuracy in metres)
    geo_status TEXT,                            -- ok | denied | off | unavailable | timeout | unsupported | missing
    file_age INTEGER                            -- seconds old the file was when added (gallery check)
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    report_id INTEGER REFERENCES reports(id),
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    next_try_at TEXT NOT NULL,
    sent_at TEXT,
    bcc TEXT NOT NULL DEFAULT ''                -- private copies (the owner's), never shown in the email or to other admins
);

CREATE TABLE IF NOT EXISTS email_rules (
    form_type TEXT PRIMARY KEY,
    recipients TEXT NOT NULL DEFAULT ''          -- comma-separated, always emailed
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Activity log. Rows can be added but never changed or removed:
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor_id INTEGER,
    actor_name TEXT,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'activity log is append-only'); END;
-- The one exception: rows the app has listed in audit_delete_ok inside the same transaction
-- (the owner removing test reports or their own routine lines; see delete_audit_rows).
CREATE TABLE IF NOT EXISTS audit_delete_ok (id INTEGER PRIMARY KEY);
"""

AUDIT_DELETE_TRIGGER = ("CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit "
                        "WHEN OLD.id NOT IN (SELECT id FROM audit_delete_ok) "
                        "BEGIN SELECT RAISE(ABORT, 'activity log is append-only'); END")

# Staff as listed in the old portal + Apps Script (2026-10-01).
SEED_STAFF = [
    # name, dept, email, is_admin, sales_notify
    ("Jaime Mendoza", "Warehouse / Driver", "jaimem@simplydoors.com", 0, 0),
    ("Jose Blanco", "Warehouse / Driver", "joseb@simplydoors.com", 0, 0),
    ("Jay Bryant", "Warehouse / Driver", "jayb@simplydoors.com", 0, 0),
    ("Elijah Kimmel", "Warehouse / Driver", "elijahk@simplydoors.com", 0, 0),
    ("Lupe Sanchez", "Production", "lupes@simplydoors.com", 0, 0),
    ("Isaiah Stratton", "Production", "isaiahs@simplydoors.com", 0, 1),
    ("Ramiro Zuniga", "Production", "ramiroz@simplydoors.com", 0, 0),
    ("Gerardo Zuniga", "Production", "gerardoz@simplydoors.com", 0, 0),
    ("Adem Atis", "Sales", "adem@simplydoors.com", 1, 1),
    ("Steven Chandler", "Sales", "steven@simplydoors.com", 0, 1),
    ("John Suttin", "Sales", "john@simplydoors.com", 0, 1),
    ("Cory Broyles", "Sales", "coryb@simplydoors.com", 0, 1),
    ("Dennis Haberer", "Sales", "dennish@simplydoors.com", 0, 1),
    ("Paz Galambos", "Admin", "paz@simplydoors.com", 1, 1),
    ("Kevin Nguyen", "Admin", "kevinn@simplydoors.com", 0, 1),
]

# Always-emailed addresses per report, copied from the old Apps Script.
# Extra recipients that depend on the report (sales rep picked on a Receiving
# Report, admin@ for a defective vehicle, the employee written up) are added in code.
SEED_RULES = {
    "Receiving Report": "adem@simplydoors.com, lupes@simplydoors.com",
    "End of Shift": "adem@simplydoors.com, lupes@simplydoors.com",
    "Vehicle Inspection": "adem@simplydoors.com, lupes@simplydoors.com",
    "Employee Incident": "adem@simplydoors.com, lupes@simplydoors.com, admin@simplydoors.com",
    "Vehicle Incident": "adem@simplydoors.com, lupes@simplydoors.com, admin@simplydoors.com",
    "Disciplinary Action": "adem@simplydoors.com, paz@simplydoors.com, admin@simplydoors.com",
    "Measure Report": "admin@simplydoors.com",
    "Delivery Proof": "adem@simplydoors.com, lupes@simplydoors.com",
    "Installation Completion": "admin@simplydoors.com",
    "RMA": "admin@simplydoors.com",
    "Vehicle Inspection: when something is Defective": "admin@simplydoors.com",
}


def init_db() -> None:
    c = conn()
    c.executescript(SCHEMA)
    trig = c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='audit_no_delete'").fetchone()
    if not trig or "audit_delete_ok" not in trig[0]:
        c.execute("DROP TRIGGER IF EXISTS audit_no_delete")
        c.execute(AUDIT_DELETE_TRIGGER)
    rcols = {r[1] for r in c.execute("PRAGMA table_info(reports)")}
    if "is_test" not in rcols:
        c.execute("ALTER TABLE reports ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0")
    cols = {r[1] for r in c.execute("PRAGMA table_info(staff)")}
    pcols = {r[1] for r in c.execute("PRAGMA table_info(photos)")}
    for col, typ in (("taken_at", "TEXT"), ("lat", "REAL"), ("lon", "REAL"), ("acc", "REAL"),
                     ("geo_status", "TEXT"), ("file_age", "INTEGER")):
        if col not in pcols:
            c.execute(f"ALTER TABLE photos ADD COLUMN {col} {typ}")
    if "studio_link" not in cols:
        # NULL = automatic (admins and the Sales / Admin departments see the Simply Studio tile)
        c.execute("ALTER TABLE staff ADD COLUMN studio_link INTEGER")
    if "is_owner" not in cols:
        # the person who built the app: full admin, can manage other admins; only changed from the server console
        c.execute("ALTER TABLE staff ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0")
    if "lock_level" not in cols:
        c.execute("ALTER TABLE staff ADD COLUMN lock_level INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE staff ADD COLUMN last_lock_at TEXT")
    if c.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
        for name, dept, email, admin, notify in SEED_STAFF:
            c.execute(
                "INSERT INTO staff(name, dept, email, is_admin, sales_notify, created_at) VALUES (?,?,?,?,?,?)",
                (name, dept, email, admin, notify, now_iso()),
            )
        audit(None, "system", "staff_seeded", None, {"count": len(SEED_STAFF)})
    if not c.execute("SELECT 1 FROM staff WHERE is_owner=1").fetchone():
        c.execute("UPDATE staff SET is_owner=1, is_admin=1 WHERE name='Adem Atis'")
    for form, rcpts in SEED_RULES.items():
        c.execute("INSERT OR IGNORE INTO email_rules(form_type, recipients) VALUES (?,?)", (form, rcpts))
    ecols = {r[1] for r in c.execute("PRAGMA table_info(emails)")}
    if "bcc" not in ecols:
        c.execute("ALTER TABLE emails ADD COLUMN bcc TEXT NOT NULL DEFAULT ''")
    _move_owner_off_lists(c)


def _move_owner_off_lists(c) -> None:
    """Once: take the owner's address off the shared email lists (other admins can read those) and turn
    each one into a private copy instead, so the owner still gets exactly what they got before."""
    if c.execute("SELECT 1 FROM settings WHERE key='owner_copies'").fetchone():
        return
    row = c.execute("SELECT email FROM staff WHERE is_owner=1").fetchone()
    owner = (row["email"] if row else "").strip().lower()
    if not owner:
        return
    forms = []
    for r in c.execute("SELECT form_type, recipients FROM email_rules").fetchall():
        parts = [x.strip() for x in r["recipients"].split(",") if x.strip()]
        keep = [x for x in parts if x.lower() != owner]
        if len(keep) != len(parts):
            forms.append(r["form_type"])
            c.execute("UPDATE email_rules SET recipients=? WHERE form_type=?", (", ".join(keep), r["form_type"]))
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('owner_copies', ?)", (json.dumps(forms),))
    audit(None, "system", "owner_copies_set_up", None, {"forms": forms})


def delete_audit_rows(c, ids) -> int:
    """Remove specific activity-log rows. Call inside an open transaction. Nothing else can delete from the log."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    c.executemany("INSERT OR IGNORE INTO audit_delete_ok(id) VALUES (?)", [(i,) for i in ids])
    n = c.execute(f"DELETE FROM audit WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount
    c.execute("DELETE FROM audit_delete_ok")
    return n


def audit(actor_id, actor_name, action, target=None, details=None, ip=None, user_agent=None) -> None:
    conn().execute(
        "INSERT INTO audit(at, actor_id, actor_name, action, target, details, ip, user_agent) VALUES (?,?,?,?,?,?,?,?)",
        (now_iso(), actor_id, actor_name, action, target,
         json.dumps(details, ensure_ascii=False) if details is not None else None, ip, (user_agent or "")[:300]),
    )


def get_setting(key, default=None):
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    conn().execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

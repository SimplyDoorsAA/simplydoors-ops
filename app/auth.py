"""PIN hashing, login lockout, and sessions."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from .db import conn, now_iso, audit

MAX_FAILS = 5                # wrong PINs in a row before a lock
LOCK_STEPS = [15, 60]        # minutes for the 1st and 2nd lock within a day; 3rd needs an admin
ADMIN_ONLY_UNTIL = "9999-12-31T00:00:00Z"
IP_MAX_FAILS = 20            # wrong PINs from one address in IP_WINDOW before it is blocked
IP_WINDOW_MINUTES = 15
STAFF_SESSION_DAYS = 14      # staff stay signed in on their own phone
ADMIN_SESSION_HOURS = 12     # admins sign in again more often


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pin(pin: str, stored: str | None) -> bool:
    if not stored:
        # Spend the same time as a real check so a missing PIN can't be told apart.
        hashlib.scrypt(pin.encode(), salt=b"0" * 16, n=2 ** 14, r=8, p=1, dklen=32)
        return False
    try:
        _, salt_hex, h_hex = stored.split("$")
        h = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def valid_pin_format(pin: str) -> bool:
    return pin.isdigit() and 6 <= len(pin) <= 8


def set_pin(staff_id: int, pin: str, source: str) -> None:
    conn().execute(
        "UPDATE staff SET pin_hash=?, pin_set_at=?, pin_source=?, failed_count=0, locked_until=NULL, lock_level=0 WHERE id=?",
        (hash_pin(pin), now_iso(), source, staff_id),
    )


def ip_blocked(ip: str) -> bool:
    since = _iso(datetime.now(timezone.utc) - timedelta(minutes=IP_WINDOW_MINUTES))
    n = conn().execute("SELECT COUNT(*) FROM ip_failures WHERE ip=? AND at>=?", (ip, since)).fetchone()[0]
    return n >= IP_MAX_FAILS


def attempt_login(staff_name: str, pin: str, ip: str, ua: str):
    """Returns (staff_row | None, message, newly_locked: bool)."""
    c = conn()
    if ip_blocked(ip):
        audit(None, staff_name, "login_blocked_ip", staff_name, None, ip, ua)
        return None, "Too many wrong PINs from this connection. Try again in 15 minutes.", False

    row = c.execute("SELECT * FROM staff WHERE name=? AND active=1", (staff_name,)).fetchone()
    now = datetime.now(timezone.utc)
    if row and row["locked_until"] and _utc(row["locked_until"]) > now:
        audit(row["id"], row["name"], "login_while_locked", row["name"], None, ip, ua)
        if row["locked_until"] == ADMIN_ONLY_UNTIL:
            return None, "This account is locked. Ask Adem or Paz to unlock it.", False
        mins = max(1, int((_utc(row["locked_until"]) - now).total_seconds() // 60) + 1)
        return None, f"This account is locked for {mins} more minute(s), or ask Adem or Paz to unlock it.", False

    if row and check_pin(pin, row["pin_hash"]):
        c.execute("UPDATE staff SET failed_count=0, locked_until=NULL, lock_level=0 WHERE id=?", (row["id"],))
        audit(row["id"], row["name"], "login_ok", row["name"], None, ip, ua)
        return row, "ok", False

    if not row:
        check_pin(pin, None)
    c.execute("INSERT INTO ip_failures(ip, at) VALUES (?,?)", (ip, now_iso()))
    if row and not row["pin_hash"]:
        audit(row["id"], row["name"], "login_fail_no_pin", row["name"], None, ip, ua)
        return None, "Wrong PIN. If you've never been given a PIN, ask Adem or Paz.", False
    if row:
        fails = row["failed_count"] + 1
        if fails >= MAX_FAILS:
            level = row["lock_level"]
            if row["last_lock_at"] and _utc(row["last_lock_at"]) < now - timedelta(days=1):
                level = 0                       # a day without trouble starts over
            level += 1
            if level > len(LOCK_STEPS):
                until, msg = ADMIN_ONLY_UNTIL, "Too many wrong PINs. This account is locked until Adem or Paz unlocks it."
            else:
                mins = LOCK_STEPS[level - 1]
                until, msg = _iso(now + timedelta(minutes=mins)), f"Too many wrong PINs. This account is locked for {mins} minutes."
            c.execute("UPDATE staff SET failed_count=0, locked_until=?, lock_level=?, last_lock_at=? WHERE id=?",
                      (until, level, _iso(now), row["id"]))
            audit(row["id"], row["name"], "account_locked", row["name"],
                  {"lock": level, "until": "admin unlock" if until == ADMIN_ONLY_UNTIL else until}, ip, ua)
            return None, msg, True
        c.execute("UPDATE staff SET failed_count=? WHERE id=?", (fails, row["id"]))
        audit(row["id"], row["name"], "login_fail", row["name"], {"attempt": fails}, ip, ua)
        left = MAX_FAILS - fails
        return None, f"Wrong PIN. {left} more tr{'y' if left == 1 else 'ies'} before the account locks.", False
    audit(None, staff_name, "login_fail_unknown_name", staff_name, None, ip, ua)
    return None, "Wrong PIN.", False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(row, ip: str, ua: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    life = timedelta(hours=ADMIN_SESSION_HOURS) if row["is_admin"] else timedelta(days=STAFF_SESSION_DAYS)
    conn().execute(
        "INSERT INTO sessions(token_hash, staff_id, created_at, last_seen, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?)",
        (_token_hash(token), row["id"], _iso(now), _iso(now), _iso(now + life), ip, (ua or "")[:300]),
    )
    return token, int(life.total_seconds())


def session_staff(token: str | None):
    if not token:
        return None
    c = conn()
    s = c.execute("SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)).fetchone()
    if not s:
        return None
    if _utc(s["expires_at"]) < datetime.now(timezone.utc):
        c.execute("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
        return None
    row = c.execute("SELECT * FROM staff WHERE id=? AND active=1", (s["staff_id"],)).fetchone()
    if not row:
        return None
    c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now_iso(), s["token_hash"]))
    return row


def end_session(token: str | None) -> None:
    if token:
        conn().execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def end_all_sessions(staff_id: int) -> None:
    conn().execute("DELETE FROM sessions WHERE staff_id=?", (staff_id,))

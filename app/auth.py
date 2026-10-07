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


INVITE_DAYS = 7
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L mix-ups


def weak_pin(pin: str) -> bool:
    """Refuse PINs anyone would guess first."""
    if len(set(pin)) == 1:
        return True
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if steps in ({1}, {-1}):
        return True
    half = len(pin) // 2
    if len(pin) % 2 == 0 and pin[:half] == pin[half:] and len(set(pin[:half])) <= 2:
        return True
    if len(pin) % 3 == 0 and pin == pin[:3] * (len(pin) // 3):
        return True
    return pin in {"123123", "121212", "112233", "696969", "000000", "123321", "654321", "102030",
                   "147258", "159753", "789456", "456789", "202020", "101010", "131313", "123654"}


def new_invite(staff_id: int, created_by: str) -> tuple[str, str]:
    """Returns (code, expires_at). Any earlier unused invite for this person stops working."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    now = datetime.now(timezone.utc)
    c = conn()
    c.execute("UPDATE invites SET revoked_at=? WHERE staff_id=? AND used_at IS NULL AND revoked_at IS NULL",
              (_iso(now), staff_id))
    expires = _iso(now + timedelta(days=INVITE_DAYS))
    c.execute("INSERT INTO invites(staff_id, code_hash, created_by, created_at, expires_at) VALUES (?,?,?,?,?)",
              (staff_id, _token_hash(normalize_code(code)), created_by, _iso(now), expires))
    return code, expires


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def find_invite(code: str):
    """Valid, unused, unexpired invite + the person it's for, or None."""
    norm = normalize_code(code)
    if len(norm) != 8:
        return None
    return conn().execute(
        "SELECT i.*, s.name, s.active, s.is_admin FROM invites i JOIN staff s ON s.id=i.staff_id"
        " WHERE i.code_hash=? AND i.used_at IS NULL AND i.revoked_at IS NULL AND i.expires_at>? AND s.active=1",
        (_token_hash(norm), now_iso())).fetchone()


def valid_pin_format(pin: str) -> bool:
    return pin.isdigit() and 6 <= len(pin) <= 8


def set_pin(staff_id: int, pin: str, source: str) -> None:
    conn().execute(
        "UPDATE staff SET pin_hash=?, pin_set_at=?, pin_source=?, failed_count=0, locked_until=NULL, lock_level=0 WHERE id=?",
        (hash_pin(pin), now_iso(), source, staff_id),
    )


def _ip_fails(ip: str) -> int:
    since = _iso(datetime.now(timezone.utc) - timedelta(minutes=IP_WINDOW_MINUTES))
    return conn().execute("SELECT COUNT(*) FROM ip_failures WHERE ip=? AND at>=?", (ip, since)).fetchone()[0]


def ip_blocked(ip: str) -> bool:
    return _ip_fails(ip) >= IP_MAX_FAILS


def _ip_try(ip: str):
    """Counts a sign-in try against this connection BEFORE the slow PIN check, so a burst of parallel guesses
    all count. Returns the row to take back if it turns out not to be a wrong PIN, or None if blocked."""
    c = conn()
    rid = c.execute("INSERT INTO ip_failures(ip, at) VALUES (?,?)", (ip, now_iso())).lastrowid
    if _ip_fails(ip) > IP_MAX_FAILS:           # this try included, so the limit is the same as before
        c.execute("DELETE FROM ip_failures WHERE rowid=?", (rid,))
        return None
    return rid


def _locked_msg(locked_until, now) -> str | None:
    if not locked_until or _utc(locked_until) <= now:
        return None
    if locked_until == ADMIN_ONLY_UNTIL:
        return "This account is locked. Ask Adem or Paz to unlock it."
    mins = max(1, int((_utc(locked_until) - now).total_seconds() // 60) + 1)
    return f"This account is locked for {mins} more minute(s), or ask Adem or Paz to unlock it."


def attempt_login(staff_name: str, pin: str, ip: str, ua: str):
    """Returns (staff_row | None, message, newly_locked: bool)."""
    c = conn()
    ip_try = _ip_try(ip)
    if ip_try is None:
        audit(None, staff_name, "login_blocked_ip", staff_name, None, ip, ua)
        return None, "Too many wrong PINs from this connection. Try again in 15 minutes.", False

    row = c.execute("SELECT * FROM staff WHERE name=? AND active=1", (staff_name,)).fetchone()
    now = datetime.now(timezone.utc)
    if row and _locked_msg(row["locked_until"], now):
        c.execute("DELETE FROM ip_failures WHERE rowid=?", (ip_try,))     # a locked account isn't a wrong PIN
        audit(row["id"], row["name"], "login_while_locked", row["name"], None, ip, ua)
        return None, _locked_msg(row["locked_until"], now), False

    ok = check_pin(pin, row["pin_hash"] if row else None)    # slow on purpose; same time for unknown names
    if not row:
        audit(None, staff_name, "login_fail_unknown_name", staff_name, None, ip, ua)
        return None, "Wrong PIN.", False
    # From here one sign-in at a time per database, so parallel wrong PINs each count and lock exactly once,
    # and a right PIN that lands after a parallel guess locked the account is still refused.
    c.execute("BEGIN IMMEDIATE")
    try:
        cur = c.execute("SELECT failed_count, lock_level, last_lock_at, locked_until FROM staff WHERE id=?",
                        (row["id"],)).fetchone()
        msg = _locked_msg(cur["locked_until"], now)
        if msg:
            c.execute("DELETE FROM ip_failures WHERE rowid=?", (ip_try,))
            audit(row["id"], row["name"], "login_while_locked", row["name"], None, ip, ua)
            c.execute("COMMIT")
            return None, msg, False
        if ok:
            c.execute("UPDATE staff SET failed_count=0, locked_until=NULL, lock_level=0 WHERE id=?", (row["id"],))
            c.execute("DELETE FROM ip_failures WHERE rowid=?", (ip_try,))
            audit(row["id"], row["name"], "login_ok", row["name"], None, ip, ua)
            c.execute("COMMIT")
            return row, "ok", False
        if not row["pin_hash"]:
            audit(row["id"], row["name"], "login_fail_no_pin", row["name"], None, ip, ua)
            c.execute("COMMIT")
            return None, "Wrong PIN. If you've never been given a PIN, ask Adem or Paz.", False
        fails = c.execute("UPDATE staff SET failed_count=failed_count+1 WHERE id=? RETURNING failed_count",
                          (row["id"],)).fetchone()[0]
        if fails >= MAX_FAILS:
            level = cur["lock_level"]
            if cur["last_lock_at"] and _utc(cur["last_lock_at"]) < now - timedelta(days=1):
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
            c.execute("COMMIT")
            return None, msg, True
        audit(row["id"], row["name"], "login_fail", row["name"], {"attempt": fails}, ip, ua)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    left = MAX_FAILS - fails
    return None, f"Wrong PIN. {left} more tr{'y' if left == 1 else 'ies'} before the account locks.", False


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

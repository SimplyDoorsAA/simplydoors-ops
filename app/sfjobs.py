"""Job lookup: a copy of Service Fusion's open jobs, so Measure and Install can be filled in by picking a job.

- Refreshed every REFRESH_MINUTES during work hours, plus whenever someone taps Refresh (at most once a minute).
- Only OPEN jobs are kept. A failed refresh keeps the last good copy and says how old it is.
- Search shows job number, customer name, status and date. Address, phone and email are only sent once a job is
  picked, and every pick is written to the activity log (who opened which job).
- Customer email/phone come from the customer record, fetched when a job is picked and kept for a day.
- Read-only: nothing is ever written to Service Fusion.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import alerts
from .db import conn, get_setting, now_iso, set_setting

API = os.environ.get("SF_API", "https://api.servicefusion.com")
CLIENT_ID = os.environ.get("SF_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "")
TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "America/Chicago"))
REFRESH_MINUTES = 20
WORK_DAYS = range(0, 6)             # Monday-Saturday
WORK_HOURS = (6, 19)                # 6 AM - 7 PM local
MANUAL_MIN_SECONDS = 60
CONTACT_TTL = timedelta(hours=24)
PER_PAGE = 50                       # Service Fusion refuses more than 50

# Statuses whose jobs are finished. Everything else in Service Fusion's status list counts as open.
CLOSED = {"17 completed", "18 invoiced", "19 cancelled", "job closed", "paid in full"}
# Statuses the install crew is most likely looking for; shown first on the Install form.
INSTALL_STAGE = {"10 install scheduled", "13 ready for install", "16 partial completed", "warranty",
                 "18warranty scheduled", "18 warranty scheduled"}
# Used only if Service Fusion's status list can't be read.
FALLBACK_OPEN = ["1 Unscheduled", "2 Scheduled Consult", "3 Awaiting Deposit", "4 Need to Order", "5 Ordered Pend ACK",
                 "6 In Prod no ETA", "7 Awaiting Product", "8 Needs Attention", "9 In Warehouse", "10 Install Scheduled",
                 "10 Ready for Pickup", "11 Ready to deliver", "12 Ready for Pickup", "13 Ready for Install",
                 "14 Request Exhibit A", "15Delivery Scheduled", "16 Partial Completed", "Shop work Req", "Warranty",
                 "18Warranty Scheduled", "Customer Not Ready", "Measure Complete", "Consultation"]

_lock = threading.Lock()            # one refresh at a time
_token = {"value": None, "exp": 0.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sf_jobs (
    number TEXT PRIMARY KEY,
    customer_id INTEGER,
    customer_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    sub_status TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    search TEXT NOT NULL DEFAULT '',        -- lower-case customer + contact name, for name search
    data TEXT NOT NULL,                     -- the fields the forms use (JSON)
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sf_contacts (
    customer_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def init() -> None:
    conn().executescript(SCHEMA)


# ------------------------------------------------------------------ Service Fusion calls
class SFError(Exception):
    pass


def _http(method, path, data=None, token=None, form=False, tries=3):
    body, headers = None, {"Accept": "application/json", "User-Agent": "SimplyDoors-Ops/1"}
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(tries):
        req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(min(int(e.headers.get("Retry-After") or 5), 30))
                continue
            if e.code >= 500 and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise SFError(f"Service Fusion said HTTP {e.code} for {path.split('?')[0]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise SFError(f"Couldn't reach Service Fusion ({type(e).__name__})")
    raise SFError("Service Fusion didn't answer")


def _get_token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    cred = {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    try:
        t = _http("POST", "/oauth/access_token", cred)
    except SFError:
        t = _http("POST", "/oauth/access_token", cred, form=True)
    if not isinstance(t, dict) or not t.get("access_token"):
        raise SFError("Service Fusion refused the API key")
    _token["value"] = t["access_token"]
    _token["exp"] = time.time() + int(t.get("expires_in") or 3600)
    return _token["value"]


def _get(path):
    try:
        return _http("GET", path, token=_get_token())
    except SFError as e:
        if "HTTP 401" in str(e):          # token expired early: get a new one once
            _token["value"] = None
            return _http("GET", path, token=_get_token())
        raise


def _items(body):
    if isinstance(body, dict):
        return body.get("items") or [], int(((body.get("_meta") or {}).get("pageCount")) or 1)
    return (body or []), 1


def _paged(path):
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        rows, pages = _items(_get(f"{path}{sep}per-page={PER_PAGE}&page={page}"))
        out.extend(rows)
        if page >= pages or not rows or page >= 40:
            return out
        page += 1


def open_statuses() -> list[str]:
    try:
        rows = _paged("/v1/job-statuses")
        names = []
        for r in rows:
            n = str((r or {}).get("name") or (r or {}).get("code") or "").strip()
            if n and n.lower() not in CLOSED and n not in names:
                names.append(n)
        if names:
            return names
    except SFError:
        pass
    return list(FALLBACK_OPEN)


# ------------------------------------------------------------------ shaping
def _s(v, n=200) -> str:
    return " ".join(str(v or "").split())[:n]


def _date(v) -> str:
    v = str(v or "")
    return v[:10] if re.match(r"\d{4}-\d\d-\d\d", v) else ""


def _job_row(j: dict) -> dict:
    contact = " ".join(x for x in (_s(j.get("contact_first_name"), 60), _s(j.get("contact_last_name"), 60)) if x)
    return {
        "number": _s(j.get("number"), 30),
        "customer_id": j.get("customer_id"),
        "customer_name": _s(j.get("customer_name")),
        "contact": contact,
        "status": _s(j.get("status"), 60),
        "sub_status": _s(j.get("sub_status"), 60),
        "start_date": _date(j.get("start_date")),
        "category": _s(j.get("category"), 60),
        "description": _s(j.get("description"), 160),
        "po_number": _s(j.get("po_number"), 60),
        "street_1": _s(j.get("street_1")), "street_2": _s(j.get("street_2")),
        "city": _s(j.get("city"), 80), "state": _s(j.get("state_prov"), 30), "zip": _s(j.get("postal_code"), 20),
        "location_name": _s(j.get("location_name"), 80),
    }


def is_consult(row: dict) -> bool:
    return row["status"].lower() == "2 scheduled consult" or row["sub_status"].lower() == "consultation"


# ------------------------------------------------------------------ refresh
def refresh(reason: str = "schedule") -> dict:
    """Pull every open job from Service Fusion and replace the local copy. Keeps the old copy if anything fails."""
    if not configured():
        return {"ok": False, "error": "Service Fusion isn't connected yet."}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "A refresh is already running.", "busy": True}
    started = now_iso()
    set_setting("sf_last_attempt", started)
    try:
        rows, seen = [], set()
        for st in open_statuses():
            q = urllib.parse.urlencode({"filters[status]": st, "sort": "-created_at"})
            for j in _paged(f"/v1/jobs?{q}"):
                r = _job_row(j or {})
                if r["number"] and r["number"] not in seen and r["status"].lower() not in CLOSED:
                    seen.add(r["number"])
                    rows.append(r)
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM sf_jobs")
            c.executemany(
                "INSERT INTO sf_jobs(number, customer_id, customer_name, status, sub_status, start_date, search, data, synced_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [(r["number"], r["customer_id"], r["customer_name"], r["status"], r["sub_status"], r["start_date"],
                  (r["customer_name"] + " " + r["contact"]).lower(), json.dumps(r), started) for r in rows])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        set_setting("sf_last_ok", started)
        set_setting("sf_last_error", "")
        set_setting("sf_fail_count", "0")
        set_setting("sf_job_count", str(len(rows)))
        return {"ok": True, "jobs": len(rows), "at": started}
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300] if isinstance(e, SFError) else f"Refresh failed ({type(e).__name__})"
        fails = int(get_setting("sf_fail_count") or "0") + 1
        set_setting("sf_fail_count", str(fails))
        set_setting("sf_last_error", msg)
        if fails >= 3:
            alerts.push_throttled("sf-refresh", "Ops app: job lookup not refreshing",
                                  f"Service Fusion refresh failed {fails} times in a row: {msg}", "default",
                                  every_seconds=6 * 3600)
        return {"ok": False, "error": msg}
    finally:
        _lock.release()


def _due(now_local: datetime) -> bool:
    last = get_setting("sf_last_attempt")
    if not get_setting("sf_last_ok"):
        return not last or _age_seconds(last) > 15 * 60          # never synced: keep trying every 15 min
    in_hours = now_local.weekday() in WORK_DAYS and WORK_HOURS[0] <= now_local.hour < WORK_HOURS[1]
    return in_hours and (not last or _age_seconds(last) >= REFRESH_MINUTES * 60)


def _age_seconds(iso: str) -> float:
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return 1e9


def worker() -> None:
    time.sleep(20)
    while True:
        try:
            if configured() and _due(datetime.now(TZ)):
                refresh("schedule")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def start_worker() -> None:
    if configured():
        threading.Thread(target=worker, daemon=True, name="sf-refresh").start()


def manual_refresh() -> dict:
    last = get_setting("sf_last_attempt")
    if last and _age_seconds(last) < MANUAL_MIN_SECONDS:
        return {"ok": True, "skipped": True, **status()}
    r = refresh("manual")
    return {**r, **status()}


def status() -> dict:
    return {"connected": configured(), "last_ok": get_setting("sf_last_ok") or None,
            "last_error": get_setting("sf_last_error") or None,
            "jobs": int(get_setting("sf_job_count") or "0")}


# ------------------------------------------------------------------ search + pick
def _brief(r: dict) -> dict:
    """What a search result shows: no address, phone or email."""
    n = r["number"]
    return {"number": n, "last4": n[-4:], "customer": r["customer_name"], "status": r["status"],
            "date": r["start_date"], "category": r["category"]}


def _date_distance(d: str, today: str) -> int:
    if not d:
        return 10 ** 6
    try:
        return abs((datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days)
    except Exception:
        return 10 ** 6


def search(form: str, q: str, limit: int = 25) -> list[dict]:
    rows = [json.loads(r["data"]) for r in conn().execute("SELECT data FROM sf_jobs")]
    if form == "measure":
        rows = [r for r in rows if is_consult(r)]
    q = (q or "").strip().lower()
    if q:
        digits = re.sub(r"\D", "", q)
        if digits and digits == q.replace(" ", "").replace("#", ""):
            rows = [r for r in rows if r["number"].endswith(digits) or (len(digits) >= 5 and digits in r["number"])]
        else:
            words = q.split()
            rows = [r for r in rows if all(w in (r["customer_name"] + " " + r["contact"]).lower() for w in words)]
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if form == "install":
        rows.sort(key=lambda r: (0 if r["status"].lower() in INSTALL_STAGE else 1, _date_distance(r["start_date"], today),
                                 r["customer_name"].lower()))
        if not q:     # nothing typed: only the jobs most likely to be today's install
            rows = [r for r in rows if r["status"].lower() in INSTALL_STAGE
                    or (not is_consult(r) and _date_distance(r["start_date"], today) <= 14)]
    else:
        rows.sort(key=lambda r: (_date_distance(r["start_date"], today), r["customer_name"].lower()))
    return [_brief(r) for r in rows[:limit]]


def _contact(customer_id) -> dict:
    """Primary email/phone and service address from the customer record, cached for a day."""
    if not customer_id:
        return {}
    c = conn()
    row = c.execute("SELECT data, fetched_at FROM sf_contacts WHERE customer_id=?", (customer_id,)).fetchone()
    if row and _age_seconds(row["fetched_at"]) < CONTACT_TTL.total_seconds():
        return json.loads(row["data"])
    cust = _get(f"/v1/customers/{int(customer_id)}?expand=contacts,contacts.emails,contacts.phones,locations")
    if isinstance(cust, dict) and "items" in cust:
        cust = (cust.get("items") or [{}])[0]
    cust = cust or {}
    contacts = cust.get("contacts") or []
    primary = next((x for x in contacts if x.get("is_primary")), contacts[0] if contacts else {})
    pool = [primary] + [x for x in contacts if x is not primary]
    email = next((e.get("email") for x in pool for e in (x.get("emails") or []) if e.get("email")), "")
    phone = next((p.get("phone") for x in pool for p in (x.get("phones") or []) if p.get("phone")), "")
    locs = cust.get("locations") or []
    loc = next((x for x in locs if x.get("is_primary")), locs[0] if locs else {})
    out = {"email": _s(email, 200), "phone": _s(phone, 40),
           "contact": " ".join(x for x in (_s(primary.get("fname"), 60), _s(primary.get("lname"), 60)) if x),
           "street_1": _s(loc.get("street_1")), "street_2": _s(loc.get("street_2")), "city": _s(loc.get("city"), 80),
           "state": _s(loc.get("state_prov"), 30), "zip": _s(loc.get("postal_code"), 20)}
    c.execute("INSERT INTO sf_contacts(customer_id, data, fetched_at) VALUES (?,?,?) "
              "ON CONFLICT(customer_id) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
              (customer_id, json.dumps(out), now_iso()))
    return out


def details(number: str) -> dict | None:
    row = conn().execute("SELECT data FROM sf_jobs WHERE number=?", (number,)).fetchone()
    if not row:
        return None
    j = json.loads(row["data"])
    note = ""
    try:
        ct = _contact(j.get("customer_id"))
    except SFError:
        ct, note = {}, "Couldn't reach Service Fusion for the email and phone. Type them in if you need them."
    has_job_addr = bool(j["street_1"] or j["city"])
    addr = j if has_job_addr else ct
    line1 = ", ".join(x for x in (addr.get("street_1"), addr.get("street_2")) if x)
    line2 = " ".join(x for x in (addr.get("city", "") + ("," if addr.get("city") and addr.get("state") else ""),
                                 addr.get("state", ""), addr.get("zip", "")) if x).strip()
    return {**_brief(j), "contact": j["contact"] or ct.get("contact", ""), "email": ct.get("email", ""),
            "phone": ct.get("phone", ""), "address": ", ".join(x for x in (line1, line2) if x),
            "description": j["description"], "note": note}


def clear_contacts() -> None:
    conn().execute("DELETE FROM sf_contacts")

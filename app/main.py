"""SimplyDoors Operations app — web server.

Staff page:  <base>/            Admin page: <base>/admin
Everything is served under BASE_PATH (default /ops) so it can share the
OptiPlex's public address with the Sign app.
"""
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import warnings

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 40_000_000           # phone photos are ~12-50 MP; refuse anything bigger
warnings.simplefilter("error", Image.DecompressionBombWarning)
PHOTO_FORMATS = ["JPEG", "PNG", "WEBP"]

from . import alerts, auth, geo, mailer
from .db import DATA_DIR, DB_PATH, audit, conn, init_db, now_iso
from . import forms as forms_mod
from .forms import (FORM_BY_SLUG, FORMS, EXTRA_RULES, LIST_LABELS, DEFAULT_LISTS, clean, get_list, photo_minimums,
                    photo_slots, public_spec, recipients_for, set_list, split_recipients, subject_for, summary, visible_forms)
from .pdf import build_pdf

BASE_PATH = os.environ.get("BASE_PATH", "/ops").rstrip("/")
COOKIE = "sdops_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"
STATIC = os.path.join(os.path.dirname(__file__), "static")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_REQUEST_BYTES = 150 * 1024 * 1024   # a big measure job can carry 100+ photos
APP_VERSION = "stage3-16"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- plumbing
@app.middleware("http")
async def base_path_and_headers(request: Request, call_next):
    # Works whether or not the proxy strips the /ops prefix.
    path = request.scope["path"]
    try:
        if int(request.headers.get("content-length", "0")) > MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "That upload is too large."}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "Bad request"}, status_code=400)
    if BASE_PATH and (path == BASE_PATH or path.startswith(BASE_PATH + "/")):
        request.scope["path"] = path[len(BASE_PATH):] or "/"
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
    if request.scope["path"].startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()[:64]
    return request.client.host if request.client else "?"


def ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]


def require_app_header(request: Request):
    # Blocks other websites from posting to this app with a staff member's cookie.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-sd-app") != "1":
        raise HTTPException(403, "Missing app header")


def current_staff(request: Request):
    require_app_header(request)
    row = auth.session_staff(request.cookies.get(COOKIE))
    if not row:
        raise HTTPException(401, "Please sign in again.")
    return row


def current_admin(request: Request):
    row = current_staff(request)
    if not row["is_admin"]:
        audit(row["id"], row["name"], "admin_denied", request.url.path, None, client_ip(request), ua(request))
        raise HTTPException(403, "Admins only.")
    return row


def page(name: str) -> HTMLResponse:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read().replace("{{BASE}}", (BASE_PATH or "") + "/").replace("{{VERSION}}", APP_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- pages
@app.get("/", include_in_schema=False)
def staff_page():
    return page("index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return page("admin.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    with open(os.path.join(STATIC, "sw.js"), encoding="utf-8") as f:
        js = f.read().replace("{{VERSION}}", APP_VERSION)
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    base = (BASE_PATH or "") + "/"
    return JSONResponse({
        "name": "SimplyDoors Operations", "short_name": "SD Ops", "start_url": base, "scope": base,
        "display": "standalone", "background_color": "#f4f7f6", "theme_color": "#76c043",
        "icons": [{"src": base + "static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": base + "static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
        raise HTTPException(404)
    p = os.path.join(STATIC, name)
    if not os.path.isfile(p) or name.endswith(".html"):
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/healthz", include_in_schema=False)
def healthz():
    conn().execute("SELECT 1").fetchone()
    return {"ok": True, "version": APP_VERSION}


# ---------------------------------------------------------------- sign in
@app.get("/api/directory")
def directory():
    rows = conn().execute("SELECT name, dept FROM staff WHERE active=1 ORDER BY dept, name").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["dept"], []).append(r["name"])
    return out


class LoginBody(BaseModel):
    name: str = ""
    pin: str = ""


@app.post("/api/login")
def login(request: Request, body: LoginBody):
    require_app_header(request)
    name, pin = body.name[:80], body.pin[:12]
    if not name or not pin:
        raise HTTPException(400, "Pick your name and enter your PIN.")
    row, msg, newly_locked = auth.attempt_login(name, pin, client_ip(request), ua(request))
    if newly_locked:
        alerts.push_throttled(f"lock:{name}", "Ops app: account locked", f"{name} was locked after too many wrong PINs. {msg}", "high")
    if not row:
        raise HTTPException(401, msg)
    token, max_age = auth.create_session(row, client_ip(request), ua(request))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=max_age, httponly=True, secure=COOKIE_SECURE, samesite="lax",
                    path=(BASE_PATH or "") + "/")
    return resp


# ---------------------------------------------------------------- first-time setup (invite link)
class SetupBody(BaseModel):
    code: str = ""
    pin: str = ""


def _setup_fail(request: Request, what: str):
    conn().execute("INSERT INTO ip_failures(ip, at) VALUES (?,?)", (client_ip(request), now_iso()))
    audit(None, "unknown", "setup_code_rejected", None, {"step": what}, client_ip(request), ua(request))
    raise HTTPException(400, "That setup link or code isn't valid. It may have expired or already been used. "
                             "Ask Adem or Paz for a new one.")


@app.post("/api/setup/check")
def setup_check(request: Request, body: SetupBody):
    require_app_header(request)
    if auth.ip_blocked(client_ip(request)):
        raise HTTPException(429, "Too many tries from this connection. Wait 15 minutes.")
    inv = auth.find_invite(body.code[:40])
    if not inv:
        _setup_fail(request, "check")
    return {"name": inv["name"], "expires_at": inv["expires_at"]}


@app.post("/api/setup/complete")
def setup_complete(request: Request, body: SetupBody):
    require_app_header(request)
    if auth.ip_blocked(client_ip(request)):
        raise HTTPException(429, "Too many tries from this connection. Wait 15 minutes.")
    inv = auth.find_invite(body.code[:40])
    if not inv:
        _setup_fail(request, "complete")
    pin = body.pin.strip()
    if not auth.valid_pin_format(pin):
        raise HTTPException(422, "Your PIN must be 6 to 8 digits.")
    if auth.weak_pin(pin):
        raise HTTPException(422, "That PIN is too easy to guess. Avoid repeats and runs like 111111 or 123456.")
    c = conn()
    cur = c.execute("UPDATE invites SET used_at=? WHERE id=? AND used_at IS NULL", (now_iso(), inv["id"]))
    if cur.rowcount != 1:
        _setup_fail(request, "complete-race")
    auth.set_pin(inv["staff_id"], pin, "self")
    auth.end_all_sessions(inv["staff_id"])
    audit(inv["staff_id"], inv["name"], "pin_created_by_staff", inv["name"], {"invite_from": inv["created_by"]},
          client_ip(request), ua(request))
    row = c.execute("SELECT * FROM staff WHERE id=?", (inv["staff_id"],)).fetchone()
    token, max_age = auth.create_session(row, client_ip(request), ua(request))
    audit(row["id"], row["name"], "login_ok", row["name"], {"via": "setup"}, client_ip(request), ua(request))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=max_age, httponly=True, secure=COOKIE_SECURE, samesite="lax",
                    path=(BASE_PATH or "") + "/")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    require_app_header(request)
    row = auth.session_staff(request.cookies.get(COOKIE))
    auth.end_session(request.cookies.get(COOKIE))
    if row:
        audit(row["id"], row["name"], "logout", row["name"], None, client_ip(request), ua(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path=(BASE_PATH or "") + "/")
    return resp


@app.get("/api/me")
def me(staff=Depends(current_staff)):
    return {
        "id": staff["id"], "name": staff["name"], "dept": staff["dept"], "is_admin": bool(staff["is_admin"]),
        "is_owner": bool(staff["is_owner"]),
        "forms": [public_spec(t) for t in visible_forms(staff)],
    }


# ---------------------------------------------------------------- reports
def _save_photo(upload_bytes: bytes, dest: str, g: dict | None = None, receipt: str = "", who: str = "") -> int:
    """Re-saves the photo as a clean JPEG with the time/location stamp printed on it.
    Hidden file data is dropped; the location kept is the one the app recorded."""
    with Image.open(io.BytesIO(upload_bytes), formats=PHOTO_FORMATS) as im:
        im.draft("RGB", (2000, 2000))           # JPEG: decode at reduced size, saves memory
        im.thumbnail((2000, 2000))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))   # signatures: ink on white, not on black
            bg.paste(im, mask=im.split()[-1])
            im = bg
        im = im.convert("RGB")
        if g is not None:
            im = geo.stamp(im, g, receipt, who)
        im.save(dest, "JPEG", quality=85, optimize=True)
    return os.path.getsize(dest)


def _check_photo(b: bytes) -> bool:
    """Fully decode once so a damaged photo is refused now (422), not half-saved later."""
    try:
        with Image.open(io.BytesIO(b), formats=PHOTO_FORMATS) as im:
            im.draft("RGB", (2000, 2000))
            im.load()
        return True
    except Exception:
        return False


@app.post("/api/reports/{slug}")
async def submit_report(slug: str, request: Request, staff=Depends(current_staff)):
    form_type = FORM_BY_SLUG.get(slug)
    if not form_type:
        raise HTTPException(404, "Unknown form")
    spec = FORMS[form_type]
    if not staff["is_admin"] and form_type not in visible_forms(staff):
        raise HTTPException(403, "This form isn't switched on yet. Ask Adem or Paz.")
    if spec.get("admin_only") and not staff["is_admin"]:
        audit(staff["id"], staff["name"], "admin_denied", f"form:{slug}", None, client_ip(request), ua(request))
        raise HTTPException(403, "Only admins can file this form.")
    form = await request.form()
    submission_id = str(form.get("submission_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", submission_id):
        raise HTTPException(400, "Missing submission id")

    c = conn()
    existing = c.execute("SELECT id, receipt, staff_id FROM reports WHERE submission_id=?", (submission_id,)).fetchone()
    if existing:
        if existing["staff_id"] != staff["id"]:
            raise HTTPException(409, "Submission id already used")
        return {"ok": True, "receipt": existing["receipt"], "duplicate": True}

    raw = {k: v for k, v in form.items() if isinstance(v, str)}
    data, errors = clean(form_type, raw)
    kept = []
    if spec.get("kind") == "measure":
        _measure_people(c, staff, data, str(form.get("revision_of") or "").strip(), errors)

    photo_blobs = []
    for ps in photo_slots(form_type, data):
        slot, _label = ps["slot"], ps["label"]
        f = form.get(slot)
        if f is None or isinstance(f, str):
            continue
        if f.size is not None and f.size > MAX_PHOTO_BYTES:
            errors.append(f"{_label} is too large.")
            continue
        b = await f.read(MAX_PHOTO_BYTES + 1)
        if not b:
            continue
        if len(b) > MAX_PHOTO_BYTES:
            errors.append(f"{_label} is too large.")
            continue
        if not await run_in_threadpool(_check_photo, b):
            errors.append(f"{_label} could not be read as a photo. Please take it again.")
            continue
        g = None if ps["signature"] else geo.parse_geo(str(form.get(f"geo_{slot}") or ""), now_iso())
        photo_blobs.append((slot, b, g))
    got = {s for s, _, _ in photo_blobs}
    for g in spec.get("photos", []):
        cond = g.get("show_if")
        shown = not cond or str(raw.get(cond["field"]) or "") in cond["in"]
        if g.get("signature") and g.get("required") and shown and g["signature"] not in got:
            errors.append(f"{g['title']} is needed.")
    if spec.get("kind") == "measure":
        kept = _kept_photos(c, staff, form.get("keep"), {p["slot"] for p in photo_slots(form_type, data)} - got, errors)
    for title, slots, need in photo_minimums(form_type):
        have = len(got.intersection(slots))
        if have < need:
            errors.append(f"Add {need - have} more photo{'s' if need - have > 1 else ''} under “{title}”.")
    if errors:
        raise HTTPException(422, " ".join(errors))

    started_at = str(form.get("started_at", ""))[:40] or None
    queued = 1 if str(form.get("queued", "")) == "1" else 0
    return await run_in_threadpool(_store_report, form_type, spec, staff, submission_id, data, photo_blobs,
                                   started_at, queued, client_ip(request), ua(request), kept)


def _store_report(form_type, spec, staff, submission_id, data, photo_blobs, started_at, queued, ip, agent, kept=()):
    c = conn()
    rid = None
    try:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "INSERT INTO reports(submission_id, form_type, staff_id, submitted_at, started_at, queued_on_phone, data)"
            " VALUES (?,?,?,?,?,?,?)",
            (submission_id, form_type, staff["id"], now_iso(), started_at, queued, json.dumps(data, ensure_ascii=False)))
        rid = cur.lastrowid
        # each form counts on its own (VIN-00001, VIN-00002 ...), carrying on from any earlier numbers
        last = c.execute("SELECT MAX(CAST(substr(receipt, ?) AS INTEGER)) FROM reports WHERE receipt LIKE ?",
                         (len(spec["prefix"]) + 2, spec["prefix"] + "-%")).fetchone()[0]
        receipt = f"{spec['prefix']}-{(last or 0) + 1:05d}"
        c.execute("UPDATE reports SET receipt=? WHERE id=?", (receipt, rid))
        folder = os.path.join(PHOTO_DIR, str(rid))
        os.makedirs(folder, exist_ok=True)
        for slot, b, g in photo_blobs:
            dest = os.path.join(folder, f"{slot}.jpg")
            size = _save_photo(b, dest, g, receipt, staff["name"])
            if g is None:      # signature: no stamp, no location
                c.execute("INSERT INTO photos(report_id, slot, path, bytes, taken_at, geo_status) VALUES (?,?,?,?,?,?)",
                          (rid, slot, dest, size, now_iso(), "signature"))
                continue
            c.execute("INSERT INTO photos(report_id, slot, path, bytes, taken_at, lat, lon, acc, geo_status, file_age)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rid, slot, dest, size, g["taken_at"], g["lat"], g["lon"], g["acc"], g["status"], g["file_age"]))
        for slot, src in kept:     # revised measure: photos carried over from the earlier version, stamps unchanged
            dest = os.path.join(folder, f"{slot}.jpg")
            shutil.copyfile(src["path"], dest)
            c.execute("INSERT INTO photos(report_id, slot, path, bytes, taken_at, lat, lon, acc, geo_status, file_age)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rid, slot, dest, os.path.getsize(dest), src["taken_at"], src["lat"], src["lon"], src["acc"],
                       src["geo_status"], src["file_age"]))
        no_loc = sum(1 for _, _, g in photo_blobs if g is not None and g["status"] != "ok")
        details = {"form": form_type, "receipt": receipt, "photos": len(photo_blobs), "photos_without_location": no_loc,
                   "queued_on_phone": bool(queued)}
        if kept:
            details["photos_carried_over"] = len(kept)
        if data.get("revision_of"):
            details["revision_of"] = data["revision_of"]
        audit(staff["id"], staff["name"], "report_submitted", f"report:{rid}", details, ip, agent)
        to, bcc = split_recipients(form_type, data)
        mailer.queue_report_email(rid, to, subject_for(form_type, data, staff["name"], receipt), bcc)
        c.execute("COMMIT")
    except sqlite3.IntegrityError:
        c.execute("ROLLBACK")
        if rid:
            shutil.rmtree(os.path.join(PHOTO_DIR, str(rid)), ignore_errors=True)
        existing = c.execute("SELECT receipt FROM reports WHERE submission_id=?", (submission_id,)).fetchone()
        return {"ok": True, "receipt": existing["receipt"] if existing else None, "duplicate": True}
    except Exception:
        c.execute("ROLLBACK")
        if rid:
            shutil.rmtree(os.path.join(PHOTO_DIR, str(rid)), ignore_errors=True)
        raise
    mailer._wake.set()
    return {"ok": True, "receipt": receipt, "duplicate": False}


# ---------------------------------------------------------------- measures: who measured, revisions, reopening
def _can_open_measure(staff, report) -> bool:
    if staff["is_admin"] or report["staff_id"] == staff["id"]:
        return True
    try:
        return json.loads(report["data"]).get("measured_by_id") == staff["id"]
    except Exception:
        return False


def _measure_people(c, staff, data, revision_of, errors):
    """Who measured stays with the job through revisions; whoever fixes someone else's measure is 'Revised by'."""
    if revision_of:
        orig = c.execute("SELECT * FROM reports WHERE receipt=? AND form_type='Measure Report'", (revision_of,)).fetchone()
        if not orig or not _can_open_measure(staff, orig):
            errors.append(f"The measure being revised ({revision_of[:20]}) couldn't be found.")
            return
        od = json.loads(orig["data"])
        data["revision_of"] = orig["receipt"]
        data["measured_by"] = od.get("measured_by") or staff["name"]
        data["measured_by_id"] = od.get("measured_by_id", staff["id"])
        data["measured_by_email"] = od.get("measured_by_email") or ""
        if data["measured_by_id"] != staff["id"]:
            data["revised_by"], data["revised_by_email"] = staff["name"], staff["email"] or ""
        return
    data["measured_by"], data["measured_by_id"], data["measured_by_email"] = staff["name"], staff["id"], staff["email"] or ""


def _kept_photos(c, staff, raw, open_slots, errors) -> list:
    """Photos the phone asked to carry over from an earlier version: [(slot, photo row)]."""
    try:
        keep = json.loads(str(raw or "{}"))
    except Exception:
        keep = None
    if not isinstance(keep, dict):
        errors.append("The kept photos list couldn't be read.")
        return []
    out = []
    for slot, pid in list(keep.items())[:400]:
        if slot not in open_slots:
            continue
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        row = c.execute("SELECT p.*, r.staff_id, r.data, r.form_type FROM photos p JOIN reports r ON r.id=p.report_id"
                        " WHERE p.id=?", (pid,)).fetchone()
        if not row or row["form_type"] != "Measure Report" or not _can_open_measure(staff, row) \
                or not os.path.isfile(row["path"]):
            errors.append("An earlier photo couldn't be found. Take it again or remove it.")
            continue
        out.append((slot, row))
    return out


def _measure_allowed(staff):
    if "Measure Report" not in visible_forms(staff):
        raise HTTPException(403, "Measure isn't switched on yet. Ask Adem or Paz.")


@app.get("/api/measures")
def list_measures(staff=Depends(current_staff)):
    _measure_allowed(staff)
    c = conn()
    q = ("SELECT r.id, r.receipt, r.submitted_at, r.staff_id, r.data, s.name AS staff_name FROM reports r"
         " JOIN staff s ON s.id=r.staff_id WHERE r.form_type='Measure Report'")
    args: list = []
    if not staff["is_admin"]:
        q += " AND (r.staff_id=? OR json_extract(r.data, '$.measured_by_id')=?)"
        args += [staff["id"], staff["id"]]
    rows = c.execute(q + " ORDER BY r.id DESC LIMIT 150", args).fetchall()
    replaced = {}
    for x in c.execute("SELECT receipt, json_extract(data, '$.revision_of') AS of FROM reports"
                       " WHERE form_type='Measure Report' AND json_extract(data, '$.revision_of') IS NOT NULL ORDER BY id"):
        replaced[x["of"]] = x["receipt"]
    out = []
    for r in rows:
        d = json.loads(r["data"])
        out.append({"id": r["id"], "receipt": r["receipt"], "submitted_at": r["submitted_at"],
                    "customer": d.get("customer"), "po": d.get("po"), "date": d.get("date"),
                    "measured_by": d.get("measured_by") or r["staff_name"], "doors": d.get("doors", 0),
                    "windows": d.get("windows", 0), "revision_of": d.get("revision_of"),
                    "replaced_by": replaced.get(r["receipt"])})
    return out


@app.get("/api/measures/{rid}")
def open_measure(rid: int, request: Request, staff=Depends(current_staff)):
    _measure_allowed(staff)
    c = conn()
    r = c.execute("SELECT * FROM reports WHERE id=? AND form_type='Measure Report'", (rid,)).fetchone()
    if not r or not _can_open_measure(staff, r):
        raise HTTPException(404, "That measure couldn't be found.")
    photos = c.execute("SELECT id, slot FROM photos WHERE report_id=? ORDER BY id", (rid,)).fetchall()
    audit(staff["id"], staff["name"], "measure_reopened", f"report:{rid}", {"receipt": r["receipt"]},
          client_ip(request), ua(request))
    return {"id": r["id"], "receipt": r["receipt"], "data": json.loads(r["data"]),
            "photos": [{"id": p["id"], "slot": p["slot"]} for p in photos]}


@app.get("/api/measure-photos/{pid}")
def measure_photo(pid: int, request: Request, staff=Depends(current_staff)):
    p = conn().execute("SELECT p.path, r.staff_id, r.data, r.form_type FROM photos p JOIN reports r ON r.id=p.report_id"
                       " WHERE p.id=?", (pid,)).fetchone()
    if not p or p["form_type"] != "Measure Report" or not _can_open_measure(staff, p) or not os.path.isfile(p["path"]):
        raise HTTPException(404)
    return FileResponse(p["path"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/my-reports")
def my_reports(staff=Depends(current_staff)):
    rows = conn().execute(
        "SELECT receipt, form_type, submitted_at FROM reports WHERE staff_id=? ORDER BY id DESC LIMIT 20",
        (staff["id"],)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- admin: activity log
def _audit_query(params) -> tuple[str, list]:
    where, args = [], []
    if params.get("person"):
        where.append("(actor_name LIKE ? OR target LIKE ?)")
        args += [f"%{params['person']}%"] * 2
    if params.get("action"):
        where.append("action LIKE ?")
        args.append(f"%{params['action']}%")
    if params.get("from"):
        where.append("at >= ?")
        args.append(params["from"] + "T00:00:00Z" if len(params["from"]) == 10 else params["from"])
    if params.get("to"):
        where.append("at <= ?")
        args.append(params["to"] + "T23:59:59Z" if len(params["to"]) == 10 else params["to"])
    return (" WHERE " + " AND ".join(where)) if where else "", args


@app.get("/api/admin/audit")
def admin_audit(request: Request, admin=Depends(current_admin)):
    p = dict(request.query_params)
    w, args = _audit_query(p)
    try:
        limit = max(1, min(int(p.get("limit", 200)), 1000))
        offset = max(int(p.get("offset", 0)), 0)
    except ValueError:
        raise HTTPException(400, "Bad limit/offset")
    if offset == 0:
        audit(admin["id"], admin["name"], "audit_viewed", None,
              {k: v for k, v in p.items() if k in ("person", "action", "from", "to")} or None,
              client_ip(request), ua(request))
    c = conn()
    total = c.execute(f"SELECT COUNT(*) FROM audit{w}", args).fetchone()[0]
    rows = c.execute(f"SELECT * FROM audit{w} ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


@app.get("/api/admin/audit.csv")
def admin_audit_csv(request: Request, admin=Depends(current_admin)):
    w, args = _audit_query(dict(request.query_params))
    rows = conn().execute(f"SELECT * FROM audit{w} ORDER BY id", args).fetchall()
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["id", "time_utc", "person", "action", "target", "details", "ip", "device"])
    def safe(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    for r in rows:
        wr.writerow([safe(r[k]) for k in ("id", "at", "actor_name", "action", "target", "details", "ip", "user_agent")])
    audit(admin["id"], admin["name"], "audit_exported", None, {"rows": len(rows)}, client_ip(request), ua(request))
    return PlainTextResponse(out.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=activity-log.csv"})


# ---------------------------------------------------------------- admin: reports
@app.get("/api/admin/reports")
def admin_reports(request: Request, admin=Depends(current_admin)):
    p = dict(request.query_params)
    where, args = [], []
    if p.get("form"):
        where.append("r.form_type=?")
        args.append(p["form"])
    if p.get("q"):
        where.append("(r.receipt LIKE ? OR s.name LIKE ? OR r.data LIKE ?)")
        args += [f"%{p['q']}%"] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn().execute(
        f"SELECT r.id, r.receipt, r.form_type, r.submitted_at, r.queued_on_phone, s.name AS staff_name, r.data,"
        f" (SELECT status FROM emails e WHERE e.report_id=r.id ORDER BY e.id DESC LIMIT 1) AS email_status,"
        f" (SELECT COUNT(*) FROM photos p WHERE p.report_id=r.id AND COALESCE(p.geo_status,'missing') NOT IN ('ok','signature')) AS no_geo"
        f" FROM reports r JOIN staff s ON s.id=r.staff_id{w} ORDER BY r.id DESC LIMIT 300", args).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        summary = forms_mod.summary(r["form_type"], d)
        out.append({k: r[k] for k in ("id", "receipt", "form_type", "submitted_at", "queued_on_phone",
                                      "staff_name", "email_status", "no_geo")} | {"summary": summary})
    return out


@app.get("/api/admin/reports/{rid}")
def admin_report(rid: int, request: Request, admin=Depends(current_admin)):
    c = conn()
    r = c.execute("SELECT r.*, s.name AS staff_name FROM reports r JOIN staff s ON s.id=r.staff_id WHERE r.id=?",
                  (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    from .forms import display_rows
    data = json.loads(r["data"])
    photos = c.execute("SELECT id, slot, taken_at, lat, lon, acc, geo_status, file_age FROM photos"
                       " WHERE report_id=? ORDER BY id", (rid,)).fetchall()
    emails = c.execute("SELECT id, recipients, subject, status, attempts, last_error, created_at, sent_at"
                       " FROM emails WHERE report_id=? ORDER BY id", (rid,)).fetchall()
    audit(admin["id"], admin["name"], "report_viewed", f"report:{rid}", {"receipt": r["receipt"],
          "form": r["form_type"]}, client_ip(request), ua(request))
    if r["form_type"] == "Disciplinary Action":
        alerts.push("Ops app: disciplinary record opened", f"{admin['name']} opened {r['receipt']}.", "high")
    labels = {p["slot"]: p["label"] for p in photo_slots(r["form_type"], data)}
    return {
        "id": r["id"], "receipt": r["receipt"], "form_type": r["form_type"], "staff_name": r["staff_name"],
        "submitted_at": r["submitted_at"], "started_at": r["started_at"], "queued_on_phone": r["queued_on_phone"],
        "rows": display_rows(r["form_type"], data),
        "photos": [{"id": p["id"], "slot": p["slot"], "label": labels.get(p["slot"], p["slot"]),
                    "located": p["geo_status"] in ("ok", "signature"), "signature": p["geo_status"] == "signature",
                    "lines": [] if p["geo_status"] == "signature" else geo.describe(_geo_row(p, r["submitted_at"])),
                    "map": geo.map_url(p["lat"], p["lon"]) if p["geo_status"] == "ok" else None} for p in photos],
        "emails": [dict(e) for e in emails],
    }


def _geo_row(p, fallback_time):
    return {"status": p["geo_status"] or "missing", "lat": p["lat"], "lon": p["lon"], "acc": p["acc"],
            "taken_at": p["taken_at"] or fallback_time, "file_age": p["file_age"]}


@app.get("/api/admin/reports/{rid}/pdf")
def admin_report_pdf(rid: int, request: Request, admin=Depends(current_admin)):
    c = conn()
    r = c.execute("SELECT r.*, s.name AS staff_name FROM reports r JOIN staff s ON s.id=r.staff_id WHERE r.id=?",
                  (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    photos = c.execute("SELECT * FROM photos WHERE report_id=? ORDER BY id", (rid,)).fetchall()
    pdf = build_pdf(r, r["staff_name"], json.loads(r["data"]), photos)
    audit(admin["id"], admin["name"], "report_pdf_downloaded", f"report:{rid}", {"receipt": r["receipt"]},
          client_ip(request), ua(request))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={r['receipt']}.pdf"})


@app.get("/api/admin/photos/{pid}")
def admin_photo(pid: int, request: Request, admin=Depends(current_admin)):
    p = conn().execute("SELECT p.path, p.slot, r.receipt, r.form_type FROM photos p JOIN reports r ON r.id=p.report_id"
                       " WHERE p.id=?", (pid,)).fetchone()
    if not p or not os.path.isfile(p["path"]):
        raise HTTPException(404)
    audit(admin["id"], admin["name"], "photo_viewed", f"photo:{pid}", {"receipt": p["receipt"], "slot": p["slot"]},
          client_ip(request), ua(request))
    return FileResponse(p["path"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/admin/reports/{rid}/resend")
def admin_resend(rid: int, request: Request, admin=Depends(current_admin)):
    if not conn().execute("SELECT 1 FROM reports WHERE id=?", (rid,)).fetchone():
        raise HTTPException(404)
    mailer.resend(rid, admin, client_ip(request), ua(request))
    return {"ok": True}


@app.post("/api/admin/emails/{eid}/retry")
def admin_retry_email(eid: int, request: Request, admin=Depends(current_admin)):
    c = conn()
    if not c.execute("SELECT 1 FROM emails WHERE id=? AND status!='sent'", (eid,)).fetchone():
        raise HTTPException(404)
    c.execute("UPDATE emails SET status='pending', next_try_at=? WHERE id=?", (now_iso(), eid))
    audit(admin["id"], admin["name"], "email_retry_requested", f"email:{eid}", None, client_ip(request), ua(request))
    mailer._wake.set()
    return {"ok": True}


# ---------------------------------------------------------------- admin: staff
def _staff_out(r, viewer=None):
    locked = bool(r["locked_until"] and r["locked_until"] > now_iso())
    inv = conn().execute("SELECT expires_at, used_at, revoked_at FROM invites WHERE staff_id=? ORDER BY id DESC LIMIT 1",
                         (r["id"],)).fetchone()
    invite = None
    if inv:
        invite = ("used" if inv["used_at"] else "replaced" if inv["revoked_at"]
                  else "expired" if inv["expires_at"] < now_iso() else "waiting")
    return {"id": r["id"], "name": r["name"], "dept": r["dept"], "email": r["email"],
            "is_admin": bool(r["is_admin"]), "is_owner": bool(r["is_owner"]),
            "sales_notify": bool(r["sales_notify"]), "active": bool(r["active"]), "has_pin": bool(r["pin_hash"]),
            "pin_set_at": r["pin_set_at"], "pin_source": r["pin_source"], "locked": locked, "invite": invite,
            "invite_expires": inv["expires_at"] if inv else None}


@app.get("/api/admin/staff")
def admin_staff(admin=Depends(current_admin)):
    return [_staff_out(r, admin) for r in conn().execute("SELECT * FROM staff ORDER BY active DESC, dept, name")]


EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def _validate_staff(body: dict, partial: bool):
    out = {}
    if "name" in body or not partial:
        name = str(body.get("name", "")).strip()
        if not (2 <= len(name) <= 80):
            raise HTTPException(422, "Name must be 2–80 characters.")
        out["name"] = name
    if "dept" in body or not partial:
        dept = str(body.get("dept", "")).strip()
        if not (2 <= len(dept) <= 40):
            raise HTTPException(422, "Department is required.")
        out["dept"] = dept
    if "email" in body:
        email = str(body.get("email", "")).strip()
        if email and not EMAIL_RE.match(email):
            raise HTTPException(422, "That email address doesn't look right.")
        out["email"] = email
    for k in ("is_admin", "sales_notify", "active"):
        if k in body:
            out[k] = 1 if body[k] else 0
    return out


@app.post("/api/admin/staff")
async def admin_add_staff(request: Request, admin=Depends(current_admin)):
    body = await request.json()
    v = _validate_staff(body, partial=False)
    try:
        cur = conn().execute(
            "INSERT INTO staff(name, dept, email, is_admin, sales_notify, active, created_at) VALUES (?,?,?,?,?,1,?)",
            (v["name"], v["dept"], v.get("email", ""), v.get("is_admin", 0), v.get("sales_notify", 0), now_iso()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Someone with that name already exists.")
    audit(admin["id"], admin["name"], "staff_added", v["name"], v, client_ip(request), ua(request))
    alerts.push("Ops app: staff added", f"{admin['name']} added {v['name']} ({v['dept']}).")
    return {"ok": True, "id": cur.lastrowid}


def _guard_other(admin, target, sid, what="PIN"):
    """Owner can manage anyone. Other admins can't touch the owner, or another admin's PIN/access."""
    if sid == admin["id"] or admin["is_owner"]:
        return
    if target["is_owner"]:
        raise HTTPException(403, "Only the app owner can change this account.")
    if target["is_admin"]:
        raise HTTPException(422, f"Another admin's {what} can only be changed by the app owner.")


@app.patch("/api/admin/staff/{sid}")
async def admin_edit_staff(sid: int, request: Request, admin=Depends(current_admin)):
    body = await request.json()
    c = conn()
    old = c.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone()
    if not old:
        raise HTTPException(404)
    v = _validate_staff(body, partial=True)
    if sid == admin["id"] and (v.get("is_admin") == 0 or v.get("active") == 0):
        raise HTTPException(422, "You can't remove your own admin access or turn off your own account.")
    if old["is_owner"] and sid != admin["id"]:
        raise HTTPException(403, "Only the app owner can change this account.")
    if old["is_admin"] and sid != admin["id"] and not admin["is_owner"] and ("is_admin" in v or v.get("active") == 0):
        if v.get("is_admin", 1) == 0 or v.get("active") == 0:
            raise HTTPException(422, "Another admin's access can only be removed by the app owner.")
    changes = {k: {"from": old[k], "to": val} for k, val in v.items() if old[k] != val}
    if not changes:
        return {"ok": True}
    try:
        c.execute(f"UPDATE staff SET {', '.join(k + '=?' for k in v)} WHERE id=?", list(v.values()) + [sid])
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Someone with that name already exists.")
    if "active" in changes or "is_admin" in changes:
        auth.end_all_sessions(sid)   # promoted, demoted or turned off: sign in again
    audit(admin["id"], admin["name"], "staff_changed", old["name"], changes, client_ip(request), ua(request))
    alerts.push("Ops app: staff changed", f"{admin['name']} changed {old['name']}: {', '.join(changes)}.")
    return {"ok": True}


@app.post("/api/admin/staff/{sid}/pin")
async def admin_set_pin(sid: int, request: Request, admin=Depends(current_admin)):
    body = await request.json()
    pin = str(body.get("pin", ""))
    if not auth.valid_pin_format(pin):
        raise HTTPException(422, "PIN must be 6 to 8 digits.")
    c = conn()
    row = c.execute("SELECT name, is_admin, is_owner FROM staff WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    _guard_other(admin, row, sid)
    auth.set_pin(sid, pin, "admin")
    auth.end_all_sessions(sid)
    audit(admin["id"], admin["name"], "pin_reset", row["name"], None, client_ip(request), ua(request))
    alerts.push("Ops app: PIN reset", f"{admin['name']} reset the PIN for {row['name']}.", "high")
    return {"ok": True}


@app.post("/api/admin/staff/{sid}/invite")
def admin_invite(sid: int, request: Request, admin=Depends(current_admin)):
    c = conn()
    row = c.execute("SELECT name, is_admin, is_owner, active FROM staff WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if not row["active"]:
        raise HTTPException(422, "Turn this person's account back on first.")
    _guard_other(admin, row, sid)
    code, expires = auth.new_invite(sid, admin["name"])
    audit(admin["id"], admin["name"], "invite_created", row["name"], {"expires": expires}, client_ip(request), ua(request))
    alerts.push("Ops app: setup link created", f"{admin['name']} created a setup link for {row['name']}.")
    pretty = code[:4] + "-" + code[4:]
    return {"name": row["name"], "code": pretty, "expires_at": expires}


@app.post("/api/admin/staff/{sid}/unlock")
def admin_unlock(sid: int, request: Request, admin=Depends(current_admin)):
    c = conn()
    row = c.execute("SELECT name FROM staff WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    c.execute("UPDATE staff SET failed_count=0, locked_until=NULL, lock_level=0 WHERE id=?", (sid,))
    audit(admin["id"], admin["name"], "account_unlocked", row["name"], None, client_ip(request), ua(request))
    return {"ok": True}


# ---------------------------------------------------------------- admin: email rules + status
@app.get("/api/admin/email-rules")
def admin_rules(admin=Depends(current_admin)):
    rows = conn().execute("SELECT form_type, recipients FROM email_rules ORDER BY form_type").fetchall()
    extra = {
        "Receiving Report": "Plus the sales rep picked on the form.",
        "Delivery Proof": "Plus the sales rep picked on the form.",
        "Vehicle Inspection": "Plus the list below when anything is marked Defective.",
        "Vehicle Inspection: when something is Defective": "Added to the Vehicle Inspection email only when an item is Defective.",
        "Disciplinary Action": "Plus the employee being written up.",
        "Measure Report": "Plus the person who measured (and whoever revised it).",
    }
    live = set(FORMS) | set(EXTRA_RULES)
    return [{"form_type": r["form_type"], "recipients": r["recipients"], "extra": extra.get(r["form_type"], ""),
             "live": r["form_type"] in live} for r in rows]


@app.put("/api/admin/email-rules")
async def admin_set_rule(request: Request, admin=Depends(current_admin)):
    body = await request.json()
    form_type = str(body.get("form_type", ""))
    c = conn()
    old = c.execute("SELECT recipients FROM email_rules WHERE form_type=?", (form_type,)).fetchone()
    if not old:
        raise HTTPException(404)
    parts = [p.strip() for p in str(body.get("recipients", "")).replace(";", ",").split(",") if p.strip()]
    bad = [p for p in parts if not EMAIL_RE.match(p)]
    if bad:
        raise HTTPException(422, "These don't look like email addresses: " + ", ".join(bad))
    new = ", ".join(parts)
    c.execute("UPDATE email_rules SET recipients=? WHERE form_type=?", (new, form_type))
    audit(admin["id"], admin["name"], "email_rule_changed", form_type, {"from": old["recipients"], "to": new},
          client_ip(request), ua(request))
    alerts.push("Ops app: email list changed", f"{admin['name']} changed who gets {form_type}.")
    return {"ok": True}


@app.get("/api/admin/my-copies")
def my_copies(admin=Depends(current_admin)):
    if not admin["is_owner"]:
        raise HTTPException(403, "Only the app owner has private copies.")
    on = set(forms_mod.owner_copies())
    names = [k for k, _ in sorted(FORMS.items(), key=lambda kv: kv[1]["order"])] + list(EXTRA_RULES)
    return {"email": admin["email"], "forms": [{"type": n, "on": n in on} for n in names]}


@app.put("/api/admin/my-copies")
async def set_my_copies(request: Request, admin=Depends(current_admin)):
    if not admin["is_owner"]:
        raise HTTPException(403, "Only the app owner has private copies.")
    body = await request.json()
    vals = forms_mod.set_owner_copies([str(x) for x in (body.get("forms") or [])])
    audit(admin["id"], admin["name"], "private_copies_changed", None, {"forms": vals}, client_ip(request), ua(request))
    return {"ok": True, "forms": vals}


@app.get("/api/admin/lists")
def admin_lists(admin=Depends(current_admin)):
    return {"lists": [{"name": n, "label": LIST_LABELS[n], "values": get_list(n)} for n in DEFAULT_LISTS],
            "forms": [{"type": k, "enabled": k in forms_mod.enabled_forms(), "admin_only": bool(v.get("admin_only"))}
                      for k, v in sorted(FORMS.items(), key=lambda kv: kv[1]["order"])]}


@app.put("/api/admin/forms-enabled")
async def admin_set_forms(request: Request, admin=Depends(current_admin)):
    body = await request.json()
    old = forms_mod.enabled_forms()
    new = forms_mod.set_enabled_forms([str(x) for x in (body.get("forms") or [])])
    audit(admin["id"], admin["name"], "forms_switched", None, {"from": old, "to": new}, client_ip(request), ua(request))
    alerts.push("Ops app: forms switched", f"{admin['name']} set staff forms to: {', '.join(new) or 'none'}.")
    return {"ok": True, "forms": new}


@app.put("/api/admin/lists/{name}")
async def admin_set_list(name: str, request: Request, admin=Depends(current_admin)):
    if name not in DEFAULT_LISTS:
        raise HTTPException(404)
    body = await request.json()
    vals = body.get("values")
    if not isinstance(vals, list) or len(vals) > 100:
        raise HTTPException(422, "Send one item per line (up to 100).")
    old = get_list(name)
    try:
        new = set_list(name, vals)
    except ValueError as e:
        raise HTTPException(422, str(e))
    audit(admin["id"], admin["name"], "list_changed", LIST_LABELS[name], {"from": old, "to": new},
          client_ip(request), ua(request))
    return {"ok": True, "values": new}


@app.get("/api/admin/status")
def admin_status(admin=Depends(current_admin)):
    c = conn()
    counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
    recent = c.execute("SELECT e.id, e.subject, e.recipients, e.status, e.attempts, e.last_error, e.created_at,"
                       " e.sent_at, e.report_id FROM emails e ORDER BY e.id DESC LIMIT 25").fetchall()
    no_pin = [r["name"] for r in c.execute("SELECT name FROM staff WHERE active=1 AND pin_hash IS NULL ORDER BY name")]
    du = shutil.disk_usage(DATA_DIR)
    photos_bytes = c.execute("SELECT COALESCE(SUM(bytes),0) FROM photos").fetchone()[0]
    return {
        "version": APP_VERSION, "email_configured": mailer.configured(), "email_from": mailer.SMTP_USER,
        "alerts_configured": bool(alerts.NTFY_URL),
        "emails": {"pending": counts.get("pending", 0), "sent": counts.get("sent", 0), "failed": counts.get("failed", 0)},
        "recent_emails": [dict(r) for r in recent], "staff_without_pin": no_pin,
        "disk_free_gb": round(du.free / 1e9, 1), "photos_mb": round(photos_bytes / 1e6, 1),
        "db_mb": round(os.path.getsize(DB_PATH) / 1e6, 2) if os.path.exists(DB_PATH) else 0,
        "reports": c.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        "last_backup": _last_backup(),
        "offsite": offsite_status(),
    }


# ---------------------------------------------------------------- housekeeping
BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def _last_backup():
    if not os.path.isdir(BACKUP_DIR):
        return None
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db") and not f.endswith(".part"))
    return files[-1] if files else None


SNAPSHOT_HOUR = 2          # local time; the off-site copy runs at 2:30, so it always has that night's snapshot


def snapshot_db(day: str) -> None:
    """Consistent copy of the live database, written under a temp name and renamed when complete."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"ops-{day}.db")
    tmp = dest + ".part"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    dst.close()
    src.close()
    os.replace(tmp, dest)
    for f in sorted(x for x in os.listdir(BACKUP_DIR) if x.endswith(".db"))[:-14]:
        os.remove(os.path.join(BACKUP_DIR, f))


def offsite_status() -> dict:
    try:
        with open(os.path.join(DATA_DIR, "offsite-status.json")) as f:
            st = json.load(f)
    except Exception:
        return {"configured": False}
    try:
        with open(os.path.join(DATA_DIR, ".offsite-lastok")) as f:
            st["last_ok"] = f.read().strip()
    except Exception:
        st["last_ok"] = None
    st["configured"] = True
    return st


def nightly():
    """Every 10 min: nightly database snapshot (picked up by the off-site copy), stale-backup alert, tidy-up."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.environ.get("TZ_DISPLAY", "America/Chicago"))
    while True:
        try:
            local_now = datetime.now(tz)
            day = local_now.strftime("%Y-%m-%d")
            have_any = os.path.isdir(BACKUP_DIR) and any(f.endswith(".db") for f in os.listdir(BACKUP_DIR))
            if (local_now.hour >= SNAPSHOT_HOUR or not have_any) and \
                    not os.path.exists(os.path.join(BACKUP_DIR, f"ops-{day}.db")):
                snapshot_db(day)
            st = offsite_status()
            if st["configured"]:
                last = st.get("last_ok")
                age_h = (datetime.now(timezone.utc) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)).total_seconds() / 3600 if last else 999
                if age_h > 36:
                    alerts.push_throttled("offsite-stale", "Ops app: off-site backup is behind",
                                          "Reports haven't been copied to Google Drive for over a day. Check Admin > Status.",
                                          "high", every_seconds=12 * 3600)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            c = conn()
            c.execute("DELETE FROM ip_failures WHERE at < ?", (cutoff,))
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        except Exception:
            pass
        time.sleep(600)


@app.on_event("startup")
def startup():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    init_db()
    audit(None, "system", "app_started", None, {"version": APP_VERSION})
    mailer.start_worker()
    threading.Thread(target=nightly, daemon=True, name="nightly").start()

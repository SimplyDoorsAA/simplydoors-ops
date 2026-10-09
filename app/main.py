"""SimplyDoors Operations app — web server.

Staff page:  <base>/            Admin page: <base>/admin
Everything is served under BASE_PATH (default /ops) so it can share the
OptiPlex's public address with the Sign app.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import warnings
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 40_000_000           # phone photos are ~12-50 MP; refuse anything bigger
warnings.simplefilter("error", Image.DecompressionBombWarning)
PHOTO_FORMATS = ["JPEG", "PNG", "WEBP"]

from . import alerts, auth, geo, leads, mailer, pricelist, sends, sfjobs
from .db import DATA_DIR, DB_PATH, audit, conn, delete_audit_rows, get_setting, init_db, now_iso, set_setting
from . import forms as forms_mod
from .forms import (FORM_BY_SLUG, FORMS, EXTRA_RULES, LIST_LABELS, DEFAULT_LISTS, clean, get_list, photo_minimums,
                    photo_slots, public_spec, set_list, split_recipients, subject_for, visible_forms)
from .pdf import build_pdf

BASE_PATH = os.environ.get("BASE_PATH", "/ops").rstrip("/")
COOKIE = "sdops_session"
DEVICE_COOKIE = "sdops_device"      # "this phone has signed in before"; outlives the session, never cleared on sign-out
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"
STATIC = os.path.join(os.path.dirname(__file__), "static")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_REQUEST_BYTES = 150 * 1024 * 1024   # a big measure job can carry 100+ photos
APP_VERSION = "stage3-50"


@asynccontextmanager
async def lifespan(_app):
    startup()          # defined at the bottom: database, mail and job-lookup workers, nightly snapshot
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


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
    if request.scope["path"].startswith(leads.INTAKE_PATH + "/api/") and request.method == "POST":
        # the customer form is the one upload anyone can send without signing in: its size is known up front
        if "content-length" not in request.headers:
            return JSONResponse({"detail": "Please send that again."}, status_code=411)
        if int(request.headers["content-length"]) > leads.MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "Those files are too big together. Send fewer or smaller ones."},
                                status_code=413)
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


def current_owner(request: Request):
    row = current_staff(request)
    if not row["is_owner"]:
        audit(row["id"], row["name"], "admin_denied", request.url.path, None, client_ip(request), ua(request))
        raise HTTPException(403, "Only the app owner can do this.")
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


@app.get("/pricelist", include_in_schema=False)
def pricelist_page():
    return page("pricelist.html")


@app.get("/leads", include_in_schema=False)
def leads_page():
    return page("leads.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    with open(os.path.join(STATIC, "sw.js"), encoding="utf-8") as f:
        js = f.read().replace("{{VERSION}}", APP_VERSION)
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    base = (BASE_PATH or "") + "/"
    icon = [{"src": base + "static/icon-192.png", "sizes": "192x192", "type": "image/png"}]
    return JSONResponse({
        "id": base, "name": "SimplyDoors Operations", "short_name": "SD Ops",
        "description": "Receiving, delivery, installs, measures and the other job reports.",
        "start_url": base, "scope": base, "display": "standalone",
        "background_color": "#1f2933", "theme_color": "#76c043",
        "icons": [{"src": base + "static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": base + "static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  # same picture with more room around it, so a phone can crop it to a circle without cutting "OPS"
                  {"src": base + "static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
        # long-press the app icon (Android) / right-click it (computer)
        "shortcuts": [{"name": n, "url": base + "?open=" + s, "icons": icon} for n, s in
                      (("Receiving Report", "receiving"), ("Delivery Proof", "delivery"),
                       ("Installation Completion", "install"), ("Measure Report", "measure"))],
    }, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


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
def _set_device_cookie(resp, request: Request, staff_id: int | None):
    """Remembers this phone so it gets the name list next time and its wrong PINs stay short locks."""
    token, max_age = auth.remember_device(request.cookies.get(DEVICE_COOKIE), staff_id, client_ip(request), ua(request))
    resp.set_cookie(DEVICE_COOKIE, token, max_age=max_age, httponly=True, secure=COOKIE_SECURE, samesite="lax",
                    path=(BASE_PATH or "") + "/")


@app.get("/api/directory")
def directory(request: Request):
    """Staff names by department, for the sign-in screen. Only for a phone that has signed in here before (or
    finished a setup link): the app is reachable from the internet, and the list shouldn't be. A new phone types the name."""
    if not auth.known_device(request.cookies.get(DEVICE_COOKIE)):
        raise HTTPException(403, "This phone hasn't signed in here before. Type your name.")
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
    known = auth.known_device(request.cookies.get(DEVICE_COOKIE))
    row, msg, newly_locked = auth.attempt_login(name, pin, client_ip(request), ua(request), known_device=known)
    if newly_locked:
        where = "" if known else " The wrong PINs came from a phone that has never signed in here."
        alerts.push_throttled(f"lock:{name}", "Ops app: account locked", f"{name} was locked after too many wrong PINs. {msg}{where}", "high")
    if not row:
        raise HTTPException(401, msg)
    token, max_age = auth.create_session(row, client_ip(request), ua(request))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=max_age, httponly=True, secure=COOKIE_SECURE, samesite="lax",
                    path=(BASE_PATH or "") + "/")
    _set_device_cookie(resp, request, row["id"])
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
    _set_device_cookie(resp, request, row["id"])
    return resp


@app.put("/api/owner/test-mode")
async def set_test_mode(request: Request, owner=Depends(current_owner)):
    require_app_header(request)
    on = bool((await request.json()).get("on"))
    set_setting("owner_test_mode", "1" if on else "0")
    audit(owner["id"], owner["name"], "test_mode_on" if on else "test_mode_off", None, None, client_ip(request), ua(request))
    return {"test_mode": on}


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


STUDIO_URL = os.environ.get("STUDIO_URL", "https://optiplex-ai.tailf0af63.ts.net/").strip()
STUDIO_SSO_SECRET = os.environ.get("STUDIO_SSO_SECRET", "").strip()
STUDIO_DEPTS = ("sales", "admin", "office")   # these get the full Studio; everyone else Edit + Design only


def shows_studio(staff) -> bool:
    """The Simply Studio tile: everyone with a work email, unless an admin switched it off for them."""
    if not STUDIO_URL:
        return False
    if staff["studio_link"] is not None:
        return bool(staff["studio_link"])
    return bool((staff["email"] or "").strip())


def studio_role(staff) -> str:
    return "full" if staff["is_admin"] or (staff["dept"] or "").strip().lower() in STUDIO_DEPTS else "field"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


@app.post("/api/studio-link")
def studio_link(request: Request, staff=Depends(current_staff)):
    """A one-time, two-minute link that signs this person into Simply Studio (Studio checks the signature)."""
    if not shows_studio(staff):
        raise HTTPException(403, "Simply Studio isn't switched on for you. Ask Adem.")
    if not STUDIO_SSO_SECRET or not (staff["email"] or "").strip():
        return {"url": STUDIO_URL}          # not set up yet: Studio's own sign-in page
    # owner: only the Field app owner may be signed into a Studio admin account (Studio checks this)
    claims = {"aud": "simply-studio", "email": staff["email"].strip().lower(), "name": staff["name"],
              "role": studio_role(staff), "owner": bool(staff["is_owner"]), "exp": int(time.time()) + 90,
              "n": secrets.token_urlsafe(18)}
    body = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(STUDIO_SSO_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    audit(staff["id"], staff["name"], "studio_opened", None, {"as": claims["role"]}, client_ip(request), ua(request))
    return {"url": STUDIO_URL.rstrip("/") + "/sso?t=" + body + "." + sig}


@app.get("/api/me")
def me(staff=Depends(current_staff)):
    return {
        "id": staff["id"], "name": staff["name"], "dept": staff["dept"], "is_admin": bool(staff["is_admin"]),
        "is_owner": bool(staff["is_owner"]),
        "test_mode": bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1",
        "studio_url": STUDIO_URL if shows_studio(staff) else None,
        "forms": [public_spec(t) for t in visible_forms(staff)],
        "job_lookup": sfjobs.configured(),
        "price_list": pricelist.allowed(staff),
        "price_edit": pricelist.can_edit(staff),
        "leads": leads.allowed(staff),
    }


# ---------------------------------------------------------------- job lookup (Service Fusion copy)
JOB_FORMS = {"install", "measure", "delivery", "po"}


@app.get("/api/jobs")
def job_search(request: Request, form: str = "install", q: str = "", staff=Depends(current_staff)):
    if not sfjobs.configured():
        return {"connected": False, "results": []}
    form = form if form in JOB_FORMS else "install"
    return {"results": sfjobs.search(form, q[:60]), **sfjobs.status()}


@app.post("/api/jobs/refresh")
async def job_refresh(request: Request, staff=Depends(current_staff)):
    if not sfjobs.configured():
        raise HTTPException(400, "The job lookup isn't connected to Service Fusion yet.")
    r = await run_in_threadpool(sfjobs.manual_refresh)
    if not r.get("skipped"):
        audit(staff["id"], staff["name"], "jobs_refreshed", None,
              {"ok": bool(r.get("ok")), "jobs": r.get("jobs")}, client_ip(request), ua(request))
    return r


@app.get("/api/jobs/{number}")
async def job_details(number: str, request: Request, form: str = "install", staff=Depends(current_staff)):
    if not sfjobs.configured():
        raise HTTPException(400, "The job lookup isn't connected to Service Fusion yet.")
    if not re.fullmatch(r"\d{4,20}", number):
        raise HTTPException(404, "No such job.")
    d = await run_in_threadpool(sfjobs.details, number)
    if not d:
        raise HTTPException(404, "That job isn't in the open-jobs list any more. Tap Refresh, or type it in.")
    # every pick is logged: who opened which customer's details, and from which form
    audit(staff["id"], staff["name"], "job_details_opened", f"job:{number}",
          {"form": form if form in JOB_FORMS else "?"}, client_ip(request), ua(request))
    return d


# ---------------------------------------------------------------- job lookup for Simply Studio
# Studio's "Find a Service Fusion customer" box asks here (server to server, on the Docker network). Each request is
# signed with a key made from the shared Studio secret and names the Studio user, who is written to the activity log.
STUDIO_API_MAX_AGE = 60


def studio_caller(request: Request) -> str:
    if not STUDIO_SSO_SECRET:
        raise HTTPException(503, "The Service Fusion lookup isn't set up for Studio.")
    ts, who, sig = (request.headers.get(h, "") for h in ("x-studio-ts", "x-studio-who", "x-studio-sig"))
    # isascii/len first: "²".isdigit() is True and a 400-digit number overflows, both were a 500
    if not (ts.isascii() and ts.isdigit() and len(ts) < 12) or abs(time.time() - int(ts)) > STUDIO_API_MAX_AGE \
            or not who.strip():
        raise HTTPException(401, "Studio request expired or unsigned.")
    key = hmac.new(STUDIO_SSO_SECRET.encode(), b"studio-sf-api", hashlib.sha256).digest()
    want = _b64u(hmac.new(key, f"{ts}|{request.scope['path']}|{request.url.query}|{who}".encode(), hashlib.sha256).digest())
    # bytes, so a signature with odd characters is a mismatch (401), not a TypeError (500); headers arrive as latin-1
    if not hmac.compare_digest(want.encode(), sig.encode("latin-1")):
        raise HTTPException(401, "Studio request signature didn't match.")
    return who.strip()[:120]


@app.get("/api/studio/test-mode")
def studio_test_mode(request: Request, email: str = ""):
    """Studio follows the owner's Test mode switch here: one switch for both apps. True only for the owner's own
    email while the switch is on, so nobody else's Studio work can ever be turned into a test."""
    studio_caller(request)
    email = email.strip().lower()
    if not email or get_setting("owner_test_mode") != "1":
        return {"test_mode": False}
    row = conn().execute("SELECT 1 FROM staff WHERE is_owner=1 AND active=1 AND lower(trim(email))=?", (email,)).fetchone()
    return {"test_mode": bool(row)}


@app.get("/api/studio/jobs")
def studio_job_search(request: Request, q: str = ""):
    studio_caller(request)
    if not sfjobs.configured():
        return {"connected": False, "results": []}
    return {"results": sfjobs.search("studio", q[:60]), **sfjobs.status()}


@app.get("/api/studio/jobs/{number}")
async def studio_job_details(number: str, request: Request):
    who = studio_caller(request)
    if not sfjobs.configured():
        raise HTTPException(400, "The job lookup isn't connected to Service Fusion yet.")
    if not re.fullmatch(r"\d{4,20}", number):
        raise HTTPException(404, "No such job.")
    d = await run_in_threadpool(sfjobs.details, number)
    if not d:
        raise HTTPException(404, "That job isn't in the open-jobs list any more. Type the details in.")
    audit(None, f"{who} (Studio)", "job_details_opened", f"job:{number}", {"form": "studio"}, client_ip(request), ua(request))
    return d


# Studio's quote screen can find a lead the same way (signed, every pick logged), and tells this app when a quote
# was made for one: the lead then shows the quote and moves to Quoted.
@app.get("/api/studio/leads")
def studio_lead_search(request: Request, q: str = ""):
    studio_caller(request)
    return {"results": [x for x in leads.search_open(q, False) if not x["is_test"]]}


@app.get("/api/studio/leads/{lid}")
def studio_lead_details(lid: int, request: Request):
    who = studio_caller(request)
    d = leads.contact(lid, False)
    if not d:
        raise HTTPException(404, "That lead couldn't be found.")
    audit(None, f"{who} (Studio)", "lead_picked_for_quote", f"lead:{lid}", {"receipt": d["receipt"]}, client_ip(request), ua(request))
    return d


@app.post("/api/studio/leads/{lid}/quoted")
def studio_lead_quoted(lid: int, request: Request, ref: str = ""):
    who = studio_caller(request)
    ref = " ".join(ref.split())[:60]
    if not ref:
        raise HTTPException(422, "Missing the quote number.")
    if not leads.quoted(lid, ref, who, client_ip(request), ua(request)):
        raise HTTPException(404, "That lead couldn't be found.")
    return {"ok": True}


# ---------------------------------------------------------------- reports
def _save_photo(upload_bytes: bytes, dest: str, g: dict | None = None, receipt: str = "", who: str = "",
                clean_dest: str | None = None) -> int:
    """Re-saves the photo as a clean JPEG with the time/location stamp printed on it.
    Hidden file data is dropped; the location kept is the one the app recorded.
    clean_dest: also keep an unstamped copy (forms that send the customer their own copy)."""
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
        if clean_dest and g is not None:
            im.save(clean_dest, "JPEG", quality=85, optimize=True)
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
    async with request.form() as form:      # closes the uploads' temp files when done
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
            _measure_lead(staff, data, str(form.get("lead_id") or "").strip(), errors)

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
        is_test = bool(staff["is_owner"]) and str(form.get("is_test", "")) == "1"   # only the owner can file test reports
        out = await run_in_threadpool(_store_report, form_type, spec, staff, submission_id, data, photo_blobs,
                                      started_at, queued, client_ip(request), ua(request), kept, is_test)
        if data.get("lead_id") and not out.get("duplicate") and not data.get("revision_of"):
            try:     # the measure is saved either way; this only shows it on the lead
                leads.measured(data["lead_id"], out["receipt"], staff, client_ip(request), ua(request))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        return out


def _next_receipt(c, pre: str) -> str:
    # each form counts on its own (VIN-00001, VIN-00002 ...), carrying on from any earlier numbers
    # test reports count separately (TEST-RMA-00001) so real numbers never have gaps
    last = c.execute("SELECT MAX(CAST(substr(receipt, ?) AS INTEGER)) FROM reports WHERE receipt LIKE ?",
                     (len(pre) + 2, pre + "-%")).fetchone()[0]
    return f"{pre}-{(last or 0) + 1:05d}"


def _write_photos(folder, photo_blobs, kept, receipt, who, customer_copy) -> list:
    """Saves a report's photos into folder: [(slot, bytes, geo or None, kept photo row or None)]."""
    os.makedirs(folder, exist_ok=True)
    out = []
    for slot, b, g in photo_blobs:
        size = _save_photo(b, os.path.join(folder, f"{slot}.jpg"), g, receipt, who,
                           clean_dest=os.path.join(folder, f"{slot}.clean.jpg") if customer_copy else None)
        out.append((slot, size, g, None))
    for slot, src in kept:     # revised measure: photos carried over from the earlier version, stamps unchanged
        dest = os.path.join(folder, f"{slot}.jpg")
        shutil.copyfile(src["path"], dest)
        out.append((slot, os.path.getsize(dest), None, src))
    return out


def _store_report(form_type, spec, staff, submission_id, data, photo_blobs, started_at, queued, ip, agent, kept=(),
                  is_test=False):
    """Photos carry the receipt number, which is only final inside the write lock. Re-saving 100 photos in there
    held up every other report and sign-in, so they're saved first with the number this report should get, and
    only redone if another report of the same form took that number meanwhile (the last try works as before)."""
    c = conn()
    pre = ("TEST-" if is_test else "") + spec["prefix"]
    for attempt in range(4):
        inside = attempt == 3
        tmp = os.path.join(PHOTO_DIR, f"tmp-{secrets.token_hex(8)}")
        want = None if inside else _next_receipt(c, pre)
        if not inside:
            try:
                saved = _write_photos(tmp, photo_blobs, kept, want, staff["name"], spec.get("customer_copy"))
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        rid = None
        try:
            c.execute("BEGIN IMMEDIATE")
            reason = forms_mod.needs_attention(form_type, data)
            cur = c.execute(
                "INSERT INTO reports(submission_id, form_type, staff_id, submitted_at, started_at, queued_on_phone, data, is_test,"
                " attention, attention_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (submission_id, form_type, staff["id"], now_iso(), started_at, queued, json.dumps(data, ensure_ascii=False),
                 1 if is_test else 0, "open" if reason else None, reason))
            rid = cur.lastrowid
            receipt = _next_receipt(c, pre)
            if want and receipt != want:          # someone else got that number first: stamp again
                c.execute("ROLLBACK")
                rid = None
                shutil.rmtree(tmp, ignore_errors=True)
                continue
            c.execute("UPDATE reports SET receipt=? WHERE id=?", (receipt, rid))
            if data.get("po_id") and data.get("po_lines"):      # Receiving against a PO: count what came in
                pricelist.apply_receiving(c, data["po_id"], data["po_lines"], is_test)
            folder = os.path.join(PHOTO_DIR, str(rid))
            shutil.rmtree(folder, ignore_errors=True)       # only ever a leftover from a report that never saved
            if inside:
                saved = _write_photos(folder, photo_blobs, kept, receipt, staff["name"], spec.get("customer_copy"))
            else:
                os.rename(tmp, folder)
            for slot, size, g, src in saved:
                dest = os.path.join(folder, f"{slot}.jpg")
                if src is not None:
                    c.execute("INSERT INTO photos(report_id, slot, path, bytes, taken_at, lat, lon, acc, geo_status, file_age)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (rid, slot, dest, size, src["taken_at"], src["lat"], src["lon"], src["acc"],
                               src["geo_status"], src["file_age"]))
                elif g is None:      # signature: no stamp, no location
                    c.execute("INSERT INTO photos(report_id, slot, path, bytes, taken_at, geo_status) VALUES (?,?,?,?,?,?)",
                              (rid, slot, dest, size, now_iso(), "signature"))
                else:
                    c.execute("INSERT INTO photos(report_id, slot, path, bytes, taken_at, lat, lon, acc, geo_status, file_age)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (rid, slot, dest, size, g["taken_at"], g["lat"], g["lon"], g["acc"], g["status"], g["file_age"]))
            no_loc = sum(1 for _, _, g in photo_blobs if g is not None and g["status"] != "ok")
            details = {"form": form_type, "receipt": receipt, "photos": len(photo_blobs), "photos_without_location": no_loc,
                       "queued_on_phone": bool(queued)}
            if kept:
                details["photos_carried_over"] = len(kept)
            if data.get("revision_of"):
                details["revision_of"] = data["revision_of"]
            if is_test:
                details["test"] = True
            audit(staff["id"], staff["name"], "report_submitted", f"report:{rid}", details, ip, agent)
            subject = subject_for(form_type, data, staff["name"], receipt)
            if is_test:   # test reports only ever go to the owner
                to, bcc, subject = [e for e in [staff["email"]] if e], [], ("TEST - " + subject)[:200]
            else:
                to, bcc = split_recipients(form_type, data)
            mailer.queue_report_email(rid, to, subject, bcc)
            cust = customer_copy_to(spec, data, is_test, staff)
            if cust:
                mailer.queue_report_email(rid, [cust], customer_subject(form_type, data, is_test), audience="customer")
                audit(staff["id"], staff["name"], "customer_copy_queued", f"report:{rid}",
                      {"to": cust, "test": True} if is_test else {"to": cust}, ip, agent)
            c.execute("COMMIT")
        except sqlite3.IntegrityError:
            c.execute("ROLLBACK")
            shutil.rmtree(tmp, ignore_errors=True)
            if rid:
                shutil.rmtree(os.path.join(PHOTO_DIR, str(rid)), ignore_errors=True)
            existing = c.execute("SELECT receipt FROM reports WHERE submission_id=?", (submission_id,)).fetchone()
            return {"ok": True, "receipt": existing["receipt"] if existing else None, "duplicate": True}
        except Exception:
            c.execute("ROLLBACK")
            shutil.rmtree(tmp, ignore_errors=True)
            if rid:
                shutil.rmtree(os.path.join(PHOTO_DIR, str(rid)), ignore_errors=True)
            raise
        mailer._wake.set()
        return {"ok": True, "receipt": receipt, "duplicate": False}


def customer_copy_to(spec, data, is_test, staff) -> str:
    """Where the customer's own copy goes: the email they gave when signing. Test reports send it to the owner
    instead, so the customer version can be checked without emailing a real customer."""
    if not spec.get("customer_copy") or data.get("cust_present") != "Yes" or not data.get("cust_email"):
        return ""
    return (staff["email"] or "") if is_test else data["cust_email"]


def customer_subject(form_type, data, is_test=False) -> str:
    s = f"Your SimplyDoors installation is complete" if data.get("work") == "Yes" else "Your SimplyDoors installation record"
    if data.get("po"):
        s += f" (Job {data['po']})"
    return ("TEST - customer copy - " + s if is_test else s)[:200]


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
        if od.get("lead_id"):
            data["lead_id"], data["lead_receipt"] = od["lead_id"], od.get("lead_receipt")
        if data["measured_by_id"] != staff["id"]:
            data["revised_by"], data["revised_by_email"] = staff["name"], staff["email"] or ""
        return
    data["measured_by"], data["measured_by_id"], data["measured_by_email"] = staff["name"], staff["id"], staff["email"] or ""


def _measure_lead(staff, data, lead_id, errors):
    """A measure picked from a lead (customer form or phone call) is linked to it. A revision keeps its lead."""
    if not lead_id:
        return
    if not lead_id.isdigit() or not (d := leads.contact(int(lead_id), bool(staff["is_owner"]))):
        errors.append("That lead couldn't be found any more. Tap “Not this lead” and type the customer in.")
        return
    data["lead_id"], data["lead_receipt"] = d["id"], d["receipt"]


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


@app.get("/api/measure/leads")
def measure_leads(q: str = "", staff=Depends(current_staff)):
    """Measure's "Pick a lead": anyone who can measure (names and addresses only; phone and email come with the pick)."""
    _measure_allowed(staff)
    return {"results": leads.search_open(q, bool(staff["is_owner"]))}


@app.get("/api/measure/leads/{lid}")
def measure_lead_pick(lid: int, request: Request, staff=Depends(current_staff)):
    _measure_allowed(staff)
    d = leads.contact(lid, bool(staff["is_owner"]))
    if not d:
        raise HTTPException(404, "That lead couldn't be found.")
    audit(staff["id"], staff["name"], "lead_picked_for_measure", f"lead:{lid}", {"receipt": d["receipt"]},
          client_ip(request), ua(request))
    return d


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
def _local_day_start(day: str, after: bool = False) -> str | None:
    """Start of a YYYY-MM-DD day (or of the day after) in local time, as the UTC time the log is stored in."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=geo.TZ)
    except ValueError:
        return None
    if after:
        d += timedelta(days=1)          # wall-clock day, so it's right across a daylight-saving change
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit_query(params) -> tuple[str, list]:
    where, args = [], []
    if params.get("person"):
        where.append("(actor_name LIKE ? OR target LIKE ?)")
        args += [f"%{params['person']}%"] * 2
    if params.get("action"):
        where.append("action LIKE ?")
        args.append(f"%{params['action']}%")
    # the From/To date boxes mean whole local (Texas) days
    if params.get("from"):
        start = _local_day_start(params["from"])
        where.append("at >= ?")
        args.append(start or params["from"])
    if params.get("to"):
        end = _local_day_start(params["to"], after=True)
        where.append("at < ?" if end else "at <= ?")
        args.append(end or params["to"])
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
    own = bool(admin["is_owner"])
    return {"total": total, "can_delete": own,
            "rows": [dict(r) | {"deletable": own and _own_deletable(r, admin["id"])} for r in rows]}


# The owner may remove their OWN routine lines (views, downloads, submissions). Never: sign-ins, PINs, staff,
# email lists, settings, test-mode switches, or the receipts these deletes leave behind.
OWN_DELETABLE = {"audit_viewed", "audit_exported", "report_viewed", "report_pdf_downloaded", "photo_viewed",
                 "report_submitted", "measure_reopened", "email_resend_requested", "email_retry_requested",
                 "leads_list_viewed", "lead_viewed", "lead_file_viewed"}


def _own_deletable(r, owner_id) -> bool:
    return r["actor_id"] == owner_id and r["action"] in OWN_DELETABLE


@app.post("/api/admin/audit/delete")
async def delete_own_audit(request: Request, owner=Depends(current_owner)):
    require_app_header(request)
    try:
        ids = [int(i) for i in (await request.json()).get("ids", [])][:1000]
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad ids")
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        rows = c.execute(f"SELECT * FROM audit WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
        ok = [r for r in rows if _own_deletable(r, owner["id"])]
        if len(ok) != len(ids):
            raise HTTPException(403, "Some of those lines can't be deleted (only your own routine lines can).")
        n = delete_audit_rows(c, [r["id"] for r in ok])
        kinds = sorted({r["action"] for r in ok})
        audit(owner["id"], owner["name"], "log_lines_deleted", None, {"count": n, "kinds": kinds},
              client_ip(request), ua(request))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    alerts.push("Ops app: activity log lines deleted", f"{owner['name']} deleted {n} of their own log lines.")
    return {"deleted": n}


@app.delete("/api/admin/reports/{rid}")
def delete_test_report(rid: int, request: Request, owner=Depends(current_owner)):
    require_app_header(request)
    c = conn()
    r = c.execute("SELECT id, receipt, is_test FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    if not r["is_test"]:
        raise HTTPException(403, "Only TEST reports can be deleted.")
    c.execute("BEGIN IMMEDIATE")
    try:
        eids = [e[0] for e in c.execute("SELECT id FROM emails WHERE report_id=?", (rid,))]
        pids = [p[0] for p in c.execute("SELECT id FROM photos WHERE report_id=?", (rid,))]
        targets = [f"report:{rid}"] + [f"email:{e}" for e in eids] + [f"photo:{p}" for p in pids]
        lines = [a[0] for a in c.execute(f"SELECT id FROM audit WHERE target IN ({','.join('?' * len(targets))})", targets)]
        n = delete_audit_rows(c, lines)
        c.execute("DELETE FROM emails WHERE report_id=?", (rid,))
        c.execute("DELETE FROM photos WHERE report_id=?", (rid,))
        c.execute("DELETE FROM reports WHERE id=?", (rid,))
        audit(owner["id"], owner["name"], "test_report_deleted", r["receipt"], {"log_lines_removed": n},
              client_ip(request), ua(request))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    shutil.rmtree(os.path.join(PHOTO_DIR, str(rid)), ignore_errors=True)
    return {"deleted": r["receipt"]}


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
    if p.get("attention") in ("open", "resolved"):      # the "Needs attention" filter
        where.append("r.attention=?")
        args.append(p["attention"])
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn().execute(
        f"SELECT r.id, r.receipt, r.form_type, r.submitted_at, r.queued_on_phone, r.is_test, s.name AS staff_name, r.data,"
        f" r.attention, r.attention_reason, r.resolved_at, r.resolved_by,"
        f" (SELECT status FROM emails e WHERE e.report_id=r.id ORDER BY e.id DESC LIMIT 1) AS email_status,"
        f" (SELECT COUNT(*) FROM photos p WHERE p.report_id=r.id AND COALESCE(p.geo_status,'missing') NOT IN ('ok','signature')) AS no_geo"
        f" FROM reports r JOIN staff s ON s.id=r.staff_id{w} ORDER BY r.id DESC LIMIT 300", args).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        summary = forms_mod.summary(r["form_type"], d)
        out.append({k: r[k] for k in ("id", "receipt", "form_type", "submitted_at", "queued_on_phone", "staff_name", "email_status",
                                      "no_geo", "attention", "attention_reason", "resolved_at", "resolved_by")}
                   | {"summary": summary, "is_test": bool(r["is_test"])})
    return out


@app.get("/api/admin/attention")
def admin_attention_count(admin=Depends(current_admin)):
    """How many flagged reports are still open (for the badge on the Reports tab). Test reports don't count."""
    n = conn().execute("SELECT COUNT(*) FROM reports WHERE attention='open' AND is_test=0").fetchone()[0]
    return {"open": n}


class ResolveBody(BaseModel):
    note: str = ""


@app.post("/api/admin/reports/{rid}/resolve")
def admin_resolve_report(rid: int, body: ResolveBody, request: Request, admin=Depends(current_admin)):
    """Closes out a flagged report. The note is what was done ("brake light replaced"), and it's required: a
    resolved item with no note tells the next person nothing."""
    note = " ".join(body.note.split())[:500]
    if not note:
        raise HTTPException(400, "Say what was done, in a few words, before marking it resolved.")
    c = conn()
    r = c.execute("SELECT receipt, attention FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    if not r["attention"]:
        raise HTTPException(400, "This report was never flagged, so there's nothing to resolve.")
    if r["attention"] == "resolved":
        raise HTTPException(400, "This report is already resolved.")
    c.execute("UPDATE reports SET attention='resolved', resolved_at=?, resolved_by=?, resolved_note=? WHERE id=?",
              (now_iso(), admin["name"], note, rid))
    audit(admin["id"], admin["name"], "report_resolved", f"report:{rid}", {"receipt": r["receipt"], "note": note},
          client_ip(request), ua(request))
    return {"ok": True}


@app.post("/api/admin/reports/{rid}/reopen")
def admin_reopen_report(rid: int, request: Request, admin=Depends(current_admin)):
    """Puts a resolved report back on the Needs attention list (marked resolved by mistake, or the fix didn't hold)."""
    c = conn()
    r = c.execute("SELECT receipt, attention, resolved_note FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    if r["attention"] != "resolved":
        raise HTTPException(400, "Only a resolved report can be reopened.")
    c.execute("UPDATE reports SET attention='open', resolved_at=NULL, resolved_by=NULL, resolved_note=NULL WHERE id=?", (rid,))
    audit(admin["id"], admin["name"], "report_reopened", f"report:{rid}", {"receipt": r["receipt"], "was": r["resolved_note"]},
          client_ip(request), ua(request))
    return {"ok": True}


def backfill_attention():
    """One-time, on the first start after this feature: flagged reports from the last 30 days go on the Needs
    attention list, so nothing recent is lost in the switch-over. Older ones are left alone (handled by now, or
    not worth reopening). Admins mark the already-handled ones resolved with a note."""
    if get_setting("attention_backfill_due") != "1":
        return
    c = conn()
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for r in c.execute("SELECT id, form_type, data FROM reports WHERE attention IS NULL AND submitted_at>=?", (since,)).fetchall():
        reason = forms_mod.needs_attention(r["form_type"], json.loads(r["data"]))
        if reason:
            c.execute("UPDATE reports SET attention='open', attention_reason=? WHERE id=?", (reason, r["id"]))
            n += 1
    set_setting("attention_backfill_due", "0")
    audit(None, "system", "attention_backfilled", None, {"opened": n, "since": since})


def open_attention(min_age_hours: float = 0):
    """Flagged reports still open (never test ones), oldest first; optionally only those open longer than a cutoff."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn().execute(
        "SELECT r.id, r.receipt, r.form_type, r.attention_reason, r.submitted_at, r.data, s.name AS staff_name"
        " FROM reports r JOIN staff s ON s.id=r.staff_id WHERE r.attention='open' AND r.is_test=0 AND r.submitted_at<=?"
        " ORDER BY r.id", (cutoff,)).fetchall()


@app.get("/api/admin/vehicles")
def admin_vehicles(admin=Depends(current_admin)):
    """One card per truck: open defects, items that keep failing, the last inspection and odometer reading.
    Built from Vehicle Inspection and Vehicle Incident reports of the last 180 days (test reports left out)."""
    since = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repeat_since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn().execute(
        "SELECT r.id, r.receipt, r.form_type, r.submitted_at, r.attention, r.data, s.name AS staff_name FROM reports r"
        " JOIN staff s ON s.id=r.staff_id WHERE r.form_type IN ('Vehicle Inspection','Vehicle Incident') AND r.is_test=0"
        " AND r.submitted_at>=? ORDER BY r.id DESC", (since,)).fetchall()
    cards: dict[str, dict] = {}

    def card(name):
        return cards.setdefault(name, {"vehicle": name, "last_inspection": None, "last_odometer": None, "open_defects": [],
                                       "open_incidents": [], "repeat_items": [], "inspections": 0, "_fails": {}})
    for r in rows:
        d = json.loads(r["data"])
        v = (d.get("vehicle") or "").strip() or "(no vehicle given)"
        c = card(v)
        if r["form_type"] == "Vehicle Incident":
            if r["attention"] == "open":
                c["open_incidents"].append({"id": r["id"], "receipt": r["receipt"], "when": r["submitted_at"], "by": r["staff_name"]})
            continue
        c["inspections"] += 1
        if c["last_inspection"] is None:          # rows are newest first
            c["last_inspection"] = {"id": r["id"], "receipt": r["receipt"], "when": r["submitted_at"], "by": r["staff_name"],
                                    "trip": d.get("trip"), "defective": bool(d.get("defective"))}
        try:
            odo = int(float(d.get("odometer")))
        except (TypeError, ValueError):
            odo = None
        if odo is not None and (c["last_odometer"] is None or c["last_odometer"]["when"] < r["submitted_at"]):
            c["last_odometer"] = {"reading": odo, "when": r["submitted_at"]}
        bad = [k for k, val in (d.get("items") or {}).items() if val == "Defective"]
        if r["attention"] == "open" and bad:
            c["open_defects"].append({"id": r["id"], "receipt": r["receipt"], "when": r["submitted_at"], "by": r["staff_name"],
                                      "items": bad, "remarks": d.get("remarks") or ""})
        if r["submitted_at"] >= repeat_since:
            for k in bad:
                c["_fails"][k] = c["_fails"].get(k, 0) + 1
    for name in forms_mod.get_list("vehicles"):     # trucks with no reports yet still get a card
        card(name)
    out = []
    for c in cards.values():
        c["repeat_items"] = sorted(({"item": k, "times": n} for k, n in c.pop("_fails").items() if n >= 2),
                                   key=lambda x: -x["times"])
        out.append(c)
    out.sort(key=lambda c: (-(len(c["open_defects"]) + len(c["open_incidents"])), c["vehicle"].lower()))
    return out


ATTENTION_ALERT_HOUR = 17      # 5 pm local, Monday to Saturday


def attention_alert_once(local_now=None):
    """Once per work day after 5 pm: one phone alert to the owner if anything flagged has been open over 24 hours.
    Silent when the list is clear. Runs from the 10-minute nightly loop, so 'once' is kept in settings."""
    local_now = local_now or datetime.now(geo.TZ)
    if local_now.hour < ATTENTION_ALERT_HOUR or local_now.weekday() == 6:
        return False
    day = local_now.strftime("%Y-%m-%d")
    if get_setting("attention_alert_day") == day:
        return False
    set_setting("attention_alert_day", day)
    rows = open_attention(min_age_hours=24)
    if not rows:
        return False
    by_reason: dict[str, int] = {}
    for r in rows:
        by_reason[r["attention_reason"] or "flagged report"] = by_reason.get(r["attention_reason"] or "flagged report", 0) + 1
    parts = ", ".join(f"{n} {reason.lower()}{'' if n == 1 else 's'}" for reason, n in by_reason.items())
    oldest = rows[0]
    days = max(1, int((datetime.now(timezone.utc) - datetime.strptime(oldest["submitted_at"], "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc)).total_seconds() // 86400))
    msg = (f"{len(rows)} item{'s' if len(rows) != 1 else ''} need attention: {parts}. "
           f"Oldest: {oldest['receipt']} ({oldest['attention_reason']}), {days} day{'s' if days != 1 else ''} old. "
           f"Admin > Reports > Needs attention.")
    alerts.push("Ops app: items need attention", msg, "default", click=f"{leads.OPS_URL}/admin#attention")
    return True


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
        "is_test": bool(r["is_test"]),
        "attention": r["attention"], "attention_reason": r["attention_reason"], "resolved_at": r["resolved_at"],
        "resolved_by": r["resolved_by"], "resolved_note": r["resolved_note"],
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
            "studio": shows_studio(r), "price_list": bool(r["price_list"]), "price_edit": bool(r["price_edit"]),
            "leads": bool(r["leads"]),
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
    if "studio_link" in body:
        out["studio_link"] = 1 if body["studio_link"] else 0
    if "price_list" in body:
        out["price_list"] = 1 if body["price_list"] else 0
    if "price_edit" in body:
        out["price_edit"] = 1 if body["price_edit"] else 0
    if "leads" in body:
        out["leads"] = 1 if body["leads"] else 0
    # editing prices needs the Price List itself; taking the Price List away takes editing away too
    if out.get("price_edit") == 1:
        out["price_list"] = 1
    if out.get("price_list") == 0:
        out["price_edit"] = 0
    return out


# A work email is what signs someone into Simply Studio, so each one belongs to one person (any upper/lower case),
# only the owner sets an admin's email, and every change reaches the owner's phone.
def _email_check(email: str, sid=None):
    if not email:
        return
    row = conn().execute("SELECT name FROM staff WHERE email != '' AND lower(email)=lower(?) AND id IS NOT ?",
                         (email, sid)).fetchone()
    if row:
        raise HTTPException(400, f"{row['name']} already has that email address. Each person needs their own.")


def _staff_conflict(e: sqlite3.IntegrityError) -> HTTPException:
    if "email" in str(e):
        return HTTPException(400, "Someone else already has that email address. Each person needs their own.")
    return HTTPException(409, "Someone with that name already exists.")


@app.post("/api/admin/staff")
async def admin_add_staff(request: Request, admin=Depends(current_admin)):
    body = await request.json()
    v = _validate_staff(body, partial=False)
    if v.get("is_admin") and not admin["is_owner"]:
        raise HTTPException(403, "Only the app owner can add an admin.")
    _email_check(v.get("email", ""))
    try:
        cur = conn().execute(
            "INSERT INTO staff(name, dept, email, is_admin, sales_notify, active, created_at) VALUES (?,?,?,?,?,1,?)",
            (v["name"], v["dept"], v.get("email", ""), v.get("is_admin", 0), v.get("sales_notify", 0), now_iso()))
    except sqlite3.IntegrityError as e:
        raise _staff_conflict(e)
    audit(admin["id"], admin["name"], "staff_added", v["name"], v, client_ip(request), ua(request))
    alerts.push("Ops app: staff added", f"{admin['name']} added {v['name']} ({v['dept']})"
                + (f" with the email {v['email']}." if v.get("email") else "."), "high" if v.get("email") else "default")
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
    if v.get("is_admin") == 1 and not old["is_admin"] and not admin["is_owner"]:
        raise HTTPException(403, "Only the app owner can make someone an admin.")
    if "email" in v and v["email"] != old["email"]:
        if (old["is_admin"] or old["is_owner"]) and not admin["is_owner"]:
            raise HTTPException(403, "An admin's email (yours too) can only be changed by the app owner.")
        _email_check(v["email"], sid)
    changes = {k: {"from": old[k], "to": val} for k, val in v.items() if old[k] != val}
    if not changes:
        return {"ok": True}
    try:
        c.execute(f"UPDATE staff SET {', '.join(k + '=?' for k in v)} WHERE id=?", list(v.values()) + [sid])
    except sqlite3.IntegrityError as e:
        raise _staff_conflict(e)
    if "active" in changes or "is_admin" in changes:
        auth.end_all_sessions(sid)   # promoted, demoted or turned off: sign in again
    audit(admin["id"], admin["name"], "staff_changed", old["name"], changes, client_ip(request), ua(request))
    if "email" in changes:
        ch = changes["email"]
        alerts.push_throttled(f"email:{sid}:{ch['to']}", "Ops app: staff email changed",
                              f"{admin['name']} changed the email for {old['name']}: {ch['from'] or '(none)'} → "
                              f"{ch['to'] or '(none)'}. It's the address that signs them into Simply Studio.",
                              "high", every_seconds=60)
    rest = [k for k in changes if k != "email"]
    if rest:
        alerts.push("Ops app: staff changed", f"{admin['name']} changed {old['name']}: {', '.join(rest)}.")
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
        leads.RULE: "New leads from the customer form, with an “Open this lead” button. Suspected spam isn't emailed. "
                    "Test leads go only to the app owner.",
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
        "job_lookup": sfjobs.status(),
    }


# ---------------------------------------------------------------- Price List (beta)
def current_pricelist(request: Request):
    row = current_staff(request)
    if not pricelist.allowed(row):
        raise HTTPException(403, "The Price List isn't switched on for you. Ask Adem or Paz.")
    return row


def _vendor(code: str) -> dict:
    v = next((x for x in pricelist.vendors() if x["code"] == code), None)
    if not v:
        raise HTTPException(404, "No such vendor.")
    return v


@app.get("/api/pricelist/vendors")
def pl_vendors(staff=Depends(current_pricelist)):
    return [{k: v[k] for k in ("code", "name", "address", "sheet", "sheets")} | {"can_order": bool(v["order_email"])}
            for v in pricelist.vendors()]


@app.get("/api/pricelist/items")
def pl_items(request: Request, vendor: str, staff=Depends(current_pricelist)):
    v = _vendor(vendor)
    rows = pricelist.items(v["code"])
    audit(staff["id"], staff["name"], "price_list_viewed", v["name"], {"items": len(rows)}, client_ip(request), ua(request))
    return {"vendor": v["code"], "sheet": v["sheet"], "items": rows}


def current_pricelist_editor(request: Request):
    row = current_pricelist(request)
    if not pricelist.can_edit(row):
        raise HTTPException(403, "Editing the Price List isn't switched on for you. Ask Adem or Paz.")
    return row


def _edit_alert(staff, what: str):
    # one heads-up per person per 10 minutes, so a run of edits doesn't flood the owner's phone
    alerts.push_throttled(f"pl-edit:{staff['id']}", "Ops app: Price List edited",
                          f"{staff['name']} is editing Price List items ({what}). Details are in the activity log.",
                          every_seconds=600)


@app.patch("/api/pricelist/items/{iid}")
async def pl_edit_item(iid: int, request: Request, staff=Depends(current_pricelist_editor)):
    body = await request.json()
    try:
        vendor, sku, name, changes = pricelist.update_item(iid, body if isinstance(body, dict) else {}, staff["name"])
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except pricelist.SheetError as e:
        raise HTTPException(422, str(e)) from None
    if changes:
        audit(staff["id"], staff["name"], "price_item_changed", f"{_vendor(vendor)['name']}: {sku}",
              {"item": name, **changes}, client_ip(request), ua(request))
        _edit_alert(staff, f"changed {sku}")
    return pricelist.get_item(iid)


@app.post("/api/pricelist/items")
async def pl_add_item(request: Request, staff=Depends(current_pricelist_editor)):
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    sheet_id = body.get("sheet_id")
    try:
        iid = pricelist.add_item(int(sheet_id) if re.fullmatch(r"[0-9]{1,9}", str(sheet_id)) else 0, body, staff["name"])
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except pricelist.SheetError as e:
        raise HTTPException(422, str(e)) from None
    d = pricelist.get_item(iid)
    audit(staff["id"], staff["name"], "price_item_added", f"{_vendor(d['vendor'])['name']}: {d['sku']}",
          {"item": d["name"], "sheet": d["sheet"], "price": d["price"]}, client_ip(request), ua(request))
    _edit_alert(staff, f"added {d['sku']}")
    return d


@app.delete("/api/pricelist/items/{iid}")
def pl_delete_item(iid: int, request: Request, staff=Depends(current_pricelist_editor)):
    r = pricelist.delete_item(iid)
    if not r:
        raise HTTPException(404, "That item isn't on a live sheet any more.")
    audit(staff["id"], staff["name"], "price_item_deleted", f"{_vendor(r['sv'])['name']}: {r['sku']}",
          {"item": r["name"], "sheet": r["sheet_label"], "price": r["price"]}, client_ip(request), ua(request))
    _edit_alert(staff, f"deleted {r['sku']}")
    return {"ok": True}


@app.put("/api/pricelist/styles")
async def pl_set_style(request: Request, staff=Depends(current_pricelist_editor)):
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    v = _vendor(str(body.get("vendor", "")))
    grp = str(body.get("grp", ""))[:120]
    try:
        old, n = pricelist.set_style(v["code"], grp, str(body.get("style", "")), staff["name"])
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    new = " ".join(str(body.get("style", "")).split())[:40]
    if old != new:
        audit(staff["id"], staff["name"], "price_style_set", f"{v['name']}: {grp or '(no group)'}",
              {"style": {"from": old, "to": new}, "items": n}, client_ip(request), ua(request))
    return {"ok": True, "style": new, "items": n}


@app.get("/api/pricelist/sheets/{sid}/csv")
def pl_sheet_csv(sid: int, request: Request):
    staff = current_staff(request)
    if not (pricelist.can_edit(staff) or staff["is_admin"]):
        raise HTTPException(403, "Only people who can edit the Price List can download a sheet.")
    s, text = pricelist.sheet_csv(sid)
    if not s:
        raise HTTPException(404, "That sheet isn't live any more.")
    audit(staff["id"], staff["name"], "price_sheet_downloaded", _vendor(s["vendor"])["name"], {"sheet": s["label"]},
          client_ip(request), ua(request))
    name = re.sub(r"[^A-Za-z0-9-]+", "_", f"{s['vendor']}_{s['label']}").strip("_")[:80] or "sheet"
    return Response(text.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"', "Cache-Control": "no-store"})


@app.get("/api/pricelist/po-check")
def pl_po_check(po: str = "", staff=Depends(current_pricelist)):
    return {"sent_before": pricelist.sent_before(po.strip()[:60])}


@app.post("/api/pricelist/pos")
async def pl_send_po(request: Request, staff=Depends(current_pricelist)):
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    v = _vendor(str(body.get("vendor", "")))
    # "download": save it and hand back the PDF without emailing anyone (a vendor with no order email yet, say)
    download = body.get("action") == "download"
    test = not download and bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"
    if not download and not v["order_email"] and not test:
        raise HTTPException(400, f"{v['name']} has no order email yet. Download the PO instead, or an admin sets it in Admin → Price List.")
    if not sfjobs.configured():
        raise HTTPException(400, "The job lookup isn't connected to Service Fusion, so there's no PO number to use.")
    job = str(body.get("job_number", "")).strip()
    if not re.fullmatch(r"\d{4,20}", job):
        raise HTTPException(422, "Pick the job first.")
    d = await run_in_threadpool(sfjobs.details, job)
    if not d:
        raise HTTPException(404, "That job isn't in the open-jobs list any more. Tap Refresh and pick it again.")
    po_number = (d.get("po_number") or "").strip()
    if not po_number:
        raise HTTPException(422, "This job has no PO number in Service Fusion. Add it there, then refresh jobs.")
    method = str(body.get("ship_method", ""))
    if method not in pricelist.SHIP_METHODS:
        raise HTTPException(422, "Pick a shipping method.")
    ship_to = "site" if body.get("ship_to") == "site" else "shop"
    day = _po_day(body.get("order_date"))
    try:
        lines, total, label = pricelist.build_lines(v["code"], body.get("lines") or [])
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    notes = str(body.get("notes", "")).strip()[:1000]
    if download:
        sent_to = ""
    elif test:
        em = (staff["email"] or "").strip()
        if not em:
            raise HTTPException(400, "Test mode sends the PO to you, but your account has no email.")
        sent_to = em
    else:
        sent_to = v["order_email"]
    c = conn()
    cur = c.execute(
        "INSERT INTO pl_pos(po_number, vendor, job_number, job_customer, order_date, ship_method, ship_to, ship_address,"
        " notes, lines, total, sheet_label, staff_id, sent_to, is_test, created_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (po_number, v["code"], job, d.get("customer") or "", day, method, ship_to, d.get("address") or "", notes,
         json.dumps(lines), total, label, staff["id"], sent_to, 1 if test else 0, now_iso(),
         "downloaded" if download else "sent"))
    pid = cur.lastrowid
    if not download:
        _queue_po(pid, sent_to, test, po_number)
    audit(staff["id"], staff["name"], "po_downloaded" if download else "po_sent", f"po:{pid}",
          {"po": po_number, "vendor": v["name"], "job": job, "to": sent_to, "lines": len(lines), "total": total,
           **({"test": True} if test else {})}, client_ip(request), ua(request))
    return pricelist.po_out(pricelist.get_po(pid), with_lines=True)


def _po_day(v) -> str:
    """The PO's order date: a real YYYY-MM-DD date, otherwise today."""
    try:
        return date.fromisoformat(str(v or "")).isoformat()
    except (ValueError, TypeError):
        return datetime.now(pricelist_tz()).strftime("%Y-%m-%d")


PO_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/#-]{0,39}$")


def _po_email_to(staff, v_email: str, v_name: str):
    """(send to, is test) for emailing a PO: the owner in test mode gets it instead of the vendor."""
    test = bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"
    if test:
        em = (staff["email"] or "").strip()
        if not em:
            raise HTTPException(400, "Test mode sends the PO to you, but your account has no email.")
        return em, True
    if not v_email:
        raise HTTPException(400, f"{v_name} has no order email yet. Download the PO instead, or an admin sets the email in Admin → Price List.")
    return v_email, False


def _typed_vendor_alert(staff, po_number: str, vendor: str, to: str):
    # a PO (with net prices) emailed to an address someone typed in, not a vendor an admin set up
    alerts.push("Ops app: PO sent to a typed-in vendor",
                f"{staff['name']} emailed PO {po_number} to {vendor} at {to} (a one-off vendor, not in the vendor list).", "high")


def _queue_po(pid: int, sent_to: str, test: bool, po_number: str):
    subject = f"{'TEST - ' if test else ''}SimplyDoors Purchase Order {po_number}"
    if test:
        conn().execute("INSERT INTO emails(report_id, po_id, recipients, cc, subject, created_at, next_try_at, audience)"
                       " VALUES (NULL,?,?,'',?,?,?,'vendor')", (pid, sent_to, subject, now_iso(), now_iso()))
        mailer._wake.set()
    else:
        mailer.queue_po_email(pid, sent_to, subject)


@app.post("/api/pricelist/pos/manual")
async def pl_manual_po(request: Request, staff=Depends(current_pricelist)):
    """A PO filled in by hand: any lines and prices, a listed or one-off vendor, a Service Fusion job or a typed PO #.
    action "download" saves it as 'downloaded' (nothing emailed); "send" emails it like any PO."""
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    action = "send" if body.get("action") == "send" else "download"
    other = body.get("other_vendor") if isinstance(body.get("other_vendor"), dict) else None
    if other:
        code = pricelist.ONE_OFF
        v_name = " ".join(str(other.get("name", "")).split())[:80]
        v_addr = "\n".join(x.strip() for x in str(other.get("address", "")).splitlines() if x.strip())[:400]
        v_email = str(other.get("email", "")).strip()[:120]
        if len(v_name) < 2:
            raise HTTPException(422, "Type the vendor's name.")
        if v_email and not EMAIL_RE.match(v_email):
            raise HTTPException(422, "That vendor email doesn't look right.")
        order_email = v_email
    else:
        v = _vendor(str(body.get("vendor", "")))
        code, v_name, v_addr, v_email, order_email = v["code"], "", "", "", v["order_email"]
    job = str(body.get("job_number", "")).strip()
    customer, address = "", str(body.get("ship_address", "")).strip()[:300]
    if job:
        if not re.fullmatch(r"\d{4,20}", job):
            raise HTTPException(422, "That job number doesn't look right. Pick the job again.")
        if not sfjobs.configured():
            raise HTTPException(400, "The job lookup isn't connected to Service Fusion. Use “No job” and type the PO #.")
        d = await run_in_threadpool(sfjobs.details, job)
        if not d:
            raise HTTPException(404, "That job isn't in the open-jobs list any more. Tap Refresh and pick it again.")
        po_number = (d.get("po_number") or "").strip()
        if not po_number:
            raise HTTPException(422, "This job has no PO number in Service Fusion. Add it there, or use “No job” and type one.")
        customer, address = d.get("customer") or "", d.get("address") or ""
    else:
        po_number = " ".join(str(body.get("po_number", "")).split())
        if not PO_NUMBER_RE.match(po_number):
            raise HTTPException(422, "Type a PO number (letters, numbers, - / . #, up to 40).")
    method = str(body.get("ship_method", ""))
    if method not in pricelist.SHIP_METHODS:
        raise HTTPException(422, "Pick a shipping method.")
    ship_to = "site" if body.get("ship_to") == "site" else "shop"
    day = _po_day(body.get("order_date"))
    try:
        lines, total = pricelist.clean_manual_lines(body.get("lines"))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    notes = str(body.get("notes", "")).strip()[:1000]
    sent_to, test = (_po_email_to(staff, order_email, v_name or _vendor(code)["name"]) if action == "send" else ("", False))
    c = conn()
    cur = c.execute(
        "INSERT INTO pl_pos(po_number, vendor, job_number, job_customer, order_date, ship_method, ship_to, ship_address,"
        " notes, lines, total, sheet_label, staff_id, sent_to, is_test, created_at, status, manual, vendor_name,"
        " vendor_address, vendor_email) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
        (po_number, code, job, customer, day, method, ship_to, address, notes, json.dumps(lines), total,
         "Written by hand", staff["id"], sent_to, 1 if test else 0, now_iso(), "sent" if action == "send" else "downloaded",
         v_name, v_addr, v_email))
    pid = cur.lastrowid
    if action == "send":
        _queue_po(pid, sent_to, test, po_number)
        if other and not test:
            _typed_vendor_alert(staff, po_number, v_name, sent_to)
    audit(staff["id"], staff["name"], "po_sent" if action == "send" else "po_downloaded", f"po:{pid}",
          {"po": po_number, "vendor": v_name or _vendor(code)["name"], "job": job, "to": sent_to, "lines": len(lines),
           "total": total, "by_hand": True, **({"test": True} if test else {})}, client_ip(request), ua(request))
    return pricelist.po_out(pricelist.get_po(pid), with_lines=True)


@app.post("/api/pricelist/pos/{pid}/send")
def pl_send_saved_po(pid: int, request: Request, staff=Depends(current_pricelist)):
    """Email a PO that was saved and downloaded earlier."""
    r = _po_or_404(pid)
    if r["status"] != "downloaded":
        raise HTTPException(409, "This PO was already sent.")
    po = pricelist.po_out(r)
    if po["vendor_name"]:
        email, name = r["vendor_email"], po["vendor_name"]
    else:
        v = _vendor(po["vendor"])
        email, name = v["order_email"], v["name"]
    sent_to, test = _po_email_to(staff, email, name)
    if not test:   # a test send goes only to the owner and leaves the PO ready to send for real
        cur = conn().execute("UPDATE pl_pos SET status='sent', sent_to=? WHERE id=? AND status='downloaded'", (sent_to, pid))
        if cur.rowcount == 0:      # someone else sent it a moment ago
            raise HTTPException(409, "This PO was already sent.")
    _queue_po(pid, sent_to, test, po["po_number"])
    if po["vendor_name"] and not test:
        _typed_vendor_alert(staff, po["po_number"], name, sent_to)
    audit(staff["id"], staff["name"], "po_sent", f"po:{pid}", {"po": po["po_number"], "vendor": name, "to": sent_to,
          "after_download": True, **({"test": True} if test else {})}, client_ip(request), ua(request))
    out = pricelist.po_out(pricelist.get_po(pid), with_lines=True)
    if test:      # the saved PO itself stays a real, unsent PO; this reply says the email was a test
        out["is_test"] = True
    return out


def pricelist_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(os.environ.get("TZ_DISPLAY", "America/Chicago"))


@app.get("/api/pricelist/pos")
def pl_pos(q: str = "", staff=Depends(current_pricelist)):
    return pricelist.recent_pos(q=q[:80])


# ---------------------------------------------------------------- Receiving against a PO, and product pictures
PICTURE_DIR = os.path.join(PHOTO_DIR, "products")       # inside photos/, so the nightly off-site copy includes them


def _owner_test(staff) -> bool:
    return bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"


@app.get("/api/receiving/pos")
def receiving_pos(q: str = "", staff=Depends(current_staff)):
    """Open POs for the Receiving form: PO #, vendor, job and each line's quantity and picture. Never prices, so
    anyone who can file a Receiving Report may see it."""
    return pricelist.open_pos_for_receiving(_owner_test(staff), q[:80])


@app.get("/api/receiving/po-vendor")
def receiving_po_vendor(po: str = "", staff=Depends(current_staff)):
    """The vendor on the PO with this number, for the RMA form ('' if none)."""
    return {"vendor": pricelist.vendor_for_po_number(po[:60])}


@app.get("/api/pictures/{pid}")
def picture_file(pid: int, staff=Depends(current_staff)):
    r = pricelist.get_picture(pid)
    if not r or not os.path.isfile(r["path"]):
        raise HTTPException(404)
    return FileResponse(r["path"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def _picture_bytes(file) -> bytes:
    """A product picture from an upload: checked, turned upright, at most 1200 px, as a JPEG."""
    if file is None or isinstance(file, str):
        raise HTTPException(400, "Choose a picture first.")
    b = await file.read(MAX_PHOTO_BYTES + 1)
    if not b or len(b) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "That picture is empty or too large (15 MB at most).")
    if not await run_in_threadpool(_check_photo, b):
        raise HTTPException(422, "That file couldn't be read as a picture. Take it again.")

    def shrink():
        with Image.open(io.BytesIO(b), formats=PHOTO_FORMATS) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1200, 1200))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue()
    return await run_in_threadpool(shrink)


def _store_picture(vendor: str, key: str, jpeg: bytes, who: str, source: str) -> int:
    os.makedirs(PICTURE_DIR, exist_ok=True)
    path = os.path.join(PICTURE_DIR, f"{secrets.token_hex(12)}.jpg")
    with open(path, "wb") as f:
        f.write(jpeg)
    pid, old_path = pricelist.set_picture(vendor, key, path, who, source)
    if old_path and old_path != path:
        try:
            os.remove(old_path)
        except OSError:
            pass
    return pid


@app.post("/api/receiving/pictures")
async def receiving_add_picture(request: Request, staff=Depends(current_staff)):
    """A crew member adds the missing picture for a line on the PO they're receiving. Only fills gaps: a line that
    already has a picture keeps it (editors replace pictures in the Price List)."""
    require_app_header(request)
    async with request.form() as form:
        try:
            po_id, line = int(str(form.get("po_id", ""))), int(str(form.get("line", "")))
        except ValueError:
            raise HTTPException(400, "Pick the PO line first.") from None
        r = pricelist.get_po(po_id)
        lines = json.loads(r["lines"]) if r else []
        if not r or not 0 <= line < len(lines) or bool(r["is_test"]) != _owner_test(staff):
            raise HTTPException(404, "That PO line wasn't found.")
        key = pricelist.picture_key(lines[line].get("sku"), lines[line].get("name"))
        if not key:
            raise HTTPException(400, "This line has no part # or description to file a picture under.")
        if pricelist.find_picture(r["vendor"], key):
            raise HTTPException(409, "This item already has a picture.")
        jpeg = await _picture_bytes(form.get("file"))
    pid = _store_picture(r["vendor"], key, jpeg, staff["name"], "receiving")
    audit(staff["id"], staff["name"], "product_picture_added", f"{r['vendor']}: {key}",
          {"po": r["po_number"], "from": "receiving"}, client_ip(request), ua(request))
    return {"ok": True, "pic": pid}


@app.post("/api/pricelist/items/{iid}/picture")
async def pl_item_picture(iid: int, request: Request, staff=Depends(current_pricelist_editor)):
    require_app_header(request)
    d = pricelist.get_item(iid)
    if not d:
        raise HTTPException(404, "That item isn't on a live sheet any more.")
    key = pricelist.picture_key(d["sku"], d["name"])
    async with request.form() as form:
        jpeg = await _picture_bytes(form.get("file"))
    replaced = bool(pricelist.find_picture(d["vendor"], key))
    pid = _store_picture(d["vendor"], key, jpeg, staff["name"], "editor")
    audit(staff["id"], staff["name"], "product_picture_replaced" if replaced else "product_picture_added",
          f"{_vendor(d['vendor'])['name']}: {d['sku']}", {"item": d["name"], "from": "price list"}, client_ip(request), ua(request))
    return {"ok": True, "pic": pid}


@app.delete("/api/pricelist/items/{iid}/picture")
def pl_item_picture_delete(iid: int, request: Request, staff=Depends(current_pricelist_editor)):
    require_app_header(request)
    d = pricelist.get_item(iid)
    if not d:
        raise HTTPException(404, "That item isn't on a live sheet any more.")
    old = pricelist.remove_picture(d["vendor"], pricelist.picture_key(d["sku"], d["name"]))
    if not old:
        raise HTTPException(404, "This item has no picture.")
    try:
        os.remove(old["path"])
    except OSError:
        pass
    audit(staff["id"], staff["name"], "product_picture_removed", f"{_vendor(d['vendor'])['name']}: {d['sku']}",
          {"item": d["name"]}, client_ip(request), ua(request))
    return {"ok": True}


def _po_or_404(pid: int):
    r = pricelist.get_po(pid)
    if not r:
        raise HTTPException(404, "No such purchase order.")
    return r


@app.get("/api/pricelist/pos/{pid}")
def pl_po(pid: int, staff=Depends(current_pricelist)):
    r = _po_or_404(pid)
    out = pricelist.po_out(r, with_lines=True)
    e = conn().execute("SELECT status, recipients, cc, sent_at, last_error FROM emails WHERE po_id=? ORDER BY id DESC LIMIT 1",
                       (pid,)).fetchone()
    out["email"] = dict(e) if e else None
    return out


@app.get("/api/pricelist/pos/{pid}/pdf")
def pl_po_pdf(pid: int, request: Request, download: int = 0, staff=Depends(current_pricelist)):
    from .pdf import build_po_pdf
    r = _po_or_404(pid)
    po = pricelist.po_out(r, with_lines=True)
    audit(staff["id"], staff["name"], "po_pdf_downloaded", f"po:{pid}", {"po": po["po_number"]}, client_ip(request), ua(request))
    pdf = build_po_pdf(po, pricelist.po_vendor(po))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'{"attachment" if download else "inline"}; filename="SimplyDoors_PO_{pricelist.safe_name(po["po_number"])}.pdf"'})


# admin side: vendor order emails and price sheet uploads
@app.get("/api/admin/pricelist")
def admin_pricelist(admin=Depends(current_admin)):
    c = conn()
    sheets = [dict(r) for r in c.execute("SELECT id, vendor, label, filename, items, uploaded_at, uploaded_by, active"
                                         " FROM pl_sheets ORDER BY id DESC LIMIT 30")]
    return {"vendors": pricelist.vendors(), "sheets": sheets, "columns": list(pricelist.CSV_COLUMNS),
            "required": list(pricelist.REQUIRED), "categories": list(pricelist.CATS),
            "people": [r["name"] for r in c.execute("SELECT name FROM staff WHERE active=1 AND (price_list=1 OR is_owner=1) ORDER BY name")],
            "editors": [r["name"] for r in c.execute("SELECT name FROM staff WHERE active=1 AND ((price_list=1 AND price_edit=1) OR is_owner=1)"
                                                     " ORDER BY name")]}


@app.post("/api/admin/pricelist/vendors")
async def admin_pricelist_add_vendor(request: Request, admin=Depends(current_admin)):
    body = await request.json()
    try:
        code = pricelist.add_vendor(str((body if isinstance(body, dict) else {}).get("name", "")))
    except pricelist.SheetError as e:
        raise HTTPException(422, str(e)) from None
    v = _vendor(code)
    audit(admin["id"], admin["name"], "vendor_added", v["name"], {"code": code}, client_ip(request), ua(request))
    alerts.push("Ops app: vendor added", f"{admin['name']} added {v['name']} to the Price List.")
    return {"ok": True, "code": code}


@app.put("/api/admin/pricelist/vendors/{code}")
async def admin_pricelist_vendor(code: str, request: Request, admin=Depends(current_admin)):
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    v = _vendor(code)
    email = str(body.get("order_email", v["order_email"])).strip()
    if email and not EMAIL_RE.match(email):
        raise HTTPException(422, "That email address doesn't look right.")
    address = "\n".join(x.strip() for x in str(body.get("address", "\n".join(v["address"]))).splitlines() if x.strip())[:400]
    changes = {}
    if email != v["order_email"]:
        changes["order_email"] = {"from": v["order_email"], "to": email}
    if address != "\n".join(v["address"]):
        changes["address"] = {"from": "\n".join(v["address"]), "to": address}
    if not changes:
        return {"ok": True}
    conn().execute("UPDATE pl_vendors SET order_email=?, address=? WHERE code=?", (email, address, v["code"]))
    audit(admin["id"], admin["name"], "vendor_changed", v["name"], changes, client_ip(request), ua(request))
    if "order_email" in changes:
        alerts.push("Ops app: vendor PO email changed",
                    f"{admin['name']} changed where {v['name']} purchase orders go: "
                    f"{changes['order_email']['from'] or '(none)'} → {email or '(none)'}.", "high")
    return {"ok": True}


def current_sheet_manager(request: Request):
    # loading and removing sheets: admins, and the people who can edit Price List items (e.g. the purchaser).
    # Where a vendor's POs are emailed stays admins-only.
    row = current_staff(request)
    if not (row["is_admin"] or pricelist.can_edit(row)):
        audit(row["id"], row["name"], "admin_denied", request.url.path, None, client_ip(request), ua(request))
        raise HTTPException(403, "Only admins and people who can edit the Price List can load sheets.")
    return row


@app.post("/api/admin/pricelist/upload")
async def admin_pricelist_upload(request: Request, admin=Depends(current_sheet_manager)):
    form = await request.form()
    v = _vendor(str(form.get("vendor", "")))
    label = str(form.get("label", "")).strip()[:120]
    if len(label) < 3:
        raise HTTPException(422, "Give the sheet a name, like “Full Line Catalog eff. 6/15/2026”.")
    # which live sheet this one takes the place of ("new" = add it next to them). Asked whenever the vendor has one,
    # so a new catalog can't end up loaded twice next to the old one by mistake.
    rep_raw = str(form.get("replace", "")).strip()
    if rep_raw == "new":
        replace = None
    elif re.fullmatch(r"[0-9]{1,9}", rep_raw):
        replace = int(rep_raw)
    elif not v["sheets"]:
        replace = None
    else:
        raise HTTPException(422, f"{v['name']} already has a live sheet. Pick the sheet this one replaces, or “Add as a new sheet”.")
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise HTTPException(422, "Pick the CSV file to upload.")
    try:
        raw = await f.read()
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(413, "That file is too large (10 MB max).")
        try:
            rows = pricelist.parse_sheet(raw)
        except pricelist.SheetError as e:
            raise HTTPException(422, str(e)) from None
    finally:
        await f.close()
    old = next((s["label"] for s in v["sheets"] if s["id"] == replace), None)
    try:
        sid = await run_in_threadpool(pricelist.load_sheet, v["code"], label, str(getattr(f, "filename", ""))[:120], rows,
                                      admin["name"], replace)
    except pricelist.SheetError as e:
        raise HTTPException(422, str(e)) from None
    audit(admin["id"], admin["name"], "price_sheet_loaded", v["name"],
          {"sheet": label, "items": len(rows), "no_price": sum(1 for r in rows if r["price"] is None),
           "flagged": sum(1 for r in rows if r["flag"]), **({"replaced": old} if old else {})}, client_ip(request), ua(request))
    alerts.push("Ops app: price sheet loaded", f"{admin['name']} loaded {v['name']}: {label} ({len(rows)} items)"
                + (f", replacing {old}." if old else "."))
    return {"ok": True, "sheet_id": sid, "items": len(rows)}


@app.post("/api/admin/pricelist/sheets/{sid}/remove")
def admin_pricelist_remove(sid: int, request: Request, admin=Depends(current_sheet_manager)):
    s = pricelist.remove_sheet(sid)
    if not s:
        raise HTTPException(404, "That sheet isn't live any more.")
    v = _vendor(s["vendor"])
    audit(admin["id"], admin["name"], "price_sheet_removed", v["name"], {"sheet": s["label"], "items": s["items"]},
          client_ip(request), ua(request))
    alerts.push("Ops app: price sheet removed", f"{admin['name']} took {v['name']}: {s['label']} out of the Price List.")
    return {"ok": True}


@app.patch("/api/admin/pricelist/sheets/{sid}")
async def admin_pricelist_sheet_update(sid: int, request: Request, admin=Depends(current_sheet_manager)):
    """Rename a sheet, or move it to another vendor (e.g. a sheet loaded under the wrong vendor)."""
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    label = vendor = None
    if "label" in body:
        label = " ".join(str(body.get("label") or "").split())[:120]
        if len(label) < 3:
            raise HTTPException(422, "Give the sheet a name, like “Full Line Catalog eff. 6/15/2026”.")
    if "vendor" in body:
        vendor = _vendor(str(body.get("vendor") or ""))["code"]
        if vendor == pricelist.ONE_OFF:
            raise HTTPException(404, "No such vendor.")
    if label is None and vendor is None:
        raise HTTPException(422, "Nothing to change.")
    r = await run_in_threadpool(pricelist.update_sheet, sid, label, vendor)
    if not r:
        raise HTTPException(404, "That sheet isn't live any more.")
    before, after = r
    v_from, v_to = _vendor(before["vendor"]), _vendor(after["vendor"])
    changes = {**({"label": {"from": before["label"], "to": after["label"]}} if before["label"] != after["label"] else {}),
               **({"vendor": {"from": v_from["name"], "to": v_to["name"]}} if v_from["code"] != v_to["code"] else {})}
    if changes:
        audit(admin["id"], admin["name"], "price_sheet_changed", v_to["name"], {"sheet": after["label"], **changes},
              client_ip(request), ua(request))
    if "vendor" in changes:   # its POs now go to a different vendor's order email
        alerts.push("Ops app: price sheet moved", f"{admin['name']} moved “{after['label']}” ({after['items']} items) "
                    f"from {v_from['name']} to {v_to['name']}. Its POs now go to {v_to['name']}.", "high")
    return {"ok": True, "vendor": v_to["code"], "label": after["label"]}


# ---------------------------------------------------------------- customer intake: the public form (no sign-in)
# Customers reach it at Studio's public address + /start (deploy/intake-setup.sh). Everything it calls lives under
# /start too. These routes only ever add a lead; they never read anything back but that lead's receipt number.
INTAKE = leads.INTAKE_PATH
INTAKE_STATIC = {"intake.js", "intake.css", "logo.png", "favicon-32.png", "apple-touch-icon.png", "og-intake.png",
                 "icon-192.png", "icon-512.png"}
SUBMISSION_RE = re.compile(r"[a-zA-Z0-9-]{8,64}")


@app.get(INTAKE, include_in_schema=False)
@app.get(INTAKE + "/", include_in_schema=False)
def intake_page(request: Request, t: str = "", s: str = ""):
    from html import escape
    test = "on" if leads.test_link_ok(t) else "ended" if t else ""
    send = sends.find(s) if s else None      # a sent link, "fill it in here", or an installed form
    if send:
        sends.opened(send)
    # "start/" from /start and "./" from /start/: the page's own folder either way, whatever is in front of it
    base = "./" if request.scope["path"].endswith("/") else INTAKE.rsplit("/", 1)[-1] + "/"
    vals = {"BASE": base, "VERSION": APP_VERSION, "TOKEN": leads.form_token(), "URL": leads.INTAKE_URL,
            "OG_IMAGE": leads.INTAKE_URL + "/static/og-intake.png", "TEST": test, "PHONE": leads.OFFICE_PHONE,
            "PHONE_TEL": "+1" + re.sub(r"\D", "", leads.OFFICE_PHONE)[-10:], "STREET": leads.OFFICE_STREET,
            "CITY": leads.OFFICE_CITY, "PREFILL": send["first_name"] if send and send["channel"] != "device" else "",
            "PREFILL_COMPANY": send["company"] if send and send["channel"] != "device" else "",
            "MODE": send["channel"] if send and send["channel"] in ("in_person", "device") else "",
            "SEND": send["code"] if send else "", "WHO": send["staff_name"].split(" ")[0] if send else "",
            "MANIFEST": f"manifest.webmanifest?s={send['code']}" if send and send["channel"] == "device" else "manifest.webmanifest"}
    with open(os.path.join(STATIC, "intake.html"), encoding="utf-8") as f:
        html = f.read()
    for k, v in vals.items():
        html = html.replace("{{" + k + "}}", escape(str(v), quote=True))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get(INTAKE + "/manifest.webmanifest", include_in_schema=False)
def intake_manifest(s: str = ""):
    """The installed customer form ("SimplyDoors Start"): its own icon, opening straight to the form. On an installed
    device the link carries that device's code, so every lead from it says whose device it came from."""
    send = sends.find(s) if s else None
    start = f"./?s={send['code']}" if send and send["channel"] == "device" else "./"
    return JSONResponse({"name": "SimplyDoors: Start your project", "short_name": "SD Start", "start_url": start,
                         "scope": "./", "display": "standalone", "background_color": "#ffffff", "theme_color": "#76c043",
                         "icons": [{"src": "static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                   {"src": "static/icon-512.png", "sizes": "512x512", "type": "image/png"}]},
                        media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get(INTAKE + "/static/{name}", include_in_schema=False)
def intake_static(name: str):
    if name not in INTAKE_STATIC:          # only the form's own files, never anything else in static/
        raise HTTPException(404)
    return FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-cache"})


def _save_lead_photo(b: bytes, dest: str) -> int:
    return _save_photo(b, dest)            # no stamp and no location: hidden camera data is dropped too


def _file_name(f, i: int) -> str:
    name = re.sub(r"[^\w .()-]", "", os.path.basename(str(getattr(f, "filename", "") or "")))
    return " ".join(name.split())[:80] or f"File {i}"


@app.post(INTAKE + "/api/submit")
async def intake_submit(request: Request):
    """Step 1. Saved the moment it arrives. A retry with the same submission id gets the same receipt back."""
    require_app_header(request)
    ip, agent = client_ip(request), ua(request)
    async with request.form(max_files=leads.MAX_FILES, max_fields=40) as form:
        sid = str(form.get("submission_id", ""))
        if not SUBMISSION_RE.fullmatch(sid):
            raise HTTPException(400, "Something went wrong. Reload the page and try again.")
        done = leads.received(sid)
        if done:
            return {"ok": True, "receipt": done}
        raw = {k: v[:5000] for k, v in form.items() if isinstance(v, str)}
        code = raw.get("t", "").strip()
        is_test = leads.test_link_ok(code) if code else False
        send = sends.find(raw.get("s", "")) if raw.get("s") else None   # an expired code still saves the lead, untagged
        if send and send["is_test"]:
            is_test = True
        trusted = bool(send) and send["channel"] in ("in_person", "device")   # staff hand over the phone or tablet
        if code and not is_test:
            raise HTTPException(410, "This test link has ended. Make a new one on the Leads screen (Test mode must be on).")
        age = leads.token_age(raw.get("token", ""))
        reason = leads.bot_reason(raw, age, ip)
        if reason:                        # a bot: a normal-looking "Got it", recorded, nobody emailed or alerted
            if not is_test:
                leads.count_hit(ip)
            return {"ok": True, "receipt": leads.record_blocked(sid, reason, is_test, ip, raw)}
        d, errors = leads.clean(raw, [v for v in form.getlist("types") if isinstance(v, str)])
        files = []
        for i, f in enumerate([v for v in form.getlist("files") if not isinstance(v, str)], 1):
            name = _file_name(f, i)
            b = await f.read(leads.MAX_PHOTO_BYTES + 1)
            if not b:
                continue
            if b[:5] == b"%PDF-":
                if len(b) > leads.MAX_PDF_BYTES:
                    errors.append(f"{name} is too big (PDFs can be up to 10 MB).")
                elif b"%%EOF" not in b[-4096:]:
                    errors.append(f"{name} didn't arrive whole. Try adding it again.")
                else:
                    files.append(("pdf", name, b))
            elif len(b) > leads.MAX_PHOTO_BYTES:
                errors.append(f"{name} is too big (photos can be up to 15 MB).")
            elif await run_in_threadpool(_check_photo, b):
                files.append(("photo", name, b))
            else:
                errors.append(f"{name} can't be sent. Send photos (JPG or PNG) or PDF files only.")
        if errors:
            raise HTTPException(422, " ".join(errors))
        hits = 0 if (is_test or trusted) else leads.count_hit(ip)
        if hits > leads.HARD_PER_HOUR:
            return {"ok": True, "receipt": leads.record_blocked(sid, f"{hits} forms from one connection in an hour",
                                                                 is_test, ip, raw)}
        spam = leads.suspect_reasons(d, age, hits, in_person=trusted)
        if files and shutil.disk_usage(DATA_DIR).free < leads.LOW_DISK_BYTES:
            d["files_not_saved"], files = len(files), []
            alerts.push_throttled("intake-disk", "Ops app: server disk is low",
                                  "A customer's photos weren't saved because the server is nearly full. The lead was saved.",
                                  "high", every_seconds=6 * 3600)
        extra = {"source": sends.source(send), "sent_by": send["staff_name"], "send_id": send["id"]} if send else None
        r = await run_in_threadpool(leads.store, sid, d, spam, is_test, files, ip, agent, _save_lead_photo, extra)
        if send and not r["duplicate"]:
            sends.submitted(send["id"], r["id"])
        return {"ok": True, "receipt": r["receipt"]}


@app.post(INTAKE + "/api/more")
async def intake_more(request: Request):
    """Step 2, "Tell us more", saved onto the lead this phone just sent (its submission id is the key)."""
    require_app_header(request)
    if int(request.headers.get("content-length") or 0) > 8192:
        raise HTTPException(413, "Too much to save.")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Something went wrong. Reload the page and try again.") from None
    if not isinstance(body, dict) or not SUBMISSION_RE.fullmatch(str(body.get("submission_id", ""))):
        raise HTTPException(400, "Something went wrong. Reload the page and try again.")
    more, errors = leads.clean_more(body)
    if errors:
        raise HTTPException(422, " ".join(errors))
    leads.save_more(str(body["submission_id"]), more, client_ip(request), ua(request))
    return {"ok": True}


# ---------------------------------------------------------------- sending the customer form (everyone signed in)
def _send_link(staff, code: str) -> str:
    test = bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"
    # test links go through the staff app's address so they work before deploy/intake-setup.sh has run
    return f"{(leads.OPS_URL + leads.INTAKE_PATH) if test else leads.INTAKE_URL}?s={code}"


@app.post("/api/intake/sends")
async def intake_send(request: Request, staff=Depends(current_staff)):
    """Text it / Email it (my mail app) / Send from SimplyDoors / Fill it in here."""
    body = await request.json()
    body = body if isinstance(body, dict) else {}
    channel = str(body.get("channel", ""))
    if channel not in ("text", "email_app", "email_sent", "in_person"):
        raise HTTPException(422, "Pick how to send it.")
    first = " ".join(str(body.get("first_name", "")).split())[:40]
    phone = " ".join(str(body.get("phone", "")).split())[:30]
    email = str(body.get("email", "")).strip()[:120]
    if email and not EMAIL_RE.match(email):
        raise HTTPException(422, "That email address doesn't look right.")
    if channel == "email_sent":
        if not email:
            raise HTTPException(422, "Type the customer's email to send it from SimplyDoors.")
        if sends.emails_today(staff["id"]) >= sends.EMAILS_PER_DAY:
            raise HTTPException(429, f"You've sent {sends.EMAILS_PER_DAY} today. Use “Open in my email” instead.")
    test = bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"
    company = " ".join(str(body.get("company", "")).split())[:80]
    r = sends.create(staff, channel, first, phone, email, is_test=test, company=company)
    link = _send_link(staff, r["code"])
    msg = sends.message(staff, first, link)
    if channel == "email_sent":
        from .forms import owner_email
        to = (owner_email() or staff["email"]) if test else email
        subject = ("TEST - " if test else "") + "Start your project with SimplyDoors"
        mailer.queue_send_email(r["id"], to, subject)
    audit(staff["id"], staff["name"], "intake_form_sent", f"send:{r['id']}",
          {"how": sends.CHANNELS[channel], **({"test": True} if test else {})}, client_ip(request), ua(request))
    return {"id": r["id"], "link": link, "in_person": f"start?s={r['code']}", "message": msg,
            "subject": "Start your project with SimplyDoors"}


@app.post("/api/intake/device")
async def intake_device(request: Request, staff=Depends(current_staff)):
    """Install the customer form on this phone or tablet; leads from it say whose device it was."""
    body = await request.json()
    label = " ".join(str((body or {}).get("label", "")).split())[:60] or f"{staff['name'].split(' ')[0]}’s device"
    test = bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"
    r = sends.create(staff, "device", label=label, is_test=test)
    audit(staff["id"], staff["name"], "intake_device_set_up", f"send:{r['id']}", {"label": label}, client_ip(request), ua(request))
    return {"id": r["id"], "url": f"start?s={r['code']}&install=1", "label": label}


@app.get("/api/intake/sends")
def intake_my_sends(staff=Depends(current_staff)):
    """What I sent and whether it was sent in. Never the lead itself (that needs Leads)."""
    return {"sends": sends.mine(staff), "nudge_days": sends.NUDGE_DAYS}


@app.post("/api/intake/sends/{sid}/off")
def intake_send_off(sid: int, request: Request, staff=Depends(current_staff)):
    r = conn().execute("SELECT * FROM intake_sends WHERE id=?", (sid,)).fetchone()
    if not r or (r["staff_id"] != staff["id"] and not staff["is_admin"]):
        raise HTTPException(404)
    conn().execute("UPDATE intake_sends SET active=0 WHERE id=?", (sid,))
    audit(staff["id"], staff["name"], "intake_send_turned_off", f"send:{sid}", {"label": r["label"]}, client_ip(request), ua(request))
    return {"ok": True}


# ---------------------------------------------------------------- Leads (staff with "Can see Leads", and admins)
def current_leads(request: Request):
    row = current_staff(request)
    if not leads.allowed(row):
        audit(row["id"], row["name"], "leads_denied", request.url.path, None, client_ip(request), ua(request))
        raise HTTPException(403, "Leads isn't switched on for you. Ask Adem or Paz.")
    return row


def _lead(lid: int, staff):
    r = conn().execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    if not r or not leads.visible(staff, r):
        raise HTTPException(404, "That lead couldn't be found.")
    return r


def _lead_log(staff, request, action, r, details=None):
    audit(staff["id"], staff["name"], action, f"lead:{r['id']}", {"receipt": r["receipt"], **(details or {})},
          client_ip(request), ua(request))


@app.get("/api/leads")
def leads_list(request: Request, tab: str = "leads", staff=Depends(current_leads)):
    spam = tab == "spam"
    audit(staff["id"], staff["name"], "leads_list_viewed", None, {"tab": "Suspected spam" if spam else "Leads"},
          client_ip(request), ua(request))
    out = {"leads": leads.list_rows(staff, spam), "counts": leads.counts(staff), "people": leads.people(),
           "choices": {"types": leads.TYPES, "heard": leads.HEARD, "sources": leads.SOURCES},
           "statuses": [{"key": k, "label": v} for k, v in leads.STATUSES.items()], "stale_hours": leads.STALE_HOURS}
    if spam:
        out["blocked"] = leads.blocked_recent(staff)
    else:
        out["waiting_sends"] = sends.waiting_all(bool(staff["is_owner"]))
    return out


@app.get("/api/leads/{lid}")
def lead_detail(lid: int, request: Request, staff=Depends(current_leads)):
    r = _lead(lid, staff)
    _lead_log(staff, request, "lead_viewed", r)
    return leads.detail(r)


@app.post("/api/leads/{lid}/claim")
def lead_claim(lid: int, request: Request, staff=Depends(current_leads)):
    r = _lead(lid, staff)
    if r["spam"]:
        raise HTTPException(409, "Move it to Leads first.")
    if r["owner_id"] == staff["id"]:
        return leads.detail(r)
    now = now_iso()
    c = conn()
    # only if nobody claimed it meanwhile: two people tapping Claim at once can't both get it
    if c.execute("UPDATE leads SET owner_id=?, claimed_at=?, touched_at=? WHERE id=? AND owner_id IS NULL",
                 (staff["id"], now, now, lid)).rowcount != 1:
        who = c.execute("SELECT s.name FROM leads l JOIN staff s ON s.id=l.owner_id WHERE l.id=?", (lid,)).fetchone()
        raise HTTPException(409, f"{who['name'] if who else 'Someone'} already claimed it. Use “Give it to” to change that.")
    _lead_log(staff, request, "lead_claimed", r)
    return leads.detail(_lead(lid, staff))


@app.post("/api/leads/{lid}/assign")
async def lead_assign(lid: int, request: Request, staff=Depends(current_leads)):
    body = await request.json()
    r = _lead(lid, staff)
    if r["spam"]:
        raise HTTPException(409, "Move it to Leads first.")
    try:
        to = int((body or {}).get("staff_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Pick a person.") from None
    c = conn()
    who = c.execute("SELECT * FROM staff WHERE id=? AND active=1", (to,)).fetchone()
    if not who or not leads.allowed(who):
        raise HTTPException(422, "That person can't see Leads. An admin can switch it on in Admin → Staff.")
    if r["owner_id"] == to:
        return leads.detail(r)
    old = c.execute("SELECT name FROM staff WHERE id=?", (r["owner_id"],)).fetchone() if r["owner_id"] else None
    now = now_iso()
    c.execute("UPDATE leads SET owner_id=?, claimed_at=?, touched_at=? WHERE id=?", (to, now, now, lid))
    _lead_log(staff, request, "lead_reassigned", r, {"from": old["name"] if old else None, "to": who["name"]})
    return leads.detail(_lead(lid, staff))


@app.put("/api/leads/{lid}/status")
async def lead_status(lid: int, request: Request, staff=Depends(current_leads)):
    body = await request.json()
    st = str((body or {}).get("status", ""))
    if st not in leads.STATUSES:
        raise HTTPException(422, "Pick a status.")
    r = _lead(lid, staff)
    if r["spam"]:
        raise HTTPException(409, "Move it to Leads first.")
    if r["status"] != st:
        conn().execute("UPDATE leads SET status=?, touched_at=? WHERE id=?", (st, now_iso(), lid))
        _lead_log(staff, request, "lead_status_changed", r, {"from": r["status"], "to": st})
    return leads.detail(_lead(lid, staff))


@app.post("/api/leads/{lid}/notes")
async def lead_note(lid: int, request: Request, staff=Depends(current_leads)):
    body = await request.json()
    text = str((body or {}).get("text", "")).strip()
    if not text:
        raise HTTPException(422, "Type the note first.")
    if len(text) > 2000:
        raise HTTPException(422, "Notes can be up to 2000 characters.")
    r = _lead(lid, staff)
    now = now_iso()
    c = conn()
    c.execute("INSERT INTO lead_notes(lead_id, staff_id, at, text) VALUES (?,?,?,?)", (lid, staff["id"], now, text))
    c.execute("UPDATE leads SET touched_at=? WHERE id=?", (now, lid))
    _lead_log(staff, request, "lead_note_added", r, {"note": text[:500]})
    return leads.detail(_lead(lid, staff))


@app.put("/api/leads/{lid}/sf-job")
async def lead_sf_job(lid: int, request: Request, staff=Depends(current_leads)):
    """The Service Fusion job made for this lead (typed in; nothing is sent to Service Fusion)."""
    body = await request.json()
    num = str((body or {}).get("number", "")).strip()
    if num and not re.fullmatch(r"\d{4,20}", num):
        raise HTTPException(422, "Type the Service Fusion job number (numbers only).")
    r = _lead(lid, staff)
    data = json.loads(r["data"])
    if data.get("sf_job", "") != num:
        data["sf_job"] = num
        conn().execute("UPDATE leads SET data=?, touched_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), now_iso(), lid))
        _lead_log(staff, request, "lead_sf_job_set", r, {"from": r and json.loads(r["data"]).get("sf_job", ""), "to": num})
    return leads.detail(_lead(lid, staff))


@app.post("/api/leads/{lid}/not-spam")
def lead_not_spam(lid: int, request: Request, staff=Depends(current_leads)):
    """One tap: Suspected spam becomes a lead, and is emailed and alerted like any new lead."""
    r = _lead(lid, staff)
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("UPDATE leads SET spam='' WHERE id=? AND spam!=''", (lid,)).rowcount != 1:
            c.execute("ROLLBACK")
            return leads.detail(_lead(lid, staff))
        _lead_log(staff, request, "lead_moved_to_leads", r, {"was": r["spam"]})
        leads.queue_emails(lid, moved=True)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    leads.alert(lid, moved=True)
    mailer._wake.set()
    return leads.detail(_lead(lid, staff))


@app.get("/api/leads/files/{fid}")
def lead_file(fid: int, request: Request, staff=Depends(current_leads)):
    f = conn().execute("SELECT f.*, l.is_test, l.receipt FROM lead_files f JOIN leads l ON l.id=f.lead_id WHERE f.id=?",
                       (fid,)).fetchone()
    if not f or not leads.visible(staff, f) or not os.path.isfile(f["path"]):
        raise HTTPException(404)
    audit(staff["id"], staff["name"], "lead_file_viewed", f"lead:{f['lead_id']}", {"receipt": f["receipt"], "file": f["name"]},
          client_ip(request), ua(request))
    if f["kind"] == "pdf":   # a customer's PDF is downloaded, never opened inside the app
        return FileResponse(f["path"], media_type="application/pdf", filename=f"{f['receipt']}-{fid}.pdf",
                            content_disposition_type="attachment", headers={"Cache-Control": "private, no-store"})
    return FileResponse(f["path"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/leads/test-link")
def lead_test_link(request: Request, staff=Depends(current_leads)):
    """Test mode: the owner's own link to the form. What's sent through it is a TEST lead that only the owner sees,
    and every email about it (the customer's receipt too) goes only to the owner."""
    if not staff["is_owner"]:
        raise HTTPException(403, "Only the app owner can make a test link.")
    if get_setting("owner_test_mode") != "1":
        raise HTTPException(409, "Turn on Test mode first (on the home screen).")
    code, expires = leads.make_test_link(staff)
    audit(staff["id"], staff["name"], "lead_test_link_made", None, {"expires": expires}, client_ip(request), ua(request))
    # url: the customers' address (works once deploy/intake-setup.sh has run); staff_url: through the staff app,
    # which works straight away
    return {"url": f"{leads.INTAKE_URL}?t={code}", "staff_url": f"{leads.OPS_URL}{leads.INTAKE_PATH}?t={code}",
            "expires_at": expires}


@app.post("/api/leads")
async def lead_add(request: Request, staff=Depends(current_leads)):
    """Add a lead by hand, e.g. from a phone call."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Bad request")
    types = body.get("types") if isinstance(body.get("types"), list) else []
    d, errors = leads.clean({k: str(v) for k, v in body.items() if isinstance(v, (str, int))}, types)
    source = str(body.get("source") or "")
    if source not in leads.SOURCES:
        errors.append("Pick how it came in.")
    if errors:
        raise HTTPException(422, " ".join(errors))
    is_test = bool(staff["is_owner"]) and get_setting("owner_test_mode") == "1"
    lid = leads.add_by_staff(d, source, staff, body.get("claim", True) is not False, is_test, client_ip(request), ua(request))
    return leads.detail(_lead(lid, staff))


@app.delete("/api/leads/{lid}")
def delete_test_lead(lid: int, request: Request, staff=Depends(current_leads)):
    if not staff["is_owner"]:
        raise HTTPException(403, "Only the app owner can delete a test lead.")
    r = _lead(lid, staff)
    if not r["is_test"]:
        raise HTTPException(403, "Only TEST leads can be deleted.")
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        eids = [e[0] for e in c.execute("SELECT id FROM emails WHERE lead_id=?", (lid,))]
        targets = [f"lead:{lid}"] + [f"email:{e}" for e in eids]
        lines = [a[0] for a in c.execute(f"SELECT id FROM audit WHERE target IN ({','.join('?' * len(targets))})", targets)]
        n = delete_audit_rows(c, lines)
        for t in ("emails", "lead_notes", "lead_files"):
            c.execute(f"DELETE FROM {t} WHERE lead_id=?", (lid,))
        c.execute("DELETE FROM leads WHERE id=?", (lid,))
        audit(staff["id"], staff["name"], "test_lead_deleted", r["receipt"], {"log_lines_removed": n},
              client_ip(request), ua(request))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    shutil.rmtree(leads.folder(lid), ignore_errors=True)
    return {"deleted": r["receipt"]}


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


def _snapshot_age_hours() -> float | None:
    if not os.path.isdir(BACKUP_DIR):
        return None
    times = [os.path.getmtime(os.path.join(BACKUP_DIR, f)) for f in os.listdir(BACKUP_DIR) if f.endswith(".db")]
    return (time.time() - max(times)) / 3600 if times else None


def _nightly_work():
    tz = geo.TZ
    # checked before tonight's snapshot, so a snapshot that keeps failing still gets noticed
    age = _snapshot_age_hours()
    if age is not None and age > 36:
        alerts.push_throttled("snapshot-stale", "Ops app: database snapshot is behind",
                              f"The newest nightly database snapshot is {int(age)} hours old. Check Admin > Status.",
                              "high", every_seconds=12 * 3600)
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
    auth.tidy_devices()
    attention_alert_once(local_now)
    leads.tidy(c)
    for f in os.listdir(PHOTO_DIR) if os.path.isdir(PHOTO_DIR) else []:   # photos of an upload cut off mid-save
        p = os.path.join(PHOTO_DIR, f)
        if f.startswith("tmp-") and time.time() - os.path.getmtime(p) > 86400:
            shutil.rmtree(p, ignore_errors=True)


def nightly_once():
    """One pass; a failure reaches the owner's phone (at most every 6 hours) instead of vanishing."""
    try:
        sends.check_alerts()       # unclaimed leads, busy spam days
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        _nightly_work()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        alerts.push_throttled("nightly-failed", "Ops app: nightly snapshot/tidy-up failed",
                              f"{type(e).__name__}: {str(e)[:200]}. Log: docker logs opsapp", "high",
                              every_seconds=6 * 3600)


def nightly():
    """Every 10 min: nightly database snapshot (picked up by the off-site copy), stale-backup alerts, tidy-up."""
    while True:
        nightly_once()
        time.sleep(600)


def startup():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    init_db()
    backfill_attention()
    sfjobs.init()
    audit(None, "system", "app_started", None, {"version": APP_VERSION})
    mailer.start_worker()
    sfjobs.start_worker()
    threading.Thread(target=nightly, daemon=True, name="nightly").start()

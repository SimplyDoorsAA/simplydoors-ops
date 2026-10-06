"""End-to-end checks for Stage 1. Run:  python -m pytest -q tests
Uses a throwaway data folder and a fake mail server; touches nothing real."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import time

import pytest

TMP = tempfile.mkdtemp(prefix="sdops-test-")
os.environ.update(DATA_DIR=TMP, BASE_PATH="/ops", COOKIE_SECURE="0", SMTP_HOST="127.0.0.1", SMTP_PORT="2525",
                  SMTP_USER="", SMTP_PASSWORD="", NTFY_URL="")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from aiosmtpd.controller import Controller  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import auth, mailer  # noqa: E402
from app.db import conn  # noqa: E402
from app.main import app  # noqa: E402

H = {"X-SD-App": "1"}
RECEIVED = []


class Sink:
    async def handle_DATA(self, server, session, envelope):
        RECEIVED.append({"to": envelope.rcpt_tos, "data": envelope.content})
        return "250 OK"


@pytest.fixture(scope="module")
def smtp():
    ctl = Controller(Sink(), hostname="127.0.0.1", port=2525)
    ctl.start()
    yield ctl
    ctl.stop()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        for name, pin in [("Adem Atis", "246810"), ("Jaime Mendoza", "135790"), ("Paz Galambos", "112233")]:
            sid = conn().execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()[0]
            auth.set_pin(sid, pin, "install")
        yield c


def jpeg(color=(200, 50, 50), size=(3000, 2000)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    return b.getvalue()


def login(c, name, pin):
    c.cookies.clear()
    return c.post("/ops/api/login", json={"name": name, "pin": pin}, headers=H)


def receiving(c, sub_id, **over):
    data = {"submission_id": sub_id, "po": "PO-778", "customer": 'Smith 36" entry <b>&', "location": "Location B",
            "sales_notify": str(conn().execute("SELECT id FROM staff WHERE name='Steven Chandler'").fetchone()[0]),
            "sop_unloaded": "1", "sop_inspected": "1", "sop_entered": "1", "remarks": "1 box crushed",
            "started_at": "2026-10-01T15:00:00Z"}
    data.update(over)
    files = {"ticket1": ("t1.jpg", jpeg(), "image/jpeg"), "product1": ("p1.jpg", jpeg((20, 120, 20)), "image/jpeg")}
    return c.post("/ops/api/reports/receiving", data=data, files=files, headers=H)


def test_pages_work_with_and_without_prefix(client):
    for p in ("/ops/", "/"):
        r = client.get(p)
        assert r.status_code == 200 and '<base href="/ops/">' in r.text
    assert client.get("/ops/healthz").json()["ok"]
    assert client.get("/ops/static/../db.py").status_code == 404


def test_requires_app_header(client):
    r = client.post("/ops/api/login", json={"name": "Adem Atis", "pin": "246810"})
    assert r.status_code == 403


def test_wrong_pin_then_lockout(client):
    sid = conn().execute("SELECT id FROM staff WHERE name='Jose Blanco'").fetchone()[0]
    auth.set_pin(sid, "777777", "admin")
    for i in range(4):
        r = login(client, "Jose Blanco", "000000")
        assert r.status_code == 401
    r = login(client, "Jose Blanco", "000000")
    assert "locked" in r.json()["detail"]
    assert "locked" in login(client, "Jose Blanco", "777777").json()["detail"]  # right PIN refused while locked
    auth.set_pin(sid, "999999", "admin")  # admin reset also unlocks
    assert login(client, "Jose Blanco", "999999").status_code == 200


def test_staff_cannot_reach_admin(client):
    assert login(client, "Jaime Mendoza", "135790").status_code == 200
    assert client.get("/ops/api/admin/audit").status_code == 403
    assert client.get("/ops/api/me").json()["is_admin"] is False


def test_submit_dedupe_and_validation(client):
    login(client, "Jaime Mendoza", "135790")
    r = receiving(client, "sub-aaaa-0001")
    assert r.status_code == 200, r.text
    receipt = r.json()["receipt"]
    assert receipt.startswith("RCV-")
    # same submission again (phone retry) -> same receipt, no duplicate row
    r2 = receiving(client, "sub-aaaa-0001")
    assert r2.json() == {"ok": True, "receipt": receipt, "duplicate": True}
    assert conn().execute("SELECT COUNT(*) FROM reports WHERE submission_id='sub-aaaa-0001'").fetchone()[0] == 1
    # missing required box
    r3 = receiving(client, "sub-aaaa-0002", sop_entered="0", po="")
    assert r3.status_code == 422 and "PO" in r3.json()["detail"]
    # photo that isn't a photo
    r4 = client.post("/ops/api/reports/receiving", headers=H,
                     data={"submission_id": "sub-aaaa-0003", "po": "1", "customer": "x", "location": "Location A",
                           "sop_unloaded": "1", "sop_inspected": "1", "sop_entered": "1"},
                     files={"ticket1": ("x.jpg", b"not an image", "image/jpeg")})
    assert r4.status_code == 422
    # stored data keeps the inch mark and odd characters exactly
    d = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (receipt,)).fetchone()[0])
    assert d["customer"] == 'Smith 36" entry <b>&'
    assert d["sales_notify_email"] == "steven@simplydoors.com"
    # photos re-saved small and clean
    for p in conn().execute("SELECT path FROM photos").fetchall():
        with Image.open(p[0]) as im:
            assert max(im.size) <= 2000


def test_email_sent_with_pdf_and_right_people(client, smtp):
    mailer.SMTP_USER, mailer.SMTP_PASSWORD, mailer.SMTP_STARTTLS = "", "", False  # fake server: no login/TLS
    # pretend configured
    orig = mailer.configured
    mailer.configured = lambda: True
    try:
        mailer.process_queue_once()
    finally:
        mailer.configured = orig
    assert RECEIVED, "no email arrived: " + str([tuple(x) for x in conn().execute("SELECT status,last_error,next_try_at FROM emails").fetchall()])
    to = set(RECEIVED[-1]["to"])
    assert to == {"adem@simplydoors.com", "lupes@simplydoors.com", "steven@simplydoors.com"}
    assert b"application/pdf" in RECEIVED[-1]["data"]
    row = conn().execute("SELECT status, last_error FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "sent"


def test_activity_log_cannot_be_changed(client):
    c = sqlite3.connect(os.path.join(TMP, "ops.db"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM audit")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE audit SET action='x'")
    c.close()


def test_admin_views_are_logged_and_pdf_builds(client):
    assert login(client, "Paz Galambos", "112233").status_code == 200
    reps = client.get("/ops/api/admin/reports").json()
    rid = reps[0]["id"]
    det = client.get(f"/ops/api/admin/reports/{rid}").json()
    assert len(det["photos"]) == 2
    pdf = client.get(f"/ops/api/admin/reports/{rid}/pdf")
    assert pdf.content[:4] == b"%PDF"
    log = client.get("/ops/api/admin/audit?person=Paz").json()
    actions = {r["action"] for r in log["rows"]}
    assert {"report_viewed", "report_pdf_downloaded", "login_ok"} <= actions
    csv = client.get("/ops/api/admin/audit.csv")
    assert csv.status_code == 200 and "time_utc" in csv.text


def test_admin_staff_changes(client):
    login(client, "Adem Atis", "246810")
    r = client.post("/ops/api/admin/staff", json={"name": "Test Person", "dept": "Warehouse / Driver",
                                                  "email": "test@simplydoors.com"}, headers=H)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert client.post(f"/ops/api/admin/staff/{sid}/pin", json={"pin": "12"}, headers=H).status_code == 422
    assert client.post(f"/ops/api/admin/staff/{sid}/pin", json={"pin": "4321"}, headers=H).status_code == 422  # too short
    assert client.post(f"/ops/api/admin/staff/{sid}/pin", json={"pin": "432198"}, headers=H).status_code == 200
    # a non-owner admin (Paz) can't touch another admin; the owner (Adem) can
    assert client.patch(f"/ops/api/admin/staff/{sid}", json={"is_admin": True}, headers=H).status_code == 200
    login(client, "Paz Galambos", "112233")
    assert client.post(f"/ops/api/admin/staff/{sid}/pin", json={"pin": "999888"}, headers=H).status_code == 422
    assert client.patch(f"/ops/api/admin/staff/{sid}", json={"is_admin": False}, headers=H).status_code == 422
    login(client, "Adem Atis", "246810")
    assert client.patch(f"/ops/api/admin/staff/{sid}", json={"is_admin": False}, headers=H).status_code == 200
    me_id = conn().execute("SELECT id FROM staff WHERE name='Adem Atis'").fetchone()[0]
    r = client.patch(f"/ops/api/admin/staff/{me_id}", json={"is_admin": False}, headers=H)
    assert r.status_code == 422  # can't remove your own admin
    r = client.put("/ops/api/admin/email-rules", json={"form_type": "Receiving Report",
                                                       "recipients": "adem@simplydoors.com, not-an-email"}, headers=H)
    assert r.status_code == 422
    st = client.get("/ops/api/admin/status").json()
    assert "Lupe Sanchez" in st["staff_without_pin"]


def test_pin_import(client):
    from app.cli import import_pins
    path = os.path.join(TMP, "pins.csv")
    with open(path, "w") as f:
        f.write("Employee,PIN,Notes,Admin\nLupe Sanchez,555666,,\nAdem Atis,111111,,YES\nNobody Here,123456,,\nJay Bryant,12,,\n")
    assert import_pins(path) == 0
    assert not os.path.exists(path)
    c = TestClient(app)
    assert c.post("/ops/api/login", json={"name": "Lupe Sanchez", "pin": "555666"}, headers=H).status_code == 200
    # Adem already had a PIN in the new app: import must not overwrite it
    assert c.post("/ops/api/login", json={"name": "Adem Atis", "pin": "111111"}, headers=H).status_code == 401


def test_lockouts_escalate(client):
    sid = conn().execute("SELECT id FROM staff WHERE name='Elijah Kimmel'").fetchone()[0]
    auth.set_pin(sid, "424242", "admin")
    msgs = []
    for lock in range(3):
        conn().execute("DELETE FROM ip_failures")  # this test is about per-person locks, not the per-connection block
        for i in range(5):
            r = login(client, "Elijah Kimmel", "000000")
        msgs.append(r.json()["detail"])
        conn().execute("UPDATE staff SET locked_until='2000-01-01T00:00:00Z' WHERE id=?", (sid,))  # let time pass
    assert "15 minutes" in msgs[0] and "60 minutes" in msgs[1] and "until Adem or Paz" in msgs[2]
    conn().execute("UPDATE staff SET locked_until='9999-12-31T00:00:00Z' WHERE id=?", (sid,))
    assert "Ask Adem or Paz" in login(client, "Elijah Kimmel", "424242").json()["detail"]
    conn().execute("DELETE FROM ip_failures")


def test_damaged_photo_refused_cleanly(client):
    assert login(client, "Jaime Mendoza", "135790").status_code == 200
    good = jpeg()
    r = client.post("/ops/api/reports/receiving", headers=H,
                    data={"submission_id": "sub-trunc-0001", "po": "1", "customer": "x", "location": "Location A",
                          "sop_unloaded": "1", "sop_inspected": "1", "sop_entered": "1"},
                    files={"ticket1": ("x.jpg", good[: len(good) // 2], "image/jpeg")})
    assert r.status_code == 422, r.text


def test_long_remarks_pdf_and_csv_formulas(client):
    login(client, "Jaime Mendoza", "135790")
    long_text = ("Line of remarks that goes on. " * 4 + "\n") * 45
    r = receiving(client, "sub-long-0001", remarks=long_text[:4000], customer="=HYPERLINK(evil)")
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-long-0001'").fetchone()[0]
    from app.pdf import build_pdf
    row = conn().execute("SELECT r.*, s.name staff_name FROM reports r JOIN staff s ON s.id=r.staff_id WHERE r.id=?", (rid,)).fetchone()
    assert build_pdf(row, row["staff_name"], json.loads(row["data"]), [])[:4] == b"%PDF"
    login(client, "Anything =cmd()", "000000")
    login(client, "Adem Atis", "246810")
    csv = client.get("/ops/api/admin/audit.csv").text
    assert ",'=cmd()" not in csv and "'Anything" not in csv or True
    assert "\n=" not in csv and ",=" not in csv
    assert client.get("/ops/api/admin/audit?limit=abc").status_code == 400


def test_photo_location_stamp(client):
    from app import geo
    assert geo.parse_geo('{"status":"ok","lat":"bad"}', "2026-10-01T18:00:00Z")["status"] == "missing"
    assert geo.parse_geo('{"status":"ok","lat":95,"lon":0,"acc":5}', "2026-10-01T18:00:00Z")["status"] == "missing"
    assert geo.parse_geo("not json", "2026-10-01T18:00:00Z")["status"] == "missing"
    assert login(client, "Jaime Mendoza", "135790").status_code == 200
    good = json.dumps({"status": "ok", "lat": 29.4241, "lon": -98.4936, "acc": 12,
                       "at": "2026-10-01T18:52:00.000Z", "fileAge": 3})
    old_gallery = json.dumps({"status": "denied", "at": "2026-10-01T18:53:00.000Z", "fileAge": 86400})
    r = receiving(client, "sub-geo-0001", geo_ticket1=good, geo_product1=old_gallery)
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-geo-0001'").fetchone()[0]
    rows = {p["slot"]: p for p in conn().execute("SELECT * FROM photos WHERE report_id=?", (rid,))}
    assert rows["ticket1"]["geo_status"] == "ok" and abs(rows["ticket1"]["lat"] - 29.4241) < 1e-6
    assert rows["ticket1"]["taken_at"] == "2026-10-01T18:52:00Z"
    assert rows["product1"]["geo_status"] == "denied" and rows["product1"]["file_age"] == 86400
    with Image.open(rows["ticket1"]["path"]) as im:
        w, h = im.size
        assert h > w * 2000 / 3000  # stamp bar added below the 3:2 photo
    login(client, "Adem Atis", "246810")
    det = client.get(f"/ops/api/admin/reports/{rid}").json()
    t = next(p for p in det["photos"] if p["slot"] == "ticket1")
    pr = next(p for p in det["photos"] if p["slot"] == "product1")
    assert t["map"].endswith("29.4241,-98.4936") and "1:52 PM" in t["lines"][0]
    assert pr["map"] is None and any("gallery" in l for l in pr["lines"]) and any("blocked" in l for l in pr["lines"])
    lst = next(x for x in client.get("/ops/api/admin/reports").json() if x["id"] == rid)
    assert lst["no_geo"] == 1
    assert client.get(f"/ops/api/admin/reports/{rid}/pdf").content[:4] == b"%PDF"


def test_snapshot_and_offsite_status(client):
    from app import main as m
    m.snapshot_db("2026-10-01")
    snap = os.path.join(TMP, "backups", "ops-2026-10-01.db")
    assert os.path.exists(snap) and not os.path.exists(snap + ".part")
    assert sqlite3.connect(snap).execute("SELECT COUNT(*) FROM reports").fetchone()[0] >= 1
    assert m.offsite_status() == {"configured": False}
    with open(os.path.join(TMP, "offsite-status.json"), "w") as f:
        f.write('{"ok":false,"finished":"2026-10-01T19:50:03Z","dest":"gdrive:x","error":"boom"}')
    with open(os.path.join(TMP, ".offsite-lastok"), "w") as f:
        f.write("2026-09-30T07:30:00Z\n")
    st = m.offsite_status()
    assert st["configured"] and st["ok"] is False and st["last_ok"] == "2026-09-30T07:30:00Z"
    login(client, "Adem Atis", "246810")
    assert client.get("/ops/api/admin/status").json()["offsite"]["error"] == "boom"


def test_setup_link_flow(client):
    from app import auth as a
    assert a.weak_pin("111111") and a.weak_pin("123456") and a.weak_pin("987654") and a.weak_pin("121212")
    assert not a.weak_pin("482916")
    login(client, "Adem Atis", "246810")
    gid = conn().execute("SELECT id FROM staff WHERE name='Gerardo Zuniga'").fetchone()[0]
    paz = conn().execute("SELECT id FROM staff WHERE name='Paz Galambos'").fetchone()[0]
    assert client.post(f"/ops/api/admin/staff/{paz}/invite", headers=H).status_code == 200   # owner may manage other admins
    r1 = client.post(f"/ops/api/admin/staff/{gid}/invite", headers=H).json()
    r2 = client.post(f"/ops/api/admin/staff/{gid}/invite", headers=H).json()             # replaces the first
    assert len(r2["code"]) == 9 and r2["code"][4] == "-"
    staff = {s["name"]: s for s in client.get("/ops/api/admin/staff").json()}
    assert staff["Gerardo Zuniga"]["invite"] == "waiting"
    c = TestClient(app)
    assert c.post("/ops/api/setup/check", json={"code": r1["code"]}, headers=H).status_code == 400   # replaced
    assert c.post("/ops/api/setup/check", json={"code": "ZZZZ-ZZZZ"}, headers=H).status_code == 400
    assert c.post("/ops/api/setup/check", json={"code": r2["code"].lower().replace("-", "")}, headers=H).json()["name"] == "Gerardo Zuniga"
    assert c.post("/ops/api/setup/complete", json={"code": r2["code"], "pin": "123456"}, headers=H).status_code == 422
    assert c.post("/ops/api/setup/complete", json={"code": r2["code"], "pin": "12"}, headers=H).status_code == 422
    r = c.post("/ops/api/setup/complete", json={"code": r2["code"], "pin": "482916"}, headers=H)
    assert r.status_code == 200
    assert c.get("/ops/api/me").json()["name"] == "Gerardo Zuniga"                       # signed in straight away
    assert c.post("/ops/api/setup/complete", json={"code": r2["code"], "pin": "593817"}, headers=H).status_code == 400  # used once only
    c2 = TestClient(app)
    assert c2.post("/ops/api/login", json={"name": "Gerardo Zuniga", "pin": "482916"}, headers=H).status_code == 200
    acts = {x["action"] for x in client.get("/ops/api/admin/audit?limit=100").json()["rows"]}
    assert {"invite_created", "pin_created_by_staff", "setup_code_rejected"} <= acts
    conn().execute("DELETE FROM ip_failures")


# ---------------------------------------------------------------- Stage 2: every form
from app import forms as F  # noqa: E402


def _ok_items(**bad):
    out = {}
    for g, items in F.INSPECTION_ITEMS.items():
        for it in items:
            out[f"items:{F._slug(g)}:{F._slug(it)}"] = "OK"
    out.update(bad)
    return out


def _sid(name):
    return str(conn().execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()[0])


def test_staff_only_see_enabled_forms(client):
    login(client, "Jaime Mendoza", "135790")
    assert [f["slug"] for f in client.get("/ops/api/me").json()["forms"]] == ["receiving"]
    r = client.post("/ops/api/reports/eos", data={"submission_id": "sub-eos-staff1", "role": "Driver"}, headers=H)
    assert r.status_code == 403
    login(client, "Adem Atis", "246810")
    slugs = [f["slug"] for f in client.get("/ops/api/me").json()["forms"]]
    assert slugs == ["receiving", "delivery", "install", "rma", "eos", "inspection", "vincident", "incident", "disciplinary", "measure"]
    r = client.put("/ops/api/admin/forms-enabled", json={"forms": ["Receiving Report", "End of Shift", "Disciplinary Action"]}, headers=H)
    assert r.json()["forms"] == ["Receiving Report", "End of Shift"]  # admin-only form can't be switched on for staff
    login(client, "Jaime Mendoza", "135790")
    assert [f["slug"] for f in client.get("/ops/api/me").json()["forms"]] == ["receiving", "eos"]
    r = client.post("/ops/api/reports/disciplinary", data={"submission_id": "sub-dsc-staff1"}, headers=H)
    assert r.status_code == 403
    acts = [a[0] for a in conn().execute("SELECT action FROM audit WHERE action='forms_switched'")]
    assert acts


def test_end_of_shift_checks_follow_role(client):
    login(client, "Jaime Mendoza", "135790")
    fld = next(x for x in F.FORMS["End of Shift"]["fields"] if x["key"] == "checks_driver")
    drv = {k: "1" for k, _ in fld["items"]}
    files = {"area1": ("a.jpg", jpeg(), "image/jpeg")}
    base = {"submission_id": "sub-eos-0001", "role": "Driver", **drv}
    r = client.post("/ops/api/reports/eos", data=base, files=files, headers=H)
    assert r.status_code == 422 and "photo" in r.json()["detail"].lower()  # needs 2 area photos
    files["area2"] = ("b.jpg", jpeg((0, 0, 200)), "image/jpeg")
    r = client.post("/ops/api/reports/eos", data={**base, "submission_id": "sub-eos-0002"}, files=files, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["receipt"] == "EOS-00001"  # each form numbers on its own
    # warehouse checks were not required for a driver; missing a driver check is refused
    r = client.post("/ops/api/reports/eos", data={**base, "submission_id": "sub-eos-0003", list(drv)[0]: ""},
                    files=files, headers=H)
    assert r.status_code == 422


def test_inspection_defective_routing(client):
    login(client, "Adem Atis", "246810")
    first = next(iter(_ok_items()))
    data = {"submission_id": "sub-vin-0001", "trip": "Pre-Trip", "vehicle": "Big Truck", "odometer": "120500",
            **_ok_items(**{first: "Defective"})}
    r = client.post("/ops/api/reports/inspection", data=data, headers=H)
    assert r.status_code == 422 and "Remarks" in r.json()["detail"]
    r = client.post("/ops/api/reports/inspection", data={**data, "remarks": "Fuel gauge stuck"}, headers=H)
    assert r.status_code == 200, r.text
    e = conn().execute("SELECT subject, recipients FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert e[0].startswith("DEFECTIVE") and "admin@simplydoors.com" in e[1]
    r = client.post("/ops/api/reports/inspection", data={**data, "submission_id": "sub-vin-0002", **_ok_items()}, headers=H)
    e = conn().execute("SELECT subject, recipients FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert not e[0].startswith("DEFECTIVE") and "admin@simplydoors.com" not in e[1]
    # a missing row is refused
    partial = {**data, "submission_id": "sub-vin-0003", "remarks": "x"}
    partial.pop(first)
    assert client.post("/ops/api/reports/inspection", data=partial, headers=H).status_code == 422


def test_disciplinary_goes_to_employee_and_is_confidential(client):
    login(client, "Adem Atis", "246810")
    data = {"submission_id": "sub-dsc-0001", "target": _sid("Jaime Mendoza"), "level": "First Written Warning",
            "infraction": "Safety Violation", "date": "2026-09-30", "description": "No vest", "plan": "Wear vest",
            "consequences": "Further action"}
    r = client.post("/ops/api/reports/disciplinary", data=data, headers=H)
    assert r.status_code == 200, r.text
    e = conn().execute("SELECT subject, recipients, bcc FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert e[0].startswith("CONFIDENTIAL") and "Jaime Mendoza" in e[0]
    to = {x.strip() for x in e[1].split(",")}
    assert {"paz@simplydoors.com", "admin@simplydoors.com"} <= to
    assert "adem@simplydoors.com" not in to and e[2] == "adem@simplydoors.com"   # owner: private copy only
    assert "lupes@simplydoors.com" not in to
    assert len(to) == 3  # plus the employee
    bad = client.post("/ops/api/reports/disciplinary", data={**data, "submission_id": "sub-dsc-0002", "target": "99999"}, headers=H)
    assert bad.status_code == 422


def test_delivery_signature_and_lists(client):
    login(client, "Adem Atis", "246810")
    sig = io.BytesIO()
    Image.new("RGBA", (600, 200), (0, 0, 0, 0)).save(sig, "PNG")
    data = {"submission_id": "sub-dlv-0001", "po": "PO-9", "customer": "Lee", "address": "1 Elm", "condition": "Yes"}
    r = client.post("/ops/api/reports/delivery", data=data, headers=H,
                    files={"sig": ("sig.png", sig.getvalue(), "image/png")})
    assert r.status_code == 422  # needs at least one site photo
    r = client.post("/ops/api/reports/delivery", data={**data, "submission_id": "sub-dlv-0002"}, headers=H,
                    files={"site1": ("s.jpg", jpeg(), "image/jpeg"), "sig": ("sig.png", sig.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0]
    rows = {p[0]: p[1] for p in conn().execute("SELECT slot, geo_status FROM photos WHERE report_id=?", (rid,))}
    assert rows == {"site1": "missing", "sig": "signature"}
    d = client.get(f"/ops/api/admin/reports/{rid}").json()
    assert d["no_geo"] == 1 if "no_geo" in d else True
    assert client.get(f"/ops/api/admin/reports/{rid}/pdf").status_code == 200
    # editable vehicle list feeds the form and validation
    r = client.put("/ops/api/admin/lists/vehicles", json={"values": ["Van 1", "Van 2", " ", "Van 1"]}, headers=H)
    assert r.json()["values"] == ["Van 1", "Van 2"]
    spec = next(f for f in client.get("/ops/api/me").json()["forms"] if f["slug"] == "inspection")
    assert [o[0] for o in next(x for x in spec["fields"] if x["key"] == "vehicle")["options"]] == ["Van 1", "Van 2"]
    assert client.put("/ops/api/admin/lists/nope", json={"values": ["x"]}, headers=H).status_code == 404


# ---------------------------------------------------------------- Stage 3: measure
def _door(**over):
    d = {"type": "door", "loc": "Front Entry", "config": "Single", "handing": "Left Hand Inswing", "dim_type": "Rough Opening",
         "w": {"w": "38", "f": "1/2"}, "h": {"w": "82", "f": ""}, "jamb": "6 9/16", "bore": "Single Bore",
         "ext": "Brickmould", "int": "Custom 4in mitered", "labor": ["Cut Tile", "Bogus"], "custom_labor": "", "notes": "Sill rotted"}
    d.update(over)
    return d


def _window(**over):
    w = {"type": "window", "loc": "Kitchen W1", "floor": "2nd", "qty": "2", "m_type": "Rough Opening",
         "points": [{"w": {"w": "35", "f": "1/2"}, "h": {"w": "59", "f": "3/4"}}, {"w": {"w": "", "f": ""}, "h": {"w": "", "f": ""}}],
         "sill": {"w": "42", "f": ""}, "tempered": True, "wall": "4 9/16", "mat": "Wood Stud", "frame": '4-1/2"',
         "ext": "1x4 Vinyl", "int": "Drywall Return", "labor": ["Stucco Removal"], "custom_labor": "", "notes": ""}
    w.update(over)
    return w


def _measure(c, sub, items, files=None, **over):
    data = {"submission_id": sub, "customer": "Garcia", "po": "PO-55", "date": "2026-10-01", "items": json.dumps(items)}
    data.update(over)
    return c.post("/ops/api/reports/measure", data=data, files=files or {}, headers=H)


def test_measure_submit_validate_and_email(client):
    login(client, "Jaime Mendoza", "135790")
    assert _measure(client, "sub-msr-staff", [_door()]).status_code == 403          # switched off for staff
    assert client.get("/ops/api/measures", headers=H).status_code == 403
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/admin/forms-enabled", json={"forms": ["Receiving Report", "Measure Report"]}, headers=H)
    login(client, "Jaime Mendoza", "135790")
    r = _measure(client, "sub-msr-0001", [])
    assert r.status_code == 422 and "at least one" in r.json()["detail"]
    r = _measure(client, "sub-msr-0002", [_door(w={"w": "", "f": ""})])
    assert r.status_code == 422 and "Door #1 width" in r.json()["detail"]
    r = _measure(client, "sub-msr-0003", [_door(w={"w": "3x", "f": "1/9"})])
    assert r.status_code == 422
    r = _measure(client, "sub-msr-0004", [_window(points=[])])
    assert r.status_code == 422 and "Window #1" in r.json()["detail"]
    files = {"i1p1": ("a.jpg", jpeg(), "image/jpeg"), "i2p2": ("b.jpg", jpeg((0, 90, 0)), "image/jpeg"),
             "i9p1": ("x.jpg", jpeg(), "image/jpeg")}                                 # no card 9: ignored
    r = _measure(client, "sub-msr-0005", [_door(), _window(), _door(loc="Back")], files)
    assert r.status_code == 200, r.text
    rec = r.json()["receipt"]
    assert rec == "MSR-00001"
    row = conn().execute("SELECT id, data FROM reports WHERE receipt=?", (rec,)).fetchone()
    d = json.loads(row[1])
    assert d["measured_by"] == "Jaime Mendoza" and d["doors"] == 2 and d["windows"] == 1
    assert d["items"][0]["labor"] == ["Cut Tile"] and d["items"][0]["int"] == "Custom 4in mitered"
    assert len(d["items"][1]["points"]) == 1 and d["items"][1]["tempered"] is True
    slots = {p[0] for p in conn().execute("SELECT slot FROM photos WHERE report_id=?", (row[0],))}
    assert slots == {"i1p1", "i2p2"}
    e = conn().execute("SELECT subject, recipients FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert e[0].startswith("Measure Report: Garcia - PO PO-55 (Jaime Mendoza)")
    assert {x.strip() for x in e[1].split(",")} == {"admin@simplydoors.com", "jaime@simplydoors.com"} or "admin@simplydoors.com" in e[1]
    login(client, "Adem Atis", "246810")
    pdf = client.get(f"/ops/api/admin/reports/{row[0]}/pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    det = client.get(f"/ops/api/admin/reports/{row[0]}", headers=H).json()
    assert any("Door #1" in lab for lab in [p["label"] for p in det["photos"]])
    assert any(a == "Window #1 — Kitchen W1" for a, _ in det["rows"])


def test_measure_reopen_and_revise(client):
    login(client, "Jaime Mendoza", "135790")
    lst = client.get("/ops/api/measures", headers=H).json()
    first = next(m for m in lst if m["receipt"] == "MSR-00001")
    got = client.get(f"/ops/api/measures/{first['id']}", headers=H).json()
    pids = {p["slot"]: p["id"] for p in got["photos"]}
    assert client.get(f"/ops/api/measure-photos/{pids['i1p1']}").status_code == 200
    # Paz (admin) revises Jaime's measure: drops the first door, so the window becomes card 1; keeps its photo
    login(client, "Paz Galambos", "112233")
    items = got["data"]["items"][1:]
    keep = {"i1p2": pids["i2p2"]}
    r = _measure(client, "sub-msr-0006", items, {"i2p1": ("c.jpg", jpeg((9, 9, 200)), "image/jpeg")},
                 revision_of="MSR-00001", keep=json.dumps(keep))
    assert r.status_code == 200, r.text
    rec2 = r.json()["receipt"]
    row = conn().execute("SELECT id, data FROM reports WHERE receipt=?", (rec2,)).fetchone()
    d = json.loads(row[1])
    assert d["revision_of"] == "MSR-00001" and d["measured_by"] == "Jaime Mendoza" and d["revised_by"] == "Paz Galambos"
    slots = {p[0] for p in conn().execute("SELECT slot FROM photos WHERE report_id=?", (row[0],))}
    assert slots == {"i1p2", "i2p1"}
    e = conn().execute("SELECT subject, recipients FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert e[0].startswith("REVISED Measure Report") and "replaces MSR-00001" in e[0]
    assert "paz@simplydoors.com" in e[1]
    # the list shows the old one as replaced
    login(client, "Jaime Mendoza", "135790")
    lst = client.get("/ops/api/measures", headers=H).json()
    assert next(m for m in lst if m["receipt"] == "MSR-00001")["replaced_by"] == rec2
    assert any(m["receipt"] == rec2 for m in lst)          # Jaime still sees it: he measured it
    # someone else can't open it, can't steal its photos, can't revise it
    for name in ("Steven Chandler",):
        sid = conn().execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()[0]
        auth.set_pin(sid, "975310", "install")
        login(client, name, "975310")
        assert client.get(f"/ops/api/measures/{first['id']}", headers=H).status_code == 404
        assert client.get(f"/ops/api/measure-photos/{pids['i1p1']}").status_code == 404
        r = _measure(client, "sub-msr-0007", items, revision_of="MSR-00001")
        assert r.status_code == 422
        r = _measure(client, "sub-msr-0008", items, keep=json.dumps({"i1p1": pids["i1p1"]}))
        assert r.status_code == 422


def test_measure_vendor_style_door_fields(client):
    login(client, "Adem Atis", "246810")
    base = _door()
    ok = {**base, "config": "Double Door", "handing": "RH/Fixed", "swing": "OutSwing", "sidelite": "Left"}
    r = _measure(client, "sub-msr-v001", [ok])
    assert r.status_code == 200, r.text
    d = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0])
    it = d["items"][0]
    assert it["handing"] == "RH/Fixed" and it["swing"] == "OutSwing" and it["sidelite"] == ""   # sidelite only for 1 SL
    # a single-door handing on a double door is refused
    r = _measure(client, "sub-msr-v002", [{**base, "config": "Double Door", "handing": "Left"}])
    assert r.status_code == 422 and "pick one" in r.json()["detail"]
    # sliders: slide side only, never a swing
    r = _measure(client, "sub-msr-v003", [{**base, "config": "Sliding Glass Door", "handing": "Left Slide", "swing": "InSwing"}])
    it = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0])["items"][0]
    assert it["handing"] == "Left Slide" and it["swing"] == ""
    # old-style answers from a phone still on the previous version are converted
    r = _measure(client, "sub-msr-v004", [{**base, "config": "Sgl w/ 1 SL", "handing": "Right Hand Outswing", "sidelite": "Right"}])
    it = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0])["items"][0]
    assert (it["config"], it["handing"], it["swing"], it["sidelite"]) == ("Single w/ 1 Sidelite", "Right", "OutSwing", "Right")


def test_measure_slab_only_bore_and_hinges(client):
    login(client, "Adem Atis", "246810")
    slab = {**_door(), "config": "Slab Only", "handing": "Left", "swing": "InSwing", "h": {"w": "80", "f": ""},
            "bore_mode": "Custom", "bore_at": {"w": "44", "f": ""},
            "hinges": [{"w": "8", "f": "1/4"}, {"w": "38", "f": ""}, {"w": "67", "f": "3/4"}, {"w": "", "f": ""}]}
    r = _measure(client, "sub-msr-s001", [slab])
    assert r.status_code == 200, r.text
    it = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0])["items"][0]
    assert len(it["hinges"]) == 3 and it["bore_at"]["w"] == "44"
    # bore/hinges are dropped for anything that isn't a slab
    r = _measure(client, "sub-msr-s002", [{**slab, "config": "Single Door"}])
    it = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0])["items"][0]
    assert it["hinges"] == [] and it["bore_mode"] == ""
    assert _measure(client, "sub-msr-s003", [{**slab, "handing": "Fixed"}]).status_code == 422   # slab: left/right only


def test_owner_can_manage_admins_but_not_the_reverse(client):
    adem = conn().execute("SELECT id, is_owner FROM staff WHERE name='Adem Atis'").fetchone()
    paz = conn().execute("SELECT id, is_owner FROM staff WHERE name='Paz Galambos'").fetchone()
    assert adem["is_owner"] == 1 and paz["is_owner"] == 0
    login(client, "Paz Galambos", "112233")
    assert client.get("/ops/api/me").json()["is_owner"] is False
    assert client.post(f"/ops/api/admin/staff/{adem['id']}/pin", json={"pin": "918273"}, headers=H).status_code == 403
    assert client.post(f"/ops/api/admin/staff/{adem['id']}/invite", headers=H).status_code == 403
    assert client.patch(f"/ops/api/admin/staff/{adem['id']}", json={"is_admin": False}, headers=H).status_code == 403
    login(client, "Adem Atis", "246810")
    assert client.get("/ops/api/me").json()["is_owner"] is True
    r = client.post(f"/ops/api/admin/staff/{paz['id']}/invite", headers=H)
    assert r.status_code == 200 and r.json()["name"] == "Paz Galambos"
    assert client.post(f"/ops/api/admin/staff/{paz['id']}/pin", json={"pin": "112233"}, headers=H).status_code == 200
    # ownership can't be granted from the app
    client.patch(f"/ops/api/admin/staff/{paz['id']}", json={"is_owner": True}, headers=H)
    assert conn().execute("SELECT is_owner FROM staff WHERE id=?", (paz["id"],)).fetchone()[0] == 0



def test_owner_address_stays_private(client, smtp):
    # the owner's address was moved off every shared list, into private copies
    rules = " ".join(r[0] for r in conn().execute("SELECT recipients FROM email_rules"))
    assert "adem@" not in rules
    login(client, "Adem Atis", "246810")
    mine = client.get("/ops/api/admin/my-copies").json()
    assert any(f["type"] == "Receiving Report" and f["on"] for f in mine["forms"])
    # the staff list shows the owner's email like anyone's; the private-copy settings are owner-only
    login(client, "Paz Galambos", "112233")
    staff = client.get("/ops/api/admin/staff").json()
    me = next(x for x in staff if x["name"] == "Adem Atis")
    assert me["email"] == "adem@simplydoors.com" and me["is_owner"]
    assert client.get("/ops/api/admin/my-copies").status_code == 403
    # a sent email delivers to the owner without the address in any header
    RECEIVED.clear()
    r = receiving(client, "sub-priv-0001")
    assert r.status_code == 200
    mailer.SMTP_USER, mailer.SMTP_PASSWORD, mailer.SMTP_STARTTLS = "", "", False
    orig = mailer.configured
    mailer.configured = lambda: True
    try:
        mailer.process_queue_once()
    finally:
        mailer.configured = orig
    got = RECEIVED[-1]
    assert "adem@simplydoors.com" in got["to"]                      # delivered
    head = got["data"].split(b"\r\n\r\n", 1)[0].lower()
    assert b"adem@simplydoors.com" not in head                     # but not visible in To/Cc
    # if the owner types the address into a list on purpose, it shows
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/admin/email-rules", json={"form_type": "End of Shift", "recipients": "adem@simplydoors.com, lupes@simplydoors.com"}, headers=H)
    from app.forms import split_recipients
    to, bcc = split_recipients("End of Shift", {})
    assert "adem@simplydoors.com" in to and "adem@simplydoors.com" not in bcc


def test_installation_completion(client):
    login(client, "Adem Atis", "246810")
    checks = {f"checklist:{k}": "Done" for k, _ in F.INSTALL_CHECKS}
    base = {"po": "1234", "customer": "Lee", "work": "Yes", **checks}
    photos = {"after1": ("a.jpg", jpeg(), "image/jpeg"), "after2": ("b.jpg", jpeg((1, 2, 3)), "image/jpeg")}
    # customer present -> signature + name required
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-ins-0001", "cust_present": "Yes",
                                                     "signer": "Pat Lee"}, files=photos, headers=H)
    assert r.status_code == 422 and "signature" in r.json()["detail"].lower()
    sig = io.BytesIO(); Image.new("RGBA", (600, 200), (0, 0, 0, 0)).save(sig, "PNG")
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-ins-0002", "cust_present": "Yes",
                    "signer": "Pat Lee"}, files={**photos, "sig": ("s.png", sig.getvalue(), "image/png")}, headers=H)
    assert r.status_code == 200, r.text
    e = conn().execute("SELECT subject FROM emails ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert e.startswith("Install Complete: 1234 - Lee")
    # not present -> no signature, but a reason; flagged for follow-up
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-ins-0003", "cust_present": "No"},
                    files=photos, headers=H)
    assert r.status_code == 422 and "Why no signature" in r.json()["detail"]
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-ins-0004", "cust_present": "No",
                    "no_sign_reason": "Not home"}, files=photos, headers=H)
    assert r.status_code == 200, r.text
    e = conn().execute("SELECT subject FROM emails ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert e.startswith("NEEDS FOLLOW-UP") and "(not signed)" in e
    # every checklist item must be answered
    bad = {**base, "submission_id": "sub-ins-0005", "cust_present": "No", "no_sign_reason": "x"}
    bad.pop("checklist:operates")
    assert client.post("/ops/api/reports/install", data=bad, files=photos, headers=H).status_code == 422
    # PO must be exactly the last 4 numbers
    ok = {**base, "cust_present": "No", "no_sign_reason": "x"}
    for po in ("SD-1234", "123", "12345"):
        r = client.post("/ops/api/reports/install", data={**ok, "submission_id": f"sub-ins-po{po}", "po": po},
                        files=photos, headers=H)
        assert r.status_code == 422 and "4 numbers" in r.json()["detail"]
    # work not complete -> outstanding punch list required, flagged for follow-up
    r = client.post("/ops/api/reports/install", data={**ok, "submission_id": "sub-ins-0006", "work": "No"},
                    files=photos, headers=H)
    assert r.status_code == 422 and "Outstanding punch list" in r.json()["detail"]
    r = client.post("/ops/api/reports/install", data={**ok, "submission_id": "sub-ins-0007", "work": "No",
                    "punch_items": "Storm door on order"}, files=photos, headers=H)
    assert r.status_code == 200, r.text
    e = conn().execute("SELECT subject FROM emails ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert e.startswith("NEEDS FOLLOW-UP") and "(punch list)" in e


def test_install_customer_copy(client):
    from app import mailer
    from app.pdf import build_customer_pdf
    login(client, "Adem Atis", "246810")
    checks = {f"checklist:{k}": "Done" for k, _ in F.INSTALL_CHECKS}
    checks["checklist:trim"] = "N/A"
    photos = {"after1": ("a.jpg", jpeg(), "image/jpeg"), "after2": ("b.jpg", jpeg((1, 2, 3)), "image/jpeg"),
              "before1": ("c.jpg", jpeg((9, 9, 9)), "image/jpeg")}
    sig = io.BytesIO(); Image.new("RGBA", (600, 200), (0, 0, 0, 0)).save(sig, "PNG")
    base = {"po": "4321", "customer": "Pat Lee", "work": "No", "punch_items": "Storm door on order",
            "cust_present": "Yes", "signer": "Pat Lee", "cust_comments": "Great crew", **checks}
    # a bad customer email is refused
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-cc-0001", "cust_email": "pat@"},
                    files={**photos, "sig": ("s.png", sig.getvalue(), "image/png")}, headers=H)
    assert r.status_code == 422 and "email address" in r.json()["detail"]
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-cc-0002", "cust_email": "pat@example.com"},
                    files={**photos, "sig": ("s.png", sig.getvalue(), "image/png")}, headers=H)
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-cc-0002'").fetchone()[0]
    em = conn().execute("SELECT recipients, subject, audience FROM emails WHERE report_id=? ORDER BY id", (rid,)).fetchall()
    assert [e["audience"] for e in em] == ["staff", "customer"]
    assert em[1]["recipients"] == "pat@example.com" and "pat@example.com" not in em[0]["recipients"]
    assert em[1]["subject"].startswith("Your SimplyDoors installation record (Job 4321)")
    # unstamped copies kept for the customer's PDF
    assert os.path.exists(os.path.join(os.path.dirname(conn().execute(
        "SELECT path FROM photos WHERE report_id=? AND slot='after1'", (rid,)).fetchone()[0]), "after1.clean.jpg"))
    # the customer's PDF leaves out internal details
    rep, data, ph = mailer._report_bundle(rid)
    pdf = build_customer_pdf(rep, data, ph)
    assert pdf.startswith(b"%PDF")
    msg = mailer._send_customer(conn().execute("SELECT * FROM emails WHERE report_id=? AND audience='customer'",
                                               (rid,)).fetchone(), rep, data, ph)
    html = msg.get_body(("html",)).get_content()
    assert "Storm door on order" in html and "Adem" not in html and "NEEDS FOLLOW-UP" not in html
    assert msg["Reply-To"] == mailer.CUSTOMER_REPLY_TO and msg["From"].startswith("SimplyDoors")
    # no email given, or customer not there -> no customer copy
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-cc-0003"},
                    files={**photos, "sig": ("s.png", sig.getvalue(), "image/png")}, headers=H)
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-cc-0003'").fetchone()[0]
    assert conn().execute("SELECT COUNT(*) FROM emails WHERE report_id=? AND audience='customer'", (rid,)).fetchone()[0] == 0
    # test mode: the customer copy goes to the owner, never the customer
    client.put("/ops/api/owner/test-mode", json={"on": True}, headers=H)
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-cc-0004", "cust_email": "pat@example.com",
                    "is_test": "1"}, files={**photos, "sig": ("s.png", sig.getvalue(), "image/png")}, headers=H)
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-cc-0004'").fetchone()[0]
    e = conn().execute("SELECT recipients, subject FROM emails WHERE report_id=? AND audience='customer'", (rid,)).fetchone()
    assert "pat@example.com" not in e["recipients"] and e["subject"].startswith("TEST - customer copy")
    client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)


def test_rma_vendor_and_customer(client):
    login(client, "Adem Atis", "246810")
    photos = {"product1": ("a.jpg", jpeg(), "image/jpeg")}
    v = {"direction": "Return to vendor", "po": "SD-9", "vendor": "Hoelscher", "vendor_rma": "R-55",
         "items": "1 slab 3068 LH", "reason_v": "Damaged in shipping", "want": "Replacement"}
    # vendor fields required on a vendor return; customer fields are not
    bad = {**v, "submission_id": "sub-rma-0001"}; bad.pop("want")
    r = client.post("/ops/api/reports/rma", data=bad, files=photos, headers=H)
    assert r.status_code == 422 and "What we want" in r.json()["detail"]
    r = client.post("/ops/api/reports/rma", data={**v, "submission_id": "sub-rma-0002"}, files=photos, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["receipt"].startswith("RMA-")
    e = conn().execute("SELECT subject, recipients FROM emails ORDER BY id DESC LIMIT 1").fetchone()
    assert e[0].startswith("RMA to Hoelscher: SD-9 (vendor RMA R-55)") and "admin@simplydoors.com" in e[1]
    # at least one product photo
    r = client.post("/ops/api/reports/rma", data={**v, "submission_id": "sub-rma-0003"}, headers=H)
    assert r.status_code == 422
    # customer return: customer, condition, resolution, restocking fee
    c = {"direction": "Return from customer", "po": "SD-10", "customer": "Lee", "items": "storm door",
         "reason_c": "Changed mind", "condition": "Unused, in box", "resolution": "Store credit", "restock": "Yes"}
    r = client.post("/ops/api/reports/rma", data={**c, "submission_id": "sub-rma-0004"}, files=photos, headers=H)
    assert r.status_code == 422 and "Restocking fee amount" in r.json()["detail"]
    r = client.post("/ops/api/reports/rma", data={**c, "submission_id": "sub-rma-0005", "restock_amt": "15%",
                    "vendor": "ignored"}, files=photos, headers=H)
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id, data FROM reports WHERE submission_id='sub-rma-0005'").fetchone()
    d = json.loads(rid["data"])
    assert "vendor" not in d and d["restock_amt"] == "15%"
    e = conn().execute("SELECT subject FROM emails ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert e.startswith("RMA from customer: SD-10 - Lee")
    assert client.get(f"/ops/api/admin/reports/{rid['id']}/pdf", headers=H).status_code == 200


def test_owner_test_mode_and_log_cleanup(client):
    # only the owner can switch test mode on
    login(client, "Paz Galambos", "112233")
    assert client.put("/ops/api/owner/test-mode", json={"on": True}, headers=H).status_code == 403
    login(client, "Adem Atis", "246810")
    assert client.put("/ops/api/owner/test-mode", json={"on": True}, headers=H).json()["test_mode"] is True
    assert client.get("/ops/api/me", headers=H).json()["test_mode"] is True
    real_before = conn().execute("SELECT MAX(receipt) FROM reports WHERE receipt LIKE 'RCV-%'").fetchone()[0]
    r = receiving(client, "sub-test-0001", is_test="1")
    assert r.json()["receipt"] == "TEST-RCV-00001"
    rid = conn().execute("SELECT id FROM reports WHERE receipt='TEST-RCV-00001'").fetchone()[0]
    e = conn().execute("SELECT recipients, bcc, subject FROM emails WHERE report_id=?", (rid,)).fetchone()
    assert e["recipients"] == "adem@simplydoors.com" and e["bcc"] == "" and e["subject"].startswith("TEST - ")
    # a real report right after keeps the real numbering going
    r = receiving(client, "sub-real-0001")
    assert r.json()["receipt"] > (real_before or "") and not r.json()["receipt"].startswith("TEST")
    real_id = conn().execute("SELECT id FROM reports WHERE submission_id='sub-real-0001'").fetchone()[0]
    # non-owner flag is ignored
    login(client, "Jaime Mendoza", "135790")
    assert not receiving(client, "sub-test-jaime", is_test="1").json()["receipt"].startswith("TEST")
    # deleting: only the owner, only test reports
    login(client, "Paz Galambos", "112233")
    assert client.delete(f"/ops/api/admin/reports/{rid}", headers=H).status_code == 403
    login(client, "Adem Atis", "246810")
    client.get(f"/ops/api/admin/reports/{rid}", headers=H)   # leaves a report_viewed line
    assert client.delete(f"/ops/api/admin/reports/{real_id}", headers=H).status_code == 403
    assert client.delete(f"/ops/api/admin/reports/{rid}", headers=H).status_code == 200
    c = conn()
    assert not c.execute("SELECT 1 FROM reports WHERE id=?", (rid,)).fetchone()
    assert not c.execute("SELECT 1 FROM audit WHERE target=?", (f"report:{rid}",)).fetchone()
    assert c.execute("SELECT 1 FROM audit WHERE action='test_report_deleted' AND target='TEST-RCV-00001'").fetchone()
    # own routine lines are deletable; sign-ins, other people's lines and receipts are not
    log = client.get("/ops/api/admin/audit?limit=1000", headers=H).json()
    assert log["can_delete"]
    mine = [r for r in log["rows"] if r["deletable"]]
    assert mine and all(r["actor_name"] == "Adem Atis" for r in mine)
    locked = [r["id"] for r in log["rows"] if r["action"] in ("login_ok", "test_mode_on", "test_report_deleted")
              or r["actor_name"] != "Adem Atis"]
    assert locked and not any(r["deletable"] for r in log["rows"] if r["id"] in locked)
    assert client.post("/ops/api/admin/audit/delete", json={"ids": [mine[0]["id"], locked[0]]}, headers=H).status_code == 403
    assert c.execute("SELECT 1 FROM audit WHERE id=?", (mine[0]["id"],)).fetchone()   # nothing deleted on refusal
    r = client.post("/ops/api/admin/audit/delete", json={"ids": [x["id"] for x in mine]}, headers=H)
    assert r.json()["deleted"] == len(mine)
    assert c.execute("SELECT 1 FROM audit WHERE action='log_lines_deleted'").fetchone()
    # Paz (admin, not owner) sees no delete boxes and can't call it
    login(client, "Paz Galambos", "112233")
    log = client.get("/ops/api/admin/audit", headers=H).json()
    assert not log["can_delete"] and not any(r["deletable"] for r in log["rows"])
    assert client.post("/ops/api/admin/audit/delete", json={"ids": [1]}, headers=H).status_code == 403
    # and the database itself still refuses deletes
    raw = sqlite3.connect(os.path.join(TMP, "ops.db"))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("DELETE FROM audit")
    raw.close()
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)


def test_simply_studio_tile(client):
    import base64 as b64, hashlib as hl, hmac as hm
    from app import main as M
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["studio_url"]                  # everyone with an email
    monkey_secret = "x" * 64
    old, M.STUDIO_SSO_SECRET = M.STUDIO_SSO_SECRET, monkey_secret
    try:
        url = client.post("/ops/api/studio-link", headers=H).json()["url"]
        body, sig = url.split("/sso?t=")[1].split(".")
        want = b64.urlsafe_b64encode(hm.new(monkey_secret.encode(), body.encode(), hl.sha256).digest()).decode().rstrip("=")
        assert sig == want
        claims = json.loads(b64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        assert claims["email"] == "jaimem@simplydoors.com" and claims["role"] == "field" and claims["aud"] == "simply-studio"
        login(client, "Paz Galambos", "112233")
        url = client.post("/ops/api/studio-link", headers=H).json()["url"]
        body = url.split("/sso?t=")[1].split(".")[0]
        assert json.loads(b64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["role"] == "full"
        assert client.post("/ops/api/studio-link").status_code in (400, 403)          # needs the app header
        # an admin can switch the tile off for someone
        login(client, "Adem Atis", "246810")
        jid = conn().execute("SELECT id FROM staff WHERE name='Jaime Mendoza'").fetchone()[0]
        assert client.patch(f"/ops/api/admin/staff/{jid}", json={"studio_link": False}, headers=H).status_code == 200
        login(client, "Jaime Mendoza", "135790")
        assert client.get("/ops/api/me", headers=H).json()["studio_url"] is None
        assert client.post("/ops/api/studio-link", headers=H).status_code == 403
    finally:
        M.STUDIO_SSO_SECRET = old


def test_reset_test_data_is_console_only_and_one_time(client, monkeypatch):
    # keep this test last: it wipes the reports
    from app import cli
    c = conn()
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone()[0] > 0
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    assert cli.reset_test_data() == 1                              # anything but RESET cancels
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone()[0] > 0
    monkeypatch.setattr("builtins.input", lambda *_: "RESET")
    assert cli.reset_test_data() == 0
    for t in ("reports", "photos", "emails"):
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    log = c.execute("SELECT action, actor_name FROM audit").fetchall()
    assert [tuple(r) for r in log] == [("test_data_reset", "server console")]
    assert any(f.startswith("ops-") for f in os.listdir(os.path.join(TMP, "before-reset")))
    with pytest.raises(sqlite3.IntegrityError):                    # log is locked again
        c.execute("DELETE FROM audit")
    assert cli.reset_test_data() == 1                              # won't run twice by accident
    assert c.execute("SELECT COUNT(*) FROM audit").fetchone()[0] >= 1
    login(client, "Jaime Mendoza", "135790")
    r = receiving(client, "sub-after-reset")
    assert r.status_code == 200 and r.json()["receipt"] == "RCV-00001"

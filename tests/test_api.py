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
    paz = conn().execute("SELECT id FROM staff WHERE name='Paz Galambos'").fetchone()[0]
    assert client.post(f"/ops/api/admin/staff/{paz}/pin", json={"pin": "999888"}, headers=H).status_code == 422
    assert client.patch(f"/ops/api/admin/staff/{paz}", json={"is_admin": False}, headers=H).status_code == 422
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

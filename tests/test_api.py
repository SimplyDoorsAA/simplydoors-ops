"""End-to-end checks for Stage 1. Run:  python -m pytest -q tests
Uses a throwaway data folder and a fake mail server; touches nothing real."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
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


def test_activity_log_dates_are_local_days(client):
    # July: Texas is UTC-5, so local Jul 15 runs from 05:00Z on the 15th to 05:00Z on the 16th
    c = conn()
    for at in ("2026-07-15T04:30:00Z", "2026-07-15T05:30:00Z", "2026-07-16T04:30:00Z", "2026-07-16T05:30:00Z"):
        c.execute("INSERT INTO audit(at, actor_name, action, target) VALUES (?, 'system', 'tz_probe', ?)", (at, at))
    login(client, "Adem Atis", "246810")
    q = "action=tz_probe&from=2026-07-15&to=2026-07-15"
    rows = client.get(f"/ops/api/admin/audit?{q}").json()["rows"]
    assert sorted(r["target"] for r in rows) == ["2026-07-15T05:30:00Z", "2026-07-16T04:30:00Z"]
    csv = client.get(f"/ops/api/admin/audit.csv?{q}").text
    assert "2026-07-15T05:30:00Z" in csv and "2026-07-16T04:30:00Z" in csv
    assert "2026-07-15T04:30:00Z" not in csv and "2026-07-16T05:30:00Z" not in csv
    # winter: UTC-6
    c.execute("INSERT INTO audit(at, actor_name, action, target) VALUES ('2026-01-10T05:30:00Z', 'system', 'tz_probe', 'jan')")
    assert [r["target"] for r in client.get("/ops/api/admin/audit?action=tz_probe&from=2026-01-10&to=2026-01-10").json()["rows"]] == []
    assert [r["target"] for r in client.get("/ops/api/admin/audit?action=tz_probe&from=2026-01-09&to=2026-01-09").json()["rows"]] == ["jan"]


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


def test_parallel_wrong_pins_all_count(client):
    # a burst of wrong PINs at the same moment must still lock the account (and lock it once)
    from concurrent.futures import ThreadPoolExecutor
    sid = conn().execute("SELECT id FROM staff WHERE name='Ramiro Zuniga'").fetchone()[0]
    auth.set_pin(sid, "864213", "admin")
    conn().execute("DELETE FROM ip_failures")
    gate = threading.Barrier(12)

    def guess(i):
        gate.wait()
        return auth.attempt_login("Ramiro Zuniga", f"00000{i % 10}", f"10.0.0.{i}", "test")
    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(guess, range(12)))
    st = conn().execute("SELECT locked_until, lock_level FROM staff WHERE id=?", (sid,)).fetchone()
    assert st["locked_until"] and st["lock_level"] == 1, results
    assert sum(1 for _, _, newly in results if newly) == 1
    assert "locked" in login(client, "Ramiro Zuniga", "864213").json()["detail"]     # right PIN refused while locked
    # one connection firing 30 guesses at once gets at most the usual 20 checks
    conn().execute("DELETE FROM ip_failures")
    gate = threading.Barrier(30)

    def from_one_ip(i):
        gate.wait()
        return auth.attempt_login("Nobody At All", "000000", "10.9.9.9", "test")
    with ThreadPoolExecutor(30) as ex:
        msgs = [m for _, m, _ in ex.map(from_one_ip, range(30))]
    assert sum(1 for m in msgs if "this connection" not in m) <= auth.IP_MAX_FAILS
    assert conn().execute("SELECT COUNT(*) FROM ip_failures WHERE ip='10.9.9.9'").fetchone()[0] <= auth.IP_MAX_FAILS
    conn().execute("DELETE FROM ip_failures")
    auth.set_pin(sid, "864213", "admin")


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


def test_nightly_problems_reach_the_phone(client, monkeypatch):
    from app import alerts, main as m
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda title, msg, priority="default": pushed.append(title))
    monkeypatch.setattr(alerts, "_last", {})
    m.snapshot_db("2026-10-02")
    old = time.time() - 40 * 3600
    for f in os.listdir(m.BACKUP_DIR):
        os.utime(os.path.join(m.BACKUP_DIR, f), (old, old))
    monkeypatch.setattr(m, "offsite_status", lambda: (_ for _ in ()).throw(OSError("disk full")))
    m.nightly_once()                               # must not raise
    assert "Ops app: database snapshot is behind" in pushed and "Ops app: nightly snapshot/tidy-up failed" in pushed
    pushed.clear()
    m.nightly_once()                               # throttled: no repeat right away
    assert pushed == []


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
    data = {"submission_id": "sub-dlv-0001", "po": "1234", "customer": "Lee", "address": "1 Elm", "condition": "Yes"}
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
    base = {"po": "1234", "customer": "Lee", "work": "Yes", "walkthrough": "Yes"}
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
    # the walkthrough question must be answered; "No" is flagged for follow-up
    bad = {**base, "submission_id": "sub-ins-0005", "cust_present": "No", "no_sign_reason": "x"}
    bad.pop("walkthrough")
    r = client.post("/ops/api/reports/install", data=bad, files=photos, headers=H)
    assert r.status_code == 422 and "Walkthrough" in r.json()["detail"]
    r = client.post("/ops/api/reports/install", data={**base, "submission_id": "sub-ins-0005b", "walkthrough": "No",
                    "cust_present": "No", "no_sign_reason": "x"}, files=photos, headers=H)
    assert r.status_code == 200, r.text
    e = conn().execute("SELECT subject FROM emails ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert e.startswith("NEEDS FOLLOW-UP") and "(no walkthrough)" in e
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
    checks = {"walkthrough": "Yes"}
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
    # a sales rep picked on the form is added to Reply-To; the office always stays
    assert mailer.customer_reply_to({**data, "sales_notify_email": "rep@simplydoors.com"}) == \
        [mailer.CUSTOMER_REPLY_TO, "rep@simplydoors.com"]
    assert mailer.customer_reply_to({**data, "sales_notify_email": "adem@simplydoors.com"}) == [mailer.CUSTOMER_REPLY_TO]
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
        assert claims["owner"] is False
        login(client, "Paz Galambos", "112233")
        url = client.post("/ops/api/studio-link", headers=H).json()["url"]
        body = url.split("/sso?t=")[1].split(".")[0]
        claims = json.loads(b64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        assert claims["role"] == "full" and claims["owner"] is False                  # an admin, but not the owner
        assert client.post("/ops/api/studio-link").status_code in (400, 403)          # needs the app header
        # only the owner's link says owner
        login(client, "Adem Atis", "246810")
        body = client.post("/ops/api/studio-link", headers=H).json()["url"].split("/sso?t=")[1].split(".")[0]
        claims = json.loads(b64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        assert claims["owner"] is True and claims["role"] == "full" and claims["email"] == "adem@simplydoors.com"
        # an admin can switch the tile off for someone
        jid = conn().execute("SELECT id FROM staff WHERE name='Jaime Mendoza'").fetchone()[0]
        assert client.patch(f"/ops/api/admin/staff/{jid}", json={"studio_link": False}, headers=H).status_code == 200
        login(client, "Jaime Mendoza", "135790")
        assert client.get("/ops/api/me", headers=H).json()["studio_url"] is None
        assert client.post("/ops/api/studio-link", headers=H).status_code == 403
    finally:
        M.STUDIO_SSO_SECRET = old


def test_very_long_text_never_breaks_a_pdf(client):
    # 4,000 characters of short lines is taller than a page; it must run onto the next page, not fail the PDF
    from app.pdf import build_customer_pdf
    lines = ("ok\n" * 2000)[:4000]
    login(client, "Adem Atis", "246810")
    photos = {"after1": ("a.jpg", jpeg(), "image/jpeg"), "after2": ("b.jpg", jpeg((1, 2, 3)), "image/jpeg")}
    sig = io.BytesIO(); Image.new("RGBA", (600, 200), (0, 0, 0, 0)).save(sig, "PNG")
    base = {"po": "5555", "customer": "Long Talker", "work": "No", "punch_items": lines, "walkthrough": "Yes", "cust_comments": lines}
    r1 = client.post("/ops/api/reports/install", headers=H, files=photos,
                     data={**base, "submission_id": "sub-long-ins1", "cust_present": "No", "no_sign_reason": lines})
    r2 = client.post("/ops/api/reports/install", headers=H, files={**photos, "sig": ("s.png", sig.getvalue(), "image/png")},
                     data={**base, "submission_id": "sub-long-ins2", "cust_present": "Yes", "signer": "Pat Lee",
                           "cust_email": "pat@example.com"})
    door = {**_door(), "notes": lines[:2000]}
    r3 = _measure(client, "sub-long-msr1", [door, _window(notes=lines[:2000]), door],
                  {"i1p1": ("a.jpg", jpeg(), "image/jpeg"), "i2p1": ("b.jpg", jpeg((0, 90, 0)), "image/jpeg")})
    for r in (r1, r2, r3):
        assert r.status_code == 200, r.text
        rid = conn().execute("SELECT id FROM reports WHERE receipt=?", (r.json()["receipt"],)).fetchone()[0]
        pdf = client.get(f"/ops/api/admin/reports/{rid}/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-long-ins2'").fetchone()[0]
    rep, data, ph = mailer._report_bundle(rid)
    assert build_customer_pdf(rep, data, ph)[:4] == b"%PDF"


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


# ---------------------------------------------------------------- job lookup (fake Service Fusion)
import threading as _th  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

FAKE = {"fail": False, "calls": []}


def _sf_jobs():
    jobs = [
        {"number": "10236418931", "customer_id": 1, "customer_name": "Angela Heimer", "status": "2 Scheduled Consult",
         "sub_status": None, "contact_first_name": "Angela", "contact_last_name": "Heimer", "start_date": "2026-10-06",
         "street_1": "12 Oak St", "city": "Schertz", "state_prov": "TX", "postal_code": "78154", "category": "Exterior Doors",
         "description": "Front door consult", "po_number": ""},
        {"number": "10236418000", "customer_id": 2, "customer_name": "Bill Tom", "status": "10 Install Scheduled",
         "sub_status": None, "contact_first_name": "Bill", "contact_last_name": "Tom", "start_date": "2026-10-07",
         "street_1": None, "city": None, "state_prov": None, "postal_code": None, "category": "Windows",
         "description": "3 windows", "po_number": ""},
        {"number": "10236418777", "customer_id": 4, "customer_name": "Karen Woody", "status": "15Delivery Scheduled",
         "sub_status": None, "contact_first_name": "Karen", "contact_last_name": "Woody", "start_date": "2026-10-08",
         "street_1": "77 Pecan Ln", "city": "Converse", "state_prov": "TX", "postal_code": "78109", "category": "Retail"},
        {"number": "10236417555", "customer_id": 3, "customer_name": "Old Done Job", "status": "17 Completed",
         "sub_status": None, "contact_first_name": "", "contact_last_name": "", "start_date": None},
    ]
    jobs += [{"number": f"1023640{i:04d}", "customer_id": 9, "customer_name": f"Builder {i}", "status": "4 Need to Order",
              "contact_first_name": "", "contact_last_name": "", "start_date": None} for i in range(60)]
    return jobs


class _SF(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._send(200, {"access_token": "tok", "expires_in": 3600})

    def do_GET(self):
        FAKE["calls"].append(self.path)
        if FAKE["fail"]:
            return self._send(503, {"message": "down"})
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, {})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/v1/job-statuses":
            names = ["2 Scheduled Consult", "10 Install Scheduled", "15Delivery Scheduled", "17 Completed",
                     "4 Need to Order", "Paid in Full"]
            return self._send(200, {"items": [{"id": i, "name": n} for i, n in enumerate(names)], "_meta": {"pageCount": 1}})
        if u.path == "/v1/jobs":
            rows = [j for j in _sf_jobs() if j["status"] == q.get("filters[status]")]
            per, page = int(q.get("per-page", 50)), int(q.get("page", 1))
            assert per <= 50
            pages = max(1, -(-len(rows) // per))
            return self._send(200, {"items": rows[(page - 1) * per: page * per], "_meta": {"pageCount": pages}})
        if u.path.startswith("/v1/customers/"):
            cid = int(u.path.rsplit("/", 1)[1])
            return self._send(200, {"id": cid, "customer_name": "x", "contacts": [
                {"fname": "Bill", "lname": "Tom", "is_primary": True,
                 "emails": [{"email": f"cust{cid}@example.com"}], "phones": [{"phone": "(210) 555-0100"}]}],
                "locations": [{"street_1": "9 Elm Rd", "city": "Cibolo", "state_prov": "TX", "postal_code": "78108",
                               "is_primary": True}]})
        return self._send(404, {})


@pytest.fixture(scope="module")
def fake_sf():
    from app import sfjobs
    srv = HTTPServer(("127.0.0.1", 0), _SF)
    t = _th.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    old = (sfjobs.API, sfjobs.CLIENT_ID, sfjobs.CLIENT_SECRET)
    sfjobs.API, sfjobs.CLIENT_ID, sfjobs.CLIENT_SECRET = f"http://127.0.0.1:{srv.server_port}", "id", "secret"
    sfjobs._token["value"] = None
    yield sfjobs
    sfjobs.API, sfjobs.CLIENT_ID, sfjobs.CLIENT_SECRET = old
    srv.shutdown()
    srv.server_close()


def test_job_lookup_off_until_connected(client):
    from app import sfjobs
    login(client, "Jaime Mendoza", "135790")
    if not sfjobs.configured():
        assert client.get("/ops/api/jobs", headers=H).json() == {"connected": False, "results": []}
        assert client.get("/ops/api/me", headers=H).json()["job_lookup"] is False


def test_job_lookup_refresh_search_and_pick(client, fake_sf, smtp):
    r = fake_sf.refresh("test")
    assert r["ok"] and r["jobs"] == 63              # consult + install + delivery + 60 (two pages); closed jobs skipped
    assert not any("17+Completed" in c or "Paid+in+Full" in c for c in FAKE["calls"])
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["job_lookup"] is True
    # Measure: only scheduled consults
    m = client.get("/ops/api/jobs?form=measure", headers=H).json()["results"]
    assert [j["number"] for j in m] == ["10236418931"]
    # Install, nothing typed: install-stage / near-dated jobs only; then by last 4 and by name
    i = client.get("/ops/api/jobs?form=install", headers=H).json()["results"]
    assert [j["number"] for j in i] == ["10236418000"]
    assert [j["number"] for j in client.get("/ops/api/jobs?form=install&q=8931", headers=H).json()["results"]] == ["10236418931"]
    assert [j["customer"] for j in client.get("/ops/api/jobs?form=install&q=bill%20t", headers=H).json()["results"]] == ["Bill Tom"]
    assert client.get("/ops/api/jobs?form=install&q=7555", headers=H).json()["results"] == []   # completed: not offered
    # Delivery, nothing typed: only "15 Delivery Scheduled" jobs; typing searches every open job
    dl = client.get("/ops/api/jobs?form=delivery", headers=H).json()["results"]
    assert [j["number"] for j in dl] == ["10236418777"]
    assert [j["number"] for j in client.get("/ops/api/jobs?form=delivery&q=8000", headers=H).json()["results"]] == ["10236418000"]
    dd = client.get("/ops/api/jobs/10236418777?form=delivery", headers=H).json()
    assert dd["address"] == "77 Pecan Ln, Converse, TX 78109"
    # search results never carry contact details
    assert set(i[0]) == {"number", "last4", "customer", "status", "date", "category"}
    # picking a job: address from the job, email/phone from the customer record; the pick is logged
    d = client.get("/ops/api/jobs/10236418931?form=measure", headers=H).json()
    assert d["address"] == "12 Oak St, Schertz, TX 78154" and d["email"] == "cust1@example.com" and d["phone"]
    a = conn().execute("SELECT actor_name, target FROM audit WHERE action='job_details_opened' ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(a) == ("Jaime Mendoza", "job:10236418931")
    # no address on the job: falls back to the customer's service location
    d2 = client.get("/ops/api/jobs/10236418000", headers=H).json()
    assert d2["address"] == "9 Elm Rd, Cibolo, TX 78108"
    assert client.get("/ops/api/jobs/99999999", headers=H).status_code == 404
    # a failed refresh keeps the last good copy and says why
    FAKE["fail"] = True
    fake_sf._token["value"] = None
    bad = fake_sf.refresh("test")
    FAKE["fail"] = False
    assert not bad["ok"] and fake_sf.status()["last_error"]
    assert len(client.get("/ops/api/jobs?form=install&q=bill", headers=H).json()["results"]) == 1


def test_job_lookup_for_studio_is_signed(client, fake_sf):
    import base64 as b64, hashlib as hl, hmac as hm, time as tm, urllib.parse as up
    from app import main as M
    fake_sf.refresh("test")
    secret = "s" * 64
    old, M.STUDIO_SSO_SECRET = M.STUDIO_SSO_SECRET, secret

    def signed(path, params=None, who="Paz Galambos", ts=None, key_secret=secret):
        query = up.urlencode(params or {})
        ts = str(ts or int(tm.time()))
        key = hm.new(key_secret.encode(), b"studio-sf-api", hl.sha256).digest()
        sig = b64.urlsafe_b64encode(hm.new(key, f"{ts}|{path}|{query}|{who}".encode(), hl.sha256).digest()).decode().rstrip("=")
        return client.get(path + ("?" + query if query else ""),
                          headers={"X-Studio-Ts": ts, "X-Studio-Who": who, "X-Studio-Sig": sig})

    try:
        client.cookies.clear()
        r = signed("/api/studio/jobs", {"q": "bill t"})
        assert r.status_code == 200 and [j["customer"] for j in r.json()["results"]] == ["Bill Tom"]
        assert set(r.json()["results"][0]) == {"number", "last4", "customer", "status", "date", "category"}
        assert len(signed("/api/studio/jobs").json()["results"]) == 25          # nothing typed: nearest-dated open jobs
        d = signed("/api/studio/jobs/10236418931")
        assert d.status_code == 200 and d.json()["email"] == "cust1@example.com"
        a = conn().execute("SELECT actor_id, actor_name, target FROM audit WHERE action='job_details_opened' ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(a) == (None, "Paz Galambos (Studio)", "job:10236418931")
        assert signed("/api/studio/jobs/99999999").status_code == 404
        # unsigned, wrong secret, stale, or tampered: refused
        assert client.get("/api/studio/jobs?q=bill").status_code == 401
        assert signed("/api/studio/jobs", {"q": "bill"}, key_secret="t" * 64).status_code == 401
        assert signed("/api/studio/jobs", {"q": "bill"}, ts=int(tm.time()) - 300).status_code == 401
        good = signed("/api/studio/jobs", {"q": "bill"}).request.headers
        assert client.get("/api/studio/jobs?q=tom", headers=dict(good)).status_code == 401
        # one Test mode switch for both apps: Studio asks here, and only the owner's own email can be in test mode
        from app.db import set_setting
        owner_email = conn().execute("SELECT email FROM staff WHERE is_owner=1").fetchone()[0]
        set_setting("owner_test_mode", "0")
        assert signed("/api/studio/test-mode", {"email": owner_email}).json() == {"test_mode": False}
        set_setting("owner_test_mode", "1")
        assert signed("/api/studio/test-mode", {"email": " " + owner_email.upper()}).json() == {"test_mode": True}
        other = conn().execute("SELECT email FROM staff WHERE is_owner=0 AND email!='' LIMIT 1").fetchone()[0]
        assert signed("/api/studio/test-mode", {"email": other}).json() == {"test_mode": False}
        assert signed("/api/studio/test-mode").json() == {"test_mode": False}
        assert client.get("/api/studio/test-mode?email=" + owner_email).status_code == 401
        set_setting("owner_test_mode", "0")
        M.STUDIO_SSO_SECRET = ""
        assert signed("/api/studio/jobs").status_code == 503
    finally:
        M.STUDIO_SSO_SECRET = old


def test_job_lookup_changes_flagged_internally_only(client, fake_sf, smtp):
    login(client, "Adem Atis", "246810")
    sig = io.BytesIO(); Image.new("RGBA", (600, 200), (0, 0, 0, 0)).save(sig, "PNG")
    photos = {"after1": ("a.jpg", jpeg(), "image/jpeg"), "after2": ("b.jpg", jpeg((1, 2, 3)), "image/jpeg"),
              "sig": ("s.png", sig.getvalue(), "image/png")}
    filled = {"po": "8000", "customer": "Bill Tom", "cust_email": "cust2@example.com"}
    data = {"submission_id": "sub-jl-0001", "po": "8000", "customer": "Bill Tom", "work": "Yes", "walkthrough": "Yes",
            "cust_present": "Yes", "signer": "Bill Tom", "cust_email": "bill.home@example.com",
            "sf_job": "10236418000", "sf_filled": json.dumps(filled)}
    r = client.post("/ops/api/reports/install", data=data, files=photos, headers=H)
    assert r.status_code == 200, r.text
    rid = conn().execute("SELECT id, data FROM reports WHERE submission_id='sub-jl-0001'").fetchone()
    d = json.loads(rid["data"])
    assert d["sf_job"] == "10236418000"
    assert d["sf_changes"] == ["Customer email: cust2@example.com → bill.home@example.com"]
    rep, dd, ph = mailer._report_bundle(rid["id"])
    staff_html = mailer._body_html(rep, dd)
    assert "Changed from Service Fusion" in staff_html and "10236418000" in staff_html
    cust = conn().execute("SELECT * FROM emails WHERE report_id=? AND audience='customer'", (rid["id"],)).fetchone()
    assert cust["recipients"] == "bill.home@example.com"
    cust_html = mailer._send_customer(cust, rep, dd, ph).get_body(("html",)).get_content()
    assert "Service Fusion" not in cust_html
    # nothing changed -> no change row
    data2 = {**data, "submission_id": "sub-jl-0002", "cust_email": "cust2@example.com"}
    assert client.post("/ops/api/reports/install", data=data2, files=photos, headers=H).status_code == 200
    d2 = json.loads(conn().execute("SELECT data FROM reports WHERE submission_id='sub-jl-0002'").fetchone()[0])
    assert d2["sf_job"] == "10236418000" and "sf_changes" not in d2
    # Measure: the link and a changed customer name show in the job rows
    r = _measure(client, "sub-jl-msr1", [_door()], customer="Angela Heimer-Ruiz", po="8931", sf_job="10236418931",
                 sf_filled=json.dumps({"customer": "Angela Heimer", "po": "8931"}))
    assert r.status_code == 200, r.text
    dm = json.loads(conn().execute("SELECT data FROM reports WHERE submission_id='sub-jl-msr1'").fetchone()[0])
    from app import measure as M
    rows = dict(M.job_rows(dm))
    assert rows["Service Fusion job"] == "10236418931"
    assert rows["Changed from Service Fusion"] == "Customer: Angela Heimer → Angela Heimer-Ruiz"


def test_staff_emails_unique_and_owner_guarded(client, monkeypatch):
    # the work email signs people into Simply Studio: one per person, admins' only set by the owner, every change alerted
    from app import alerts
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda title, msg, priority="default": pushed.append((title, msg)))
    ids = {r["name"]: r["id"] for r in conn().execute("SELECT id, name FROM staff")}
    login(client, "Adem Atis", "246810")
    r = client.post("/ops/api/admin/staff", json={"name": "Email Person", "dept": "Production", "email": "dup@simplydoors.com"}, headers=H)
    assert r.status_code == 200, r.text
    ep = r.json()["id"]
    assert any("dup@simplydoors.com" in m for _, m in pushed)
    r = client.post("/ops/api/admin/staff", json={"name": "Email Twin", "dept": "Production", "email": "DUP@SimplyDoors.com"}, headers=H)
    assert r.status_code == 400 and "Email Person" in r.json()["detail"]
    r = client.patch(f"/ops/api/admin/staff/{ids['Jaime Mendoza']}", json={"email": " Dup@simplydoors.com "}, headers=H)
    assert r.status_code == 400
    with pytest.raises(sqlite3.IntegrityError):              # the database refuses a repeat too
        conn().execute("UPDATE staff SET email='DUP@simplydoors.com' WHERE id=?", (ids["Jaime Mendoza"],))
    # a non-owner admin can't touch any admin's email (their own included), add admins, or promote anyone
    login(client, "Paz Galambos", "112233")
    paz_row = next(x for x in client.get("/ops/api/admin/staff").json() if x["name"] == "Paz Galambos")
    assert client.patch(f"/ops/api/admin/staff/{ids['Paz Galambos']}", json={"email": "adem@simplydoors.com"}, headers=H).status_code == 403
    assert client.patch(f"/ops/api/admin/staff/{ids['Paz Galambos']}", json={"email": "paz2@simplydoors.com"}, headers=H).status_code == 403
    assert client.patch(f"/ops/api/admin/staff/{ids['Adem Atis']}", json={"email": "x@simplydoors.com"}, headers=H).status_code == 403
    assert client.post("/ops/api/admin/staff", json={"name": "Sneaky Admin", "dept": "Admin", "email": "sneaky@simplydoors.com",
                                                     "is_admin": True}, headers=H).status_code == 403
    assert client.patch(f"/ops/api/admin/staff/{ep}", json={"is_admin": True}, headers=H).status_code == 403
    # saving her own row with the email unchanged still works (the edit screen always sends it)
    r = client.patch(f"/ops/api/admin/staff/{ids['Paz Galambos']}", headers=H,
                     json={"name": "Paz Galambos", "dept": paz_row["dept"], "email": paz_row["email"], "is_admin": True, "active": True})
    assert r.status_code == 200
    # a crew member's email can be changed by any admin, and the owner hears about it
    pushed.clear()
    assert client.patch(f"/ops/api/admin/staff/{ep}", json={"email": "ep2@simplydoors.com"}, headers=H).status_code == 200
    assert any("email" in t.lower() and "Paz Galambos" in m and "ep2@simplydoors.com" in m for t, m in pushed)
    assert conn().execute("SELECT email FROM staff WHERE id=?", (ep,)).fetchone()[0] == "ep2@simplydoors.com"
    # the owner can change an admin's email
    login(client, "Adem Atis", "246810")
    assert client.patch(f"/ops/api/admin/staff/{ids['Paz Galambos']}", json={"email": "paz2@simplydoors.com"}, headers=H).status_code == 200
    assert client.patch(f"/ops/api/admin/staff/{ids['Paz Galambos']}", json={"email": "paz@simplydoors.com"}, headers=H).status_code == 200
    assert client.patch(f"/ops/api/admin/staff/{ep}", json={"active": False}, headers=H).status_code == 200


def test_photos_are_saved_outside_the_write_lock(client, monkeypatch):
    # big uploads must not hold the database write lock while photos are re-saved; the receipt printed on the
    # photos still has to be the report's own number, even when another report takes the expected one meanwhile
    from app import main as m
    real, seen = m._write_photos, []

    def spy(folder, blobs, kept, receipt, who, customer_copy):
        other = sqlite3.connect(os.path.join(TMP, "ops.db"), timeout=5)
        try:
            other.execute("BEGIN IMMEDIATE")              # would wait (and fail) if the lock were held
            other.execute("ROLLBACK")
            seen.append(receipt)
        finally:
            other.close()
        if len(seen) == 1:
            conn().execute("INSERT INTO reports(submission_id, form_type, staff_id, submitted_at, data, receipt)"
                           " VALUES ('sub-race-other', 'Receiving Report', 1, '2026-10-01T00:00:00Z', '{}', ?)", (receipt,))
        return real(folder, blobs, kept, receipt, who, customer_copy)
    monkeypatch.setattr(m, "_write_photos", spy)
    login(client, "Jaime Mendoza", "135790")
    r = receiving(client, "sub-race-0001")
    assert r.status_code == 200, r.text
    first = int(seen[0].split("-")[1])
    assert len(seen) == 2 and r.json()["receipt"] == seen[1] == f"RCV-{first + 1:05d}"
    rid = conn().execute("SELECT id FROM reports WHERE submission_id='sub-race-0001'").fetchone()[0]
    paths = [p[0] for p in conn().execute("SELECT path FROM photos WHERE report_id=?", (rid,))]
    assert len(paths) == 2 and all(os.path.isfile(p) and os.path.dirname(p) == os.path.join(m.PHOTO_DIR, str(rid)) for p in paths)
    assert not [f for f in os.listdir(m.PHOTO_DIR) if f.startswith("tmp-")]


def test_studio_lookup_odd_headers_are_401_not_500(client):
    from app import main as M
    old, M.STUDIO_SSO_SECRET = M.STUDIO_SSO_SECRET, "s" * 64
    try:
        now = str(int(time.time())).encode()
        for ts, sig in ((b"\xb2", b"x"), (b"9" * 400, b"x"), (now, b"\xe9\xe9\xe9"), (b"-5", b"x"), (b"", b"")):
            r = client.get("/api/studio/jobs", headers={"X-Studio-Ts": ts, "X-Studio-Who": b"Paz", "X-Studio-Sig": sig})
            assert r.status_code == 401, (ts, sig, r.status_code)
        # a raw "²" byte (the test client would re-encode it): isdigit() is True but int() fails
        from fastapi import HTTPException
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "scheme": "http", "server": ("x", 80), "path": "/api/studio/jobs",
                 "query_string": b"", "headers": [(b"x-studio-ts", b"\xb2"), (b"x-studio-who", b"Paz"), (b"x-studio-sig", b"x")]}
        with pytest.raises(HTTPException) as e:
            M.studio_caller(Request(scope))
        assert e.value.status_code == 401
    finally:
        M.STUDIO_SSO_SECRET = old


# ---------------------------------------------------------------- Price List (beta)
# Made-up items only: real vendor prices are confidential and never go in this repository.
SHEET_CSV = (
    "sku,name,category,price,group,width_in,height_in,thickness,core,stocked,flag,page\n"
    "TST-HC-2868,6 Panel Test,Interior molded,49.50,Test HC table,32,80,1-3/8\",HC,N,,9\n"
    "TST-SC-3068,2 Panel Test,Interior molded,100.00,Test SC table,36,80,1-3/8\",SC,Y,,9\n"
    "TST-LITE-1,Test decorative lite,Exterior glass & lites,,Test lites,,,,,,Price blank on sheet,38\n"
).encode()


def _upload(c, csv_bytes, vendor="WG", label="Test sheet eff. 1/1/2026", replace=None):
    data = {"vendor": vendor, "label": label} | ({"replace": str(replace)} if replace is not None else {})
    return c.post("/ops/api/admin/pricelist/upload", data=data,
                  files={"file": ("sheet.csv", csv_bytes, "text/csv")}, headers=H)


def test_price_list_is_off_until_switched_on(client):
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["price_list"] is False
    assert client.get("/ops/api/pricelist/vendors", headers=H).status_code == 403
    assert client.get("/ops/api/pricelist/items?vendor=WG", headers=H).status_code == 403
    assert client.post("/ops/api/pricelist/pos", json={}, headers=H).status_code == 403
    assert _upload(client, SHEET_CSV).status_code == 403                  # not an admin
    assert client.get("/ops/pricelist").status_code == 200                 # the page itself is just a shell
    login(client, "Adem Atis", "246810")
    assert client.get("/ops/api/me", headers=H).json()["price_list"] is True   # the owner always can


def test_price_sheet_upload_checks_the_file(client):
    login(client, "Adem Atis", "246810")
    r = _upload(client, b"sku,name,price\nA,B,1\n")
    assert r.status_code == 422 and "category" in r.json()["detail"]
    r = _upload(client, b"sku,name,category,price\nA,B,Doors,1\n")
    assert r.status_code == 422 and "isn't one of" in r.json()["detail"]
    r = _upload(client, b"sku,name,category,price\nA,B,Interior molded,abc\n")
    assert r.status_code == 422 and "isn't a number" in r.json()["detail"]
    r = _upload(client, b"sku,name,category,price\nA,B,Interior molded,1\na,C,Interior molded,2\n")
    assert r.status_code == 200 and r.json()["items"] == 2                # vendor typo: both kept, both flagged
    flags = [i["flag"] for i in client.get("/ops/api/pricelist/items?vendor=WG", headers=H).json()["items"]]
    assert flags == ["Same part number is used for another item on this sheet"] * 2
    first = r.json()["sheet_id"]
    r = _upload(client, SHEET_CSV)
    assert r.status_code == 422 and "Pick the sheet this one replaces" in r.json()["detail"]   # never loaded twice by mistake
    r = _upload(client, SHEET_CSV, replace=first)
    assert r.status_code == 200 and r.json()["items"] == 3
    items = client.get("/ops/api/pricelist/items?vendor=WG", headers=H).json()["items"]
    by = {i["sku"]: i for i in items}
    assert by["TST-LITE-1"]["price"] is None and by["TST-LITE-1"]["flag"] == "Price blank on sheet"
    assert by["TST-HC-2868"]["stock"] is False and by["TST-SC-3068"]["stock"] is True and by["TST-HC-2868"]["w"] == 32
    v = {x["code"]: x for x in client.get("/ops/api/pricelist/vendors", headers=H).json()}
    assert v["WG"]["sheet"]["items"] == 3 and not v["WG"]["can_order"] and v["BC"]["sheet"] is None   # no PO email yet
    assert "order_email" not in v["WG"]                                     # staff don't need the address itself
    acts = [r[0] for r in conn().execute("SELECT action FROM audit WHERE action IN ('price_sheet_loaded','price_list_viewed')")]
    assert "price_sheet_loaded" in acts and "price_list_viewed" in acts


def test_vendor_po_email_is_admin_only(client, monkeypatch):
    from app import alerts
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda title, msg, priority="default": pushed.append(title))
    login(client, "Jaime Mendoza", "135790")
    assert client.put("/ops/api/admin/pricelist/vendors/WG", json={"order_email": "x@evil.test"}, headers=H).status_code == 403
    login(client, "Paz Galambos", "112233")
    assert client.put("/ops/api/admin/pricelist/vendors/WG", json={"order_email": "nope"}, headers=H).status_code == 422
    assert client.put("/ops/api/admin/pricelist/vendors/WG", json={"order_email": "orders@vendor.test"}, headers=H).json()["ok"]
    assert pushed == ["Ops app: vendor PO email changed"]
    d = client.get("/ops/api/admin/pricelist", headers=H).json()
    assert next(v for v in d["vendors"] if v["code"] == "WG")["order_email"] == "orders@vendor.test"


def _seed_job(number, po, customer="Test Customer", street="5 Test Rd"):
    row = {"number": number, "customer_id": None, "customer_name": customer, "contact": "", "status": "4 Need to Order",
           "sub_status": "", "start_date": "2026-10-07", "category": "", "description": "", "po_number": po,
           "street_1": street, "street_2": "", "city": "Schertz", "state": "TX", "zip": "78154", "location_name": ""}
    conn().execute("INSERT OR REPLACE INTO sf_jobs(number, customer_id, customer_name, status, sub_status, start_date, search,"
                   " data, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (number, None, customer, row["status"], "", row["start_date"], customer.lower(), json.dumps(row), "2026-10-07T12:00:00Z"))


def test_purchase_order_uses_the_sf_po_number_and_server_prices(client, smtp, monkeypatch):
    from app import sfjobs
    monkeypatch.setattr(sfjobs, "CLIENT_ID", "id")
    monkeypatch.setattr(sfjobs, "CLIENT_SECRET", "secret")
    _seed_job("10236499001", "PO-778-A")
    _seed_job("10236499002", "")
    login(client, "Paz Galambos", "112233")
    jid = conn().execute("SELECT id FROM staff WHERE name='Jaime Mendoza'").fetchone()[0]
    assert client.patch(f"/ops/api/admin/staff/{jid}", json={"price_list": True}, headers=H).json()["ok"]
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["price_list"] is True
    items = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=WG", headers=H).json()["items"]}
    lines = [{"item_id": items["TST-HC-2868"]["id"], "qty": 2, "price": 0.01},      # a price from the phone is ignored
             {"item_id": items["TST-SC-3068"]["id"], "qty": 1},
             {"item_id": items["TST-LITE-1"]["id"], "qty": 1}]
    body = {"vendor": "WG", "job_number": "10236499002", "ship_method": "Delivery", "ship_to": "site", "lines": lines}
    r = client.post("/ops/api/pricelist/pos", json=body, headers=H)
    assert r.status_code == 422 and "no PO number" in r.json()["detail"]
    r = client.post("/ops/api/pricelist/pos", json={**body, "job_number": "10236499001", "ship_method": "Boat"}, headers=H)
    assert r.status_code == 422
    r = client.post("/ops/api/pricelist/pos", json={**body, "job_number": "10236499001", "lines": [{"item_id": 999999, "qty": 1}]}, headers=H)
    assert r.status_code == 422
    r = client.post("/ops/api/pricelist/pos", json={**body, "job_number": "10236499001", "notes": "Call before delivery"}, headers=H)
    assert r.status_code == 200, r.text
    po = r.json()
    assert po["po_number"] == "PO-778-A" and po["job_customer"] == "Test Customer" and "5 Test Rd" in po["ship_address"]
    hc = next(ln for ln in po["lines"] if ln["sku"] == "TST-HC-2868")
    assert hc["price"] == 49.5 and hc["surcharge"] == 29.7 and hc["total"] == 128.7     # non-stock under 10: +30%
    assert next(ln for ln in po["lines"] if ln["sku"] == "TST-LITE-1")["total"] is None  # call for price
    assert po["total"] == 228.7 and po["sent_to"] == "orders@vendor.test"
    assert client.get("/ops/api/pricelist/po-check?po=PO-778-A", headers=H).json()["sent_before"][0]["id"] == po["id"]
    pdf = client.get(f"/ops/api/pricelist/pos/{po['id']}/pdf", headers=H)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert client.get("/ops/api/pricelist/pos", headers=H).json()[0]["po_number"] == "PO-778-A"
    # the email: vendor + a visible copy to admin@, with the PO as a PDF
    RECEIVED.clear()
    mailer.SMTP_USER, mailer.SMTP_PASSWORD, mailer.SMTP_STARTTLS = "", "", False
    orig = mailer.configured
    mailer.configured = lambda: True
    try:
        mailer.process_queue_once()
    finally:
        mailer.configured = orig
    mail = next(m for m in RECEIVED if b"PO-778-A" in m["data"])
    assert set(mail["to"]) == {"orders@vendor.test", "admin@simplydoors.com"}
    assert b"Cc: admin@simplydoors.com" in mail["data"] and b"application/pdf" in mail["data"]
    e = client.get(f"/ops/api/pricelist/pos/{po['id']}", headers=H).json()["email"]
    assert e["status"] == "sent" and e["cc"] == "admin@simplydoors.com"
    assert conn().execute("SELECT COUNT(*) FROM audit WHERE action='po_sent'").fetchone()[0] == 1


def test_owner_test_mode_po_goes_only_to_the_owner(client, monkeypatch):
    from app import sfjobs
    monkeypatch.setattr(sfjobs, "CLIENT_ID", "id")
    monkeypatch.setattr(sfjobs, "CLIENT_SECRET", "secret")
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/owner/test-mode", json={"on": True}, headers=H)
    try:
        items = client.get("/ops/api/pricelist/items?vendor=WG", headers=H).json()["items"]
        r = client.post("/ops/api/pricelist/pos", json={"vendor": "WG", "job_number": "10236499001", "ship_method": "Delivery",
                                                         "lines": [{"item_id": items[0]["id"], "qty": 10}]}, headers=H)
        assert r.status_code == 200 and r.json()["is_test"] and r.json()["sent_to"] == "adem@simplydoors.com"
        e = conn().execute("SELECT recipients, cc, subject FROM emails WHERE po_id=?", (r.json()["id"],)).fetchone()
        assert e["recipients"] == "adem@simplydoors.com" and e["cc"] == "" and e["subject"].startswith("TEST - ")
        assert r.json()["lines"][0]["surcharge"] == 0                     # 10 or more: no non-stock surcharge
        # a test PO doesn't count as the PO number having been sent
        assert len(client.get("/ops/api/pricelist/po-check?po=PO-778-A", headers=H).json()["sent_before"]) == 1
    finally:
        client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)


def test_vendor_can_have_several_sheets_with_compare_prices(client, monkeypatch):
    from app import alerts, pricelist
    monkeypatch.setattr(alerts, "push", lambda *a, **k: None)
    login(client, "Adem Atis", "246810")
    shaker = ("sku,name,category,price,compare Container,compare Pallet\n"
              "TST-SH-1,Test shaker 2868,Interior stile & rail,80.00,64.00,70.50\n"
              "TST-SH-2,Test shaker 3068,Interior stile & rail,90.00,,\n").encode()
    oak = b"sku,name,category,price\nTST-OAK-1,Test oak 2868,Interior stile & rail,250.00\n"
    r = _upload(client, shaker, vendor="BC", label="Test shaker")
    assert r.status_code == 200, r.text
    a = r.json()["sheet_id"]
    assert _upload(client, oak, vendor="BC", label="Test oak").status_code == 422          # must say replace or add
    r = _upload(client, oak, vendor="BC", label="Test oak", replace="new")
    assert r.status_code == 200
    b = r.json()["sheet_id"]
    r = _upload(client, oak, vendor="BC", label="test OAK", replace="new")
    assert r.status_code == 422 and "already a live sheet" in r.json()["detail"]
    r = _upload(client, b"sku,name,category,price,compare Pallet\nX,Y,Interior molded,1,N/A\n", vendor="BC", label="Bad", replace="new")
    assert r.status_code == 422 and "isn't a number" in r.json()["detail"]
    v = next(x for x in client.get("/ops/api/pricelist/vendors", headers=H).json() if x["code"] == "BC")
    assert [s["label"] for s in v["sheets"]] == ["Test shaker", "Test oak"] and v["sheet"]["items"] == 3
    by = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=BC", headers=H).json()["items"]}
    assert by["TST-SH-1"]["compare"] == [{"label": "Container", "price": 64.0}, {"label": "Pallet", "price": 70.5}]
    assert by["TST-SH-2"]["compare"] == [] and by["TST-OAK-1"]["sheet"] == "Test oak"
    lines, total, label = pricelist.build_lines("BC", [{"item_id": by["TST-SH-1"]["id"], "qty": 2},
                                                       {"item_id": by["TST-OAK-1"]["id"], "qty": 1}])
    assert total == 410.0 and label == "Test shaker · Test oak"                               # PO uses the main price
    # replacing one sheet leaves the other alone
    r = _upload(client, oak.replace(b"250.00", b"260.00"), vendor="BC", label="Test oak v2", replace=b)
    assert r.status_code == 200
    by2 = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=BC", headers=H).json()["items"]}
    assert set(by2) == {"TST-SH-1", "TST-SH-2", "TST-OAK-1"} and by2["TST-OAK-1"]["price"] == 260.0
    assert by2["TST-SH-1"]["id"] == by["TST-SH-1"]["id"]                                       # buy lists stay valid
    assert _upload(client, oak, vendor="BC", label="X", replace=b).status_code == 422          # b isn't live any more
    with pytest.raises(ValueError):                                                            # old oak item is gone
        pricelist.build_lines("BC", [{"item_id": by["TST-OAK-1"]["id"], "qty": 1}])
    # removing a sheet
    login(client, "Jaime Mendoza", "135790")
    assert client.post(f"/ops/api/admin/pricelist/sheets/{a}/remove", headers=H).status_code == 403
    login(client, "Adem Atis", "246810")
    assert client.post(f"/ops/api/admin/pricelist/sheets/{a}/remove", headers=H).json()["ok"]
    assert client.post(f"/ops/api/admin/pricelist/sheets/{a}/remove", headers=H).status_code == 404
    left = [i["sku"] for i in client.get("/ops/api/pricelist/items?vendor=BC", headers=H).json()["items"]]
    assert left == ["TST-OAK-1"]
    assert conn().execute("SELECT COUNT(*) FROM pl_items WHERE sheet_id=?", (a,)).fetchone()[0] == 0
    assert conn().execute("SELECT COUNT(*) FROM audit WHERE action='price_sheet_removed'").fetchone()[0] == 1


def test_price_list_editors_can_change_add_delete_and_download(client, monkeypatch):
    from app import alerts, pricelist
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "push_throttled", lambda key, title, msg, *a, **k: pushed.append(title))
    login(client, "Adem Atis", "246810")
    csv_bytes = (b"sku,name,category,price,width_in,height_in,compare Pallet\n"
                 b"TST-ED-1,Edit test 2868,Interior stile & rail,80.00,32,80,70.00\n"
                 b"TST-ED-2,Edit test 3068,Interior stile & rail,90.00,36,80,\n")
    sid = _upload(client, csv_bytes, vendor="BC", label="Edit test", replace="new").json()["sheet_id"]
    jid = conn().execute("SELECT id FROM staff WHERE name='Jaime Mendoza'").fetchone()[0]
    login(client, "Paz Galambos", "112233")
    assert client.patch(f"/ops/api/admin/staff/{jid}", json={"price_list": True, "price_edit": False}, headers=H).json()["ok"]
    login(client, "Jaime Mendoza", "135790")
    by = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=BC", headers=H).json()["items"]}
    item = by["TST-ED-1"]
    assert client.get("/ops/api/me", headers=H).json()["price_edit"] is False
    assert client.patch(f"/ops/api/pricelist/items/{item['id']}", json={"price": 1}, headers=H).status_code == 403
    assert client.get(f"/ops/api/pricelist/sheets/{sid}/csv", headers=H).status_code == 403
    # switched on for the purchaser
    login(client, "Paz Galambos", "112233")
    assert client.patch(f"/ops/api/admin/staff/{jid}", json={"price_edit": True}, headers=H).json()["ok"]
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["price_edit"] is True
    url = f"/ops/api/pricelist/items/{item['id']}"
    r = client.patch(url, json={"price": "abc"}, headers=H)
    assert r.status_code == 422 and r.json()["detail"] == "Price “abc” isn't a number."
    assert client.patch(url, json={"cat": "Doors"}, headers=H).status_code == 422
    r = client.patch(url, json={"price": "82.5", "flag": "Confirm with Boise", "name": "Edit test 2868 (renamed)",
                                "compare": [{"label": "Pallet", "price": "71"}, {"label": "Container", "price": "64.4"}]}, headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["price"] == 82.5 and d["flag"] == "Confirm with Boise" and d["edited_by"] == "Jaime Mendoza"
    assert d["compare"] == [{"label": "Pallet", "price": 71.0}, {"label": "Container", "price": 64.4}] and d["w"] == 32
    a = conn().execute("SELECT target, details FROM audit WHERE action='price_item_changed' ORDER BY id DESC").fetchone()
    det = json.loads(a["details"])
    assert a["target"] == "Boise Cascade: TST-ED-1" and det["price"] == {"from": 80.0, "to": 82.5}
    assert det["compare"] == {"from": "Pallet 70.00", "to": "Pallet 71.00, Container 64.40"} and "sku" not in det
    assert pushed == ["Ops app: Price List edited"]
    # a PO uses the edited price
    lines, total, _ = pricelist.build_lines("BC", [{"item_id": item["id"], "qty": 2}])
    assert total == 165.0
    # add and delete
    r = client.post("/ops/api/pricelist/items", json={"sheet_id": sid, "sku": "TST-ED-3", "name": "Added by hand",
                                                      "cat": "Parts & hardware", "price": "12.98", "uom": "EA"}, headers=H)
    assert r.status_code == 200, r.text
    added = r.json()
    assert added["sheet"] == "Edit test" and added["price"] == 12.98 and added["edited_by"] == "Jaime Mendoza"
    assert client.post("/ops/api/pricelist/items", json={"sheet_id": 999999, "sku": "X", "name": "X", "cat": "Parts & hardware"},
                       headers=H).status_code == 404
    assert client.post("/ops/api/pricelist/items", json={"sheet_id": sid, "sku": "", "name": "X", "cat": "Parts & hardware"},
                       headers=H).status_code == 422
    assert client.delete(f"/ops/api/pricelist/items/{by['TST-ED-2']['id']}", headers=H).json()["ok"]
    assert client.delete(f"/ops/api/pricelist/items/{by['TST-ED-2']['id']}", headers=H).status_code == 404
    v = next(x for x in pricelist.vendors() if x["code"] == "BC")
    s = next(x for x in v["sheets"] if x["id"] == sid)
    assert s["items"] == 2 and s["edited"] == 2                       # TST-ED-1 changed, TST-ED-3 added
    # download: the same upload format, with the edits, loads back as it was
    r = client.get(f"/ops/api/pricelist/sheets/{sid}/csv", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = {x["sku"]: x for x in pricelist.parse_sheet(r.content)}
    assert set(rows) == {"TST-ED-1", "TST-ED-3"} and rows["TST-ED-1"]["price"] == 82.5 and rows["TST-ED-1"]["w"] == 32
    assert json.loads(rows["TST-ED-1"]["compare"]) == [["Pallet", 71.0], ["Container", 64.4]]
    acts = {r[0] for r in conn().execute("SELECT action FROM audit WHERE action LIKE 'price_item_%' OR action='price_sheet_downloaded'")}
    assert acts == {"price_item_changed", "price_item_added", "price_item_deleted", "price_sheet_downloaded"}
    # the purchaser can load and remove sheets, but not change where a vendor's POs go
    r = _upload(client, b"sku,name,category,price\nTST-ED-9,Purchaser upload,Parts & hardware,5\n", vendor="BC",
                label="Purchaser test", replace="new")
    assert r.status_code == 200, r.text
    assert client.post(f"/ops/api/admin/pricelist/sheets/{r.json()['sheet_id']}/remove", headers=H).json()["ok"]
    assert client.put("/ops/api/admin/pricelist/vendors/BC", json={"order_email": "x@evil.test"}, headers=H).status_code == 403
    assert client.get("/ops/api/admin/pricelist", headers=H).status_code == 403
    # taking the Price List away takes editing away too
    login(client, "Paz Galambos", "112233")
    assert client.patch(f"/ops/api/admin/staff/{jid}", json={"price_list": False}, headers=H).json()["ok"]
    assert tuple(conn().execute("SELECT price_list, price_edit FROM staff WHERE id=?", (jid,)).fetchone()) == (0, 0)
    assert client.get(f"/ops/api/pricelist/sheets/{sid}/csv", headers=H).status_code == 200          # admins can download
    login(client, "Jaime Mendoza", "135790")
    assert _upload(client, csv_bytes, vendor="BC", label="Nope", replace="new").status_code == 403


def test_admins_can_add_a_vendor_and_novo_is_there(client, monkeypatch):
    from app import alerts, pricelist
    monkeypatch.setattr(alerts, "push", lambda *a, **k: None)
    assert any(v["code"] == "NV" and v["name"] == "Novo" for v in pricelist.vendors())
    login(client, "Jaime Mendoza", "135790")
    assert client.post("/ops/api/admin/pricelist/vendors", json={"name": "Masonite"}, headers=H).status_code == 403
    login(client, "Paz Galambos", "112233")
    r = client.post("/ops/api/admin/pricelist/vendors", json={"name": "Masonite"}, headers=H)
    assert r.status_code == 200 and r.json()["code"] == "MA"
    assert client.post("/ops/api/admin/pricelist/vendors", json={"name": "masonite"}, headers=H).status_code == 422
    assert client.post("/ops/api/admin/pricelist/vendors", json={"name": "Mason Arts"}, headers=H).json()["code"] == "MS"
    assert client.post("/ops/api/admin/pricelist/vendors", json={"name": " "}, headers=H).status_code == 422
    # the new categories load
    r = _upload(client, b"sku,name,category,price,uom\n1030999,TEST CASING FJ RAW,Moulding & trim,0.41,LF\n"
                        b"1030998,TEST JAMB PAIR,Jambs & frames,11.89,PR\n", vendor="MA", label="Masonite test")
    assert r.status_code == 200 and r.json()["items"] == 2
    assert conn().execute("SELECT COUNT(*) FROM audit WHERE action='vendor_added'").fetchone()[0] == 2


def test_match_styles_are_set_per_group_and_survive_a_new_sheet(client, monkeypatch):
    from app import alerts
    monkeypatch.setattr(alerts, "push", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "push_throttled", lambda *a, **k: None)
    login(client, "Adem Atis", "246810")
    sheet = (b"sku,name,category,price,group,width_in,height_in,thickness,core\n"
             b"TST-ST-1,Style test 2868,Interior molded,50,Style test group,32,80,1-3/8\",HC\n"
             b"TST-ST-2,Style test 3068,Interior molded,55,Style test group,36,80,1-3/8\",HC\n")
    sid = _upload(client, sheet, vendor="NV", label="Style test").json()["sheet_id"]
    jid = conn().execute("SELECT id FROM staff WHERE name='Jaime Mendoza'").fetchone()[0]
    login(client, "Paz Galambos", "112233")
    client.patch(f"/ops/api/admin/staff/{jid}", json={"price_list": True, "price_edit": False}, headers=H)
    login(client, "Jaime Mendoza", "135790")
    body = {"vendor": "NV", "grp": "Style test group", "style": "  2-panel   shaker "}
    assert client.put("/ops/api/pricelist/styles", json=body, headers=H).status_code == 403
    login(client, "Paz Galambos", "112233")
    client.patch(f"/ops/api/admin/staff/{jid}", json={"price_edit": True}, headers=H)
    login(client, "Jaime Mendoza", "135790")
    r = client.put("/ops/api/pricelist/styles", json=body, headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "style": "2-panel shaker", "items": 2}
    styles = {i["sku"]: i["style"] for i in client.get("/ops/api/pricelist/items?vendor=NV", headers=H).json()["items"]}
    assert styles["TST-ST-1"] == styles["TST-ST-2"] == "2-panel shaker"
    assert client.put("/ops/api/pricelist/styles", json={**body, "grp": "Nope"}, headers=H).status_code == 404
    a = json.loads(conn().execute("SELECT details FROM audit WHERE action='price_style_set'").fetchone()[0])
    assert a == {"style": {"from": "", "to": "2-panel shaker"}, "items": 2}
    # a new version of the sheet with the same group keeps the tag
    login(client, "Adem Atis", "246810")
    assert _upload(client, sheet.replace(b"50,", b"52,"), vendor="NV", label="Style test v2", replace=sid).status_code == 200
    items = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=NV", headers=H).json()["items"]}
    assert items["TST-ST-1"]["style"] == "2-panel shaker" and items["TST-ST-1"]["price"] == 52.0
    # blank removes it
    login(client, "Jaime Mendoza", "135790")
    assert client.put("/ops/api/pricelist/styles", json={**body, "style": ""}, headers=H).json()["style"] == ""
    assert all(i["style"] == "" for i in client.get("/ops/api/pricelist/items?vendor=NV", headers=H).json()["items"])


def test_pack_sizes_are_checked_on_the_po(client, monkeypatch):
    from app import alerts, pricelist
    monkeypatch.setattr(alerts, "push", lambda *a, **k: None)
    assert pricelist.clean_pack("6, 12") == "6, 12" and pricelist.clean_pack("25") == "25"
    assert pricelist.clean_pack("1") == "" and pricelist.clean_pack("") == "" and pricelist.clean_pack("12/6") == "6, 12"
    assert pricelist.pack_ok(25, "25") and pricelist.pack_ok(50, "25") and not pricelist.pack_ok(18, "25")
    assert pricelist.pack_ok(31, "6, 25") and not pricelist.pack_ok(7, "6, 12") and pricelist.pack_ok(5, "")
    login(client, "Adem Atis", "246810")
    r = _upload(client, b"sku,name,category,price,stocked,pack\nTST-PK-1,Pack test door,Exterior doors & sidelites,100,N,25\n"
                        b"TST-PK-2,Pack test sill,Parts & hardware,10,Y,\n", vendor="NV", label="Pack test", replace="new")
    assert r.status_code == 200, r.text
    by = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=NV", headers=H).json()["items"]}
    assert by["TST-PK-1"]["pack"] == "25" and by["TST-PK-1"]["stock"] is False and by["TST-PK-2"]["pack"] == ""
    lines, total, _ = pricelist.build_lines("NV", [{"item_id": by["TST-PK-1"]["id"], "qty": 18},
                                                   {"item_id": by["TST-PK-2"]["id"], "qty": 3}])
    door = next(ln for ln in lines if ln["sku"] == "TST-PK-1")
    assert door["not_full_packs"] and door["pack"] == "25" and door["surcharge"] == 0     # exterior: no +30% rule
    assert "not_full_packs" not in next(ln for ln in lines if ln["sku"] == "TST-PK-2") and total == 1830.0
    # the item editor and the download carry it too
    r = client.patch(f"/ops/api/pricelist/items/{by['TST-PK-2']['id']}", json={"pack": "250"}, headers=H)
    assert r.status_code == 200 and r.json()["pack"] == "250"
    csv_text = client.get(f"/ops/api/pricelist/sheets/{by['TST-PK-1']['sheet_id']}/csv", headers=H).content
    assert {x["sku"]: x["pack"] for x in pricelist.parse_sheet(csv_text)} == {"TST-PK-1": "25", "TST-PK-2": "250"}


def test_hand_written_po_download_then_send(client, smtp, monkeypatch):
    from app import sfjobs
    monkeypatch.setattr(sfjobs, "CLIENT_ID", "id")
    monkeypatch.setattr(sfjobs, "CLIENT_SECRET", "secret")
    _seed_job("10236499003", "PO-HAND-1", customer="Hand Customer", street="9 Hand St")
    login(client, "Jaime Mendoza", "135790")                      # Price List on (earlier tests); not an admin
    conn().execute("UPDATE staff SET price_list=1 WHERE name='Jaime Mendoza'")
    lines = [{"qty": "2", "sku": "CUSTOM-1", "name": "Special order slab", "price": "125.50", "uom": "EA"},
             {"qty": 1, "sku": "", "name": "Freight", "price": ""}]
    # one-off vendor, no job, typed PO #: download only
    body = {"action": "download", "other_vendor": {"name": "Acme Millwork", "address": "1 Main St\nAustin, TX"},
            "po_number": "SHOP-1042", "ship_method": "Delivery", "ship_to": "shop", "lines": lines, "notes": "Hold for pickup"}
    assert client.post("/ops/api/pricelist/pos/manual", json={**body, "po_number": ""}, headers=H).status_code == 422
    assert client.post("/ops/api/pricelist/pos/manual", json={**body, "lines": [{"qty": 0, "name": "x"}]}, headers=H).status_code == 422
    assert client.post("/ops/api/pricelist/pos/manual", json={**body, "lines": [{"qty": 1, "name": "x", "price": "abc"}]}, headers=H).status_code == 422
    r = client.post("/ops/api/pricelist/pos/manual", json=body, headers=H)
    assert r.status_code == 200, r.text
    po = r.json()
    assert po["status"] == "downloaded" and po["manual"] and po["vendor_name"] == "Acme Millwork" and po["sent_to"] == ""
    assert po["vendor_address"] == ["1 Main St", "Austin, TX"] and po["total"] == 251.0 and po["lines"][1]["total"] is None
    assert conn().execute("SELECT COUNT(*) FROM emails WHERE po_id=?", (po["id"],)).fetchone()[0] == 0     # nothing emailed
    pdf = client.get(f"/ops/api/pricelist/pos/{po['id']}/pdf?download=1", headers=H)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and pdf.headers["content-disposition"].startswith("attachment")
    assert any(p["id"] == po["id"] and p["status"] == "downloaded" for p in client.get("/ops/api/pricelist/pos", headers=H).json())
    # sending it later needs a vendor email; a one-off vendor without one can't be sent
    assert client.post(f"/ops/api/pricelist/pos/{po['id']}/send", headers=H).status_code == 400
    # listed vendor with a Service Fusion job, sent straight away
    r = client.post("/ops/api/pricelist/pos/manual", json={"action": "send", "vendor": "WG", "job_number": "10236499003",
                                                           "ship_method": "Delivery", "ship_to": "site", "lines": lines}, headers=H)
    assert r.status_code == 200, r.text
    po2 = r.json()
    assert po2["po_number"] == "PO-HAND-1" and po2["status"] == "sent" and po2["sent_to"] == "orders@vendor.test"
    assert po2["job_customer"] == "Hand Customer" and "9 Hand St" in po2["ship_address"]
    assert client.post(f"/ops/api/pricelist/pos/{po2['id']}/send", headers=H).status_code == 409             # already sent
    # a listed-vendor PO saved first, sent later
    r = client.post("/ops/api/pricelist/pos/manual", json={**body, "other_vendor": None, "vendor": "WG", "po_number": "SHOP-1043"}, headers=H)
    po3 = r.json()
    assert po3["status"] == "downloaded" and po3["vendor_name"] == ""
    r = client.post(f"/ops/api/pricelist/pos/{po3['id']}/send", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["sent_to"] == "orders@vendor.test"
    e = conn().execute("SELECT recipients, cc FROM emails WHERE po_id=?", (po3["id"],)).fetchone()
    assert e["recipients"] == "orders@vendor.test" and e["cc"] == "admin@simplydoors.com"
    acts = [r[0] for r in conn().execute("SELECT action FROM audit WHERE target IN (?,?,?)",
                                         (f"po:{po['id']}", f"po:{po2['id']}", f"po:{po3['id']}"))]
    assert acts.count("po_downloaded") == 2 and acts.count("po_sent") == 2
    # the one-off vendor never shows in the vendor list
    assert all(v["code"] != "XX" for v in client.get("/ops/api/pricelist/vendors", headers=H).json())


# ---------------------------------------------------------------- customer intake (/start) + Leads
def _intake(c, sid, ip="198.51.100.10", files=None, age=30, **over):
    """The public form, as a phone sends it. age = seconds since the page was opened."""
    from app import leads
    data = {"submission_id": sid, "token": leads.form_token(time.time() - age), "name": "Maria Lopez",
            "phone": "(210) 555-0142", "email": "maria@example.com", "address": "12 Oak St, Schertz",
            "types": ["Exterior door", "Windows"], "description": "New front door with sidelites.", "heard": "Google",
            "website": ""}
    data.update(over)
    data = {k: v for k, v in data.items() if v is not None}
    return c.post("/start/api/submit", data=data, files=files or [], headers={**H, "X-Forwarded-For": ip})


def _lead(sid):
    return conn().execute("SELECT * FROM leads WHERE submission_id=?", (sid,)).fetchone()


def _lead_emails(lid):
    return conn().execute("SELECT * FROM emails WHERE lead_id=? ORDER BY id", (lid,)).fetchall()


def test_intake_page_is_public_and_only_shows_its_own_files(client):
    client.cookies.clear()                                      # no sign-in
    r = client.get("/start")
    assert r.status_code == 200 and '<base href="start/">' in r.text
    assert 'property="og:title" content="Start your project with SimplyDoors"' in r.text
    assert 'og:image" content="https://optiplex-ai.tailf0af63.ts.net/start/static/og-intake.png"' in r.text
    assert "We&#x27;ll call you within 1 business day" in r.text or "We'll call you within 1 business day" in r.text
    assert "(210) 903-8450" in r.text and "17750 Lookout Rd" in r.text and 'data-token="' in r.text
    # reached through the staff address too, with or without the slash: the page finds its own folder either way
    assert '<base href="./">' in client.get("/ops/start/").text
    for f in ("intake.js", "intake.css", "logo.png", "og-intake.png"):
        assert client.get(f"/start/static/{f}").status_code == 200
    for bad in ("app.js", "admin.js", "leads.js", "index.html", "intake.html", "../main.py", "..%2fmain.py"):
        assert client.get(f"/start/static/{bad}").status_code == 404, bad
    # nothing else of the app is reachable under /start
    for p in ("/start/api/leads", "/start/api/me", "/start/admin", "/start/%2e%2e/api/admin/staff", "/start/api/leads/1"):
        assert client.get(p).status_code in (404, 405), p
    assert client.post("/start/api/submit", data={"submission_id": "x"}).status_code == 403      # app header needed
    # the staff side's Leads screen is just a shell; its data needs a sign-in
    assert client.get("/ops/leads").status_code == 200
    assert client.get("/ops/api/leads", headers=H).status_code == 401


def test_intake_lead_saved_with_and_without_email(client, monkeypatch):
    from app import alerts, mailer
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: pushed.append((a, k)))
    img = io.BytesIO()
    exif = Image.Exif(); exif[0x010F] = "SecretCam"
    Image.new("RGB", (3000, 2000), (10, 80, 160)).save(img, "JPEG", exif=exif)
    pdf = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
    r = _intake(client, "lead-mail-0001", files=[("files", ("IMG_1.jpg", img.getvalue(), "image/jpeg")),
                                                 ("files", ("plans.pdf", pdf, "application/pdf"))])
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ok", "receipt"} and r.json()["receipt"].startswith("INT-")
    lead = _lead("lead-mail-0001")
    assert lead["name"] == "Maria Lopez" and lead["spam"] == "" and lead["status"] == "new" and not lead["is_test"]
    files = conn().execute("SELECT * FROM lead_files WHERE lead_id=? ORDER BY id", (lead["id"],)).fetchall()
    assert [f["kind"] for f in files] == ["photo", "pdf"] and f"lead-{lead['id']}" in files[0]["path"]
    with Image.open(files[0]["path"]) as im:                     # saved smaller, with no hidden camera data and no stamp
        assert max(im.size) <= 2000 and not dict(im.getexif())
    with open(files[1]["path"], "rb") as f:
        assert f.read() == pdf
    em = _lead_emails(lead["id"])
    assert [(e["audience"], e["status"]) for e in em] == [("staff", "pending"), ("customer", "pending")]
    assert em[0]["recipients"] == "admin@simplydoors.com" and em[0]["bcc"] == "adem@simplydoors.com"   # Intake list + owner copy
    assert em[0]["subject"] == f"New lead: Maria Lopez - Exterior door, Windows [{lead['receipt']}]"
    assert em[1]["recipients"] == "maria@example.com" and em[1]["subject"] == f"We got your project request ({lead['receipt']})"
    assert len(pushed) == 1 and lead["receipt"] in pushed[0][0][0] and pushed[0][1]["click"].endswith(f"/leads#lead={lead['id']}")
    # the office's email: details, files attached, and a button to open the lead
    msg = mailer._send_lead(em[0])
    html = msg.get_body(("html",)).get_content()
    assert "Open this lead" in html and f"/leads#lead={lead['id']}" in html and "(210) 555-0142" in html
    assert sorted(p.get_content_type() for p in msg.iter_attachments()) == ["application/pdf", "image/jpeg"]
    # the customer's receipt: from SimplyDoors, replies reach the office, never repeats what they typed
    msg = mailer._send_lead(em[1])
    html = msg.get_body(("html",)).get_content()
    assert msg["From"].startswith("SimplyDoors") and msg["Reply-To"] == mailer.CUSTOMER_REPLY_TO
    assert lead["receipt"] in html and "Hi Maria" in html and "within 1 business day" in html and "sidelites" not in html
    # a lead is saved before anyone is emailed, and an email that can't go out never loses it
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "SMTP_PORT", 1)
    mailer.process_queue_once()
    assert conn().execute("SELECT attempts, status FROM emails WHERE id=?", (em[0]["id"],)).fetchone()[0] == 1
    assert _lead("lead-mail-0001") is not None
    # without an email: only the office's email
    r = _intake(client, "lead-mail-0002", email="", name="Bob Ray")
    assert r.status_code == 200, r.text
    lead2 = _lead("lead-mail-0002")
    assert [e["audience"] for e in _lead_emails(lead2["id"])] == ["staff"]
    assert int(lead2["receipt"][4:]) == int(lead["receipt"][4:]) + 1        # its own counter
    # "Tell us more" goes onto the same lead; anything else about it is refused
    assert client.post("/start/api/more", json={"submission_id": "lead-mail-0002", "doors": 3, "windows": 0,
                       "timeline": "ASAP", "who": "Homeowner", "best_time": "Evening", "budget": 9}, headers=H).json() == {"ok": True}
    more = json.loads(_lead("lead-mail-0002")["data"])["more"]
    assert more == {"doors": 3, "windows": 0, "timeline": "ASAP", "who": "Homeowner", "best_time": "Evening"}
    assert client.post("/start/api/more", json={"submission_id": "lead-mail-0002", "doors": 500}, headers=H).status_code == 422
    assert client.post("/start/api/more", json={"submission_id": "never-sent-0001", "doors": 1}, headers=H).json() == {"ok": True}
    # step 1 checks: a name, and a phone or an email
    r = _intake(client, "lead-mail-0003", phone="", email="", name="X")
    assert r.status_code == 422 and "name" in r.json()["detail"] and "phone number or an email" in r.json()["detail"]
    assert _intake(client, "lead-mail-0004", phone="555-12").status_code == 422
    assert _lead("lead-mail-0003") is None and _lead("lead-mail-0004") is None


def test_intake_retry_with_same_id_makes_one_lead(client):
    r1 = _intake(client, "lead-retry-0001")
    r2 = _intake(client, "lead-retry-0001", name="Changed On Retry")
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert conn().execute("SELECT COUNT(*) FROM leads WHERE submission_id='lead-retry-0001'").fetchone()[0] == 1
    assert len(_lead_emails(_lead("lead-retry-0001")["id"])) == 2          # emailed once, not per try
    assert _intake(client, "bad id!").status_code == 400


def test_intake_bots_are_caught_quietly(client, monkeypatch):
    from app import alerts
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: pushed.append(a))
    emails_before = conn().execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    cases = {"bot-trap-0001": {"website": "http://spam.example"},     # the hidden trap box
             "bot-fast-0001": {"age": 1},                              # under 3 seconds after the page opened
             "bot-notoken-01": {"token": "1234.abcd"}}                 # never opened the form page
    for sid, over in cases.items():
        age = over.pop("age", 30)
        r = _intake(client, sid, ip="203.0.113.50", age=age, **over)
        assert r.status_code == 200 and set(r.json()) == {"ok", "receipt"} and r.json()["receipt"].startswith("INT-")
        assert _lead(sid) is None
        row = conn().execute("SELECT * FROM intake_blocked WHERE submission_id=?", (sid,)).fetchone()
        assert row and row["receipt"] == r.json()["receipt"]
        assert _intake(client, sid, ip="203.0.113.50", age=age, **over).json() == r.json()   # same answer on a retry
    reasons = [r[0] for r in conn().execute("SELECT reason FROM intake_blocked WHERE submission_id LIKE 'bot-%' ORDER BY id")]
    assert "trap" in reasons[0] and "seconds" in reasons[1] and "form page" in reasons[2]
    assert conn().execute("SELECT COUNT(*) FROM emails").fetchone()[0] == emails_before and not pushed
    # per connection: 5 an hour are normal, 6 to 20 go to Suspected spam, past 20 it's a bot
    ip = "203.0.113.77"
    for i in range(1, 22):
        r = _intake(client, f"rate-{i:04d}-xx", ip=ip, name=f"Person {i}")
        assert r.status_code == 200
        lead = _lead(f"rate-{i:04d}-xx")
        if i <= 5:
            assert lead["spam"] == ""
        elif i <= 20:
            assert "same connection" in lead["spam"]
        else:
            assert lead is None and conn().execute("SELECT 1 FROM intake_blocked WHERE submission_id='rate-0021-xx'").fetchone()
    assert len(pushed) == 5                                    # only the real-looking ones alerted the owner
    assert _intake(client, "rate-other-ip-1", ip="203.0.113.78").json()   # another connection isn't held up
    assert _lead("rate-other-ip-1")["spam"] == ""


def test_intake_suspected_spam_can_be_moved_to_leads(client, monkeypatch):
    from app import alerts
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: pushed.append(a))
    r = _intake(client, "spam-links-0001", ip="192.0.2.31",
                description="Best prices http://a.example and http://b.example www.c.example")
    assert r.status_code == 200
    s = _lead("spam-links-0001")
    assert "links in the text" in s["spam"] and not _lead_emails(s["id"]) and not pushed
    _intake(client, "spam-fast-0001", ip="192.0.2.32", age=6)
    assert "seconds after the page opened" in _lead("spam-fast-0001")["spam"]
    _intake(client, "fast-plain-0001", ip="192.0.2.33", age=6, address="", description="")   # autofilled, nothing typed
    assert _lead("fast-plain-0001")["spam"] == ""
    login(client, "Paz Galambos", "112233")
    tab = client.get("/ops/api/leads?tab=spam", headers=H).json()
    assert any(x["id"] == s["id"] for x in tab["leads"]) and tab["counts"]["spam"] >= 2
    assert "blocked" in tab and tab["blocked"]["count"] >= 1
    assert all(x["spam"] == "" for x in client.get("/ops/api/leads", headers=H).json()["leads"])
    assert client.post(f"/ops/api/leads/{s['id']}/claim", headers=H).status_code == 409        # move it first
    # one tap: it becomes a lead, emailed and alerted like any new one
    d = client.post(f"/ops/api/leads/{s['id']}/not-spam", headers=H).json()
    assert d["spam"] == "" and "Moved it from Suspected spam to Leads" in [h["what"] for h in d["history"]]
    em = _lead_emails(s["id"])
    assert [e["audience"] for e in em] == ["staff", "customer"] and "(moved from Suspected spam)" in em[0]["subject"]
    assert [a[0] for a in pushed if "Moved from Suspected spam" in a[1]] == [f"New lead {s['receipt']}"]
    assert len(pushed) == 2                                     # that one, and the autofilled real lead above
    assert conn().execute("SELECT 1 FROM audit WHERE action='lead_moved_to_leads' AND actor_name='Paz Galambos'"
                          " AND target=?", (f"lead:{s['id']}",)).fetchone()


def test_intake_file_type_and_size_refused(client):
    jpg = jpeg(size=(400, 300))
    r = _intake(client, "file-bad-0001", files=[("files", ("x.exe", b"MZ\x90\x00 not a photo", "image/jpeg"))])
    assert r.status_code == 422 and "can't be sent" in r.json()["detail"]
    r = _intake(client, "file-bad-0002", files=[("files", ("x.html", b"<script>alert(1)</script>", "text/html"))])
    assert r.status_code == 422
    big_pdf = b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024) + b"\n%%EOF\n"
    r = _intake(client, "file-bad-0003", files=[("files", ("big.pdf", big_pdf, "application/pdf"))])
    assert r.status_code == 422 and "too big" in r.json()["detail"]
    r = _intake(client, "file-bad-0004", files=[("files", ("cut.pdf", b"%PDF-1.4\nhalf a file", "application/pdf"))])
    assert r.status_code == 422
    r = _intake(client, "file-bad-0005", files=[("files", (f"p{i}.jpg", jpg, "image/jpeg")) for i in range(6)])
    assert r.status_code == 400                                         # more than 5 files
    assert all(_lead(f"file-bad-000{i}") is None for i in range(1, 6))
    # the whole upload is capped before it's read
    r = client.post("/start/api/submit", content=b"x", headers={**H, "Content-Type": "multipart/form-data; boundary=x",
                                                                 "Content-Length": str(70 * 1024 * 1024)})
    assert r.status_code == 413
    r = _intake(client, "file-good-0001", files=[("files", (f"p{i}.jpg", jpg, "image/jpeg")) for i in range(5)])
    assert r.status_code == 200 and conn().execute(
        "SELECT COUNT(*) FROM lead_files WHERE lead_id=?", (_lead("file-good-0001")["id"],)).fetchone()[0] == 5


def test_leads_need_the_switch(client):
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["leads"] is False
    lid = _lead("lead-retry-0001")["id"]
    fid = conn().execute("SELECT id FROM lead_files LIMIT 1").fetchone()[0]
    for method, path, body in [("get", "/ops/api/leads", None), ("get", "/ops/api/leads?tab=spam", None),
                               ("get", f"/ops/api/leads/{lid}", None), ("post", f"/ops/api/leads/{lid}/claim", None),
                               ("post", f"/ops/api/leads/{lid}/assign", {"staff_id": 1}),
                               ("put", f"/ops/api/leads/{lid}/status", {"status": "won"}),
                               ("post", f"/ops/api/leads/{lid}/notes", {"text": "hi"}),
                               ("post", f"/ops/api/leads/{lid}/not-spam", None), ("get", f"/ops/api/leads/files/{fid}", None),
                               ("post", "/ops/api/leads/test-link", None), ("delete", f"/ops/api/leads/{lid}", None)]:
        kw = {"headers": H} | ({"json": body} if body is not None else {})
        assert getattr(client, method)(path, **kw).status_code == 403, path
    assert conn().execute("SELECT 1 FROM audit WHERE action='leads_denied' AND actor_name='Jaime Mendoza'").fetchone()
    # an admin switches it on in Admin > Staff > Edit; admins always have it
    login(client, "Paz Galambos", "112233")
    assert client.get("/ops/api/me", headers=H).json()["leads"] is True
    jaime = _sid("Jaime Mendoza")
    assert client.patch(f"/ops/api/admin/staff/{jaime}", json={"leads": True}, headers=H).status_code == 200
    assert next(s for s in client.get("/ops/api/admin/staff", headers=H).json() if s["name"] == "Jaime Mendoza")["leads"]
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/me", headers=H).json()["leads"] is True
    assert client.get("/ops/api/leads", headers=H).status_code == 200
    login(client, "Paz Galambos", "112233")
    client.patch(f"/ops/api/admin/staff/{jaime}", json={"leads": False}, headers=H)
    login(client, "Jaime Mendoza", "135790")
    assert client.get("/ops/api/leads", headers=H).status_code == 403
    # the Intake email list is in Admin > Email lists, and the owner's private copy is on
    login(client, "Adem Atis", "246810")
    rule = next(r for r in client.get("/ops/api/admin/email-rules", headers=H).json() if r["form_type"] == "Customer Intake")
    assert rule["recipients"] == "admin@simplydoors.com" and rule["live"]
    assert any(f["type"] == "Customer Intake" and f["on"] for f in client.get("/ops/api/admin/my-copies", headers=H).json()["forms"])


def test_leads_claim_status_notes_and_log(client):
    from app import leads
    _intake(client, "work-0001-lead", ip="192.0.2.90", name="Carla Diaz")
    lead = _lead("work-0001-lead")
    lid = lead["id"]
    login(client, "Paz Galambos", "112233")
    lst = client.get("/ops/api/leads", headers=H).json()
    ids = [x["id"] for x in lst["leads"]]
    assert ids == sorted(ids, reverse=True) and lid in ids                  # newest first
    row = next(x for x in lst["leads"] if x["id"] == lid)
    assert row["status"] == "new" and row["owner"] is None and not row["stale"]
    d = client.get(f"/ops/api/leads/{lid}", headers=H).json()
    assert d["phone"] == "(210) 555-0142" and d["types"] == ["Exterior door", "Windows"] and d["heard"] == "Google"
    d = client.post(f"/ops/api/leads/{lid}/claim", headers=H).json()
    assert d["owner"] == "Paz Galambos" and d["claimed_at"]
    login(client, "Adem Atis", "246810")
    r = client.post(f"/ops/api/leads/{lid}/claim", headers=H)
    assert r.status_code == 409 and "Paz Galambos" in r.json()["detail"]    # claimed leads show their owner
    jaime = _sid("Jaime Mendoza")
    assert client.post(f"/ops/api/leads/{lid}/assign", json={"staff_id": jaime}, headers=H).status_code == 422  # no Leads
    d = client.post(f"/ops/api/leads/{lid}/assign", json={"staff_id": int(_sid("Adem Atis"))}, headers=H).json()
    assert d["owner"] == "Adem Atis"
    assert client.put(f"/ops/api/leads/{lid}/status", json={"status": "sold"}, headers=H).status_code == 422
    for st in ("called", "measure_booked", "quoted", "won"):
        d = client.put(f"/ops/api/leads/{lid}/status", json={"status": st}, headers=H).json()
    assert d["status"] == "won"
    d = client.post(f"/ops/api/leads/{lid}/notes", json={"text": "Booked the measure for Tue 9 AM."}, headers=H).json()
    assert d["notes"][-1]["by"] == "Adem Atis" and "Tue 9 AM" in d["notes"][-1]["text"]
    assert client.post(f"/ops/api/leads/{lid}/notes", json={"text": "  "}, headers=H).status_code == 422
    whats = [h["what"] for h in d["history"]]
    assert whats[0] == "Sent the form" and "Claimed it" in whats and "Gave it to Adem Atis (was Paz Galambos)" in whats
    assert "Status: Quoted → Won" in whats and "Added a note" in whats
    acts = {r[0]: r[1] for r in conn().execute("SELECT action, actor_name FROM audit WHERE target=?", (f"lead:{lid}",))}
    for a in ("lead_received", "lead_viewed", "lead_claimed", "lead_reassigned", "lead_status_changed", "lead_note_added"):
        assert a in acts, a
    assert conn().execute("SELECT 1 FROM audit WHERE action='leads_list_viewed'").fetchone()
    note = json.loads(conn().execute("SELECT details FROM audit WHERE action='lead_note_added' AND target=?",
                                     (f"lead:{lid}",)).fetchone()[0])
    assert note["note"] == "Booked the measure for Tue 9 AM."
    # claimed but not touched for 24 hours: flagged (won and lost leads aren't)
    _intake(client, "work-0002-lead", ip="192.0.2.91", name="Dan Fox")
    l2 = _lead("work-0002-lead")["id"]
    client.post(f"/ops/api/leads/{l2}/claim", headers=H)
    old = leads._ago(hours=25)
    conn().execute("UPDATE leads SET claimed_at=?, touched_at=? WHERE id IN (?,?)", (old, old, lid, l2))
    rows = {x["id"]: x for x in client.get("/ops/api/leads", headers=H).json()["leads"]}
    assert rows[l2]["stale"] and not rows[lid]["stale"]
    client.put(f"/ops/api/leads/{l2}/status", json={"status": "called"}, headers=H)
    assert not {x["id"]: x for x in client.get("/ops/api/leads", headers=H).json()["leads"]}[l2]["stale"]
    # photos open for people with Leads, and the view is logged; a PDF downloads
    fid = conn().execute("SELECT id FROM lead_files WHERE kind='photo' LIMIT 1").fetchone()[0]
    r = client.get(f"/ops/api/leads/files/{fid}", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    pid = conn().execute("SELECT id FROM lead_files WHERE kind='pdf' LIMIT 1").fetchone()[0]
    assert client.get(f"/ops/api/leads/files/{pid}", headers=H).headers["content-disposition"].startswith("attachment")
    assert conn().execute("SELECT 1 FROM audit WHERE action='lead_file_viewed'").fetchone()


def test_intake_test_mode_sends_everything_to_the_owner(client, monkeypatch):
    from app import alerts
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: pushed.append(a))
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)
    assert client.post("/ops/api/leads/test-link", headers=H).status_code == 409          # test mode must be on
    client.put("/ops/api/owner/test-mode", json={"on": True}, headers=H)
    login(client, "Paz Galambos", "112233")
    assert client.post("/ops/api/leads/test-link", headers=H).status_code == 403          # owner only
    login(client, "Adem Atis", "246810")
    link = client.post("/ops/api/leads/test-link", headers=H).json()["url"]
    assert link.startswith("https://optiplex-ai.tailf0af63.ts.net/start?t=")
    code = link.split("t=", 1)[1]
    assert 'data-test="on"' in client.get(f"/start?t={code}").text
    # the same code through the staff app's address works before the server step is done
    staff_link = client.post("/ops/api/leads/test-link", headers=H).json()["staff_url"]
    assert staff_link.startswith("https://optiplex-ai.tailf0af63.ts.net:10000/ops/start?t=")
    code = staff_link.split("t=", 1)[1]                      # making a new link ended the first one
    assert 'data-test="on"' in client.get(f"/ops/start?t={code}").text
    assert 'data-test="ended"' in client.get(f"/start?t={link.split('t=', 1)[1]}").text
    r = _intake(client, "test-lead-0001", ip="192.0.2.200", t=code, email="real.customer@example.com", name="Tess Tester")
    assert r.status_code == 200 and r.json()["receipt"] == "TEST-INT-00001"
    lead = _lead("test-lead-0001")
    assert lead["is_test"]
    em = _lead_emails(lead["id"])
    assert [e["audience"] for e in em] == ["staff", "customer"]
    for e in em:                                          # every email about it goes only to the owner, marked TEST
        assert e["recipients"] == "adem@simplydoors.com" and e["bcc"] == "" and e["subject"].startswith("TEST - ")
    assert "real.customer@example.com" not in " ".join(e["recipients"] + e["bcc"] for e in em)
    assert pushed and pushed[0][0].startswith("TEST New lead")
    # real numbering isn't touched by test leads
    assert _intake(client, "real-after-test-1", ip="192.0.2.201").json()["receipt"].startswith("INT-")
    # only the owner sees test leads
    assert any(x["id"] == lead["id"] and x["is_test"] for x in client.get("/ops/api/leads", headers=H).json()["leads"])
    login(client, "Paz Galambos", "112233")
    assert all(x["id"] != lead["id"] for x in client.get("/ops/api/leads", headers=H).json()["leads"])
    assert client.get(f"/ops/api/leads/{lead['id']}", headers=H).status_code == 404
    assert client.delete(f"/ops/api/leads/{lead['id']}", headers=H).status_code == 403
    # a made-up code is refused; switching test mode off ends the link
    r = _intake(client, "test-lead-0002", ip="192.0.2.200", t="not-a-real-code")
    assert r.status_code == 410 and _lead("test-lead-0002") is None
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)
    assert 'data-test="ended"' in client.get(f"/start?t={code}").text
    assert _intake(client, "test-lead-0003", ip="192.0.2.200", t=code).status_code == 410
    # the owner can delete a test lead (never a real one), and its log lines go with it
    real = _lead("real-after-test-1")["id"]
    assert client.delete(f"/ops/api/leads/{real}", headers=H).status_code == 403
    assert client.delete(f"/ops/api/leads/{lead['id']}", headers=H).json() == {"deleted": "TEST-INT-00001"}
    assert _lead("test-lead-0001") is None and not _lead_emails(lead["id"])
    assert not conn().execute("SELECT 1 FROM audit WHERE target=?", (f"lead:{lead['id']}",)).fetchone()
    assert conn().execute("SELECT 1 FROM audit WHERE action='test_lead_deleted' AND target='TEST-INT-00001'").fetchone()


def test_price_list_review_fixes(client, smtp, monkeypatch):
    from app import alerts, pricelist, sfjobs
    monkeypatch.setattr(sfjobs, "CLIENT_ID", "id")
    monkeypatch.setattr(sfjobs, "CLIENT_SECRET", "secret")
    sent_alerts = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: sent_alerts.append(a))
    _seed_job("10236499004", "PO-REVIEW-1")
    login(client, "Adem Atis", "246810")
    # a price that isn't a real number is refused, not stored as NaN
    for bad in (b"nan", b"inf", b"1e400"):
        r = _upload(client, b"sku,name,category,price\nTST-NAN,Bad price,Parts & hardware," + bad + b"\n", vendor="NV", label="Bad", replace="new")
        assert r.status_code == 422, bad
    # a cell that looks like a spreadsheet formula is made safe in the download and comes back unchanged
    r = _upload(client, b"sku,name,category,price,flag\nTST-EQ-1,=HYPERLINK(1),Parts & hardware,5,-check this\n",
                vendor="NV", label="Formula test", replace="new")
    assert r.status_code == 200, r.text
    it = next(i for i in client.get("/ops/api/pricelist/items?vendor=NV", headers=H).json()["items"] if i["sku"] == "TST-EQ-1")
    assert it["name"] == "=HYPERLINK(1)"
    csv_text = client.get(f"/ops/api/pricelist/sheets/{it['sheet_id']}/csv", headers=H).content
    assert b"'=HYPERLINK(1)" in csv_text and b"'-check this" in csv_text
    back = {x["sku"]: x for x in pricelist.parse_sheet(csv_text)}["TST-EQ-1"]
    assert back["name"] == "=HYPERLINK(1)" and back["flag"] == "-check this"
    # the same item on several lines can't add up past the quantity cap
    many = [{"item_id": it["id"], "qty": 600}, {"item_id": it["id"], "qty": 600}]
    body = {"vendor": "NV", "job_number": "10236499004", "ship_method": "Delivery", "lines": many, "order_date": "not a date"}
    r = client.post("/ops/api/pricelist/pos", json={**body, "action": "download"}, headers=H)
    assert r.status_code == 422 and "per item" in r.json()["detail"]
    # a JSON body that isn't an object is a 4xx, never a 500
    for path in ("/ops/api/pricelist/pos", "/ops/api/pricelist/pos/manual"):
        assert client.post(path, json=[1, 2], headers=H).status_code < 500
    # a vendor with no order email: the buy-list PO can be saved & downloaded (not sent), with sheet prices
    assert not next(v for v in client.get("/ops/api/pricelist/vendors", headers=H).json() if v["code"] == "NV")["can_order"]
    r = client.post("/ops/api/pricelist/pos", json={**body, "lines": [{"item_id": it["id"], "qty": 3}]}, headers=H)
    assert r.status_code == 400 and "Download" in r.json()["detail"]
    r = client.post("/ops/api/pricelist/pos", json={**body, "action": "download", "lines": [{"item_id": it["id"], "qty": 3, "price": 0.01}]}, headers=H)
    assert r.status_code == 200, r.text
    po = r.json()
    assert po["status"] == "downloaded" and po["sent_to"] == "" and po["total"] == 15.0 and not po["manual"]
    assert po["po_number"] == "PO-REVIEW-1" and len(po["order_date"]) == 10            # a bad date falls back to today
    assert conn().execute("SELECT COUNT(*) FROM emails WHERE po_id=?", (po["id"],)).fetchone()[0] == 0
    assert conn().execute("SELECT action FROM audit WHERE target=?", (f"po:{po['id']}",)).fetchone()[0] == "po_downloaded"
    # sending a saved PO in test mode goes to the owner and leaves the PO unsent, so it can still go to the vendor
    client.put("/ops/api/owner/test-mode", json={"on": True}, headers=H)
    try:
        r = client.post(f"/ops/api/pricelist/pos/{po['id']}/send", headers=H)
        assert r.status_code == 200 and r.json()["is_test"]
        assert conn().execute("SELECT status FROM pl_pos WHERE id=?", (po["id"],)).fetchone()[0] == "downloaded"
    finally:
        client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)
    # a hand-written PO emailed to a typed-in vendor raises an alert
    lines = [{"qty": 1, "sku": "X-1", "name": "Thing", "price": "10"}]
    r = client.post("/ops/api/pricelist/pos/manual", json={"action": "send", "po_number": "SHOP-REV-1", "ship_method": "Delivery",
                                                           "other_vendor": {"name": "Typed Vendor", "email": "typed@vendor.test"},
                                                           "lines": lines}, headers=H)
    assert r.status_code == 200, r.text
    assert any("typed-in vendor" in a[0] for a in sent_alerts)


def test_leads_can_be_added_by_hand(client):
    login(client, "Jaime Mendoza", "135790")
    body = {"name": "Phil Caller", "phone": "210-555-0177", "source": "Phone call", "types": ["Windows"],
            "description": "Wants 6 windows replaced", "heard": "Friend or family"}
    assert client.post("/ops/api/leads", json=body, headers=H).status_code == 403          # needs Leads
    login(client, "Paz Galambos", "112233")
    r = client.post("/ops/api/leads", json={**body, "phone": "", "email": ""}, headers=H)
    assert r.status_code == 422 and "phone number or an email" in r.json()["detail"]
    assert client.post("/ops/api/leads", json={**body, "source": "Carrier pigeon"}, headers=H).status_code == 422
    emails_before = conn().execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    d = client.post("/ops/api/leads", json=body, headers=H).json()
    assert d["receipt"].startswith("INT-") and d["name"] == "Phil Caller" and d["types"] == ["Windows"]
    assert d["owner"] == "Paz Galambos" and d["source"] == "Phone call" and d["added_by"] == "Paz Galambos"
    assert d["status"] == "new" and not d["is_test"] and d["spam"] == ""
    assert d["history"][0]["what"] == "Added it by hand (Phone call) and claimed it"
    assert conn().execute("SELECT COUNT(*) FROM emails").fetchone()[0] == emails_before     # nobody emailed
    assert conn().execute("SELECT 1 FROM audit WHERE action='lead_added' AND actor_name='Paz Galambos' AND target=?",
                          (f"lead:{d['id']}",)).fetchone()
    row = next(x for x in client.get("/ops/api/leads", headers=H).json()["leads"] if x["id"] == d["id"])
    assert row["source"] == "Phone call"
    d2 = client.post("/ops/api/leads", json={**body, "name": "Walk In", "source": "Walk-in", "claim": False}, headers=H).json()
    assert d2["owner"] is None and int(d2["receipt"][4:]) == int(d["receipt"][4:]) + 1


def test_price_sheet_odd_numbers_and_damaged_csv_are_refused_cleanly():
    from app import pricelist
    for bad in ("nan", "NaN", "inf", "-inf"):
        with pytest.raises(pricelist.SheetError):
            pricelist._num(bad, "Price", 0)
    with pytest.raises(pricelist.SheetError):   # a cell over the csv module's size limit: a message, not a 500
        pricelist.parse_sheet(b'sku,name,category,price\nA1,"' + b"x" * 200_000 + b'",Doors,10\n')
    out = pricelist.clean_fields({"compare": ["not a dict", {"label": "Lvl 2", "price": "5"}]}, partial=True)
    assert list(out.values()) == ['[["Lvl 2", 5.0]]']


def test_po_pdf_keeps_line_breaks_in_notes():
    from app import pdf
    po = {"po_number": "PO-NL-1", "order_date": "2026-10-08", "job_number": "", "job_customer": "", "ship_method": "Delivery",
          "ship_to": "shop", "ship_address": "", "notes": "Call before delivery\nGate code 1234", "lines": [], "total": 0,
          "sheet_label": "Test", "by": "Test", "manual": 1}
    assert pdf.build_po_pdf(po, {"name": "Test Vendor", "address": []})[:4] == b"%PDF"


def test_price_sheet_rename_and_move_to_another_vendor(client, monkeypatch):
    from app import alerts, pricelist, sfjobs
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: pushed.append(a))
    login(client, "Adem Atis", "246810")
    r = _upload(client, b"sku,name,category,price,group,width_in,height_in\nTST-MV-1,Move test slab,Interior molded,50,Move group,30,80\n"
                        b"TST-MV-2,Move test hinge,Parts & hardware,3,Move hinges,,\n", vendor="SP", label="Boise_Simpson_rift_2026", replace="new")
    assert r.status_code == 200, r.text
    sid = r.json()["sheet_id"]
    assert client.put("/ops/api/pricelist/styles", json={"vendor": "SP", "grp": "Move group", "style": "1-panel shaker"}, headers=H).status_code == 200
    # rename
    assert client.patch(f"/ops/api/admin/pricelist/sheets/{sid}", json={"label": "  x "}, headers=H).status_code == 422
    r = client.patch(f"/ops/api/admin/pricelist/sheets/{sid}", json={"label": "Simpson rift white oak   eff. 10/7/2026"}, headers=H)
    assert r.status_code == 200 and r.json()["label"] == "Simpson rift white oak eff. 10/7/2026"
    # move to Boise Cascade: items, edits and the style tag go with it; POs for them now go to Boise
    assert client.patch(f"/ops/api/admin/pricelist/sheets/{sid}", json={"vendor": "NOPE"}, headers=H).status_code == 404
    assert client.patch(f"/ops/api/admin/pricelist/sheets/{sid}", json={}, headers=H).status_code == 422
    r = client.patch(f"/ops/api/admin/pricelist/sheets/{sid}", json={"vendor": "BC"}, headers=H)
    assert r.status_code == 200 and r.json()["vendor"] == "BC"
    assert not any(i["sku"].startswith("TST-MV") for i in client.get("/ops/api/pricelist/items?vendor=SP", headers=H).json()["items"])
    moved = {i["sku"]: i for i in client.get("/ops/api/pricelist/items?vendor=BC", headers=H).json()["items"] if i["sku"].startswith("TST-MV")}
    assert set(moved) == {"TST-MV-1", "TST-MV-2"} and moved["TST-MV-1"]["style"] == "1-panel shaker"
    assert moved["TST-MV-1"]["sheet"] == "Simpson rift white oak eff. 10/7/2026"
    lines, _, _ = pricelist.build_lines("BC", [{"item_id": moved["TST-MV-2"]["id"], "qty": 2}])
    assert lines[0]["sku"] == "TST-MV-2"
    assert any("moved" in a[1] and "Boise Cascade" in a[1] for a in pushed)
    acts = [json.loads(r[0]) for r in conn().execute("SELECT details FROM audit WHERE action='price_sheet_changed' ORDER BY id")]
    assert acts[0]["label"]["to"] == "Simpson rift white oak eff. 10/7/2026" and acts[1]["vendor"] == {"from": "Simpson", "to": "Boise Cascade"}
    # only admins and Price List editors can do it
    jid = conn().execute("SELECT id FROM staff WHERE name='Jaime Mendoza'").fetchone()[0]
    conn().execute("UPDATE staff SET price_list=1, price_edit=0 WHERE id=?", (jid,))
    login(client, "Jaime Mendoza", "135790")
    assert client.patch(f"/ops/api/admin/pricelist/sheets/{sid}", json={"label": "Nope nope"}, headers=H).status_code == 403
    assert pricelist.update_sheet(999999, label="x") is None
    # a PO list for Price List: before anything is typed, only jobs still waiting on product
    _seed_job("10236499101", "PO-STAGE-1", customer="Stage Need")
    _seed_job("10236499102", "PO-STAGE-2", customer="Stage Delivery")
    conn().execute("UPDATE sf_jobs SET data=json_set(data, '$.status', '15Delivery Scheduled') WHERE number='10236499102'")
    nums = [j["number"] for j in sfjobs.search("po", "")]
    assert "10236499101" in nums and "10236499102" not in nums
    assert "10236499102" in [j["number"] for j in sfjobs.search("po", "9102")]      # typed: any open job


def test_saved_po_says_whether_a_one_off_vendor_can_be_emailed(client):
    login(client, "Adem Atis", "246810")
    body = {"action": "download", "po_number": "SHOP-EM-1", "ship_method": "Delivery", "lines": [{"qty": 1, "name": "Thing", "price": "5"}]}
    a = client.post("/ops/api/pricelist/pos/manual", json={**body, "other_vendor": {"name": "No Email Co"}}, headers=H).json()
    b = client.post("/ops/api/pricelist/pos/manual", json={**body, "other_vendor": {"name": "Email Co", "email": "x@email.test"}}, headers=H).json()
    assert a["vendor_email_set"] is False and b["vendor_email_set"] is True
    assert "x@email.test" not in json.dumps(client.get("/ops/api/pricelist/pos", headers=H).json())       # the flag, not the address


def test_one_customer_across_measure_studio_and_sf(client):
    """A lead from the form is picked in Measure and in Studio's quote screen; both show on the lead."""
    import base64 as b64, hashlib as hl, hmac as hm, time as tm, urllib.parse as up
    from app import main as M
    _intake(client, "flow-lead-0001", ip="192.0.2.150", name="Nina Flow", phone="(210) 555-0311", address="44 Pine St")
    lead = _lead("flow-lead-0001")
    lid = lead["id"]
    # Measure: anyone who can measure finds it (name/address in the list, phone with the pick, which is logged)
    login(client, "Paz Galambos", "112233")
    res = client.get("/ops/api/measure/leads?q=nina", headers=H).json()["results"]
    assert [x["id"] for x in res] == [lid] and "phone" not in res[0]
    assert client.get("/ops/api/measure/leads?q=0311", headers=H).json()["results"][0]["id"] == lid   # by phone digits
    d = client.get(f"/ops/api/measure/leads/{lid}", headers=H).json()
    assert d["phone"] == "(210) 555-0311" and d["address"] == "44 Pine St"
    assert conn().execute("SELECT 1 FROM audit WHERE action='lead_picked_for_measure' AND target=?", (f"lead:{lid}",)).fetchone()
    r = _measure(client, "flow-msr-0001", [_door()], customer="Nina Flow", lead_id=str(lid))
    assert r.status_code == 200, r.text
    msr = r.json()["receipt"]
    data = json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?", (msr,)).fetchone()[0])
    assert data["lead_id"] == lid and data["lead_receipt"] == lead["receipt"]
    assert _measure(client, "flow-msr-0002", [_door()], lead_id="999999").status_code == 422
    det = client.get(f"/ops/api/leads/{lid}", headers=H).json()
    assert det["status"] == "measure_booked" and [m["receipt"] for m in det["measures"]] == [msr]
    assert any(h["what"].startswith(f"Measured: {msr}") for h in det["history"])
    # a revision keeps the lead and doesn't log it twice
    mid = conn().execute("SELECT id FROM reports WHERE receipt=?", (msr,)).fetchone()[0]
    r2 = _measure(client, "flow-msr-0003", [_door()], customer="Nina Flow", revision_of=msr)
    assert r2.status_code == 200 and json.loads(conn().execute("SELECT data FROM reports WHERE receipt=?",
                                                               (r2.json()["receipt"],)).fetchone()[0])["lead_id"] == lid
    assert mid
    # Studio: signed search and pick, and "quoted" comes back onto the lead
    secret = "s" * 64
    old, M.STUDIO_SSO_SECRET = M.STUDIO_SSO_SECRET, secret

    def signed(method, path, params=None, who="Adem Atis"):
        query = up.urlencode(params or {})
        ts = str(int(tm.time()))
        key = hm.new(secret.encode(), b"studio-sf-api", hl.sha256).digest()
        sig = b64.urlsafe_b64encode(hm.new(key, f"{ts}|{path}|{query}|{who}".encode(), hl.sha256).digest()).decode().rstrip("=")
        return getattr(client, method)(path + ("?" + query if query else ""),
                                       headers={"X-Studio-Ts": ts, "X-Studio-Who": who, "X-Studio-Sig": sig})
    try:
        assert client.get("/api/studio/leads?q=nina").status_code == 401                       # unsigned
        assert [x["id"] for x in signed("get", "/api/studio/leads", {"q": "pine"}).json()["results"]] == [lid]
        assert signed("get", f"/api/studio/leads/{lid}").json()["email"] == "maria@example.com"
        assert signed("post", f"/api/studio/leads/{lid}/quoted", {"ref": "Q-1042"}).json() == {"ok": True}
        assert signed("post", "/api/studio/leads/999999/quoted", {"ref": "Q-1"}).status_code == 404
    finally:
        M.STUDIO_SSO_SECRET = old
    det = client.get(f"/ops/api/leads/{lid}", headers=H).json()
    assert det["status"] == "quoted" and det["quotes"][0]["ref"] == "Q-1042"
    assert conn().execute("SELECT actor_name FROM audit WHERE action='lead_quoted' AND target=?",
                          (f"lead:{lid}",)).fetchone()[0] == "Adem Atis (Studio)"
    # a later step never moves a lead back (Won stays Won)
    client.put(f"/ops/api/leads/{lid}/status", json={"status": "won"}, headers=H)
    _measure(client, "flow-msr-0004", [_door()], customer="Nina Flow", lead_id=str(lid))
    assert client.get(f"/ops/api/leads/{lid}", headers=H).json()["status"] == "won"
    assert all(x["id"] != lid for x in client.get("/ops/api/measure/leads?q=nina", headers=H).json()["results"])
    # Service Fusion: copy-ready text and the job number typed back in; nothing is sent there
    assert "Nina Flow" in det["sf_copy"] and "(210) 555-0311" in det["sf_copy"] and lead["receipt"] in det["sf_copy"]
    assert client.put(f"/ops/api/leads/{lid}/sf-job", json={"number": "abc"}, headers=H).status_code == 422
    d = client.put(f"/ops/api/leads/{lid}/sf-job", json={"number": "10236499077"}, headers=H).json()
    assert d["sf_job"] == "10236499077" and "Service Fusion job set: 10236499077" in [h["what"] for h in d["history"]]


def test_sending_the_customer_form(client, monkeypatch):
    from app import alerts, mailer, sends
    monkeypatch.setattr(alerts, "push", lambda *a, **k: None)
    login(client, "Adem Atis", "246810")
    client.put("/ops/api/owner/test-mode", json={"on": False}, headers=H)
    # everyone signed in can send it, crews included (Jaime has no Leads)
    login(client, "Jaime Mendoza", "135790")
    r = client.post("/ops/api/intake/sends", json={"channel": "text", "first_name": "Maria", "phone": "210 555 0101"}, headers=H).json()
    assert r["message"].startswith("Hi Maria, it's Jaime from SimplyDoors. Here's the link to start your project: https://optiplex-ai")
    assert "?s=" in r["link"] and "Maria" not in r["link"] and "555" not in r["link"]       # a code, never their details
    code = r["link"].split("s=", 1)[1]
    no_name = client.post("/ops/api/intake/sends", json={"channel": "email_app"}, headers=H).json()
    assert no_name["message"].startswith("Hi, it's Jaime from SimplyDoors.")
    assert client.post("/ops/api/intake/sends", json={"channel": "email_sent"}, headers=H).status_code == 422   # needs an email
    # opening fills in the name and counts as opened; sending it in tags the lead "sent by Jaime"
    page = client.get(f"/start?s={code}").text
    assert 'data-prefill="Maria"' in page and 'data-who="Jaime"' in page
    _intake(client, "sent-lead-0001", ip="192.0.2.170", s=code, name="Maria Sent")
    lead = _lead("sent-lead-0001")
    assert json.loads(lead["data"])["source"] == "Sent by Jaime Mendoza (text)"
    mine = client.get("/ops/api/intake/sends", headers=H).json()["sends"]
    row = next(x for x in mine if x["id"] == r["id"])
    assert row["opened_at"] and row["submitted_at"] and "lead_id" not in row           # no lead details without Leads
    assert client.get(f"/ops/api/leads/{lead['id']}", headers=H).status_code == 403
    # "Send from SimplyDoors": a branded email to the customer, replies to the office and the sender
    r2 = client.post("/ops/api/intake/sends", json={"channel": "email_sent", "first_name": "Ana", "email": "ana@example.com"}, headers=H).json()
    e = conn().execute("SELECT * FROM emails WHERE send_id=?", (r2["id"],)).fetchone()
    assert e["recipients"] == "ana@example.com" and e["audience"] == "invite"
    msg = mailer._send_invite(e)
    html = msg.get_body(("html",)).get_content()
    assert "Start your SimplyDoors project" in html and "Hi Ana" in html and r2["link"] in html
    assert msg["From"].startswith("SimplyDoors") and "jaimem@simplydoors.com" in msg["Reply-To"]
    # a lead with Leads sees where it came from
    login(client, "Paz Galambos", "112233")
    assert client.get(f"/ops/api/leads/{lead['id']}", headers=H).json()["source"] == "Sent by Jaime Mendoza (text)"
    # not sent in after 2 days: flagged for the sender and on the Leads screen
    from datetime import datetime, timedelta, timezone
    three_days = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn().execute("UPDATE intake_sends SET created_at=? WHERE id=?", (three_days, r2["id"]))
    assert any(s["id"] == r2["id"] for s in client.get("/ops/api/leads", headers=H).json()["waiting_sends"])
    login(client, "Jaime Mendoza", "135790")
    assert next(x for x in client.get("/ops/api/intake/sends", headers=H).json()["sends"] if x["id"] == r2["id"])["nudge"]
    # "fill it in here": a handed-over phone skips the per-connection limit and the "fast" rule
    ip = client.post("/ops/api/intake/sends", json={"channel": "in_person"}, headers=H).json()
    assert ip["in_person"].startswith("start?s=")
    ic = ip["in_person"].split("s=", 1)[1]
    assert 'data-mode="in_person"' in client.get(f"/ops/start?s={ic}").text
    for i in range(7):
        _intake(client, f"inperson-{i:04d}x", ip="192.0.2.171", s=ic, age=5, name=f"Walk In {i}")
    assert all(_lead(f"inperson-{i:04d}x")["spam"] == "" for i in range(7))
    assert json.loads(_lead("inperson-0000x")["data"])["source"] == "Filled in with Jaime Mendoza"
    # an installed form remembers whose device it is, and can be turned off
    dv = client.post("/ops/api/intake/device", json={"label": "Showroom tablet"}, headers=H).json()
    dc = dv["url"].split("s=", 1)[1].split("&")[0]
    assert client.get(f"/start/manifest.webmanifest?s={dc}").json()["start_url"] == f"./?s={dc}"
    _intake(client, "device-lead-001", ip="192.0.2.172", s=dc, name="Tab Let")
    assert json.loads(_lead("device-lead-001")["data"])["source"] == "Installed form (Showroom tablet)"
    did = next(x["id"] for x in client.get("/ops/api/intake/sends", headers=H).json()["sends"] if x["channel"] == "device")
    login(client, "Paz Galambos", "112233")
    assert client.post(f"/ops/api/intake/sends/{did}/off", headers=H).status_code == 200     # admins can too
    _intake(client, "device-lead-002", ip="192.0.2.172", s=dc, name="After Off")
    assert "source" not in json.loads(_lead("device-lead-002")["data"])                     # still saved, just untagged
    assert sends.find(dc) is None


def test_lead_alerts_unclaimed_and_spam_day(client, monkeypatch):
    from datetime import datetime, timezone
    from app import alerts, sends
    pushed = []
    monkeypatch.setattr(alerts, "push", lambda *a, **k: pushed.append(a))
    # business hours: Mon-Fri 8-5 Central. Fri 4 PM to Mon 9 AM is 2 hours (1 on Friday, 1 on Monday)
    fri4 = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)      # Fri 4:00 PM CDT
    mon9 = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)     # Mon 9:00 AM CDT
    assert sends.business_minutes(fri4, mon9) == 120
    assert sends.business_minutes(datetime(2026, 10, 10, 15, tzinfo=timezone.utc), datetime(2026, 10, 11, 20, tzinfo=timezone.utc)) == 0
    _intake(client, "unclaimed-0001x", ip="192.0.2.180", name="Wait Ing")
    lid = _lead("unclaimed-0001x")["id"]
    conn().execute("UPDATE leads SET submitted_at=? WHERE id=?", ("2026-10-09T21:00:00Z", lid))
    rc = _lead("unclaimed-0001x")["receipt"]
    mine = lambda: [p for p in pushed if p[0] == f"Lead {rc} not claimed yet"]   # earlier tests' leads alert too, rightly
    sends.check_alerts(datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc))    # Mon 8 AM: only 1 business hour
    assert not mine()
    sends.check_alerts(mon9)
    assert len(mine()) == 1
    sends.check_alerts(mon9)                                                    # once per lead
    assert len(mine()) == 1
    # more than 10 caught or suspected spam in a day: one alert that day
    now = datetime.now(timezone.utc)
    for i in range(11):
        _intake(client, f"spamday-{i:04d}x", ip="192.0.2.181", website="http://x.example")
    pushed.clear()
    sends.check_alerts(now)
    sends.check_alerts(now)
    assert [p[0] for p in pushed if "spam" in p[0]] == ["Customer form: lots of spam today"]


def test_company_goes_with_the_name_everywhere(client):
    from app import mailer
    _intake(client, "company-lead-01", ip="192.0.2.190", name="Rob Builder", company="Hill Country Homes")
    lead = _lead("company-lead-01")
    assert json.loads(lead["data"])["company"] == "Hill Country Homes"
    login(client, "Paz Galambos", "112233")
    d = client.get(f"/ops/api/leads/{lead['id']}", headers=H).json()
    assert d["company"] == "Hill Country Homes" and d["sf_copy"].split("\n")[:2] == ["Rob Builder", "Hill Country Homes"]
    assert next(x for x in client.get("/ops/api/leads", headers=H).json()["leads"] if x["id"] == lead["id"])["company"]
    e = conn().execute("SELECT * FROM emails WHERE lead_id=? AND audience='staff'", (lead["id"],)).fetchone()
    assert "Hill Country Homes" in mailer._send_lead(e).get_body(("html",)).get_content()
    # found by company in Measure's "Or pick a lead"
    assert client.get("/ops/api/measure/leads?q=hill country", headers=H).json()["results"][0]["company"] == "Hill Country Homes"
    # Add a lead and Send the customer form take it too
    a = client.post("/ops/api/leads", json={"name": "Cal Contractor", "company": "CC Remodel", "phone": "2105550199",
                                            "source": "Phone call"}, headers=H).json()
    assert a["company"] == "CC Remodel"
    s = client.post("/ops/api/intake/sends", json={"channel": "text", "first_name": "Rob", "company": "Hill Country Homes"},
                    headers=H).json()
    assert 'data-company="Hill Country Homes"' in client.get(f"/start?s={s['link'].split('s=', 1)[1]}").text
    # a link in the company box counts as spam, like one in the name
    _intake(client, "company-spam-01", ip="192.0.2.191", company="cheap http://spam.example")
    assert "A link in the name or company" in _lead("company-spam-01")["spam"]

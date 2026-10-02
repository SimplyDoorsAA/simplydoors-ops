"""Email queue. A report is saved first; its email is queued and retried until it goes out."""
import json
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

from . import alerts
from .db import audit, conn, now_iso
from . import measure as measure_mod
from .forms import FORMS, display_rows, email_rows
from .pdf import build_pdf, local_time

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") == "1"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "SimplyDoors Portal [DO NOT REPLY]")
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "noreply@simplydoors.com")
MAX_ATTEMPTS = 6
RETRY_SECONDS = [0, 60, 300, 900, 1800, 3600]  # wait before attempt n+1

_wake = threading.Event()


def configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def queue_report_email(report_id: int, recipients: list[str], subject: str, bcc: list[str] = ()) -> None:
    if not recipients and not bcc:
        audit(None, "system", "email_skipped_no_recipients", f"report:{report_id}")
        return
    conn().execute("INSERT INTO emails(report_id, recipients, subject, created_at, next_try_at, bcc) VALUES (?,?,?,?,?,?)",
                   (report_id, ", ".join(recipients), subject, now_iso(), now_iso(), ", ".join(bcc)))
    _wake.set()


def _report_bundle(report_id: int):
    c = conn()
    r = c.execute("SELECT r.*, s.name AS staff_name FROM reports r JOIN staff s ON s.id=r.staff_id WHERE r.id=?",
                  (report_id,)).fetchone()
    photos = c.execute("SELECT * FROM photos WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
    return r, json.loads(r["data"]), photos


def _body_html(r, data) -> str:
    rows = "".join(
        f"<tr><td style='padding:8px 12px;border:1px solid #e0e0e0;background:#f2f9eb;font-weight:bold;width:40%'>{escape(a)}</td>"
        f"<td style='padding:8px 12px;border:1px solid #e0e0e0'>{escape(str(b))}</td></tr>"
        for a, b in ([("Received", local_time(r["submitted_at"]))] if data.get("measured_by") == r["staff_name"]
                     else [("Submitted by", r["staff_name"]), ("Received", local_time(r["submitted_at"]))])
        + (measure_mod.job_rows(data) if FORMS.get(r["form_type"], {}).get("kind") == "measure"
           else email_rows(r["form_type"], data)))
    confidential = bool(FORMS.get(r["form_type"], {}).get("confidential"))
    alarm = confidential or bool(data.get("defective")) or bool(data.get("attention"))
    head = "#b3261e" if alarm else "#2f6f1f"
    tag = (" — CONFIDENTIAL" if confidential else " — DEFECTIVE" if data.get("defective")
           else " — NEEDS FOLLOW-UP" if data.get("attention") else "")
    return f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #ddd;border-radius:8px;overflow:hidden">
<div style="background:#f8d7da;color:#721c24;padding:8px;text-align:center;font-size:12px;font-weight:bold">AUTOMATED MESSAGE — DO NOT REPLY</div>
<div style="background:{head};color:#ffffff;padding:16px 20px"><h2 style="margin:0;color:#ffffff">{escape(r['form_type'])}{tag}</h2>
<div style="font-size:13px;color:#ffffff">Receipt {escape(r['receipt'])}</div></div>
<div style="padding:20px"><table style="border-collapse:collapse;width:100%;font-size:14px">{rows}</table>
<p style="font-size:12px;color:#666;margin-top:20px">The full report with every photo is attached as a PDF.</p></div></div>"""


def _send_one(email_row) -> None:
    r, data, photos = _report_bundle(email_row["report_id"])
    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    msg["To"] = email_row["recipients"] or "undisclosed-recipients:;"
    msg["Reply-To"] = MAIL_REPLY_TO
    msg["Subject"] = email_row["subject"]
    msg.set_content(f"{r['form_type']} {r['receipt']} from {r['staff_name']}. The full report is attached as a PDF.")
    msg.add_alternative(_body_html(r, data), subtype="html")
    pdf = build_pdf(r, r["staff_name"], data, photos)
    msg.add_attachment(pdf, maintype="application", subtype="pdf",
                       filename=f"{r['form_type'].replace(' ', '_')}_{r['receipt']}.pdf")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        s.ehlo()
        if SMTP_STARTTLS:
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        if SMTP_USER and SMTP_PASSWORD:
            s.login(SMTP_USER, SMTP_PASSWORD)
        bcc = [x.strip() for x in (email_row["bcc"] or "").split(",") if x.strip()]
        to = [x.strip() for x in email_row["recipients"].split(",") if x.strip()]
        s.send_message(msg, to_addrs=to + bcc)        # private copies go out without appearing in any header


def process_queue_once() -> None:
    c = conn()
    if not configured():
        return  # emails stay pending until sending is set up
    due = c.execute("SELECT * FROM emails WHERE status='pending' AND next_try_at<=? ORDER BY id",
                    (now_iso(),)).fetchall()
    for e in due:
        try:
            _send_one(e)
            c.execute("UPDATE emails SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                      (now_iso(), e["id"]))
            details = {"report_id": e["report_id"], "to": e["recipients"], "subject": e["subject"]}
            if e["bcc"]:
                details["private_copies"] = len([x for x in e["bcc"].split(",") if x.strip()])
            audit(None, "system", "email_sent", f"email:{e['id']}", details)
        except Exception as ex:  # noqa: BLE001
            attempts = e["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            wait = RETRY_SECONDS[min(attempts, len(RETRY_SECONDS) - 1)]
            next_try = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait))
            c.execute("UPDATE emails SET attempts=?, status=?, last_error=?, next_try_at=? WHERE id=?",
                      (attempts, status, str(ex)[:500], next_try, e["id"]))
            audit(None, "system", "email_failed", f"email:{e['id']}",
                  {"report_id": e["report_id"], "attempt": attempts, "error": str(ex)[:300]})
            if attempts == 3 or status == "failed":
                alerts.push("Ops app: email not sending",
                            f"Report email '{e['subject']}' failed {attempts}x: {str(ex)[:150]}", "high")


def resend(report_id: int, actor, ip=None, agent=None) -> None:
    from .forms import split_recipients, subject_for
    r, data, _ = _report_bundle(report_id)
    subject = subject_for(r["form_type"], data, r["staff_name"], r["receipt"]) + " (resent)"
    if r["is_test"]:   # test reports only ever go to the person who filed them (the owner)
        em = conn().execute("SELECT email FROM staff WHERE id=?", (r["staff_id"],)).fetchone()
        rcpts, bcc, subject = [e for e in [em and em["email"]] if e], [], ("TEST - " + subject)[:200]
    else:
        rcpts, bcc = split_recipients(r["form_type"], data)
    queue_report_email(report_id, rcpts, subject, bcc)
    audit(actor["id"], actor["name"], "email_resend_requested", f"report:{report_id}", {"to": rcpts}, ip, agent)


def worker() -> None:
    while True:
        try:
            process_queue_once()
        except Exception:  # noqa: BLE001
            pass
        _wake.wait(30)
        _wake.clear()


def start_worker() -> None:
    threading.Thread(target=worker, daemon=True, name="email-worker").start()

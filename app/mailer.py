"""Email queue. A report is saved first; its email is queued and retried until it goes out."""
import json
import os
import smtplib
import ssl
import threading
import time
import traceback
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

from . import alerts
from .db import audit, conn, now_iso
from . import measure as measure_mod
from .forms import FORMS, email_rows
from .pdf import build_customer_pdf, build_pdf, local_time

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") == "1"
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "SimplyDoors Portal [DO NOT REPLY]")
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "noreply@simplydoors.com")
# the customer's own copy: from "SimplyDoors", and a reply reaches the office instead of a no-reply box
CUSTOMER_FROM_NAME = os.environ.get("CUSTOMER_FROM_NAME", "SimplyDoors")
CUSTOMER_REPLY_TO = os.environ.get("CUSTOMER_REPLY_TO", "admin@simplydoors.com")
LEAD_ATTACH_MAX = 15 * 1024 * 1024   # a lead's photos/PDFs are attached when together they're under this
MAX_ATTEMPTS = 6
RETRY_SECONDS = [0, 60, 300, 900, 1800, 3600]  # wait before attempt n+1

_wake = threading.Event()


def configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def queue_report_email(report_id: int, recipients: list[str], subject: str, bcc: list[str] = (),
                       audience: str = "staff") -> None:
    if not recipients and not bcc:
        audit(None, "system", "email_skipped_no_recipients", f"report:{report_id}")
        return
    conn().execute("INSERT INTO emails(report_id, recipients, subject, created_at, next_try_at, bcc, audience)"
                   " VALUES (?,?,?,?,?,?,?)",
                   (report_id, ", ".join(recipients), subject, now_iso(), now_iso(), ", ".join(bcc), audience))
    _wake.set()


def queue_po_email(po_id: int, vendor_email: str, subject: str) -> None:
    """A purchase order to a vendor. admin@simplydoors.com always gets a visible copy (Cc)."""
    from .pricelist import ADMIN_COPY
    cc = ADMIN_COPY if ADMIN_COPY.lower() != vendor_email.lower() else ""
    conn().execute("INSERT INTO emails(report_id, po_id, recipients, cc, subject, created_at, next_try_at, audience)"
                   " VALUES (NULL,?,?,?,?,?,?,'vendor')",
                   (po_id, vendor_email, cc, subject, now_iso(), now_iso()))
    _wake.set()


def queue_lead_email(lead_id: int, recipients: list[str], subject: str, bcc: list[str] = (),
                     audience: str = "staff") -> None:
    """About a lead from the customer form: the office's email (staff) or the customer's receipt (customer)."""
    if not recipients and not bcc:
        audit(None, "system", "email_skipped_no_recipients", f"lead:{lead_id}")
        return
    conn().execute("INSERT INTO emails(report_id, lead_id, recipients, subject, created_at, next_try_at, bcc, audience)"
                   " VALUES (NULL,?,?,?,?,?,?,?)",
                   (lead_id, ", ".join(recipients), subject, now_iso(), now_iso(), ", ".join(bcc), audience))
    _wake.set()


def queue_send_email(send_id: int, to: str, subject: str) -> None:
    """"Send from SimplyDoors": the customer form, emailed to a customer with a Start your project button."""
    if not to:
        audit(None, "system", "email_skipped_no_recipients", f"send:{send_id}")
        return
    conn().execute("INSERT INTO emails(report_id, send_id, recipients, subject, created_at, next_try_at, audience)"
                   " VALUES (NULL,?,?,?,?,?,'invite')", (send_id, to, subject, now_iso(), now_iso()))
    _wake.set()


def _send_invite(email_row) -> EmailMessage:
    from .leads import INTAKE_URL, OFFICE_CITY, OFFICE_PHONE, OFFICE_STREET, OPS_URL, INTAKE_PATH
    from .forms import owner_email
    s = conn().execute("SELECT s.*, st.name AS staff_name, st.email AS staff_email FROM intake_sends s"
                       " JOIN staff st ON st.id=s.staff_id WHERE s.id=?", (email_row["send_id"],)).fetchone()
    if not s:
        raise RuntimeError("that send was removed")
    link = f"{(OPS_URL + INTAKE_PATH) if s['is_test'] else INTAKE_URL}?s={s['code']}"
    me, hi = s["staff_name"].split(" ")[0], f"Hi {s['first_name']}" if s["first_name"] else "Hi"
    msg = EmailMessage()
    msg["From"] = formataddr((CUSTOMER_FROM_NAME, SMTP_USER))
    msg["To"] = email_row["recipients"]
    # replies reach the office and the person who sent it (never the owner's own address, as with every customer email)
    reply = [CUSTOMER_REPLY_TO]
    se = (s["staff_email"] or "").strip()
    if se and se.lower() not in (CUSTOMER_REPLY_TO.lower(), owner_email().lower()):
        reply.append(se)
    msg["Reply-To"] = ", ".join(reply)
    msg["Subject"] = email_row["subject"]
    msg.set_content(f"{hi}, it's {me} from SimplyDoors. Here's the link to start your project: {link}\n\n"
                    f"SimplyDoors · {OFFICE_STREET}, {OFFICE_CITY} · {OFFICE_PHONE}")
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e3e7ea;border-radius:10px;overflow:hidden;color:#1f2a33">
<div style="background:#2f6f1f;color:#ffffff;padding:18px 22px"><h2 style="margin:0;color:#ffffff;font-size:20px">Start your project with SimplyDoors</h2></div>
<div style="padding:22px;font-size:15px;line-height:1.5">
<p style="margin:0 0 12px">{escape(hi)},</p>
<p style="margin:0">It's {escape(me)} from SimplyDoors. Tell us about your doors, windows or millwork and add a few photos.
We'll call you within 1 business day.</p>
<p style="margin:22px 0"><a href="{escape(link)}" style="display:inline-block;background:#2f6f1f;color:#ffffff;text-decoration:none;
font-weight:bold;font-size:17px;padding:14px 26px;border-radius:8px">Start your SimplyDoors project</a></p>
<p style="margin:0;font-size:13px;color:#5f6b76">Questions? Just reply to this email.</p>
<p style="margin:20px 0 0">Thank you,<br><b>{escape(s['staff_name'])}</b>, SimplyDoors<br>
<span style="color:#5f6b76;font-size:13px">{escape(OFFICE_STREET)}, {escape(OFFICE_CITY)} · {escape(OFFICE_PHONE)}</span></p></div></div>""",
                        subtype="html")
    return msg


def _lead_html(lead, data, files, link: str, attached: bool) -> str:
    from .leads import more_rows
    rows = [("Name", lead["name"])] + ([("Company", data["company"])] if data.get("company") else []) + [("Phone", lead["phone"] or "—"), ("Email", lead["email"] or "—"),
            ("Project address", lead["address"] or "—"), ("Project", ", ".join(data.get("types") or []) or "—"),
            ("About the project", data.get("description") or "—"), ("How they heard about us", data.get("heard") or "—")]
    rows += more_rows(data)
    if files:
        rows.append(("Photos and files", f"{len(files)} " + ("attached" if attached else "in the app (too big to attach)")))
    if data.get("files_not_saved"):
        rows.append(("Files not saved", f"{data['files_not_saved']} (the server was low on space)"))
    rows.append(("Received", local_time(lead["submitted_at"])))
    body = "".join(
        f"<tr><td style='padding:8px 12px;border:1px solid #e0e0e0;background:#f2f9eb;font-weight:bold;width:38%'>{escape(a)}</td>"
        f"<td style='padding:8px 12px;border:1px solid #e0e0e0;white-space:pre-line'>{escape(str(b))}</td></tr>" for a, b in rows)
    tag = " — TEST" if lead["is_test"] else ""
    return f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #ddd;border-radius:8px;overflow:hidden">
<div style="background:#f8d7da;color:#721c24;padding:8px;text-align:center;font-size:12px;font-weight:bold">AUTOMATED MESSAGE — DO NOT REPLY</div>
<div style="background:#2f6f1f;color:#ffffff;padding:16px 20px"><h2 style="margin:0;color:#ffffff">New lead{tag}</h2>
<div style="font-size:13px;color:#ffffff">Receipt {escape(lead['receipt'])} · from the customer form</div></div>
<div style="padding:20px"><p style="margin:0 0 18px"><a href="{escape(link)}" style="display:inline-block;background:#2f6f1f;color:#ffffff;
text-decoration:none;font-weight:bold;font-size:16px;padding:12px 22px;border-radius:8px">Open this lead</a></p>
<table style="border-collapse:collapse;width:100%;font-size:14px">{body}</table>
<p style="font-size:12px;color:#666;margin-top:20px">Claim it in the app so everyone knows who is calling.</p></div></div>"""


def _lead_customer_html(lead) -> str:
    """The customer's receipt: short and warm. It never repeats what they typed (a spammer can't use it to send
    their text to someone else's address); just the number, and when we'll call."""
    from .leads import OFFICE_CITY, OFFICE_PHONE, OFFICE_STREET, greeting_name
    first = greeting_name(lead["name"])
    return f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e3e7ea;border-radius:10px;overflow:hidden;color:#1f2a33">
<div style="background:#2f6f1f;color:#ffffff;padding:18px 22px"><h2 style="margin:0;color:#ffffff;font-size:20px">We got your project request</h2>
<div style="font-size:13px;color:#ffffff;opacity:.9">Your number: {escape(lead['receipt'])}</div></div>
<div style="padding:22px;font-size:15px;line-height:1.5">
<p style="margin:0 0 12px">Hi{(' ' + escape(first)) if first else ' there'},</p>
<p style="margin:0">Thank you for reaching out to SimplyDoors. <b>We'll call you within 1 business day.</b></p>
<p style="margin:16px 0 0">Want to add something, or send more photos? Just reply to this email.</p>
<p style="margin:20px 0 0">Thank you,<br><b>The SimplyDoors team</b><br>
<span style="color:#5f6b76;font-size:13px">{escape(OFFICE_STREET)}, {escape(OFFICE_CITY)} · {escape(OFFICE_PHONE)}</span></p></div></div>"""


def _send_lead(email_row) -> EmailMessage:
    from .leads import OFFICE_PHONE, OPS_URL, bundle
    got = bundle(email_row["lead_id"])
    if not got:
        raise RuntimeError("that lead was deleted")
    lead, data, files = got
    msg = EmailMessage()
    msg["To"] = email_row["recipients"] or "undisclosed-recipients:;"
    msg["Subject"] = email_row["subject"]
    if email_row["audience"] == "customer":
        msg["From"] = formataddr((CUSTOMER_FROM_NAME, SMTP_USER))
        msg["Reply-To"] = CUSTOMER_REPLY_TO
        msg.set_content(f"Thank you for reaching out to SimplyDoors. We got your project request {lead['receipt']}. "
                        f"We'll call you within 1 business day. Questions? Reply to this email or call {OFFICE_PHONE}.")
        msg.add_alternative(_lead_customer_html(lead), subtype="html")
        return msg
    link = f"{OPS_URL}/leads#lead={lead['id']}"
    attach = sum(f["bytes"] for f in files) <= LEAD_ATTACH_MAX and all(os.path.isfile(f["path"]) for f in files)
    msg["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    msg["Reply-To"] = MAIL_REPLY_TO
    msg.set_content(f"New lead {lead['receipt']}: {lead['name']}, {lead['phone'] or lead['email']}.\nOpen this lead: {link}")
    msg.add_alternative(_lead_html(lead, data, files, link, attach), subtype="html")
    if attach:
        for i, f in enumerate(files, 1):
            with open(f["path"], "rb") as fh:
                b = fh.read()
            if f["kind"] == "pdf":
                msg.add_attachment(b, maintype="application", subtype="pdf", filename=f"{lead['receipt']}_{i}.pdf")
            else:
                msg.add_attachment(b, maintype="image", subtype="jpeg", filename=f"{lead['receipt']}_{i}.jpg")
    return msg


def _size_html(ln: dict) -> str:
    return f"<br><span style='color:#666'>{escape(ln['size'])}</span>" if ln.get("size") else ""


def _po_html(po: dict) -> str:
    rows = "".join(
        f"<tr><td style='padding:6px 10px;border:1px solid #e0e0e0'>{ln['qty']}</td>"
        f"<td style='padding:6px 10px;border:1px solid #e0e0e0;font-family:monospace'>{escape(ln['sku'])}</td>"
        f"<td style='padding:6px 10px;border:1px solid #e0e0e0'>{escape(ln['name'])}"
        f"{_size_html(ln)}</td></tr>"
        for ln in po["lines"])
    notes = f"<p style='margin:14px 0 0'><b>Notes:</b> {escape(po['notes'])}</p>" if po.get("notes") else ""
    return f"""<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;border:1px solid #ddd;border-radius:8px;overflow:hidden;color:#1f2933">
<div style="background:#2f5d50;color:#ffffff;padding:16px 20px"><h2 style="margin:0;color:#ffffff">Purchase Order {escape(po['po_number'])}</h2>
<div style="font-size:13px;color:#ffffff">SimplyDoors · {escape(po['order_date'])} · {escape(po['ship_method'])}</div></div>
<div style="padding:20px;font-size:14px"><p style="margin:0 0 12px">Please process the attached purchase order and reference
PO # <b>{escape(po['po_number'])}</b> on the invoice and packing slip.</p>
<table style="border-collapse:collapse;width:100%;font-size:14px"><tr style="background:#f2f9eb"><th style="padding:6px 10px;border:1px solid #e0e0e0;text-align:left">Qty</th>
<th style="padding:6px 10px;border:1px solid #e0e0e0;text-align:left">Part #</th><th style="padding:6px 10px;border:1px solid #e0e0e0;text-align:left">Description</th></tr>{rows}</table>{notes}
<p style="font-size:12px;color:#666;margin-top:18px">The full PO with prices is attached as a PDF. Questions? Reply to this email.</p></div></div>"""


def _send_po(email_row) -> EmailMessage:
    from .pdf import build_po_pdf
    from .pricelist import get_po, po_out, po_vendor, safe_name
    r = get_po(email_row["po_id"])
    po = po_out(r, with_lines=True)
    vendor = po_vendor(po)
    msg = EmailMessage()
    msg["From"] = formataddr((CUSTOMER_FROM_NAME, SMTP_USER))
    msg["To"] = email_row["recipients"]
    if email_row["cc"]:
        msg["Cc"] = email_row["cc"]
    msg["Reply-To"] = CUSTOMER_REPLY_TO
    msg["Subject"] = email_row["subject"]
    msg.set_content(f"SimplyDoors purchase order {po['po_number']} is attached as a PDF. "
                    f"Please reference PO # {po['po_number']} on the invoice and packing slip.")
    msg.add_alternative(_po_html(po), subtype="html")
    msg.add_attachment(build_po_pdf(po, vendor), maintype="application", subtype="pdf",
                       filename=f"SimplyDoors_PO_{safe_name(po['po_number'])}.pdf")
    return msg


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


def _customer_html(r, data) -> str:
    """The customer's email: short, warm, no internal details. The PDF carries the full record."""
    name = str(data.get("signer") or data.get("customer") or "").strip().split(" ")[0]
    done = data.get("work") == "Yes"
    msg = ("Your installation is complete. Thank you for choosing SimplyDoors!" if done else
           "Thank you for choosing SimplyDoors! Most of your installation is done, and we'll be in touch to schedule "
           "what's left.")
    punch = "" if done or not data.get("punch_items") else (
        "<p style='margin:16px 0 4px;font-weight:bold'>Still to finish</p>"
        f"<p style='margin:0;white-space:pre-line'>{escape(str(data['punch_items']))}</p>")
    job = f"Job {escape(str(data['po']))} · " if data.get("po") else ""
    return f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e3e7ea;border-radius:10px;overflow:hidden;color:#1f2a33">
<div style="background:#2f6f1f;color:#ffffff;padding:18px 22px"><h2 style="margin:0;color:#ffffff;font-size:20px">Your installation record</h2>
<div style="font-size:13px;color:#ffffff;opacity:.9">{job}{escape(local_time(r['submitted_at']))}</div></div>
<div style="padding:22px;font-size:15px;line-height:1.5">
<p style="margin:0 0 12px">Hi{(' ' + escape(name)) if name else ''},</p>
<p style="margin:0">{msg}</p>{punch}
<p style="margin:16px 0 0">Your signed installation record, with photos of the finished work, is attached as a PDF for your files.</p>
<p style="margin:16px 0 0">Questions about your installation? Just reply to this email and our office will get back to you.</p>
<p style="margin:20px 0 0">Thank you,<br><b>The SimplyDoors team</b></p></div></div>"""


def customer_reply_to(data) -> list[str]:
    """A customer's reply always reaches the office, plus the sales rep picked on the form.
    The owner's own address is never shown to a customer (same rule as the staff emails)."""
    from .forms import owner_email
    out = [CUSTOMER_REPLY_TO]
    rep, owner = str(data.get("sales_notify_email") or "").strip(), owner_email().lower()
    if rep and rep.lower() != CUSTOMER_REPLY_TO.lower() and rep.lower() != owner:
        out.append(rep)
    return out


def _send_customer(email_row, r, data, photos) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((CUSTOMER_FROM_NAME, SMTP_USER))
    msg["To"] = email_row["recipients"]
    msg["Reply-To"] = ", ".join(customer_reply_to(data))
    msg["Subject"] = email_row["subject"]
    msg.set_content("Thank you for choosing SimplyDoors. Your signed installation record is attached as a PDF. "
                    "Questions? Just reply to this email.")
    msg.add_alternative(_customer_html(r, data), subtype="html")
    po = "".join(ch for ch in str(data.get("po") or r["receipt"]) if ch.isalnum() or ch == "-")
    msg.add_attachment(build_customer_pdf(r, data, photos), maintype="application", subtype="pdf",
                       filename=f"SimplyDoors_Installation_{po}.pdf")
    return msg


def _send_one(email_row) -> None:
    if "po_id" in email_row.keys() and email_row["po_id"]:
        _smtp_send(_send_po(email_row), email_row)
        return
    if "lead_id" in email_row.keys() and email_row["lead_id"]:
        _smtp_send(_send_lead(email_row), email_row)
        return
    if "send_id" in email_row.keys() and email_row["send_id"]:
        _smtp_send(_send_invite(email_row), email_row)
        return
    r, data, photos = _report_bundle(email_row["report_id"])
    if (email_row["audience"] if "audience" in email_row.keys() else "staff") == "customer":
        _smtp_send(_send_customer(email_row, r, data, photos), email_row)
        return
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
    _smtp_send(msg, email_row)


def _smtp_send(msg, email_row) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        s.ehlo()
        if SMTP_STARTTLS:
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        if SMTP_USER and SMTP_PASSWORD:
            s.login(SMTP_USER, SMTP_PASSWORD)
        bcc = [x.strip() for x in (email_row["bcc"] or "").split(",") if x.strip()]
        cc = [x.strip() for x in ((email_row["cc"] if "cc" in email_row.keys() else "") or "").split(",") if x.strip()]
        to = [x.strip() for x in email_row["recipients"].split(",") if x.strip()]
        s.send_message(msg, to_addrs=to + cc + bcc)        # private copies go out without appearing in any header


def process_queue_once() -> None:
    c = conn()
    if not configured():
        return  # emails stay pending until sending is set up
    due = c.execute("SELECT * FROM emails WHERE status='pending' AND next_try_at<=? ORDER BY id",
                    (now_iso(),)).fetchall()
    for e in due:
        try:
            _send_one(e)
        except Exception as ex:  # noqa: BLE001
            _send_failed(c, e, ex)
            continue
        # it went out: from here on nothing may put it back in the queue, or everyone gets it twice
        c.execute("UPDATE emails SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                  (now_iso(), e["id"]))
        try:
            details = {"report_id": e["report_id"], "to": e["recipients"], "subject": e["subject"]}
            if e["po_id"]:
                details = {"po_id": e["po_id"], "to": e["recipients"], "cc": e["cc"], "subject": e["subject"]}
            elif e["lead_id"]:
                details = {"lead_id": e["lead_id"], "to": e["recipients"], "subject": e["subject"]}
            elif e["send_id"]:
                details = {"send_id": e["send_id"], "to": e["recipients"], "subject": e["subject"]}
            if e["bcc"]:
                details["private_copies"] = len([x for x in e["bcc"].split(",") if x.strip()])
            audit(None, "system", "email_sent", f"email:{e['id']}", details)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _send_failed(c, e, ex) -> None:
    attempts = e["attempts"] + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    wait = RETRY_SECONDS[min(attempts, len(RETRY_SECONDS) - 1)]
    next_try = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait))
    c.execute("UPDATE emails SET attempts=?, status=?, last_error=?, next_try_at=? WHERE id=?",
              (attempts, status, str(ex)[:500], next_try, e["id"]))
    audit(None, "system", "email_failed", f"email:{e['id']}",
          {"lead_id": e["lead_id"], "attempt": attempts, "error": str(ex)[:300]} if e["lead_id"] else
          {"report_id": e["report_id"], "attempt": attempts, "error": str(ex)[:300]})
    if attempts == 3 or status == "failed":
        kind = "PO" if e["po_id"] else "Lead" if e["lead_id"] else "Customer form" if e["send_id"] else "Report"
        alerts.push("Ops app: email not sending",
                    f"{kind} email '{e['subject']}' failed {attempts}x: {str(ex)[:150]}", "high")


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

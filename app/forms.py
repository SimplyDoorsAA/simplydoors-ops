"""Form definitions. Stage 1 has the Receiving Report; Stage 2 adds the rest here.

Each form says which fields it has, which are required, how they show in the
PDF/email, and which extra people get emailed.
"""
from .db import conn

RECEIVING_LOCATIONS = ["Location A", "Location B", "Location C", "Location D"]

FORMS = {
    "Receiving Report": {
        "slug": "receiving",
        "prefix": "RCV",
        "fields": [
            # key, label, required
            ("po", "Job / PO Number", True),
            ("customer", "Customer", True),
            ("location", "Receiving Location", True),
            ("sales_notify_name", "Sales Rep Notified", False),
            ("sop_unloaded", "Safely unloaded the goods", True),
            ("sop_inspected", "Inspected for damage, counts and quality", True),
            ("sop_entered", "Entered items into the system (or will right away)", True),
            ("remarks", "Remarks / Notes", False),
        ],
        "checkboxes": ["sop_unloaded", "sop_inspected", "sop_entered"],
        "photos": [
            ("ticket1", "Receiving ticket 1"), ("ticket2", "Receiving ticket 2"), ("ticket3", "Receiving ticket 3"),
            ("product1", "Product photo 1"), ("product2", "Product photo 2"),
        ],
    },
}

FORM_BY_SLUG = {v["slug"]: k for k, v in FORMS.items()}

MAX_TEXT = 4000


def clean(form_type: str, raw: dict) -> tuple[dict, list[str]]:
    """Validate submitted fields. Returns (data, errors)."""
    spec = FORMS[form_type]
    data, errors = {}, []
    if form_type == "Receiving Report":
        # The sales rep is picked by staff id; store name + email at submit time.
        sid = (raw.get("sales_notify") or "").strip()
        data["sales_notify_name"], data["sales_notify_email"] = "Not requested", ""
        if sid and sid != "none":
            row = conn().execute("SELECT name, email FROM staff WHERE id=? AND sales_notify=1 AND active=1",
                                 (sid,)).fetchone()
            if not row:
                errors.append("Pick a sales rep from the list.")
            else:
                data["sales_notify_name"], data["sales_notify_email"] = row["name"], row["email"]
        loc = (raw.get("location") or "").strip()
        if loc == "Custom":
            loc = (raw.get("location_custom") or "").strip()
        raw = dict(raw, location=loc)

    for key, label, required in spec["fields"]:
        if key == "sales_notify_name":
            continue
        if key in spec.get("checkboxes", []):
            val = str(raw.get(key, "")).lower() in ("1", "true", "on", "yes")
            if required and not val:
                errors.append(f"Check the box: {label}.")
            data[key] = val
            continue
        val = (raw.get(key) or "").strip()[:MAX_TEXT]
        if required and not val:
            errors.append(f"{label} is required.")
        data[key] = val
    return data, errors


def display_rows(form_type: str, data: dict) -> list[tuple[str, str]]:
    rows = []
    for key, label, _ in FORMS[form_type]["fields"]:
        v = data.get(key, "")
        if isinstance(v, bool):
            v = "Yes" if v else "No"
        rows.append((label, v if v not in ("", None) else "—"))
    return rows


def subject_for(form_type: str, data: dict, staff_name: str, receipt: str) -> str:
    if form_type == "Receiving Report":
        s = f"Receiving Report: {data.get('po')} - {data.get('customer')} [{receipt}]"
    else:
        s = f"{form_type}: {staff_name} [{receipt}]"
    return " ".join(s.split())[:200]


def recipients_for(form_type: str, data: dict) -> list[str]:
    row = conn().execute("SELECT recipients FROM email_rules WHERE form_type=?", (form_type,)).fetchone()
    rcpts = [r.strip() for r in (row["recipients"] if row else "").split(",") if r.strip()]
    if form_type == "Receiving Report" and data.get("sales_notify_email"):
        rcpts.append(data["sales_notify_email"])
    seen, out = set(), []
    for r in rcpts:
        if r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out

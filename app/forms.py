"""Form definitions: one place describes every form.

The server validates against these, the phone draws the form from them, and the
PDF/email are built from them. Adding a form = adding an entry to FORMS.

Field types
  text, textarea, number, date, time
  select   options from a list name ("locations", "vehicles"), a fixed list, or
           people ("sales_reps", "staff"); allow_other adds "Something else (type it)"
  choice   big tap buttons, fixed options (e.g. Yes / No)
  checks   boxes that must all be ticked
  okdef    inspection grid: every item OK or Defective
Fields can have show_if={"field": key, "in": [values]}: hidden and not required otherwise.
"""
import json
import re

from . import measure as M
from .db import conn, get_setting, set_setting

DEFAULT_LISTS = {
    "locations": ["Location A", "Location B", "Location C", "Location D"],
    "vehicles": ["Sprinter Van", "Small Truck", "Big Truck"],
    "vendors": ["Hoelscher"],
}
LIST_LABELS = {"locations": "Receiving locations", "vehicles": "Vehicles", "vendors": "Vendors (RMA form)"}

INSPECTION_ITEMS = {
    "Engine / Fluids": ["Fuel Level", "Oil Level", "Transmission Fluid", "Coolant", "Leaks Under Vehicle"],
    "Exterior": ["Headlights / Taillights", "Turn Signals", "Tire Condition", "Mirrors", "Body Damage"],
    "Interior": ["Brakes / Parking Brake", "Steering", "Horn", "Wipers", "Seatbelts"],
}

EOS_CHECKS = {
    "Warehouse": [
        "All units and slabs needed for next-day delivery/pickups are completed.",
        "All deliveries received today are entered in the system, tagged, and put up in the racks.",
        "Warehouse is cleaned and swept.",
        "Extra materials are put away. Floor and walls are cleared of leaning trim, doors, and boxes.",
    ],
    "Driver": [
        "Load has been double-checked against all paperwork.",
        "Items are tied securely with cardboard corners to prevent product damage.",
        "Box truck is cleaned out and swept.",
    ],
    "Production": [
        "Miter saw and table saw are cleared of leftover materials.",
        "All tools have been returned to their designated spots.",
        "Any tools or materials that are damaged or broken have been reported to the supervisor ASAP.",
        "Maintenance needs on the Maverick or any other tools have been reported to the supervisor to get fixed.",
        "Production area is cleaned, swept, and secured for the end of the shift.",
    ],
}

YES_NO = ["No", "Yes"]

INSTALL_CHECKS = [
    ("operates", "Door opens, closes and latches smoothly (no rubbing)"),
    ("hardware", "Lockset and deadbolt installed and working"),
    ("trim", "Exterior and interior trim installed and caulked"),
    ("cleanup", "Work area cleaned and old material hauled off"),
]
RMA_VENDOR, RMA_CUSTOMER = "Return to vendor", "Return from customer"
_V = {"field": "direction", "in": [RMA_VENDOR]}
_C = {"field": "direction", "in": [RMA_CUSTOMER]}
RMA_RETURN_TEXT = "By signing, the customer confirms the items listed above were returned to SimplyDoors."

ACCEPT_TEXT = ("By signing, the customer confirms the work listed above was completed and accepts the installation, "
               "except for anything listed on the punch list.")

FORMS = {
    "Receiving Report": {
        "slug": "receiving", "prefix": "RCV", "order": 1,
        "blurb": "Log a delivery coming in: tickets, product photos, where it went.",
        "fields": [
            {"key": "po", "label": "Job / PO Number", "type": "text", "required": True},
            {"key": "customer", "label": "Customer", "type": "text", "required": True},
            {"key": "location", "label": "Receiving Location", "ask": "Where did you put it?", "type": "select",
             "options": "locations", "allow_other": True, "required": True},
            {"key": "sales_notify", "label": "Sales Rep Notified", "ask": "Notify a sales rep (optional)",
             "type": "select", "options": "sales_reps", "none_label": "Don't notify anyone", "notify": True},
            {"key": "sop", "label": "Checklist", "ask": "Check each one", "type": "checks", "required": True,
             "items": [("sop_unloaded", "I safely unloaded the goods from the truck."),
                       ("sop_inspected", "I checked for damage, counted everything and checked quality."),
                       ("sop_entered", "I entered the items into the system (or will right away).")]},
            {"key": "remarks", "label": "Remarks / Notes", "type": "textarea",
             "placeholder": "Missing items, crushed boxes, anything unusual…"},
        ],
        "photos": [
            {"group": "ticket", "title": "Receiving tickets", "help": "Bill of lading, packing slips or delivery tickets.",
             "slots": [("ticket1", "Ticket 1"), ("ticket2", "Ticket 2"), ("ticket3", "Ticket 3")]},
            {"group": "product", "title": "Product condition", "help": "Photos of the product as it arrived.",
             "slots": [("product1", "Product 1"), ("product2", "Product 2")]},
        ],
        "summary": ["po", "customer"],
    },
    "Delivery Proof": {
        "slug": "delivery", "prefix": "DLV", "order": 2,
        "blurb": "Prove a delivery to a customer: photos at the site, who received it, signature.",
        "fields": [
            {"key": "po", "label": "Job / PO Number", "type": "text", "required": True},
            {"key": "customer", "label": "Customer", "type": "text", "required": True},
            {"key": "address", "label": "Delivery address", "type": "text", "required": True,
             "placeholder": "Street, city"},
            {"key": "vehicle", "label": "Vehicle", "type": "select", "options": "vehicles", "allow_other": True},
            {"key": "received_by", "label": "Received by (name)", "type": "text",
             "placeholder": "Leave empty if nobody was there"},
            {"key": "condition", "label": "Delivered in good condition?", "type": "choice",
             "options": ["Yes", "No, see notes"], "required": True},
            {"key": "sales_notify", "label": "Sales Rep Notified", "ask": "Notify a sales rep (optional)",
             "type": "select", "options": "sales_reps", "none_label": "Don't notify anyone", "notify": True},
            {"key": "remarks", "label": "Notes", "type": "textarea",
             "placeholder": "Where it was left, damage, anything the customer said…"},
        ],
        "photos": [
            {"group": "site", "title": "Photos at the site", "help": "Product where you left it, plus the house or address.",
             "slots": [("site1", "Photo 1"), ("site2", "Photo 2"), ("site3", "Photo 3")], "min": 1},
            {"group": "signature", "title": "Customer signature (optional)", "signature": "sig",
             "help": "Hand the phone to the customer to sign with their finger."},
        ],
        "summary": ["po", "customer"],
    },
    "Installation Completion": {
        "slug": "install", "prefix": "INS", "order": 3,
        "blurb": "Close out an install: checklist, before/after photos, notes and the customer's sign-off.",
        "fields": [
            {"key": "po", "label": "Job / PO Number", "type": "text", "required": True},
            {"key": "customer", "label": "Customer", "type": "text", "required": True},
            {"key": "address", "label": "Job address", "type": "text", "placeholder": "Street, city"},
            {"key": "work", "label": "Work completed", "type": "textarea", "required": True,
             "placeholder": "e.g. 1 front entry door with 2 sidelites, new storm door"},
            {"key": "crew", "label": "Install crew", "type": "text", "placeholder": "Who did the install"},
            {"key": "checklist", "label": "Completion checklist", "type": "donena", "required": True,
             "items": INSTALL_CHECKS},
            {"key": "punch", "label": "Punch list", "ask": "Anything left to finish or come back for?", "type": "choice",
             "options": ["No, all done", "Yes"], "required": True},
            {"key": "punch_items", "label": "What's left", "type": "textarea", "required": True,
             "show_if": {"field": "punch", "in": ["Yes"]}, "placeholder": "Parts on order, touch-up paint, return visit…"},
            {"key": "sales_notify", "label": "Sales Rep Notified", "ask": "Notify a sales rep (optional)",
             "type": "select", "options": "sales_reps", "none_label": "Don't notify anyone", "notify": True},
            {"key": "notes", "label": "Notes", "type": "textarea", "placeholder": "Anything the office should know"},
            {"key": "cust_comments", "label": "Customer comments or concerns", "type": "textarea", "tail": True,
             "placeholder": "Anything the customer said about the job, good or bad"},
            # customer acceptance: shown after the photos, right above the signature
            {"key": "cust_present", "label": "Customer present to sign", "ask": "Is the customer here to sign off?",
             "type": "choice", "options": ["Yes", "No"], "required": True, "tail": True},
            {"key": "signer", "label": "Signed by (print name)", "type": "text", "required": True, "tail": True,
             "show_if": {"field": "cust_present", "in": ["Yes"]}},
            {"key": "no_sign_reason", "label": "Why no signature", "type": "textarea", "required": True, "tail": True,
             "show_if": {"field": "cust_present", "in": ["No"]}, "placeholder": "e.g. customer not home, left with contractor"},
        ],
        "photos": [
            {"group": "before", "title": "Before photos (optional)", "help": "The opening before you started.",
             "slots": [("before1", "Before 1"), ("before2", "Before 2")]},
            {"group": "after", "title": "Finished install", "help": "Outside, inside, and the lock / hardware.",
             "slots": [("after1", "Outside"), ("after2", "Inside"), ("after3", "Lock / hardware"), ("after4", "Extra")], "min": 2},
            {"group": "signature", "title": "Customer signature", "signature": "sig", "required": True,
             "show_if": {"field": "cust_present", "in": ["Yes"]},
             "help": ACCEPT_TEXT},
        ],
        "photo_grid": True,
        "email_keys": ["po", "customer", "address", "work", "crew", "punch", "punch_items", "cust_comments", "signer", "no_sign_reason"],
        "summary": ["po", "customer"],
    },
    "RMA": {
        "slug": "rma", "prefix": "RMA", "order": 3,
        "blurb": "Product going back to a vendor or coming back from a customer: what, why, photos, RMA #.",
        "fields": [
            {"key": "direction", "label": "Return type", "ask": "Which way is it going?", "type": "choice",
             "options": [RMA_VENDOR, RMA_CUSTOMER], "required": True},
            {"key": "po", "label": "Job / PO Number", "type": "text", "required": True},
            # vendor return
            {"key": "vendor", "label": "Vendor", "type": "select", "options": "vendors", "allow_other": True,
             "required": True, "show_if": _V},
            {"key": "vendor_order", "label": "Vendor order / invoice #", "type": "text", "show_if": _V},
            {"key": "vendor_rma", "label": "Vendor RMA / authorization #", "type": "text", "show_if": _V,
             "placeholder": "Leave empty if they haven't given one yet"},
            {"key": "job_customer", "label": "Customer / job (if any)", "type": "text", "show_if": _V},
            # customer return
            {"key": "customer", "label": "Customer", "type": "text", "required": True, "show_if": _C},
            # both
            {"key": "items", "label": "Item(s) being returned", "type": "textarea", "required": True,
             "placeholder": "Qty, description, size, handing, color…"},
            {"key": "reason_v", "label": "Reason", "type": "select", "required": True, "allow_other": True, "show_if": _V,
             "options": ["Damaged in shipping", "Defective / manufacturer issue", "Wrong size", "Wrong handing / swing",
                         "Wrong item shipped", "Ordered wrong (our mistake)", "Not needed / extra"]},
            {"key": "want", "label": "What we want", "type": "choice", "show_if": _V, "required": True,
             "options": ["Credit", "Replacement", "Repair", "Not decided yet"]},
            {"key": "reason_c", "label": "Reason", "type": "select", "required": True, "allow_other": True, "show_if": _C,
             "options": ["Changed mind", "Wrong item / size ordered", "Damaged", "Defective", "Extra / not needed"]},
            {"key": "condition", "label": "Condition", "type": "choice", "show_if": _C, "required": True,
             "options": ["Unused, in box", "Opened, not installed", "Installed / used", "Damaged"]},
            {"key": "resolution", "label": "Resolution", "type": "choice", "show_if": _C, "required": True,
             "options": ["Refund", "Store credit", "Exchange", "Not decided yet"]},
            {"key": "restock", "label": "Restocking fee", "type": "choice", "show_if": _C, "required": True,
             "options": ["No", "Yes"]},
            {"key": "restock_amt", "label": "Restocking fee amount", "type": "text", "required": True,
             "show_if": {"field": "restock", "in": ["Yes"]}, "placeholder": "e.g. 15% or $75"},
            {"key": "location", "label": "Where is it now", "type": "select", "options": "locations", "allow_other": True},
            {"key": "sales_notify", "label": "Sales Rep Notified", "ask": "Notify a sales rep (optional)",
             "type": "select", "options": "sales_reps", "none_label": "Don't notify anyone", "notify": True},
            {"key": "notes", "label": "Notes", "type": "textarea",
             "placeholder": "Who you talked to, pickup date, anything else"},
            {"key": "signer", "label": "Signed by (print name)", "type": "text", "tail": True, "show_if": _C,
             "placeholder": "Leave empty if the customer isn't here"},
        ],
        "photos": [
            {"group": "product", "title": "Product / damage", "help": "The item and any damage, close up.",
             "slots": [("product1", "Photo 1"), ("product2", "Photo 2"), ("product3", "Photo 3")], "min": 1},
            {"group": "labels", "title": "Labels & paperwork (optional)",
             "help": "Product label or sticker, packing slip, the vendor's RMA paperwork.",
             "slots": [("label1", "Label / slip 1"), ("label2", "Label / slip 2")]},
            {"group": "signature", "title": "Customer signature (optional)", "signature": "sig",
             "show_if": _C, "help": RMA_RETURN_TEXT},
        ],
        "photo_grid": True,
        "email_keys": ["direction", "po", "vendor", "vendor_rma", "job_customer", "customer", "items", "reason_v",
                       "want", "reason_c", "resolution", "restock_amt", "location"],
        "summary": ["po", "vendor", "customer"],
    },
    "End of Shift": {
        "slug": "eos", "prefix": "EOS", "order": 3,
        "blurb": "Close out your shift: checklist and clean-area photos.",
        "fields": [
            {"key": "role", "label": "Role", "ask": "Your position today", "type": "choice",
             "options": ["Warehouse", "Driver", "Production"], "required": True},
        ] + [
            {"key": f"checks_{r.lower()}", "label": f"{r} checklist", "ask": f"{r} checklist", "type": "checks",
             "required": True, "show_if": {"field": "role", "in": [r]},
             "items": [(f"{r.lower()}_{i + 1}", t) for i, t in enumerate(items)]}
            for r, items in EOS_CHECKS.items()
        ] + [
            {"key": "remarks", "label": "Remarks / Maintenance & Broken Tools", "type": "textarea",
             "placeholder": "Exceptions, items left out, broken tools, maintenance needs…"},
        ],
        "photos": [
            {"group": "area", "title": "Photo check", "help": "Wide photos of the clean area (warehouse floor, truck bed and load, or production area).",
             "slots": [("area1", "Photo 1"), ("area2", "Photo 2")], "min": 2},
        ],
        "summary": ["role"],
    },
    "Vehicle Inspection": {
        "slug": "inspection", "prefix": "VIN", "order": 4,
        "blurb": "Pre-trip or post-trip check of a vehicle.",
        "fields": [
            {"key": "trip", "label": "Inspection", "type": "choice", "options": ["Pre-Trip", "Post-Trip"], "required": True},
            {"key": "vehicle", "label": "Vehicle", "type": "select", "options": "vehicles", "allow_other": True, "required": True},
            {"key": "odometer", "label": "Odometer", "type": "number", "required": True, "max": 2_000_000},
            {"key": "items", "label": "Inspection", "type": "okdef", "required": True, "groups": INSPECTION_ITEMS},
            {"key": "remarks", "label": "Remarks / Problem Report", "type": "textarea",
             "help": "Required if anything is Defective.", "required_if_defective": True},
        ],
        "photos": [
            {"group": "vehicle", "title": "Photos (up to 3)", "help": "The vehicle, or close-ups of any damage.",
             "slots": [("veh1", "Photo 1"), ("veh2", "Photo 2"), ("veh3", "Photo 3")]},
        ],
        "summary": ["vehicle", "trip"],
    },
    "Vehicle Incident": {
        "slug": "vincident", "prefix": "VIC", "order": 5,
        "blurb": "Report vehicle damage, a collision, or a roadside emergency.",
        "fields": [
            {"key": "vehicle", "label": "Vehicle involved", "type": "select", "options": "vehicles", "allow_other": True, "required": True},
            {"key": "date", "label": "Date of incident", "type": "date", "required": True, "default": "today"},
            {"key": "time", "label": "Time of incident", "type": "time", "required": True},
            {"key": "location", "label": "Exact location", "type": "text", "required": True,
             "placeholder": "Street / intersection, city"},
            {"key": "police", "label": "Police report filed?", "type": "choice", "options": YES_NO, "required": True},
            {"key": "other_vehicle", "label": "Other vehicle or property involved?", "type": "choice", "options": YES_NO, "required": True},
            {"key": "description", "label": "What happened / damage", "type": "textarea", "required": True},
        ],
        "photos": [
            {"group": "damage", "title": "Photos of damage (up to 3)", "help": "Wide shots of the vehicle and close-ups of the damage.",
             "slots": [("dmg1", "Photo 1"), ("dmg2", "Photo 2"), ("dmg3", "Photo 3")]},
        ],
        "summary": ["vehicle", "date"],
    },
    "Employee Incident": {
        "slug": "incident", "prefix": "INC", "order": 6,
        "blurb": "Report a workplace accident, injury or safety concern.",
        "fields": [
            {"key": "department", "label": "Department", "type": "choice", "options": ["Warehouse", "Delivery", "Production", "Sales", "Office"], "required": True},
            {"key": "supervisor", "label": "Supervisor", "type": "text", "required": True},
            {"key": "date", "label": "Date", "type": "date", "required": True, "default": "today"},
            {"key": "time", "label": "Time", "type": "time", "required": True},
            {"key": "location", "label": "Location", "type": "text", "required": True},
            {"key": "description", "label": "What happened", "type": "textarea", "required": True},
            {"key": "witnesses", "label": "Witnesses", "type": "text", "placeholder": "Names, separated by commas"},
            {"key": "action", "label": "Immediate action taken", "type": "textarea", "required": True},
            {"key": "root_cause", "label": "Why it happened (root cause)", "type": "textarea", "required": True},
            {"key": "prevent", "label": "How to stop it happening again", "type": "textarea", "required": True},
        ],
        "photos": [
            {"group": "scene", "title": "Photos (up to 3)", "help": "The hazard or the area where it happened.",
             "slots": [("scene1", "Photo 1"), ("scene2", "Photo 2"), ("scene3", "Photo 3")]},
        ],
        "summary": ["department", "date"],
    },
    "Disciplinary Action": {
        "slug": "disciplinary", "prefix": "DSC", "order": 7, "admin_only": True, "confidential": True,
        "blurb": "Document a formal warning (admins only).",
        "fields": [
            {"key": "target", "label": "Employee", "ask": "Employee being written up", "type": "select",
             "options": "staff", "required": True, "target": True},
            {"key": "level", "label": "Warning level", "type": "select", "required": True,
             "options": ["Verbal Warning (Documented)", "First Written Warning", "Final Written Warning", "Suspension"]},
            {"key": "infraction", "label": "Nature of infraction", "type": "select", "required": True,
             "options": ["Tardiness / Absenteeism", "Safety Violation", "Poor Work Quality / Performance",
                         "Insubordination", "Damage to Company Property", "Policy Violation (Other)"]},
            {"key": "date", "label": "Date of incident", "type": "date", "required": True},
            {"key": "time", "label": "Time of incident", "type": "time"},
            {"key": "description", "label": "What happened", "type": "textarea", "required": True,
             "placeholder": "State exactly what occurred…"},
            {"key": "prior", "label": "Prior warnings", "type": "textarea",
             "placeholder": "Dates and context of earlier talks about this, or 'None'"},
            {"key": "plan", "label": "Corrective action plan", "type": "textarea", "required": True,
             "placeholder": "What the employee must do to improve"},
            {"key": "consequences", "label": "Consequences of failure to improve", "type": "textarea", "required": True,
             "default": "Further violations of company policy or failure to meet the outlined performance expectations "
                        "will result in further disciplinary action, up to and including termination of employment."},
        ],
        "photos": [],
        "summary": ["target", "level"],
    },
    "Measure Report": {
        "slug": "measure", "prefix": "MSR", "order": 8, "kind": "measure",
        "blurb": "Measure doors and windows on site: sizes, trim, labor, photos. Reopen and fix later.",
        "fields": [], "photos": [],
    },
}

FORM_BY_SLUG = {v["slug"]: k for k, v in FORMS.items()}
# keep the home screen order: Installation Completion, then RMA, right after Delivery Proof
for _i, _k in enumerate(sorted(FORMS, key=lambda k: (FORMS[k]["order"], {"Installation Completion": 0, "RMA": 1}.get(k, 2))), 1):
    FORMS[_k]["order"] = _i
MAX_TEXT = 4000

# Extra email lists beyond each form's main list (editable in Admin > Email lists)
EXTRA_RULES = {"Vehicle Inspection: when something is Defective": "admin@simplydoors.com"}


# ------------------------------------------------------------------ lists
def get_list(name: str) -> list[str]:
    raw = get_setting(f"list:{name}")
    if raw:
        try:
            vals = json.loads(raw)
            if isinstance(vals, list) and vals:
                return [str(v) for v in vals]
        except Exception:
            pass
    return list(DEFAULT_LISTS[name])


def set_list(name: str, values: list[str]) -> list[str]:
    clean_vals, seen = [], set()
    for v in values:
        v = " ".join(str(v).split())[:80]
        if v and v.lower() not in seen:
            seen.add(v.lower())
            clean_vals.append(v)
    if not clean_vals:
        raise ValueError("The list can't be empty.")
    set_setting(f"list:{name}", json.dumps(clean_vals))
    return clean_vals


def _people(kind: str):
    if kind == "sales_reps":
        q = "SELECT id, name, email FROM staff WHERE sales_notify=1 AND active=1 ORDER BY name"
    else:
        q = "SELECT id, name, email FROM staff WHERE active=1 ORDER BY name"
    return conn().execute(q).fetchall()


def options_for(field) -> list:
    """Options as [(value, label)] for selects/choices."""
    opts = field.get("options")
    if opts in ("sales_reps", "staff"):
        return [(str(r["id"]), r["name"]) for r in _people(opts)]
    if isinstance(opts, str):
        return [(v, v) for v in get_list(opts)]
    return [(v, v) for v in (opts or [])]


# ------------------------------------------------------------------ what the phone gets
def enabled_forms() -> list[str]:
    """Forms switched on for staff (Admin > Lists). Admins always see every form so they can test."""
    raw = get_setting("forms_enabled")
    try:
        vals = json.loads(raw) if raw else ["Receiving Report"]
    except Exception:
        vals = ["Receiving Report"]
    return [v for v in vals if v in FORMS]


def set_enabled_forms(names: list[str]) -> list[str]:
    vals = [n for n in names if n in FORMS and not FORMS[n].get("admin_only")]
    set_setting("forms_enabled", json.dumps(vals))
    return vals


def visible_forms(staff) -> list[str]:
    on = set(enabled_forms())
    return [k for k, v in sorted(FORMS.items(), key=lambda kv: kv[1]["order"])
            if staff["is_admin"] or (k in on and not v.get("admin_only"))]


def public_spec(form_type: str) -> dict:
    spec = FORMS[form_type]
    fields = []
    for f in spec["fields"]:
        out = {k: v for k, v in f.items() if k not in ("options", "groups", "items")}
        if f["type"] in ("select", "choice"):
            out["options"] = options_for(f)
        if f["type"] in ("checks", "donena"):
            out["items"] = f["items"]
        if f["type"] == "okdef":
            out["groups"] = f["groups"]
        fields.append(out)
    out = {"type": form_type, "slug": spec["slug"], "blurb": spec["blurb"], "fields": fields,
           "photos": spec["photos"], "admin_only": bool(spec.get("admin_only")), "kind": spec.get("kind", "form"),
           "staff_can_see": form_type in enabled_forms() and not spec.get("admin_only")}
    if spec.get("kind") == "measure":
        out["measure"] = M.public()
    return out


# ------------------------------------------------------------------ validation
def _shown(f, raw) -> bool:
    cond = f.get("show_if")
    return not cond or (raw.get(cond["field"]) or "").strip() in cond["in"]


def _truthy(v) -> bool:
    return str(v or "").lower() in ("1", "true", "on", "yes")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def clean(form_type: str, raw: dict) -> tuple[dict, list[str]]:
    """Validate submitted fields against the form definition. Returns (data, errors)."""
    spec = FORMS[form_type]
    if spec.get("kind") == "measure":
        return M.clean(raw)
    data, errors = {}, []
    any_defective = False
    for f in spec["fields"]:
        key, label, t = f["key"], f["label"], f["type"]
        if not _shown(f, raw):
            continue
        need = f.get("required", False)
        if t in ("text", "textarea"):
            v = (raw.get(key) or "").strip()[:MAX_TEXT if t == "textarea" else 300]
            if need and not v:
                errors.append(f"{label} is required.")
            data[key] = v
        elif t == "number":
            v = (raw.get(key) or "").strip().replace(",", "")
            if v and not re.fullmatch(r"\d{1,9}", v):
                errors.append(f"{label} must be a whole number.")
            elif v and int(v) > f.get("max", 10 ** 9):
                errors.append(f"{label} looks too large.")
            elif need and not v:
                errors.append(f"{label} is required.")
            data[key] = v
        elif t == "date":
            v = (raw.get(key) or "").strip()
            if v and not re.fullmatch(r"20\d\d-\d\d-\d\d", v):
                errors.append(f"{label} isn't a valid date.")
            elif need and not v:
                errors.append(f"{label} is required.")
            data[key] = v
        elif t == "time":
            v = (raw.get(key) or "").strip()[:5]
            if v and not re.fullmatch(r"\d\d:\d\d", v):
                errors.append(f"{label} isn't a valid time.")
            elif need and not v:
                errors.append(f"{label} is required.")
            data[key] = v
        elif t in ("select", "choice"):
            v = (raw.get(key) or "").strip()
            opts = dict(options_for(f))
            if f.get("options") in ("sales_reps", "staff"):
                if not v or v == "none":
                    if need:
                        errors.append(f"Pick: {label}.")
                    data[key], data[key + "_email"] = ("Not requested" if f.get("notify") else ""), ""
                else:
                    row = next((r for r in _people(f["options"]) if str(r["id"]) == v), None)
                    if not row:
                        errors.append(f"Pick {label} from the list.")
                    else:
                        data[key], data[key + "_email"] = row["name"], row["email"]
                continue
            if v == "Custom" and f.get("allow_other"):
                v = (raw.get(key + "_custom") or "").strip()[:120]
                if need and not v:
                    errors.append(f"Type the {label.lower()}.")
            elif v and v not in opts:
                errors.append(f"Pick {label} from the list.")
                v = ""
            elif need and not v:
                errors.append(f"Pick: {label}.")
            data[key] = v
        elif t == "checks":
            for ikey, text in f["items"]:
                ticked = _truthy(raw.get(ikey))
                if need and not ticked:
                    errors.append(f"Check the box: {text}")
                data[ikey] = ticked
        elif t == "donena":
            results, missing = {}, 0
            for ikey, text in f["items"]:
                v = (raw.get(f"{key}:{ikey}") or "").strip()
                if v not in ("Done", "N/A"):
                    missing += 1
                else:
                    results[text] = v
            if need and missing:
                errors.append(f"Mark every checklist item Done or N/A ({missing} left).")
            data[key] = results
        elif t == "okdef":
            results = {}
            missing = 0
            for group, items in f["groups"].items():
                for item in items:
                    v = (raw.get(f"{key}:{_slug(group)}:{_slug(item)}") or "").strip()
                    if v not in ("OK", "Defective"):
                        missing += 1
                    else:
                        results[f"{group} — {item}"] = v
                        any_defective |= v == "Defective"
            if need and missing:
                errors.append(f"Mark every inspection item OK or Defective ({missing} left).")
            data[key] = results
    for f in spec["fields"]:
        if f.get("required_if_defective") and any_defective and not data.get(f["key"]):
            errors.append(f"{f['label']} is required when something is Defective.")
    if form_type == "Vehicle Inspection":
        data["defective"] = any_defective
    if form_type == "Installation Completion":
        data["attention"] = data.get("punch") == "Yes" or data.get("cust_present") == "No"
    return data, errors


# ------------------------------------------------------------------ PDF / email / admin
def display_rows(form_type: str, data: dict) -> list[tuple[str, str]]:
    if FORMS.get(form_type, {}).get("kind") == "measure":
        return M.display_rows(data)
    rows = []
    for f in FORMS[form_type]["fields"]:
        key, t = f["key"], f["type"]
        if t == "checks":
            if not any(ik in data for ik, _ in f["items"]):
                continue
            for ik, text in f["items"]:
                rows.append((text, "Yes" if data.get(ik) else "No"))
            continue
        if t == "donena":
            res = data.get(key) or {}
            na = [k for k, v in res.items() if v == "N/A"]
            rows.append((f["label"], f"{len(res) - len(na)} done" + (f", {len(na)} N/A" if na else "")))
            for k, v in res.items():
                rows.append((k, "Done" if v == "Done" else "N/A"))
            continue
        if t == "okdef":
            res = data.get(key) or {}
            bad = [k for k, v in res.items() if v == "Defective"]
            rows.append(("Defective items", ", ".join(bad) if bad else "None"))
            for k, v in res.items():
                rows.append((k, "DEFECTIVE" if v == "Defective" else "OK"))
            continue
        if key not in data:
            continue          # hidden section, or an older report before this field existed
        v = data.get(key)
        if isinstance(v, bool):
            v = "Yes" if v else "No"
        rows.append((f["label"], v if v not in ("", None) else "—"))
    return rows


def email_rows(form_type: str, data: dict) -> list[tuple[str, str]]:
    """Short version for the email body; the PDF attachment has everything."""
    keys = FORMS.get(form_type, {}).get("email_keys")
    if not keys:
        return display_rows(form_type, data)
    by = {f["key"]: f for f in FORMS[form_type]["fields"]}
    rows = []
    for k in keys:
        if data.get(k) not in ("", None):
            rows.append((by[k]["label"], data[k]))
    for f in FORMS[form_type]["fields"]:
        if f["type"] == "donena" and data.get(f["key"]):
            res = data[f["key"]]
            na = [k for k, v in res.items() if v == "N/A"]
            rows.append((f["label"], f"{len(res) - len(na)} of {len(res)} done" + (f" ({len(na)} N/A)" if na else "")))
    return rows


def summary(form_type: str, data: dict) -> str:
    if FORMS.get(form_type, {}).get("kind") == "measure":
        return " · ".join(x for x in (data.get("customer"), data.get("po"),
                                     f"revises {data['revision_of']}" if data.get("revision_of") else "") if x)
    keys = FORMS.get(form_type, {}).get("summary", [])
    return " · ".join(str(data.get(k)) for k in keys if data.get(k))


def subject_for(form_type: str, data: dict, staff_name: str, receipt: str) -> str:
    d = data
    s = {
        "Receiving Report": f"Receiving Report: {d.get('po')} - {d.get('customer')}",
        "Delivery Proof": f"Delivery Proof: {d.get('po')} - {d.get('customer')}",
        "Installation Completion": f"{'NEEDS FOLLOW-UP - ' if d.get('attention') else ''}Install Complete: {d.get('po')} - {d.get('customer')}"
                                   f"{' (punch list)' if d.get('punch') == 'Yes' else ''}{' (not signed)' if d.get('cust_present') == 'No' else ''}",
        "End of Shift": f"End of Shift: {d.get('role')} - {staff_name}",
        "Vehicle Inspection": f"{'DEFECTIVE - ' if d.get('defective') else ''}Vehicle {d.get('trip')}: {d.get('vehicle')} - {staff_name}",
        "Vehicle Incident": f"URGENT: Vehicle Incident - {d.get('vehicle')} ({staff_name})",
        "Employee Incident": f"URGENT: Employee Incident - {staff_name}",
        "RMA": (f"RMA to {d.get('vendor')}: {d.get('po')}" + (f" - {d['job_customer']}" if d.get('job_customer') else "")
                + (f" (vendor RMA {d['vendor_rma']})" if d.get('vendor_rma') else "")
                if d.get("direction") == RMA_VENDOR else f"RMA from customer: {d.get('po')} - {d.get('customer')}"),
        "Disciplinary Action": f"CONFIDENTIAL: Disciplinary Action - {d.get('target')}",
        "Measure Report": f"{'REVISED ' if d.get('revision_of') else ''}Measure Report: {d.get('customer')}"
                          f"{' - PO ' + d['po'] if d.get('po') else ''} ({d.get('measured_by') or staff_name})",
    }.get(form_type, f"{form_type}: {staff_name}")
    if form_type == "Measure Report" and d.get("revision_of"):
        s += f" replaces {d['revision_of']}"
    return " ".join(f"{s} [{receipt}]".split())[:200]


def _rule(name: str) -> list[str]:
    row = conn().execute("SELECT recipients FROM email_rules WHERE form_type=?", (name,)).fetchone()
    return [r.strip() for r in (row["recipients"] if row else "").split(",") if r.strip()]


def owner_email() -> str:
    row = conn().execute("SELECT email FROM staff WHERE is_owner=1 AND active=1").fetchone()
    return (row["email"] if row else "").strip()


def owner_copies() -> list[str]:
    try:
        return [x for x in json.loads(get_setting("owner_copies") or "[]") if isinstance(x, str)]
    except Exception:
        return []


def set_owner_copies(forms: list[str]) -> list[str]:
    allowed = set(FORMS) | set(EXTRA_RULES)
    vals = [f for f in forms if f in allowed]
    set_setting("owner_copies", json.dumps(vals))
    return vals


def split_recipients(form_type: str, data: dict) -> tuple[list[str], list[str]]:
    """(to, private copies). The owner's address is only ever shown in 'To' when someone typed it into an
    email list on purpose; when it gets added automatically (sales rep picked, who measured, owner copy)
    it goes as a private BCC copy instead."""
    owner = owner_email().lower()
    rule = _rule(form_type)
    if form_type == "Vehicle Inspection" and data.get("defective"):
        rule += _rule("Vehicle Inspection: when something is Defective")
    auto = []
    for f in FORMS[form_type]["fields"]:
        if (f.get("notify") or f.get("target")) and data.get(f["key"] + "_email"):
            auto.append(data[f["key"] + "_email"])
    if form_type == "Measure Report":
        auto += [e for e in (data.get("measured_by_email"), data.get("revised_by_email")) if e]
    to, bcc = [], []
    for e in rule:
        to.append(e)
    for e in auto:
        (bcc if owner and e.lower() == owner else to).append(e)
    if owner and (form_type in owner_copies() or (form_type == "Vehicle Inspection" and data.get("defective")
                                                    and "Vehicle Inspection: when something is Defective" in owner_copies())):
        bcc.append(owner_email())

    def dedupe(xs, skip=()):
        seen, out = {x.lower() for x in skip}, []
        for x in xs:
            if x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
        return out
    to = dedupe(to)
    return to, dedupe(bcc, skip=to)


def recipients_for(form_type: str, data: dict) -> list[str]:
    rcpts = _rule(form_type)
    for f in FORMS[form_type]["fields"]:
        if (f.get("notify") or f.get("target")) and data.get(f["key"] + "_email"):
            rcpts.append(data[f["key"] + "_email"])
    if form_type == "Vehicle Inspection" and data.get("defective"):
        rcpts += _rule("Vehicle Inspection: when something is Defective")
    if form_type == "Measure Report":
        rcpts += [e for e in (data.get("measured_by_email"), data.get("revised_by_email")) if e]
    seen, out = set(), []
    for r in rcpts:
        if r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def photo_slots(form_type: str, data: dict | None = None) -> list[dict]:
    """Every photo/signature slot of a form: slot, label, group title, signature?
    Measure slots depend on how many cards the report has, so pass its data."""
    if FORMS.get(form_type, {}).get("kind") == "measure":
        return M.photo_slots(data)
    out = []
    for g in FORMS[form_type]["photos"]:
        if g.get("signature"):
            out.append({"slot": g["signature"], "label": "Customer signature", "group": g["title"], "signature": True})
        for slot, label in g.get("slots", []):
            out.append({"slot": slot, "label": label, "group": g["title"], "signature": False})
    return out


def photo_minimums(form_type: str) -> list[tuple[str, list[str], int]]:
    return [(g["title"], [s for s, _ in g.get("slots", [])], g["min"]) for g in FORMS[form_type]["photos"] if g.get("min")]

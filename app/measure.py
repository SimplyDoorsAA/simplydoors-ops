"""Measure Report: one job, any number of door and window cards, up to 3 photos per card.

Field names and option lists match the old Measure App (measure.html) so nothing the
measurers are used to changes. Sizes are kept exactly as typed: whole inches plus a
fraction, never converted or rounded.
"""
import json
import re

FRACTIONS = ["", "1/8", "1/4", "3/8", "1/2", "5/8", "3/4", "7/8"]
MAX_ITEMS = 60
MAX_POINTS = 20
PHOTOS_PER_ITEM = 4
# guided photo slots per card, in order (slot k = label k)
PHOTO_LABELS = {
    "door": ["Outside, whole door", "Inside, whole door", "Sill and floor", "Extra"],
    "window": ["Outside, whole window", "Inside, whole window", "Sill / trim close-up", "Extra"],
}

CONFIGS = ["Single Door", "Double Door", "Single w/ 1 Sidelite", "Single w/ 2 Sidelites", "Sliding Glass Door"]
SINGLE_HANDING = ["Left", "Right", "Fixed"]
HANDINGS = {
    "Double Door": ["Left Hand Active", "Right Hand Active", "LH/Fixed", "RH/Fixed", "Fixed/LH", "Fixed/RH", "Fixed/Fixed"],
    "Sliding Glass Door": ["Left Slide", "Right Slide"],
    "_default": SINGLE_HANDING,
}
# answers saved by the first version of this app, mapped onto the vendor-style fields
LEGACY_CONFIG = {"Single": "Single Door", "Double": "Double Door", "Sgl w/ 1 SL": "Single w/ 1 Sidelite",
                 "Sgl w/ 2 SL": "Single w/ 2 Sidelites"}


def pick_options(f: dict, item: dict) -> list:
    if "options_by" in f:
        return f["options_by"].get(item.get("config") or "", f["options_by"]["_default"])
    return f["options"]


def pick_shown(f: dict, item: dict) -> bool:
    for k, vals in (f.get("show_if") or {}).items():
        if item.get(k) not in vals:
            return False
    for k, vals in (f.get("hide_if") or {}).items():
        if item.get(k) in vals:
            return False
    return True


def upgrade_door(it: dict) -> dict:
    """Turn an older door card (config 'Sgl w/ 1 SL', handing 'Left Hand Inswing') into the current fields."""
    it = dict(it)
    it["config"] = LEGACY_CONFIG.get(it.get("config") or "", it.get("config") or "")
    h = it.get("handing") or ""
    m = re.fullmatch(r"(Left|Right) Hand (In|Out)swing", h)
    if m:
        it["swing"] = it.get("swing") or f"{m.group(2)}Swing"
        it["handing"] = (f"{m.group(1)} Hand Active" if it["config"] == "Double Door" else m.group(1))
    elif h == "Slider":
        it["config"], it["handing"] = "Sliding Glass Door", ""
    return it


# "custom": True means the phone offers "Custom (type it)" and any typed value is accepted.
DOOR_FIELDS = [
    {"key": "loc", "label": "Location", "type": "text", "placeholder": "e.g. Front Entry"},
    # Config → sidelite side → handing → swing, the same order as the door vendors' order forms.
    # Handing is always judged from the EXTERIOR (vendor convention: "Exterior View").
    {"key": "config", "label": "Config", "type": "pick", "icon": "config", "custom": True,
     "options": CONFIGS},
    {"key": "sidelite", "label": "Sidelite location", "type": "pick", "icon": "sidelite",
     "options": ["Left", "Right"], "show_if": {"config": ["Single w/ 1 Sidelite"]}},
    {"key": "handing", "label": "Handing (exterior view)", "type": "pick", "icon": "handing",
     "options_by": HANDINGS},
    {"key": "swing", "label": "Swing", "type": "pick", "icon": "swing", "options": ["InSwing", "OutSwing"],
     "hide_if": {"config": ["Sliding Glass Door"], "handing": ["Fixed", "Fixed/Fixed"]}},
    {"key": "dim_type", "label": "Size type", "type": "select", "options": ["Unit Size", "Rough Opening"], "default": "Unit Size"},
    {"key": "w", "label": "Width", "type": "size", "required": True},
    {"key": "h", "label": "Height", "type": "size", "required": True},
    {"key": "jamb", "label": "Jamb depth", "type": "select", "custom": True, "options": ["4 9/16", "6 9/16", "5 1/4"]},
    {"key": "bore", "label": "Bore / Lock", "type": "select", "custom": True,
     "options": ["Single Bore", "Double Bore", "Mortise", "None"]},
    {"key": "ext", "label": "Ext trim", "type": "select", "custom": True, "options": ["Brickmould", "1x4", "1x6", "No Trim"]},
    {"key": "int", "label": "Int trim", "type": "select", "custom": True,
     "options": ["2 1/4 Casing", "2 1/2 Col. Casing", "3 1/4 Casing", "1x4", "No Trim"]},
    {"key": "labor", "label": "Labor", "type": "labor", "options": ["Cut Tile", "Cut Wood", "Cement Support", "Wood Support"]},
    {"key": "custom_labor", "label": "Other labor", "type": "text"},
    {"key": "notes", "label": "Notes", "type": "textarea"},
]

WINDOW_FIELDS = [
    {"key": "loc", "label": "Location / mark", "type": "text", "placeholder": "e.g. Kitchen W1"},
    {"key": "floor", "label": "Floor", "type": "select", "options": ["1st", "2nd", "3rd"], "default": "1st"},
    {"key": "qty", "label": "Qty", "type": "qty", "default": "1"},
    {"key": "m_type", "label": "Size type", "type": "select", "options": ["Rough Opening", "Unit Size", "Daylight"],
     "default": "Rough Opening"},
    {"key": "points", "label": "Measurements (W × H)", "type": "points", "required": True},
    {"key": "sill", "label": "Floor to sill", "type": "size"},
    {"key": "tempered", "label": "Tempered", "type": "toggle"},
    {"key": "wall", "label": "Wall thickness", "type": "select", "custom": True, "options": ["4 9/16", "6 9/16", "3 1/2", "5 1/2"]},
    {"key": "mat", "label": "Material", "type": "select", "options": ["Wood Stud", "CMU/Block"]},
    {"key": "frame", "label": "Frame depth", "type": "select", "options": ['4-1/2"', '6"']},
    {"key": "ext", "label": "Ext trim", "type": "select", "options": ["1x4 Vinyl", "No Trim"]},
    {"key": "int", "label": "Int trim", "type": "select", "options": ["Drywall Return", "Casing"]},
    {"key": "labor", "label": "Labor", "type": "labor", "options": ["Cut Sheetrock/Wood", "Cut Tile/Granite", "Stucco Removal"]},
    {"key": "custom_labor", "label": "Other labor", "type": "text"},
    {"key": "notes", "label": "Notes", "type": "textarea"},
]

ITEM_FIELDS = {"door": DOOR_FIELDS, "window": WINDOW_FIELDS}
TYPE_NAME = {"door": "Door", "window": "Window"}


def public() -> dict:
    return {"fractions": FRACTIONS, "legacy_config": LEGACY_CONFIG, "door": DOOR_FIELDS, "window": WINDOW_FIELDS,
            "max_items": MAX_ITEMS, "photos_per_item": PHOTOS_PER_ITEM, "photo_labels": PHOTO_LABELS}


# ------------------------------------------------------------------ sizes
_WHOLE = re.compile(r"\d{1,4}(\.\d{1,3})?")


def _size(raw, label, errors, required=False):
    raw = raw if isinstance(raw, dict) else {}
    w = str(raw.get("w") or "").strip()
    f = str(raw.get("f") or "").strip()
    if f not in FRACTIONS:
        errors.append(f"{label}: pick a fraction from the list.")
        f = ""
    if w and not _WHOLE.fullmatch(w):
        errors.append(f"{label}: inches must be a number.")
        w = ""
    if required and not w:
        errors.append(f"{label} is needed.")
    return {"w": w, "f": f}


def fmt_size(s) -> str:
    if not isinstance(s, dict) or not s.get("w"):
        return ""
    return f'{s["w"]}{" " + s["f"] if s.get("f") else ""}"'


def _txt(v, n=120) -> str:
    return " ".join(str(v or "").split())[:n] if n <= 300 else str(v or "").strip()[:n]


# ------------------------------------------------------------------ validation
def clean(raw: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    data = {"customer": _txt(raw.get("customer"), 200), "po": _txt(raw.get("po"), 120),
            "date": str(raw.get("date") or "").strip()}
    if not data["customer"]:
        errors.append("Customer name is required.")
    if data["date"] and not re.fullmatch(r"20\d\d-\d\d-\d\d", data["date"]):
        errors.append("Date isn't a valid date.")
    try:
        items = json.loads(raw.get("items") or "[]")
    except Exception:
        items = None
    if not isinstance(items, list):
        return data, errors + ["The measure list couldn't be read. Please try again."]
    if not items:
        errors.append("Add at least one door or window.")
    if len(items) > MAX_ITEMS:
        errors.append(f"A measure can have up to {MAX_ITEMS} doors and windows. Split it into two jobs.")
        items = items[:MAX_ITEMS]
    out, counts = [], {"door": 0, "window": 0}
    for it in items:
        if not isinstance(it, dict) or it.get("type") not in ITEM_FIELDS:
            errors.append("One of the cards couldn't be read.")
            continue
        t = it["type"]
        if t == "door":
            it = upgrade_door(it)          # a phone still on the old version can send old-style answers
        counts[t] += 1
        name = f"{TYPE_NAME[t]} #{counts[t]}"
        o = {"type": t}
        for f in ITEM_FIELDS[t]:
            k, ft = f["key"], f["type"]
            v = it.get(k)
            label = f"{name} {f['label'].lower()}"
            if ft == "text":
                o[k] = _txt(v, 200)
            elif ft == "textarea":
                o[k] = _txt(v, 2000)
            elif ft == "pick":
                if not pick_shown(f, o):
                    o[k] = ""
                    continue
                o[k] = _txt(v, 120)
                if o[k] and o[k] not in pick_options(f, o) and not f.get("custom"):
                    errors.append(f"{label}: pick one of the pictures.")
                    o[k] = ""
            elif ft == "select":
                o[k] = _txt(v, 120)
                if o[k] and not f.get("custom") and o[k] not in f["options"]:
                    errors.append(f"{label}: pick from the list.")
            elif ft == "size":
                o[k] = _size(v, f"{name} {f['label'].lower()}", errors, f.get("required", False))
            elif ft == "qty":
                q = str(v or "1").strip()
                if not re.fullmatch(r"\d{1,2}", q) or int(q) < 1:
                    errors.append(f"{name} qty must be 1 to 99.")
                    q = "1"
                o[k] = q
            elif ft == "toggle":
                o[k] = v is True or str(v).lower() in ("1", "true", "on", "yes")
            elif ft == "labor":
                picked = v if isinstance(v, list) else []
                o[k] = [x for x in f["options"] if x in picked]
            elif ft == "points":
                pts = v if isinstance(v, list) else []
                pts = pts[:MAX_POINTS]
                clean_pts = []
                for i, p in enumerate(pts, 1):
                    p = p if isinstance(p, dict) else {}
                    pw = _size(p.get("w"), f"{name} point {i} width", errors)
                    ph = _size(p.get("h"), f"{name} point {i} height", errors)
                    if pw["w"] or ph["w"]:
                        if not (pw["w"] and ph["w"]):
                            errors.append(f"{name} point {i} needs both width and height.")
                        clean_pts.append({"w": pw, "h": ph})
                if not clean_pts:
                    errors.append(f"{name} needs at least one width × height.")
                o[k] = clean_pts
        out.append(o)
    data["items"] = out
    data["doors"], data["windows"] = counts["door"], counts["window"]
    return data, errors


# ------------------------------------------------------------------ display
def item_name(data: dict, idx: int) -> str:
    """'Door #2' for the idx-th card (0-based), doors and windows numbered separately."""
    items = data.get("items") or []
    t = items[idx]["type"]
    n = sum(1 for it in items[: idx + 1] if it["type"] == t)
    return f"{TYPE_NAME[t]} #{n}"


def item_title(data: dict, idx: int) -> str:
    it = data["items"][idx]
    return item_name(data, idx) + (f" — {it['loc']}" if it.get("loc") else "")


def item_summary(it: dict) -> str:
    if it["type"] == "door":
        size = " × ".join(x for x in (fmt_size(it.get("w")), fmt_size(it.get("h"))) if x)
        cfg = it.get("config") or ""
        if it.get("sidelite"):
            cfg += f" ({it['sidelite']} SL)"
        hand = " ".join(x for x in (it.get("handing"), it.get("swing")) if x)
        parts = [f"{size} {it.get('dim_type') or ''}".strip(), cfg, hand]
    else:
        pts = [f'{fmt_size(p["w"])} × {fmt_size(p["h"])}' for p in it.get("points") or []]
        parts = [f"Qty {it.get('qty') or 1}", ", ".join(pts) + (f" ({it['m_type']})" if it.get("m_type") else ""),
                 f"{it['floor']} floor" if it.get("floor") else "", "TEMPERED" if it.get("tempered") else ""]
    return " · ".join(p for p in parts if p)


def item_rows(it: dict) -> list[tuple[str, str]]:
    rows = []
    for f in ITEM_FIELDS[it["type"]]:
        k, ft, v = f["key"], f["type"], it.get(f["key"])
        if ft == "size":
            v = fmt_size(v)
        elif ft == "points":
            v = "\n".join(f'{i}) {fmt_size(p["w"])} W × {fmt_size(p["h"])} H' for i, p in enumerate(v or [], 1))
        elif ft == "toggle":
            v = "YES" if v else "No"
        elif ft == "labor":
            v = ", ".join(v or [])
        if k == "loc":
            continue                      # already in the card title
        if v in ("", None, []) and ft not in ("toggle",):
            continue                      # leave out what wasn't filled in
        if ft == "toggle" and not it.get(k):
            continue
        rows.append((f["label"], str(v)))
    return rows


def job_rows(data: dict) -> list[tuple[str, str]]:
    rows = [("Customer", data.get("customer") or "—"), ("PO / Reference #", data.get("po") or "—"),
            ("Date measured", data.get("date") or "—"), ("Measured by", data.get("measured_by") or "—")]
    if data.get("revised_by"):
        rows.append(("Revised by", data["revised_by"]))
    if data.get("revision_of"):
        rows.append(("REVISED", f"This replaces {data['revision_of']}"))
    d, w = data.get("doors", 0), data.get("windows", 0)
    rows.append(("Items", ", ".join(x for x in (f"{d} door{'s' if d != 1 else ''}" if d else "",
                                               f"{w} window{'s' if w != 1 else ''}" if w else "") if x) or "—"))
    return rows


def display_rows(data: dict) -> list[tuple[str, str]]:
    """Job details plus one summary line per card (email, admin screen)."""
    rows = job_rows(data)
    for i, it in enumerate(data.get("items") or []):
        rows.append((item_title(data, i), item_summary(it) or "—"))
    return rows


def photo_slots(data: dict | None) -> list[dict]:
    """i{n}p{k}: card n (1-based, in the order sent), photo k."""
    out = []
    items = (data or {}).get("items") or []
    for i in range(len(items)):
        name = item_name(data, i)
        labels = PHOTO_LABELS[items[i]["type"]]
        for k in range(1, PHOTOS_PER_ITEM + 1):
            out.append({"slot": f"i{i + 1}p{k}", "label": f"{name} · {labels[k - 1]}", "group": item_title(data, i),
                        "signature": False, "item": i})
    return out

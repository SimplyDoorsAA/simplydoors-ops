"""Price List (beta): vendor price sheets, a shared buy list per person, and purchase orders sent to the vendor.

- Price sheets are uploaded by an admin (Admin -> Price List) as a CSV and kept only in the database on the OptiPlex.
  Vendor prices are confidential, so none are ever stored in this repository.
- A vendor can have several live sheets (Boise Cascade: Simpson shaker, Simpson rift white oak, Steves). An upload either
  replaces one of them or is added next to them.
- A sheet can carry other price levels ("compare Container", "compare Pallet", ...) shown for comparison only; a PO
  always uses the main price.
- Only people with the Price List switch on (Admin -> Staff) can see net costs; the owner always can.
- A few of them (e.g. the purchaser) can also edit items in the app: change any field, add, delete. Every change is in
  the activity log, and an edited item says who changed it. Replacing a sheet with a new upload replaces its edits too.
- A purchase order's number is the PO number already on the Service Fusion job. The app never writes to Service Fusion.
- Sending a PO emails a PDF to the vendor's order address, with a copy to admin@simplydoors.com.
- Prices on a PO are always worked out here from the loaded sheet, never taken from the phone.
"""
import csv
import io
import json
import re

from .db import conn, now_iso

ADMIN_COPY = "admin@simplydoors.com"
NONSTOCK_MIN_QTY = 10          # non-stock interior slabs: +30% when fewer than 10 of one size / style
NONSTOCK_SURCHARGE = 0.30
NONSTOCK_CATS = ("Interior molded", "Interior flush", "Interior bifolds")
MAX_LINES = 200
MAX_QTY = 999

CATS = ("Interior molded", "Interior flush", "Interior stile & rail", "Interior bifolds",
        "Exterior doors & sidelites", "Exterior glass & lites", "Parts & hardware",
        "Moulding & trim", "Jambs & frames", "Boards", "Stair parts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_vendors (
    code TEXT PRIMARY KEY,                       -- WG, BC, SP
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',            -- lines separated by newlines, printed on the PO
    order_email TEXT NOT NULL DEFAULT '',        -- where POs are emailed (set in Admin -> Price List; kept out of the repo)
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pl_sheets (
    id INTEGER PRIMARY KEY,
    vendor TEXT NOT NULL REFERENCES pl_vendors(code),
    label TEXT NOT NULL,                         -- e.g. "Full Line Catalog eff. 6/15/2026"
    filename TEXT NOT NULL DEFAULT '',
    items INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1            -- 1 = live; replaced or removed sheets are kept (0) for the record
);

CREATE TABLE IF NOT EXISTS pl_items (
    id INTEGER PRIMARY KEY,
    sheet_id INTEGER NOT NULL REFERENCES pl_sheets(id),
    vendor TEXT NOT NULL,
    sku TEXT NOT NULL,
    mfr TEXT NOT NULL DEFAULT '',                -- manufacturer's number (e.g. Therma-Tru), if the sheet has one
    name TEXT NOT NULL,
    cat TEXT NOT NULL,
    grp TEXT NOT NULL DEFAULT '',                -- the table / section it sits in on the sheet
    w REAL NOT NULL DEFAULT 0,                   -- door width / height in inches (0 = not a door)
    h REAL NOT NULL DEFAULT 0,
    th TEXT NOT NULL DEFAULT '',
    core TEXT NOT NULL DEFAULT '',
    price REAL,                                  -- NULL = blank on the sheet: call for price
    stock INTEGER,                               -- 1 stocked, 0 non-stock, NULL not marked
    uom TEXT NOT NULL DEFAULT '',
    hand TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    page INTEGER,
    flag TEXT NOT NULL DEFAULT '',               -- why this line needs checking with the rep
    compare TEXT NOT NULL DEFAULT '',            -- JSON [[label, price], ...]: other price levels, for comparison only
    edited_at TEXT,                              -- set when someone changed or added this item in the app
    edited_by TEXT,
    pack TEXT NOT NULL DEFAULT ''                -- pack sizes it's sold in, e.g. "25" or "6, 12" ('' = any quantity)
);
CREATE INDEX IF NOT EXISTS pl_items_sheet ON pl_items(sheet_id);

-- "Match style": one tag per vendor group (e.g. Steves Carrara = "2-panel shaker"), so the same door can be compared
-- across vendors. Kept by group name, so it carries over when a sheet is replaced with the same groups.
CREATE TABLE IF NOT EXISTS pl_styles (
    vendor TEXT NOT NULL,
    grp TEXT NOT NULL,
    style TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY (vendor, grp)
);

CREATE TABLE IF NOT EXISTS pl_pos (
    id INTEGER PRIMARY KEY,
    po_number TEXT NOT NULL,
    vendor TEXT NOT NULL REFERENCES pl_vendors(code),
    job_number TEXT NOT NULL,
    job_customer TEXT NOT NULL DEFAULT '',
    order_date TEXT NOT NULL,
    ship_method TEXT NOT NULL,
    ship_to TEXT NOT NULL,                       -- 'shop' | 'site'
    ship_address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    lines TEXT NOT NULL,                         -- JSON: what was ordered, at the prices of the sheet used
    total REAL NOT NULL,
    sheet_label TEXT NOT NULL DEFAULT '',
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    sent_to TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0,          -- owner's test mode: emailed only to the owner, never the vendor
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_pos_number ON pl_pos(po_number);
"""

SEED_VENDORS = [
    ("WG", "Woodgrain – Dallas", "2115 W. Valley View Lane, Ste 120\nFarmers Branch, TX 75234\n972-620-2200",
     "", 1),
    ("BC", "Boise Cascade", "", "", 2),
    ("SP", "Simpson", "", "", 3),
    ("NV", "Novo", "", "", 4),
]

OUR_NAME = "SimplyDoors"
OUR_ADDRESS = ["17750 Lookout Rd, Unit 150", "Schertz, TX 78154", "(210) 903-8450", "admin@simplydoors.com"]
SHIP_METHODS = ("Delivery", "Will Call – Dallas")


def init(c) -> None:
    c.executescript(SCHEMA)
    for code, name, addr, email, sort in SEED_VENDORS:
        c.execute("INSERT OR IGNORE INTO pl_vendors(code, name, address, order_email, sort) VALUES (?,?,?,?,?)",
                  (code, name, addr, email, sort))
    cols = [r[1] for r in c.execute("PRAGMA table_info(pl_items)")]
    if "compare" not in cols:
        c.execute("ALTER TABLE pl_items ADD COLUMN compare TEXT NOT NULL DEFAULT ''")
    if "edited_at" not in cols:
        c.execute("ALTER TABLE pl_items ADD COLUMN edited_at TEXT")
        c.execute("ALTER TABLE pl_items ADD COLUMN edited_by TEXT")
    if "pack" not in cols:
        c.execute("ALTER TABLE pl_items ADD COLUMN pack TEXT NOT NULL DEFAULT ''")


# ------------------------------------------------------------------ who can see it
def allowed(staff) -> bool:
    keys = staff.keys()
    return bool(staff["is_owner"]) or bool("price_list" in keys and staff["price_list"])


def can_edit(staff) -> bool:
    keys = staff.keys()
    return bool(staff["is_owner"]) or bool(allowed(staff) and "price_edit" in keys and staff["price_edit"])


# ------------------------------------------------------------------ vendors + sheets
def add_vendor(name: str) -> str:
    """Add a vendor (Admin -> Price List). Returns its code, made from the name (e.g. "Masonite" -> "MA")."""
    c = conn()
    name = " ".join(name.split())[:60]
    if len(name) < 2:
        raise SheetError("Give the vendor a name.")
    taken = {r["code"] for r in c.execute("SELECT code FROM pl_vendors")}
    if any(r["name"].lower() == name.lower() for r in c.execute("SELECT name FROM pl_vendors")):
        raise SheetError(f"There's already a vendor called {name}.")
    if len(taken) >= 40:
        raise SheetError("That's the most vendors the Price List holds.")
    letters = re.sub(r"[^A-Z]", "", name.upper()) or "V"
    code = next((x for x in [letters[:2], letters[0] + letters[-1], letters[:3]] if len(x) >= 2 and x not in taken), None)
    n = 2
    while not code:
        code = f"{letters[0]}{n}" if f"{letters[0]}{n}" not in taken else None
        n += 1
    sort = (c.execute("SELECT MAX(sort) FROM pl_vendors").fetchone()[0] or 0) + 1
    c.execute("INSERT INTO pl_vendors(code, name, sort) VALUES (?,?,?)", (code, name, sort))
    return code


def live_sheets(vendor: str) -> list[dict]:
    rows = conn().execute("SELECT s.id, s.label, s.filename, s.items, s.uploaded_at, s.uploaded_by,"
                          " (SELECT COUNT(*) FROM pl_items i WHERE i.sheet_id=s.id AND i.edited_at IS NOT NULL) AS edited"
                          " FROM pl_sheets s WHERE s.vendor=? AND s.active=1 ORDER BY s.id", (vendor,))
    return [dict(r) for r in rows]


def vendors() -> list[dict]:
    c = conn()
    out = []
    for v in c.execute("SELECT * FROM pl_vendors ORDER BY sort, code"):
        sheets = live_sheets(v["code"])
        newest = sheets[-1] if sheets else None
        out.append({"code": v["code"], "name": v["name"], "address": v["address"].split("\n") if v["address"] else [],
                    "order_email": v["order_email"], "sheets": sheets,
                    # all live sheets together, for the places that show one line per vendor
                    "sheet": {"id": newest["id"], "label": " · ".join(s["label"] for s in sheets),
                              "items": sum(s["items"] for s in sheets), "uploaded_at": newest["uploaded_at"],
                              "uploaded_by": newest["uploaded_by"]} if newest else None})
    return out


ITEM_COLS = ("id", "sku", "mfr", "name", "cat", "grp", "w", "h", "th", "core", "price", "stock", "uom", "hand",
             "brand", "page", "flag", "pack")


def _compare(raw: str) -> list[dict]:
    try:
        return [{"label": str(lbl), "price": float(p)} for lbl, p in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        return []


def items(vendor: str) -> list[dict]:
    rows = conn().execute(f"SELECT {', '.join('i.' + k for k in ITEM_COLS)}, i.compare, i.edited_at, i.edited_by,"
                          " i.sheet_id, s.label AS sheet, st.style"
                          " FROM pl_items i JOIN pl_sheets s ON s.id=i.sheet_id"
                          " LEFT JOIN pl_styles st ON st.vendor=s.vendor AND st.grp=i.grp"
                          " WHERE s.vendor=? AND s.active=1 ORDER BY s.id, i.id", (vendor,))
    out = []
    for r in rows:
        d = {k: r[k] for k in ITEM_COLS}
        d["stock"] = None if d["stock"] is None else bool(d["stock"])
        d["sheet"], d["sheet_id"] = r["sheet"], r["sheet_id"]
        d["compare"] = _compare(r["compare"])
        d["edited_at"], d["edited_by"] = r["edited_at"], r["edited_by"]
        d["style"] = r["style"] or ""
        out.append(d)
    return out


def set_style(vendor: str, grp: str, style: str, who: str):
    """Tag every item in one of a vendor's groups with a match style ("" removes it).
    Returns (old style, items in the group). Raises LookupError if the vendor has no live items in that group."""
    c = conn()
    style = " ".join(str(style or "").split())[:40]
    n = c.execute("SELECT COUNT(*) FROM pl_items i JOIN pl_sheets s ON s.id=i.sheet_id"
                  " WHERE s.vendor=? AND s.active=1 AND i.grp=?", (vendor, grp)).fetchone()[0]
    if not n:
        raise LookupError("That group isn't on a live sheet any more. Reload the Price List.")
    old = c.execute("SELECT style FROM pl_styles WHERE vendor=? AND grp=?", (vendor, grp)).fetchone()
    if style:
        c.execute("INSERT INTO pl_styles(vendor, grp, style, set_by, set_at) VALUES (?,?,?,?,?)"
                  " ON CONFLICT(vendor, grp) DO UPDATE SET style=excluded.style, set_by=excluded.set_by, set_at=excluded.set_at",
                  (vendor, grp, style, who, now_iso()))
    else:
        c.execute("DELETE FROM pl_styles WHERE vendor=? AND grp=?", (vendor, grp))
    return (old["style"] if old else ""), n


def get_item(item_id: int):
    """One item on a live sheet, as items() returns it, plus its vendor. None if it isn't live."""
    r = conn().execute("SELECT s.vendor FROM pl_items i JOIN pl_sheets s ON s.id=i.sheet_id WHERE i.id=? AND s.active=1",
                       (item_id,)).fetchone()
    if not r:
        return None
    d = next((x for x in items(r["vendor"]) if x["id"] == item_id), None)
    if d:
        d["vendor"] = r["vendor"]
    return d


# The upload format. One row per item; the first row holds these names (any order, case doesn't matter).
CSV_COLUMNS = ("sku", "name", "category", "price", "group", "mfr", "width_in", "height_in", "thickness", "core",
               "stocked", "uom", "hand", "brand", "page", "flag", "pack")
REQUIRED = ("sku", "name", "category", "price")
COMPARE_PREFIX = "compare "   # e.g. a column named "compare Pallet": another price level, shown for comparison only
MAX_COMPARE = 6


class SheetError(ValueError):
    pass


def clean_pack(v) -> str:
    """Pack sizes as "6, 12": whole numbers 2-999, any separators, smallest first. 1 or blank = any quantity."""
    sizes = sorted({int(x) for x in re.findall(r"\d+", str(v or "")) if 2 <= int(x) <= 999})[:5]
    return ", ".join(str(x) for x in sizes)


def pack_ok(qty: int, pack: str) -> bool:
    """Can qty be made of whole packs (any mix of the sizes)?"""
    sizes = [int(x) for x in re.findall(r"\d+", pack or "")]
    if not sizes:
        return True
    can = [True] + [False] * qty
    for q in range(1, qty + 1):
        can[q] = any(q >= s and can[q - s] for s in sizes)
    return can[qty]


def _num(v, field, line, allow_blank=True):
    at = f"Line {line}: " if line else ""     # line 0: typed in the app, not a line of a file
    v = (v or "").strip().replace("$", "").replace(",", "")
    if not v:
        if allow_blank:
            return None
        raise SheetError(f"{at}{field} is empty.")
    try:
        n = float(v)
    except ValueError:
        raise SheetError(f"{at}{field} “{v[:20]}” isn't a number.") from None
    if n < 0 or n > 1_000_000:
        raise SheetError(f"{at}{field} {n} is out of range.")
    return n


def parse_sheet(raw: bytes) -> list[dict]:
    """Read an uploaded CSV into item rows. Raises SheetError with a plain-English reason."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            raise SheetError("The file isn't a readable CSV. Save it from Excel as “CSV UTF-8”.") from None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SheetError("The file is empty.")
    names = {f.strip().lower(): f for f in reader.fieldnames if f}
    missing = [c for c in REQUIRED if c not in names]
    if missing:
        raise SheetError(f"The first row must name the columns. Missing: {', '.join(missing)}.")
    get = lambda row, c: (row.get(names[c]) or "").strip() if c in names else ""  # noqa: E731
    compare_cols = [(f.strip()[len(COMPARE_PREFIX):].strip()[:30], f) for f in reader.fieldnames
                    if f and f.strip().lower().startswith(COMPARE_PREFIX) and f.strip()[len(COMPARE_PREFIX):].strip()]
    if len(compare_cols) > MAX_COMPARE:
        raise SheetError(f"At most {MAX_COMPARE} “compare …” price columns.")
    out = []
    for i, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        sku = get(row, "sku")[:60]
        if not sku:
            raise SheetError(f"Line {i}: the part number (sku) is empty.")
        cat = get(row, "category")
        if cat not in CATS:
            raise SheetError(f"Line {i}: category “{cat[:40]}” isn't one of: {', '.join(CATS)}.")
        price = _num(get(row, "price"), "price", i)
        stocked = get(row, "stocked").upper()
        page = _num(get(row, "page"), "page", i)
        compare = []
        for lbl, col in compare_cols:
            n = _num((row.get(col) or "").strip(), f"“{lbl}” price", i)
            if n:
                compare.append([lbl, round(n, 2)])
        out.append({
            "sku": sku, "mfr": get(row, "mfr")[:60], "name": get(row, "name")[:160] or sku, "cat": cat,
            "grp": get(row, "group")[:120], "w": _num(get(row, "width_in"), "width_in", i) or 0,
            "h": _num(get(row, "height_in"), "height_in", i) or 0, "th": get(row, "thickness")[:20],
            "core": get(row, "core")[:40], "price": None if not price else round(price, 2),
            "stock": 1 if stocked in ("Y", "YES", "1", "TRUE") else 0 if stocked in ("N", "NO", "0", "FALSE") else None,
            "uom": get(row, "uom")[:30], "hand": get(row, "hand")[:4], "brand": get(row, "brand")[:40],
            "page": int(page) if page is not None else None, "flag": get(row, "flag")[:300],
            "compare": json.dumps(compare) if compare else "", "pack": clean_pack(get(row, "pack")),
        })
    if not out:
        raise SheetError("The file has no items.")
    # Vendor sheets do reuse a part number for two different items (a typo on their side). Keep both rows, so no
    # item goes missing, and flag them so nobody orders by that part number without checking.
    counts: dict[str, int] = {}
    for r in out:
        counts[r["sku"].upper()] = counts.get(r["sku"].upper(), 0) + 1
    for r in out:
        if counts[r["sku"].upper()] > 1:
            note = "Same part number is used for another item on this sheet"
            r["flag"] = (r["flag"] + " · " + note if r["flag"] and note not in r["flag"] else r["flag"] or note)[:300]
    if len(out) > 20000:
        raise SheetError("That's more than 20,000 items. Split the file.")
    return out


def load_sheet(vendor: str, label: str, filename: str, rows: list[dict], who: str, replace=None) -> int:
    """Load a sheet for a vendor. replace = id of one of the vendor's live sheets to take the place of, or None to add
    this sheet next to the ones already live. Raises SheetError for a sheet id that isn't this vendor's live sheet."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        live = {s["id"]: s for s in live_sheets(vendor)}
        if replace is not None:
            if replace not in live:
                raise SheetError("The sheet to replace isn't live any more. Reload the page and pick again.")
            c.execute("UPDATE pl_sheets SET active=0 WHERE id=?", (replace,))
        if any(s["label"].strip().lower() == label.strip().lower() for sid_, s in live.items() if sid_ != replace):
            raise SheetError(f"There's already a live sheet called “{label}”. Pick it under “Replaces” instead.")
        cur = c.execute("INSERT INTO pl_sheets(vendor, label, filename, items, uploaded_at, uploaded_by) VALUES (?,?,?,?,?,?)",
                        (vendor, label, filename, len(rows), now_iso(), who))
        sid = cur.lastrowid
        c.executemany(
            "INSERT INTO pl_items(sheet_id, vendor, sku, mfr, name, cat, grp, w, h, th, core, price, stock, uom, hand, brand,"
            " page, flag, compare, pack) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, vendor, r["sku"], r["mfr"], r["name"], r["cat"], r["grp"], r["w"], r["h"], r["th"], r["core"],
              r["price"], r["stock"], r["uom"], r["hand"], r["brand"], r["page"], r["flag"], r.get("compare", ""),
              r.get("pack", "")) for r in rows])
        _drop_dead_items(c, vendor)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return sid


def _drop_dead_items(c, vendor: str) -> None:
    # items of replaced or removed sheets aren't needed any more (every sent PO keeps its own copy of its lines)
    c.execute("DELETE FROM pl_items WHERE vendor=? AND sheet_id IN (SELECT id FROM pl_sheets WHERE vendor=? AND active=0)",
              (vendor, vendor))


def remove_sheet(sheet_id: int):
    """Take a live sheet out of the Price List. Returns the sheet row, or None if it wasn't live."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        s = c.execute("SELECT * FROM pl_sheets WHERE id=? AND active=1", (sheet_id,)).fetchone()
        if s:
            c.execute("UPDATE pl_sheets SET active=0 WHERE id=?", (sheet_id,))
            _drop_dead_items(c, s["vendor"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return s


# ------------------------------------------------------------------ editing items in the app
# field in the app -> (column, max length or kind). Same limits as an uploaded sheet.
EDIT_FIELDS = {"sku": ("sku", 60), "name": ("name", 160), "cat": ("cat", "cat"), "grp": ("grp", 120), "mfr": ("mfr", 60),
               "w": ("w", "num"), "h": ("h", "num"), "th": ("th", 20), "core": ("core", 40), "price": ("price", "price"),
               "stock": ("stock", "stock"), "uom": ("uom", 30), "hand": ("hand", 4), "brand": ("brand", 40),
               "flag": ("flag", 300), "compare": ("compare", "compare"), "pack": ("pack", "pack")}


def clean_fields(body: dict, partial: bool) -> dict:
    """Check the fields sent from the edit form. Returns {column: value}; raises SheetError with a plain reason."""
    out = {}
    for key, (col, kind) in EDIT_FIELDS.items():
        if key not in body:
            if partial:
                continue
            body = {**body, key: None}
        v = body[key]
        if kind == "cat":
            if v not in CATS:
                raise SheetError(f"Category must be one of: {', '.join(CATS)}.")
            out[col] = v
        elif kind == "num":
            n = _num(str(v if v is not None else ""), key == "w" and "Width" or "Height", 0)
            out[col] = n or 0
        elif kind == "price":
            n = _num(str(v if v is not None else ""), "Price", 0)
            out[col] = None if not n else round(n, 2)
        elif kind == "stock":
            out[col] = None if v is None or v == "" else (1 if v in (True, 1, "1", "Y", "y") else 0)
        elif kind == "pack":
            out[col] = clean_pack(v)
        elif kind == "compare":
            rows = []
            for c in (v or [])[:MAX_COMPARE]:
                lbl = str((c or {}).get("label", "")).strip()[:30]
                n = _num(str((c or {}).get("price", "") or ""), f"“{lbl or 'compare'}” price", 0)
                if lbl and n:
                    rows.append([lbl, round(n, 2)])
            out[col] = json.dumps(rows) if rows else ""
        else:
            out[col] = str(v if v is not None else "").strip()[:kind]
    if "sku" in out and not out["sku"]:
        raise SheetError("The part number can't be empty.")
    if "name" in out and not out["name"]:
        out["name"] = out.get("sku") or ""
        if not out["name"]:
            raise SheetError("The name can't be empty.")
    return out


def _shown(col, v):
    if col == "compare":
        return ", ".join(f"{lbl} {p:.2f}" for lbl, p in json.loads(v)) if v else ""
    if col == "stock":
        return {1: "stocked", 0: "non-stock"}.get(v, "not marked")
    return v


def update_item(item_id: int, body: dict, who: str):
    """Change a live item. Returns (vendor, sku, name, {field: {from, to}}); raises SheetError / LookupError."""
    c = conn()
    old = c.execute("SELECT i.*, s.vendor AS sv FROM pl_items i JOIN pl_sheets s ON s.id=i.sheet_id"
                    " WHERE i.id=? AND s.active=1", (item_id,)).fetchone()
    if not old:
        raise LookupError("That item isn't on a live sheet any more. Reload the Price List.")
    new = clean_fields(body, partial=True)
    changes = {col: {"from": _shown(col, old[col]), "to": _shown(col, val)} for col, val in new.items() if old[col] != val}
    if changes:
        cols = [col for col in new if col in changes]
        c.execute(f"UPDATE pl_items SET {', '.join(col + '=?' for col in cols)}, edited_at=?, edited_by=? WHERE id=?",
                  [new[col] for col in cols] + [now_iso(), who, item_id])
    return old["sv"], old["sku"], old["name"], changes


def add_item(sheet_id: int, body: dict, who: str) -> int:
    c = conn()
    s = c.execute("SELECT * FROM pl_sheets WHERE id=? AND active=1", (sheet_id,)).fetchone()
    if not s:
        raise LookupError("Pick a live sheet to add the item to.")
    d = clean_fields(body, partial=False)
    cols = list(d)
    cur = c.execute(f"INSERT INTO pl_items(sheet_id, vendor, {', '.join(cols)}, edited_at, edited_by)"
                    f" VALUES (?,?,{','.join('?' * len(cols))},?,?)", [sheet_id, s["vendor"]] + [d[k] for k in cols] + [now_iso(), who])
    c.execute("UPDATE pl_sheets SET items=items+1 WHERE id=?", (sheet_id,))
    return cur.lastrowid


def delete_item(item_id: int):
    """Delete a live item. Returns its row (with the sheet's vendor and label), or None if it wasn't live."""
    c = conn()
    r = c.execute("SELECT i.*, s.vendor AS sv, s.label AS sheet_label FROM pl_items i JOIN pl_sheets s ON s.id=i.sheet_id"
                  " WHERE i.id=? AND s.active=1", (item_id,)).fetchone()
    if r:
        c.execute("DELETE FROM pl_items WHERE id=?", (item_id,))
        c.execute("UPDATE pl_sheets SET items=MAX(items-1, 0) WHERE id=?", (r["sheet_id"],))
    return r


def sheet_csv(sheet_id: int):
    """A live sheet, with any edits, in the upload format (so it can be changed in Excel and loaded back).
    Returns (sheet row, CSV text) or (None, "")."""
    c = conn()
    s = c.execute("SELECT * FROM pl_sheets WHERE id=? AND active=1", (sheet_id,)).fetchone()
    if not s:
        return None, ""
    rows = c.execute("SELECT * FROM pl_items WHERE sheet_id=? ORDER BY id", (sheet_id,)).fetchall()
    labels: list[str] = []
    for r in rows:
        for lbl, _ in json.loads(r["compare"] or "[]"):
            if lbl not in labels:
                labels.append(lbl)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(CSV_COLUMNS) + [COMPARE_PREFIX + lbl for lbl in labels[:MAX_COMPARE]])
    num = lambda v: ("%g" % v) if v else ""  # noqa: E731
    for r in rows:
        cmp = dict((lbl, p) for lbl, p in json.loads(r["compare"] or "[]"))
        w.writerow([r["sku"], r["name"], r["cat"], "" if r["price"] is None else f"{r['price']:.2f}", r["grp"], r["mfr"],
                    num(r["w"]), num(r["h"]), r["th"], r["core"], {1: "Y", 0: "N"}.get(r["stock"], ""), r["uom"],
                    r["hand"], r["brand"], "" if r["page"] is None else r["page"], r["flag"], r["pack"]]
                   + [f"{cmp[lbl]:.2f}" if lbl in cmp else "" for lbl in labels[:MAX_COMPARE]])
    return s, buf.getvalue()


# ------------------------------------------------------------------ purchase orders
def line_calc(item: dict, qty: int) -> dict:
    price = item["price"]
    base = round((price or 0) * qty, 2)
    sur = round(base * NONSTOCK_SURCHARGE, 2) if (item["stock"] == 0 and item["cat"] in NONSTOCK_CATS
                                                  and qty < NONSTOCK_MIN_QTY and price) else 0.0
    return {"base": base, "surcharge": sur, "total": round(base + sur, 2)}


def size_label(w: float, h: float) -> str:
    if not w or not h:
        return ""
    def ft(x):
        x = int(round(x))
        return f"{x // 12}/{x % 12}"
    hh = {80: "6'8\"", 96: "8'0\"", 84: "7'0\""}.get(int(round(h)), ft(h).replace("/", "'") + '"')
    return f"{ft(w)} × {hh} ({int(round(w))}×{int(round(h))})"


def build_lines(vendor: str, wanted: list) -> tuple[list[dict], float, str]:
    """wanted: [{item_id, qty}] from the phone. Prices come from the vendor's live sheets only."""
    if not live_sheets(vendor):
        raise ValueError("No price sheet is loaded for this vendor.")
    if not wanted:
        raise ValueError("The buy list is empty.")
    if len(wanted) > MAX_LINES:
        raise ValueError(f"A PO can have at most {MAX_LINES} lines.")
    c = conn()
    qty_by_id: dict[int, int] = {}
    for w in wanted:
        try:
            iid, qty = int(w.get("item_id")), int(w.get("qty"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("A line on the buy list isn't readable. Remove it and add it again.") from None
        if not 1 <= qty <= MAX_QTY:
            raise ValueError(f"Quantities must be 1 to {MAX_QTY}.")
        qty_by_id[iid] = qty_by_id.get(iid, 0) + qty
    lines, total, labels = [], 0.0, []
    for iid, qty in qty_by_id.items():
        r = c.execute("SELECT i.*, s.label AS sheet_label FROM pl_items i JOIN pl_sheets s ON s.id=i.sheet_id"
                      " WHERE i.id=? AND s.vendor=? AND s.active=1", (iid, vendor)).fetchone()
        if not r:
            raise ValueError("An item on the buy list isn't on the current price sheet any more. Remove it and find it again.")
        it = dict(r)
        if it["sheet_label"] not in labels:
            labels.append(it["sheet_label"])
        calc = line_calc(it, min(qty, MAX_QTY))
        lines.append({"item_id": iid, "sku": it["sku"], "name": it["name"], "size": size_label(it["w"], it["h"]),
                      "qty": min(qty, MAX_QTY), "price": it["price"], "uom": it["uom"], "surcharge": calc["surcharge"],
                      "total": calc["total"] if it["price"] is not None else None, "flag": it["flag"],
                      **({"pack": it["pack"], "not_full_packs": True} if not pack_ok(min(qty, MAX_QTY), it["pack"]) else {})})
        total += calc["total"] if it["price"] is not None else 0
    return lines, round(total, 2), " · ".join(labels)


def po_out(r, with_lines=False) -> dict:
    d = {"id": r["id"], "po_number": r["po_number"], "vendor": r["vendor"], "job_number": r["job_number"],
         "job_customer": r["job_customer"], "order_date": r["order_date"], "ship_method": r["ship_method"],
         "ship_to": r["ship_to"], "ship_address": r["ship_address"], "notes": r["notes"], "total": r["total"],
         "sheet_label": r["sheet_label"], "sent_to": r["sent_to"], "created_at": r["created_at"],
         "is_test": bool(r["is_test"]),
         "by": r["staff_name"] if "staff_name" in r.keys() else ""}
    lines = json.loads(r["lines"])
    d["line_count"] = len(lines)
    if with_lines:
        d["lines"] = lines
    return d


def recent_pos(limit=50) -> list[dict]:
    rows = conn().execute("SELECT p.*, s.name AS staff_name FROM pl_pos p JOIN staff s ON s.id=p.staff_id "
                          "ORDER BY p.id DESC LIMIT ?", (limit,)).fetchall()
    return [po_out(r) for r in rows]


def get_po(pid: int):
    return conn().execute("SELECT p.*, s.name AS staff_name FROM pl_pos p JOIN staff s ON s.id=p.staff_id WHERE p.id=?",
                          (pid,)).fetchone()


def sent_before(po_number: str) -> list[dict]:
    if not po_number:
        return []
    rows = conn().execute("SELECT id, order_date, created_at FROM pl_pos WHERE po_number=? AND is_test=0 ORDER BY id",
                          (po_number,))
    return [dict(r) for r in rows]


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", s or "")[:40] or "PO"

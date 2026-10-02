"""PDF copy of a report (attached to the email, downloadable from the admin screen)."""
import io
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, KeepTogether
from xml.sax.saxutils import escape

from . import measure as M
from .forms import FORMS, display_rows, photo_slots

GREEN = colors.HexColor("#76c043")
MS_SIDE, MS_TOP, MS_BOTTOM = 0.4 * inch, 0.35 * inch, 0.5 * inch
LOGO = os.path.join(os.path.dirname(__file__), "static", "logo.png")
TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "America/Chicago"))


def local_time(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(TZ)
    return d.strftime("%b %-d, %Y %-I:%M %p")


def _img(path, max_w, max_h):
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        w, h = im.size
    s = min(max_w / w, max_h / h)
    return Image(path, width=w * s, height=h * s)


def _caption(p, labels, fallback_time, data=None) -> str:
    from . import geo
    keys = p.keys()
    g = {"status": (p["geo_status"] if "geo_status" in keys else None) or "missing",
         "lat": p["lat"] if "lat" in keys else None, "lon": p["lon"] if "lon" in keys else None,
         "acc": p["acc"] if "acc" in keys else None,
         "taken_at": (p["taken_at"] if "taken_at" in keys else None) or fallback_time,
         "file_age": p["file_age"] if "file_age" in keys else None}
    if g["status"] == "signature":
        who = str((data or {}).get("received_by") or "").strip()
        return "<b>" + escape(labels.get(p["slot"], p["slot"])) + "</b>" + (f" · Signed by {escape(who)}" if who else "") \
            + " · " + escape(geo.local(g["taken_at"]))
    text = "<b>" + escape(labels.get(p["slot"], p["slot"])) + "</b> · " + " · ".join(escape(x) for x in geo.describe(g))
    if g["status"] == "ok":
        text += f' · <a href="{geo.map_url(g["lat"], g["lon"])}" color="#2a7ab0">View on map</a>'
    return text


def build_pdf(report, staff_name: str, data: dict, photos: list) -> bytes:
    """report: row from reports; photos: list of rows from photos (slot, path)."""
    form_type = report["form_type"]
    buf = io.BytesIO()
    tight = FORMS.get(form_type, {}).get("kind") == "measure"     # measure: thin margins, the photos need the room
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=(MS_SIDE if tight else 0.6 * inch),
                            rightMargin=(MS_SIDE if tight else 0.6 * inch),
                            topMargin=(MS_TOP if tight else 0.5 * inch), bottomMargin=(MS_BOTTOM if tight else 0.6 * inch),
                            title=f"{form_type} {report['receipt']}")
    ss = getSampleStyleSheet()
    alarm = FORMS.get(form_type, {}).get("confidential") or data.get("defective")
    accent = colors.HexColor("#c62828") if alarm else GREEN
    h1 = ParagraphStyle("h1", parent=ss["Title"], alignment=0, textColor=accent, fontSize=(15 if tight else 20),
                        leading=(17 if tight else 24), spaceAfter=(0 if tight else 2), spaceBefore=0)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=9, textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=10, leading=13)
    lab = ParagraphStyle("lab", parent=cell, fontName="Helvetica-Bold")
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=GREEN, fontSize=13)

    story = []
    title = form_type.upper() + (" — CONFIDENTIAL" if FORMS.get(form_type, {}).get("confidential") else "")
    head_left = [Paragraph(escape(title), h1),
                 Paragraph(f"Receipt <b>{escape(report['receipt'])}</b> &nbsp;·&nbsp; "
                           f"Received {escape(local_time(report['submitted_at']))}"
                           + (" (sent from phone's offline queue)" if report["queued_on_phone"] else ""), small)]
    logo = _img(LOGO, 1.2 * inch if tight else 1.6 * inch, 0.38 * inch if tight else 0.6 * inch) if os.path.exists(LOGO) else ""
    t = Table([[head_left, logo]], colWidths=[doc.width - 1.9 * inch, 1.9 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                           ("LINEBELOW", (0, 0), (-1, 0), 2, accent), ("BOTTOMPADDING", (0, 0), (-1, 0), 4 if tight else 8),
                           ("TOPPADDING", (0, 0), (-1, 0), 0)]))
    story += [t, Spacer(1, 4 if tight else 10)]

    if FORMS.get(form_type, {}).get("kind") == "measure":
        _measure_body(story, report, staff_name, data, photos, cell, lab, small, h2)
        extra = " · ".join(x for x in (data.get("customer"), f"PO {data['po']}" if data.get("po") else "") if x)
        doc.build(story, onFirstPage=_footer(form_type, report, extra), onLaterPages=_footer(form_type, report, extra))
        return buf.getvalue()

    rows = [("Submitted by", staff_name)] + display_rows(form_type, data)
    tbl = Table([[Paragraph(escape(a), lab), Paragraph(escape(str(b)).replace("\n", "<br/>"), cell)] for a, b in rows],
                colWidths=[2.3 * inch, 5.0 * inch], splitInRow=1)
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
                             ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f9eb")),
                             ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]
                            + [("BACKGROUND", (1, i), (1, i), colors.HexColor("#fdecea"))
                               for i, (a, b) in enumerate(rows) if b == "DEFECTIVE" or (a == "Defective items" and b != "None")]))
    story.append(tbl)

    slots = {s["slot"]: s for s in photo_slots(form_type, data)} if form_type in FORMS else {}
    labels = {k: v["label"] for k, v in slots.items()}
    groups = {}
    for p in photos:
        title = slots.get(p["slot"], {}).get("group", "Photos")
        groups.setdefault(title, []).append(p)
    for title, items in groups.items():
        story.append(Spacer(1, 14))
        story.append(Paragraph(escape(title), h2))
        for p in items:
            if not os.path.exists(p["path"]):
                story.append(Paragraph(f"(missing file for {escape(p['slot'])})", small))
                continue
            is_sig = slots.get(p["slot"], {}).get("signature")
            story.append(KeepTogether([_img(p["path"], 3.2 * inch if is_sig else 7.2 * inch, 1.4 * inch if is_sig else 4.6 * inch),
                                       Paragraph(_caption(p, labels, report["submitted_at"], data), small), Spacer(1, 8)]))

    footer = _footer(form_type, report)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _footer(form_type, report, extra=""):
    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(0.6 * inch, 0.35 * inch, f"SimplyDoors Operations · {form_type} · {report['receipt']}"
                          + (f" · {extra[:80]}" if extra else ""))
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.35 * inch, f"Page {d.page}")
        canvas.restoreState()
    return footer


def _kv(rows, cell, lab, widths=(2.0, 5.3), shade=None):
    t = Table([[Paragraph(escape(a), lab), Paragraph(escape(str(b)).replace("\n", "<br/>"), cell)] for a, b in rows],
              colWidths=[widths[0] * inch, widths[1] * inch], splitInRow=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f9eb")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)] + (shade or [])))
    return t


def _aspect(path) -> float:
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        return im.size[1] / im.size[0]


def _spec_table(rows, style, W, grey):
    if not rows:
        return None
    lw = 0.82 * inch
    vw = (W - 3 * lw) / 3
    sp = Table(rows, colWidths=[lw, vw, lw, vw, lw, vw])
    sp.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)] + style))
    return sp


def _measure_body(story, report, staff_name, data, photos, cell, lab, small, h2):
    """One-line job strip, then each opening in a fixed half-page block (two per page, page 1 included):
    title bar with the size in big type, a compact two-column spec grid, then the photos as large as fit."""
    from reportlab.platypus.flowables import HRFlowable
    from . import geo
    W = letter[0] - 2 * MS_SIDE - 12                                # page frames keep 6pt padding on every side
    HALF = (letter[1] - MS_TOP - MS_BOTTOM - 12) / 2 - 2            # exactly half the usable page
    red, grey = colors.HexColor("#c62828"), colors.HexColor("#dddddd")
    lab7 = ParagraphStyle("lab7", parent=lab, fontSize=7.5, leading=9, textColor=colors.HexColor("#5f6b76"))
    val9 = ParagraphStyle("val9", parent=cell, fontSize=9.5, leading=11.5)
    cap = ParagraphStyle("cap", parent=small, fontSize=7.5, leading=9, alignment=1)
    ttl = ParagraphStyle("ttl", parent=lab, fontSize=12.5, leading=15)
    big = ParagraphStyle("big", parent=lab, fontSize=13, leading=15, alignment=2)

    # job details: one compact strip, label above value
    job = [(a, b) for a, b in M.job_rows(data)]
    if staff_name != data.get("measured_by") and staff_name != data.get("revised_by"):
        job.insert(4, ("Sent by", staff_name))
    jl = ParagraphStyle("jl", parent=lab7, fontSize=6.5, leading=8)
    jv = ParagraphStyle("jv", parent=val9, fontSize=10, leading=12, fontName="Helvetica-Bold")
    weight = {"Customer": 2.0, "REVISED": 2.0, "Items": 1.8}
    tot = sum(weight.get(a, 1.4) for a, _ in job)
    jt = Table([[[Paragraph(escape(a.upper()), jl), Paragraph(escape(str(b)), jv)] for a, b in job]],
               colWidths=[W * weight.get(a, 1.4) / tot for a, _ in job])
    jt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, grey), ("LINEBEFORE", (1, 0), (-1, 0), 0.6, grey),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f7f9f8")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
                           + [("BACKGROUND", (c, 0), (c, 0), colors.HexColor("#fff4d6")) for c, (a, _) in enumerate(job) if a == "REVISED"]))
    story += [jt, Spacer(1, 5)]
    items = data.get("items") or []
    # page 1 has the header, so its two openings share what's left; later pages split the full page
    head_h = sum(f.wrap(W, 2000)[1] for f in story)
    FIRST_HALF = (letter[1] - MS_TOP - MS_BOTTOM - 12 - head_h) / 2 - 2

    slots = {s["slot"]: s for s in M.photo_slots(data)}
    by_item: dict[int, list] = {}
    for p in photos:
        if p["slot"] in slots and os.path.exists(p["path"]):
            by_item.setdefault(slots[p["slot"]]["item"], []).append(p)

    def divider():
        return [Spacer(1, 4), HRFlowable(width="100%", thickness=0.6, color=grey, dash=(3, 3)), Spacer(1, 4)]

    def fit(head, build, target, start):
        """Grow or shrink the photos until head + photos + divider fill `target` points of height."""
        h = start
        for _ in range(4):
            body = build(h)
            total = sum(f.wrap(W, 5000)[1] for f in head + [body] + divider())
            gap = target - total
            if -0.5 <= gap <= 1.5:
                break
            h = max(0.9 * inch, h + gap - 0.5)
        if total > target:            # never spill past half a page
            body = build(max(0.9 * inch, h + (target - total) - 1))
        return head + [body] + divider()

    for i, it in enumerate(items):
        half = FIRST_HALF if i < 2 else HALF
        # title bar: name + location on the left, the size big on the right
        if it["type"] == "door":
            size = " × ".join(x for x in (M.fmt_size(it.get("w")) + " W" if it.get("w", {}).get("w") else "",
                                          M.fmt_size(it.get("h")) + " H" if it.get("h", {}).get("w") else "") if x)
            sub = it.get("dim_type") or ""
        else:
            size = ", ".join(f'{M.fmt_size(p["w"])} × {M.fmt_size(p["h"])}' for p in it.get("points") or [])
            sub = " · ".join(x for x in (it.get("m_type") or "", f"Qty {it.get('qty')}" if str(it.get("qty") or "1") != "1" else "") if x)
        left = f"<b>{escape(M.item_name(data, i).upper())}</b>" + (f" &nbsp;{escape(it['loc'])}" if it.get("loc") else "")
        if it.get("tempered"):
            left += ' &nbsp;<font color="#c62828"><b>TEMPERED</b></font>'
        right = escape(size) + (f'<br/><font size="8" color="#5f6b76">{escape(sub)}</font>' if sub else "")
        tb = Table([[Paragraph(left, ttl), Paragraph(right, big)]], colWidths=[W * 0.54, W * 0.46])
        tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef6e6") if it["type"] == "door" else colors.HexColor("#e8f1f8")),
                                ("LINEBEFORE", (0, 0), (0, 0), 4, GREEN if it["type"] == "door" else colors.HexColor("#4a90c2")),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))

        # specs: two label/value pairs per row; long ones (notes, multiple sizes) across the full width
        specs = [(a, b) for a, b in M.item_rows(it) if a not in ("Width", "Height")]
        if it["type"] == "window" and len(it.get("points") or []) <= 1:
            specs = [(a, b) for a, b in specs if not a.startswith("Measurements")]
        short_label = {"Measurements (W × H)": "Sizes (W × H)", "Wall thickness": "Wall thick."}
        specs = [(short_label.get(a, a), " · ".join(b.split("\n")) if a.startswith("Measurements") else b) for a, b in specs]
        short = [(a, b) for a, b in specs if a not in ("Notes",) and "\n" not in b and len(b) <= 26]
        long_ = [(a, b) for a, b in specs if (a, b) not in short]
        rows, style = [], []
        for k in range(0, len(short), 3):
            trio = short[k:k + 3]
            row = []
            for a, b in trio:
                row += [Paragraph(escape(a), lab7), Paragraph(escape(b), val9)]
                if a == "Tempered":
                    style.append(("TEXTCOLOR", (len(row) - 1, len(rows)), (len(row) - 1, len(rows)), red))
            rows.append(row + [""] * (6 - len(row)))
        for a, b in long_:
            style.append(("SPAN", (1, len(rows)), (5, len(rows))))
            rows.append([Paragraph(escape(a), lab7), Paragraph(escape(b).replace("\n", "<br/>"), val9), "", "", "", ""])
        pics = sorted(by_item.get(i, []), key=lambda x: x["slot"])
        labels = M.PHOTO_LABELS[it["type"]]

        def photo_cell(p, max_w, max_h, n):
            k = int(p["slot"].rsplit("p", 1)[1])
            g = {"status": p["geo_status"] or "missing", "lat": p["lat"], "lon": p["lon"], "acc": p["acc"],
                 "taken_at": p["taken_at"] or report["submitted_at"], "file_age": p["file_age"]}
            # full time/location is printed on the photo itself; keep the caption to one short line
            where = f' · <a href="{geo.map_url(g["lat"], g["lon"])}" color="#2a7ab0">map</a>' if g["status"] == "ok" else ""
            when = " ".join(geo.local(g["taken_at"]).split()[3:5])          # "7:24 PM"
            return [_img(p["path"], max_w, max_h), Paragraph(f"<b>{escape(labels[k - 1])}</b> · {escape(when)}{where}", cap)]

        # 1-2 photos: specs on the left, photos on the right, so the photos can use the full block height
        if 0 < len(pics) <= 2:
            one = [[Paragraph(escape(a), lab7), Paragraph(escape(b).replace("\n", "<br/>"), val9)] for a, b in specs]
            left = Table(one or [[Paragraph("—", val9), ""]], colWidths=[0.95 * inch, 2.15 * inch])
            left.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                      ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
            lh = left.wrap(3.1 * inch, 2000)[1]
            avail = half - tb.wrap(W, 2000)[1] - 30
            # how tall the photos could get beside the specs vs. in a row under them; use whichever is bigger
            aspect = max(_aspect(p["path"]) for p in pics)
            side_h = min(avail, ((W - 3.2 * inch) / len(pics) - 8) * aspect)
            spec_h = (sp_preview.wrap(W, 2000)[1] if (sp_preview := _spec_table(rows, style, W, grey)) else 0)
            row_h = min(half - tb.wrap(W, 2000)[1] - spec_h - 30, (W / len(pics) - 6) * aspect)
            if lh <= avail and side_h >= row_h:
                n = len(pics)
                pw = (W - 3.2 * inch) / n

                def build_side(mh, n=n, pw=pw, left=left, pics=pics):
                    ptab = Table([[photo_cell(p, pw - 8, mh, n) for p in pics]], colWidths=[pw] * n)
                    ptab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                              ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
                    both = Table([[left, ptab]], colWidths=[3.2 * inch, W - 3.2 * inch])
                    both.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (0, 0), 4)]))
                    return both
                story.append(KeepTogether(fit([tb], build_side, half, avail)))
                continue

        block = [tb]
        if rows:
            block.append(_spec_table(rows, style, W, grey))
        used = sum(f.wrap(W, 2000)[1] for f in block)

        # photos: one row, grown until the block fills its half of the page
        if pics:
            n = len(pics)
            col = W / n

            def build_row(mh, n=n, col=col, pics=pics):
                pt = Table([[photo_cell(p, col - 6, mh, n) for p in pics]], colWidths=[col] * n)
                pt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                        ("TOPPADDING", (0, 0), (-1, -1), 5)]))
                return pt
            story.append(KeepTogether(fit(block, build_row, half, half - used - 30)))
            continue
        block.append(Paragraph("No photos for this one.", small))
        story.append(KeepTogether(block + [Spacer(1, 6), HRFlowable(width="100%", thickness=0.6, color=grey, dash=(3, 3)), Spacer(1, 6)]))

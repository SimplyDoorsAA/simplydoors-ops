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

from .forms import FORMS, display_rows

GREEN = colors.HexColor("#76c043")
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


def _caption(p, labels, fallback_time) -> str:
    from . import geo
    keys = p.keys()
    g = {"status": (p["geo_status"] if "geo_status" in keys else None) or "missing",
         "lat": p["lat"] if "lat" in keys else None, "lon": p["lon"] if "lon" in keys else None,
         "acc": p["acc"] if "acc" in keys else None,
         "taken_at": (p["taken_at"] if "taken_at" in keys else None) or fallback_time,
         "file_age": p["file_age"] if "file_age" in keys else None}
    text = "<b>" + escape(labels.get(p["slot"], p["slot"])) + "</b> · " + " · ".join(escape(x) for x in geo.describe(g))
    if g["status"] == "ok":
        text += f' · <a href="{geo.map_url(g["lat"], g["lon"])}" color="#2a7ab0">View on map</a>'
    return text


def build_pdf(report, staff_name: str, data: dict, photos: list) -> bytes:
    """report: row from reports; photos: list of rows from photos (slot, path)."""
    form_type = report["form_type"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.6 * inch,
                            title=f"{form_type} {report['receipt']}")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], alignment=0, textColor=GREEN, fontSize=20, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=9, textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=10, leading=13)
    lab = ParagraphStyle("lab", parent=cell, fontName="Helvetica-Bold")
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=GREEN, fontSize=13)

    story = []
    head_left = [Paragraph(escape(form_type.upper()), h1),
                 Paragraph(f"Receipt <b>{escape(report['receipt'])}</b> &nbsp;·&nbsp; "
                           f"Received {escape(local_time(report['submitted_at']))}"
                           + (" (sent from phone's offline queue)" if report["queued_on_phone"] else ""), small)]
    logo = _img(LOGO, 1.6 * inch, 0.6 * inch) if os.path.exists(LOGO) else ""
    t = Table([[head_left, logo]], colWidths=[5.4 * inch, 1.9 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                           ("LINEBELOW", (0, 0), (-1, 0), 2, GREEN), ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))
    story += [t, Spacer(1, 10)]

    rows = [("Submitted by", staff_name)] + display_rows(form_type, data)
    tbl = Table([[Paragraph(escape(a), lab), Paragraph(escape(str(b)).replace("\n", "<br/>"), cell)] for a, b in rows],
                colWidths=[2.3 * inch, 5.0 * inch], splitInRow=1)
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
                             ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f9eb")),
                             ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(tbl)

    labels = dict(FORMS[form_type]["photos"])
    groups = {}
    for p in photos:
        key = "".join(ch for ch in p["slot"] if not ch.isdigit())
        groups.setdefault(key, []).append(p)
    titles = {"ticket": "Receiving tickets", "product": "Product condition", "photo": "Photos"}
    for key, items in groups.items():
        story.append(Spacer(1, 14))
        story.append(Paragraph(titles.get(key, key.title()), h2))
        for p in items:
            if not os.path.exists(p["path"]):
                story.append(Paragraph(f"(missing file for {escape(p['slot'])})", small))
                continue
            story.append(KeepTogether([_img(p["path"], 7.2 * inch, 4.4 * inch),
                                       Paragraph(_caption(p, labels, report["submitted_at"]), small), Spacer(1, 8)]))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(0.6 * inch, 0.35 * inch, f"SimplyDoors Operations · {form_type} · {report['receipt']}")
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.35 * inch, f"Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()

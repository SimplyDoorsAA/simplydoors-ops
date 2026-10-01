"""Photo time + location stamps.

The phone sends, for each photo, where it was and when it was added (only while the
app is open; nothing is tracked in the background). The server checks those values,
saves them with the photo, and prints them in a bar along the bottom of the picture,
so the stamp travels with the photo into the PDF and email.
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

TZ = ZoneInfo(os.environ.get("TZ_DISPLAY", "America/Chicago"))
STATUSES = {"ok", "denied", "off", "unavailable", "timeout", "unsupported", "missing"}
OLD_PHOTO_SECONDS = 600   # file is more than 10 minutes old when added -> probably from the gallery

STATUS_TEXT = {
    "denied": "No location (location blocked on this phone)",
    "off": "No location (location not turned on in the app)",
    "unavailable": "No location (phone couldn't find a GPS signal)",
    "timeout": "No location (GPS took too long)",
    "unsupported": "No location (this browser doesn't share location)",
    "missing": "No location",
}


def parse_geo(raw: str | None, fallback_time: str) -> dict:
    """Validate what the phone sent. Anything odd becomes 'missing'; never raises."""
    out = {"status": "missing", "lat": None, "lon": None, "acc": None, "taken_at": fallback_time, "file_age": None}
    try:
        g = json.loads(raw or "{}")
        if not isinstance(g, dict):
            return out
        at = str(g.get("at", ""))[:40]
        if at:
            d = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            out["taken_at"] = d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fa = g.get("fileAge")
        if isinstance(fa, (int, float)) and 0 <= fa < 10 ** 9:
            out["file_age"] = int(fa)
        status = str(g.get("status", "missing"))
        if status == "ok":
            lat, lon, acc = float(g["lat"]), float(g["lon"]), float(g.get("acc") or 0)
            if -90 <= lat <= 90 and -180 <= lon <= 180 and 0 <= acc <= 100000:
                out.update(status="ok", lat=round(lat, 6), lon=round(lon, 6), acc=round(acc))
        elif status in STATUSES:
            out["status"] = status
    except Exception:
        pass
    return out


def local(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(TZ)
    return d.strftime("%b %-d, %Y %-I:%M %p %Z")


def map_url(lat, lon) -> str:
    return f"https://maps.google.com/?q={lat},{lon}"


def describe(geo: dict) -> list[str]:
    """Plain-English lines used on the stamp, in the PDF and on the admin screen."""
    lines = [f"Added {local(geo['taken_at'])}"]
    if geo["status"] == "ok":
        acc = geo["acc"]
        where = f"{geo['lat']:.5f}, {geo['lon']:.5f}  (within {acc} m)"
        if acc and acc > 200:
            where += "  LOW ACCURACY"
        lines.append(where)
    else:
        lines.append(STATUS_TEXT.get(geo["status"], "No location"))
    if geo.get("file_age") is not None and geo["file_age"] > OLD_PHOTO_SECONDS:
        lines.append("Older photo, likely picked from the gallery")
    return lines


def stamp(im: Image.Image, geo: dict, receipt: str, who: str) -> Image.Image:
    """Print the stamp in a dark bar along the bottom of the photo."""
    w, h = im.size
    size = max(18, max(w, h) // 42)   # sized from the long side so upright photos get a readable stamp
    font = ImageFont.load_default(size=size)
    lines = describe(geo) + [f"SimplyDoors {receipt} · {who}"]
    pad = size // 2
    line_h = int(size * 1.3)
    bar_h = pad * 2 + line_h * len(lines)
    out = Image.new("RGB", (w, h + bar_h), (20, 24, 28))
    out.paste(im, (0, 0))
    d = ImageDraw.Draw(out)
    y = h + pad
    for i, line in enumerate(lines):
        color = (255, 214, 102) if ("No location" in line or "LOW" in line or "Older photo" in line) else (255, 255, 255)
        d.text((pad, y), line, fill=color if i < len(lines) - 1 else (170, 178, 186), font=font)
        y += line_h
    return out

"""Push alerts to Adem's phone through the ntfy server already running on the OptiPlex."""
import os
import threading
import urllib.request

NTFY_URL = os.environ.get("NTFY_URL", "")  # e.g. http://100.101.142.26:8080/adem-alerts ; empty = off
_last = {}
_lock = threading.Lock()


def push_throttled(key: str, title: str, message: str, priority: str = "default", every_seconds: int = 600) -> None:
    """Same kind of alert at most once per window (e.g. lockouts), so the phone isn't flooded."""
    import time
    with _lock:
        now = time.time()
        if now - _last.get(key, 0) < every_seconds:
            return
        _last[key] = now
    push(title, message, priority)


def push(title: str, message: str, priority: str = "default", click: str = "") -> None:
    """click: a link the phone opens when the alert is tapped (e.g. the new lead)."""
    if not NTFY_URL:
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": "door"}
    if click:
        headers["Click"] = click.encode("ascii", "ignore").decode()

    def _send():
        try:
            req = urllib.request.Request(NTFY_URL, data=message.encode("utf-8"), method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass  # an alert failing must never break the app

    threading.Thread(target=_send, daemon=True).start()

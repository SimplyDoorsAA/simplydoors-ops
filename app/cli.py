"""Command-line helpers, run inside the container:

  docker exec -it opsapp python -m app.cli set-pin "Adem Atis"
  docker exec opsapp python -m app.cli import-pins /data/import/pins.csv
  docker exec opsapp python -m app.cli staff
  docker exec opsapp python -m app.cli set-owner "Adem Atis"
"""
import csv
import getpass
import os
import shutil
import sqlite3
import sys
import time

from . import auth
from .db import AUDIT_DELETE_TRIGGER, DATA_DIR, DB_PATH, audit, conn, get_setting, init_db, set_setting


def set_pin(name: str) -> int:
    row = conn().execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()
    if not row:
        print(f"No staff member named '{name}'.")
        return 1
    for _ in range(3):
        pin = getpass.getpass(f"New PIN for {name} (6-8 digits, hidden): ").strip()
        again = getpass.getpass("Type it again: ").strip()
        if pin != again:
            print("They didn't match. Try again.")
            continue
        if not auth.valid_pin_format(pin):
            print("PIN must be 4 to 8 digits.")
            continue
        auth.set_pin(row["id"], pin, "install")
        audit(None, "server console", "pin_set_console", name)
        print(f"PIN set for {name}.")
        return 0
    return 1


def import_pins(path: str) -> int:
    """Old Sheet 'PINs' tab exported as CSV: column A = name, column B = PIN.
    Fills in only people who don't have a PIN in the new app yet. Deletes the file afterwards."""
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        return 1
    c = conn()
    done, skipped_has_pin, unknown, bad = [], [], [], []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for i, row in enumerate(csv.reader(f)):
                if len(row) < 2:
                    continue
                name, pin = row[0].strip(), row[1].strip()
                if i == 0 and not pin.isdigit():
                    continue  # header row
                if not name:
                    continue
                st = c.execute("SELECT id, pin_hash FROM staff WHERE name=?", (name,)).fetchone()
                if not st:
                    unknown.append(name)
                    continue
                if st["pin_hash"]:
                    skipped_has_pin.append(name)
                    continue
                if not auth.valid_pin_format(pin):
                    bad.append(name)
                    continue
                auth.set_pin(st["id"], pin, "import")
                done.append(name)
    finally:
        try:
            os.remove(path)
            removed = True
        except OSError:
            removed = False
    audit(None, "server console", "pins_imported", None,
          {"imported": done, "kept_existing": skipped_has_pin, "not_in_staff_list": unknown, "invalid_pin": bad})
    print(f"Imported PINs for {len(done)} people: {', '.join(done) or '-'}")
    if skipped_has_pin:
        print(f"Kept their new-app PIN (not overwritten): {', '.join(skipped_has_pin)}")
    if unknown:
        print(f"Names in the file that aren't in the staff list (skipped): {', '.join(unknown)}")
    if bad:
        print(f"PIN in the file isn't 4-8 digits (skipped, set by hand): {', '.join(bad)}")
    missing = [r["name"] for r in c.execute("SELECT name FROM staff WHERE active=1 AND pin_hash IS NULL")]
    if missing:
        print(f"Still without a PIN: {', '.join(missing)}")
    print("The CSV file was deleted." if removed else f"WARNING: could not delete {path}; delete it by hand.")
    return 0


def list_staff() -> int:
    for r in conn().execute("SELECT name, dept, is_admin, is_owner, pin_hash IS NOT NULL AS has_pin FROM staff ORDER BY dept, name"):
        print(f"{r['dept']:<20} {r['name']:<20} {'owner' if r['is_owner'] else 'admin' if r['is_admin'] else '':<6} {'PIN set' if r['has_pin'] else 'no PIN'}")
    return 0


def set_owner(name: str) -> int:
    """Move app ownership to someone else (there is only ever one owner)."""
    c = conn()
    row = c.execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()
    if not row:
        print(f"No one called {name!r}. Names are listed with: python -m app.cli staff")
        return 1
    c.execute("UPDATE staff SET is_owner=0")
    c.execute("UPDATE staff SET is_owner=1, is_admin=1 WHERE id=?", (row["id"],))
    audit(None, "server console", "owner_set", name)
    print(f"{name} is now the app owner.")
    return 0


RESET_DIR = os.path.join(DATA_DIR, "before-reset")


def reset_test_data(again: bool = False) -> int:
    """One-time clean slate before go-live. Wipes every report, photo, email and the
    activity log, so numbering starts again at 00001. Keeps staff, PINs, sign-ins,
    email lists, dropdown lists and settings. A full copy of everything is kept in
    opsapp-data/before-reset/ first. Server console only - there is no button for this."""
    c = conn()
    done = get_setting("test_reset_done")
    if done and not again:
        print(f"This was already done on {done}. It is meant to run once, before go-live,")
        print("so it won't run again by accident and wipe real reports.")
        print("If you really mean it, run:  python -m app.cli reset-test-data --again")
        return 1
    n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("reports", "photos", "emails", "audit")}
    print("This permanently clears, from the app:")
    print(f"  {n['reports']} reports, {n['photos']} photos, {n['emails']} emails, {n['audit']} activity-log entries")
    print("It keeps: staff, PINs, who is signed in, email lists, dropdown lists, settings.")
    print("Report numbers start again at 00001.")
    print(f"A full copy is saved first in opsapp-data/before-reset/ in case you need it back.")
    if input('Type RESET to go ahead (anything else cancels): ').strip() != "RESET":
        print("Cancelled. Nothing was changed.")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(RESET_DIR, exist_ok=True)
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(os.path.join(RESET_DIR, f"ops-{stamp}.db"))
    src.backup(dst)
    dst.close()
    src.close()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM emails")
        c.execute("DELETE FROM photos")
        c.execute("DELETE FROM reports")
        c.execute("DELETE FROM ip_failures")
        c.execute("DROP TRIGGER IF EXISTS audit_no_delete")
        c.execute("DELETE FROM audit")
        c.execute(AUDIT_DELETE_TRIGGER)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    photos = os.path.join(DATA_DIR, "photos")
    if os.path.isdir(photos):
        shutil.move(photos, os.path.join(RESET_DIR, f"photos-{stamp}"))
    os.makedirs(photos, exist_ok=True)
    set_setting("test_reset_done", time.strftime("%Y-%m-%d %H:%M"))
    audit(None, "server console", "test_data_reset", None,
          {"cleared": n, "copy_kept": f"before-reset/ops-{stamp}.db"})
    print("Done. The app is clean and the activity log is locked again.")
    print(f"Copy of the old data: ~/ai-server/opsapp-data/before-reset/ (ops-{stamp}.db and photos-{stamp})")
    return 0


def main(argv):
    init_db()
    if len(argv) >= 2 and argv[0] == "set-pin":
        return set_pin(argv[1])
    if len(argv) >= 2 and argv[0] == "import-pins":
        return import_pins(argv[1])
    if len(argv) >= 2 and argv[0] == "set-owner":
        return set_owner(argv[1])
    if argv and argv[0] == "reset-test-data":
        return reset_test_data(again="--again" in argv)
    if argv and argv[0] == "staff":
        return list_staff()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

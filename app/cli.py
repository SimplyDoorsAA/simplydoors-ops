"""Command-line helpers, run inside the container:

  docker exec -it opsapp python -m app.cli set-pin "Adem Atis"
  docker exec opsapp python -m app.cli import-pins /data/import/pins.csv
  docker exec opsapp python -m app.cli staff
"""
import csv
import getpass
import os
import sys

from . import auth
from .db import audit, conn, init_db


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
    for r in conn().execute("SELECT name, dept, is_admin, pin_hash IS NOT NULL AS has_pin FROM staff ORDER BY dept, name"):
        print(f"{r['dept']:<20} {r['name']:<20} {'admin' if r['is_admin'] else '':<6} {'PIN set' if r['has_pin'] else 'no PIN'}")
    return 0


def main(argv):
    init_db()
    if len(argv) >= 2 and argv[0] == "set-pin":
        return set_pin(argv[1])
    if len(argv) >= 2 and argv[0] == "import-pins":
        return import_pins(argv[1])
    if argv and argv[0] == "staff":
        return list_staff()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

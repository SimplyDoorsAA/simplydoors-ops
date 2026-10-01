# SimplyDoors Operations

Internal operations app for SimplyDoors staff. It runs on the OptiPlex home server (`optiplex-ai`) and replaces the
GitHub Pages portal + Google Apps Script, one form at a time.

- **Staff app:** https://optiplex-ai.tailf0af63.ts.net:10000/ops/
- **Admin (Adem, Paz):** https://optiplex-ai.tailf0af63.ts.net:10000/ops/admin

## Stage 1 (this version)

- Sign-in with name + 6–8 digit PIN, checked on the server. Lockouts get longer with repeated misses (15 min, 1 hour, then admin unlock). Connections making many wrong guesses are blocked.
- **Receiving Report** form, built for phones:
  - Big tap targets, photo previews, take a photo or pick one from the gallery.
  - Every field and photo is saved on the phone while it's typed, and survives reloads.
  - Reports are kept on the phone until the server confirms it has them, so nothing is lost on bad signal.
  - Every report gets a receipt number (`RCV-00001`).
- Every field and every photo is **stored on the server first**. The email (with a PDF of the full report) is queued and retried until it goes out.
- **Admin screen:**
  - Reports with photos and PDF.
  - **Activity log** of every sign-in, report, view, email and change. It can't be edited or deleted from the app (database triggers block it). Exportable to a spreadsheet.
  - Staff & PINs.
  - Email lists per form.
  - Status.
- Phone alerts (ntfy) for lockouts, PIN resets, staff changes, email-list changes, emails that keep failing, and any opening of a disciplinary record (Stage 2).

### Email rules

The rules were copied from the old Apps Script and are editable in the admin screen.

| Report | Always | Plus |
|---|---|---|
| Receiving Report | adem@, lupes@ | sales rep picked on the form |
| End of Shift | adem@, lupes@ | |
| Vehicle Inspection | adem@, lupes@ | admin@ if anything is Defective |
| Employee Incident | adem@, lupes@, admin@ | |
| Vehicle Incident | adem@, lupes@, admin@ | |
| Disciplinary Action | adem@, paz@, admin@ | the employee written up |
| Measure Report | admin@ | the person who measured |

## Install / update (on the OptiPlex, as `adem`)

```
tar xzf ~/opsapp-stage1.tar.gz -C ~ && bash ~/simplydoors-ops/deploy/install.sh
```

Running it again updates the app and keeps all data. The script:

- Adds an `opsapp` service to `~/ai-server/docker-compose.override.yml`, after making a backup.
- Builds the container, bound to `127.0.0.1:8010`.
- Publishes it through Tailscale Funnel on public port **10000**, which keeps it separate from the Sign app on 443.
- Checks that the Sign app answers exactly as it did before.

## One-time PIN import from the old Google Sheet

1. In the Sheet, open the **PINs** tab and choose File → Download → CSV.
2. Copy the file to the OptiPlex home folder as `pins.csv`. **Don't send it through chat or email.**
3. Run: `bash ~/ai-server/opsapp/deploy/import-pins.sh`

The import only fills in people who don't have a PIN yet in the new app. PINs shorter than 6 digits are skipped and listed. The CSV is deleted afterwards. Then delete the PINs tab from the Google Sheet.

## Undo

```
bash ~/ai-server/opsapp/deploy/uninstall.sh
```

This takes the app off the internet, removes the container, and restores the compose file. Data stays in `~/ai-server/opsapp-data` until you delete it.

## Where things live

- `~/ai-server/opsapp/` holds the app code and `.env`, which contains email settings (the Gmail app password is here, permissions 600).
- `~/ai-server/opsapp-data/ops.db` is the database: reports, staff, activity log.
- `~/ai-server/opsapp-data/photos/<report id>/` holds the photos.
- `~/ai-server/opsapp-data/backups/` holds daily database snapshots (kept 14 days). **These are on the same disk.** Add `opsapp-data` to the nightly off-site backup.

## Console commands

```
docker exec -it opsapp python -m app.cli set-pin "Adem Atis"   # also the way to reset an admin's PIN
docker exec opsapp python -m app.cli staff                     # who has a PIN
docker logs --tail 50 opsapp
```

## Development

```
pip install -r requirements.txt pytest aiosmtpd httpx
python -m pytest -q tests
DATA_DIR=./data COOKIE_SECURE=0 uvicorn app.main:app --port 8010   # then open http://localhost:8010/ops/
```

Forms are defined in `app/forms.py`. Stage 2 adds End of Shift, Vehicle Inspection, the incident reports and Disciplinary there.

# SimplyDoors Operations

Internal operations app for SimplyDoors staff. It runs on the OptiPlex home server (`optiplex-ai`) and replaces the
GitHub Pages portal + Google Apps Script, one form at a time.

- **Staff app:** https://optiplex-ai.tailf0af63.ts.net:10000/ops/
- **Admin (Adem, Paz):** https://optiplex-ai.tailf0af63.ts.net:10000/ops/admin

## Updates are hands-off

- Whatever is merged into `main` goes live on the OptiPlex by itself within about 5 minutes, and your phone gets an alert each time.
  Pull requests run the tests on GitHub first (`.github/workflows/tests.yml`).
- Every 5 minutes, cron runs `deploy/autodeploy.sh` on the OptiPlex. When there's a new commit it:
  1. runs the tests again on the server;
  2. swaps in the new code and rebuilds;
  3. checks health, and if the new version isn't healthy, puts the previous version back.
  A commit whose tests fail is skipped until a newer one lands. If only the package download (no internet) or the
  copy of the running code failed, nothing changes and it tries again 5 minutes later (one alert, not one per try).
- It never touches `.env`, the data folder, docker-compose or Tailscale. A change that needs a new setting or a
  compose change still needs `deploy/install.sh` (or a setup script like `sf-setup.sh`) run on the server.
- One-time setup, on the OptiPlex as `adem`:
  `bash <(curl -fsSL https://raw.githubusercontent.com/SimplyDoorsAA/simplydoors-ops/main/deploy/autodeploy-setup.sh)`
- Pause: `touch ~/ai-server/opsapp-autodeploy.pause` (delete the file to resume). Log: `~/opsapp-autodeploy.log`.

## Job lookup (Service Fusion)

- **Install**, **Delivery Proof** and **Measure** start with "Find the job": type the last 4 of the job # or the customer's name, tap the job,
  and the form fills in (Install: last 4, customer, customer email; Delivery: last 4, customer, delivery address; Measure: customer, PO). Everything stays editable.
- The server keeps a copy of Service Fusion's **open jobs**, refreshed every 20 minutes Mon-Sat 6 AM-7 PM, plus a
  Refresh button (at most once a minute). Measure lists only Scheduled Consult jobs; Delivery lists "15 Delivery Scheduled" jobs; Install and Delivery search every open job once you type.
- Search shows job #, name, status and date only. Address, phone and email appear after picking a job, and every pick
  is in the activity log. Read-only: nothing is ever written to Service Fusion.
- If a crew changes a pre-filled field, the internal email/PDF shows "Changed from Service Fusion" so the office can fix
  Service Fusion. The customer's copy never shows it.
- Key: `SF_CLIENT_ID` / `SF_CLIENT_SECRET` in `~/ai-server/opsapp/.env`, saved by `deploy/sf-setup.sh`. Health: Admin → Status.

## Price List (beta)

- A **Price List** tile (marked BETA) on the home screen opens vendor net costs, a buy list and purchase orders: `/ops/pricelist`.
- **Who sees it:** only people with *Can open the Price List* switched on (Admin → Staff & PINs → Edit), plus the owner.
  Everyone else gets a 403 from every price endpoint.
- **Price sheets are never in this repository** (it's public and vendor prices are confidential). An admin loads each vendor's
  sheet as a CSV in **Admin → Price List**; it lives only in the database on the OptiPlex. A vendor can have several live
  sheets (Boise Cascade: Simpson shaker, Simpson rift white oak, Steves); each upload says which one it replaces, or is added
  next to them, and a sheet can be removed. Columns: `sku, name, category, price` (required) plus `group, mfr, width_in,
  height_in, thickness, core, stocked, uom, hand, brand, page, flag`, and up to 6 `compare <name>` columns (e.g.
  `compare Pallet`): other price levels shown on the item for comparison only; a PO always uses `price`. A blank price
  shows as "Call for price"; a part number used twice is kept and flagged. A `pack` column (e.g. `25` or `6, 12`)
  makes the buy list and PO preview warn when a quantity isn't whole packs, with a one-tap round-up.
- **Vendors:** Woodgrain, Boise Cascade, Simpson and Novo are built in; an admin can add more in Admin → Price List →
  Add a vendor (name only; the PO email and address are set on the vendor's card).
- **Compare vendors:** every door's item panel shows each other vendor's cheapest matching door (same size,
  thickness and core; same **match style** when both groups are tagged, otherwise the same category, marked "check the
  style"), and the **Compare** tab lists every vendor's matches for a size, cheapest first. Editors tag styles per group
  (Price sheets → Tag match styles, or in the item editor); tags are kept by group name, so they carry over to a new
  version of the sheet.
- **Hand-written POs:** Buy list → "Write a PO by hand" (or "Edit as a hand-written PO" on a vendor's buy list): a
  listed or one-off vendor, any lines and prices (or pick from the price list), a Service Fusion job or "No job" with a
  typed PO #. "Save & download PDF" keeps it in Purchase orders as "Downloaded, not sent" (nothing emailed; it can be
  sent later); "Send to vendor" emails it like any PO. Every PO can be downloaded as a PDF.
- **Editing items:** people with **Can edit Price List items** (Admin → Staff, e.g. the purchaser; it needs the Price List
  switch too) get **✎ Edit this item** on every item: any field, the ⚠ note and the comparison prices. They can also add an
  item to a sheet, delete one, and download a sheet (with its edits) as a CSV to change in Excel and load back. Every change
  is in the activity log with the old and new value, the owner gets one phone alert per editor per 10 minutes, and an edited
  item shows who changed it. Editors can also load and remove sheets (Price List → Price sheets); where a vendor's POs are
  emailed stays admins-only. Uploading a new version of a sheet replaces its edits; the app warns first.
- **Shop:** pick a vendor, then a category, then items (or search). Flagged lines (wrong part number, odd price on the
  vendor's sheet) show **⚠ Check** with the reason. Non-stock interior slabs add 30% under 10 of one size/style.
- **Purchase orders:** the buy list (kept on that phone) becomes a PO for one vendor. The PO number is the **PO number already
  on the Service Fusion job** (picked with Find the job); a job without one can't be ordered. Prices are worked out again on
  the server from the loaded sheet. Sending emails a PDF to the vendor's PO email (set in Admin → Price List; until it's set, that
  vendor's POs can't be sent) with a visible copy to **admin@simplydoors.com**. Anyone with access can send. The owner's test mode
  sends the PO only to the owner. Every PO, sheet load and vendor email change is in the activity log; a vendor email
  change also reaches the owner's phone.

## Customer intake + Leads

- **The customer form:** https://optiplex-ai.tailf0af63.ts.net/start (Studio's public address, no :10000). No sign-in.
  Name, a phone or an email (at least one), project address, what the project is, a description, up to 5 photos or PDFs,
  and how they heard about us. They get "Got it" with a receipt number (`INT-00001`), then an optional "Tell us more"
  (number of doors and windows, timeline, who they are, best time to call), saved onto the same lead. No budget question.
- **Saved on their phone first.** What the customer sends stays on their phone until the server has it, and is sent again
  on a bad signal (also when they open the page again). Each send carries its own id, so a retry never makes a second lead.
- **Saved before anyone is emailed.** Then the **Customer Intake** list (Admin → Email lists: admin@, plus the owner's
  private copy) gets the details, the photos and an **Open this lead** button; the owner's phone gets an alert; and if
  they gave an email, the customer gets a receipt from "SimplyDoors" (replies go to admin@). Emails use the normal queue,
  so a failed email never loses a lead.
- **Files:** photos are saved on the server next to the report photos (`photos/lead-<id>/`, copied off-site the same way),
  smaller, with no stamp and no hidden location. PDFs are kept as sent and download when opened. Up to 5 files: photos
  up to 15 MB, PDFs up to 10 MB. Anything else is refused.
- **Leads screen:** the **Leads** tile on the home screen (`/ops/leads`). Admins, and people with **Can see Leads**
  switched on (Admin → Staff & PINs → Edit); everyone else gets a 403 from every lead endpoint. Tabs: Leads and Suspected
  spam. Claim, Give it to, status (New → Called → Measure booked → Quoted → Won / Lost), notes, and Call / Text / Email
  buttons. A lead claimed but with no claim, status change or note for 24 hours is flagged. Every view, claim, status
  change and note is in the activity log. Leads stay in this app: nothing goes to Service Fusion.
- **Spam (no outside service):**
  - **Robots** get a normal-looking "Got it", are never emailed and never alert anyone. They're listed under Suspected
    spam → "Stopped automatically" for 30 days. Caught when: the hidden trap box is filled in, it's sent under 3 seconds
    after the page opened, it didn't come from the form page, or a connection sends more than 20 forms in an hour.
  - **Suspected spam** is kept, but nobody is emailed or alerted until someone taps **Not spam: move to Leads** (then it's
    emailed and alerted like any new lead). Flagged when: address and description filled in and sent under 10 seconds,
    2 or more links in the text or a link in the name, or the 6th to 20th form from one connection in an hour.
  - Cloudflare Turnstile can be added later in one place: `captcha_ok` in `app/leads.py`.
- **Test mode:** with Test mode on, the owner taps **Make a test link** on the Leads screen. Anything sent through that link
  is a TEST lead (`TEST-INT-00001`) that only the owner sees, and every email about it, the customer's receipt too, goes
  only to the owner with "TEST -" in the subject. The link ends after 24 hours, when a new one is made, or when Test mode
  is turned off. Test links skip the per-hour limit. The owner can delete a test lead.
- **One-time server change (after hours):** `bash ~/ai-server/opsapp/deploy/intake-setup.sh`. It saves Tailscale's
  setting, records how Studio answers, adds only `/start`, checks Studio answers exactly the same, and puts the old
  setting back by itself if anything differs. Safe to run twice. Undo: `bash ~/ai-server/opsapp/deploy/intake-setup.sh undo`.
  Autodeploy never touches Tailscale.

## Stage 3 (this version): Measure

- **Measure** form on the phone: one job (customer, PO, date), then any number of **door** and **window** cards. Field names and choices are copied from the old Measure App.
  - Each card has up to 3 stamped photos.
  - Windows can have several W × H measurements.
  - The app has "Copy last" and collapsible cards, and keeps the draft on the phone if the signal drops.
- Sizes are kept exactly as typed: whole inches plus a fraction, never converted.
- Every door needs a width and height, and every window needs at least one W × H. (The old app only required the customer name.)
- **Reopen and revise.** "Past measures" lists your own measures (admins see everyone's).
  - Reopening one and sending it again creates a new receipt marked **REVISED, replaces MSR-…**.
  - The old version stays on file, shown as "Replaced by".
  - Photos carry over without uploading them again.
  - "Measured by" stays the original person. Whoever revises someone else's measure is listed as "Revised by".
- Email goes to the Measure list (admin@) plus whoever measured, and whoever revised it.
- The PDF has a summary table of every opening for ordering, then one section per door or window with its photos.
- Round 2 additions:
  - **Handing picker:** a drawing of each handing, seen from outside.
  - **Fraction buttons:** 0 to 7/8, with a live size preview.
  - **Warnings:** unusual sizes (typos like 3612, width bigger than height, a wide single door) show a ⚠. They never block sending, but the app asks "Send anyway?".
  - **Guided photo slots:** outside, inside, sill and floor, extra.
  - **Card controls:** duplicate any card, and move cards up or down with ▲▼.
  - **Mark-up:** tap any new photo to draw or write on it, in any form, not just Measure.
- Measure starts **switched off for staff**. Turn it on in Admin → Forms & lists.

## Stage 2: every portal form

- Seven forms on the phone: Receiving Report, **Delivery Proof** (new: site photos + customer signature), End of Shift, Vehicle Inspection, Vehicle Incident, Employee Incident, Disciplinary Action (admins only, marked CONFIDENTIAL).
- Forms are described once in `app/forms.py`. That one description drives the phone screen, the server's checks, the PDF and the email.
- **Admin → Forms & lists**:
  - Choose which forms staff see. Default is Receiving only, so staff keep using the old portal for the rest until you switch each one on. Admins always see every form.
  - Edit the location and vehicle pick lists.
- Each form numbers its own receipts (RCV-, DLV-, EOS-, VIN-, VIC-, INC-, DSC-).
- Defective inspections and disciplinary records get a red header in the email and PDF.

## Stage 1

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
- **Photo time + location stamps:**
  - Asked once per phone, on a one-time screen after the first sign-in. Location is used only when a photo is added, never in the background.
  - Each photo gets a bar printed along the bottom: when it was added, GPS position and accuracy, receipt number and who took it.
  - The admin screen and PDF show the same details with a "View on map" link.
  - Photos without a location still go through but are flagged.
  - Photos that look older than 10 minutes when added (likely picked from the gallery) are flagged too.
  - iPhones re-ask about location about once a day unless each phone sets Settings → Apps → Safari → Location → Allow. The one-time screen tells staff how.
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
| Delivery Proof | adem@, lupes@ | sales rep picked on the form |
| Measure Report | admin@ | the person who measured |

## Install / update (on the OptiPlex, as `adem`)

```
tar xzf ~/opsapp-stage1.tar.gz -C ~ && bash ~/simplydoors-ops/deploy/install.sh
```

Running it again updates the app and keeps all data. The script:

- Adds an `opsapp` service to `~/ai-server/docker-compose.override.yml`, after making a backup.
- Builds the container, bound to `127.0.0.1:8010`.
- Publishes it through Tailscale Funnel on public port **10000**, which keeps it separate from the Sign app on 443.
- Sets up the nightly off-site copy to Google Drive and runs the first one.
- Checks that the Sign app answers exactly as it did before.

## Giving people access (setup links)

Each person makes their own PIN. Nobody reads or types anyone else's PIN.

1. Go to Admin → Staff & PINs and tap **Invite** next to a name.
2. Send them the link: **Text it** opens Messages with it filled in, or use **Copy message**.
   - The link works once, only for that person, and expires after 7 days. Making a new link cancels the old one.
3. They open it on their phone. A guided setup walks them through:
   - making their PIN (6–8 digits; obvious ones like 111111 or 123456 are refused);
   - turning on photo location, with a test that shows a ✓ when it works;
   - adding the app to their home screen;
   - "only when parked".
4. Forgot their PIN? Tap **New link** next to their name. Their old PIN keeps working until they set a new one.

Another admin's PIN can only be reset on the server: `docker exec -it opsapp python -m app.cli set-pin "Name"`.

## (Optional) One-time PIN import from the old Google Sheet

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
- `~/ai-server/opsapp-data/backups/` holds daily database snapshots, taken at 2:00 AM and kept 14 days on the server.
- **Off-site copy:** the `opsapp-offsite` container copies the snapshots and every photo to Google Drive each night at 2:30 AM.
  - It reuses your existing nightly backup's Drive connection, as its own copy in `~/ai-server/opsapp-offsite/`, and saves to `<remote>:optiplex-backups/opsapp/` by default.
  - It only ever copies and never deletes off-site, so a broken disk here can't wipe the backup.
  - Admin → Status shows the last successful copy. Your phone gets an alert if a copy fails, or if a day passes with no successful copy.
  - To force a copy now: `touch ~/ai-server/opsapp-data/.offsite-now && docker restart opsapp-offsite`
  - To set it up again: `bash ~/ai-server/opsapp/deploy/setup-offsite.sh`
- **Restore:** download the newest `ops-YYYY-MM-DD.db` from Drive into `~/ai-server/opsapp-data/ops.db`, and the `photos` folder into `~/ai-server/opsapp-data/photos/`. Then `docker restart opsapp`.

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

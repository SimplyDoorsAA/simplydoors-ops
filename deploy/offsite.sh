#!/bin/sh
# Off-site backup for the SimplyDoors Operations app.
# Runs inside the "opsapp-offsite" container (rclone image). Once a night, after
# OFFSITE_AT (default 02:30 Central), it COPIES to Google Drive:
#   /data/backups  -> <remote>/database   (daily database snapshots)
#   /data/photos   -> <remote>/photos     (every photo)
# It only ever copies; it never deletes anything off-site, so a wiped or broken
# disk here can't wipe the backup. Result is written to /data/offsite-status.json
# (shown on Admin > Status) and failures send a phone alert.
# To force a run now:  touch ~/ai-server/opsapp-data/.offsite-now
set -u
DEST="${OFFSITE_DEST:?OFFSITE_DEST not set}"
AT=$(printf '%s' "${OFFSITE_AT:-0230}" | sed 's/^0*//'); AT=${AT:-0}
CONF=/config/rclone.conf
STATUS=/data/offsite-status.json
LASTDAY=/data/.offsite-lastday

json_escape() { printf '%s' "$1" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g' | cut -c1-400; }

alert() {
  [ -n "${NTFY_URL:-}" ] || return 0
  wget -q -O /dev/null --header="Title: Ops app: off-site backup failed" --header="Priority: high" \
    --post-data="$1" "$NTFY_URL" 2>/dev/null || true
}

run_backup() {
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  log=/tmp/offsite.log
  : > "$log"
  ok=1
  mkdir -p /data/backups /data/photos
  rclone copy /data/backups "$DEST/database" --config "$CONF" --transfers 2 --retries 5 --low-level-retries 10 >>"$log" 2>&1 || ok=0
  rclone copy /data/photos "$DEST/photos" --config "$CONF" --transfers 4 --retries 5 --low-level-retries 10 >>"$log" 2>&1 || ok=0
  finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if [ "$ok" = 1 ]; then
    printf '{"ok":true,"started":"%s","finished":"%s","dest":"%s"}\n' "$started" "$finished" "$(json_escape "$DEST")" > "$STATUS.tmp"
    date +%Y-%m-%d > "$LASTDAY"
    echo "$finished" > /data/.offsite-lastok
    echo "$(date) off-site backup OK"
  else
    err=$(grep -i 'error' "$log" | tail -n 3)
    [ -n "$err" ] || err=$(tail -n 3 "$log")
    printf '{"ok":false,"started":"%s","finished":"%s","dest":"%s","error":"%s"}\n' \
      "$started" "$finished" "$(json_escape "$DEST")" "$(json_escape "$err")" > "$STATUS.tmp"
    echo "$(date) off-site backup FAILED: $err"
    if [ "$(cat /data/.offsite-alertday 2>/dev/null)" != "$(date +%Y-%m-%d)" ]; then
      alert "Copying ops app data to Google Drive failed: $err"      # at most one alert a day
      date +%Y-%m-%d > /data/.offsite-alertday
    fi
    # last day not updated, so it tries again on a later check
  fi
  mv "$STATUS.tmp" "$STATUS"
  [ "$ok" = 1 ]
}

echo "$(date) off-site backup service started; copies to $DEST nightly after ${OFFSITE_AT:-0230}"
while true; do
  today=$(date +%Y-%m-%d)
  now=$(date +%H%M | sed 's/^0*//'); now=${now:-0}
  last=$(cat "$LASTDAY" 2>/dev/null || true)
  if [ -f /data/.offsite-now ]; then
    rm -f /data/.offsite-now
    run_backup || sleep 3000          # after a failure, wait about an hour before trying again
  elif [ "$now" -ge "$AT" ] && [ "$today" != "$last" ]; then
    run_backup || sleep 3000
  fi
  sleep 600
done

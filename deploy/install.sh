#!/usr/bin/env bash
# SimplyDoors Operations app — install or update on the OptiPlex (adem@optiplex-ai).
# Safe to run again: an update keeps all reports, photos, PINs and the activity log.
#
# What it does:
#   1. Copies the app into ~/ai-server/opsapp (data lives in ~/ai-server/opsapp-data)
#   2. Asks for email + alert settings the first time (saved in ~/ai-server/opsapp/.env)
#   3. Adds an "opsapp" service to docker-compose.override.yml (backup made first)
#   4. Builds and starts it on 127.0.0.1:8010 (not reachable from the network directly)
#   5. Sets your admin PIN the first time
#   6. Sets up a nightly off-site copy to Google Drive, reusing your existing backup's Drive connection
#   7. Publishes it at https://optiplex-ai.tailf0af63.ts.net:10000/ops/ (its own public port, kept
#      separate from the Sign app on the normal address) and checks the Sign app is still up
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="$HOME/ai-server"
APPDIR="$STACK/opsapp"
DATADIR="$STACK/opsapp-data"
OVERRIDE="$STACK/docker-compose.override.yml"
PORT=8010
HOST="optiplex-ai.tailf0af63.ts.net"
PUBLIC="https://$HOST"                 # Sign app (not touched)
FUNNEL_PORT=10000                      # one of the 3 ports Tailscale Funnel allows; 443=Sign app, 8443=private apps
OPS="https://$HOST:$FUNNEL_PORT/ops"
LOG="$HOME/opsapp-install-$(date +%Y%m%d-%H%M%S).log"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*"; echo "Nothing after this point was changed. Log: $LOG"; exit 1; }
exec > >(tee -a "$LOG") 2>&1

say "Checking the server"
[ "$(hostname)" = "optiplex-ai" ] || warn "This doesn't look like optiplex-ai (hostname is $(hostname)). Continuing anyway."
command -v docker >/dev/null || die "Docker isn't installed."
command -v tailscale >/dev/null || die "Tailscale isn't installed."
[ -d "$STACK" ] || die "Can't find $STACK."
[ -f "$OVERRIDE" ] || die "Can't find $OVERRIDE."
cd "$STACK"
docker compose version >/dev/null || die "docker compose isn't working."
FIRST_INSTALL=1
grep -qE '^[[:space:]]+opsapp:' "$OVERRIDE" && FIRST_INSTALL=0
if [ $FIRST_INSTALL = 1 ] && (ss -ltn 2>/dev/null | grep -q ":$PORT "); then
  die "Port $PORT is already in use by something else."
fi

# The Sign app must keep working. Record how it answers right now.
SIGN_BEFORE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$PUBLIC/" || echo "000")
echo "Sign app public address answers: $SIGN_BEFORE"

say "Copying the app into $APPDIR"
mkdir -p "$APPDIR" "$DATADIR"
ENV_SAVED=""
[ -f "$APPDIR/.env" ] && ENV_SAVED=$(cat "$APPDIR/.env")
find "$APPDIR" -mindepth 1 -maxdepth 1 ! -name '.env' -exec rm -rf {} +
cp -r "$SRC/app" "$SRC/Dockerfile" "$SRC/requirements.txt" "$SRC/.dockerignore" "$APPDIR/"
cp -r "$SRC/deploy" "$APPDIR/deploy"
[ -n "$ENV_SAVED" ] && printf '%s\n' "$ENV_SAVED" > "$APPDIR/.env"
sudo chown -R 1000:1000 "$DATADIR"

if [ ! -f "$APPDIR/.env" ]; then
  say "Email + phone alert settings (first time only)"
  echo "Report emails are sent through Gmail, like the Sign app."
  echo "Use a Google 'app password' (myaccount.google.com/apppasswords). Leave blank to set it up later;"
  echo "reports are still saved, and their emails wait until this is filled in."
  read -r -p "Send from which Gmail/Workspace address? [adem@simplydoors.com]: " SMTP_USER
  SMTP_USER=${SMTP_USER:-adem@simplydoors.com}
  read -r -s -p "App password for $SMTP_USER (typing is hidden, Enter to skip): " SMTP_PASSWORD; echo
  SMTP_PASSWORD=$(printf '%s' "$SMTP_PASSWORD" | tr -d ' ')
  umask 077
  cat > "$APPDIR/.env" <<EOF
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_STARTTLS=1
SMTP_USER=${SMTP_USER}
SMTP_PASSWORD=${SMTP_PASSWORD}
MAIL_FROM_NAME=SimplyDoors Portal [DO NOT REPLY]
MAIL_REPLY_TO=noreply@simplydoors.com
NTFY_URL=http://host.docker.internal:8080/adem-alerts
EOF
  chmod 600 "$APPDIR/.env"
  umask 022
fi

if [ $FIRST_INSTALL = 1 ]; then
  say "Adding the opsapp service to docker-compose.override.yml"
  [ -f "$OVERRIDE.before-opsapp" ] || cp "$OVERRIDE" "$OVERRIDE.before-opsapp"
  cp "$OVERRIDE" "$OVERRIDE.bak-$(date +%Y%m%d-%H%M%S)"
  echo "Backup: $OVERRIDE.before-opsapp"
  python3 - "$OVERRIDE" <<'PY'
import re, sys
p = sys.argv[1]
lines = open(p).read().split("\n")
idx = next((i for i, l in enumerate(lines) if re.match(r"^services:\s*(#.*)?$", l)), None)
if idx is None:
    sys.exit("no top-level 'services:' line found")
indent = "  "
for l in lines[idx + 1:]:
    if l.strip() and not l.lstrip().startswith("#"):
        m = re.match(r"^(\s+)\S", l)
        if m:
            indent = m.group(1)
        break
i2, i3 = indent * 2, indent * 3
block = f"""{indent}opsapp:   # SimplyDoors Operations app (added by opsapp installer)
{i2}build: ./opsapp
{i2}container_name: opsapp
{i2}restart: unless-stopped
{i2}user: "1000:1000"
{i2}env_file: ./opsapp/.env
{i2}environment:
{i3}- DATA_DIR=/data
{i3}- BASE_PATH=/ops
{i3}- TZ=America/Chicago
{i2}volumes:
{i3}- ./opsapp-data:/data
{i2}ports:
{i3}- "127.0.0.1:8010:8000"
{i2}extra_hosts:
{i3}- "host.docker.internal:host-gateway"
""".rstrip("\n").split("\n")
lines[idx + 1:idx + 1] = block
open(p, "w").write("\n".join(lines))
PY
  if ! docker compose config -q; then
    cp "$OVERRIDE.before-opsapp" "$OVERRIDE"
    die "The compose file didn't validate after adding opsapp, so I put the backup back."
  fi
fi

say "Building and starting the app (first build takes a few minutes)"
docker compose up -d --build opsapp
for i in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/ops/healthz" >/dev/null && break
  sleep 3
done
curl -sf "http://127.0.0.1:$PORT/ops/healthz" >/dev/null || { docker logs --tail 40 opsapp; die "The app didn't start. Its last log lines are above."; }
echo "App is running: $(curl -s http://127.0.0.1:$PORT/ops/healthz)"

if ! docker exec opsapp python -c "from app.db import conn;import sys;sys.exit(0 if conn().execute('select 1 from staff where is_admin=1 and pin_hash is not null').fetchone() else 1)"; then
  say "Choose YOUR admin PIN for the new app (6-8 digits)"
  docker exec -it opsapp python -m app.cli set-pin "Adem Atis"
fi

say "Publishing at $OPS/"
FUNNEL_BEFORE=$(sudo tailscale funnel status 2>&1 || true)
echo "$FUNNEL_BEFORE"
if echo "$FUNNEL_BEFORE" | grep -q "127.0.0.1:$PORT"; then
  echo "Already published."
else
  if echo "$FUNNEL_BEFORE" | grep -q ":$FUNNEL_PORT"; then
    die "Something else is already using public port $FUNNEL_PORT, so I won't touch it. Send me the status above."
  fi
  sudo tailscale funnel --bg --https=$FUNNEL_PORT --set-path /ops "http://127.0.0.1:$PORT"
fi
sleep 3
sudo tailscale funnel status 2>&1 || true

SIGN_AFTER=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$PUBLIC/" || echo "000")
OPS_AFTER=$(curl -s --max-time 15 "$OPS/healthz" || true)
echo "Sign app answers: before=$SIGN_BEFORE after=$SIGN_AFTER"
echo "Ops app answers:  $OPS_AFTER"
if [ "$SIGN_BEFORE" != "$SIGN_AFTER" ]; then
  warn "The Sign app answers differently than before. Taking the ops app back off the internet to be safe."
  sudo tailscale funnel --https=$FUNNEL_PORT off || true
  die "Sign app check failed (before=$SIGN_BEFORE after=$SIGN_AFTER); the ops app was unpublished. Send me this whole output."
fi
case "$OPS_AFTER" in *'"ok":true'*) ;; *) warn "Couldn't reach $OPS from here yet; it can take a minute. Try it on your phone." ;; esac

bash "$APPDIR/deploy/setup-offsite.sh" || warn "Off-site backup setup hit a problem; everything else is installed. Re-run: bash $APPDIR/deploy/setup-offsite.sh"

say "Sending a test alert to your phone"
docker exec opsapp python -c "
import os,urllib.request
u=os.environ.get('NTFY_URL','')
try:
    urllib.request.urlopen(urllib.request.Request(u,data=b'SimplyDoors Ops app is installed. Alerts work.',headers={'Title':'Ops app installed'}),timeout=8); print('Alert sent. You should see it on your phone.')
except Exception as e: print('Alert could not be sent:',e)
" || true

say "Done"
cat <<EOF
Staff app:   $OPS/
Admin:       $OPS/admin
Settings:    $APPDIR/.env   (email password lives here; never shared)
Data:        $DATADIR       (database, photos, daily database snapshots)
Off-site:    copied nightly at 2:30 AM to Google Drive (see Admin > Status)
Log of this install: $LOG
To undo:     bash $APPDIR/deploy/uninstall.sh
EOF

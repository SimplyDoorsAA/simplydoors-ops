#!/usr/bin/env bash
# Sets up the nightly off-site copy of the ops app's data to Google Drive, reusing the
# Google Drive connection (rclone) your existing nightly server backup already uses.
# Called by install.sh; safe to run again on its own:  bash ~/ai-server/opsapp/deploy/setup-offsite.sh
set -euo pipefail
STACK="$HOME/ai-server"
OVERRIDE="$STACK/docker-compose.override.yml"
DATADIR="$STACK/opsapp-data"
CONFDIR="$STACK/opsapp-offsite"
CONF="$CONFDIR/rclone.conf"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="rclone/rclone:latest"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!  %s\033[0m\n' "$*"; }
skip() { warn "$1"; echo "Off-site backup was NOT set up. Everything else is fine. Run this again later:"; echo "  bash $STACK/opsapp/deploy/setup-offsite.sh"; exit 0; }
cd "$STACK"

if grep -qE '^[[:space:]]+opsapp-offsite:' "$OVERRIDE"; then
  say "Off-site backup is already set up; making sure it's running"
  docker compose up -d opsapp-offsite
  exit 0
fi

say "Setting up the nightly off-site copy to Google Drive"
docker pull -q "$IMAGE" >/dev/null || skip "Couldn't download the rclone tool image."
rcl() { docker run --rm -v "$CONF:/config/rclone.conf" -e XDG_CACHE_HOME=/tmp "$IMAGE" --config /config/rclone.conf "$@"; }

# 1. Find the Google Drive connection the existing backup uses.
CANDIDATES=()
BACKUP_CTRS=$(docker ps -a --format '{{.Names}}' | grep -i backup | grep -v opsapp || true)
BACKUP_TEXT=""
for c in $BACKUP_CTRS; do
  BACKUP_TEXT+=" $(docker inspect -f '{{json .Config.Cmd}} {{json .Config.Entrypoint}} {{json .Config.Env}}' "$c" 2>/dev/null || true)"
  while IFS= read -r src; do
    [ -n "$src" ] || continue
    if sudo test -f "$src"; then
      case "$src" in *rclone*.conf) CANDIDATES+=("$src") ;; *.sh) BACKUP_TEXT+=" $(sudo head -c 20000 "$src" 2>/dev/null || true)" ;; esac
    elif sudo test -d "$src"; then
      for f in "$src/rclone.conf" "$src/rclone/rclone.conf" "$src/.config/rclone/rclone.conf"; do
        sudo test -f "$f" && CANDIDATES+=("$f")
      done
      for f in $(sudo find "$src" -maxdepth 2 -name '*.sh' -size -100k 2>/dev/null | head -n 10); do
        BACKUP_TEXT+=" $(sudo cat "$f" 2>/dev/null || true)"
      done
    fi
  done < <(docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$c" 2>/dev/null || true)
done
for f in "$HOME/.config/rclone/rclone.conf" /root/.config/rclone/rclone.conf; do
  sudo test -f "$f" && CANDIDATES+=("$f")
done
while IFS= read -r f; do CANDIDATES+=("$f"); done < <(sudo find "$STACK" -maxdepth 4 -name rclone.conf -not -path "$CONFDIR/*" 2>/dev/null || true)
# de-duplicate
mapfile -t CANDIDATES < <(printf '%s\n' "${CANDIDATES[@]}" | awk 'NF && !seen[$0]++')
[ ${#CANDIDATES[@]} -gt 0 ] || skip "I couldn't find the Google Drive connection (rclone.conf) your nightly backup uses."
echo "Found Google Drive connection file(s):"; printf '  %s\n' "${CANDIDATES[@]}"
SRC_CONF="${CANDIDATES[0]}"
if [ ${#CANDIDATES[@]} -gt 1 ]; then
  read -r -p "Use which one? Press Enter for the first, or paste a path: " pick
  [ -n "$pick" ] && SRC_CONF="$pick"
fi

# 2. Keep our own copy, so the two backups never fight over the same file.
sudo mkdir -p "$CONFDIR"
sudo cp "$SRC_CONF" "$CONF"
sudo chown 1000:1000 "$CONFDIR" "$CONF"
sudo chmod 700 "$CONFDIR"; sudo chmod 600 "$CONF"

mapfile -t REMOTES < <(rcl listremotes 2>/dev/null | sed 's/:$//' | awk 'NF')
[ ${#REMOTES[@]} -gt 0 ] || skip "That rclone.conf has no Google Drive connections in it."
REMOTE=""
for r in "${REMOTES[@]}"; do
  if printf '%s' "$BACKUP_TEXT" | grep -q "$r:"; then REMOTE="$r"; break; fi
done
[ -n "$REMOTE" ] || REMOTE="${REMOTES[0]}"
FOLDER=$(printf '%s' "$BACKUP_TEXT" | grep -oE "$REMOTE:[A-Za-z0-9._-]+" | head -n 1 | cut -d: -f2 || true)
FOLDER=${FOLDER:-optiplex-backups}
DEST="$REMOTE:$FOLDER/opsapp"
echo "Connections in it: ${REMOTES[*]}"
read -r -p "Copy ops app data to [$DEST]? Press Enter to accept, or type another: " pick
[ -n "$pick" ] && DEST="$pick"
case "$DEST" in *:*) ;; *) skip "'$DEST' isn't a valid destination (it should look like remote:folder)." ;; esac
case "$DEST" in *\"*|*\'*|*'$'*|*'`'*) skip "Please use a destination without quotes or \$ in it." ;; esac

# 3. Prove it works before relying on it.
say "Testing the connection to $DEST"
echo "SimplyDoors ops app backup test $(date)" | sudo tee "$CONFDIR/install-test.txt" >/dev/null
if ! docker run --rm -v "$CONF:/config/rclone.conf" -v "$CONFDIR/install-test.txt:/t.txt:ro" -e XDG_CACHE_HOME=/tmp \
      "$IMAGE" --config /config/rclone.conf copyto /t.txt "$DEST/_install-test.txt"; then
  skip "Couldn't write to $DEST."
fi
rcl lsf "$DEST" | grep -q '_install-test.txt' || skip "Wrote a test file to $DEST but couldn't see it afterwards."
rcl deletefile "$DEST/_install-test.txt" || true
sudo rm -f "$CONFDIR/install-test.txt"
echo "Connection works."

# 4. Add the opsapp-offsite service.
[ -f "$OVERRIDE.before-opsapp" ] || cp "$OVERRIDE" "$OVERRIDE.before-opsapp"
cp "$OVERRIDE" "$OVERRIDE.bak-$(date +%Y%m%d-%H%M%S)"
DEST="$DEST" python3 - "$OVERRIDE" <<'PY'
import os, re, sys
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
dest = os.environ["DEST"].replace('"', "")
block = f"""{indent}opsapp-offsite:   # nightly copy of opsapp data to Google Drive (added by opsapp installer)
{i2}image: rclone/rclone:latest
{i2}container_name: opsapp-offsite
{i2}restart: unless-stopped
{i2}user: "1000:1000"
{i2}entrypoint: ["/bin/sh", "/offsite.sh"]
{i2}environment:
{i3}- "OFFSITE_DEST={dest}"
{i3}- OFFSITE_AT=0230
{i3}- TZ=CST6CDT
{i3}- XDG_CACHE_HOME=/tmp
{i3}- NTFY_URL=http://host.docker.internal:8080/adem-alerts
{i2}volumes:
{i3}- ./opsapp-data:/data
{i3}- ./opsapp-offsite/rclone.conf:/config/rclone.conf
{i3}- ./opsapp/deploy/offsite.sh:/offsite.sh:ro
{i2}extra_hosts:
{i3}- "host.docker.internal:host-gateway"
""".rstrip("\n").split("\n")
lines[idx + 1:idx + 1] = block
open(p, "w").write("\n".join(lines))
PY
if ! docker compose config -q; then
  cp "$(ls -t "$OVERRIDE".bak-* | head -n 1)" "$OVERRIDE"
  skip "The compose file didn't validate after adding the backup service, so I put it back the way it was."
fi
docker compose up -d opsapp-offsite

# 5. First copy right now, and wait for the result.
say "Running the first off-site copy now"
sudo -u "#1000" touch "$DATADIR/.offsite-now" 2>/dev/null || { sudo touch "$DATADIR/.offsite-now"; sudo chown 1000:1000 "$DATADIR/.offsite-now"; }
docker restart opsapp-offsite >/dev/null
before=$(sudo cat "$DATADIR/offsite-status.json" 2>/dev/null || true)
for i in $(seq 1 60); do
  now=$(sudo cat "$DATADIR/offsite-status.json" 2>/dev/null || true)
  [ -n "$now" ] && [ "$now" != "$before" ] && break
  sleep 5
done
now=$(sudo cat "$DATADIR/offsite-status.json" 2>/dev/null || true)
case "$now" in
  *'"ok":true'*) echo "First off-site copy worked. From now on it runs every night at 2:30 AM: $DEST" ;;
  *'"ok":false'*) warn "The first copy failed: $now"; echo "It will keep retrying every hour. Logs: docker logs opsapp-offsite" ;;
  *) warn "The first copy is still running (or hasn't started). Check Admin > Status later, or: docker logs opsapp-offsite" ;;
esac

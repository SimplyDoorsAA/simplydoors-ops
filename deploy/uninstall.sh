#!/usr/bin/env bash
# Undo the SimplyDoors Operations app install on the OptiPlex.
# Keeps your data (~/ai-server/opsapp-data) unless you delete it yourself.
set -uo pipefail
STACK="$HOME/ai-server"
OVERRIDE="$STACK/docker-compose.override.yml"
echo "==> Taking the ops app off the internet (public port 10000)"
sudo tailscale funnel --https=10000 off || true
echo "==> Stopping and removing the opsapp container"
cd "$STACK" && docker compose rm -sf opsapp || docker rm -f opsapp || true
if [ -f "$OVERRIDE.before-opsapp" ]; then
  echo "==> Restoring docker-compose.override.yml from before the install"
  cp "$OVERRIDE" "$OVERRIDE.with-opsapp.bak"
  cp "$OVERRIDE.before-opsapp" "$OVERRIDE"
fi
docker compose config -q && echo "Compose file OK."
echo "==> Checking the Sign app"
curl -s -o /dev/null -w 'Sign app answers: %{http_code}\n' https://optiplex-ai.tailf0af63.ts.net/ || true
echo
echo "Removed. Your reports, photos and log are still in $STACK/opsapp-data."
echo "Delete them only when you're sure:  sudo rm -rf $STACK/opsapp-data $STACK/opsapp"

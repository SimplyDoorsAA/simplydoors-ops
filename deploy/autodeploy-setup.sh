#!/usr/bin/env bash
# One-time setup for hands-off updates of the ops app. Run on adem@optiplex-ai:
#   bash <(curl -fsSL https://raw.githubusercontent.com/SimplyDoorsAA/simplydoors-ops/main/deploy/autodeploy-setup.sh)
# Afterwards, whatever is merged into GitHub's main goes live on its own within ~5 minutes (see deploy/autodeploy.sh).
# Safe to run again. To turn it off: crontab -l | grep -v opsapp-autodeploy | crontab -
set -euo pipefail
SRC="$HOME/opsapp-src"
APPDIR="$HOME/ai-server/opsapp"
REPO="https://github.com/SimplyDoorsAA/simplydoors-ops.git"
say() { printf '\n==> %s\n' "$*"; }

[ -f "$APPDIR/.env" ] || { echo "STOP: can't find $APPDIR/.env. Install the ops app first (deploy/install.sh). Nothing was changed."; exit 1; }
for c in git docker flock crontab curl; do
  command -v "$c" >/dev/null || { echo "STOP: '$c' isn't installed. Nothing was changed."; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "STOP: Docker doesn't answer for $(whoami) without sudo. Nothing was changed."; exit 1; }

say "Getting the code from GitHub into $SRC"
if [ -d "$SRC/.git" ]; then git -C "$SRC" fetch -q origin main; else git clone -q --branch main "$REPO" "$SRC"; fi
git -C "$SRC" checkout -qf --detach origin/main

say "Comparing the server's running code with GitHub's main"
DIFF=$(diff -rq -x __pycache__ -x '*.pyc' "$APPDIR/app" "$SRC/app" 2>&1 || true)
if [ -n "$DIFF" ]; then
  echo "$DIFF" | sed "s#$HOME/##g"
  echo
  echo "The server is running code that differs from main (above). Turning this on replaces it with main."
  read -r -p "Go ahead? [y/N]: " ANS
  [[ "${ANS:-n}" =~ ^[Yy] ]] || { echo "Stopped. Nothing on the server was changed."; exit 1; }
else
  echo "Same as main."
fi

say "First update (runs the tests, then rebuilds; staff may see a few seconds' blip)"
rm -f "$HOME/ai-server/opsapp-failed-commit" "$HOME/ai-server/opsapp-deployed-commit"
if ! bash "$SRC/deploy/autodeploy.sh"; then
  tail -40 "$HOME/opsapp-autodeploy.log"
  echo "STOP: the first update didn't go through (above). Cron was NOT turned on."; exit 1
fi
tail -3 "$HOME/opsapp-autodeploy.log"

say "Checking GitHub for updates every 5 minutes"
LINE="*/5 * * * * bash $APPDIR/deploy/autodeploy.sh  # opsapp-autodeploy"
( crontab -l 2>/dev/null | grep -v opsapp-autodeploy || true; echo "$LINE" ) | crontab -
crontab -l | grep opsapp-autodeploy

cat <<EOF

Done. Merges to main now go live by themselves within ~5 minutes; your phone gets an alert each time.
Pause:   touch ~/ai-server/opsapp-autodeploy.pause     (resume: rm ~/ai-server/opsapp-autodeploy.pause)
Log:     ~/opsapp-autodeploy.log
Turn off: crontab -l | grep -v opsapp-autodeploy | crontab -
EOF

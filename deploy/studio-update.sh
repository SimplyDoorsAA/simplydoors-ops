#!/bin/bash
# Brings Simply Studio on the server up to date with its private GitHub copy (SimplyDoorsAA/simplydoors-sign, main),
# through ~/signapp.git, the same GitHub copy and access signapp-save uses. Studio's data folder is never touched.
# Run on adem@optiplex-ai:  bash <(curl -fsSL https://raw.githubusercontent.com/SimplyDoorsAA/simplydoors-ops/main/deploy/studio-update.sh)
# Safe by design: it stops without changing anything if the server's Studio code differs from what it expects.
set -e
S=~/ai-server/services/signapp
say() { printf '\n==> %s\n' "$*"; }
[ -d "$S/app" ] || { echo "STOP: can't find $S/app. Nothing was changed."; exit 1; }
# The earlier one-off "Field app" link patch is part of this update: put those two files back first.
for f in app/templates/admin.html app/static/style.css; do
  [ -f "$S/$f.before-field" ] && mv -f "$S/$f.before-field" "$S/$f" && echo "Put back $f (the update includes the Field app link)."
done

say "Getting the latest Studio from GitHub (same access signapp-save uses)"
G="git --git-dir=$HOME/signapp.git --work-tree=$S"
[ -d "$HOME/signapp.git" ] || { echo "STOP: can't find ~/signapp.git. Nothing was changed."; exit 1; }
$G fetch -q origin main || { echo "STOP: couldn't reach GitHub. Nothing was changed."; exit 1; }
if ! $G diff --quiet HEAD -- . ; then
  echo "STOP: Studio on the server has changes that aren't saved to GitHub yet. Nothing was changed:"
  $G diff --name-only HEAD -- . ; echo "(Run signapp-save first if those changes are yours, then run this again.)"; exit 1
fi
if ! $G merge-base --is-ancestor HEAD origin/main; then
  echo "STOP: the server's Studio has saved work that isn't on GitHub's main. Nothing was changed."; exit 1
fi
BEFORE=$($G rev-parse --short HEAD)
KEEP=~/signapp-code-before-$(date +%Y%m%d-%H%M%S).tar.gz
tar czf "$KEEP" -C "$S" --exclude=./data .
$G merge -q --ff-only origin/main
echo "Code: $BEFORE -> $($G rev-parse --short HEAD)"
$G diff --name-only "$BEFORE" HEAD | sed 's/^/  updated /'

say "Rebuilding Studio (customers on a signing page see a short reload)"
cd ~/ai-server
docker compose build signapp
docker compose up -d signapp
echo "Waiting for Studio to come back..."
for i in $(seq 1 40); do sleep 3
  if docker exec signapp python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status==200 else 1)" 2>/dev/null; then break; fi
done
V=$(docker exec signapp python -c "import app.whatsnew as w; print(w.RELEASES[0][0])" 2>/dev/null || echo "?")
[ "$V" = "?" ] && { echo "PROBLEM: Studio isn't answering. Send Claude a screenshot. To undo: tar xzf $KEEP -C $S && cd ~/ai-server && docker compose up -d --build signapp"; exit 1; }
echo "DONE: Simply Studio is on Update $V."
echo "Old code saved in $KEEP"
echo "To undo: tar xzf $KEEP -C $S && cd ~/ai-server && docker compose up -d --build signapp"

#!/bin/bash
# Brings Simply Studio on the server up to date with its GitHub copy (SimplyDoorsAA/simplydoors-sign, main).
# Run on adem@optiplex-ai:  bash <(curl -fsSL https://raw.githubusercontent.com/SimplyDoorsAA/simplydoors-ops/main/deploy/studio-update.sh)
# Safe by design: it stops without changing anything if the server has Studio work that isn't on GitHub yet.
set -e
S=~/ai-server/services/signapp
cd "$S" || { echo "Can't find $S"; exit 1; }
say() { printf '\n==> %s\n' "$*"; }
if [ ! -d .git ]; then
  echo "STOP: $S isn't a git copy, so I can't update it this way. Nothing was changed."
  echo "Send Claude a screenshot of this:"; ls -la; type signapp-save 2>/dev/null | head -20; exit 1
fi
# The earlier one-off "Field app" link patch is part of this update, so put those two files back first.
for f in app/templates/admin.html app/static/style.css; do
  [ -f "$f.before-field" ] && mv -f "$f.before-field" "$f" && echo "Put back $f (the update includes the Field app link)."
done
say "Checking Studio against GitHub"
git fetch -q origin main || { echo "STOP: couldn't reach GitHub. Nothing was changed."; exit 1; }
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "STOP: Studio on the server has changes that aren't on GitHub yet. Nothing was changed."; git status --short; exit 1
fi
if ! git merge-base --is-ancestor HEAD origin/main; then
  echo "STOP: the server's Studio has saved work that isn't on GitHub. Run signapp-save first. Nothing was changed."; exit 1
fi
BEFORE=$(git rev-parse --short HEAD)
git merge -q --ff-only origin/main
echo "Code: $BEFORE -> $(git rev-parse --short HEAD)"
say "Rebuilding Studio (customers on a signing page see a short reload)"
cd ~/ai-server
docker compose build signapp
docker compose up -d signapp
echo "Waiting for Studio to come back..."
for i in $(seq 1 40); do sleep 3
  if docker exec signapp python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status==200 else 1)" 2>/dev/null; then break; fi
done
V=$(docker exec signapp python -c "import app.whatsnew as w; print(w.RELEASES[0][0])" 2>/dev/null || echo "?")
if [ "$V" = "?" ]; then echo "PROBLEM: Studio isn't answering. Send Claude a screenshot."; exit 1; fi
echo "DONE: Simply Studio is on Update $V."
echo "To undo: cd $S && git reset -q --hard $BEFORE && cd ~/ai-server && docker compose up -d --build signapp"

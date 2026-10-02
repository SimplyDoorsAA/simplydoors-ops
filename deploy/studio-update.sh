#!/bin/bash
# Brings Simply Studio on the server up to date with its private GitHub copy (SimplyDoorsAA/simplydoors-sign, main),
# using the same GitHub access signapp-save uses. Copies only the code; Studio's data folder is never touched.
# Run on adem@optiplex-ai:  bash <(curl -fsSL https://raw.githubusercontent.com/SimplyDoorsAA/simplydoors-ops/main/deploy/studio-update.sh)
# Safe by design: it stops without changing anything if the server's Studio code differs from what it expects.
set -e
S=~/ai-server/services/signapp
BASE=b5ebca3   # Update 25: what the server runs before this update
say() { printf '\n==> %s\n' "$*"; }
[ -d "$S/app" ] || { echo "STOP: can't find $S/app. Nothing was changed."; exit 1; }
# The earlier one-off "Field app" link patch is part of this update: put those two files back first.
for f in app/templates/admin.html app/static/style.css; do
  [ -f "$S/$f.before-field" ] && mv -f "$S/$f.before-field" "$S/$f" && echo "Put back $f (the update includes the Field app link)."
done

say "Downloading Studio from GitHub"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
GOT=""
for u in "git@github.com:SimplyDoorsAA/simplydoors-sign.git" "https://github.com/SimplyDoorsAA/simplydoors-sign.git"; do
  if GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
     git clone -q --depth 10 "$u" "$T/s" 2>/dev/null; then GOT=1; break; fi
done
[ -n "$GOT" ] || { echo "STOP: couldn't download Studio from GitHub (private repo). Nothing was changed."; \
  echo "Send Claude a screenshot of:  grep -c github ~/.local/bin/signapp-save; ls ~/.ssh"; exit 1; }
cd "$T/s"
git cat-file -e "$BASE^{commit}" 2>/dev/null || { echo "STOP: GitHub doesn't have the expected starting point. Nothing was changed."; exit 1; }

say "Checking the server's Studio is still Update 25 (so nothing newer gets overwritten)"
CHANGED=$(git --work-tree="$S" diff --name-only "$BASE" -- app Dockerfile requirements.txt .dockerignore | grep -v '^app/storage/' || true)
if [ -n "$CHANGED" ]; then
  echo "STOP: these Studio files on the server are different from Update 25, so I won't overwrite them. Nothing was changed:"
  echo "$CHANGED"; exit 1
fi
NEW=$(git log -1 --format=%h); echo "Server matches Update 25. Updating to $NEW."

say "Saving a copy of the current code, then updating"
KEEP=~/signapp-code-before-$(date +%Y%m%d-%H%M%S).tar.gz
tar czf "$KEEP" -C "$S" --exclude=./data .
git diff --name-only "$BASE" HEAD -- app Dockerfile requirements.txt .dockerignore README.md | while read -r f; do
  if [ -f "$f" ]; then mkdir -p "$S/$(dirname "$f")"; cp -f "$f" "$S/$f"; echo "  updated $f"; else rm -f "$S/$f"; echo "  removed $f"; fi
done

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

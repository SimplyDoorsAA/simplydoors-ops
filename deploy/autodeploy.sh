#!/usr/bin/env bash
# Hands-off updates for the ops app on the OptiPlex (adem@optiplex-ai). Cron runs this every 5 minutes.
# When GitHub's main has a new commit, it:
#   1. runs the tests on the server, in a throwaway container (fail = nothing changes);
#   2. keeps a copy of the running code, swaps in the new code, rebuilds and checks health;
#   3. if the new version isn't healthy, puts the previous code back and rebuilds that.
# Your phone gets an ntfy alert after every update or failure. A commit that failed is skipped until a newer one lands.
# Never touches .env, the data folder, docker-compose files or Tailscale: changes to those still need install.sh.
# Pause:  touch ~/ai-server/opsapp-autodeploy.pause     Resume: rm ~/ai-server/opsapp-autodeploy.pause
# Log:    ~/opsapp-autodeploy.log                        Set up: deploy/autodeploy-setup.sh
# All the work is inside main(), so bash has read the whole script before an update replaces this file.
set -uo pipefail

main() {
  export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  local SRC="$HOME/opsapp-src" STACK="$HOME/ai-server"
  local APPDIR="$STACK/opsapp" STATE="$STACK/opsapp-deployed-commit" FAILED="$STACK/opsapp-failed-commit"
  local PAUSE="$STACK/opsapp-autodeploy.pause" LOG="$HOME/opsapp-autodeploy.log" PORT=8010
  local BACKUP="$STACK/opsapp-code-before-update.tar.gz"

  exec 9>"$HOME/.opsapp-autodeploy.lock"
  flock -n 9 || return 0                         # an update is already running
  [ -f "$PAUSE" ] && return 0
  [ -d "$SRC/.git" ] && [ -d "$APPDIR" ] || { echo "autodeploy: run deploy/autodeploy-setup.sh first" >&2; return 1; }

  git -C "$SRC" fetch -q origin main 2>/dev/null || return 0   # no internet right now: try again next time
  local NEW OLD
  NEW=$(git -C "$SRC" rev-parse origin/main)
  OLD=$(cat "$STATE" 2>/dev/null || echo none)
  [ "$NEW" = "$OLD" ] && return 0
  [ "$NEW" = "$(cat "$FAILED" 2>/dev/null)" ] && return 0

  # Something to do: from here on, keep a log (trimmed so it never grows past ~1 MB).
  [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1000000 ] && tail -c 500000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  exec >>"$LOG" 2>&1
  local SHORT="${NEW:0:7}" SUBJECT
  git -C "$SRC" checkout -qf --detach "$NEW" && git -C "$SRC" clean -qfdx
  SUBJECT=$(git -C "$SRC" log -1 --format=%s)
  echo; echo "=== $(date '+%F %T') updating ${OLD:0:7} -> $SHORT: $SUBJECT"

  echo "Running the tests"
  if ! docker run --rm -v "$SRC":/src:ro python:3.12-slim sh -c \
      'cp -r /src /t && cd /t && pip install -q --root-user-action=ignore -r requirements.txt pytest aiosmtpd httpx \
       && python -m pytest -q -p no:cacheprovider tests'; then
    echo "$NEW" > "$FAILED"
    alert "Ops app NOT updated" "Tests failed on $SHORT ($SUBJECT). The app is unchanged. Log: ~/opsapp-autodeploy.log" high
    return 1
  fi

  echo "Swapping in the new code (copy of the current code: $BACKUP)"
  tar czf "$BACKUP" -C "$APPDIR" --exclude=./.env .
  replace_code "$APPDIR" "$SRC"
  if (cd "$STACK" && docker compose up -d --build opsapp) && healthy "$PORT"; then
    echo "$NEW" > "$STATE"; rm -f "$FAILED"
    echo "Updated to $SHORT"
    alert "Ops app updated" "$SHORT: $SUBJECT" default
    return 0
  fi

  echo "New version isn't healthy; putting the previous code back"
  docker logs --tail 40 opsapp 2>&1 | sed 's/^/  opsapp| /'
  find "$APPDIR" -mindepth 1 -maxdepth 1 ! -name '.env' -exec rm -rf {} +
  tar xzf "$BACKUP" -C "$APPDIR"
  echo "$NEW" > "$FAILED"
  if (cd "$STACK" && docker compose up -d --build opsapp) && healthy "$PORT"; then
    alert "Ops app update rolled back" "$SHORT ($SUBJECT) didn't start, so the previous version is back and running. Log: ~/opsapp-autodeploy.log" high
  else
    alert "OPS APP IS DOWN" "Update $SHORT failed AND the previous version didn't come back. Log: ~/opsapp-autodeploy.log" urgent
  fi
  return 1
}

# Same copy step as install.sh: everything but .env is replaced.
replace_code() {
  find "$1" -mindepth 1 -maxdepth 1 ! -name '.env' -exec rm -rf {} +
  cp -r "$2/app" "$2/Dockerfile" "$2/requirements.txt" "$2/.dockerignore" "$2/deploy" "$1/"
}

healthy() {
  local i
  for i in $(seq 1 40); do
    curl -sf --max-time 5 "http://127.0.0.1:$1/ops/healthz" | grep -q '"ok":true' && return 0
    sleep 3
  done
  return 1
}

# Phone alert through the same ntfy the app uses (its .env points at it from inside Docker).
alert() {
  local url
  url=$(grep '^NTFY_URL=' "$HOME/ai-server/opsapp/.env" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's#host\.docker\.internal#127.0.0.1#')
  echo "ALERT: $1 - $2"
  [ -n "$url" ] && curl -s --max-time 10 -H "Title: $1" -H "Priority: $3" -d "$2" "$url" >/dev/null || true
}

main "$@"; exit $?

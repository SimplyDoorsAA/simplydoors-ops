#!/usr/bin/env bash
# Customer form: make it reachable at https://optiplex-ai.tailf0af63.ts.net/start (Simply Studio's public address).
# Run on adem@optiplex-ai, after hours. Autodeploy never touches Tailscale; this script is the only thing that does,
# and it only ever adds or removes the one path /start. Studio's own setting is never changed.
#
#   bash ~/ai-server/opsapp/deploy/intake-setup.sh          add /start (safe to run again)
#   bash ~/ai-server/opsapp/deploy/intake-setup.sh undo     take /start off again
#
# Before the change it records how Studio answers (home page, sign-in path, health check, and one signing link if you
# paste one). After the change it checks Studio answers exactly the same. If anything differs, it puts the old
# setting back by itself and says so. Everything it sees is saved in ~/intake-funnel/.
set -uo pipefail

HOST="optiplex-ai.tailf0af63.ts.net"
PUBLIC="https://$HOST"
MOUNT="/start"
TARGET="http://127.0.0.1:8010/start"      # the ops app on this server; it answers the form at /start
MARK="Start your project"                 # text on the form page, to know it's really the form answering
KEEP="$HOME/intake-funnel"
STAMP=$(date +%Y%m%d-%H%M%S)
MODE="${1:-add}"
mkdir -p "$KEEP"
LOG="$KEEP/intake-setup-$STAMP.log"
exec > >(tee -a "$LOG") 2>&1

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!  %s\033[0m\n' "$*"; }
stop() { printf '\n\033[1;31mSTOPPED: %s\033[0m\n' "$*"; echo "Log: $LOG"; exit 1; }

case "$MODE" in add|undo) ;; *) stop "Use: bash intake-setup.sh   (or: bash intake-setup.sh undo)";; esac
command -v tailscale >/dev/null || stop "Tailscale isn't installed. Nothing was changed."
command -v python3 >/dev/null || stop "python3 isn't installed. Nothing was changed."
command -v curl >/dev/null || stop "curl isn't installed. Nothing was changed."

# ------------------------------------------------------------------ helpers
cfg() {   # reads Tailscale's saved setting (JSON). cfg start|funnel|root FILE, or cfg same FILE1 FILE2
  python3 - "$@" <<'PY'
import json, sys
HP, MOUNT = "optiplex-ai.tailf0af63.ts.net:443", "/start"
what, files = sys.argv[1], sys.argv[2:]
def load(p):
    try:
        t = open(p).read().strip()
        return json.loads(t) if t else {}
    except Exception:
        sys.exit(3)
def handlers(c):
    return (((c.get("Web") or {}).get(HP) or {}).get("Handlers") or {})
def without_start(c):
    c = json.loads(json.dumps(c))
    for m in (MOUNT, MOUNT + "/"):       # Tailscale may keep it with or without the end slash
        handlers(c).pop(m, None)
    return c
A = load(files[0])
if what == "start":        # what /start on port 443 points at now ("" = nothing)
    h = handlers(A).get(MOUNT) or handlers(A).get(MOUNT + "/") or {}
    print(h.get("Proxy") or ("something else" if h else ""))
elif what == "funnel":     # is port 443 public (Funnel on)?
    sys.exit(0 if (A.get("AllowFunnel") or {}).get(HP) else 1)
elif what == "root":       # does port 443 serve Studio at "/" the usual way?
    sys.exit(0 if handlers(A).get("/") else 1)
elif what == "same":       # the same setting, apart from /start?
    sys.exit(0 if without_start(A) == without_start(load(files[1])) else 1)
PY
}

grab() {   # saves Tailscale's current setting into a file
  sudo tailscale serve status --json > "$1" 2>/dev/null || return 1
  python3 -c "import json,sys; t=open(sys.argv[1]).read().strip(); json.loads(t) if t else None" "$1" 2>/dev/null
}

PATHS=("/" "/sso" "/healthz")              # Studio's home page, its sign-in path, its health check
snap() {   # one line per path: path, status code, content type, redirect, fingerprint of the page
  for p in "${PATHS[@]}"; do
    local hdr body code ctype loc sum
    hdr=$(mktemp); body=$(mktemp)
    code=$(curl -s -o "$body" -D "$hdr" -w '%{http_code}' --max-time 20 "$PUBLIC$p") || code="000"
    ctype=$(grep -i '^content-type:' "$hdr" | head -1 | cut -d: -f2- | tr -d ' \r' | cut -d';' -f1)
    loc=$(grep -i '^location:' "$hdr" | head -1 | cut -d: -f2- | tr -d ' \r')
    sum=$(sha256sum "$body" | cut -c1-16)
    rm -f "$hdr" "$body"
    echo "$p $code ${ctype:--} ${loc:--} $sum"
  done
}

compare() {   # compare BEFORE1 BEFORE2 AFTER: Studio answers the same? (a page that changes every load is checked
              # by its status, type and redirect only)
  python3 - "$@" <<'PY'
import sys
def read(p):
    out = {}
    for line in open(p):
        f = line.split()
        if f:
            out[f[0]] = f[1:]
    return out
b1, b2, af = (read(p) for p in sys.argv[1:4])
bad = []
for path, v in b1.items():
    w = af.get(path)
    fixed_page = b2.get(path) == v
    if not w or w[:3] != v[:3] or (fixed_page and w[3] != v[3]):
        bad.append(f"  {path}: before {' '.join(v[:3])}, after {' '.join((w or ['nothing'])[:3])}"
                   + ("" if not w or w[:3] != v[:3] else " (same code, different page)"))
print("\n".join(bad))
sys.exit(1 if bad else 0)
PY
}

steady() {   # steady BEFORE1 BEFORE2: Studio gave the same codes twice in a row (so a later difference means something)
  [ "$(cut -d' ' -f1-4 "$1")" = "$(cut -d' ' -f1-4 "$2")" ]
}

remove_start() {
  sudo tailscale funnel --https=443 --set-path "$MOUNT" off
}

rebuild() {   # rebuild SAVED_JSON NOW_JSON: set port 443's paths back the way they were saved (Studio's "/" included)
  python3 - "$@" <<'PY'
import json, subprocess, sys
HP, PORT = "optiplex-ai.tailf0af63.ts.net:443", "443"
def load(p):
    t = open(p).read().strip()
    return json.loads(t) if t else {}
before, now = load(sys.argv[1]), load(sys.argv[2])
hb = ((before.get("Web") or {}).get(HP) or {}).get("Handlers") or {}
hn = ((now.get("Web") or {}).get(HP) or {}).get("Handlers") or {}
public = bool((before.get("AllowFunnel") or {}).get(HP))
cmds = []
for mount, h in hb.items():
    if hn.get(mount) == h and (not public or (now.get("AllowFunnel") or {}).get(HP)):
        continue
    target = h.get("Proxy") or h.get("Path") or ("text:" + h["Text"] if h.get("Text") else "")
    if not target:
        print(f"  can't rebuild {mount} by itself: {h}"); continue
    cmds.append(["funnel" if public else "serve", "--bg", "--https=" + PORT, "--set-path", mount, target])
for mount in hn:
    if mount not in hb:
        cmds.append(["funnel" if public else "serve", "--https=" + PORT, "--set-path", mount, "off"])
for c in cmds:
    print("  sudo tailscale " + " ".join(c))
    subprocess.run(["sudo", "tailscale", *c])
PY
}

put_back() {   # put_back SAVED_JSON: take /start off again and check the setting is what it was
  warn "Putting the old Funnel setting back."
  remove_start || true
  sleep 3
  local now="$KEEP/after-putback-$STAMP.json"
  if grab "$now" && ! { cfg same "$1" "$now" && { ! cfg funnel "$1" || cfg funnel "$now"; }; }; then
    echo "Taking /start off wasn't enough. Setting Studio's paths back from $1:"
    rebuild "$1" "$now"
    sleep 3
    grab "$now" || true
  fi
  if cfg same "$1" "$now" && [ -z "$(cfg start "$now")" ] && { ! cfg funnel "$1" || cfg funnel "$now"; }; then
    echo "Put back: Tailscale's setting is the same as before. Studio wasn't left changed."
  else
    warn "The setting doesn't match what it was before. The old one is saved in $1."
    sudo tailscale serve status 2>&1 || true
    echo "Send Claude this whole output. Studio's address is $PUBLIC/ - check it opens."
  fi
}

check_form() {   # the form answers at $PUBLIC/start, and its script loads from under /start
  local page js
  page=$(curl -s --max-time 20 "$PUBLIC$MOUNT") || page=""
  js=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$PUBLIC$MOUNT/static/intake.js") || js="000"
  echo "Form page: $(printf '%s' "$page" | grep -c "$MARK") match(es) for \"$MARK\"; its script: HTTP $js"
  printf '%s' "$page" | grep -q "$MARK" && [ "$js" = "200" ]
}

# ------------------------------------------------------------------ what's there now
say "Saving Tailscale's current setting"
BEFORE="$KEEP/before-$STAMP.json"
grab "$BEFORE" || stop "Couldn't read Tailscale's setting (sudo tailscale serve status --json). Nothing was changed."
sudo tailscale funnel status 2>&1 | tee "$KEEP/before-$STAMP.txt"
echo "Saved: $BEFORE"
NOW_START=$(cfg start "$BEFORE")

if [ "$MODE" = "undo" ]; then
  say "Taking $MOUNT off Studio's public address"
  [ -z "$NOW_START" ] && { echo "$MOUNT isn't set up, so there's nothing to undo. Nothing was changed."; exit 0; }
  [ "$NOW_START" = "$TARGET" ] || stop "$MOUNT points at '$NOW_START', not the ops app. I won't touch it. Nothing was changed."
  snap > "$KEEP/studio-a-$STAMP.txt"; sleep 2; snap > "$KEEP/studio-b-$STAMP.txt"
  remove_start || stop "Tailscale refused the change (see above). Nothing was changed."
  sleep 3
  AFTER="$KEEP/after-undo-$STAMP.json"
  grab "$AFTER" || warn "Couldn't read Tailscale's setting after the change."
  snap > "$KEEP/studio-after-$STAMP.txt"
  OK=1
  [ -z "$(cfg start "$AFTER")" ] || { warn "$MOUNT is still there."; OK=0; }
  cfg same "$BEFORE" "$AFTER" || { warn "Something other than $MOUNT changed in Tailscale's setting."; OK=0; }
  if cfg funnel "$BEFORE" && ! cfg funnel "$AFTER"; then warn "Studio's address is no longer public (Funnel went off)."; OK=0; fi
  if ! DIFF=$(compare "$KEEP/studio-a-$STAMP.txt" "$KEEP/studio-b-$STAMP.txt" "$KEEP/studio-after-$STAMP.txt"); then
    warn "Studio answers differently than before:"; echo "$DIFF"; OK=0
  fi
  if [ $OK = 1 ]; then
    say "Done: $MOUNT is off. Studio answers exactly as before."
    echo "Customers who open the form link now get Studio's 'not found' page. To put it back: bash $0"
    exit 0
  fi
  stop "Undo finished, but the checks above didn't all pass. The setting from before the undo is saved in $BEFORE. Send Claude this output."
fi

# ------------------------------------------------------------------ add /start
if [ "$NOW_START" = "$TARGET" ]; then
  say "$MOUNT is already set up. Checking it still works."
  if check_form; then
    echo "Already done: $PUBLIC$MOUNT is the customer form. Nothing was changed."
    exit 0
  fi
  stop "$MOUNT is set up but the form isn't answering. Is the ops app running? (docker ps | grep opsapp). Nothing was changed."
fi
[ -z "$NOW_START" ] || stop "$MOUNT already points at '$NOW_START'. I won't change it. Nothing was changed."
cfg root "$BEFORE" || stop "Port 443 isn't set up the way I expected (no web setting at /). Nothing was changed. Send Claude the status above."
cfg funnel "$BEFORE" || stop "Studio's address isn't public (Funnel is off on port 443). Nothing was changed. Send Claude the status above."

say "Checking the ops app has the form"
LOCAL=$(curl -s --max-time 15 "$TARGET") || LOCAL=""
printf '%s' "$LOCAL" | grep -q "$MARK" || stop "The ops app on this server doesn't show the form at $TARGET yet. It arrives with autodeploy about 5 minutes after the change is merged. Nothing was changed."
echo "The ops app answers the form at $TARGET."

say "Checking Studio doesn't already use $MOUNT"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$PUBLIC$MOUNT") || CODE="000"
echo "Studio answers $PUBLIC$MOUNT with HTTP $CODE"
[ "$CODE" = "404" ] || stop "Studio already answers $MOUNT (HTTP $CODE), so it may be using it. Nothing was changed. Tell Claude: use /project instead."

if [ -z "${SIGN_PATH:-}" ] && [ -t 0 ]; then
  echo
  echo "Optional: paste one Studio signing link to check as well (use a test or already-signed document:"
  read -r -p "opening it counts as a view in Studio), or press Enter to skip: " SIGN_LINK
  SIGN_PATH="${SIGN_LINK#$PUBLIC}"
fi
if [ -n "${SIGN_PATH:-}" ]; then
  case "$SIGN_PATH" in /*) PATHS+=("$SIGN_PATH") ;; *) warn "That doesn't look like a link on $PUBLIC, so it's skipped." ;; esac
fi

say "Recording how Studio answers before the change"
snap > "$KEEP/studio-a-$STAMP.txt"; sleep 3; snap > "$KEEP/studio-b-$STAMP.txt"
cat "$KEEP/studio-a-$STAMP.txt"
steady "$KEEP/studio-a-$STAMP.txt" "$KEEP/studio-b-$STAMP.txt" \
  || stop "Studio answered differently from one moment to the next, so a before/after check wouldn't mean anything. Nothing was changed. Try again later."

say "Adding $MOUNT -> the ops app (only this path; Studio's setting stays as it is)"
if ! sudo tailscale funnel --bg --https=443 --set-path "$MOUNT" "$TARGET"; then
  AFTER="$KEEP/after-$STAMP.json"
  if grab "$AFTER" && cfg same "$BEFORE" "$AFTER" && [ -z "$(cfg start "$AFTER")" ]; then
    stop "Tailscale refused the change (see above). Nothing was changed."
  fi
  put_back "$BEFORE"
  stop "Tailscale gave an error part way. The old setting was put back (see above)."
fi
sleep 3

say "Checking Studio answers exactly the same after the change"
AFTER="$KEEP/after-$STAMP.json"
OK=1
if ! grab "$AFTER"; then warn "Couldn't read Tailscale's setting after the change."; OK=0; fi
if [ $OK = 1 ]; then
  [ "$(cfg start "$AFTER")" = "$TARGET" ] || { warn "$MOUNT doesn't point at the ops app."; OK=0; }
  cfg same "$BEFORE" "$AFTER" || { warn "Something other than $MOUNT changed in Tailscale's setting."; OK=0; }
  cfg funnel "$AFTER" || { warn "Studio's address is no longer public (Funnel went off)."; OK=0; }
fi
snap > "$KEEP/studio-after-$STAMP.txt"
cat "$KEEP/studio-after-$STAMP.txt"
if ! DIFF=$(compare "$KEEP/studio-a-$STAMP.txt" "$KEEP/studio-b-$STAMP.txt" "$KEEP/studio-after-$STAMP.txt"); then
  warn "Studio answers differently than before:"; echo "$DIFF"; OK=0
fi
say "Checking the form answers at $PUBLIC$MOUNT"
check_form || { warn "The form doesn't answer at $PUBLIC$MOUNT."; OK=0; }

if [ $OK = 0 ]; then
  put_back "$BEFORE"
  stop "Not everything checked out, so the change was undone. Send Claude this whole output."
fi

say "Done"
cat <<EOF
Customer form:  $PUBLIC$MOUNT   (no :10000)
Studio:         answers exactly as before (checked: ${PATHS[*]})
Saved:          $KEEP/   (Tailscale's setting before and after, Studio's answers, this log)
To undo:        bash $0 undo
Check from your phone on mobile data (not Wi-Fi): open $PUBLIC$MOUNT
EOF

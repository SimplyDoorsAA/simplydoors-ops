#!/usr/bin/env bash
# Service Fusion: save the API key for the ops app, then report how Service Fusion lays out its data.
# Run on adem@optiplex-ai. Nothing is changed in Service Fusion (read-only), and no customer details
# are printed: only field NAMES, status names and counts. The output is also saved to ~/sf-probe.txt.
set -euo pipefail
ENVF="$HOME/ai-server/opsapp/.env"
[ -f "$ENVF" ] || { echo "STOPPED: can't find $ENVF. Install the ops app first."; exit 1; }

if grep -q '^SF_CLIENT_ID=' "$ENVF" && grep -q '^SF_CLIENT_SECRET=' "$ENVF"; then
  read -r -p "A Service Fusion key is already saved. Replace it? [y/N]: " ANS
else
  ANS=y
fi
if [[ "${ANS:-n}" =~ ^[Yy] ]]; then
  echo "In Service Fusion: My Office > Developer Settings > API Credentials."
  read -r -p "Client ID: " SF_ID
  read -r -s -p "Client Secret (typing is hidden): " SF_SECRET; echo
  [ -n "$SF_ID" ] && [ -n "$SF_SECRET" ] || { echo "STOPPED: both are needed. Nothing saved."; exit 1; }
  cp "$ENVF" "$ENVF.bak-$(date +%Y%m%d-%H%M%S)"
  grep -v -e '^SF_CLIENT_ID=' -e '^SF_CLIENT_SECRET=' "$ENVF" > "$ENVF.tmp" || true
  printf 'SF_CLIENT_ID=%s\nSF_CLIENT_SECRET=%s\n' "$SF_ID" "$SF_SECRET" >> "$ENVF.tmp"
  mv "$ENVF.tmp" "$ENVF"; chmod 600 "$ENVF"
  echo "Saved in $ENVF (only you can read that file)."
fi

# read only the two Service Fusion lines (the file has other settings with spaces that the shell can't load)
export SF_CLIENT_ID="$(grep '^SF_CLIENT_ID=' "$ENVF" | tail -1 | cut -d= -f2-)"
export SF_CLIENT_SECRET="$(grep '^SF_CLIENT_SECRET=' "$ENVF" | tail -1 | cut -d= -f2-)"
python3 - <<'PY' 2>&1 | tee "$HOME/sf-probe.txt"
import json, os, sys, urllib.parse, urllib.request, urllib.error

API = "https://api.servicefusion.com"
SAFE_VALUES = {"status", "sub_status", "category", "source", "code", "is_custom", "type"}   # never "name": that can be a customer

def req(path, data=None, token=None, form=False):
    url = API + path
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"null")
        except Exception:
            msg = None
        return e.code, dict(e.headers), msg

def shape(v, path="", out=None, depth=0):
    """Field names and types only. Values are shown only for harmless keys like status names."""
    out = [] if out is None else out
    if isinstance(v, dict):
        for k, x in v.items():
            p = f"{path}.{k}" if path else k
            if isinstance(x, (dict, list)) and depth < 4:
                out.append(f"  {p}: {'object' if isinstance(x, dict) else 'list[' + str(len(x)) + ']'}")
                shape(x, p, out, depth + 1)
            else:
                t = type(x).__name__
                hint = f" = {x!r}" if k in SAFE_VALUES and isinstance(x, (str, int, bool)) else ""
                out.append(f"  {p}: {t}{hint}")
    elif isinstance(v, list) and v:
        shape(v[0], path + "[0]", out, depth + 1)
    return out

def total(h):
    for k, v in h.items():
        if k.lower() == "x-pagination-total-count":
            return v
    return "?"

cid, sec = os.environ.get("SF_CLIENT_ID"), os.environ.get("SF_CLIENT_SECRET")
if not (cid and sec):
    sys.exit("STOPPED: no Service Fusion key saved.")
cred = {"grant_type": "client_credentials", "client_id": cid, "client_secret": sec}
st, _, tok = req("/oauth/access_token", cred)
if st != 200:
    st, _, tok = req("/oauth/access_token", cred, form=True)
if st != 200 or not tok or "access_token" not in tok:
    sys.exit(f"STOPPED: Service Fusion refused the key (HTTP {st}). Check the Client ID and Secret.")
T = tok["access_token"]
print("== Sign-in: OK")

def items(body):
    return body.get("items", body) if isinstance(body, dict) else body

st, _, b = req("/v1/job-statuses?per-page=50", token=T)
print(f"\n== Job statuses (HTTP {st})")
for it in (items(b) or []):
    if isinstance(it, dict):
        print("  ", {k: v for k, v in it.items() if k in ("id", "code", "name", "is_custom", "category")})

for label, path in [
    ("Newest job, plain", "/v1/jobs?per-page=1&sort=-created_at"),
    ("Newest job, with extras", "/v1/jobs?per-page=1&sort=-created_at&expand=agents,techs_assigned,contact,location,customer"),
    ("Newest customer, with contacts", "/v1/customers?per-page=1&sort=-created_at&expand=contacts,contacts.emails,contacts.phones,locations"),
]:
    st, h, b = req(path, token=T)
    print(f"\n== {label} (HTTP {st}, total {total(h)})")
    if st == 200:
        rows = items(b) or []
        print("\n".join(shape(rows[0])) if rows else "  (none)")
    else:
        print("  error:", json.dumps(b)[:300])

print("\n== Filter tests (counts only)")
for label, q in [
    ("status = '2 Scheduled Consult'", {"filters[status]": "2 Scheduled Consult"}),
    ("status = 'Scheduled Consult'", {"filters[status]": "Scheduled Consult"}),
    ("sub_status = 'Consultation'", {"filters[sub_status]": "Consultation"}),
    ("status = '10 Install Scheduled'", {"filters[status]": "10 Install Scheduled"}),
    ("start_date from 2026-09-29", {"filters[start_date][gte]": "2026-09-29"}),
    ("updated since 2026-10-05", {"filters[updated_at][gte]": "2026-10-05"}),
]:
    st, h, b = req("/v1/jobs?per-page=1&" + urllib.parse.urlencode(q), token=T)
    print(f"  {label}: HTTP {st}, total {total(h)}")
print("\nDone. Nothing was changed in Service Fusion.")
PY
echo "Saved a copy of this report in ~/sf-probe.txt"

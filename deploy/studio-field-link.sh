#!/bin/bash
# One-time: adds the "Field app" link to Simply Studio's left menu on the server (only that; nothing else in Studio changes).
# Run on adem@optiplex-ai:  curl -fsSL https://raw.githubusercontent.com/SimplyDoorsAA/simplydoors-ops/main/deploy/studio-field-link.sh | bash
set -e
S=~/ai-server/services/signapp
cd "$S"
if grep -q 'tab-field' app/templates/admin.html; then echo "Field app link is already in Studio."; else
  cp app/templates/admin.html app/templates/admin.html.before-field
  cp app/static/style.css app/static/style.css.before-field
  python3 - <<'PY'
p='app/templates/admin.html'
s=open(p).read()
a='    <button type="button" class="tab tab-activity" data-tab="activity"'
assert s.count(a)==1, "menu not found - nothing changed"
link='    <a class="tab tab-field" href="https://optiplex-ai.tailf0af63.ts.net:10000/ops/" target="_blank" rel="noopener" title="SimplyDoors Field app: receiving, delivery, installs, measures and the other job reports (opens in a new tab)"><svg class="tab-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2 M10 7h4 M10 11h4"/></svg><span class="tab-txt">Field app ↗</span></a>\n'
open(p,'w').write(s.replace(a, link+a))
open('app/static/style.css','a').write('''
/* link out to the SimplyDoors Field app (ops app), just above Activity in the menu; not in the phone's bottom bar */
.studio-rail .tabs a.tab-field { order: 98; text-decoration: none; }
@media (min-width: 701px) { .studio-rail .tabs a.tab-field { margin-top: auto; } .studio-rail .tabs .tab-field ~ .tab-activity { margin-top: 0; } }
@media (max-width: 700px) { .studio-rail .tabs a.tab-field { display: none; } }
''')
print("Field app link added.")
PY
fi
cd ~/ai-server
docker compose build signapp
docker compose up -d signapp
echo "Waiting for Studio to come back..."
for i in $(seq 1 30); do sleep 3; if docker exec signapp python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status==200 else 1)" 2>/dev/null; then break; fi; done
docker exec signapp grep -c 'tab-field' app/templates/admin.html >/dev/null && echo "DONE: Studio is running with the Field app link." || echo "PROBLEM: send Claude a screenshot."
echo "To undo: cd $S && mv app/templates/admin.html.before-field app/templates/admin.html && mv app/static/style.css.before-field app/static/style.css && cd ~/ai-server && docker compose up -d --build signapp"

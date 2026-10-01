/* SimplyDoors Operations — staff app.
   Reports are saved on the phone first (outbox) and only removed once the server
   confirms it has them, so nothing is lost on a bad signal. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  let me = null;            // {name, dept, is_admin, sales_reps, locations}
  let photos = {};          // slot -> Blob for the open form
  let photoMeta = {};       // slot -> {status, lat, lon, acc, at, fileAge}
  let previews = {};        // slot -> object URL
  let startedAt = null;
  let pickSlot = null;
  let flushing = false;

  // ------------------------------------------------------------ storage (IndexedDB)
  let dbp = null;
  function db() {
    if (!dbp) dbp = new Promise((res, rej) => {
      const r = indexedDB.open("sdops", 1);
      r.onupgradeneeded = () => {
        r.result.createObjectStore("outbox", { keyPath: "id" });
        r.result.createObjectStore("drafts");
      };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
    return dbp;
  }
  async function idb(store, mode, fn) {
    const d = await db();
    return new Promise((res, rej) => {
      const tx = d.transaction(store, mode);
      const req = fn(tx.objectStore(store));
      tx.oncomplete = () => res(req ? req.result : undefined);
      tx.onerror = () => rej(tx.error);
      tx.onabort = () => rej(tx.error);
    });
  }
  const outboxAll = () => idb("outbox", "readonly", s => s.getAll());
  const outboxPut = (e) => idb("outbox", "readwrite", s => s.put(e));
  const outboxDel = (id) => idb("outbox", "readwrite", s => s.delete(id));
  const draftKey = (slug) => `${me ? me.id : "?"}|${slug}`;
  const draftGet = (slug) => idb("drafts", "readonly", s => s.get(draftKey(slug)));
  const draftPut = (slug, v) => idb("drafts", "readwrite", s => s.put(v, draftKey(slug)));
  const draftDel = (slug) => idb("drafts", "readwrite", s => s.delete(draftKey(slug)));

  // ------------------------------------------------------------ server calls
  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }
  async function api(path, opts = {}) {
    let r;
    try {
      r = await fetch(path, { credentials: "same-origin", ...opts,
        headers: { "X-SD-App": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
        body: opts.json ? JSON.stringify(opts.json) : opts.body });
    } catch (e) {
      throw new ApiError(0, "No connection");
    }
    let data = null;
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) throw new ApiError(r.status, (data && data.detail) || `Server error (${r.status})`);
    return data;
  }

  // ------------------------------------------------------------ views
  function show(id) {
    $$(".view").forEach(v => v.classList.add("hidden"));
    $("#" + id).classList.remove("hidden");
    window.scrollTo(0, 0);
  }
  function setUser() {
    $("#userBox").classList.toggle("hidden", !me);
    if (me) {
      $("#userName").textContent = me.name;
      $("#adminLink").classList.toggle("hidden", !me.is_admin);
    }
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------ sign in
  async function showLogin() {
    me = null; setUser(); show("viewLogin");
    let dir = null;
    try { dir = await api("api/directory"); localStorage.setItem("sdops_dir", JSON.stringify(dir)); }
    catch (e) { dir = JSON.parse(localStorage.getItem("sdops_dir") || "null"); }
    const pills = $("#deptPills");
    pills.innerHTML = "";
    if (!dir) { pills.innerHTML = `<p class="error">Can't reach the server. Check your signal and reload.</p>`; return; }
    Object.keys(dir).forEach(dept => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "pill"; b.textContent = dept;
      b.onclick = () => {
        $$(".pill").forEach(p => p.classList.remove("active")); b.classList.add("active");
        const sel = $("#loginName");
        sel.innerHTML = `<option value="" disabled selected>Select your name…</option>` +
          dir[dept].map(n => `<option>${esc(n)}</option>`).join("");
        $("#loginForm").classList.remove("hidden");
        $("#loginError").classList.add("hidden");
      };
      pills.appendChild(b);
    });
  }
  $("#loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = $("#loginName").value, pin = $("#loginPin").value.trim();
    const err = $("#loginError"), btn = $("#loginBtn");
    if (!name || !pin) { err.textContent = "Pick your name and enter your PIN."; err.classList.remove("hidden"); return; }
    btn.disabled = true; btn.textContent = "Checking…";
    try {
      await api("api/login", { method: "POST", json: { name, pin } });
      $("#loginPin").value = "";
      await loadMe();
    } catch (e) {
      err.textContent = e.status === 0 ? "No connection. Check your signal and try again." : e.message;
      err.classList.remove("hidden");
    } finally { btn.disabled = false; btn.textContent = "Sign in"; }
  });

  $("#logoutBtn").addEventListener("click", async () => {
    const mine = (await outboxAll()).filter(e => me && e.userId === me.id);
    if (mine.length && !confirm(`You have ${mine.length} report(s) on this phone that haven't sent yet. They'll stay here and send next time you sign in. Sign out anyway?`)) return;
    try { await api("api/logout", { method: "POST" }); } catch (e) { /* offline: cookie stays but we forget the user */ }
    localStorage.removeItem("sdops_me");
    showLogin(); updateBanner();
  });

  async function loadMe() {
    try {
      me = await api("api/me");
      localStorage.setItem("sdops_me", JSON.stringify(me));
    } catch (e) {
      if (e.status === 401) { localStorage.removeItem("sdops_me"); return showLogin(); }
      me = JSON.parse(localStorage.getItem("sdops_me") || "null");   // offline: keep working
      if (!me) return showLogin();
    }
    setUser();
    if (!(await maybeShowLocationScreen())) showHome();
    flushOutbox();
  }

  // ------------------------------------------------------------ photo location
  // Asked once (the screen below), used only when a photo is added, never in the background.
  const GEO_KEY = "sdops_geo_choice";          // "yes" | "no"
  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  // Which iPhone browser is this? Each keeps its own location setting.
  const iosBrowser = !isIOS ? null : /CriOS/.test(navigator.userAgent) ? "Chrome"
    : /FxiOS/.test(navigator.userAgent) ? "Firefox" : /EdgiOS/.test(navigator.userAgent) ? "Edge" : "Safari";
  function fixSteps() {
    if (iosBrowser === "Safari") return "Settings → Privacy & Security → Location Services (on) → Safari Websites → While Using the App, and Settings → Apps → Safari → Location → Allow";
    if (iosBrowser) return `Settings → Privacy & Security → Location Services (on) → ${iosBrowser} → While Using the App`;
    return "your browser's site settings for this page → Location → Allow";
  }
  let lastFix = null;                          // reused for 2 minutes so the phone isn't asked twice in a row

  async function geoPermission() {
    if (!("geolocation" in navigator)) return "unsupported";
    try { return (await navigator.permissions.query({ name: "geolocation" })).state; }   // granted | prompt | denied
    catch (e) { return "unknown"; }
  }
  function getFix(timeoutMs = 12000) {
    return new Promise(res => {
      if (!("geolocation" in navigator)) return res({ status: "unsupported" });
      if (lastFix && Date.now() - lastFix.t < 120000) return res(lastFix.fix);
      navigator.geolocation.getCurrentPosition(
        p => { const fix = { status: "ok", lat: p.coords.latitude, lon: p.coords.longitude, acc: Math.round(p.coords.accuracy) };
               lastFix = { t: Date.now(), fix }; res(fix); },
        err => res({ status: err.code === 1 ? "denied" : err.code === 3 ? "timeout" : "unavailable" }),
        { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 60000 });
    });
  }
  // Called when a photo is added. Only shows the phone's location question if the person said yes on our screen.
  async function locationForPhoto() {
    const perm = await geoPermission();
    if (perm === "unsupported") return { status: "unsupported" };
    if (perm === "denied") return { status: "denied" };
    if (perm !== "granted" && localStorage.getItem(GEO_KEY) === "no") return { status: "off" };
    const fix = await getFix();
    if (fix.status === "denied") localStorage.setItem(GEO_KEY, "no");   // don't keep asking someone who said no
    return fix;
  }
  async function maybeShowLocationScreen() {
    if (localStorage.getItem(GEO_KEY)) return false;
    const perm = await geoPermission();
    if (perm === "granted") { localStorage.setItem(GEO_KEY, "yes"); return false; }
    if (perm === "unsupported" || perm === "denied") { localStorage.setItem(GEO_KEY, "no"); return false; }
    $("#iosTip").classList.toggle("hidden", iosBrowser !== "Safari");   // only Safari re-asks every day
    show("viewLocation");
    return true;
  }
  $("#geoYes").addEventListener("click", async () => {
    const btn = $("#geoYes"); btn.disabled = true; btn.textContent = "Waiting for your phone…";
    const fix = await getFix();
    localStorage.setItem(GEO_KEY, fix.status === "denied" ? "no" : "yes");
    btn.disabled = false; btn.textContent = "Turn on photo location";
    showHome();
  });
  $("#geoNo").addEventListener("click", () => { localStorage.setItem(GEO_KEY, "no"); showHome(); });

  async function updateGeoNote() {
    const note = $("#geoNote");
    const perm = await geoPermission();
    const off = perm === "denied" || (perm !== "granted" && localStorage.getItem(GEO_KEY) === "no");
    if (!off) { note.classList.add("hidden"); return; }
    note.innerHTML = perm === "denied"
      ? `Photo location is blocked${iosBrowser ? ` for ${iosBrowser}` : ""} on this phone, so photos are marked “No location”. To turn it on: ${fixSteps()}. Then come back and reload this page.`
      : `Photo location is off, so photos are marked “No location”. <button type="button" class="link" id="geoAgain">Turn it on</button>`;
    note.classList.remove("hidden");
    const again = $("#geoAgain");
    if (again) again.onclick = () => { localStorage.removeItem(GEO_KEY); maybeShowLocationScreen(); };
  }

  // ------------------------------------------------------------ first-time setup (invite link) + walkthrough
  let setupCode = null, setupStage = "code";
  function readLinkCode() {
    const m = location.hash.match(/^#setup=([A-Za-z0-9-]+)/);
    if (!m) return null;
    history.replaceState(null, "", location.pathname + location.search);   // don't leave the code in the address bar
    return m[1];
  }
  function setupError(msg) { const e = $("#setupError"); e.textContent = msg; e.classList.toggle("hidden", !msg); }
  async function openSetup(code) {
    me = null; setUser();
    setupStage = "code"; setupCode = null;
    $("#setupHello").textContent = "Set up your account";
    $("#codeBox").classList.remove("hidden"); $("#pinBox").classList.add("hidden");
    $("#newPin").value = ""; $("#newPin2").value = "";
    $("#setupCode").value = code || "";
    $("#setupBtn").textContent = "Next";
    setupError("");
    show("viewSetup");
    if (code) await checkCode(code);
  }
  async function checkCode(code) {
    const btn = $("#setupBtn"); btn.disabled = true;
    try {
      const r = await api("api/setup/check", { method: "POST", json: { code } });
      setupCode = code; setupStage = "pin";
      $("#setupHello").textContent = `Hi ${r.name.split(" ")[0]}!`;
      $("#codeBox").classList.add("hidden"); $("#pinBox").classList.remove("hidden");
      btn.textContent = "Create my PIN"; setupError("");
      setTimeout(() => $("#newPin").focus(), 50);
    } catch (e) {
      setupError(e.status === 0 ? "No connection. Check your signal and try again." : e.message);
    } finally { btn.disabled = false; }
  }
  $("#setupForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (setupStage === "code") {
      const code = $("#setupCode").value.trim();
      if (!code) return setupError("Type the setup code from your text.");
      return checkCode(code);
    }
    const p1 = $("#newPin").value.trim(), p2 = $("#newPin2").value.trim();
    if (!/^[0-9]{6,8}$/.test(p1)) return setupError("Your PIN must be 6 to 8 numbers.");
    if (p1 !== p2) return setupError("The two PINs don't match. Type them again.");
    const btn = $("#setupBtn"); btn.disabled = true; btn.textContent = "Saving…";
    try {
      await api("api/setup/complete", { method: "POST", json: { code: setupCode, pin: p1 } });
      $("#newPin").value = ""; $("#newPin2").value = "";
      me = await api("api/me");
      localStorage.setItem("sdops_me", JSON.stringify(me));
      localStorage.removeItem(GEO_KEY);          // new person on this phone: walk them through it
      setUser(); startOnboard(); flushOutbox();
    } catch (e) {
      setupError(e.status === 0 ? "No connection. Check your signal and try again." : e.message);
      if (e.status === 400) { setupStage = "code"; $("#codeBox").classList.remove("hidden"); $("#pinBox").classList.add("hidden"); }
    } finally { btn.disabled = false; btn.textContent = setupStage === "pin" ? "Create my PIN" : "Next"; }
  });
  $("#haveCode").addEventListener("click", () => openSetup(null));
  $("#setupBack").addEventListener("click", () => showLogin());

  const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const isAndroid = /Android/.test(navigator.userAgent);
  let installPrompt = null;
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installPrompt = e; $("#obInstall").classList.remove("hidden"); });

  function obStep(n) {
    ["obLocation", "obHomeScreen", "obSafety"].forEach((id, i) => $("#" + id).classList.toggle("hidden", i !== n));
    $("#dot3").classList.toggle("on", n >= 1); $("#dot4").classList.toggle("on", n >= 2);
    window.scrollTo(0, 0);
  }
  function startOnboard() {
    let steps;
    if (iosBrowser === "Safari") steps = `<b>On this iPhone:</b><ol>
        <li>Open <b>Settings</b> → <b>Privacy &amp; Security</b> → <b>Location Services</b>. Make sure it's <b>On</b>.</li>
        <li>Scroll down to <b>Safari Websites</b>. Choose <b>While Using the App</b> and turn on <b>Precise Location</b>.</li>
        <li>Go back to <b>Settings</b> → <b>Apps</b> → <b>Safari</b> → <b>Location</b> → <b>Allow</b> (so it doesn't ask every day).</li>
        <li>Come back here and tap the green button.</li></ol>`;
    else if (iosBrowser) steps = `<b>On this iPhone:</b><ol>
        <li>Open <b>Settings</b> → <b>Privacy &amp; Security</b> → <b>Location Services</b>. Make sure it's <b>On</b>.</li>
        <li>Scroll down to <b>${iosBrowser}</b>. Choose <b>While Using the App</b> and turn on <b>Precise Location</b>.</li>
        <li>Come back here, tap the green button, then tap <b>Allow</b> when asked.</li></ol>`;
    else steps = `Tap the green button. When your phone asks, choose <b>Allow</b> (or <b>While using the app</b>).`;
    $("#obLocSteps").innerHTML = steps;
    $("#obLocResult").className = "result-line hidden";
    $("#obLocTest").textContent = "Turn on & test my location";
    $("#obLocTest").dataset.done = "";

    let home;
    if (iosBrowser === "Safari") home = `<ol><li>Tap the <b>Share</b> button (square with an arrow) at the bottom of the screen.</li>
        <li>Scroll down and tap <b>Add to Home Screen</b>, then <b>Add</b>.</li></ol>`;
    else if (iosBrowser) home = `<ol><li>Tap the <b>Share</b> button (square with an arrow) next to the address bar.</li>
        <li>Tap <b>Add to Home Screen</b>, then <b>Add</b>.</li></ol>`;
    else if (isAndroid) home = `<ol><li>Tap the <b>⋮</b> menu at the top right.</li><li>Tap <b>Add to Home screen</b> or <b>Install app</b>.</li></ol>`;
    else home = `On a computer this is optional. You can bookmark this page instead.`;
    $("#obHomeSteps").innerHTML = home;
    show("viewOnboard"); obStep(0);
  }
  $("#obLocTest").addEventListener("click", async () => {
    const btn = $("#obLocTest"), out = $("#obLocResult");
    if (btn.dataset.done) return obStep(isStandalone() ? 2 : 1);
    btn.disabled = true; btn.textContent = "Checking with your phone…";
    lastFix = null;
    const fix = await getFix(15000);
    btn.disabled = false;
    if (fix.status === "ok") {
      localStorage.setItem(GEO_KEY, "yes");
      out.className = "result-line ok";
      out.textContent = `✓ Location works (accurate to about ${fix.acc} m).` + (fix.acc > 200 ? " Turn on Precise Location for a better stamp." : "");
      btn.textContent = "Next"; btn.dataset.done = "1";
    } else {
      out.className = "result-line bad";
      out.textContent = fix.status === "denied"
        ? `Location is blocked. Follow the steps above, then tap Try again.`
        : `Couldn't get a location (${fix.status === "timeout" ? "took too long" : "no GPS signal"}). Step outside or near a window and tap Try again.`;
      btn.textContent = "Try again";
    }
  });
  $("#obLocSkip").addEventListener("click", () => { localStorage.setItem(GEO_KEY, "no"); obStep(isStandalone() ? 2 : 1); });
  $("#obInstall").addEventListener("click", async () => {
    if (!installPrompt) return;
    installPrompt.prompt(); await installPrompt.userChoice.catch(() => {}); installPrompt = null;
    $("#obInstall").classList.add("hidden");
  });
  $("#obHomeNext").addEventListener("click", () => obStep(2));
  $("#obFinish").addEventListener("click", () => showHome());

  // ------------------------------------------------------------ home
  async function showHome() {
    show("viewHome");
    updateGeoNote();
    const cards = $("#formCards");
    cards.innerHTML = (me.forms || []).map(f => `<button class="card${f.admin_only ? " admin" : ""}" type="button" data-open="${esc(f.slug)}">
        <span class="card-title">${esc(f.type)}${me.is_admin && !f.staff_can_see ? ' <span class="pillnote">hidden from staff</span>' : ""}</span>
        <span class="card-sub">${esc(f.blurb)}</span></button>`).join("") || `<p class="muted">No forms are switched on yet.</p>`;
    const list = $("#recentList");
    const pending = (await outboxAll()).filter(e => e.userId === me.id);
    let rows = pending.map(e => `<li><span>${esc(e.type)}</span><span class="tag wait">${e.error ? "Needs fixing" : "Waiting to send"}</span></li>`);
    try {
      const sent = await api("api/my-reports");
      rows = rows.concat(sent.map(r => `<li><span>${esc(r.form_type)} · ${esc(fmtTime(r.submitted_at))}</span><span class="tag ok">${esc(r.receipt)} ✓</span></li>`));
    } catch (e) { if (!rows.length) rows.push(`<li class="muted">Can't load right now.</li>`); }
    list.innerHTML = rows.join("") || `<li class="muted">Nothing yet.</li>`;
  }
  function fmtTime(iso) {
    try { return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); }
    catch (e) { return iso; }
  }

  document.addEventListener("click", (ev) => {
    const open = ev.target.closest("[data-open]");
    if (open) openForm(open.dataset.open === "again" ? spec.slug : open.dataset.open);
    if (ev.target.closest("[data-home]")) showHome();
  });

  // ------------------------------------------------------------ forms (drawn from the server's definitions)
  const form = $("#genForm");
  let spec = null;                 // the form being filled in
  const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  const okdefName = (key, group, item) => `${key}:${slugify(group)}:${slugify(item)}`;
  const allSlots = () => spec.photos.flatMap(g => [...(g.slots || []).map(s => s[0]), ...(g.signature ? [g.signature] : [])]);
  const slotLabel = (slot) => { for (const g of spec.photos) { for (const [s, l] of (g.slots || [])) if (s === slot) return l; if (g.signature === slot) return "Signature"; } return slot; };

  function fieldHTML(f) {
    const id = "f_" + f.key, req = f.required ? " *" : "";
    const ask = esc(f.ask || f.label) + req;
    const help = f.help ? `<p class="muted small">${esc(f.help)}</p>` : "";
    const ph = f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : "";
    let inner = "";
    switch (f.type) {
      case "text": inner = `<label for="${id}">${ask}</label>${help}<input id="${id}" name="${esc(f.key)}" type="text" maxlength="300" autocomplete="off"${ph}>`; break;
      case "textarea": inner = `<label for="${id}">${ask}</label>${help}<textarea id="${id}" name="${esc(f.key)}" rows="4" maxlength="4000"${ph}></textarea>`; break;
      case "number": inner = `<label for="${id}">${ask}</label>${help}<input id="${id}" name="${esc(f.key)}" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="9">`; break;
      case "date": inner = `<label for="${id}">${ask}</label>${help}<input id="${id}" name="${esc(f.key)}" type="date">`; break;
      case "time": inner = `<label for="${id}">${ask}</label>${help}<input id="${id}" name="${esc(f.key)}" type="time">`; break;
      case "select": {
        const people = f.options && f.options.length && /^\d+$/.test(String(f.options[0][0]));
        const first = f.none_label ? `<option value="none">${esc(f.none_label)}</option>`
          : `<option value="" disabled selected>${people ? "Pick a person…" : "Pick one…"}</option>`;
        inner = `<label for="${id}">${ask}</label>${help}<select id="${id}" name="${esc(f.key)}">${first}` +
          f.options.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("") +
          (f.allow_other ? `<option value="Custom">Something else (type it)</option>` : "") + `</select>` +
          (f.allow_other ? `<input name="${esc(f.key)}_custom" type="text" class="hidden other" maxlength="120" placeholder="Type it">` : "");
        break;
      }
      case "choice":
        inner = `<fieldset><legend>${ask}</legend>${help}<div class="choice">` +
          f.options.map(([v, l]) => `<label><input type="radio" name="${esc(f.key)}" value="${esc(v)}"><span>${esc(l)}</span></label>`).join("") + `</div></fieldset>`;
        break;
      case "checks":
        inner = `<fieldset><legend>${ask}</legend>${help}` +
          f.items.map(([k, t]) => `<label class="check"><input type="checkbox" name="${esc(k)}"><span>${esc(t)}</span></label>`).join("") + `</fieldset>`;
        break;
      case "okdef":
        inner = `<div class="okdef-head"><b>${esc(f.label)}${req}</b><button type="button" class="link" data-allok="${esc(f.key)}">Mark all OK</button></div>` +
          Object.entries(f.groups).map(([g, items]) => `<h3>${esc(g)}</h3>` + items.map(it => {
            const n = okdefName(f.key, g, it);
            return `<div class="okrow" data-row="${esc(n)}"><span>${esc(it)}</span><div class="okbtns">
              <label><input type="radio" name="${esc(n)}" value="OK"><span>OK</span></label>
              <label class="bad"><input type="radio" name="${esc(n)}" value="Defective"><span>Defective</span></label></div></div>`;
          }).join("")).join("");
        break;
    }
    const cond = f.show_if ? ` data-show-field="${esc(f.show_if.field)}" data-show-in="${esc(f.show_if.in.join("|"))}"` : "";
    return `<div class="fld" data-key="${esc(f.key)}"${cond}>${inner}</div>`;
  }

  function photosHTML() {
    return spec.photos.map(g => g.signature
      ? `<div class="fld"><h2>${esc(g.title)}</h2>${g.help ? `<p class="muted small">${esc(g.help)}</p>` : ""}
          <div class="sigwrap" data-sigslot="${esc(g.signature)}">
            <button type="button" class="sigopen" data-sigopen="${esc(g.signature)}"><span class="sigpen" aria-hidden="true">✍</span> Tap to sign</button>
            <div class="sigdone hidden"><img alt="Signature"><div class="sigacts">
              <button type="button" class="link" data-sigopen="${esc(g.signature)}">Sign again</button>
              <button type="button" class="link danger" data-sigclear="${esc(g.signature)}">Remove</button></div></div>
          </div></div>`
      : `<div class="fld"><h2>${esc(g.title)}${g.min ? ` <span class="muted small">(at least ${g.min})</span>` : ""}</h2>
          ${g.help ? `<p class="muted small">${esc(g.help)}</p>` : ""}<div class="photos" data-group="${esc(g.group)}"></div></div>`).join("");
  }

  function applyConditions() {
    $$(".fld[data-show-field]", form).forEach(div => {
      const ctl = form.elements[div.dataset.showField];
      const val = ctl ? (ctl.value !== undefined ? ctl.value : "") : "";
      div.classList.toggle("hidden", !div.dataset.showIn.split("|").includes(val));
    });
    $$("select", form).forEach(sel => {
      const other = form.elements[sel.name + "_custom"];
      if (other) other.classList.toggle("hidden", sel.value !== "Custom");
    });
  }

  function renderTiles() {
    spec.photos.filter(g => !g.signature).forEach(g => {
      const box = $(`.photos[data-group="${g.group}"]`, form);
      box.innerHTML = "";
      g.slots.forEach(([slot, label]) => {
        const t = document.createElement("div");
        t.className = "tile" + (photos[slot] ? " filled" : "");
        t.dataset.slot = slot;
        t.setAttribute("role", "button"); t.tabIndex = 0;
        t.setAttribute("aria-label", photos[slot] ? `${label} added` : `Add ${label}`);
        if (photos[slot]) {
          if (!previews[slot]) previews[slot] = URL.createObjectURL(photos[slot]);
          const m = photoMeta[slot] || {};
          const badge = m.status === "ok" ? (m.acc > 200 ? `<span class="geo warn">📍 ±${m.acc} m</span>` : `<span class="geo">📍 Located</span>`)
            : m.status ? `<span class="geo warn">No location</span>` : "";
          t.innerHTML = `<img src="${previews[slot]}" alt="">${badge}<button type="button" class="remove" aria-label="Remove photo">×</button>`;
          t.querySelector(".remove").onclick = (e) => { e.stopPropagation(); removePhoto(slot); };
        } else {
          t.innerHTML = `<span><span class="plus">+</span>${esc(label)}</span>`;
        }
        t.onclick = () => { pickSlot = slot; $("#photoPicker").value = ""; $("#photoPicker").click(); };
        t.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); t.click(); } };
        box.appendChild(t);
      });
    });
  }
  function removePhoto(slot) {
    if (previews[slot]) URL.revokeObjectURL(previews[slot]);
    delete previews[slot]; delete photos[slot]; delete photoMeta[slot];
    renderTiles(); saveDraftSoon();
  }

  // ---- signature pad: opens full screen so the customer has room to sign; saved as an image, never location-stamped
  const sigPreviews = {};
  function renderSignatures() {
    $$(".sigwrap[data-sigslot]", form).forEach(w => {
      const slot = w.dataset.sigslot, blob = photos[slot];
      $(".sigopen", w).classList.toggle("hidden", !!blob);
      $(".sigdone", w).classList.toggle("hidden", !blob);
      if (sigPreviews[slot]) { URL.revokeObjectURL(sigPreviews[slot]); delete sigPreviews[slot]; }
      if (blob) { sigPreviews[slot] = URL.createObjectURL(blob); $(".sigdone img", w).src = sigPreviews[slot]; }
    });
  }
  function setupSignatures() {
    $$("[data-sigopen]", form).forEach(b => b.onclick = () => openSigPad(b.dataset.sigopen));
    $$("[data-sigclear]", form).forEach(b => b.onclick = () => {
      if (!confirm("Remove this signature?")) return;
      delete photos[b.dataset.sigclear]; renderSignatures(); saveDraftSoon();
    });
    renderSignatures();
  }

  const sig = { slot: null, strokes: [], cur: null, scrollY: 0 };
  const sigCv = $("#sigCanvas"), sigBox = $("#sigOverlay");
  function sigSize() {
    const r = sigCv.parentElement.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
    sigCv.width = Math.round(r.width * ratio); sigCv.height = Math.round(r.height * ratio);
    sigCv.style.width = r.width + "px"; sigCv.style.height = r.height + "px";
    sigDraw();
  }
  function sigDraw() {
    // strokes are kept as fractions of the pad, so turning the phone sideways keeps the signature
    const ctx = sigCv.getContext("2d"), w = sigCv.width, h = sigCv.height, ratio = window.devicePixelRatio || 1;
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = 3 * ratio; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#111";
    for (const st of sig.strokes) {
      ctx.beginPath(); ctx.moveTo(st[0][0] * w, st[0][1] * h);
      if (st.length === 1) ctx.lineTo(st[0][0] * w + 0.1, st[0][1] * h);
      for (const [x, y] of st.slice(1)) ctx.lineTo(x * w, y * h);
      ctx.stroke();
    }
    $("#sigDone").disabled = !sig.strokes.length;
    $("#sigHint").classList.toggle("hidden", sig.strokes.length > 0);
  }
  const sigPt = (e) => { const r = sigCv.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]; };
  sigCv.addEventListener("pointerdown", (e) => { e.preventDefault(); sigCv.setPointerCapture(e.pointerId); sig.cur = [sigPt(e)]; sig.strokes.push(sig.cur); sigDraw(); });
  sigCv.addEventListener("pointermove", (e) => {
    if (!sig.cur) return; e.preventDefault();
    for (const ev of (e.getCoalescedEvents ? e.getCoalescedEvents() : [e])) sig.cur.push(sigPt(ev));
    sigDraw();
  });
  const sigEnd = () => { sig.cur = null; };
  sigCv.addEventListener("pointerup", sigEnd); sigCv.addEventListener("pointercancel", sigEnd);
  // stop the page behind from scrolling or bouncing while someone signs
  sigBox.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });

  function openSigPad(slot) {
    sig.slot = slot; sig.strokes = []; sig.cur = null; sig.scrollY = window.scrollY;
    const who = form.elements.received_by && form.elements.received_by.value.trim();
    $("#sigWho").textContent = who ? `Signing as ${who}` : "";
    sigBox.classList.remove("hidden");
    document.documentElement.classList.add("sigopen-lock");
    requestAnimationFrame(sigSize);
  }
  function closeSigPad() {
    sigBox.classList.add("hidden");
    document.documentElement.classList.remove("sigopen-lock");
    window.scrollTo(0, sig.scrollY);
    const w = $(`.sigwrap[data-sigslot="${sig.slot}"]`, form); if (w) w.scrollIntoView({ block: "center" });
  }
  window.addEventListener("resize", () => { if (!sigBox.classList.contains("hidden")) sigSize(); });
  $("#sigCancel").onclick = closeSigPad;
  $("#sigClear").onclick = () => { sig.strokes = []; sigDraw(); };
  $("#sigDone").onclick = () => {
    if (!sig.strokes.length) return;
    // crop to the ink, on white, at a fixed wide shape so every signature looks the same in the PDF
    const pw = sigCv.width, ph = sigCv.height;
    let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
    for (const st of sig.strokes) for (const [x, y] of st) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    const bw = Math.max((x1 - x0) * pw, 40), bh = Math.max((y1 - y0) * ph, 20);
    const W = 1200, H = 400, pad = 40, scale = Math.min((W - pad * 2) / bw, (H - pad * 2) / bh, 3);
    const out = document.createElement("canvas"); out.width = W; out.height = H;
    const c = out.getContext("2d");
    c.fillStyle = "#fff"; c.fillRect(0, 0, W, H);
    c.lineWidth = Math.max(4, 3 * (window.devicePixelRatio || 1) * scale * 0.6); c.lineCap = "round"; c.lineJoin = "round"; c.strokeStyle = "#111";
    const ox = (W - bw * scale) / 2, oy = (H - bh * scale) / 2;
    const map = ([x, y]) => [ox + (x - x0) * pw * scale, oy + (y - y0) * ph * scale];
    for (const st of sig.strokes) {
      c.beginPath(); const [sx, sy] = map(st[0]); c.moveTo(sx, sy);
      if (st.length === 1) c.lineTo(sx + 0.1, sy);
      for (const p of st.slice(1)) { const [x, y] = map(p); c.lineTo(x, y); }
      c.stroke();
    }
    const slot = sig.slot;
    out.toBlob(b => { if (b) { photos[slot] = b; renderSignatures(); saveDraftSoon(); } closeSigPad(); }, "image/png");
  };

  // Shrink a phone photo to a reasonable size. Fails loudly instead of hanging.
  function compress(file) {
    const work = (async () => {
      let src;
      try { src = await createImageBitmap(file, { imageOrientation: "from-image" }); }
      catch (e) {
        src = await new Promise((res, rej) => {
          const img = new Image(); const u = URL.createObjectURL(file);
          img.onload = () => { URL.revokeObjectURL(u); res(img); };
          img.onerror = () => { URL.revokeObjectURL(u); rej(new Error("unreadable")); };
          img.src = u;
        });
      }
      const w = src.width, h = src.height;
      if (!w || !h) throw new Error("unreadable");
      const scale = Math.min(1, 1600 / Math.max(w, h));
      const c = document.createElement("canvas");
      c.width = Math.round(w * scale); c.height = Math.round(h * scale);
      c.getContext("2d").drawImage(src, 0, 0, c.width, c.height);
      if (src.close) src.close();
      const blob = await new Promise(res => c.toBlob(res, "image/jpeg", 0.82));
      c.width = c.height = 0;
      if (!blob) throw new Error("unreadable");
      return blob;
    })();
    const timeout = new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 25000));
    return Promise.race([work, timeout]);
  }

  $("#photoPicker").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    const slot = pickSlot;
    if (!file || !slot) return;
    const tile = $(`.tile[data-slot="${slot}"]`);
    if (tile) tile.classList.add("busy");
    const addedAt = new Date();
    const fileAge = file.lastModified ? Math.max(0, Math.round((addedAt.getTime() - file.lastModified) / 1000)) : null;
    const where = locationForPhoto();          // runs while the photo is being shrunk
    try {
      const blob = await compress(file);
      const loc = await where;
      if (previews[slot]) { URL.revokeObjectURL(previews[slot]); delete previews[slot]; }
      photos[slot] = blob;
      photoMeta[slot] = { ...loc, at: addedAt.toISOString(), fileAge };
      renderTiles(); saveDraftSoon();
    } catch (e) {
      if (tile) tile.classList.remove("busy");
      alert("That photo couldn't be read. Please take it again, or pick a different photo.");
    }
  });

  function readFields() {
    const f = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") f[el.name] = el.checked;
      else if (el.type === "radio") { if (el.checked) f[el.name] = el.value; else if (!(el.name in f)) f[el.name] = ""; }
      else f[el.name] = el.value;
    }
    return f;
  }
  function writeFields(vals) {
    for (const el of form.elements) {
      if (!el.name) continue;
      const v = vals[el.name];
      if (el.type === "checkbox") el.checked = !!v;
      else if (el.type === "radio") el.checked = v !== undefined && el.value === v;
      else if (v !== undefined) el.value = v;
    }
    applyConditions();
  }
  function defaults() {
    const d = {};
    spec.fields.forEach(f => {
      if (f.default === "today") d[f.key] = todayISO();
      else if (f.default) d[f.key] = f.default;
      if (f.none_label) d[f.key] = "none";
    });
    return d;
  }

  let draftTimer = null;
  function saveDraftSoon() {
    clearTimeout(draftTimer);
    const slug = spec && spec.slug;
    if (!slug) return;
    draftTimer = setTimeout(async () => {
      try {
        await draftPut(slug, { fields: readFields(), photos: { ...photos }, photoMeta: { ...photoMeta }, startedAt });
        $("#draftNote").textContent = "Saved on this phone";
      } catch (e) { /* storage full or private mode: the form still works */ }
    }, 400);
  }
  form.addEventListener("input", (e) => { clearMark(e.target); applyConditions(); saveDraftSoon(); });
  form.addEventListener("change", (e) => { clearMark(e.target); applyConditions(); saveDraftSoon(); });
  form.addEventListener("click", (e) => {
    const all = e.target.closest("[data-allok]");
    if (!all) return;
    $$(`input[type=radio][value="OK"]`, form).forEach(r => { if (r.name.startsWith(all.dataset.allok + ":")) r.checked = true; });
    $$(".okrow.invalid", form).forEach(r => r.classList.remove("invalid"));
    saveDraftSoon();
  });
  function clearMark(el) {
    el.classList.remove("invalid");
    const wrap = el.closest(".okrow, label.check, .fld"); if (wrap) wrap.classList.remove("invalid");
  }

  async function openForm(slug, prefill) {
    const s = (me.forms || []).find(f => f.slug === slug);
    if (!s) { alert("This form isn't available."); return; }
    spec = s;
    Object.values(previews).forEach(u => URL.revokeObjectURL(u));
    photos = {}; previews = {}; photoMeta = {};
    $("#formTitle").textContent = spec.type;
    $("#formFields").innerHTML = spec.fields.map(fieldHTML).join("") + photosHTML();
    $("#submitBtn").textContent = "Submit " + spec.type;
    $("#formError").classList.add("hidden");
    let draft = prefill || null;
    if (!draft) { try { draft = await draftGet(spec.slug); } catch (e) { draft = null; } }
    if (draft) {
      writeFields({ ...defaults(), ...(draft.fields || {}) });
      photos = { ...(draft.photos || {}) };
      photoMeta = { ...(draft.photoMeta || {}) };
      startedAt = draft.startedAt || new Date().toISOString();
      $("#draftNote").textContent = prefill ? "Fix the problem below, then submit again" : "Picked up where you left off";
    } else {
      writeFields(defaults());
      startedAt = new Date().toISOString();
      $("#draftNote").textContent = "";
    }
    show("viewForm");
    renderTiles();
    setupSignatures();
  }

  $("#clearBtn").addEventListener("click", async () => {
    if (!confirm("Clear everything on this form, including photos?")) return;
    await draftDel(spec.slug).catch(() => {});
    openForm(spec.slug);
  });

  function validate(vals) {
    const problems = [];
    const markDiv = (key) => { const d = $(`.fld[data-key="${CSS.escape(key)}"]`, form); if (d) d.classList.add("invalid"); };
    let anyDefective = false;
    for (const f of spec.fields) {
      const div = $(`.fld[data-key="${CSS.escape(f.key)}"]`, form);
      if (div && div.classList.contains("hidden")) continue;
      const v = vals[f.key];
      if (f.type === "checks") {
        if (f.required) f.items.forEach(([k, t]) => { if (!vals[k]) { const l = form.elements[k].closest("label"); l.classList.add("invalid"); problems.push(t.length > 40 ? t.slice(0, 38) + "…" : t); } });
        continue;
      }
      if (f.type === "okdef") {
        let left = 0;
        Object.entries(f.groups).forEach(([g, items]) => items.forEach(it => {
          const n = okdefName(f.key, g, it), val = vals[n];
          if (val === "Defective") anyDefective = true;
          if (!val) { left++; const row = $(`.okrow[data-row="${CSS.escape(n)}"]`, form); if (row) row.classList.add("invalid"); }
        }));
        if (f.required && left) problems.push(`${left} inspection item${left > 1 ? "s" : ""} not marked`);
        continue;
      }
      const empty = !v || !String(v).trim() || (f.type === "select" && v === "Custom" && !String(vals[f.key + "_custom"] || "").trim());
      if (f.required && empty) { markDiv(f.key); problems.push(f.label); }
      if (f.type === "number" && v && !/^\d+$/.test(String(v).replace(/,/g, ""))) { markDiv(f.key); problems.push(`${f.label} (numbers only)`); }
    }
    spec.fields.filter(f => f.required_if_defective).forEach(f => {
      if (anyDefective && !String(vals[f.key] || "").trim()) { markDiv(f.key); problems.push(`${f.label} (something is Defective)`); }
    });
    spec.photos.filter(g => g.min).forEach(g => {
      const have = g.slots.filter(([s]) => photos[s]).length;
      if (have < g.min) problems.push(`${g.min - have} more photo${g.min - have > 1 ? "s" : ""} in “${g.title}”`);
    });
    return problems;
  }

  const newId = () => (crypto.randomUUID ? crypto.randomUUID() :
    "id-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12));

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#submitBtn");
    if (btn.disabled) return;
    const f = readFields();
    const problems = validate(f);
    const err = $("#formError");
    if (problems.length) {
      err.textContent = "Still needed: " + problems.join(", ") + ".";
      err.classList.remove("hidden");
      const first = $(".invalid", form); if (first) first.scrollIntoView({ block: "center" });
      return;
    }
    err.classList.add("hidden");
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Saving…";
    clearTimeout(draftTimer);
    const keep = new Set(allSlots());
    const ph = Object.fromEntries(Object.entries(photos).filter(([s]) => keep.has(s)));
    const entry = { id: newId(), slug: spec.slug, type: spec.type, userId: me.id, user: me.name, fields: f,
      photos: ph, photoMeta: { ...photoMeta }, startedAt, createdAt: new Date().toISOString(), tries: 0 };
    try {
      await outboxPut(entry);              // safe on the phone before anything else
      await draftDel(spec.slug).catch(() => {});
    } catch (e) {
      btn.disabled = false; btn.textContent = label;
      err.textContent = "This phone couldn't save the report (storage full?). Don't close this page; free up space and try again.";
      err.classList.remove("hidden");
      return;
    }
    btn.textContent = "Sending…";
    const result = await sendEntry(entry);
    btn.disabled = false; btn.textContent = label;
    showResult(result, entry);
    updateBanner();
  });

  function showResult(result, entry) {
    const box = $("#resultBox");
    $("#againBtn").textContent = "Start another " + entry.type;
    if (result.ok) {
      box.className = "result";
      box.innerHTML = `<div class="big">Received ✓</div><div class="muted">Your receipt number</div>
        <div class="receipt">${esc(result.receipt)}</div><p class="muted small">The office will be emailed a copy.</p>`;
    } else if (result.fix) {
      // The server rejected something; put it straight back in the form. Save it as the draft first, then take it out of the queue.
      const back = { fields: entry.fields, photos: entry.photos, photoMeta: entry.photoMeta, startedAt: entry.startedAt };
      draftPut(entry.slug, back).then(() => outboxDel(entry.id)).then(updateBanner).catch(() => {});
      openForm(entry.slug, back).then(() => { $("#formError").textContent = result.message; $("#formError").classList.remove("hidden"); });
      return;
    } else {
      box.className = "result wait";
      box.innerHTML = `<div class="big">Saved on this phone</div>
        <p>${result.login ? "Your sign-in expired. Sign in again and it will send." :
          "No connection right now. It will send automatically when you have signal."}</p>
        <p class="muted small">If you close the app, open it again later so it can send.</p>`;
    }
    show("viewDone");
  }

  // ------------------------------------------------------------ sending + outbox
  async function sendEntry(entry) {
    const fd = new FormData();
    Object.entries(entry.fields).forEach(([k, v]) => fd.append(k, typeof v === "boolean" ? (v ? "1" : "0") : v));
    Object.entries(entry.photos || {}).forEach(([slot, blob]) => {
      fd.append(slot, blob, slot + (blob.type === "image/png" ? ".png" : ".jpg"));
      fd.append("geo_" + slot, JSON.stringify((entry.photoMeta || {})[slot] || { status: "missing" }));
    });
    fd.append("submission_id", entry.id);
    fd.append("started_at", entry.startedAt || "");
    fd.append("queued", entry.tries > 0 ? "1" : "0");
    try {
      const r = await api(`api/reports/${entry.slug}`, { method: "POST", body: fd });
      await outboxDel(entry.id);
      return { ok: true, receipt: r.receipt };
    } catch (e) {
      entry.tries += 1;
      entry.lastError = e.message;
      if (e.status === 422 || e.status === 400 || e.status === 409 || e.status === 403) {
        entry.error = e.message;         // kept on the phone, flagged "needs fixing"
        await outboxPut(entry).catch(() => {});
        return { ok: false, fix: true, message: e.message };
      }
      if (e.status >= 500) {
        entry.serverFails = (entry.serverFails || 0) + 1;
        if (entry.serverFails >= 3) entry.error = "The server couldn't accept this report after 3 tries. Check the photos and send it again.";
      }
      await outboxPut(entry).catch(() => {});
      return { ok: false, login: e.status === 401, offline: e.status === 0 };
    }
  }

  let needLogin = false;
  async function flushOutbox() {
    if (flushing || !me) return;
    flushing = true;
    try {
      const all = await outboxAll();
      for (const e of all) {
        if (e.userId !== me.id || e.error) continue;   // never send someone else's report under this login
        const r = await sendEntry(e);
        if (r.login) { needLogin = true; break; }
        needLogin = false;
        if (r.offline) break;                          // no signal; try again later
        // a server error on one report doesn't hold up the others
      }
    } finally {
      flushing = false;
      updateBanner();
      if (!$("#viewHome").classList.contains("hidden")) showHome();
    }
  }

  async function openFix(id) {
    const e = (await outboxAll()).find(x => x.id === id);
    if (!e) return;
    const existing = await draftGet(e.slug).catch(() => null);
    if (existing && !confirm(`You have an unfinished ${e.type} open. Replace it with the one that needs fixing?`)) return;
    const back = { fields: e.fields, photos: e.photos, photoMeta: e.photoMeta, startedAt: e.startedAt };
    await draftPut(e.slug, back);
    await outboxDel(e.id);
    await openForm(e.slug, back);
    $("#formError").textContent = "Fix this, then submit again: " + e.error;
    $("#formError").classList.remove("hidden");
    updateBanner();
  }

  async function updateBanner() {
    const b = $("#outboxBanner");
    let all = [];
    try { all = await outboxAll(); } catch (e) { /* no storage */ }
    const mine = all.filter(e => me && e.userId === me.id);
    const waiting = mine.filter(e => !e.error), broken = mine.filter(e => e.error);
    const others = all.filter(e => !me || e.userId !== me.id);
    const parts = [];
    if (waiting.length) parts.push(`${waiting.length} report${waiting.length > 1 ? "s" : ""} saved on this phone, waiting to send. ` +
      (needLogin ? `<button type="button" class="link" id="reLogin">Sign in again to send</button>` :
                   `<button type="button" class="link" id="sendNow">Send now</button>`));
    broken.forEach(e => parts.push(`A saved report needs fixing (${esc(e.error)}). <button type="button" class="link" data-fix="${esc(e.id)}">Open it</button>`));
    if (others.length) {
      const names = [...new Set(others.map(e => e.user))].join(", ");
      parts.push(`${others.length} saved report${others.length > 1 ? "s" : ""} from ${esc(names)} will send when they sign in on this phone.`);
    }
    b.innerHTML = parts.join("<br>");
    b.className = "banner wait" + (parts.length ? "" : " hidden");
    const sn = $("#sendNow"); if (sn) sn.onclick = () => flushOutbox();
    const rl = $("#reLogin"); if (rl) rl.onclick = () => { needLogin = false; showLogin(); };
    $$("[data-fix]", b).forEach(btn => { btn.onclick = () => openFix(btn.dataset.fix); });
  }

  window.addEventListener("online", flushOutbox);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) flushOutbox(); });
  setInterval(flushOutbox, 30000);

  // ------------------------------------------------------------ start
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
  const linkCode = readLinkCode();
  (linkCode ? openSetup(linkCode) : loadMe()).then(updateBanner);
})();

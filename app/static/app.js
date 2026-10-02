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
  let lastSlug = null;      // the form most recently opened ("Start another")

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
    if (open) {
      const slug = open.dataset.open === "again" ? lastSlug : open.dataset.open;
      slug === "measure" ? openMeasures() : openForm(slug);
    }
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

  // one photo tile; photos[slot] is a Blob (new) or {keep: id} (carried over from an earlier version of a measure)
  function makeTile(slot, label) {
    const t = document.createElement("div");
    const v = photos[slot];
    t.className = "tile" + (v ? " filled" : "");
    t.dataset.slot = slot;
    t.setAttribute("role", "button"); t.tabIndex = 0;
    t.setAttribute("aria-label", v ? `${label} added` : `Add ${label}`);
    if (v) {
      let badge = "", src;
      if (v instanceof Blob) {
        if (!previews[slot]) previews[slot] = URL.createObjectURL(v);
        src = previews[slot];
        const m = photoMeta[slot] || {};
        badge = m.marked ? `<span class="geo">✎ Marked up</span>` : m.status === "ok" ? (m.acc > 200 ? `<span class="geo warn">📍 ±${m.acc} m</span>` : `<span class="geo">📍 Located</span>`)
          : m.status ? `<span class="geo warn">No location</span>` : "";
      } else {
        src = `api/measure-photos/${encodeURIComponent(v.keep)}`;
        badge = `<span class="geo">Earlier photo</span>`;
      }
      t.innerHTML = `<img src="${src}" alt="">${badge}<button type="button" class="remove" aria-label="Remove photo">×</button>`;
      t.querySelector(".remove").onclick = (e) => { e.stopPropagation(); removePhoto(slot); };
    } else {
      t.innerHTML = `<span><span class="plus">+</span>${esc(label)}</span>`;
    }
    t.onclick = () => { if (photos[slot]) openPhotoSheet(slot, label); else pickPhoto(slot); };
    t.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); t.click(); } };
    return t;
  }
  function pickPhoto(slot) { pickSlot = slot; $("#photoPicker").value = ""; $("#photoPicker").click(); }

  // ---- tapping a photo: look at it, mark it up, replace or remove it
  let sheetSlot = null;
  function openPhotoSheet(slot, label) {
    sheetSlot = slot;
    const v = photos[slot], isNew = v instanceof Blob;
    $("#phTitle").textContent = label;
    $("#phImg").src = isNew ? (previews[slot] || (previews[slot] = URL.createObjectURL(v))) : `api/measure-photos/${encodeURIComponent(v.keep)}`;
    $("#phMark").classList.toggle("hidden", !isNew);
    $("#phOld").classList.toggle("hidden", isNew);
    $("#phSheet").classList.remove("hidden");
    document.documentElement.classList.add("sigopen-lock");
  }
  function closePhotoSheet() { $("#phSheet").classList.add("hidden"); document.documentElement.classList.remove("sigopen-lock"); }
  $("#phClose").onclick = closePhotoSheet;
  $("#phReplace").onclick = () => { const s = sheetSlot; closePhotoSheet(); pickPhoto(s); };
  $("#phRemove").onclick = () => { const s = sheetSlot; if (!confirm("Remove this photo?")) return; closePhotoSheet(); removePhoto(s); };
  $("#phMark").onclick = () => { const s = sheetSlot; closePhotoSheet(); openMarkup(s); };

  // ---- mark-up: draw or write on a photo; the marks become part of the photo that's sent
  const mk = { slot: null, img: null, items: [], cur: null, color: "#ff3b30", mode: "pen", moved: false, down: null };
  const mkCv = $("#mkCanvas"), mkBox = $("#mkOverlay");
  function mkRect() {
    const W = mkCv.clientWidth, H = mkCv.clientHeight, iw = mk.img.naturalWidth, ih = mk.img.naturalHeight;
    const sc = Math.min(W / iw, H / ih), w = iw * sc, h = ih * sc;
    return { x: (W - w) / 2, y: (H - h) / 2, w, h };
  }
  function mkPaint(ctx, x, y, w, h) {
    ctx.drawImage(mk.img, x, y, w, h);
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    for (const it of mk.items) {
      if (it.t === "pen") {
        ctx.strokeStyle = it.c; ctx.lineWidth = it.size * w;
        ctx.beginPath(); ctx.moveTo(x + it.pts[0][0] * w, y + it.pts[0][1] * h);
        if (it.pts.length === 1) ctx.lineTo(x + it.pts[0][0] * w + 0.1, y + it.pts[0][1] * h);
        for (const [px, py] of it.pts.slice(1)) ctx.lineTo(x + px * w, y + py * h);
        ctx.stroke();
      } else {
        const fs = it.size * w;
        ctx.font = `bold ${fs}px -apple-system, Segoe UI, Roboto, Arial, sans-serif`;
        ctx.textBaseline = "middle"; ctx.lineWidth = fs * 0.16; ctx.strokeStyle = it.c === "#111111" ? "#ffffff" : "#000000";
        ctx.strokeText(it.text, x + it.x * w, y + it.y * h); ctx.fillStyle = it.c; ctx.fillText(it.text, x + it.x * w, y + it.y * h);
      }
    }
  }
  function mkSize() {
    const r = mkCv.parentElement.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
    mkCv.width = Math.round(r.width * ratio); mkCv.height = Math.round(r.height * ratio);
    mkCv.style.width = r.width + "px"; mkCv.style.height = r.height + "px";
    mkDraw();
  }
  function mkDraw() {
    if (!mk.img) return;
    const ctx = mkCv.getContext("2d"), ratio = window.devicePixelRatio || 1;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, mkCv.width, mkCv.height);
    const r = mkRect(); mkPaint(ctx, r.x, r.y, r.w, r.h);
    $("#mkUndo").disabled = !mk.items.length;
  }
  const mkPt = (e) => { const b = mkCv.getBoundingClientRect(), r = mkRect(); return [(e.clientX - b.left - r.x) / r.w, (e.clientY - b.top - r.y) / r.h]; };
  const inImg = ([x, y]) => x >= 0 && x <= 1 && y >= 0 && y <= 1;
  mkCv.addEventListener("pointerdown", (e) => {
    e.preventDefault(); const p = mkPt(e);
    if (!inImg(p)) return;
    mkCv.setPointerCapture(e.pointerId); mk.down = p; mk.moved = false;
    if (mk.mode === "pen") { mk.cur = { t: "pen", c: mk.color, size: 0.008, pts: [p] }; mk.items.push(mk.cur); mkDraw(); }
  });
  mkCv.addEventListener("pointermove", (e) => {
    if (!mk.cur) return; e.preventDefault();
    for (const ev of (e.getCoalescedEvents ? e.getCoalescedEvents() : [e])) mk.cur.pts.push(mkPt(ev).map(v => Math.min(1, Math.max(0, v))));
    mk.moved = true; mkDraw();
  });
  mkCv.addEventListener("pointerup", () => {
    const p = mk.down; mk.cur = null; mk.down = null;
    if (mk.mode === "text" && p) {
      const text = (prompt("Text to write on the photo (e.g. 36 1/2\")") || "").trim().slice(0, 60);
      if (text) { mk.items.push({ t: "text", c: mk.color, size: 0.06, x: p[0], y: p[1], text }); mkDraw(); }
    }
  });
  mkCv.addEventListener("pointercancel", () => { mk.cur = null; mk.down = null; });
  mkBox.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });
  $$("[data-mkcolor]").forEach(b => b.onclick = () => {
    mk.color = b.dataset.mkcolor; $$("[data-mkcolor]").forEach(x => x.classList.toggle("on", x === b));
  });
  $$("[data-mkmode]").forEach(b => b.onclick = () => {
    mk.mode = b.dataset.mkmode; $$("[data-mkmode]").forEach(x => x.classList.toggle("on", x === b));
    $("#mkHint").textContent = mk.mode === "pen" ? "Draw with your finger" : "Tap where the text should go";
  });
  $("#mkUndo").onclick = () => { mk.items.pop(); mkDraw(); };
  function openMarkup(slot) {
    const blob = photos[slot];
    if (!(blob instanceof Blob)) return;
    mk.slot = slot; mk.items = []; mk.cur = null;
    const img = new Image();
    img.onload = () => { mk.img = img; mkBox.classList.remove("hidden"); document.documentElement.classList.add("sigopen-lock"); requestAnimationFrame(mkSize); };
    img.src = previews[slot] || (previews[slot] = URL.createObjectURL(blob));
  }
  function closeMarkup() { mkBox.classList.add("hidden"); document.documentElement.classList.remove("sigopen-lock"); mk.img = null; }
  $("#mkCancel").onclick = () => { if (mk.items.length && !confirm("Throw away these marks?")) return; closeMarkup(); };
  $("#mkDone").onclick = () => {
    if (!mk.items.length) return closeMarkup();
    const iw = mk.img.naturalWidth, ih = mk.img.naturalHeight, c = document.createElement("canvas");
    c.width = iw; c.height = ih;
    mkPaint(c.getContext("2d"), 0, 0, iw, ih);
    const slot = mk.slot;
    c.toBlob(b => {
      if (b) {
        if (previews[slot]) { URL.revokeObjectURL(previews[slot]); delete previews[slot]; }
        photos[slot] = b;
        photoMeta[slot] = { ...(photoMeta[slot] || {}), marked: true };
        renderTiles(); saveDraftSoon();
      }
      closeMarkup();
    }, "image/jpeg", 0.88);
  };
  window.addEventListener("resize", () => { if (!mkBox.classList.contains("hidden")) mkSize(); });

  function renderTiles() {
    if (spec && spec.kind === "measure") return mRenderTiles();
    spec.photos.filter(g => !g.signature).forEach(g => {
      const box = $(`.photos[data-group="${g.group}"]`, form);
      box.innerHTML = "";
      g.slots.forEach(([slot, label]) => box.appendChild(makeTile(slot, label)));
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
    if (spec && spec.kind === "measure") return mSaveDraftSoon();
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
    spec = s; lastSlug = slug;
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
    $("#againBtn").textContent = entry.slug === "measure" ? "Back to Measures" : "Start another " + entry.type;
    if (result.ok) {
      box.className = "result";
      box.innerHTML = `<div class="big">Received ✓</div><div class="muted">Your receipt number</div>
        <div class="receipt">${esc(result.receipt)}</div><p class="muted small">The office will be emailed a copy.</p>`;
    } else if (result.fix) {
      // The server rejected something; put it straight back in the form. Save it as the draft first, then take it out of the queue.
      const back = entry.slug === "measure" ? entryToMeasure(entry)
        : { fields: entry.fields, photos: entry.photos, photoMeta: entry.photoMeta, startedAt: entry.startedAt };
      draftPut(entry.slug, back).then(() => outboxDel(entry.id)).then(updateBanner).catch(() => {});
      openAny(entry.slug, back, result.message);
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

  // ------------------------------------------------------------ MEASURE: one job, any number of door / window cards
  const mForm = $("#mForm");
  let MS = null;                          // the measure spec from the server
  let mJob = null;                        // {revision_of, measured_by}
  let mDraftTimer = null;
  const TYPE = { door: "Door", window: "Window" };
  const cardId = () => "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const mFields = (t) => MS.measure[t];
  const blankSize = () => ({ w: "", f: "" });
  const blankPoint = () => ({ w: blankSize(), h: blankSize() });
  const fmtSize = (s) => s && s.w ? `${s.w}${s.f ? " " + s.f : ""}"` : "";

  function blankCard(type) {
    const c = { id: cardId(), type, open: true };
    mFields(type).forEach(f => {
      c[f.key] = f.type === "size" ? blankSize() : f.type === "points" ? [blankPoint()] : f.type === "labor" ? []
        : f.type === "toggle" ? false : (f.default || "");
    });
    return c;
  }

  function sizeCell(v, label, attrs, req) {
    v = v || blankSize();
    return `<div class="sz"><span class="szl">${esc(label)}${req ? " *" : ""}</span>
      <div class="szrow"><input type="text" inputmode="decimal" ${attrs} data-part="w" value="${esc(v.w)}" maxlength="8" placeholder="inches" aria-label="${esc(label)} inches">
        <span class="szval" aria-live="polite">${esc(fmtSize(v))}</span></div>
      <input type="hidden" ${attrs} data-part="f" value="${esc(v.f || "")}">
      <div class="fracs" role="group" aria-label="${esc(label)} fraction">${MS.measure.fractions.map(x =>
        `<button type="button" class="frac${x === (v.f || "") ? " on" : ""}" data-frac="${x}" aria-pressed="${x === (v.f || "")}">${x || "0"}</button>`).join("")}</div></div>`;
  }
  function pointRow(p, i) {
    return `<div class="mpt"><div class="mpthead"><b>Size ${i + 1}</b><button type="button" class="link danger" data-delpt>Remove</button></div>
      ${sizeCell(p.w, "Width", 'data-pt="w"', true)}${sizeCell(p.h, "Height", 'data-pt="h"', true)}</div>`;
  }
  // door handing, drawn from above. Standing OUTSIDE facing the door: hinges on your left = Left Hand.
  function handSVG(opt) {
    const left = /^Left/.test(opt), inswing = /Inswing/.test(opt);
    const hx = left ? 27 : 73, fx = left ? 73 : 27, ty = inswing ? 14 : 106;
    const sweep = (left === inswing) ? 0 : 1;
    return `<svg viewBox="0 0 100 120" aria-hidden="true"><text x="50" y="9" class="hs-t">INSIDE</text><text x="50" y="118" class="hs-t">OUTSIDE</text>
      <line x1="2" y1="60" x2="27" y2="60" class="hs-wall"/><line x1="73" y1="60" x2="98" y2="60" class="hs-wall"/>
      <path d="M ${fx} 60 A 46 46 0 0 ${sweep} ${hx} ${ty}" class="hs-arc"/><line x1="${hx}" y1="60" x2="${hx}" y2="${ty}" class="hs-leaf"/>
      <circle cx="${hx}" cy="60" r="3.5" class="hs-hinge"/></svg>`;
  }
  function slideSVG() {
    return `<svg viewBox="0 0 100 120" aria-hidden="true"><text x="50" y="9" class="hs-t">INSIDE</text><text x="50" y="118" class="hs-t">OUTSIDE</text>
      <line x1="2" y1="60" x2="22" y2="60" class="hs-wall"/><line x1="78" y1="60" x2="98" y2="60" class="hs-wall"/>
      <line x1="22" y1="56" x2="54" y2="56" class="hs-leaf"/><line x1="46" y1="64" x2="78" y2="64" class="hs-leaf"/>
      <path d="M 30 44 L 50 44 M 44 38 L 50 44 L 44 50" class="hs-arc solid"/></svg>`;
  }
  function handingHTML(f, c) {
    const v = c[f.key] || "", custom = (v && !f.options.includes(v)) || c[f.key + "__custom"];
    const short = (o) => o.replace("Left Hand ", "LH ").replace("Right Hand ", "RH ");
    return `<div class="mf wide"><span class="szl">${esc(f.label)}</span>
      <p class="muted small hhelp">Stand <b>outside</b>, facing the door. Hinges on your left = Left Hand. Opens away from you = Inswing.</p>
      <input type="hidden" data-k="${esc(f.key)}" value="${esc(custom ? "Custom" : v)}">
      <div class="hand" role="radiogroup" aria-label="${esc(f.label)}">${f.options.map(o =>
        `<button type="button" class="handopt${!custom && o === v ? " on" : ""}" role="radio" aria-checked="${!custom && o === v}" data-hand="${esc(o)}">
          ${o === "Slider" ? slideSVG() : handSVG(o)}<span>${esc(short(o))}</span></button>`).join("")}
        <button type="button" class="handopt other-opt${custom ? " on" : ""}" role="radio" aria-checked="${!!custom}" data-hand="Custom"><span class="big">✎</span><span>Other</span></button></div>
      <input type="text" class="other${custom ? "" : " hidden"}" data-kc="${esc(f.key)}" maxlength="120" placeholder="Type it" value="${esc(custom ? v : "")}"></div>`;
  }

  // ---- sanity checks: warnings only, never block (the measurer may be right)
  const inches = (s) => s && s.w ? parseFloat(s.w) + (s.f ? (([a, b]) => a / b)(s.f.split("/").map(Number)) : 0) : 0;
  function cardWarnings(c) {
    const out = [], typo = " Typo? For 36 1/2 type 36 and tap 1/2.";
    const both = (s, what) => { if (s && s.w && s.w.includes(".") && s.f) out.push(`${what} has both a decimal and a fraction (${fmtSize(s)}).`); };
    if (c.type === "door") {
      const w = inches(c.w), h = inches(c.h);
      if (w > 100) out.push(`Width ${fmtSize(c.w)} is over 8 ft.${typo}`);
      else if (w && w < 18) out.push(`Width ${fmtSize(c.w)} is under 18".`);
      if (h > 120) out.push(`Height ${fmtSize(c.h)} is over 10 ft.${typo}`);
      else if (h && h < 66) out.push(`Height ${fmtSize(c.h)} is under 5'6".`);
      if (w && h && w > h && w <= 100) out.push(`Width is bigger than height. Swapped?`);
      if (c.config === "Single" && w > 44 && w <= 100) out.push(`${fmtSize(c.w)} is wide for a single door. Double or sidelights?`);
      if (c.config === "Double" && w && w < 48) out.push(`${fmtSize(c.w)} is narrow for a double door.`);
      both(c.w, "Width"); both(c.h, "Height");
    } else {
      (c.points || []).forEach((p, i) => {
        const w = inches(p.w), h = inches(p.h), n = (c.points.length > 1 ? `Size ${i + 1} ` : "");
        if (w > 144 || h > 144) out.push(`${n}${fmtSize(p.w)} × ${fmtSize(p.h)} is over 12 ft.${typo}`);
        if ((w && w < 8) || (h && h < 8)) out.push(`${n}${fmtSize(p.w)} × ${fmtSize(p.h)} has a side under 8".`);
        both(p.w, `${n}width`); both(p.h, `${n}height`);
      });
      if (+c.qty > 20) out.push(`Qty ${c.qty}. Is that right?`);
      if (inches(c.sill) > 96) out.push(`Floor to sill ${fmtSize(c.sill)} is over 8 ft.`);
    }
    return out;
  }
  function showWarnings(card, c) {
    const list = cardWarnings(c || readCard(card));
    const box = $(".mwarns", card);
    box.innerHTML = list.map(w => `<li>${esc(w)}</li>`).join("");
    box.classList.toggle("hidden", !list.length);
    $(".mwarnflag", card).classList.toggle("hidden", !list.length);
    return list;
  }

  function mFieldHTML(f, c) {
    const v = c[f.key], k = esc(f.key), lab = esc(f.label);
    switch (f.type) {
      case "text": return `<div class="mf wide"><label>${lab}</label><input type="text" data-k="${k}" maxlength="200" value="${esc(v)}"${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ""}></div>`;
      case "textarea": return `<div class="mf wide"><label>${lab}</label><textarea data-k="${k}" rows="2" maxlength="2000">${esc(v)}</textarea></div>`;
      case "qty": return `<div class="mf"><label>${lab}</label><input type="text" inputmode="numeric" data-k="${k}" maxlength="2" value="${esc(v)}"></div>`;
      case "toggle": return `<div class="mf"><label class="tog"><input type="checkbox" data-k="${k}"${v ? " checked" : ""}><span>${lab}</span></label></div>`;
      case "size": return `<div class="mf wide">${sizeCell(v, f.label, `data-sz="${k}"`, f.required)}</div>`;
      case "labor": return `<div class="mf wide"><span class="szl">${lab}</span><div class="mlabor">${f.options.map(o =>
        `<label class="check"><input type="checkbox" data-labor value="${esc(o)}"${(v || []).includes(o) ? " checked" : ""}><span>${esc(o)}</span></label>`).join("")}</div></div>`;
      case "points": return `<div class="mf wide"><span class="szl">${lab} *</span><div class="mpts">${(v && v.length ? v : [blankPoint()]).map(pointRow).join("")}</div>
        <button type="button" class="link" data-addpt>+ Add another size</button></div>`;
      case "select": {
        if (f.picker === "handing") return handingHTML(f, c);
        const custom = f.custom && v && !f.options.includes(v) || c[f.key + "__custom"];
        const opts = (f.default ? "" : `<option value="">Pick…</option>`) + f.options.map(o =>
          `<option value="${esc(o)}"${!custom && o === v ? " selected" : ""}>${esc(o)}</option>`).join("") +
          (f.custom ? `<option value="Custom"${custom ? " selected" : ""}>Custom (type it)</option>` : "");
        return `<div class="mf"><label>${lab}</label><select data-k="${k}">${opts}</select>` +
          (f.custom ? `<input type="text" class="other${custom ? "" : " hidden"}" data-kc="${k}" maxlength="120" placeholder="Type it" value="${esc(custom ? v : "")}">` : "") + `</div>`;
      }
    }
    return "";
  }
  function cardSummary(c) {
    if (c.type === "door") return [fmtSize(c.w) && fmtSize(c.h) ? `${fmtSize(c.w)} × ${fmtSize(c.h)}` : "", c.config, c.handing].filter(Boolean).join(" · ");
    const pts = (c.points || []).filter(p => p.w.w || p.h.w).map(p => `${fmtSize(p.w)} × ${fmtSize(p.h)}`);
    return [c.qty && c.qty !== "1" ? `Qty ${c.qty}` : "", pts.join(", "), c.tempered ? "TEMPERED" : ""].filter(Boolean).join(" · ");
  }
  function cardHTML(c, n) {
    const name = `${TYPE[c.type]} #${n}`;
    return `<section class="mcard${c.open === false ? " closed" : ""}" data-id="${esc(c.id)}" data-type="${esc(c.type)}">
      <div class="mhead"><button type="button" class="mtoggle" aria-expanded="${c.open !== false}">
        <span class="mname">${name}</span><span class="mloc">${esc(c.loc || "")}</span><span class="mwarnflag hidden" title="Something looks unusual">⚠</span>
        <span class="msum">${esc(cardSummary(c))}</span></button>
        <button type="button" class="mmove" data-move="-1" aria-label="Move ${name} up">▲</button>
        <button type="button" class="mmove" data-move="1" aria-label="Move ${name} down">▼</button></div>
      <div class="mbody"><ul class="mwarns hidden" role="status"></ul><div class="mgrid">${mFields(c.type).map(f => mFieldHTML(f, c)).join("")}</div>
        <span class="szl">Photos</span><div class="photos mphotos" data-mphotos="${esc(c.id)}"></div>
        <div class="mfoot"><button type="button" class="link" data-dup>Duplicate this ${TYPE[c.type].toLowerCase()}</button>
          <button type="button" class="link danger mdel">Remove</button></div></div></section>`;
  }

  function readSize(root, attr) {
    return { w: ($(`[${attr}][data-part="w"]`, root) || {}).value?.trim() || "", f: ($(`[${attr}][data-part="f"]`, root) || {}).value || "" };
  }
  function readCard(el) {
    const c = { id: el.dataset.id, type: el.dataset.type, open: !el.classList.contains("closed") };
    mFields(c.type).forEach(f => {
      const k = f.key;
      if (f.type === "size") c[k] = readSize(el, `data-sz="${k}"`);
      else if (f.type === "points") c[k] = $$(".mpt", el).map(r => ({ w: readSize(r, 'data-pt="w"'), h: readSize(r, 'data-pt="h"') }));
      else if (f.type === "labor") c[k] = $$("[data-labor]", el).filter(x => x.checked).map(x => x.value);
      else if (f.type === "toggle") c[k] = $(`[data-k="${k}"]`, el).checked;
      else if (f.type === "select" && f.custom) {
        const sel = $(`[data-k="${k}"]`, el);
        c[k + "__custom"] = sel.value === "Custom";
        c[k] = sel.value === "Custom" ? $(`[data-kc="${k}"]`, el).value.trim() : sel.value;
      } else c[k] = $(`[data-k="${k}"]`, el).value;
    });
    return c;
  }
  const readCards = () => $$(".mcard", mForm).map(readCard);
  const readJob = () => ({ customer: $("#m_customer").value.trim(), po: $("#m_po").value.trim(), date: $("#m_date").value });

  function renderCards(cards) {
    const counts = { door: 0, window: 0 };
    $("#mCards").innerHTML = cards.map(c => cardHTML(c, ++counts[c.type])).join("");
    $("#mEmpty").classList.toggle("hidden", cards.length > 0);
    $("#mCopy").disabled = !cards.length;
    $$(".mcard", mForm).forEach((el, i) => {
      showWarnings(el, cards[i]);
      $('[data-move="-1"]', el).disabled = i === 0;
      $('[data-move="1"]', el).disabled = i === cards.length - 1;
    });
    mRenderTiles();
  }
  function mRenderTiles() {
    $$(".mcard", mForm).forEach(el => {
      const box = $(`[data-mphotos]`, el); box.innerHTML = "";
      const labels = MS.measure.photo_labels[el.dataset.type];
      for (let k = 1; k <= MS.measure.photos_per_item; k++) box.appendChild(makeTile(`${el.dataset.id}:${k}`, labels[k - 1]));
    });
  }
  function mState() {
    return { job: readJob(), cards: readCards(), photos: { ...photos }, photoMeta: { ...photoMeta }, startedAt,
      revision_of: mJob.revision_of || "", measured_by: mJob.measured_by || "" };
  }
  function mSaveDraftSoon(now) {
    clearTimeout(mDraftTimer);
    const run = async () => {
      try { await draftPut("measure", mState()); $("#mDraftNote").textContent = "Saved on this phone"; } catch (e) { /* storage full */ }
    };
    if (now) return run();
    mDraftTimer = setTimeout(run, 500);
  }

  async function openMeasures() {
    MS = (me.forms || []).find(f => f.slug === "measure");
    if (!MS) { alert("Measure isn't available."); return; }
    spec = null; lastSlug = "measure";
    show("viewMeasures");
    const draft = await draftGet("measure").catch(() => null);
    const box = $("#mDraftBox");
    if (draft && draft.cards) {
      const n = draft.cards.length;
      box.innerHTML = `<button class="card" type="button" id="mContinue"><span class="card-title">Continue: ${esc(draft.job.customer || "unnamed job")}</span>
        <span class="card-sub">${n} item${n === 1 ? "" : "s"}${draft.revision_of ? ` · revising ${esc(draft.revision_of)}` : ""} · not sent yet</span></button>`;
      $("#mContinue").onclick = () => openMeasureEditor(draft);
    } else box.innerHTML = "";
    const list = $("#mList");
    list.innerHTML = `<li class="muted">Loading…</li>`;
    try {
      const rows = await api("api/measures");
      mListRows = rows;
      $("#mSearch").classList.toggle("hidden", rows.length < 8);
      drawMeasureList();
    } catch (e) {
      list.innerHTML = `<li class="muted">${e.status === 0 ? "No signal, so past measures can't load right now. You can still start a new one." : esc(e.message)}</li>`;
    }
  }
  let mListRows = [];
  function drawMeasureList() {
    const q = $("#mSearch").value.trim().toLowerCase();
    const rows = mListRows.filter(r => !q || [r.customer, r.po, r.receipt, r.measured_by].join(" ").toLowerCase().includes(q));
    $("#mList").innerHTML = rows.map(r => {
      const parts = [r.date || fmtTime(r.submitted_at), [r.doors ? `${r.doors} door${r.doors > 1 ? "s" : ""}` : "", r.windows ? `${r.windows} window${r.windows > 1 ? "s" : ""}` : ""].filter(Boolean).join(", "), r.receipt];
      if (me.is_admin || r.measured_by !== me.name) parts.push(r.measured_by);
      return `<li class="mitem${r.replaced_by ? " old" : ""}"><div class="mtext"><b>${esc(r.customer)}</b>${r.po ? ` <span class="muted">· PO ${esc(r.po)}</span>` : ""}
        <div class="muted small">${parts.filter(Boolean).map(esc).join(" · ")}${r.revision_of ? ` · revises ${esc(r.revision_of)}` : ""}</div>
        ${r.replaced_by ? `<div class="small">Replaced by ${esc(r.replaced_by)}</div>` : ""}</div>
        ${r.replaced_by ? "" : `<button type="button" class="mini" data-reopen="${r.id}">Reopen</button>`}</li>`;
    }).join("") || `<li class="muted">${q ? "Nothing matches." : "No measures yet."}</li>`;
    $$("[data-reopen]", $("#mList")).forEach(b => b.onclick = () => reopenMeasure(b.dataset.reopen, b));
  }
  $("#mSearch").addEventListener("input", drawMeasureList);

  async function confirmReplaceDraft() {
    const d = await draftGet("measure").catch(() => null);
    return !d || !d.cards || confirm(`Throw away the unfinished measure for ${d.job.customer || "an unnamed job"}? It hasn't been sent.`);
  }
  $("#mNew").addEventListener("click", async () => {
    if (!(await confirmReplaceDraft())) return;
    await draftDel("measure").catch(() => {});
    openMeasureEditor(null);
  });
  async function reopenMeasure(id, btn) {
    if (!(await confirmReplaceDraft())) return;
    btn.disabled = true; btn.textContent = "Opening…";
    try {
      const m = await api(`api/measures/${encodeURIComponent(id)}`);
      const cards = (m.data.items || []).map(it => ({ ...it, id: cardId(), open: false }));
      const ph = {};
      m.photos.forEach(p => {
        const x = /^i(\d+)p(\d+)$/.exec(p.slot);
        if (x && cards[x[1] - 1]) ph[`${cards[x[1] - 1].id}:${x[2]}`] = { keep: p.id };
      });
      await openMeasureEditor({ job: { customer: m.data.customer, po: m.data.po, date: m.data.date }, cards, photos: ph, photoMeta: {},
        startedAt: new Date().toISOString(), revision_of: m.receipt, measured_by: m.data.measured_by, fresh: true });
      mSaveDraftSoon(true);
    } catch (e) {
      btn.disabled = false; btn.textContent = "Reopen";
      alert(e.status === 0 ? "No signal. Try again when you have a connection." : e.message);
    }
  }

  async function openMeasureEditor(state) {
    MS = (me.forms || []).find(f => f.slug === "measure");
    spec = MS; lastSlug = "measure";
    Object.values(previews).forEach(u => URL.revokeObjectURL(u));
    photos = { ...((state && state.photos) || {}) }; photoMeta = { ...((state && state.photoMeta) || {}) }; previews = {};
    startedAt = (state && state.startedAt) || new Date().toISOString();
    mJob = { revision_of: (state && state.revision_of) || "", measured_by: (state && state.measured_by) || me.name };
    const job = (state && state.job) || { customer: "", po: "", date: todayISO() };
    $("#m_customer").value = job.customer || ""; $("#m_po").value = job.po || ""; $("#m_date").value = job.date || todayISO();
    $("#m_by").value = mJob.measured_by;
    const rn = $("#mRevNote");
    rn.textContent = mJob.revision_of ? `Revising ${mJob.revision_of}. Sending makes a new copy marked REVISED; the old one stays on file.` : "";
    rn.classList.toggle("hidden", !mJob.revision_of);
    $("#mTitle").textContent = mJob.revision_of ? "Revise measure" : "New measure";
    $("#mError").classList.add("hidden");
    $$(".invalid", mForm).forEach(x => x.classList.remove("invalid"));
    $("#mDraftNote").textContent = state && !state.fresh ? "Picked up where you left off" : "";
    if (state) delete state.fresh;
    renderCards((state && state.cards) || []);
    show("viewMeasure");
  }

  // editing
  function updateHead(card) {
    const c = readCard(card);
    $(".mloc", card).textContent = c.loc || "";
    $(".msum", card).textContent = cardSummary(c);
    showWarnings(card, c);
  }
  mForm.addEventListener("input", (e) => {
    e.target.classList.remove("invalid");
    const sz = e.target.closest(".sz");
    if (sz) { const w = $('[data-part="w"]', sz).value.trim(), f = $('[data-part="f"]', sz).value; $(".szval", sz).textContent = fmtSize({ w, f }); }
    const card = e.target.closest(".mcard");
    if (card) { card.classList.remove("invalid"); updateHead(card); }
    mSaveDraftSoon();
  });
  // tapping a number box selects what's there, so typing replaces it (qty "1" doesn't become "12")
  mForm.addEventListener("focusin", (e) => {
    if (e.target.matches('[data-k="qty"], [data-part="w"]')) setTimeout(() => { try { e.target.select(); } catch (x) { /* ok */ } }, 0);
  });
  mForm.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches("select[data-k]")) {
      const other = t.parentElement.querySelector(`[data-kc="${t.dataset.k}"]`);
      if (other) { other.classList.toggle("hidden", t.value !== "Custom"); if (t.value === "Custom") other.focus(); }
    }
    const card = t.closest(".mcard"); if (card) updateHead(card);
    mSaveDraftSoon();
  });
  mForm.addEventListener("click", (e) => {
    const add = e.target.closest("[data-madd]");
    if (add) {
      const cards = readCards().map(c => ({ ...c, open: false }));
      const c = blankCard(add.dataset.madd);
      cards.push(c); renderCards(cards); mSaveDraftSoon();
      const el = $(`.mcard[data-id="${c.id}"]`, mForm); el.scrollIntoView({ block: "start" });
      return;
    }
    if (e.target.closest("#mCopy")) {
      const cards = readCards();
      if (!cards.length) return;
      const last = cards[cards.length - 1];
      const c = { ...JSON.parse(JSON.stringify(last)), id: cardId(), open: true, loc: "", notes: "" };
      if (c.type === "door") { c.w = blankSize(); c.h = blankSize(); } else c.points = [blankPoint()];
      cards.forEach(x => x.open = false);
      cards.push(c); renderCards(cards); mSaveDraftSoon();
      const el = $(`.mcard[data-id="${c.id}"]`, mForm); el.scrollIntoView({ block: "start" });
      const loc = $('[data-k="loc"]', el); if (loc) loc.focus({ preventScroll: true });
      return;
    }
    const card = e.target.closest(".mcard");
    if (!card) return;
    const fr = e.target.closest("[data-frac]");
    if (fr) {
      const sz = fr.closest(".sz"), hid = $('input[type=hidden][data-part="f"]', sz);
      hid.value = fr.dataset.frac;
      $$(".frac", sz).forEach(b => { b.classList.toggle("on", b === fr); b.setAttribute("aria-pressed", String(b === fr)); });
      $(".szval", sz).textContent = fmtSize({ w: $('[data-part="w"]', sz).value.trim(), f: hid.value });
      updateHead(card); mSaveDraftSoon(); return;
    }
    const ho = e.target.closest("[data-hand]");
    if (ho) {
      const wrap = ho.closest(".mf"), hid = $("input[type=hidden][data-k]", wrap), other = $("[data-kc]", wrap);
      hid.value = ho.dataset.hand;
      $$(".handopt", wrap).forEach(b => { b.classList.toggle("on", b === ho); b.setAttribute("aria-checked", String(b === ho)); });
      other.classList.toggle("hidden", ho.dataset.hand !== "Custom");
      if (ho.dataset.hand === "Custom") other.focus();
      updateHead(card); mSaveDraftSoon(); return;
    }
    const mv = e.target.closest("[data-move]");
    if (mv) {
      const cards = readCards(), i = cards.findIndex(c => c.id === card.dataset.id), j = i + Number(mv.dataset.move);
      if (j < 0 || j >= cards.length) return;
      [cards[i], cards[j]] = [cards[j], cards[i]];
      renderCards(cards); mSaveDraftSoon();
      const el = $(`.mcard[data-id="${card.dataset.id}"]`, mForm);
      $(`[data-move="${mv.dataset.move}"]`, el).focus({ preventScroll: true });
      el.scrollIntoView({ block: "nearest" });
      return;
    }
    if (e.target.closest("[data-dup]")) {
      const cards = readCards(), i = cards.findIndex(c => c.id === card.dataset.id);
      const copy = { ...JSON.parse(JSON.stringify(cards[i])), id: cardId(), open: true };
      cards.forEach(x => x.open = false);
      cards.splice(i + 1, 0, copy);
      renderCards(cards); mSaveDraftSoon();
      $(`.mcard[data-id="${copy.id}"]`, mForm).scrollIntoView({ block: "start" });
      return;
    }
    if (e.target.closest(".mtoggle")) {
      const closed = card.classList.toggle("closed");
      $(".mtoggle", card).setAttribute("aria-expanded", String(!closed));
      mSaveDraftSoon(); return;
    }
    if (e.target.closest(".mdel")) {
      const name = $(".mname", card).textContent, loc = $(".mloc", card).textContent;
      if (!confirm(`Remove ${name}${loc ? " (" + loc + ")" : ""}? Its photos go too.`)) return;
      Object.keys(photos).filter(k => k.startsWith(card.dataset.id + ":")).forEach(k => {
        if (previews[k]) URL.revokeObjectURL(previews[k]); delete previews[k]; delete photos[k]; delete photoMeta[k];
      });
      renderCards(readCards().filter(c => c.id !== card.dataset.id)); mSaveDraftSoon(); return;
    }
    if (e.target.closest("[data-addpt]")) {
      const c = readCard(card); c.points.push(blankPoint());
      $(".mpts", card).innerHTML = c.points.map(pointRow).join(""); mSaveDraftSoon(); return;
    }
    if (e.target.closest("[data-delpt]")) {
      const rows = $$(".mpt", card), row = e.target.closest(".mpt");
      if (rows.length === 1) {
        $$("input", row).forEach(i => i.value = ""); $$(".frac", row).forEach(b => b.classList.toggle("on", b.dataset.frac === ""));
        $$(".szval", row).forEach(x => x.textContent = "");
      }
      else row.remove();
      $$(".mpt .mpthead b", card).forEach((n, i) => n.textContent = `Size ${i + 1}`);
      updateHead(card); mSaveDraftSoon();
    }
  });
  $("#mBack").addEventListener("click", async () => { await mSaveDraftSoon(true); openMeasures(); });
  $("#mClear").addEventListener("click", async () => {
    if (!confirm(mJob.revision_of ? "Stop revising? Your changes are thrown away; the sent measure stays as it was."
                                  : "Throw away this whole measure, including photos? It hasn't been sent.")) return;
    clearTimeout(mDraftTimer);
    await draftDel("measure").catch(() => {});
    openMeasures();
  });

  function mValidate(job, cards) {
    const problems = [];
    $$(".invalid", mForm).forEach(x => x.classList.remove("invalid"));
    if (!job.customer) { $("#m_customer").classList.add("invalid"); problems.push("Customer name"); }
    if (!cards.length) problems.push("at least one door or window");
    const counts = { door: 0, window: 0 };
    cards.forEach(c => {
      const name = `${TYPE[c.type]} #${++counts[c.type]}`;
      const el = $(`.mcard[data-id="${c.id}"]`, mForm);
      const bad = (sel) => { $$(sel, el).forEach(x => x.classList.add("invalid")); };
      const num = (v) => !v || /^\d{1,4}(\.\d{1,3})?$/.test(v);
      let miss = [];
      if (c.type === "door") {
        if (!c.w.w || !num(c.w.w)) { miss.push("width"); bad('[data-sz="w"][data-part="w"]'); }
        if (!c.h.w || !num(c.h.w)) { miss.push("height"); bad('[data-sz="h"][data-part="w"]'); }
      } else {
        const filled = c.points.filter(p => p.w.w || p.h.w);
        if (!filled.length) { miss.push("a width × height"); bad('.mpt [data-part="w"]'); }
        c.points.forEach((p, i) => {
          if ((p.w.w || p.h.w) && !(p.w.w && p.h.w && num(p.w.w) && num(p.h.w))) {
            miss.push(`size ${i + 1}`); $$('[data-part="w"]', $$(".mpt", el)[i]).forEach(x => x.classList.add("invalid"));
          }
        });
        if (!/^\d{1,2}$/.test(c.qty) || +c.qty < 1) { miss.push("qty"); bad('[data-k="qty"]'); }
      }
      if (c.sill && c.sill.w && !num(c.sill.w)) { miss.push("sill (numbers only)"); bad('[data-sz="sill"][data-part="w"]'); }
      if (miss.length) {
        el.classList.add("invalid"); el.classList.remove("closed");
        problems.push(`${name}: ${miss.join(", ")}`);
      }
    });
    return problems;
  }

  mForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#mSubmit");
    if (btn.disabled) return;
    const job = readJob(), cards = readCards();
    const err = $("#mError");
    const problems = mValidate(job, cards);
    if (problems.length) {
      err.textContent = "Still needed: " + problems.join("; ") + ".";
      err.classList.remove("hidden");
      const first = $(".invalid", mForm); if (first) first.scrollIntoView({ block: "center" });
      return;
    }
    err.classList.add("hidden");
    const warns = [];
    $$(".mcard", mForm).forEach(el => { const name = $(".mname", el).textContent; showWarnings(el).forEach(w => warns.push(`${name}: ${w}`)); });
    if (warns.length && !confirm(`Double-check before sending:\n\n• ${warns.join("\n• ")}\n\nSend anyway?`)) {
      const first = $(".mwarns:not(.hidden)", mForm); if (first) { first.closest(".mcard").classList.remove("closed"); first.scrollIntoView({ block: "center" }); }
      return;
    }
    const items = cards.map(c => {
      const o = { ...c }; delete o.id; delete o.open;
      Object.keys(o).filter(k => k.endsWith("__custom")).forEach(k => delete o[k]);
      return o;
    });
    const ph = {}, meta = {}, keep = {};
    cards.forEach((c, i) => {
      for (let k = 1; k <= MS.measure.photos_per_item; k++) {
        const v = photos[`${c.id}:${k}`], slot = `i${i + 1}p${k}`;
        if (v instanceof Blob) { ph[slot] = v; meta[slot] = photoMeta[`${c.id}:${k}`] || { status: "missing" }; }
        else if (v && v.keep) keep[slot] = v.keep;
      }
    });
    btn.disabled = true; btn.textContent = "Saving…";
    clearTimeout(mDraftTimer);
    const entry = { id: newId(), slug: "measure", type: MS.type, userId: me.id, user: me.name,
      fields: { ...job, items: JSON.stringify(items), keep: JSON.stringify(keep), revision_of: mJob.revision_of || "" },
      photos: ph, photoMeta: meta, measured_by: mJob.measured_by, startedAt, createdAt: new Date().toISOString(), tries: 0 };
    try {
      await outboxPut(entry);
      await draftDel("measure").catch(() => {});
    } catch (e) {
      btn.disabled = false; btn.textContent = "Send measure";
      err.textContent = "This phone couldn't save the measure (storage full?). Don't close this page; free up space and try again.";
      err.classList.remove("hidden");
      return;
    }
    btn.textContent = "Sending…";
    const result = await sendEntry(entry);
    btn.disabled = false; btn.textContent = "Send measure";
    showResult(result, entry);
    updateBanner();
  });

  // a measure that came back from the server (or the outbox) needing a fix goes back into the editor as it was sent
  function entryToMeasure(entry) {
    let items = [], keep = {};
    try { items = JSON.parse(entry.fields.items || "[]"); keep = JSON.parse(entry.fields.keep || "{}"); } catch (e) { /* leave empty */ }
    const cards = items.map(it => ({ ...it, id: cardId(), open: true }));
    const ph = {}, meta = {};
    cards.forEach((c, i) => {
      for (let k = 1; k <= 4; k++) {
        const slot = `i${i + 1}p${k}`;
        if (entry.photos && entry.photos[slot]) { ph[`${c.id}:${k}`] = entry.photos[slot]; meta[`${c.id}:${k}`] = (entry.photoMeta || {})[slot]; }
        else if (keep[slot]) ph[`${c.id}:${k}`] = { keep: keep[slot] };
      }
    });
    return { job: { customer: entry.fields.customer, po: entry.fields.po, date: entry.fields.date }, cards, photos: ph, photoMeta: meta,
      startedAt: entry.startedAt, revision_of: entry.fields.revision_of || "", measured_by: entry.measured_by || me.name };
  }
  async function openAny(slug, back, message) {
    if (slug === "measure") {
      if (back) { await draftPut("measure", back).catch(() => {}); await openMeasureEditor(back); }
      else return openMeasures();
      if (message) { $("#mError").textContent = message; $("#mError").classList.remove("hidden"); }
      return;
    }
    await openForm(slug, back);
    if (message) { $("#formError").textContent = message; $("#formError").classList.remove("hidden"); }
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
    if (existing && (e.slug !== "measure" || existing.cards) &&
        !confirm(`You have an unfinished ${e.type} open. Replace it with the one that needs fixing?`)) return;
    const back = e.slug === "measure" ? entryToMeasure(e)
      : { fields: e.fields, photos: e.photos, photoMeta: e.photoMeta, startedAt: e.startedAt };
    await draftPut(e.slug, back);
    await outboxDel(e.id);
    await openAny(e.slug, back, "Fix this, then send again: " + e.error);
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

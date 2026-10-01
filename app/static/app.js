/* SimplyDoors Operations — staff app.
   Reports are saved on the phone first (outbox) and only removed once the server
   confirms it has them, so nothing is lost on a bad signal. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const RECEIVING = {
    slug: "receiving", type: "Receiving Report",
    photos: { ticket: ["ticket1", "ticket2", "ticket3"], product: ["product1", "product2"] },
    labels: { ticket1: "Ticket 1", ticket2: "Ticket 2", ticket3: "Ticket 3", product1: "Product 1", product2: "Product 2" },
  };

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

  // ------------------------------------------------------------ home
  async function showHome() {
    show("viewHome");
    updateGeoNote();
    const list = $("#recentList");
    const pending = (await outboxAll()).filter(e => e.userId === me.id);
    let rows = pending.map(e => `<li><span>${esc(e.type)} · ${esc(e.fields.po || "")} ${esc(e.fields.customer || "")}</span><span class="tag wait">${e.error ? "Needs fixing" : "Waiting to send"}</span></li>`);
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
    if (open && open.dataset.open === "receiving") openReceiving();
    if (ev.target.closest("[data-home]")) showHome();
  });

  // ------------------------------------------------------------ receiving form
  const form = $("#receivingForm");

  function buildSelects() {
    const loc = $("#location");
    loc.innerHTML = `<option value="" disabled selected>Pick a location…</option>` +
      me.locations.map(l => `<option>${esc(l)}</option>`).join("") +
      `<option value="Custom">Somewhere else (type it)</option>`;
    const sales = $("#sales_notify");
    sales.innerHTML = `<option value="none">Don't notify anyone</option>` +
      me.sales_reps.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
  }

  function renderTiles() {
    Object.entries(RECEIVING.photos).forEach(([group, slots]) => {
      const box = $(`.photos[data-group="${group}"]`);
      box.innerHTML = "";
      slots.forEach(slot => {
        const t = document.createElement("div");
        t.className = "tile" + (photos[slot] ? " filled" : "");
        t.dataset.slot = slot;
        t.setAttribute("role", "button"); t.tabIndex = 0;
        t.setAttribute("aria-label", photos[slot] ? `${RECEIVING.labels[slot]} added` : `Add ${RECEIVING.labels[slot]}`);
        if (photos[slot]) {
          if (!previews[slot]) previews[slot] = URL.createObjectURL(photos[slot]);
          const m = photoMeta[slot] || {};
          const badge = m.status === "ok" ? (m.acc > 200 ? `<span class="geo warn">📍 ±${m.acc} m</span>` : `<span class="geo">📍 Located</span>`)
            : m.status ? `<span class="geo warn">No location</span>` : "";
          t.innerHTML = `<img src="${previews[slot]}" alt="">${badge}<button type="button" class="remove" aria-label="Remove photo">×</button>`;
          t.querySelector(".remove").onclick = (e) => { e.stopPropagation(); removePhoto(slot); };
        } else {
          t.innerHTML = `<span><span class="plus">+</span>${esc(RECEIVING.labels[slot])}</span>`;
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

  $("#location").addEventListener("change", () => {
    const custom = $("#location").value === "Custom";
    $("#location_custom").classList.toggle("hidden", !custom);
    if (custom) $("#location_custom").focus();
  });

  function readFields() {
    const f = {};
    ["po", "customer", "location", "location_custom", "sales_notify", "remarks"].forEach(k => { f[k] = form.elements[k].value; });
    ["sop_unloaded", "sop_inspected", "sop_entered"].forEach(k => { f[k] = form.elements[k].checked; });
    return f;
  }
  function writeFields(f) {
    form.reset();
    ["po", "customer", "location_custom", "remarks"].forEach(k => { form.elements[k].value = f[k] || ""; });
    if (f.location) form.elements.location.value = f.location;
    if (f.sales_notify) form.elements.sales_notify.value = f.sales_notify;
    ["sop_unloaded", "sop_inspected", "sop_entered"].forEach(k => { form.elements[k].checked = !!f[k]; });
    $("#location_custom").classList.toggle("hidden", form.elements.location.value !== "Custom");
  }

  let draftTimer = null;
  function saveDraftSoon() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(async () => {
      try {
        await draftPut(RECEIVING.slug, { fields: readFields(), photos: { ...photos }, photoMeta: { ...photoMeta }, startedAt });
        $("#draftNote").textContent = "Saved on this phone";
      } catch (e) { /* storage full or private mode: the form still works */ }
    }, 400);
  }
  form.addEventListener("input", (e) => { e.target.classList.remove("invalid"); saveDraftSoon(); });
  form.addEventListener("change", (e) => { e.target.classList.remove("invalid"); saveDraftSoon(); });

  async function openReceiving(prefill) {
    buildSelects();
    Object.values(previews).forEach(u => URL.revokeObjectURL(u));
    photos = {}; previews = {}; photoMeta = {};
    $("#formError").classList.add("hidden");
    $$(".invalid", form).forEach(el => el.classList.remove("invalid"));
    let draft = prefill || null;
    if (!draft) { try { draft = await draftGet(RECEIVING.slug); } catch (e) { draft = null; } }
    if (draft) {
      writeFields(draft.fields || {});
      photos = { ...(draft.photos || {}) };
      photoMeta = { ...(draft.photoMeta || {}) };
      startedAt = draft.startedAt || new Date().toISOString();
      $("#draftNote").textContent = prefill ? "Fix the problem below, then submit again" : "Picked up where you left off";
    } else {
      writeFields({});
      startedAt = new Date().toISOString();
      $("#draftNote").textContent = "";
    }
    renderTiles();
    show("viewReceiving");
  }

  $("#clearBtn").addEventListener("click", async () => {
    if (!confirm("Clear everything on this form, including photos?")) return;
    await draftDel(RECEIVING.slug).catch(() => {});
    openReceiving();
  });

  function validate(f) {
    const problems = [];
    const mark = (name, msg) => { const el = form.elements[name]; if (el) el.classList.add("invalid"); problems.push(msg); };
    if (!f.po.trim()) mark("po", "Job / PO number");
    if (!f.customer.trim()) mark("customer", "Customer name");
    if (!f.location) mark("location", "Location");
    if (f.location === "Custom" && !f.location_custom.trim()) mark("location_custom", "Type the location");
    [["sop_unloaded", "Unloaded box"], ["sop_inspected", "Inspected box"], ["sop_entered", "Entered box"]].forEach(([k, label]) => {
      if (!f[k]) { form.elements[k].closest("label").classList.add("invalid"); problems.push(label); }
    });
    return problems;
  }
  form.addEventListener("change", (e) => { if (e.target.type === "checkbox") e.target.closest("label").classList.remove("invalid"); });

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
    btn.disabled = true; btn.textContent = "Saving…";
    clearTimeout(draftTimer);
    const entry = { id: newId(), slug: RECEIVING.slug, type: RECEIVING.type, userId: me.id, user: me.name, fields: f,
      photos: { ...photos }, photoMeta: { ...photoMeta }, startedAt, createdAt: new Date().toISOString(), tries: 0 };
    try {
      await outboxPut(entry);              // safe on the phone before anything else
      await draftDel(RECEIVING.slug).catch(() => {});
    } catch (e) {
      btn.disabled = false; btn.textContent = "Submit Receiving Report";
      err.textContent = "This phone couldn't save the report (storage full?). Don't close this page; free up space and try again.";
      err.classList.remove("hidden");
      return;
    }
    btn.textContent = "Sending…";
    const result = await sendEntry(entry);
    btn.disabled = false; btn.textContent = "Submit Receiving Report";
    showResult(result, entry);
    updateBanner();
  });

  function showResult(result, entry) {
    const box = $("#resultBox");
    if (result.ok) {
      box.className = "result";
      box.innerHTML = `<div class="big">Received ✓</div><div class="muted">Your receipt number</div>
        <div class="receipt">${esc(result.receipt)}</div><p class="muted small">The office will be emailed a copy.</p>`;
    } else if (result.fix) {
      // The server rejected something; put it straight back in the form (the form slot is empty: we just submitted it).
      // Save it as the draft first, and only then take it out of the queue.
      const back = { fields: entry.fields, photos: entry.photos, photoMeta: entry.photoMeta, startedAt: entry.startedAt };
      draftPut(RECEIVING.slug, back).then(() => outboxDel(entry.id)).then(updateBanner).catch(() => {});
      openReceiving(back);
      $("#formError").textContent = result.message; $("#formError").classList.remove("hidden");
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
      fd.append(slot, blob, slot + ".jpg");
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
      if (e.status === 422 || e.status === 400 || e.status === 409) {
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
    const existing = await draftGet(RECEIVING.slug).catch(() => null);
    if (existing && !confirm("You have an unfinished Receiving Report open. Replace it with the one that needs fixing?")) return;
    const back = { fields: e.fields, photos: e.photos, photoMeta: e.photoMeta, startedAt: e.startedAt };
    await draftPut(RECEIVING.slug, back);
    await outboxDel(e.id);
    await openReceiving(back);
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
  loadMe().then(updateBanner);
})();

/* SimplyDoors customer form (/start).
   What the customer sends is saved on their phone first and kept there until the server has it, so a bad signal
   never loses it. Each send carries its own id, so a retry can never make a second lead. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const TOKEN = document.body.dataset.token || "";
  const TEST = document.body.dataset.test || "";            // "on": a working test link; "ended": an old one
  const T = new URLSearchParams(location.search).get("t") || "";
  const B = document.body.dataset;
  const SEND = B.send || "", MODE = B.mode || "";             // MODE: "in_person" (handed over) or "device" (installed form)
  const OPENED = Date.now();
  const MAX_FILES = 5, MAX_PDF = 10 * 1024 * 1024, MAX_PHOTO = 15 * 1024 * 1024;
  const RETRY = [3, 10, 30, 60];                             // seconds between tries while the page is open
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  // ------------------------------------------------------------ test links (the owner's test mode)
  const bar = $("#testBar");
  if (TEST === "on") {
    bar.textContent = "TEST link: what you send here is marked TEST. Every email about it, the customer's receipt too, goes only to the app owner.";
    bar.classList.remove("hidden");
  } else if (TEST === "ended") {
    bar.textContent = "This test link has ended. Make a new one on the Leads screen (Test mode must be on).";
    bar.className = "testbar bad";
    $("#f1").classList.add("hidden");
  }

  // ------------------------------------------------------------ a sent link, a handed-over phone, an installed form
  if (B.company) $("#company").value = B.company;
  if (B.prefill) { $("#name").value = B.prefill; $("#hello").textContent = `Hi ${B.prefill}, start your project`; }
  if (B.who && !MODE) { $("#sentBy").textContent = `${B.who} from SimplyDoors sent you this form.`; $("#sentBy").classList.remove("hidden"); }
  if (MODE) document.body.classList.add("kiosk");
  const params = new URLSearchParams(location.search);
  const ibar = $("#installBar");
  let installEvt = null;
  const installed = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const isApple = /iPhone|iPad|iPod/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
  function showInstall() {
    if (MODE !== "device" || !params.get("install") || installed()) return;
    ibar.classList.remove("hidden");
    ibar.innerHTML = installEvt
      ? `<b>Install the customer form on this device</b><button type="button" class="btn small" id="doInstall">Install</button>`
      : isApple ? `<b>Install the customer form on this ${/iPad/.test(navigator.userAgent) || navigator.maxTouchPoints > 1 ? "iPad" : "iPhone"}:</b>
          tap <b>Share</b> <span aria-label="Share">⬆︎</span>, then <b>Add to Home Screen</b>. It gets its own “SD Start” icon that opens this form.`
      : `<b>Install the customer form:</b> open your browser's menu ⋮ and choose <b>Install app</b> or <b>Add to Home screen</b>.`;
    const d = $("#doInstall");
    if (d) d.onclick = async () => { installEvt.prompt(); await installEvt.userChoice.catch(() => null); installEvt = null; ibar.classList.add("hidden"); };
  }
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvt = e; showInstall(); });
  showInstall();

  // ------------------------------------------------------------ storage on this phone (IndexedDB)
  const mem = new Map();          // a browser that can't store anything (some private modes) keeps it while the page is open
  let dbp = null;
  function db() {
    if (!dbp) dbp = new Promise((res, rej) => {
      const r = indexedDB.open("sdintake", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("outbox", { keyPath: "id" });
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
    return dbp;
  }
  async function idb(mode, fn) {
    const d = await db();
    return new Promise((res, rej) => {
      const tx = d.transaction("outbox", mode);
      const req = fn(tx.objectStore("outbox"));
      tx.oncomplete = () => res(req ? req.result : undefined);
      tx.onerror = () => rej(tx.error);
      tx.onabort = () => rej(tx.error);
    });
  }
  async function all() {
    try { const got = await idb("readonly", s => s.getAll()); return got.length ? got : [...mem.values()]; }
    catch (e) { return [...mem.values()]; }
  }
  async function put(e) { mem.set(e.id, e); try { await idb("readwrite", s => s.put(e)); } catch (er) { /* kept in memory */ } }
  async function del(id) { mem.delete(id); try { await idb("readwrite", s => s.delete(id)); } catch (e) { /* */ } }
  function newId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    return [...b].map(x => x.toString(16).padStart(2, "0")).join("").replace(/^(.{8})(.{4})(.{4})(.{4})/, "$1-$2-$3-$4-");
  }

  // ------------------------------------------------------------ screens
  function show(id) {
    ["stepOne", "waiting", "done"].forEach(s => $("#" + s).classList.toggle("hidden", s !== id));
    window.scrollTo(0, 0);
  }
  function showWaiting(title, text, canRetry) {
    $("#waitTitle").textContent = title;
    $("#waitText").textContent = text;
    $("#tryNow").classList.toggle("hidden", !canRetry);
    show("waiting");
  }
  function showErr(id, msg) {
    const el = $(id);
    el.textContent = msg;
    el.classList.toggle("hidden", !msg);
    if (msg) el.scrollIntoView({ block: "center" });
  }

  // ------------------------------------------------------------ photos and PDFs
  let files = [];       // {blob, name, kind, url}
  let busy = 0;         // photos still being made smaller
  $("#addFile").addEventListener("click", () => $("#picker").click());
  $("#picker").addEventListener("change", async (ev) => {
    const list = [...(ev.target.files || [])];
    ev.target.value = "";
    const room = MAX_FILES - files.length - busy;
    fileNote(list.length > room ? `Up to ${MAX_FILES} files.${room > 0 ? ` Only the first ${room} were added.` : ""}` : "");
    for (const f of list.slice(0, Math.max(0, room))) await addFile(f);
  });
  function fileNote(msg) {
    const n = $("#fileNote");
    n.textContent = msg || "Photos of the door, window or room help us come prepared.";
    n.classList.toggle("bad", !!msg);
  }
  async function addFile(f) {
    if (f.type === "application/pdf" || /\.pdf$/i.test(f.name || "")) {
      if (f.size > MAX_PDF) return fileNote(`${f.name} is too big. PDFs can be up to 10 MB.`);
      files.push({ blob: f, name: f.name || "plans.pdf", kind: "pdf" });
      return render();
    }
    busy++; render();
    let blob = null;
    try { blob = await shrink(f); } catch (e) { blob = null; }
    busy--;
    if (!blob && /^image\/(jpeg|png|webp)$/.test(f.type) && f.size <= MAX_PHOTO) blob = f;   // couldn't shrink it: send as is
    if (!blob) { render(); return fileNote(`${f.name || "That file"} can't be sent. Send photos (JPG or PNG) or PDF files.`); }
    const name = (f.name || "photo").replace(/\.[^.]*$/, "") + ".jpg";
    files.push({ blob, name, kind: "photo", url: URL.createObjectURL(blob) });
    render();
  }
  // smaller photos send faster on a weak signal: at most 1600 px, as a JPEG
  function shrink(file) {
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
    return Promise.race([work, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 25000))]);
  }
  function render() {
    const box = $("#files");
    box.innerHTML = "";
    files.forEach((f, i) => {
      const d = document.createElement("div");
      d.className = "file";
      if (f.kind === "photo") {
        const img = document.createElement("img"); img.src = f.url; img.alt = f.name; d.appendChild(img);
      } else {
        const s = document.createElement("span"); s.className = "pdf"; s.textContent = "📄"; d.append(s, f.name);
      }
      const rm = document.createElement("button");
      rm.type = "button"; rm.className = "rm"; rm.textContent = "×"; rm.setAttribute("aria-label", "Remove " + f.name);
      rm.onclick = () => { if (f.url) URL.revokeObjectURL(f.url); files.splice(i, 1); fileNote(""); render(); };
      d.appendChild(rm);
      box.appendChild(d);
    });
    for (let i = 0; i < busy; i++) { const d = document.createElement("div"); d.className = "file busy"; box.appendChild(d); }
    $("#addFile").disabled = files.length + busy >= MAX_FILES;
  }

  // ------------------------------------------------------------ step 1
  const f1 = $("#f1");
  function values() {
    const v = (id) => $("#" + id).value.trim();
    return { name: v("name"), company: v("company"), phone: v("phone"), email: v("email"), address: v("address"), description: v("description"),
      website: $("#website").value, types: $$("input[name=types]:checked", f1).map(i => i.value),
      heard: ($("input[name=heard]:checked", f1) || {}).value || "" };
  }
  function problems(v) {
    $$(".invalid", f1).forEach(el => el.classList.remove("invalid"));
    const out = [];
    const bad = (id, msg) => { $("#" + id).classList.add("invalid"); out.push(msg); };
    if (v.name.length < 2) bad("name", "your name");
    const digits = v.phone.replace(/\D/g, "");
    if (v.phone && (digits.length < 10 || digits.length > 15)) bad("phone", "a phone number with all 10 digits");
    if (v.email && !/^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$/.test(v.email)) bad("email", "an email address that looks right");
    if (!v.phone && !v.email) { bad("phone", "a phone number or an email"); $("#email").classList.add("invalid"); }
    return out;
  }
  f1.addEventListener("input", (ev) => {     // a box turns back to normal as soon as it's fixed
    const ids = ["phone", "email"].includes(ev.target.id) ? ["phone", "email"] : [ev.target.id];
    ids.forEach(id => { const el = $("#" + id); if (el) el.classList.remove("invalid"); });
    if (!$(".invalid", f1)) showErr("#err1", "");
  });
  let current = null;       // the send this screen is about
  let sending = false;
  f1.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (sending) return;
    const v = values();
    const p = problems(v);
    if (p.length) { showErr("#err1", "Still needed: " + p.join(", ") + "."); const first = $(".invalid", f1); if (first) first.focus(); return; }
    if (busy) { showErr("#err1", "One moment: a photo is still getting ready."); return; }
    showErr("#err1", "");
    sending = true;
    $("#send").disabled = true; $("#send").textContent = "Sending…";
    const wait = 3500 - (Date.now() - OPENED);       // a person is never mistaken for a robot that sends at once
    if (wait > 0) await sleep(wait);
    current = { id: newId(), token: TOKEN, t: T, s: SEND, fields: v, files: files.map(f => ({ blob: f.blob, name: f.name, kind: f.kind })),
      createdAt: Date.now(), receipt: null, more: null, moreDone: false };
    await put(current);                              // on the phone before anything else
    showWaiting("Sending…", "One moment.", false);
    await pump();
  });
  function resetForm() {
    sending = false;
    $("#send").disabled = false; $("#send").textContent = "Send";
  }
  function clearForm() {
    f1.reset();
    files.forEach(f => f.url && URL.revokeObjectURL(f.url));
    files = []; render(); fileNote("");
    resetForm();
  }
  function backToForm(e, msg) {
    // the server refused it (a mistake to fix): put it back in the form as it was sent
    const f = e.fields || {};
    ["name", "company", "phone", "email", "address", "description"].forEach(k => { $("#" + k).value = f[k] || ""; });
    $$("input[name=types]", f1).forEach(i => { i.checked = (f.types || []).includes(i.value); });
    $$("input[name=heard]", f1).forEach(i => { i.checked = i.value === f.heard; });
    files.forEach(x => x.url && URL.revokeObjectURL(x.url));
    files = (e.files || []).map(x => ({ ...x, url: x.kind === "photo" ? URL.createObjectURL(x.blob) : null }));
    render();
    resetForm();
    show("stepOne");
    showErr("#err1", msg);
  }

  // ------------------------------------------------------------ sending (and trying again)
  async function post(path, opts) {
    let r;
    try {
      r = await fetch(path, { method: "POST", credentials: "omit",
        headers: { "X-SD-App": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}) },
        body: opts.json ? JSON.stringify(opts.json) : opts.body });
    } catch (e) { return { wait: true }; }                // no signal
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (r.ok) return { ok: true, receipt: data && data.receipt };
    if (r.status >= 500 || [408, 411, 429].includes(r.status)) return { wait: true };
    return { fix: true, message: (data && data.detail) || "Something went wrong. Check the form and try again." };
  }
  function sendOne(e) {
    const fd = new FormData();
    fd.append("submission_id", e.id);
    fd.append("token", e.token);
    if (e.t) fd.append("t", e.t);
    if (e.s) fd.append("s", e.s);
    const f = e.fields;
    ["name", "company", "phone", "email", "address", "description", "website"].forEach(k => fd.append(k, f[k] || ""));
    (f.types || []).forEach(t => fd.append("types", t));
    if (f.heard) fd.append("heard", f.heard);
    (e.files || []).forEach(x => fd.append("files", x.blob, x.name));
    return post("api/submit", { body: fd });
  }
  const sendMore = (e) => post("api/more", { json: { submission_id: e.id, ...e.more } });

  let tries = 0, timer = null, pumping = false;
  function later() {
    clearTimeout(timer);
    timer = setTimeout(pump, RETRY[Math.min(tries, RETRY.length - 1)] * 1000);
    tries++;
  }
  async function pump() {
    if (pumping) return;
    pumping = true;
    clearTimeout(timer);
    let again = false;
    try {
      const list = (await all()).sort((a, b) => a.createdAt - b.createdAt);
      for (const e of list) {
        const mine = current && e.id === current.id;
        if (!e.receipt) {
          const r = await sendOne(e);
          if (r.ok) {
            e.receipt = r.receipt;
            await put(e);
            if (mine) { current = e; clearForm(); showDone(e); }
          } else if (r.fix) {
            await del(e.id);
            if (mine) backToForm(e, r.message);
          } else {
            again = true;
            if (mine) showWaiting("Saved on your phone", "There's no signal right now. It will send by itself as soon as there is. Please keep this page open.", true);
          }
        } else if (e.more && !e.moreDone) {
          const r = await sendMore(e);
          if (r.wait) again = true;
          else {
            await del(e.id);
            if (mine) thanks(r.ok ? "That helps us prepare. You can close this page." : r.message);
          }
        } else if (e.moreDone || !mine) {
          await del(e.id);          // finished, or left over from an earlier visit
        }
      }
    } finally { pumping = false; }
    if (again) later(); else tries = 0;
  }
  $("#tryNow").addEventListener("click", () => { tries = 0; pump(); });
  window.addEventListener("online", () => { tries = 0; pump(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pump(); });

  // ------------------------------------------------------------ "Got it" and step 2
  const counts = { doors: null, windows: null };
  function showDone(e) {
    $("#receipt").textContent = e.receipt;
    const note = $("#emailNote");
    note.textContent = TEST === "on" || /^TEST/.test(e.receipt || "") ? "Test: the receipt email goes to the app owner, not to this address."
      : "We'll also email you a receipt.";
    note.classList.toggle("hidden", !(e.fields && e.fields.email));
    $("#f2").reset();
    counts.doors = counts.windows = null;
    $("#doors").textContent = $("#windows").textContent = "–";
    $("#f2").classList.remove("hidden");
    $("#thanks").classList.add("hidden");
    showErr("#err2", "");
    show("done");
  }
  $$("[data-count]").forEach(b => b.addEventListener("click", () => {
    const k = b.dataset.count;
    counts[k] = Math.max(0, Math.min(99, (counts[k] ?? 0) + Number(b.dataset.step)));
    $("#" + k).textContent = counts[k];
  }));
  function thanks(text) {
    $("#f2").classList.add("hidden");
    $("#thanksText").textContent = text;
    $("#thanks").classList.remove("hidden");
    if (MODE) {                                   // a staff phone or the showroom tablet: ready for the next person
      $("#thanksText").textContent = "We'll call you within 1 business day.";
      $("#handBack").classList.remove("hidden");
      $("#backStaff").classList.toggle("hidden", MODE !== "in_person");
      $("#handBack .big").textContent = MODE === "in_person" ? "Please hand the phone back" : "All done. Thank you!";
    }
  }
  $("#nextCustomer").addEventListener("click", () => {
    // a fresh page: new token, empty form (an in-person code works for 12 hours)
    location.replace(location.pathname + "?s=" + encodeURIComponent(SEND));
  });
  $("#f2").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!current) return;
    const pick = (n) => ($(`input[name=${n}]:checked`, ev.target) || {}).value;
    const more = {};
    ["doors", "windows"].forEach(k => { if (counts[k] !== null) more[k] = counts[k]; });
    ["timeline", "who", "best_time"].forEach(k => { const v = pick(k); if (v) more[k] = v; });
    if (!Object.keys(more).length) { skip(); return; }
    current.more = more;
    await put(current);
    $("#saveMore").disabled = true;
    await pump();
    $("#saveMore").disabled = false;
    const still = (await all()).find(x => x.id === current.id);
    if (still && still.more && !still.moreDone) thanks("Saved on your phone. It will send by itself when there's signal. Please keep this page open a moment.");
  });
  async function skip() {
    if (current) { current.moreDone = true; await del(current.id); }
    thanks("You can close this page.");
  }
  $("#skipMore").addEventListener("click", skip);

  // ------------------------------------------------------------ start: anything from an earlier visit that didn't send?
  (async () => {
    const waiting = (await all()).filter(e => !e.receipt).sort((a, b) => a.createdAt - b.createdAt);
    if (waiting.length) {
      current = waiting[0];
      showWaiting("Sending what you sent earlier…", "It didn't go through last time. Trying again now.", true);
    }
    pump();
  })();
})();

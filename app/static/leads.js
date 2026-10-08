/* SimplyDoors Operations — Leads: what customers sent with the form at /start.
   Only people with "Can see Leads" (and admins) get past the server. Every view, claim, status change and note
   is in the activity log. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TZ = "America/Chicago";
  const when = (iso) => iso ? new Date(iso).toLocaleString([], { timeZone: TZ, month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit" }) : "";
  function ago(iso) {
    const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
  }

  async function api(path, opts = {}) {
    let r;
    try {
      r = await fetch(path, { credentials: "same-origin", method: opts.method || "GET",
        headers: { "X-SD-App": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}) },
        body: opts.json ? JSON.stringify(opts.json) : undefined });
    } catch (e) { const err = new Error("No signal right now. Try again in a moment."); err.status = 0; throw err; }
    let data = null; try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) { const err = new Error((data && data.detail) || `Error ${r.status}`); err.status = r.status; throw err; }
    return data;
  }
  let toastTimer;
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
  }
  const fail = (e) => toast(e.message || "Something went wrong");

  let me = null, tab = "leads", PEOPLE = [], STATUSES = [], CHOICES = null, staleHours = 24;

  function gate(msg) {
    $("#gateMsg").textContent = msg;
    $("#gate").classList.remove("hidden");
    $("#ui").classList.add("hidden");
  }

  // ------------------------------------------------------------ test mode (owner): a link that sends TEST leads
  function testBox() {
    const box = $("#testBox");
    if (!me.is_owner) return box.classList.add("hidden");
    box.classList.remove("hidden");
    if (!me.test_mode) {
      box.className = "testlink off";
      box.innerHTML = `<b>Trying out the customer form?</b> Turn on <b>Test mode</b> on the <a href="./">home screen</a>, then come back here for a test link.`;
      return;
    }
    box.className = "testlink";
    box.innerHTML = `<b>Test mode is on.</b> A test link opens the customer form; what you send through it is a <b>TEST</b> lead
      that only you see, and every email about it (the customer's receipt too) goes only to you.
      <div><button type="button" class="btn small" id="makeTest">Make a test link</button></div><div id="testOut"></div>`;
    $("#makeTest").onclick = async () => {
      let d;
      try { d = await api("api/leads/test-link", { method: "POST" }); } catch (e) { return fail(e); }
      $("#testOut").innerHTML = `<div class="linkbox">${esc(d.staff_url)}</div>
        <div class="lbtns"><a class="mini primary" href="${esc(d.staff_url)}" target="_blank" rel="noopener">Open it</a>
        <a class="mini" href="sms:?&body=${encodeURIComponent(d.staff_url)}">Text it to myself</a>
        <button type="button" class="mini" id="copyTest">Copy</button></div>
        <p class="muted small">Works until ${esc(when(d.expires_at))}, or until you make a new one or turn Test mode off.</p>
        <p class="muted small">This opens the form through the staff app's address, so it works now. The customers' address,
        <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url.split("?")[0])}</a> with the same code, works once the
        server step (intake-setup.sh) has been run.</p>`;
      $("#copyTest").onclick = async () => {
        try { await navigator.clipboard.writeText(d.staff_url); toast("Copied"); } catch (e) { toast("Press and hold the link to copy it"); }
      };
    };
  }

  // ------------------------------------------------------------ list
  const statusLabel = (k) => (STATUSES.find(s => s.key === k) || { label: k }).label;
  $$(".ltabs button").forEach(b => b.onclick = () => {
    tab = b.dataset.tab;
    $$(".ltabs button").forEach(x => x.classList.toggle("on", x === b));
    loadList();
  });

  async function loadList() {
    let d;
    try { d = await api(`api/leads?tab=${tab}`); } catch (e) { $("#list").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    PEOPLE = d.people; STATUSES = d.statuses; CHOICES = d.choices; staleHours = d.stale_hours;
    const cn = $("#cNew"), cs = $("#cSpam");
    cn.textContent = d.counts.new; cn.classList.toggle("hidden", !d.counts.new);
    cs.textContent = d.counts.spam; cs.classList.toggle("hidden", !d.counts.spam);
    $("#tabNote").textContent = tab === "spam"
      ? "These looked like spam, so nobody was emailed or alerted. If one is a real customer, open it and tap “Not spam: move to Leads”."
      : "Newest first. Claim a lead so everyone knows who is calling.";
    $("#list").innerHTML = d.leads.length ? d.leads.map(card).join("")
      : `<p class="muted center">${tab === "spam" ? "Nothing in Suspected spam." : "No leads yet."}</p>`;
    $$("#list .lcard").forEach(c => c.onclick = () => { location.hash = "lead=" + c.dataset.id; });
    $("#blocked").innerHTML = tab === "spam" && d.blocked ? blockedHtml(d.blocked) : "";
  }
  function card(l) {
    const who = l.owner ? `Claimed by <b>${esc(l.owner)}</b>` : `<span class="unclaimed">Not claimed</span>`;
    return `<button type="button" class="lcard${l.stale ? " stale" : ""}" data-id="${l.id}">
      <span class="l1"><b class="lname">${esc(l.name)}</b>${l.is_test ? ' <span class="tag test">TEST</span>' : ""}
        ${l.spam ? "" : `<span class="st st-${esc(l.status)}">${esc(statusLabel(l.status))}</span>`}</span>
      <span class="l2">${esc([l.types.join(", "), l.address].filter(Boolean).join(" · ") || "No details")}</span>
      <span class="l3">${l.source ? `${esc(l.source)} · ` : ""}${esc(ago(l.submitted_at))} · ${l.spam ? esc(l.receipt) : who}${l.files ? ` · 📷 ${l.files}` : ""}</span>
      ${l.stale ? `<span class="flag">⚠ Claimed, but no update in ${staleHours} hours</span>` : ""}
      ${l.spam ? `<span class="why">${esc(l.spam)}</span>` : ""}</button>`;
  }
  function blockedHtml(b) {
    if (!b.count) return `<p class="muted small center">No robots were stopped in the last 7 days.</p>`;
    return `<details class="blocked"><summary>Stopped automatically in the last 7 days: <b>${b.count}</b></summary>
      <p class="muted small">Robots filling in the form. They were shown a normal “Got it”, but nothing was saved as a lead and nobody was
      emailed. Kept 30 days.</p>
      ${b.rows.map(r => `<div class="brow"><div><b>${esc(r.name || "(no name)")}</b>${r.is_test ? ' <span class="tag test">TEST</span>' : ""}
        <span class="muted small">${esc(when(r.at))} · showed ${esc(r.receipt)}</span></div>
        <div class="small">${esc(r.reason)}</div>${r.contact ? `<div class="small muted">${esc(r.contact)}</div>` : ""}
        ${r.text ? `<div class="small muted">${esc(r.text)}</div>` : ""}</div>`).join("")}</details>`;
  }

  // ------------------------------------------------------------ add a lead by hand (a phone call, a walk-in)
  async function addForm() {
    if (!CHOICES) { try { CHOICES = (await api("api/leads?tab=leads")).choices; } catch (e) { fail(e); location.hash = ""; return; } }
    $("#listView").classList.add("hidden");
    const v = $("#detailView");
    v.classList.remove("hidden");
    const chip = (name, val, type) => `<label><input type="${type}" name="${name}" value="${esc(val)}"><span>${esc(val)}</span></label>`;
    v.innerHTML = `<div class="topline"><button class="back" type="button" id="backBtn">‹ All leads</button></div>
      <h1>Add a lead</h1>
      ${me.test_mode ? '<p class="flag">Test mode is on: this will be a TEST lead.</p>' : ""}
      <form id="addLead" class="sec" novalidate>
        <label>How did it come in?</label><div class="lchips">${CHOICES.sources.map((s, i) => chip("source", s, "radio").replace("<input", i ? "<input" : "<input checked")).join("")}</div>
        <label for="aName">Name *</label><input id="aName" name="name" type="text" maxlength="80" autocomplete="off">
        <label for="aPhone">Phone</label><input id="aPhone" name="phone" type="tel" inputmode="tel" maxlength="30" autocomplete="off">
        <label for="aEmail">Email</label><input id="aEmail" name="email" type="email" maxlength="120" autocomplete="off">
        <p class="muted small">A phone number or an email: at least one.</p>
        <label for="aAddress">Project address</label><input id="aAddress" name="address" type="text" maxlength="200" autocomplete="off">
        <label>What's the project?</label><div class="lchips">${CHOICES.types.map(t => chip("types", t, "checkbox")).join("")}</div>
        <label for="aDesc">What they need</label><textarea id="aDesc" name="description" rows="4" maxlength="4000"></textarea>
        <label>How did they hear about us?</label><div class="lchips">${CHOICES.heard.map(h => chip("heard", h, "radio")).join("")}</div>
        <label class="check"><input type="checkbox" name="claim" checked> Claim it for me</label>
        <p class="error hidden" id="addErr"></p>
        <button type="submit" class="btn">Save lead</button>
        <p class="muted small">Nobody is emailed: you already have it. It goes in the activity log like any lead.</p>
      </form>`;
    window.scrollTo(0, 0);
    $("#backBtn").onclick = () => { location.hash = ""; };
    $("#addLead").onsubmit = async (ev) => {
      ev.preventDefault();
      const f = ev.target, val = (n) => f.elements[n].value.trim();
      const body = { name: val("name"), phone: val("phone"), email: val("email"), address: val("address"), description: val("description"),
        source: ($("input[name=source]:checked", f) || {}).value || "", heard: ($("input[name=heard]:checked", f) || {}).value || "",
        types: $$("input[name=types]:checked", f).map(i => i.value), claim: f.elements.claim.checked };
      const err = $("#addErr");
      if (body.name.length < 2 || (!body.phone && !body.email)) {
        err.textContent = "Type their name, and a phone number or an email."; err.classList.remove("hidden"); return;
      }
      try {
        const d = await api("api/leads", { method: "POST", json: body });
        toast(`Saved as ${d.receipt}`);
        location.hash = "lead=" + d.id;
      } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
    };
  }

  // ------------------------------------------------------------ one lead
  function route() {
    if (location.hash === "#add") return addForm();
    const m = /lead=(\d+)/.exec(location.hash);
    if (m) return openLead(Number(m[1]));
    $("#detailView").classList.add("hidden");
    $("#listView").classList.remove("hidden");
    loadList();
  }
  window.addEventListener("hashchange", route);

  async function openLead(id) {
    let d;
    try { d = await api(`api/leads/${id}`); } catch (e) { fail(e); location.hash = ""; return; }
    if (!STATUSES.length) {   // opened straight from an email: the lists come with the list
      try { const l = await api(`api/leads?tab=leads`); PEOPLE = l.people; STATUSES = l.statuses; staleHours = l.stale_hours; } catch (e) { /* */ }
    }
    render(d);
  }

  const digits = (p) => String(p || "").replace(/\D/g, "");
  const tel = (p) => { const n = digits(p); return n.length === 10 ? "+1" + n : "+" + n; };

  function render(d) {
    $("#listView").classList.add("hidden");
    const v = $("#detailView");
    v.classList.remove("hidden");
    const contact = [
      d.phone ? `<a class="cbtn" href="tel:${esc(tel(d.phone))}"><span aria-hidden="true">📞</span>Call</a>` : "",
      d.phone ? `<a class="cbtn" href="sms:${esc(tel(d.phone))}"><span aria-hidden="true">💬</span>Text</a>` : "",
      d.email ? `<a class="cbtn" href="mailto:${esc(d.email)}?subject=${encodeURIComponent("Your SimplyDoors project (" + d.receipt + ")")}"><span aria-hidden="true">✉️</span>Email</a>` : "",
    ].join("");
    const others = PEOPLE.filter(p => p.id !== d.owner_id);
    const rows = [["Phone", d.phone], ["Email", d.email],
      ["Project address", d.address ? `${esc(d.address)} <a class="maplink" target="_blank" rel="noopener" href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(d.address)}">Map</a>` : "", true],
      ["Project", d.types.join(", ")], ["About the project", d.description], ["How they heard about us", d.heard],
      ["Came in", d.source + (d.added_by ? ` · added by ${d.added_by}` : "")]];
    v.innerHTML = `<div class="topline"><button class="back" type="button" id="backBtn">‹ All leads</button>
        <span class="muted small">${esc(d.receipt)}</span></div>
      <h1>${esc(d.name)}${d.is_test ? ' <span class="tag test">TEST</span>' : ""}</h1>
      <p class="muted small sub">${d.added_by ? "Added" : "Sent"} ${esc(when(d.submitted_at))} (${esc(ago(d.submitted_at))})</p>
      ${d.spam ? `<div class="spambox"><b>In Suspected spam</b>, so nobody was emailed or alerted.<div class="why">${esc(d.spam)}</div>
        <button type="button" class="btn" id="notSpam">Not spam: move to Leads</button>
        <p class="muted small">This emails the office and the customer's receipt, like any new lead.</p></div>` : ""}
      <div class="contact">${contact || '<p class="muted">No phone or email.</p>'}</div>

      ${d.spam ? "" : `<section class="sec">
        <h2>Who's on it</h2>
        ${d.owner ? `<p>Claimed by <b>${esc(d.owner)}</b> · ${esc(when(d.claimed_at))}</p>` : `<p class="unclaimed">Nobody has claimed it yet.</p>`}
        ${d.stale ? `<p class="flag">⚠ No claim, status change or note in ${staleHours} hours.</p>` : ""}
        ${d.owner_id === me.id ? "" : `<button type="button" class="btn${d.owner ? " secondary" : ""}" id="claim">${d.owner ? "Take it over myself" : "Claim it"}</button>`}
        ${others.length ? `<div class="assign"><label for="assignTo">Give it to</label><div class="arow">
          <select id="assignTo"><option value="">Pick a person…</option>${others.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
          <button type="button" class="mini primary" id="assignBtn">Give</button></div></div>` : ""}
      </section>
      <section class="sec"><h2>Status</h2>
        <div class="stbtns">${STATUSES.map(s => `<button type="button" class="stb${s.key === d.status ? " on" : ""}" data-st="${esc(s.key)}">${esc(s.label)}</button>`).join("")}</div>
      </section>`}

      <section class="sec"><h2>${d.added_by ? "Details" : "What they sent"}</h2>
        <dl class="kv">${rows.map(([k, val, raw]) => `<dt>${esc(k)}</dt><dd>${val ? (raw ? val : esc(val)) : '<span class="muted">—</span>'}</dd>`).join("")}</dl>
        <h3>Tell us more</h3>
        ${d.more.length ? `<dl class="kv">${d.more.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val)}</dd>`).join("")}</dl>`
          : `<p class="muted small">They didn't answer the extra questions.</p>`}
      </section>

      ${d.spam ? "" : `<section class="sec"><h2>Measures and quotes</h2>
        ${d.measures.length ? d.measures.map(m => `<div class="note1"><b>${esc(m.receipt)}</b> <span class="muted small">measured by ${esc(m.by || "?")} · ${esc(when(m.submitted_at))}</span></div>`).join("")
          : '<p class="muted small">No measure yet. On the Measure form, use “Or pick a lead” and choose this customer.</p>'}
        ${d.quotes.length ? d.quotes.map(q => `<div class="note1"><b>Quote ${esc(q.ref)}</b> <span class="muted small">${esc(q.by)} in Studio · ${esc(when(q.at))}</span></div>`).join("")
          : '<p class="muted small">No quote from Studio yet.</p>'}
        <h3>Service Fusion</h3>
        <p class="muted small">Nothing is sent to Service Fusion from here. Copy the details, make the customer and job there, then type the job number below.</p>
        <button type="button" class="btn secondary" id="sfCopy">Copy details for Service Fusion</button>
        <div class="arow" style="margin-top:10px"><input id="sfJob" type="text" inputmode="numeric" maxlength="20" placeholder="Service Fusion job #" value="${esc(d.sf_job)}">
          <button type="button" class="mini primary" id="sfSave">Save</button></div>
      </section>`}

      ${d.files.length || d.files_not_saved ? `<section class="sec"><h2>Photos and files</h2>
        <div class="lphotos">${d.files.filter(f => f.kind === "photo").map(f => `<a href="api/leads/files/${f.id}" target="_blank" rel="noopener">
          <img src="api/leads/files/${f.id}" alt="${esc(f.name)}" loading="lazy"></a>`).join("")}</div>
        ${d.files.filter(f => f.kind === "pdf").map(f => `<a class="pdfrow" href="api/leads/files/${f.id}">📄 ${esc(f.name)} <span class="muted small">download</span></a>`).join("")}
        ${d.files_not_saved ? `<p class="flag">${d.files_not_saved} file(s) weren't saved: the server was low on space.</p>` : ""}
      </section>` : ""}

      <section class="sec"><h2>Notes</h2>
        ${d.notes.length ? d.notes.map(n => `<div class="note1"><div class="muted small">${esc(n.by)} · ${esc(when(n.at))}</div><div class="ntext">${esc(n.text)}</div></div>`).join("")
          : '<p class="muted small">No notes yet.</p>'}
        <textarea id="noteText" rows="3" maxlength="2000" placeholder="e.g. Left a voicemail. Wants a quote for 2 sidelites too."></textarea>
        <button type="button" class="btn secondary" id="addNote">Add note</button>
      </section>

      <section class="sec"><h2>History</h2>
        <ul class="hist">${d.history.map(h => `<li><span class="muted small">${esc(when(h.at))}</span> <b>${esc(h.who)}</b>: ${esc(h.what)}</li>`).join("")}</ul>
        ${d.emails.length ? `<p class="muted small">Emails: ${d.emails.map(e => `${e.audience === "customer" ? "customer's receipt" : "office"} ${esc(e.status === "sent" ? "sent" : e.status === "failed" ? "failed" : "waiting")}`).join(" · ")}</p>` : ""}
      </section>
      ${d.is_test && me.is_owner ? `<button type="button" class="link danger" id="delTest">Delete this test lead</button>` : ""}`;
    window.scrollTo(0, 0);
    wire(d);
  }

  function wire(d) {
    $("#backBtn").onclick = () => { location.hash = ""; };
    const act = async (fn) => { try { render(await fn()); } catch (e) { fail(e); } };
    const ns = $("#notSpam");
    if (ns) ns.onclick = () => act(async () => { const r = await api(`api/leads/${d.id}/not-spam`, { method: "POST" }); toast("Moved to Leads. The office was emailed."); return r; });
    const cl = $("#claim");
    if (cl) cl.onclick = () => {
      if (d.owner && !confirm(`${d.owner} has this lead. Take it over?`)) return;
      act(() => d.owner ? api(`api/leads/${d.id}/assign`, { method: "POST", json: { staff_id: me.id } })
                        : api(`api/leads/${d.id}/claim`, { method: "POST" }));
    };
    const ab = $("#assignBtn");
    if (ab) ab.onclick = () => {
      const to = Number($("#assignTo").value);
      if (!to) return toast("Pick a person first.");
      act(async () => { const r = await api(`api/leads/${d.id}/assign`, { method: "POST", json: { staff_id: to } }); toast("Done"); return r; });
    };
    $$(".stb").forEach(b => b.onclick = () => {
      if (b.dataset.st === d.status) return;
      act(() => api(`api/leads/${d.id}/status`, { method: "PUT", json: { status: b.dataset.st } }));
    });
    $("#addNote").onclick = () => {
      const text = $("#noteText").value.trim();
      if (!text) return toast("Type the note first.");
      act(() => api(`api/leads/${d.id}/notes`, { method: "POST", json: { text } }));
    };
    const sc = $("#sfCopy");
    if (sc) sc.onclick = async () => {
      try { await navigator.clipboard.writeText(d.sf_copy); toast("Copied. Paste it into Service Fusion."); }
      catch (e) { prompt("Copy this:", d.sf_copy); }
    };
    const ss = $("#sfSave");
    if (ss) ss.onclick = () => act(async () => { const r = await api(`api/leads/${d.id}/sf-job`, { method: "PUT", json: { number: $("#sfJob").value.trim() } }); toast("Saved"); return r; });
    const dt = $("#delTest");
    if (dt) dt.onclick = async () => {
      if (!confirm("Delete this test lead, its files and its activity-log lines?")) return;
      try { await api(`api/leads/${d.id}`, { method: "DELETE" }); toast("Deleted"); location.hash = ""; } catch (e) { fail(e); }
    };
  }

  // ------------------------------------------------------------ start
  (async () => {
    try { me = await api("api/me"); }
    catch (e) {
      if (e.status === 401) {
        // come straight back here (e.g. to the lead from an email) after signing in
        try { localStorage.setItem("sdops_next", JSON.stringify({ to: "leads" + location.hash, at: Date.now() })); } catch (er) { /* */ }
        return gate("Sign in on the staff app first. You'll come right back here.");
      }
      return gate(e.message);
    }
    if (!me.leads) return gate("Leads isn't switched on for you. Ask Adem or Paz.");
    $("#who").textContent = me.name;
    $("#ui").classList.remove("hidden");
    $("#gate").classList.add("hidden");
    testBox();
    route();
  })();
})();

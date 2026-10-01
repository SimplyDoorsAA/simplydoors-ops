/* SimplyDoors Operations — admin screen (Adem and Paz). */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TZ = "America/Chicago";
  const when = (iso) => iso ? new Date(iso).toLocaleString([], { timeZone: TZ, month: "short", day: "numeric",
    year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" }) : "—";

  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts,
      headers: { "X-SD-App": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}) },
      body: opts.json ? JSON.stringify(opts.json) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    if (!r.ok) { const err = new Error((data && data.detail) || `Error ${r.status}`); err.status = r.status; throw err; }
    return data;
  }
  function toast(msg) {
    const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t); setTimeout(() => t.remove(), 3200);
  }
  const fail = (e) => toast(e.message || "Something went wrong");

  // ------------------------------------------------------------ plain-English activity names
  const ACTIONS = {
    login_ok: "Signed in", login_fail: "Wrong PIN", login_fail_no_pin: "Tried to sign in (no PIN set)",
    login_fail_unknown_name: "Sign-in with unknown name", login_while_locked: "Tried to sign in while locked",
    login_blocked_ip: "Blocked: too many wrong PINs from one connection", account_locked: "Account locked",
    account_unlocked: "Account unlocked", logout: "Signed out", report_submitted: "Submitted a report",
    report_viewed: "Opened a report", report_pdf_downloaded: "Downloaded a report PDF", email_sent: "Email sent",
    email_failed: "Email failed", email_skipped_no_recipients: "Email skipped (nobody on the list)",
    email_resend_requested: "Asked to resend email", email_retry_requested: "Asked to retry email",
    staff_added: "Added a person", staff_changed: "Changed a person", pin_reset: "Reset a PIN",
    pin_set_console: "PIN set on the server", pins_imported: "PINs imported", email_rule_changed: "Changed an email list",
    admin_denied: "Blocked from admin page", audit_exported: "Exported the activity log",
    audit_viewed: "Looked at the activity log", photo_viewed: "Opened a photo",
    app_started: "App started", staff_seeded: "Staff list created",
  };

  // ------------------------------------------------------------ tabs
  function tab(name) {
    $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    $$("[data-panel]").forEach(p => p.classList.toggle("hidden", p.dataset.panel !== name));
    ({ reports: loadReports, log: () => loadLog(true), staff: loadStaff, rules: loadRules, status: loadStatus })[name]();
  }
  $$(".tabs button").forEach(b => b.onclick = () => tab(b.dataset.tab));

  // ------------------------------------------------------------ reports
  async function loadReports() {
    const q = encodeURIComponent($("#repQ").value.trim()), f = encodeURIComponent($("#repForm").value);
    try {
      const rows = await api(`api/admin/reports?q=${q}&form=${f}`);
      $("#repList").innerHTML = rows.length ? `<table class="rows"><thead><tr><th>Receipt</th><th>Form</th><th>From</th>
        <th class="hide-sm">Details</th><th>Received</th><th>Email</th></tr></thead><tbody>` +
        rows.map(r => `<tr class="click" data-id="${r.id}"><td><b>${esc(r.receipt)}</b></td><td>${esc(r.form_type)}</td>
          <td>${esc(r.staff_name)}</td><td class="hide-sm">${esc(r.summary)}</td><td>${esc(when(r.submitted_at))}${r.queued_on_phone ? ' <span class="badge warn">sent late</span>' : ""}${r.no_geo ? ` <span class="badge warn">${r.no_geo} photo${r.no_geo > 1 ? "s" : ""} without location</span>` : ""}</td>
          <td>${emailBadge(r.email_status)}</td></tr>`).join("") + `</tbody></table>`
        : `<p class="muted">No reports yet.</p>`;
      $$("#repList tr.click").forEach(tr => tr.onclick = () => openReport(tr.dataset.id));
    } catch (e) { fail(e); }
  }
  const emailBadge = (s) => s === "sent" ? '<span class="badge ok">sent</span>' : s === "failed" ? '<span class="badge bad">failed</span>'
    : s === "pending" ? '<span class="badge warn">waiting</span>' : '<span class="badge">none</span>';
  let repTimer; $("#repQ").oninput = () => { clearTimeout(repTimer); repTimer = setTimeout(loadReports, 300); };
  $("#repForm").onchange = loadReports;

  async function openReport(id) {
    try {
      const r = await api(`api/admin/reports/${id}`);
      sheet(`<h2>${esc(r.form_type)} · ${esc(r.receipt)}</h2>
        <p class="muted small">From ${esc(r.staff_name)} · received ${esc(when(r.submitted_at))}
        ${r.started_at ? ` · form opened ${esc(when(r.started_at))} (phone clock)` : ""}${r.queued_on_phone ? " · sent from the phone's offline queue" : ""}</p>
        <div class="actions"><a class="mini" href="api/admin/reports/${r.id}/pdf" target="_blank" rel="noopener">Open PDF</a>
        <button class="mini" id="resend" type="button">Email it again</button></div>
        <table class="kv">${r.rows.map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join("")}</table>
        <h3>Photos (${r.photos.length})</h3>
        <div class="gallery">${r.photos.map(p => `<div><a href="api/admin/photos/${p.id}" target="_blank" rel="noopener"><img loading="lazy" src="api/admin/photos/${p.id}" alt=""></a>
          <span><b>${esc(p.label)}</b>${p.located ? "" : ' <span class="badge warn">no location</span>'}</span>
          ${p.lines.map(l => `<span>${esc(l)}</span>`).join("")}
          ${p.map ? `<a class="maplink" href="${esc(p.map)}" target="_blank" rel="noopener">View on map</a>` : ""}</div>`).join("") || '<p class="muted">None</p>'}</div>
        <h3>Emails</h3>
        ${r.emails.map(e => `<div class="rule"><b>${emailBadge(e.status)}</b> ${esc(e.subject)}<div class="det">To: ${esc(e.recipients)}
          ${e.sent_at ? `<br>Sent ${esc(when(e.sent_at))}` : ""}${e.last_error ? `<br>Last error: ${esc(e.last_error)}` : ""}</div>
          ${e.status !== "sent" ? `<button class="mini" data-retry="${e.id}" type="button">Try sending now</button>` : ""}</div>`).join("") || '<p class="muted">No email (nobody on the list).</p>'}`);
      $("#resend").onclick = async () => {
        if (!confirm("Send this report's email again to everyone on the list?")) return;
        try { await api(`api/admin/reports/${r.id}/resend`, { method: "POST" }); toast("Queued to send again"); openReport(id); } catch (e) { fail(e); }
      };
      $$("[data-retry]").forEach(b => b.onclick = async () => {
        try { await api(`api/admin/emails/${b.dataset.retry}/retry`, { method: "POST" }); toast("Trying again"); setTimeout(() => openReport(id), 2500); } catch (e) { fail(e); }
      });
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ activity log
  let logOffset = 0;
  function logQuery() {
    const p = new URLSearchParams();
    if ($("#logPerson").value.trim()) p.set("person", $("#logPerson").value.trim());
    if ($("#logAction").value) p.set("action", $("#logAction").value);
    if ($("#logFrom").value) p.set("from", $("#logFrom").value);
    if ($("#logTo").value) p.set("to", $("#logTo").value);
    return p;
  }
  async function loadLog(reset) {
    if (reset) { logOffset = 0; $("#logList").innerHTML = ""; }
    const p = logQuery(); p.set("limit", 200); p.set("offset", logOffset);
    try {
      const d = await api(`api/admin/audit?${p}`);
      logOffset += d.rows.length;
      $("#logCount").textContent = `${d.total} entr${d.total === 1 ? "y" : "ies"}`;
      const html = d.rows.map(r => `<tr><td>${esc(when(r.at))}</td><td>${esc(r.actor_name || "—")}</td>
        <td><b>${esc(ACTIONS[r.action] || r.action)}</b>${r.target ? `<div class="det">${esc(r.target)}</div>` : ""}</td>
        <td class="hide-sm det">${esc(detail(r.details))}${r.ip ? `<br>${esc(r.ip)} · ${esc(device(r.user_agent))}` : ""}</td></tr>`).join("");
      if (reset) $("#logList").innerHTML = `<table class="rows"><thead><tr><th>When</th><th>Who</th><th>What</th><th class="hide-sm">Details</th></tr></thead><tbody>${html}</tbody></table>`;
      else $("#logList tbody").insertAdjacentHTML("beforeend", html);
      $("#logMore").classList.toggle("hidden", logOffset >= d.total);
    } catch (e) { fail(e); }
  }
  function detail(j) {
    if (!j) return "";
    try { const o = JSON.parse(j); return Object.entries(o).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "); }
    catch (e) { return j; }
  }
  function device(u) {
    if (!u) return "";
    if (/iPhone/.test(u)) return "iPhone"; if (/iPad/.test(u)) return "iPad"; if (/Android/.test(u)) return "Android";
    if (/Windows/.test(u)) return "Windows"; if (/Mac OS/.test(u)) return "Mac"; if (/Linux/.test(u)) return "Linux"; return u.slice(0, 40);
  }
  Object.entries(ACTIONS).forEach(([k, v]) => $("#logAction").insertAdjacentHTML("beforeend", `<option value="${k}">${esc(v)}</option>`));
  ["#logPerson", "#logFrom", "#logTo", "#logAction"].forEach(s => $(s).addEventListener("change", () => loadLog(true)));
  $("#logMore").onclick = () => loadLog(false);
  $("#logCsv").onclick = () => { location.href = `api/admin/audit.csv?${logQuery()}`; };

  // ------------------------------------------------------------ staff
  async function loadStaff() {
    try {
      const rows = await api("api/admin/staff");
      $("#depts").innerHTML = [...new Set(rows.map(r => r.dept))].map(d => `<option>${esc(d)}</option>`).join("");
      $("#staffList").innerHTML = `<table class="rows"><thead><tr><th>Name</th><th>Dept</th><th class="hide-sm">Email</th><th>PIN</th><th></th></tr></thead><tbody>` +
        rows.map(r => `<tr${r.active ? "" : ' style="opacity:.5"'}><td><b>${esc(r.name)}</b>
          ${r.is_admin ? ' <span class="badge ok">admin</span>' : ""}${r.sales_notify ? ' <span class="badge">sales list</span>' : ""}
          ${r.active ? "" : ' <span class="badge">turned off</span>'}${r.locked ? ' <span class="badge bad">locked</span>' : ""}</td>
          <td>${esc(r.dept)}</td><td class="hide-sm">${esc(r.email)}</td>
          <td>${r.has_pin ? `<span class="badge ok">set</span><div class="det">${esc(r.pin_source || "")}</div>` : '<span class="badge bad">none</span>'}</td>
          <td><button class="mini" data-edit="${r.id}" type="button">Edit</button></td></tr>`).join("") + `</tbody></table>`;
      $$("[data-edit]").forEach(b => b.onclick = () => editStaff(rows.find(r => r.id == b.dataset.edit)));
    } catch (e) { fail(e); }
  }
  function editStaff(r) {
    sheet(`<h2>${esc(r.name)}</h2>
      <form id="editForm" class="grid">
        <label>Name<input name="name" value="${esc(r.name)}" required></label>
        <label>Department<input name="dept" list="depts" value="${esc(r.dept)}" required></label>
        <label>Work email<input name="email" type="email" value="${esc(r.email)}"></label>
        <label class="inline"><input name="sales_notify" type="checkbox" ${r.sales_notify ? "checked" : ""}> Shows in "Notify a sales rep"</label>
        <label class="inline"><input name="is_admin" type="checkbox" ${r.is_admin ? "checked" : ""}> Admin (sees everything)</label>
        <label class="inline"><input name="active" type="checkbox" ${r.active ? "checked" : ""}> Can sign in</label>
        <button class="mini primary" type="submit">Save changes</button>
      </form>
      <h3>PIN</h3>
      <form id="pinForm" class="grid"><label>New PIN (6–8 digits)<input name="pin" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="off" required></label>
        <button class="mini" type="submit">Set PIN</button></form>
      ${r.is_admin ? '<p class="det">Another admin\'s PIN and admin access can only be changed on the server console.</p>' : ""}
      ${r.locked ? `<p><button class="mini danger" id="unlock" type="button">Unlock account now</button></p>` : ""}`);
    $("#editForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const f = ev.target;
      const el = f.elements;
      const body = { name: el.name.value, dept: el.dept.value, email: el.email.value,
        sales_notify: el.sales_notify.checked, is_admin: el.is_admin.checked, active: el.active.checked };
      if (body.is_admin && !r.is_admin && !confirm(`Make ${r.name} an admin? Admins can see every report, including disciplinary records, and the full activity log.`)) return;
      try { await api(`api/admin/staff/${r.id}`, { method: "PATCH", json: body }); toast("Saved"); closeSheet(); loadStaff(); } catch (e) { fail(e); }
    };
    $("#pinForm").onsubmit = async (ev) => {
      ev.preventDefault();
      try { await api(`api/admin/staff/${r.id}/pin`, { method: "POST", json: { pin: ev.target.elements.pin.value.trim() } });
        toast(`PIN set for ${r.name}. Tell them in person.`); closeSheet(); loadStaff(); } catch (e) { fail(e); }
    };
    const u = $("#unlock"); if (u) u.onclick = async () => {
      try { await api(`api/admin/staff/${r.id}/unlock`, { method: "POST" }); toast("Unlocked"); closeSheet(); loadStaff(); } catch (e) { fail(e); }
    };
  }
  $("#addStaff").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    try {
      await api("api/admin/staff", { method: "POST", json: { name: f.elements.name.value, dept: f.elements.dept.value, email: f.elements.email.value,
        sales_notify: f.elements.sales_notify.checked, is_admin: f.elements.is_admin.checked } });
      toast("Added. Now set their PIN."); f.reset(); loadStaff();
    } catch (e) { fail(e); }
  };

  // ------------------------------------------------------------ email lists
  async function loadRules() {
    try {
      const rows = await api("api/admin/email-rules");
      $("#rulesList").innerHTML = rows.map((r, i) => `<div class="rule"><h3>${esc(r.form_type)} ${r.live ? '<span class="badge ok">live</span>' : '<span class="badge">coming later</span>'}</h3>
        <textarea data-form="${esc(r.form_type)}" aria-label="Recipients for ${esc(r.form_type)}">${esc(r.recipients)}</textarea>
        ${r.extra ? `<div class="det">${esc(r.extra)}</div>` : ""}<button class="mini primary" data-save="${i}" type="button">Save</button></div>`).join("");
      $$("#rulesList textarea").forEach(ta => { ta.style.height = "auto"; ta.style.height = (ta.scrollHeight + 4) + "px"; });
      $$("[data-save]").forEach(b => b.onclick = async () => {
        const ta = b.parentElement.querySelector("textarea");
        try { await api("api/admin/email-rules", { method: "PUT", json: { form_type: ta.dataset.form, recipients: ta.value } }); toast("Saved"); loadRules(); }
        catch (e) { fail(e); }
      });
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ status
  async function loadStatus() {
    try {
      const s = await api("api/admin/status");
      $("#statusBox").innerHTML = `
        ${s.email_configured ? "" : '<p class="error">Email sending is not set up yet. Reports are saved, and their emails are waiting until it is.</p>'}
        ${!s.offsite.configured ? '<p class="error">Off-site backup is not set up. Reports and photos exist only on the OptiPlex.</p>'
          : !s.offsite.ok ? `<p class="error">Last off-site backup failed (${esc(when(s.offsite.finished))}): ${esc(s.offsite.error || "")}</p>` : ""}
        ${s.staff_without_pin.length ? `<p class="error" style="background:var(--amber-bg);color:var(--amber)">No PIN yet: ${esc(s.staff_without_pin.join(", "))}</p>` : ""}
        <div class="stat">
          <div>Reports<b>${s.reports}</b></div>
          <div>Emails sent<b>${s.emails.sent}</b></div>
          <div>Emails waiting<b>${s.emails.pending}</b></div>
          <div>Emails failed<b>${s.emails.failed}</b></div>
          <div>Photos stored<b>${s.photos_mb} MB</b></div>
          <div>Free disk<b>${s.disk_free_gb} GB</b></div>
        </div>
        <p class="muted small">Sending from: ${esc(s.email_from || "not set")} · Phone alerts: ${s.alerts_configured ? "on" : "off"} ·
          Last database snapshot: ${esc(s.last_backup || "none yet")} ·
          Last off-site copy: ${s.offsite.configured ? esc(s.offsite.last_ok ? when(s.offsite.last_ok) : "never") + (s.offsite.dest ? ` to ${esc(s.offsite.dest)}` : "") : "not set up"} ·
          Version ${esc(s.version)}</p>
        <h3>Recent emails</h3>
        ${s.recent_emails.map(e => `<div class="rule">${emailBadge(e.status)} ${esc(e.subject)}<div class="det">To: ${esc(e.recipients)} · tries: ${e.attempts}
          ${e.last_error ? `<br>Error: ${esc(e.last_error)}` : ""}</div>
          ${e.status !== "sent" ? `<button class="mini" data-retry="${e.id}" type="button">Try sending now</button>` : ""}</div>`).join("") || '<p class="muted">None yet.</p>'}`;
      $$("#statusBox [data-retry]").forEach(b => b.onclick = async () => {
        try { await api(`api/admin/emails/${b.dataset.retry}/retry`, { method: "POST" }); toast("Trying again"); setTimeout(loadStatus, 2500); } catch (e) { fail(e); }
      });
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ sheet (pop-up)
  function sheet(html) { $("#sheetBody").innerHTML = html; $("#sheet").classList.remove("hidden"); }
  function closeSheet() { $("#sheet").classList.add("hidden"); $("#sheetBody").innerHTML = ""; }
  $(".sheet-close").onclick = closeSheet;
  $("#sheet").onclick = (e) => { if (e.target.id === "sheet") closeSheet(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

  // ------------------------------------------------------------ start
  (async () => {
    try {
      const me = await api("api/me");
      if (!me.is_admin) throw Object.assign(new Error("no"), { status: 403 });
      $("#who").textContent = me.name;
      me.forms.forEach(f => $("#repForm").insertAdjacentHTML("beforeend", `<option>${esc(f.type)}</option>`));
      $("#ui").classList.remove("hidden");
      tab("reports");
    } catch (e) { $("#gate").classList.remove("hidden"); }
  })();
})();

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
    invite_created: "Made a setup link", pin_created_by_staff: "Made their own PIN", setup_code_rejected: "Wrong or expired setup code",
    pin_set_console: "PIN set on the server", pins_imported: "PINs imported", email_rule_changed: "Changed an email list",
    admin_denied: "Blocked from admin page", audit_exported: "Exported the activity log",
    audit_viewed: "Looked at the activity log", photo_viewed: "Opened a photo",
    app_started: "App started", staff_seeded: "Staff list created",
    owner_set: "App owner set (server console)", private_copies_changed: "Changed their private copies",
    owner_copies_set_up: "Owner's address moved to private copies", test_data_reset: "Test data cleared (server console)", list_changed: "Changed a pick list", forms_switched: "Changed which forms staff see", measure_reopened: "Reopened a measure",
    test_mode_on: "Turned test mode on", test_mode_off: "Turned test mode off",
    log_lines_deleted: "Deleted own log lines", studio_opened: "Opened Simply Studio", test_report_deleted: "Deleted a test report",
  };

  // ------------------------------------------------------------ tabs
  function tab(name) {
    $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    $$("[data-panel]").forEach(p => p.classList.toggle("hidden", p.dataset.panel !== name));
    ({ reports: loadReports, log: () => loadLog(true), staff: loadStaff, rules: loadRules, lists: loadLists, status: loadStatus })[name]();
  }
  $$(".tabs button").forEach(b => b.onclick = () => tab(b.dataset.tab));

  // ------------------------------------------------------------ reports
  async function loadReports() {
    const q = encodeURIComponent($("#repQ").value.trim()), f = encodeURIComponent($("#repForm").value);
    try {
      const rows = await api(`api/admin/reports?q=${q}&form=${f}`);
      $("#repList").innerHTML = rows.length ? `<table class="rows"><thead><tr><th>Receipt</th><th>Form</th><th>From</th>
        <th class="hide-sm">Details</th><th>Received</th><th>Email</th></tr></thead><tbody>` +
        rows.map(r => `<tr class="click" data-id="${r.id}"><td><b>${esc(r.receipt)}</b>${r.is_test ? ' <span class="badge warn">TEST</span>' : ""}</td><td>${esc(r.form_type)}</td>
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
        <button class="mini" id="resend" type="button">Email it again</button>
        ${r.is_test && amOwner ? '<button class="mini danger" id="delTest" type="button">Delete test report</button>' : ""}</div>
        <table class="kv">${r.rows.map(([a, b]) => `<tr><td>${esc(a)}</td><td>${esc(b)}</td></tr>`).join("")}</table>
        <h3>Photos (${r.photos.length})</h3>
        <div class="gallery">${r.photos.map(p => `<div><a href="api/admin/photos/${p.id}" target="_blank" rel="noopener"><img loading="lazy" src="api/admin/photos/${p.id}" alt=""></a>
          <span><b>${esc(p.label)}</b>${p.signature || p.located ? "" : ' <span class="badge warn">no location</span>'}</span>
          ${p.signature ? "" : p.lines.map(l => `<span>${esc(l)}</span>`).join("")}
          ${p.map ? `<a class="maplink" href="${esc(p.map)}" target="_blank" rel="noopener">View on map</a>` : ""}</div>`).join("") || '<p class="muted">None</p>'}</div>
        <h3>Emails</h3>
        ${r.emails.map(e => `<div class="rule"><b>${emailBadge(e.status)}</b> ${esc(e.subject)}<div class="det">To: ${esc(e.recipients)}
          ${e.sent_at ? `<br>Sent ${esc(when(e.sent_at))}` : ""}${e.last_error ? `<br>Last error: ${esc(e.last_error)}` : ""}</div>
          ${e.status !== "sent" ? `<button class="mini" data-retry="${e.id}" type="button">Try sending now</button>` : ""}</div>`).join("") || '<p class="muted">No email (nobody on the list).</p>'}`);
      $("#resend").onclick = async () => {
        if (!confirm("Send this report's email again to everyone on the list?")) return;
        try { await api(`api/admin/reports/${r.id}/resend`, { method: "POST" }); toast("Queued to send again"); openReport(id); } catch (e) { fail(e); }
      };
      if ($("#delTest")) $("#delTest").onclick = async () => {
        if (!confirm(`Delete ${r.receipt}? Its photos, emails and log lines go too. This can't be undone.`)) return;
        try { await api(`api/admin/reports/${r.id}`, { method: "DELETE" }); toast(`${r.receipt} deleted`); closeSheet(); loadReports(); }
        catch (e) { fail(e); }
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
      logCanDelete = !!d.can_delete;
      const box = (r) => logCanDelete ? `<td class="ck">${r.deletable ? `<input type="checkbox" data-del="${r.id}" aria-label="Select line">` : ""}</td>` : "";
      const html = d.rows.map(r => `<tr>${box(r)}<td>${esc(when(r.at))}</td><td>${esc(r.actor_name || "—")}</td>
        <td><b>${esc(ACTIONS[r.action] || r.action)}</b>${r.target ? `<div class="det">${esc(r.target)}</div>` : ""}</td>
        <td class="hide-sm det">${esc(detail(r.details))}${r.ip ? `<br>${esc(r.ip)} · ${esc(device(r.user_agent))}` : ""}</td></tr>`).join("");
      if (reset) $("#logList").innerHTML = `<table class="rows"><thead><tr>${logCanDelete ? "<th></th>" : ""}<th>When</th><th>Who</th><th>What</th><th class="hide-sm">Details</th></tr></thead><tbody>${html}</tbody></table>`;
      else $("#logList tbody").insertAdjacentHTML("beforeend", html);
      $("#logMore").classList.toggle("hidden", logOffset >= d.total);
      $("#logSelAll").classList.toggle("hidden", !logCanDelete);
      $("#logDel").classList.toggle("hidden", !logCanDelete);
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
  let logCanDelete = false;
  $("#logSelAll").onclick = () => { const bs = $$("#logList [data-del]"); const on = bs.some(b => !b.checked); bs.forEach(b => b.checked = on); };
  $("#logDel").onclick = async () => {
    const ids = $$("#logList [data-del]:checked").map(b => +b.dataset.del);
    if (!ids.length) return toast("Tick the lines to delete first (only your own routine lines have a box).");
    if (!confirm(`Delete ${ids.length} line${ids.length > 1 ? "s" : ""} from the activity log? A note that you did this stays in the log.`)) return;
    try { const r = await api("api/admin/audit/delete", { method: "POST", json: { ids } }); toast(`Deleted ${r.deleted}`); loadLog(true); }
    catch (e) { fail(e); }
  };
  $("#logCsv").onclick = () => { location.href = `api/admin/audit.csv?${logQuery()}`; };

  // ------------------------------------------------------------ staff
  async function loadStaff() {
    try {
      const rows = await api("api/admin/staff");
      $("#depts").innerHTML = [...new Set(rows.map(r => r.dept))].map(d => `<option>${esc(d)}</option>`).join("");
      $("#staffList").innerHTML = `<table class="rows"><thead><tr><th>Name</th><th class="hide-sm">Dept</th><th class="hide-sm">Email</th><th>PIN</th><th></th></tr></thead><tbody>` +
        rows.map(r => `<tr${r.active ? "" : ' style="opacity:.5"'}><td><b>${esc(r.name)}</b>
          ${r.is_owner ? ' <span class="badge ok">owner</span>' : r.is_admin ? ' <span class="badge ok">admin</span>' : ""}${r.sales_notify ? ' <span class="badge">sales list</span>' : ""}
          ${r.active ? "" : ' <span class="badge">turned off</span>'}${r.locked ? ' <span class="badge bad">locked</span>' : ""}</td>
          <td class="hide-sm">${esc(r.dept)}</td><td class="hide-sm">${esc(r.email)}</td>
          <td>${r.has_pin ? `<span class="badge ok">set</span><div class="det">${esc(PIN_SRC[r.pin_source] || r.pin_source || "")}</div>` : '<span class="badge bad">none</span>'}
            ${r.invite === "waiting" ? `<div class="det">setup link sent, expires ${esc(shortDate(r.invite_expires))}</div>`
              : r.invite === "expired" ? '<div class="det">setup link expired</div>' : ""}</td>
          <td class="btns">${r.active && (!r.is_admin || r.id === myId || (amOwner && !r.is_owner))
              ? `<button class="mini primary" data-invite="${r.id}" type="button">${r.has_pin ? "New link" : "Invite"}</button>` : ""}
            <button class="mini" data-edit="${r.id}" type="button">Edit</button></td></tr>`).join("") + `</tbody></table>`;
      $$("[data-invite]").forEach(b => b.onclick = () => invite(rows.find(r => r.id == b.dataset.invite)));
      $$("[data-edit]").forEach(b => b.onclick = () => editStaff(rows.find(r => r.id == b.dataset.edit)));
    } catch (e) { fail(e); }
  }
  const PIN_SRC = { self: "made by them", admin: "set by admin", install: "set on server", import: "imported" };
  const shortDate = (iso) => iso ? new Date(iso).toLocaleDateString([], { timeZone: TZ, month: "short", day: "numeric" }) : "";
  let myId = null, amOwner = false;

  async function invite(r) {
    const msg = r.has_pin
      ? `Make a new setup link for ${r.name}? Their current PIN keeps working until they use the link to make a new one.`
      : `Make a setup link for ${r.name}?`;
    if (!confirm(msg)) return;
    let d;
    try { d = await api(`api/admin/staff/${r.id}/invite`, { method: "POST" }); } catch (e) { return fail(e); }
    const link = new URL("./", document.baseURI).href + "#setup=" + d.code.replace("-", "");
    const first = d.name.split(" ")[0];
    const text = `Hi ${first}, here's your link to set up the SimplyDoors app: ${link}\n` +
      `Open it on your phone, make your PIN, and follow the steps. It works once and expires ${shortDate(d.expires_at)}. ` +
      `If the link doesn't open, go to ${new URL("./", document.baseURI).href} and use setup code ${d.code}.`;
    sheet(`<h2>Setup link for ${esc(d.name)}</h2>
      <p class="muted small">Works once, only for ${esc(first)}, until ${esc(shortDate(d.expires_at))}. Making a new one cancels this one.</p>
      <div class="codebig">${esc(d.code)}</div>
      <div class="actions">
        <a class="mini primary" id="smsIt" href="sms:?&body=${encodeURIComponent(text)}">Text it</a>
        <button class="mini" id="copyMsg" type="button">Copy message</button>
        <button class="mini" id="copyLink" type="button">Copy link only</button>
      </div>
      <p class="det">In person? Read them the code. They tap “Use a setup code” on the sign-in screen.</p>`);
    const copy = async (t) => {
      try { await navigator.clipboard.writeText(t); toast("Copied"); }
      catch (e) { $("#sheetBody").insertAdjacentHTML("beforeend", `<textarea readonly style="width:100%;min-height:120px">${esc(t)}</textarea>`); toast("Select the text below and copy it"); }
    };
    $("#copyMsg").onclick = () => copy(text);
    $("#copyLink").onclick = () => copy(link);
    loadStaff();
  }

  function editStaff(r) {
    sheet(`<h2>${esc(r.name)}</h2>
      <form id="editForm" class="grid">
        <label>Name<input name="name" value="${esc(r.name)}" required></label>
        <label>Department<input name="dept" list="depts" value="${esc(r.dept)}" required></label>
        <label>Work email<input name="email" type="email" value="${esc(r.email)}"></label>
        <label class="inline"><input name="sales_notify" type="checkbox" ${r.sales_notify ? "checked" : ""}> Shows in "Notify a sales rep"</label>
        <label class="inline"><input name="studio_link" type="checkbox" ${r.studio ? "checked" : ""}> Shows the Simply Studio tile</label>
        <label class="inline"><input name="is_admin" type="checkbox" ${r.is_admin ? "checked" : ""}> Admin (sees everything)</label>
        <label class="inline"><input name="active" type="checkbox" ${r.active ? "checked" : ""}> Can sign in</label>
        <button class="mini primary" type="submit">Save changes</button>
      </form>
      <h3>PIN</h3>
      <form id="pinForm" class="grid"><label>New PIN (6–8 digits)<input name="pin" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="off" required></label>
        <button class="mini" type="submit">Set PIN</button></form>
      ${r.id === myId ? "" : r.is_owner && !amOwner ? '<p class="det">Only the app owner can change this account.</p>'
        : r.is_admin && !amOwner ? '<p class="det">Another admin\'s PIN and admin access can only be changed by the app owner.</p>' : ""}
      ${r.locked ? `<p><button class="mini danger" id="unlock" type="button">Unlock account now</button></p>` : ""}`);
    $("#editForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const f = ev.target;
      const el = f.elements;
      const body = { name: el.name.value, dept: el.dept.value, ...(el.email ? { email: el.email.value } : {}),
        sales_notify: el.sales_notify.checked, is_admin: el.is_admin.checked, active: el.active.checked,
        ...(el.studio_link.checked !== !!r.studio ? { studio_link: el.studio_link.checked } : {}) };
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
      toast("Added. Now tap Invite to send them a setup link."); f.reset(); loadStaff();
    } catch (e) { fail(e); }
  };

  // ------------------------------------------------------------ email lists
  async function loadRules() {
    try {
      const rows = await api("api/admin/email-rules");
      let mine = "";
      if (amOwner) {
        const m = await api("api/admin/my-copies");
        mine = `<div class="rule mine"><h3>My private copies <span class="badge">only you see this</span></h3>
          <p class="det">You get a copy of the reports ticked here. Copies go out as BCC, so your address (${esc(m.email)})
          isn't shown to anyone, not in the email and not on these lists. If you type your address into a list below, it will show there.</p>
          <div id="myCopies">${m.forms.map(f => `<label class="switch"><input type="checkbox" value="${esc(f.type)}" ${f.on ? "checked" : ""}><span>${esc(f.type)}</span></label>`).join("")}</div>
          <button class="mini primary" id="saveMine" type="button">Save my copies</button></div>`;
      }
      $("#rulesList").innerHTML = mine + rows.map((r, i) => `<div class="rule"><h3>${esc(r.form_type)} ${r.live ? '<span class="badge ok">live</span>' : '<span class="badge">coming later</span>'}</h3>
        <textarea data-form="${esc(r.form_type)}" aria-label="Recipients for ${esc(r.form_type)}">${esc(r.recipients)}</textarea>
        ${r.extra ? `<div class="det">${esc(r.extra)}</div>` : ""}<button class="mini primary" data-save="${i}" type="button">Save</button></div>`).join("");
      $$("#rulesList textarea").forEach(ta => { ta.style.height = "auto"; ta.style.height = (ta.scrollHeight + 4) + "px"; });
      const sm = $("#saveMine");
      if (sm) sm.onclick = async () => {
        const forms = $$("#myCopies input:checked").map(i => i.value);
        try { await api("api/admin/my-copies", { method: "PUT", json: { forms } }); toast("Saved. Only you can see this."); } catch (e) { fail(e); }
      };
      $$("[data-save]").forEach(b => b.onclick = async () => {
        const ta = b.parentElement.querySelector("textarea");
        try { await api("api/admin/email-rules", { method: "PUT", json: { form_type: ta.dataset.form, recipients: ta.value } }); toast("Saved"); loadRules(); }
        catch (e) { fail(e); }
      });
    } catch (e) { fail(e); }
  }

  // ------------------------------------------------------------ forms on/off + pick lists
  async function loadLists() {
    try {
      const d = await api("api/admin/lists");
      $("#formsSwitches").innerHTML = d.forms.map(f => `<label class="switch"><input type="checkbox" value="${esc(f.type)}"
          ${f.enabled && !f.admin_only ? "checked" : ""}${f.admin_only ? " disabled" : ""}>
          <span>${esc(f.type)}${f.admin_only ? ' <span class="badge">admins only</span>' : ""}</span></label>`).join("");
      $("#pickLists").innerHTML = d.lists.map(l => `<div class="rule"><h3>${esc(l.label)}</h3>
          <textarea data-list="${esc(l.name)}" aria-label="${esc(l.label)}, one per line">${esc(l.values.join("\n"))}</textarea>
          <button class="mini primary" data-savelist="${esc(l.name)}" type="button">Save</button></div>`).join("");
      $$("#pickLists textarea").forEach(ta => { ta.style.height = "auto"; ta.style.height = (ta.scrollHeight + 4) + "px"; });
      $$("[data-savelist]").forEach(b => b.onclick = async () => {
        const ta = $(`textarea[data-list="${b.dataset.savelist}"]`);
        const values = ta.value.split("\n").map(v => v.trim()).filter(Boolean);
        try { await api(`api/admin/lists/${b.dataset.savelist}`, { method: "PUT", json: { values } }); toast("Saved. Phones pick it up next time a form opens."); loadLists(); }
        catch (e) { fail(e); }
      });
    } catch (e) { fail(e); }
  }
  $("#saveForms").onclick = async () => {
    const forms = $$("#formsSwitches input:checked").map(i => i.value);
    if (!confirm(forms.length ? `Staff will see: ${forms.join(", ")}. Save?` : "Staff will see no forms at all. Save?")) return;
    try { await api("api/admin/forms-enabled", { method: "PUT", json: { forms } }); toast("Saved"); loadLists(); } catch (e) { fail(e); }
  };

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
      myId = me.id; amOwner = !!me.is_owner;
      me.forms.forEach(f => $("#repForm").insertAdjacentHTML("beforeend", `<option>${esc(f.type)}</option>`));
      $("#ui").classList.remove("hidden");
      tab("reports");
    } catch (e) { $("#gate").classList.remove("hidden"); }
  })();
})();

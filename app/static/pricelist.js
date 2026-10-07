/* SimplyDoors Operations — Price List (beta): vendor net costs, a buy list, and purchase orders.
   Prices come from the sheets an admin loaded; a PO's prices are worked out again on the server when it's sent. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => n == null ? "—" : "$" + Number(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");

  async function api(path, opts = {}) {
    let r;
    try {
      r = await fetch(path, { credentials: "same-origin", ...opts,
        headers: { "X-SD-App": "1", ...(opts.json ? { "Content-Type": "application/json" } : {}) },
        body: opts.json ? JSON.stringify(opts.json) : undefined });
    } catch (e) { const err = new Error("No signal right now. Try again in a moment."); err.status = 0; throw err; }
    let data = null; try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) { const err = new Error((data && data.detail) || `Error ${r.status}`); err.status = r.status; throw err; }
    return data;
  }

  const CATS = ["Interior molded", "Interior flush", "Interior stile & rail", "Interior bifolds", "Exterior doors & sidelites",
    "Exterior glass & lites", "Parts & hardware"];
  const CAT_ICON = { "Interior molded": "🚪", "Interior flush": "▭", "Interior stile & rail": "🪵", "Interior bifolds": "📂",
    "Exterior doors & sidelites": "🏠", "Exterior glass & lites": "🪟", "Parts & hardware": "🔩" };
  const NONSTOCK_CATS = ["Interior molded", "Interior flush", "Interior bifolds"];
  const OUR = { name: "SimplyDoors", addr: ["17750 Lookout Rd, Unit 150", "Schertz, TX 78154", "(210) 903-8450", "admin@simplydoors.com"] };
  const ADMIN_COPY = "admin@simplydoors.com";

  let me = null, VENDORS = [];
  const ITEMS = {};           // vendor code -> items from the live sheet
  const BYID = new Map();     // item id -> item (with .v = vendor)
  const S = { q: "", vendor: null, cat: "All", grp: "", stocked: false, flagged: false, core: "", height: 0, width: 0,
    sort: "rel", shown: 40, list: {}, po: null };

  // ------------------------------------------------------------ per-person storage on this device
  const key = (k) => `sdops_pl_${me.id}_${k}`;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(key(k)); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(key(k), JSON.stringify(v)); } catch (e) { /* private mode */ } },
    del(k) { try { localStorage.removeItem(key(k)); } catch (e) { /* */ } },
  };
  // buy list: { vendor: { itemId: qty } }
  const saveList = () => store.set("list", S.list);

  // ------------------------------------------------------------ sizes + search
  const ftin = (inches) => `${Math.floor(inches / 12)}/${Math.round(inches % 12)}`;
  const sizeCode = (w, h) => (!w || !h) ? "" : `${Math.floor(w / 12)}${Math.round(w % 12)}${Math.floor(h / 12)}${Math.round(h % 12)}`;
  function sizeLabel(d) {
    if (!d.w || !d.h) return "";
    const hh = d.h === 80 ? "6'8\"" : d.h === 96 ? "8'0\"" : d.h === 84 ? "7'0\"" : ftin(d.h).replace("/", "'") + '"';
    return `${ftin(d.w)} × ${hh} (${d.w}×${d.h})`;
  }
  function norm(s) {
    return String(s).toLowerCase()
      .replace(/(\d)\s*(pnl|panel|pn|p)\b/g, "$1panel").replace(/\b(lite|lt)\b/g, "lite")
      .replace(/\bhollow\b/g, "hc").replace(/\bsolid\b/g, "sc").replace(/smooth[- ]star/g, "smoothstar")
      .replace(/fiber[- ]classic/g, "fiberclassic").replace(/classic[- ]craft/g, "classiccraft");
  }
  function prep(d, v) {
    d.v = v;
    const extra = [sizeCode(d.w, d.h), d.w ? ftin(d.w) : "", d.w ? `${d.w}x${d.h}` : "",
      d.h === 80 ? "6/8 68" : d.h === 96 ? "8/0 80" : "", d.brand || "", d.uom || ""];
    d._hay = norm([d.name, d.sku, d.mfr, d.grp, d.cat, d.core, d.th, ...extra].join(" "));
    d._words = new Set(d._hay.split(/[^a-z0-9/]+/));
    BYID.set(d.id, d);
  }
  const tokOk = (d, t) => /^[a-z]{1,3}$/.test(t) ? d._words.has(t) : d._hay.includes(t);
  const toks = () => norm(S.q).split(/\s+/).filter(Boolean);
  const pool = () => S.vendor ? (ITEMS[S.vendor] || []) : Object.values(ITEMS).flat();
  function matches(d, ts) {
    if (S.cat !== "All" && d.cat !== S.cat) return false;
    if (S.grp && d.grp !== S.grp) return false;
    if (S.stocked && d.stock !== true) return false;
    if (S.flagged && !d.flag) return false;
    if (S.core && !(d.core || "").toUpperCase().startsWith(S.core)) return false;
    if (S.height && d.h !== S.height) return false;
    if (S.width && d.w !== S.width) return false;
    return ts.every(t => tokOk(d, t));
  }
  function search() {
    const ts = toks();
    const res = pool().filter(d => matches(d, ts));
    if (S.sort === "price") res.sort((a, b) => (a.price ?? 1e9) - (b.price ?? 1e9));
    else if (S.sort === "size") res.sort((a, b) => (a.h - b.h) || (a.w - b.w) || ((a.price ?? 0) - (b.price ?? 0)));
    else if (ts.length) {
      const q = norm(S.q).replace(/\s+/g, "");
      res.sort((a, b) => (b.sku.toLowerCase() === q) - (a.sku.toLowerCase() === q) || (b.stock === true) - (a.stock === true));
    }
    return res;
  }
  const vname = (code) => (VENDORS.find(v => v.code === code) || { name: code }).name;

  // ------------------------------------------------------------ shop: vendor -> category -> items
  function tags(d) {
    const t = [`<span class="tag v">${esc(vname(d.v))}${d.brand ? " · " + esc(d.brand) : ""}</span>`];
    if (d.price == null) t.push('<span class="tag cfp">Call for price</span>');
    if (d.stock === true) t.push('<span class="tag stock">✓ Stocked</span>');
    if (d.stock === false && NONSTOCK_CATS.includes(d.cat)) t.push('<span class="tag ns">Non-stock · +30% under 10</span>');
    if (d.flag) t.push('<span class="tag flag">⚠ Check</span>');
    if (d.uom) t.push(`<span class="tag v">per ${esc(d.uom)}</span>`);
    return t.join("");
  }
  function card(d) {
    const sub = [sizeLabel(d), d.core, d.th, d.hand].filter(Boolean).join(" · ");
    return `<button type="button" class="item" data-id="${d.id}">
      <div class="main"><div class="t">${esc(d.name)}</div><div class="s">${esc(sub || d.grp)}</div>
        <div class="s mono">${esc(d.sku)}${d.mfr ? " · " + esc(d.mfr) : ""}</div><div class="meta">${tags(d)}</div></div>
      <div class="price"><div class="p">${d.price == null ? "—" : money(d.price)}</div>
        <div class="pl">${/Parts|glass/i.test(d.cat) ? "net" : "slab net"}</div></div></button>`;
  }
  const resetFilters = () => { S.stocked = false; S.flagged = false; S.core = ""; S.height = 0; S.width = 0; };
  const filtersOn = () => S.stocked || S.flagged || S.core || S.height || S.width;

  function renderCrumbs() {
    const c = [];
    const at = (label, act) => c.push(`<button type="button" data-crumb="${act}">${esc(label)}</button><span class="sep">›</span>`);
    if (S.vendor || S.q) at("All vendors", "home");
    if (S.vendor && (S.cat !== "All" || S.q)) at(vname(S.vendor), "vendor");
    if (S.cat !== "All" && S.q) at(S.cat, "cat");
    const cur = S.q ? `Results for “${S.q}”` : S.cat !== "All" ? S.cat : S.vendor ? vname(S.vendor) : "";
    $("#crumbs").innerHTML = c.join("") + (cur ? `<b>${esc(cur)}</b>` : "");
  }
  function renderBrowse() {
    if (!S.vendor) {
      $("#browse").innerHTML = "<h1>Pick a vendor</h1><div class=\"tiles\">" + VENDORS.map(v => {
        const live = !!v.sheet;
        return `<button type="button" class="tile2 ${live ? "live" : "off"}" ${live ? `data-vendor="${esc(v.code)}"` : "disabled"}>
          <div class="tt">${esc(v.name)} ${live ? '<span class="tag stock">Live</span>' : '<span class="tag ns">No sheet yet</span>'}</div>
          <div class="ts">${live ? `${v.sheet.items.toLocaleString()} items · ${esc(v.sheet.label)}` : "Waiting for a price sheet"}</div></button>`;
      }).join("") + "</div>";
      return;
    }
    const items = ITEMS[S.vendor] || [];
    const v = VENDORS.find(x => x.code === S.vendor);
    $("#browse").innerHTML = `<h1>${esc(v.name)}</h1><div class="muted small">${esc(v.sheet ? v.sheet.label : "")} · pick a category</div><div class="tiles">` +
      CATS.map(c => {
        const it = items.filter(d => d.cat === c); if (!it.length) return "";
        const pr = it.map(d => d.price).filter(p => p != null);
        const st = it.filter(d => d.stock === true).length;
        return `<button type="button" class="tile2" data-cat="${esc(c)}"><div class="tt">${esc(c)}<span class="ic2">${CAT_ICON[c] || ""}</span></div>
          <div class="ts">${it.length} items${st ? ` · ${st} stocked` : ""}<br>${pr.length ? money(Math.min(...pr)) + " – " + money(Math.max(...pr)) : ""}</div></button>`;
      }).join("") + "</div>";
  }
  function renderChips() {
    const ts = toks();
    if (S.cat !== "All") {
      const inCat = pool().filter(d => d.cat === S.cat && ts.every(t => tokOk(d, t)));
      const grps = [...new Set(inCat.map(d => d.grp))];
      $("#grps").innerHTML = grps.length > 1 ? `<button type="button" class="chip ${!S.grp ? "on" : ""}" data-grp="">All types<span class="n">${inCat.length}</span></button>` +
        grps.map(g => `<button type="button" class="chip ${S.grp === g ? "on" : ""}" data-grp="${esc(g)}">${esc(g)}<span class="n">${inCat.filter(d => d.grp === g).length}</span></button>`).join("") : "";
    } else $("#grps").innerHTML = "";
    const f = [["stocked", "✓ Stocked only", S.stocked], ["flagged", "⚠ Flagged only", S.flagged, "warn"],
      ["core:HC", "Hollow core", S.core === "HC"], ["core:SC", "Solid core", S.core === "SC"],
      ["h:80", "6'8\"", S.height === 80], ["h:96", "8'0\"", S.height === 96]];
    $("#filters").innerHTML = f.map(([k, l, on, cls]) => `<button type="button" class="chip ${cls || ""} ${on ? "on" : ""}" data-f="${k}">${l}</button>`).join("");
    const showW = S.cat === "All" || /Interior|Exterior doors/.test(S.cat);
    $("#widths").innerHTML = showW ? [18, 20, 24, 28, 30, 32, 34, 36].map(w => `<button type="button" class="chip ${S.width === w ? "on" : ""}" data-w="${w}">${ftin(w)}</button>`).join("") : "";
  }
  function renderResults() {
    const res = search();
    $("#count").textContent = res.length.toLocaleString() + (res.length === 1 ? " item" : " items");
    if (!res.length && S.q && (S.cat !== "All" || S.grp)) {
      const ts = toks(), n = pool().filter(d => ts.every(t => tokOk(d, t))).length;
      if (n) { $("#results").innerHTML = `<div class="empty">Nothing in ${esc(S.cat)}.<button type="button" class="more" id="widen">Show ${n} match${n > 1 ? "es" : ""} in all categories</button></div>`; return; }
    }
    if (!res.length) {
      $("#results").innerHTML = `<div class="empty">Nothing matches. Try fewer words, or a size like 2868.${filtersOn() ? '<button type="button" class="more" id="clearf">Clear filters</button>' : ""}</div>`;
      return;
    }
    $("#results").innerHTML = res.slice(0, S.shown).map(card).join("") +
      (res.length > S.shown ? `<button type="button" class="more" id="more">Show more (${(res.length - S.shown).toLocaleString()} left)</button>` : "");
  }
  function render() {
    const showResults = !!S.q || S.cat !== "All";
    $("#browse").classList.toggle("hidden", showResults);
    $("#resultsWrap").classList.toggle("hidden", !showResults);
    $("#q").placeholder = S.vendor ? (S.cat !== "All" ? `Search ${S.cat.toLowerCase()}…` : `Search ${vname(S.vendor)}…`) : "Search all vendors: 2868 6 panel, part #…";
    renderCrumbs();
    if (showResults) { renderChips(); renderResults(); } else renderBrowse();
    $("#examples").classList.toggle("hidden", !!S.q || S.cat !== "All");
    $("#qclear").classList.toggle("hidden", !S.q);
  }

  // ------------------------------------------------------------ item sheet
  function openItem(id) {
    const d = BYID.get(id); if (!d) return;
    const sib = (ITEMS[d.v] || []).filter(x => x.grp === d.grp && x.name === d.name && x.w).sort((a, b) => (a.h - b.h) || (a.w - b.w));
    const inList = (S.list[d.v] || {})[d.id] || 0;
    const others = VENDORS.filter(v => v.code !== d.v);
    $("#sheet").innerHTML = `<div class="grab"></div><button type="button" class="close" id="close" aria-label="Close">×</button>
      <div class="muted small">${esc(d.cat)} · ${esc(d.grp)}</div>
      <h1 style="margin-top:4px">${esc(d.name)}</h1>
      <div class="meta" style="display:flex;flex-wrap:wrap;gap:6px">${tags(d)}</div>
      ${d.flag ? `<div class="flagbox">⚠ ${esc(d.flag)}<div class="small" style="font-weight:500;margin-top:4px">Confirm with the vendor before ordering.</div></div>` : ""}
      <div class="box"><div class="vendorhead"><b>Compare vendors</b><span class="small muted">net cost</span></div>
        <div class="vrow"><div><div class="vn">${esc(vname(d.v))}</div><div class="small muted">${esc(d.sheet || "")}</div></div>
          <div class="vp">${d.price == null ? '<span class="small muted">call</span>' : money(d.price)}</div></div>
        ${others.map(v => `<div class="vrow"><div><div class="vn">${esc(v.name)}</div><div class="small muted">${v.sheet ? "Matching across vendors comes in a later version" : "No price sheet yet"}</div></div><div class="vp"><span class="small muted">—</span></div></div>`).join("")}
      </div>
      ${(d.compare || []).length ? `<div class="box"><div class="vendorhead"><b>Other price levels</b><span class="small muted">for comparison only</span></div>
        ${d.compare.map(c => `<div class="vrow"><div><div class="vn">${esc(c.label)}</div>${d.price ? `<div class="small muted">${c.price < d.price ? `${money(d.price - c.price)} less` : c.price > d.price ? `${money(c.price - d.price)} more` : "same"} than your price</div>` : ""}</div>
          <div class="vp">${money(c.price)}</div></div>`).join("")}
        <div class="small muted" style="margin-top:6px">Your price is ${d.price == null ? "call for price" : money(d.price)}. POs always use it.</div></div>` : ""}
      <div class="box"><div class="kv">
        <div>Part #</div><div class="mono">${esc(d.sku)}</div>
        ${d.mfr ? `<div>${esc(d.brand || "Maker")} #</div><div class="mono">${esc(d.mfr)}</div>` : ""}
        ${d.w ? `<div>Size</div><div>${esc(sizeLabel(d))}</div>` : ""}
        ${d.th ? `<div>Thickness</div><div>${esc(d.th)}</div>` : ""}
        ${d.core ? `<div>Core / material</div><div>${esc(d.core)}</div>` : ""}
        ${d.uom ? `<div>Sold per</div><div>${esc(d.uom)}</div>` : ""}
        <div>Stock</div><div>${d.stock === true ? "Stocked" : d.stock === false ? "Non-stock: +30% if fewer than 10 of one size/style" : "Not marked on sheet"}</div>
        ${d.page ? `<div>Source</div><div>${esc(vname(d.v))} sheet p.${d.page}</div>` : ""}
      </div></div>
      ${sib.length > 1 ? `<div class="box"><b>Other sizes</b><div class="sizes" style="margin-top:10px">${sib.map(x => `<button type="button" class="psz ${x.id === d.id ? "cur" : ""}" data-id="${x.id}"><b>${ftin(x.w)} ${x.h === 96 ? "8'0" : x.h === 80 ? "6'8" : ""}</b>${x.price == null ? "call" : money(x.price)}${x.stock ? " ✓" : ""}</button>`).join("")}</div></div>` : ""}
      <div class="addrow"><div class="qty"><button type="button" data-step="-1" aria-label="Less">−</button><input id="qty" inputmode="numeric" value="${inList || 1}" aria-label="Quantity"><button type="button" data-step="1" aria-label="More">+</button></div>
        <button type="button" class="btn" id="add">${inList ? "Update buy list" : "Add to buy list"}</button></div>
      <button type="button" class="btn secondary" id="copy">Copy part #</button>`;
    $("#sheet").dataset.id = d.id;
    $("#sheet").classList.remove("hidden"); $("#sheetbg").classList.remove("hidden"); $("#sheet").scrollTop = 0;
  }
  const closeSheet = () => { $("#sheet").classList.add("hidden"); $("#sheetbg").classList.add("hidden"); };
  function toast(t) { const el = $("#toast"); el.textContent = t; el.classList.remove("hidden"); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.add("hidden"), 2400); }

  // ------------------------------------------------------------ buy list + PO
  function lineCalc(d, qty) {
    const base = (d.price || 0) * qty;
    const sur = d.stock === false && NONSTOCK_CATS.includes(d.cat) && qty < 10 && d.price ? base * 0.3 : 0;
    return { base, sur, total: base + sur };
  }
  function setQty(v, id, q) {
    q = Math.max(0, Math.min(999, parseInt(q, 10) || 0));
    S.list[v] = S.list[v] || {};
    if (q) S.list[v][id] = q; else delete S.list[v][id];
    if (!Object.keys(S.list[v]).length) delete S.list[v];
    saveList(); renderList();
  }
  function listLines(v) {
    return Object.entries(S.list[v] || {}).map(([id, q]) => {
      const d = BYID.get(+id); if (!d) return null;
      const c = lineCalc(d, q);
      return { id: d.id, d, qty: q, ...c };
    }).filter(Boolean);
  }
  function draft(v) { return Object.assign({ date: localDay(), method: "Delivery", shipto: "shop", notes: "", job: null }, store.get("draft_" + v, {})); }
  function saveDraft(v, f) { store.set("draft_" + v, f); }
  function localDay() { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }

  function renderList() {
    const vs = Object.keys(S.list).filter(v => listLines(v).length);
    const n = vs.reduce((a, v) => a + listLines(v).reduce((x, l) => x + l.qty, 0), 0);
    $("#listcount").textContent = n; $("#listcount").classList.toggle("hidden", !n);
    if (!vs.length) {
      $("#listbody").innerHTML = '<div class="empty">Your buy list is empty.<br>Go to Shop, pick an item and tap “Add to buy list”.</div>';
      $("#poform").innerHTML = ""; return;
    }
    if (!S.poVendor || !vs.includes(S.poVendor)) S.poVendor = vs[0];
    $("#listbody").innerHTML = vs.map(v => {
      const lines = listLines(v); let sub = 0, tbd = 0;
      const rows = lines.map(l => { if (l.d.price == null) tbd++; else sub += l.total;
        return `<div class="line"><div class="d"><div class="t">${esc(l.d.name)}</div><div class="s mono">${esc(l.d.sku)}</div>
          <div class="s">${esc(sizeLabel(l.d))} ${l.d.price == null ? "· <b>call for price</b>" : "· " + money(l.d.price) + (l.d.uom ? " per " + esc(l.d.uom) : " each")}</div>
          ${l.sur ? `<div class="sur">+30% non-stock (under 10): ${money(l.sur)}</div>` : ""}
          ${l.d.flag ? `<div class="sur">⚠ ${esc(l.d.flag)}</div>` : ""}</div>
          <div class="qty"><button type="button" data-lq="-1" data-v="${esc(v)}" data-id="${l.id}" aria-label="Less">−</button><input value="${l.qty}" data-lv="${esc(v)}" data-lid="${l.id}" inputmode="numeric" aria-label="Quantity"><button type="button" data-lq="1" data-v="${esc(v)}" data-id="${l.id}" aria-label="More">+</button></div>
          <div class="amt">${l.d.price == null ? "TBD" : money(l.total)}</div></div>`; }).join("");
      return `<div class="box"><div class="vendorhead"><b>${esc(vname(v))}</b><span class="small muted">${lines.length} line${lines.length > 1 ? "s" : ""}</span></div>${rows}
        <div class="total"><span>Subtotal</span><span>${money(sub)}</span></div>
        ${tbd ? `<div class="small" style="color:var(--blue);margin-top:6px">${tbd} line${tbd > 1 ? "s need" : " needs"} a price from the vendor (shows as TBD on the PO).</div>` : ""}
        ${vs.length > 1 ? `<button type="button" class="btn ${S.poVendor === v ? "" : "secondary"}" data-povendor="${esc(v)}">${S.poVendor === v ? "Making this PO ↓" : "Make the PO for " + esc(vname(v))}</button>` : ""}</div>`;
    }).join("");
    renderPoForm(S.poVendor);
  }
  function renderPoForm(v) {
    const f = draft(v), vend = VENDORS.find(x => x.code === v) || {};
    const job = f.job;
    let jobHtml;
    if (job) {
      jobHtml = `<div class="field"><label>Job (Service Fusion)</label>
        <div class="po-row" style="cursor:default;border-color:var(--green);background:var(--green-bg)"><div><b>${esc(job.customer || "Job")}</b>
          <div class="small">Job ${esc(job.number)}${job.status ? " · " + esc(job.status) : ""}</div>${job.address ? `<div class="small muted">${esc(job.address)}</div>` : ""}</div>
          <button type="button" class="link" id="jobChange">Change</button></div>
        ${job.po_number ? `<div class="small"><b>PO # ${esc(job.po_number)}</b> <span class="muted">(from the job in Service Fusion)</span></div><div id="dupWarn"></div>`
          : '<div class="warnbox">This job has no PO number in Service Fusion. Add it there, then tap Refresh and pick the job again.</div>'}</div>`;
    } else {
      jobHtml = `<div class="field"><label for="jobq">Find the job <span class="muted small">(the PO number comes from the job in Service Fusion)</span></label>
        <div class="jl-row" style="display:flex;gap:8px"><input id="jobq" type="search" placeholder="Last 4 of job # or customer name" autocomplete="off"><button type="button" class="mini" id="jobRefresh">Refresh</button></div>
        <div class="small muted" id="jobStatus" style="margin-top:6px"></div><div id="jobRes"></div></div>`;
    }
    const ok = job && job.po_number;
    $("#poform").innerHTML = `<div class="box"><b style="font-size:18px">Purchase order to ${esc(vname(v))}</b>
      ${jobHtml}
      <div class="field"><label for="po-date">Order date</label><input id="po-date" type="date" data-d="date" value="${esc(f.date)}"></div>
      <div class="grid2">
        <div class="field"><label for="po-method">Shipping method</label><select id="po-method" data-d="method">
          ${["Delivery", "Will Call – Dallas"].map(m => `<option ${f.method === m ? "selected" : ""}>${m}</option>`).join("")}</select></div>
        <div class="field"><label for="po-ship">Ship to</label><select id="po-ship" data-d="shipto">
          <option value="shop" ${f.shipto === "shop" ? "selected" : ""}>SimplyDoors shop (Schertz)</option>
          <option value="site" ${f.shipto === "site" ? "selected" : ""}>Job site (address from Service Fusion)</option></select></div>
      </div>
      <div class="field"><label for="po-notes">Notes for the vendor</label><textarea id="po-notes" data-d="notes" rows="2" maxlength="1000" placeholder="Delivery instructions, call before delivery…">${esc(f.notes)}</textarea></div>
      <div class="small muted" style="margin-top:8px">Sending emails the PO as a PDF to <b>${esc(vend.can_order ? "the " + vname(v) + " order email set in Admin" : "(no order email set yet: ask an admin)")}</b> and a copy to <b>${ADMIN_COPY}</b>.</div>
      <button type="button" class="btn" id="po-preview" ${ok ? "" : "disabled"}>${!job ? "Pick the job first" : !job.po_number ? "Job needs a PO # in Service Fusion" : "Preview purchase order"}</button>
      <button type="button" class="btn secondary" id="clearlist">Clear this buy list</button></div>`;
    if (!job) runJobSearch();
    else if (job.po_number) api(`api/pricelist/po-check?po=${encodeURIComponent(job.po_number)}`).then(r => {
      const el = $("#dupWarn");
      if (el && r.sent_before.length) el.innerHTML = `<div class="warnbox">PO # ${esc(job.po_number)} was already sent ${r.sent_before.length > 1 ? r.sent_before.length + " times" : "on " + esc(r.sent_before[0].order_date)}. Sending again uses the same PO number. If this is a separate order, add a new PO number to the job in Service Fusion first, then refresh.</div>`;
    }).catch(() => {});
  }
  let jobSeq = 0, jobTimer;
  async function runJobSearch() {
    const my = ++jobSeq, q = $("#jobq") ? $("#jobq").value.trim() : "";
    if (!me.job_lookup) { if ($("#jobRes")) $("#jobRes").innerHTML = '<div class="warnbox">The job lookup isn\'t connected to Service Fusion yet, so POs can\'t be sent.</div>'; return; }
    try {
      const r = await api(`api/jobs?form=po&q=${encodeURIComponent(q)}`);
      if (my !== jobSeq || !$("#jobRes")) return;
      $("#jobStatus").textContent = r.last_ok ? `Job list updated ${new Date(r.last_ok).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : "";
      $("#jobRes").innerHTML = r.results.map(j => `<button type="button" class="po-row" data-job="${esc(j.number)}"><div><b>${esc(j.customer || "(no name)")}</b>
        <div class="small">Job …${esc(j.last4)}</div></div><div class="small muted" style="text-align:right">${esc(j.status || "")}</div></button>`).join("")
        || `<div class="small muted" style="margin-top:8px">${q ? "No open job matches. Check the number or tap Refresh." : "Type the last 4 of the job # or the customer's name."}</div>`;
    } catch (e) { if ($("#jobRes")) $("#jobRes").innerHTML = `<div class="small muted">${esc(e.message)}</div>`; }
  }
  async function pickJob(num) {
    const v = S.poVendor;
    try {
      const d = await api(`api/jobs/${encodeURIComponent(num)}?form=po`);
      const f = draft(v);
      f.job = { number: d.number, customer: d.customer, status: d.status, address: d.address, po_number: d.po_number || "" };
      saveDraft(v, f); renderPoForm(v);
    } catch (e) { toast(e.message); }
  }

  function poPaper(p, vend) {
    const flagged = p.lines.filter(l => l.flag), tbd = p.lines.filter(l => l.price == null);
    const sur = p.lines.reduce((a, l) => a + (l.surcharge || 0), 0);
    const shipTo = p.ship_to === "site" ? ["Job site", p.ship_address || "(no address on the job)"] : [OUR.name, ...OUR.addr.slice(0, 2)];
    return { flagged, tbd, html: `<div class="paper">
      <div class="po-head"><div><img src="static/logo.png" alt="SimplyDoors"><div style="margin-top:8px"><b>${OUR.name}</b><br>${OUR.addr.map(esc).join("<br>")}</div></div>
        <div><h2>PURCHASE ORDER</h2><table class="po-meta"><tr><td>PO #</td><td>${esc(p.po_number)}</td></tr><tr><td>Date</td><td>${esc(p.order_date)}</td></tr>
          <tr><td>Job</td><td>${esc(p.job_number)}${p.job_customer ? " · " + esc(p.job_customer) : ""}</td></tr></table></div></div>
      <div class="po-blocks"><div><span class="po-lbl">VENDOR</span><b>${esc(vend.name)}</b><br>${(vend.address || []).map(esc).join("<br>")}</div>
        <div><span class="po-lbl">SHIP TO</span><b>${esc(shipTo[0])}</b><br>${shipTo.slice(1).map(esc).join("<br>")}</div>
        <div><span class="po-lbl">SHIPPING</span>${esc(p.ship_method)}<br><span class="po-lbl" style="margin-top:8px">ORDERED BY</span>${esc(p.by || me.name)}</div></div>
      <table class="po-lines"><thead><tr><th style="width:44px">QTY</th><th>PART #</th><th>DESCRIPTION</th><th class="r">UNIT</th><th class="r">AMOUNT</th></tr></thead><tbody>
        ${p.lines.map(l => `<tr><td>${l.qty}</td><td class="mono">${esc(l.sku)}</td><td>${esc(l.name)}${l.size ? `<br><span style="color:#555">${esc(l.size)}</span>` : ""}${l.surcharge ? '<br><span style="color:#8a5a00">Non-stock, under 10: +30%</span>' : ""}</td>
          <td class="r">${l.price == null ? "TBD" : money(l.price)}${l.uom ? `<br><span style="color:#555">per ${esc(l.uom)}</span>` : ""}</td><td class="r">${l.total == null ? "TBD" : money(l.total)}</td></tr>`).join("")}
      </tbody></table>
      <table class="po-tot"><tr><td>Items</td><td style="text-align:right">${money(p.total - sur)}</td></tr>
        ${sur ? `<tr><td>Non-stock surcharge (30%)</td><td style="text-align:right">${money(sur)}</td></tr>` : ""}
        <tr><td>Total${tbd.length ? " (excl. TBD)" : ""}</td><td style="text-align:right">${money(p.total)}</td></tr></table>
      <div class="po-notes"><b>Notes:</b> ${esc(p.notes) || '<span style="color:#888">—</span>'}</div>
      <div class="po-sign"><div>Authorized by</div><div>Date</div></div>
      <div class="po-foot">Prices per ${esc(vend.name)} ${esc(p.sheet_label || "")}. Please reference PO # ${esc(p.po_number)} on all invoices and packing slips.</div></div>` };
  }
  function previewPo() {
    const v = S.poVendor, f = draft(v), vend = VENDORS.find(x => x.code === v);
    const lines = listLines(v).map(l => ({ sku: l.d.sku, name: l.d.name, size: sizeLabel(l.d), qty: l.qty, price: l.d.price, uom: l.d.uom,
      surcharge: l.sur, total: l.d.price == null ? null : l.total, flag: l.d.flag }));
    const p = { po_number: f.job.po_number, order_date: f.date, job_number: f.job.number, job_customer: f.job.customer, ship_method: f.method,
      ship_to: f.shipto, ship_address: f.job.address, notes: f.notes, lines, total: lines.reduce((a, l) => a + (l.total || 0), 0),
      sheet_label: (vend.sheet || {}).label, by: me.name };
    const paper = poPaper(p, vend);
    $("#podoc").innerHTML = `<div class="po-actions"><button type="button" class="btn secondary" id="po-back">← Back to edit</button>
        <button type="button" class="btn" id="po-send">Send to ${esc(vend.name.split(" ")[0])}</button></div>
      <div class="po-actions po-warn"><div class="box small" style="flex:1;margin:0">${me.test_mode ? "<b>Test mode is on:</b> this PO is emailed only to you, not to the vendor." :
        `Sending emails this PO as a PDF to the ${esc(vend.name)} order email, with a copy to <b>${ADMIN_COPY}</b>. Prices are checked again against the loaded sheet when it's sent.`}</div></div>
      ${paper.flagged.length || paper.tbd.length ? `<div class="po-actions po-warn"><div class="warnbox" style="flex:1;margin:0">
        ${paper.flagged.length ? `⚠ ${paper.flagged.length} line${paper.flagged.length > 1 ? "s are" : " is"} flagged on the vendor's sheet (${paper.flagged.map(l => esc(l.sku)).join(", ")}). Confirm the part number with the vendor before sending.` : ""}
        ${paper.tbd.length ? `<div>${paper.tbd.length} line${paper.tbd.length > 1 ? "s have" : " has"} no price on the sheet. They show as TBD.</div>` : ""}</div></div>` : ""}
      ${paper.html}`;
    $("#podoc").classList.remove("hidden"); $("#podoc").scrollTop = 0;
  }
  async function sendPo(btn) {
    const v = S.poVendor, f = draft(v);
    btn.disabled = true; btn.textContent = "Sending…";
    try {
      const po = await api("api/pricelist/pos", { method: "POST", json: { vendor: v, job_number: f.job.number, order_date: f.date,
        ship_method: f.method, ship_to: f.shipto, notes: f.notes, lines: listLines(v).map(l => ({ item_id: l.id, qty: l.qty })) } });
      delete S.list[v]; saveList(); store.del("draft_" + v);
      $("#podoc").classList.add("hidden"); renderList(); loadPos();
      toast(po.is_test ? `Test PO ${po.po_number} emailed to you` : `PO ${po.po_number} sent to ${vname(v)} · copy to ${ADMIN_COPY}`);
      openPo(po.id);
    } catch (e) { alert(e.message); btn.disabled = false; btn.textContent = "Send"; }
  }
  async function loadPos() {
    try {
      const rows = await api("api/pricelist/pos");
      $("#pastpos").innerHTML = rows.map(p => `<button type="button" class="po-row" data-po="${p.id}">
        <div><b>${esc(p.po_number)}</b>${p.is_test ? ' <span class="tag flag">TEST</span>' : ""}<div class="small muted">${esc(vname(p.vendor))} · ${esc(p.order_date)} · ${p.line_count} line${p.line_count > 1 ? "s" : ""} · ${esc(p.job_customer || p.job_number)} · by ${esc(p.by)}</div></div>
        <div style="text-align:right"><b>${money(p.total)}</b><div><span class="st sent">Sent</span></div></div></button>`).join("") || '<div class="muted small">No purchase orders yet.</div>';
    } catch (e) { $("#pastpos").innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
  }
  async function openPo(id) {
    try {
      const p = await api(`api/pricelist/pos/${id}`);
      const vend = VENDORS.find(x => x.code === p.vendor) || { name: p.vendor };
      const full = Object.assign({ address: [] }, vend);
      const paper = poPaper(p, full);
      const e = p.email;
      const st = !e ? "No email on record." : e.status === "sent" ? `Emailed ${new Date(e.sent_at).toLocaleString()} to <b>${esc(e.recipients)}</b>${e.cc ? `, copy to <b>${esc(e.cc)}</b>` : ""}.`
        : e.status === "failed" ? `<span style="color:var(--red)">The email failed: ${esc(e.last_error || "")}. An admin can retry it from Admin → Status.</span>`
        : `Waiting to email to <b>${esc(e.recipients)}</b>${e.cc ? `, copy to <b>${esc(e.cc)}</b>` : ""}.`;
      $("#podoc").innerHTML = `<div class="po-actions"><button type="button" class="btn secondary" id="po-close">← Close</button>
          <a class="btn secondary" style="text-align:center;line-height:54px;text-decoration:none" href="api/pricelist/pos/${p.id}/pdf" target="_blank" rel="noopener">Open PDF</a>
          <button type="button" class="btn secondary" id="po-reorder" data-po="${p.id}">Copy into buy list</button></div>
        <div class="po-actions po-warn"><div class="box small" style="flex:1;margin:0">${p.is_test ? "<b>TEST PO</b> (emailed only to you). " : ""}Sent by ${esc(p.by)} · ${st}</div></div>${paper.html}`;
      $("#podoc").dataset.po = JSON.stringify({ vendor: p.vendor, lines: p.lines.map(l => ({ sku: l.sku, qty: l.qty })) });
      $("#podoc").classList.remove("hidden"); $("#podoc").scrollTop = 0;
    } catch (e) { toast(e.message); }
  }
  function reorder() {
    const d = JSON.parse($("#podoc").dataset.po || "{}");
    const bySku = new Map((ITEMS[d.vendor] || []).map(x => [x.sku, x]));
    let missing = 0;
    S.list[d.vendor] = S.list[d.vendor] || {};
    d.lines.forEach(l => { const it = bySku.get(l.sku); if (it) S.list[d.vendor][it.id] = l.qty; else missing++; });
    saveList(); $("#podoc").classList.add("hidden"); renderList();
    toast(missing ? `Copied. ${missing} item${missing > 1 ? "s aren't" : " isn't"} on the current sheet.` : "Copied into the buy list");
  }

  // ------------------------------------------------------------ sheets tab
  function renderSheets() {
    $("#sheetCards").innerHTML = VENDORS.map(v => `<div class="vcard ${v.sheet ? "live" : ""}"><div class="vt">${esc(v.name)} ${v.sheet ? '<span class="tag stock">Live</span>' : '<span class="tag ns">No sheet yet</span>'}</div>
      ${v.sheet ? `${(v.sheets || []).map(s => `<div class="muted small">${esc(s.label)} · ${s.items.toLocaleString()} items · loaded ${esc(new Date(s.uploaded_at).toLocaleDateString())} by ${esc(s.uploaded_by)}</div>`).join("")}
        <div class="stats"><div class="stat"><b>${(ITEMS[v.code] || []).length.toLocaleString()}</b><span>items</span></div>
          <div class="stat"><b>${(ITEMS[v.code] || []).filter(d => d.flag).length}</b><span>flagged to check</span></div>
          <div class="stat"><b>${(ITEMS[v.code] || []).filter(d => d.price == null).length}</b><span>call for price</span></div></div>`
        : '<div class="muted small">An admin loads the sheet in Admin → Price List.</div>'}
      <div class="small" style="margin-top:8px">${v.can_order ? "✓ Order email set" : "No order email set yet, so POs can't be sent."}</div></div>`).join("");
  }

  // ------------------------------------------------------------ events
  function tab(name) {
    $$("#nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
    ["search", "list", "sheets"].forEach(n => $("#tab-" + n).classList.toggle("hidden", n !== name));
    if (name === "list") { renderList(); loadPos(); }
    if (name === "sheets") renderSheets();
    window.scrollTo(0, 0);
  }
  let qTimer;
  $("#q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = e.target.value.trim(); S.shown = 40; render(); }, 120); });
  $("#qclear").onclick = () => { $("#q").value = ""; S.q = ""; render(); $("#q").focus(); };
  $("#sort").onchange = (e) => { S.sort = e.target.value; renderResults(); };
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    const ex = t.closest("[data-ex]"); if (ex) { $("#q").value = ex.dataset.ex; S.q = ex.dataset.ex; S.shown = 40; render(); return; }
    const vn = t.closest("[data-vendor]"); if (vn) { S.vendor = vn.dataset.vendor; S.cat = "All"; S.grp = ""; render(); window.scrollTo(0, 0); return; }
    if (t.id === "widen") { S.cat = "All"; S.grp = ""; resetFilters(); render(); return; }
    if (t.id === "clearf") { resetFilters(); render(); return; }
    const cat = t.closest("[data-cat]"); if (cat) { S.cat = cat.dataset.cat; S.grp = ""; resetFilters(); S.shown = 40; render(); window.scrollTo(0, 0); return; }
    const gp = t.closest("[data-grp]"); if (gp) { S.grp = gp.dataset.grp; S.shown = 40; render(); return; }
    const cr = t.closest("[data-crumb]"); if (cr) {
      S.q = ""; $("#q").value = ""; S.grp = "";
      if (cr.dataset.crumb === "home") { S.vendor = null; S.cat = "All"; } else if (cr.dataset.crumb === "vendor") S.cat = "All";
      S.shown = 40; render(); window.scrollTo(0, 0); return;
    }
    const f = t.closest("[data-f]"); if (f) {
      const k = f.dataset.f;
      if (k === "stocked") S.stocked = !S.stocked; else if (k === "flagged") S.flagged = !S.flagged;
      else if (k.startsWith("core:")) S.core = S.core === k.slice(5) ? "" : k.slice(5);
      else if (k.startsWith("h:")) S.height = S.height === +k.slice(2) ? 0 : +k.slice(2);
      S.shown = 40; render(); return;
    }
    const w = t.closest("[data-w]"); if (w) { S.width = S.width === +w.dataset.w ? 0 : +w.dataset.w; S.shown = 40; render(); return; }
    if (t.id === "more") { S.shown += 60; renderResults(); return; }
    const it = t.closest(".item, .psz"); if (it) { openItem(+it.dataset.id); return; }
    if (t.id === "close" || t.id === "sheetbg") { closeSheet(); return; }
    const st = t.closest("[data-step]"); if (st) { const i = $("#qty"); i.value = Math.max(1, (parseInt(i.value, 10) || 1) + +st.dataset.step); return; }
    if (t.id === "add") { const d = BYID.get(+$("#sheet").dataset.id); setQty(d.v, d.id, $("#qty").value); closeSheet(); toast("Added to buy list"); return; }
    if (t.id === "copy") { const d = BYID.get(+$("#sheet").dataset.id); if (navigator.clipboard) navigator.clipboard.writeText(d.sku).catch(() => {}); toast("Copied " + d.sku); return; }
    const lq = t.closest("[data-lq]"); if (lq) { const v = lq.dataset.v, id = +lq.dataset.id; setQty(v, id, ((S.list[v] || {})[id] || 0) + +lq.dataset.lq); return; }
    const pv = t.closest("[data-povendor]"); if (pv) { S.poVendor = pv.dataset.povendor; renderList(); return; }
    const jb = t.closest("[data-job]"); if (jb) { pickJob(jb.dataset.job); return; }
    if (t.id === "jobChange") { const f2 = draft(S.poVendor); f2.job = null; saveDraft(S.poVendor, f2); renderPoForm(S.poVendor); return; }
    if (t.id === "jobRefresh") {
      t.disabled = true; t.textContent = "Refreshing…";
      api("api/jobs/refresh", { method: "POST" }).catch(e => toast(e.message)).finally(() => { t.disabled = false; t.textContent = "Refresh"; runJobSearch(); });
      return;
    }
    if (t.id === "clearlist") { if (!confirm(`Clear the ${vname(S.poVendor)} buy list?`)) return; delete S.list[S.poVendor]; saveList(); store.del("draft_" + S.poVendor); renderList(); return; }
    if (t.id === "po-preview") { previewPo(); return; }
    if (t.id === "po-back" || t.id === "po-close") { $("#podoc").classList.add("hidden"); return; }
    if (t.id === "po-send") { sendPo(t); return; }
    if (t.id === "po-reorder") { reorder(); return; }
    const po = t.closest(".po-row[data-po]"); if (po) { openPo(+po.dataset.po); return; }
    const nb = t.closest("#nav button"); if (nb) { tab(nb.dataset.tab); return; }
  });
  document.addEventListener("input", (e) => {
    if (e.target.id === "jobq") { clearTimeout(jobTimer); jobTimer = setTimeout(runJobSearch, 250); return; }
    if (e.target.dataset.d && e.target.tagName === "TEXTAREA") { const f = draft(S.poVendor); f[e.target.dataset.d] = e.target.value; saveDraft(S.poVendor, f); }
  });
  document.addEventListener("change", (e) => {
    if (e.target.dataset.lid) { setQty(e.target.dataset.lv, +e.target.dataset.lid, e.target.value); return; }
    if (e.target.dataset.d) { const f = draft(S.poVendor); f[e.target.dataset.d] = e.target.value; saveDraft(S.poVendor, f); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSheet(); $("#podoc").classList.add("hidden"); } });

  // ------------------------------------------------------------ start
  (async () => {
    const gate = (msg) => { $("#gateMsg").innerHTML = msg; $("#gate").classList.remove("hidden"); };
    try { me = await api("api/me"); }
    catch (e) { return gate(e.status === 401 ? 'Sign in on the <a href="./">staff app</a> first, then come back here.' : esc(e.message)); }
    $("#who").textContent = me.name.split(" ")[0];
    if (!me.price_list) return gate("The Price List isn't switched on for you. Ask Adem or Paz.");
    try {
      VENDORS = await api("api/pricelist/vendors");
      for (const v of VENDORS.filter(x => x.sheet)) {
        const r = await api(`api/pricelist/items?vendor=${encodeURIComponent(v.code)}`);
        ITEMS[v.code] = r.items; r.items.forEach(d => prep(d, v.code));
      }
    } catch (e) { return gate(esc(e.message)); }
    S.list = store.get("list", {});
    // drop lines whose item isn't on the current sheet any more (a newer sheet was loaded)
    let dropped = 0;
    Object.keys(S.list).forEach(v => Object.keys(S.list[v]).forEach(id => { if (!BYID.has(+id)) { delete S.list[v][id]; dropped++; } }));
    Object.keys(S.list).forEach(v => { if (!Object.keys(S.list[v]).length) delete S.list[v]; });
    if (dropped) { saveList(); setTimeout(() => toast(`${dropped} item${dropped > 1 ? "s were" : " was"} removed from your buy list: a new price sheet was loaded.`), 300); }
    const live = VENDORS.filter(v => v.sheet);
    if (live.length === 1) S.vendor = live[0].code;     // only one vendor loaded: start on its categories
    $("#tab-search").classList.remove("hidden"); $("#nav").classList.remove("hidden");
    render(); renderList();
  })();
})();

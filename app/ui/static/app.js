"use strict";
const BASE = location.pathname.replace(/\/$/, "");
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const FA = "۰۱۲۳۴۵۶۷۸۹";
const toFa = (s) => String(s).replace(/\d/g, (d) => FA[d]);
function fmtBytes(n) { n = Number(n || 0); const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return toFa((i === 0 ? n.toFixed(0) : n.toFixed(2))) + " " + u[i]; }
const fmtSpeed = (n) => fmtBytes(n) + "/ث";
function fmtUptime(s) { s = Number(s || 0); const d = (s / 86400) | 0, h = ((s % 86400) / 3600) | 0, m = ((s % 3600) / 60) | 0; if (d) return toFa(d) + " روز " + toFa(h) + " ساعت"; if (h) return toFa(h) + " ساعت " + toFa(m) + " دقیقه"; return toFa(m) + " دقیقه"; }
function fmtExpiry(u) { if (!u.expiry) return "نامحدود"; if (u.days_left <= 0) return "منقضی"; return toFa(u.days_left) + " روز"; }
function toast(msg, kind = "ok") { const t = $("#toast"); t.textContent = msg; t.className = "toast show " + kind; setTimeout(() => (t.className = "toast"), 2600); }
async function api(path, opts = {}) { const r = await fetch(BASE + path, { headers: { "Content-Type": "application/json" }, ...opts }); if (r.status === 401) { location.reload(); throw new Error("401"); } if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || "خطا"); } return r.status === 204 ? null : r.json(); }
function copy(text) { navigator.clipboard.writeText(text).then(() => toast("کپی شد")).catch(() => { const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); toast("کپی شد"); }); }

const I = {
  dash: '<path d="M3 13h8V3H3zM13 21h8V8h-8zM3 21h8v-6H3zM13 3v3h8V3z"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
  ips: '<path d="M21 12a9 9 0 1 1-9-9"/><path d="M21 3v6h-6"/><path d="M12 7v5l3 3"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  logs: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h8M8 9h2"/>',
  dl: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
  ul: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/>',
  sum: '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
  on: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/>',
  ram: '<path d="M6 19v-3M10 19v-3M14 19v-3M18 19v-3M3 5h18a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/>',
  qr: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><path d="M14 14h3v3h-3zM21 14v7M17 21h4M14 18v3"/>',
  edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z"/>',
  reset: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  toggle: '<rect x="1" y="5" width="22" height="14" rx="7"/><circle cx="8" cy="12" r="3"/>',
  trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  add: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  gh: '<path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.9a3.4 3.4 0 0 0-.9-2.6c3-.3 6.2-1.5 6.2-6.7A5.2 5.2 0 0 0 20 4.8 4.9 4.9 0 0 0 19.9 1S18.7.6 16 2.5a13.4 13.4 0 0 0-7 0C6.3.6 5.1 1 5.1 1A4.9 4.9 0 0 0 5 4.8a5.2 5.2 0 0 0-1.5 3.7c0 5.2 3.2 6.4 6.2 6.7A3.4 3.4 0 0 0 9 18z"/>',
  empty: '<circle cx="12" cy="12" r="10"/><path d="M8 15s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/>',
};
const svg = (p, w = 20) => `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;

const VIEWS = [
  { id: "dash", label: "داشبورد", icon: I.dash },
  { id: "users", label: "کاربران", icon: I.users },
  { id: "ips", label: "۲۰نفربرتر", icon: I.ips },
  { id: "settings", label: "تنظیمات", icon: I.settings },
  { id: "logs", label: "لاگ‌ها", icon: I.logs },
];
let current = "dash";
let chart = null;

function buildNav() {
  $("#nav").innerHTML = VIEWS.map((v) => `<a data-v="${v.id}" class="${v.id === current ? "on" : ""}">${svg(v.icon)}${v.label}</a>`).join("");
  $("#bottom-nav").innerHTML = VIEWS.map((v) => `<a data-v="${v.id}" class="${v.id === current ? "on" : ""}">${svg(v.icon)}<span>${v.label}</span></a>`).join("");
  $$("[data-v]").forEach((a) => a.onclick = () => switchView(a.dataset.v));
}
function switchView(id) {
  current = id;
  $("#view-title").textContent = VIEWS.find((v) => v.id === id).label;
  ["dash", "users", "ips", "settings", "logs"].forEach((v) => $("#view-" + v).classList.toggle("hidden", v !== id));
  $$("[data-v]").forEach((a) => a.classList.toggle("on", a.dataset.v === id));
  if (id === "dash") loadDash();
  if (id === "users") loadUsers();
  if (id === "ips") loadIps();
  if (id === "settings") loadSettings();
  if (id === "logs") loadLogs();
}

let lastOverview = null;
function speedFromHistory(hist) {
  // 🟢 سرعت واقعی از آخرین نمونهٔ history (delta تفکیک‌شدهٔ آمار هسته) خوانده می‌شود
  // هر نمونه: [ts, up_bps, down_bps]؛ برخلاف sys.rx/tx که ترافیک کل کانتینر (رمزنگاری‌شدهٔ دو‌طرفه) بود
  const pts = hist || [];
  if (!pts.length) return { up: 0, down: 0 };
  const last = pts[pts.length - 1];
  return { up: last[1] || 0, down: last[2] || 0 };
}
async function loadDash() {
  let o; try { o = await api("/api/overview"); } catch (e) { return; }
  lastOverview = o;
  const spd = speedFromHistory(o.history);
  $("#online-pill").textContent = toFa(o.online);

  // گرفتن تعداد کل IP یونیک از Axiom (در پس‌زمینه)
  let axiomIpCount = "…";
  api("/api/axiom-ip-count").then(r => {
    axiomIpCount = toFa(r.count);
    const el = document.getElementById("axiom-ip-val");
    if (el) el.textContent = axiomIpCount;
  }).catch(() => {
    const el = document.getElementById("axiom-ip-val");
    if (el) el.textContent = "—";
  });

  const cards = [
    ["dl", I.dl, "کل دانلود", fmtBytes(o.totals.down)],
    ["ul", I.ul, "کل آپلود", fmtBytes(o.totals.up)],
    ["", I.sum, "ترافیک کل", fmtBytes(o.totals.all)],
    ["on", I.on, "کاربران آنلاین", toFa(o.online)],
    ["ip", I.ips, "آی‌پی‌های فعال", `${toFa(o.ips.active)} <small>(${toFa(o.ips.total)} کل)</small>`],
    ["usr", I.users, "کل کاربران", `${toFa(o.users.total)} <small>(${toFa(o.users.active)} فعال)</small>`],
    ["", I.cpu, "پردازنده", toFa(o.sys.cpu) + "٪"],
    ["", I.ram, "حافظه", toFa(o.sys.mem_pct) + `٪ <small>${fmtBytes(o.sys.mem_used)}</small>`],
    ["axiom-ip", I.ips, "کل کسایی که وصل شدن:", `<span id="axiom-ip-val">…</span>`],
  ].map(([c, ic, lb, val]) => `<div class="card stat ${c}"><div class="ico">${svg(ic)}</div><div class="label">${lb}</div><div class="value">${val}</div></div>`).join("");

  $("#view-dash").innerHTML = `
    <div class="card-grid">${cards}</div>
    <div class="section card">
      <h3>${svg(I.sum, 18)} نمودار ترافیک لحظه‌ای</h3>
      <div class="chart-wrap" id="chart"></div>
      <div class="speed-row">
        <div class="speed-box down"><div class="speed-icon">${svg(I.dl, 28)}</div><div class="speed-label">دانلود</div><div class="speed-val">${fmtSpeed(spd.down)}</div></div>
        <div class="speed-box up"><div class="speed-icon">${svg(I.ul, 28)}</div><div class="speed-label">آپلود</div><div class="speed-val">${fmtSpeed(spd.up)}</div></div>
      </div>
    </div>`;
  drawChart($("#chart"), o.history);
}

function drawChart(cv, hist) {
  if (!cv) return;
  const pts = (hist || []).slice(-60);
  const dl = pts.map((p) => ({ x: p[0], y: p[2] }));
  const ul = pts.map((p) => ({ x: p[0], y: p[1] }));
  const options = {
    chart: { type: "area", height: 300, background: "transparent", foreColor: "#8b99b4", toolbar: { show: false }, sparkline: { enabled: false } },
    series: [{ name: "دانلود", data: dl }, { name: "آپلود", data: ul }],
    colors: ["#22d3ee", "#34d399"],
    stroke: { curve: "smooth", width: 2 },
    fill: { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.05, stops: [0, 90, 100] } },
    dataLabels: { enabled: false },
    xaxis: { type: "datetime", labels: { show: false } },
    yaxis: { labels: { formatter: (v) => fmtBytes(v) } },
    tooltip: { x: { format: "HH:mm:ss" } },
    grid: { borderColor: "rgba(148,163,184,.1)", strokeDashArray: 3 },
    legend: { show: false }
  };
  if (chart) chart.destroy();
  chart = new ApexCharts(cv, options);
  chart.render();
}

let allUsers = [], filterText = "";
async function loadUsers() {
  $("#view-users").innerHTML = `<div class="toolbar"><div class="search">${svg(I.search)}<input id="u-search" placeholder="جستجوی کاربر…" value="${filterText}"></div><button class="btn sm" id="u-add">${svg(I.add, 16)} کاربر جدید</button></div><div id="u-table"></div>`;
  $("#u-add").onclick = () => openUserModal();
  $("#u-search").oninput = (e) => { filterText = e.target.value; renderUsers(); };
  try { allUsers = await api("/api/users"); } catch (e) { return; }
  renderUsers();
}
function renderUsers() {
  const q = filterText.trim().toLowerCase();
  const list = allUsers.filter((u) => !q || u.label.toLowerCase().includes(q));
  const box = $("#u-table");
  if (!list.length) { box.innerHTML = `<div class="card empty">${svg(I.empty, 46)}<div>${allUsers.length ? "کاربری یافت نشد" : "هنوز کاربری نساخته‌اید"}</div></div>`; return; }
  const rows = list.map((u) => {
    const used = u.quota ? Math.min(100, u.used / u.quota * 100) : 0;
    const protoLabels = { ws: "WS", grpc: "gRPC", reality: "Reality" };
    const protos = u.protocols.map((p) => `<span class="proto-tag">${protoLabels[p] || p}</span>`).join("");
    const av = (u.label || "?").trim().charAt(0).toUpperCase();
    return `<tr data-uid="${u.uid}">
      <td><div class="uname"><div class="av">${av}</div><div><div>${escapeHtml(u.label)}</div><div style="font-size:11px;color:var(--dim)">${protos}</div></div></div></td>
      <td><span class="badge ${u.status}">${({ active: "فعال", expired: "منقضی", disabled: "غیرفعال" })[u.status]}</span></td>
      <td><div>${fmtBytes(u.used)}${u.quota ? " / " + fmtBytes(u.quota) : ""}</div>${u.quota ? `<div class="mini-track"><div class="fill ${used > 90 ? "rose" : ""}" style="width:${used}%"></div></div>` : ""}</td>
      <td>${fmtExpiry(u)}</td>
      <td><span class="online-dot ${u.online ? "y" : "n"}"></span>${u.online ? "آنلاین" : "—"}</td>
      <td><div class="row-actions"><button title="کانفیگ و QR" data-act="links">${svg(I.qr, 15)}</button><button title="ویرایش" data-act="edit">${svg(I.edit, 15)}</button><button title="صفر کردن ترافیک" data-act="reset">${svg(I.reset, 15)}</button><button title="${u.status === "disabled" ? "فعال‌سازی" : "غیرفعال‌سازی"}" data-act="toggle">${svg(I.toggle, 15)}</button><button title="حذف" data-act="del">${svg(I.trash, 15)}</button></div></td></tr>`;
  }).join("");
  box.innerHTML = `<div class="table-wrapper"><table><thead><tr><th>کاربر</th><th>وضعیت</th><th>مصرف</th><th>انقضا</th><th>اتصال</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  $$("#u-table tr[data-uid]").forEach((tr) => { const uid = tr.dataset.uid; $$("[data-act]", tr).forEach((b) => b.onclick = () => userAction(b.dataset.act, uid)); });
}
async function userAction(act, uid) {
  const u = allUsers.find((x) => x.uid === uid);
  if (act === "links") return showLinks(uid);
  if (act === "edit") return openUserModal(u);
  if (act === "reset") { await api(`/api/users/${uid}/reset`, { method: "POST" }); toast("ترافیک صفر شد"); return loadUsers(); }
  if (act === "toggle") { const ns = u.status === "disabled" ? "active" : "disabled"; await api(`/api/users/${uid}`, { method: "POST", body: JSON.stringify({ status: ns }) }); toast(ns === "active" ? "فعال شد" : "غیرفعال شد"); return loadUsers(); }
  if (act === "del") { if (!confirm(`حذف کاربر «${u.label}»؟`)) return; await api(`/api/users/${uid}`, { method: "DELETE" }); toast("حذف شد"); return loadUsers(); }
}

function openUserModal(u = null) {
  const edit = !!u;
  $("#um-title").textContent = edit ? "ویرایش کاربر" : "کاربر جدید";
  $("#um-uid").value = edit ? u.uid : "";
  $("#um-label").value = edit ? u.label : "";
  $("#um-days").value = edit ? 0 : 30;
  $("#um-gb").value = edit && u.quota ? (u.quota / 1024 ** 3).toFixed(0) : 0;
  $("#um-wsips").value = edit ? u.ws_ips : "";
  $("#um-sni").value = edit ? u.reality_sni : "";
  $("#um-err").textContent = "";
  $$(".chip", $("#um-protos")).forEach((c) => c.classList.toggle("on", edit ? u.protocols.includes(c.dataset.p) : true));
  $("#um-edit-extra").style.display = edit ? "block" : "none";
  if (edit) { $("#um-status").value = u.status === "disabled" ? "disabled" : "active"; }
  $("#ov-user").classList.add("show");
}
 $$("#um-protos .chip").forEach((c) => c.onclick = () => c.classList.toggle("on"));
 $("#um-save").onclick = async () => {
  const uid = $("#um-uid").value;
  const protos = $$("#um-protos .chip.on").map((c) => c.dataset.p);
  if (!protos.length) { $("#um-err").textContent = "حداقل یک پروتکل را انتخاب کنید"; return; }
  const btn = $("#um-save"); btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  try {
    if (uid) {
      const body = { label: $("#um-label").value, gb: +$("#um-gb").value, protocols: protos, status: $("#um-status").value, ws_ips: $("#um-wsips").value, reality_sni: $("#um-sni").value };
      if (+$("#um-days").value > 0) body.add_days = +$("#um-days").value;
      await api(`/api/users/${uid}`, { method: "POST", body: JSON.stringify(body) });
      toast("ذخیره شد"); $("#ov-user").classList.remove("show"); loadUsers();
    } else {
      const body = { label: $("#um-label").value, days: +$("#um-days").value, gb: +$("#um-gb").value, protocols: protos, ws_ips: $("#um-wsips").value, reality_sni: $("#um-sni").value };
      const res = await api("/api/users", { method: "POST", body: JSON.stringify(body) });
      $("#ov-user").classList.remove("show"); loadUsers(); showLinksData(res, $("#um-label").value);
    }
  } catch (e) { $("#um-err").textContent = e.message; }
  btn.disabled = false; btn.textContent = "ذخیره";
};

async function showLinks(uid) { const u = allUsers.find((x) => x.uid === uid); const data = await api(`/api/users/${uid}/links`); showLinksData(data, u.label); }
function showLinksData(data, label) {
  $("#lk-title").textContent = "کانفیگ‌های " + (label || "");
  $("#lk-sub").textContent = data.sub_link;
  loadQR(data.sub_link);
  $("#lk-list").innerHTML = data.links.map((lnk, i) => {
    let name;
    if (lnk.includes("security=reality")) {
      name = "Reality";
    } else if (lnk.includes("type=grpc")) {
      name = "gRPC";
    } else if (lnk.includes("WS-Main") || lnk.includes("ws-main")) {
      name = "WS (Main)";
    } else {
      // WS with custom IP — extract server from URL
      try { name = `WS (${lnk.split("@")[1].split(":")[0]})`; } catch (_) { name = "WS"; }
    }
    return `<div class="field"><label>${name}</label><div class="code-box" id="lkc${i}">${escapeHtml(lnk)}</div><button class="btn sm" data-copy="lkc${i}" style="width:100%">کپی</button></div>`;
  }).join("") || '<p style="color:var(--muted);font-size:13px">پروتکلی فعال نیست</p>';
  bindCopy();
  $("#ov-links").classList.add("show");
}
async function loadQR(text) {
  const box = $("#lk-qr"); box.innerHTML = '<span class="spin" style="border-top-color:#0f172a"></span>';
  try { const r = await fetch(BASE + "/api/qr?data=" + encodeURIComponent(text)); box.innerHTML = await r.text(); } catch (_) { box.innerHTML = ""; }
}

async function loadIps() {
  let ips; try { ips = await api("/api/ips"); } catch (e) { return; }
  $("#view-ips").innerHTML = `<div class="card"><h3 style="margin-bottom:14px">${svg(I.ips, 18)} ۲۰ آی‌پی پر‌مصرف</h3>
    <div class="table-wrapper"><table><thead><tr><th>آی‌پی</th><th>دانلود</th><th>آپلود</th><th>کل</th></tr></thead>
    <tbody>${ips.map(i => `<tr><td style="direction:ltr;text-align:left">${i.ip}</td><td>${fmtBytes(i.down)}</td><td>${fmtBytes(i.up)}</td><td><b>${fmtBytes(i.total)}</b></td></tr>`).join("") || `<tr><td colspan="4" class="empty">داده‌ای موجود نیست</td></tr>`}</tbody></table></div></div>`;
}

async function loadSettings() {
  const o = lastOverview || await api("/api/overview").catch(() => null);
  const gh = o ? o.gh : { enabled: false };
  const ghStatus = gh.enabled ? `<span class="badge active">متصل</span>` : `<span class="badge disabled">غیرفعال</span>`;
  const last = gh.last_ok ? new Date(gh.last_ok * 1000).toLocaleString("fa-IR") : "—";
  $("#view-settings").innerHTML = `
    <div class="card-grid" style="grid-template-columns:repeat(auto-fill,minmax(280px,1fr))">
      <div class="card"><h3 style="margin-bottom:14px">تغییر رمز عبور</h3><div class="field"><label>رمز فعلی</label><input type="password" id="s-cur"></div><div class="field"><label>رمز جدید</label><input type="password" id="s-new"></div><button class="btn" id="s-pwbtn">بروزرسانی رمز</button><div class="err" id="s-pwerr"></div></div>
      <div class="card"><h3 style="margin-bottom:14px">${svg(I.gh, 18)} پشتیبان‌گیری گیتهاب ${ghStatus}</h3><div class="kv"><span>آخرین موفقیت</span><b>${last}</b></div><div class="kv"><span>پوش / پول</span><b>${toFa(gh.pushes || 0)} / ${toFa(gh.pulls || 0)}</b></div>${gh.last_err ? `<div class="kv"><span>آخرین خطا</span><b style="color:var(--rose)">${escapeHtml(gh.last_err).slice(0, 40)}</b></div>` : ""}<div class="modal-actions"><button class="btn sm amber" id="s-backup" ${gh.enabled ? "" : "disabled"} style="flex:1">بکاپ‌گیری الان</button><button class="btn sm ghost" id="s-restore" ${gh.enabled ? "" : "disabled"} style="flex:1">بازیابی از گیتهاب</button></div></div>
      <div class="card"><h3 style="margin-bottom:14px">اطلاعات سرور</h3><div class="kv"><span>دامنه</span><b>${o ? o.host : "—"}</b></div><div class="kv"><span>هسته فعال</span><b>${o && o.core_alive ? "✅" : "❌"}</b></div><div class="kv"><span>Reality آماده</span><b>${o && o.reality_ready ? "✅" : "❌ (TCP Proxy لازم است)"}</b></div><div class="kv"><span>آپتایم</span><b>${o ? fmtUptime(o.uptime) : "—"}</b></div></div>
    </div>`;
  $("#s-pwbtn").onclick = async () => { const err = $("#s-pwerr"); err.textContent = ""; try { await api("/api/password", { method: "POST", body: JSON.stringify({ current: $("#s-cur").value, new: $("#s-new").value }) }); toast("رمز بروزرسانی شد"); $("#s-cur").value = ""; $("#s-new").value = ""; } catch (e) { err.textContent = e.message; } };
  const bk = $("#s-backup"), rs = $("#s-restore");
  if (bk) bk.onclick = async () => { bk.disabled = true; try { await api("/api/backup", { method: "POST" }); toast("بکاپ انجام شد"); } catch (e) { toast(e.message, "bad"); } loadSettings(); };
  if (rs) rs.onclick = async () => { if (!confirm("بازیابی دیتا از گیتهاب؟")) return; try { await api("/api/restore", { method: "POST" }); toast("بازیابی شد"); } catch (e) { toast(e.message, "bad"); } };
}

async function loadLogs() {
  let logs; try { logs = await api("/api/logs"); } catch (e) { return; }
  $("#view-logs").innerHTML = `<div class="card">${logs.length ? logs.map((l) => `<div class="kv"><span style="direction:ltr;font-family:monospace;font-size:12px">${escapeHtml(l.text)}</span><b style="font-size:11px;color:var(--dim)">${new Date(l.ts * 1000).toLocaleTimeString("fa-IR")}</b></div>`).join("") : `<div class="empty">${svg(I.empty, 46)}<div>لاگی ثبت نشده</div></div>`}</div>`;
}

function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function bindCopy() { $$("[data-copy]").forEach((b) => b.onclick = () => copy($("#" + b.dataset.copy).textContent)); }
 $$("[data-close]").forEach((b) => b.onclick = (e) => e.target.closest(".overlay").classList.remove("show"));
 $$(".overlay").forEach((o) => o.onclick = (e) => { if (e.target === o) o.classList.remove("show"); });
 $("#refresh").onclick = () => switchView(current);
 $("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); location.reload(); };
bindCopy();

buildNav();
switchView("dash");
setInterval(() => { if (current === "dash") loadDash(); }, 10000);

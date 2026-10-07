/* Sawazi staff console.
 *
 * Talks to the Sawazi JSON API with the staff member's login token. The server checks every
 * permission; the console only hides what the role cannot use. All data is put on the page as
 * text (never as HTML): member names and narratives come from uploaded files.
 */
"use strict";

const TOKEN_KEY = "sawazi.token";
const state = { token: null, me: null, counts: null };

// ------------------------------------------------------------------ helpers

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

const kesWhole = new Intl.NumberFormat("en-KE", { maximumFractionDigits: 0 });
const kesCents = new Intl.NumberFormat("en-KE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const kes = (v) => `KES ${(Number.isInteger(v || 0) ? kesWhole : kesCents).format(v || 0)}`;
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  if (/T00:00(:00)?$/.test(iso)) return d.toLocaleDateString("en-KE", { day: "numeric", month: "short", year: "numeric" });
  return d.toLocaleString("en-KE", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
const can = (action) => !!state.me && state.me.permissions.includes(action);
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

const KIND = {
  suspense: "Suspense", double_payment: "Possible double payment", third_party: "Paid by someone else",
  large_payment: "Unusually large payment", c2b_mismatch: "M-Pesa callback disagrees with statement",
};
const kindLabel = (k) => KIND[k] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const CHANNEL = { sms: "SMS", call: "Call", guarantor_notice: "Guarantor notice", field_visit: "Field visit", recovery: "Recovery" };

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function detailText(data) {
  if (data && typeof data === "object" && "detail" in data) {
    const d = data.detail;
    if (Array.isArray(d)) return d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ");
    return String(d);
  }
  return typeof data === "string" && data ? data.slice(0, 200) : "Something went wrong";
}

async function api(path, { method = "GET", body, form, params } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let r;
  try {
    r = await fetch(url, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, "Cannot reach the Sawazi server. Check your connection and try again.");
  }
  const isJson = (r.headers.get("content-type") || "").includes("json");
  const data = isJson ? await r.json() : await r.text();
  if (r.status === 401 && state.token) {
    signOut("Your session has ended. Please log in again.");
    throw new ApiError(401, "Session ended");
  }
  if (!r.ok) throw new ApiError(r.status, detailText(data));
  return data;
}

function note(kind, text) { return h("div", { class: `note ${kind}`, role: kind === "err" ? "alert" : "status" }, text); }

/** Run an action with a busy button and show the error next to it. */
async function busy(button, slot, fn) {
  button.disabled = true;
  slot.replaceChildren();
  try { await fn(); }
  catch (e) { if (e.status !== 401) slot.replaceChildren(note("err", e.message)); }
  finally { button.disabled = false; }
}

/** A modal confirmation. Resolves true only on an explicit "yes". */
function confirmDialog(title, body, yesLabel, { danger = false, info = false } = {}) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { "aria-labelledby": "dlg-title" });
    const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    dlg.append(h("div", { class: "dlg" },
      h("h2", { id: "dlg-title" }, title),
      body,
      h("div", { class: "actions" },
        info ? null : h("button", { type: "button", onclick: () => close(false) }, "Cancel"),
        h("button", { type: "button", class: danger ? "primary danger" : "primary", onclick: () => close(true) }, yesLabel))));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(false); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

// ------------------------------------------------------------------ session

function signOut(message) {
  const token = state.token;
  state.token = null; state.me = null; state.counts = null;
  try { sessionStorage.removeItem(TOKEN_KEY); } catch {}
  if (token && !message) fetch("/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
  renderLogin(message);
}

function renderLogin(message) {
  const err = h("div");
  const email = h("input", { type: "email", name: "email", autocomplete: "username", required: true });
  const password = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true });
  const submit = h("button", { type: "submit", class: "primary" }, "Log in");
  const form = h("form", { class: "panel", onsubmit: (e) => {
    e.preventDefault();
    busy(submit, err, async () => {
      const r = await api("/auth/login", { method: "POST", body: { email: email.value, password: password.value } });
      state.token = r.token;
      try { sessionStorage.setItem(TOKEN_KEY, r.token); } catch {}
      await start();
    });
  } },
    h("div", { class: "login-brand" }, h("img", { src: "logo.svg", alt: "", width: "44", height: "44" }),
      h("div", null, h("div", { class: "mark" }, "sawazi"), h("div", { class: "by" }, "BY PESARA"))),
    h("h1", null, "Staff console"),
    message ? note("info", message) : null,
    h("label", null, "Email", email),
    h("label", null, "Password", password),
    err, submit);
  document.getElementById("app").replaceChildren(h("div", { class: "login" }, form));
  email.focus();
}

async function start() {
  try { state.token = state.token || sessionStorage.getItem(TOKEN_KEY); } catch {}
  if (!state.token) return renderLogin();
  try {
    state.me = await api("/auth/me");
  } catch (e) {
    if (e.status !== 401) renderLogin(e.message);
    return;
  }
  if (!location.hash) location.hash = "#/dashboard";
  route();
}

// ------------------------------------------------------------------ shell and routing

const VIEWS = {
  dashboard: { label: "Dashboard", need: "read", render: viewDashboard },
  suspense: { label: "Suspense", need: "read", render: viewSuspense, badge: () => state.counts?.suspense },
  exceptions: { label: "Exceptions", need: "read", render: viewExceptions, badge: () => state.counts?.other },
  collections: { label: "Collections", need: "read", render: viewCollections },
  loans: { label: "Loans", need: "read", render: viewLoans },
  risk: { label: "Risk", need: "read", render: viewRisk },
  upload: { label: "Upload", need: "reconcile", render: viewUpload },
  rules: { label: "Allocation rules", need: "read", render: viewRules },
  products: { label: "Products", need: "read", render: viewProducts },
};

const inst = () => `/institutions/${state.me.institution_id}`;

async function refreshCounts() {
  try {
    const d = await api(`${inst()}/dashboard`);
    const exc = d.open_exceptions || {};
    const suspense = exc.suspense ? exc.suspense.count : 0;
    const total = Object.values(exc).reduce((n, v) => n + v.count, 0);
    state.counts = { suspense, other: total - suspense };
    document.querySelectorAll(".nav a[data-view]").forEach((a) => {
      const v = VIEWS[a.dataset.view];
      const n = v.badge ? v.badge() : 0;
      a.querySelector(".badge")?.remove();
      if (n) a.append(h("span", { class: "badge", "aria-label": `${n} open` }, n));
    });
    return d;
  } catch { return null; }
}

function shell(name, content) {
  const nav = h("nav", { class: "nav", "aria-label": "Sections" },
    Object.entries(VIEWS).filter(([, v]) => can(v.need)).map(([key, v]) => {
      const n = v.badge ? v.badge() : 0;
      return h("a", { href: `#/${key}`, "data-view": key, "aria-current": key === name ? "page" : null },
        v.label, n ? h("span", { class: "badge", "aria-label": `${n} open` }, n) : null);
    }));
  const top = h("header", { class: "top" }, h("div", { class: "top-in" },
    h("a", { class: "brand", href: "#/dashboard", "aria-label": "Sawazi dashboard" },
      h("img", { src: "logo.svg", alt: "", width: "28", height: "28" }), h("span", { class: "mark" }, "sawazi"),
      h("span", { class: "inst" }, state.me.institution_name || "")),
    nav,
    h("div", { class: "who" },
      h("span", null, state.me.name, " ", h("span", { class: "muted" }, `· ${state.me.role.replace("_", " ")}`)),
      h("button", { type: "button", class: "link", onclick: () => signOut() }, "Log out"))));
  document.getElementById("app").replaceChildren(top, h("main", { id: "main" }, content));
}

async function route() {
  if (!state.me) return;
  const [, name, arg] = location.hash.match(/^#\/(\w+)(?:\/(\w+))?/) || [];
  const view = VIEWS[name] && can(VIEWS[name].need) ? name : "dashboard";
  const main = h("div", { class: "stack" }, h("p", { class: "muted" }, "Loading…"));
  await refreshCounts();
  shell(view, main);
  document.title = `${VIEWS[view].label} · Sawazi`;
  try {
    main.replaceChildren(...[].concat(await VIEWS[view].render(arg)).filter(Boolean));
  } catch (e) {
    if (e.status !== 401) main.replaceChildren(note("err", e.message));
  }
}

window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ dashboard

async function viewDashboard() {
  const [d, unconfirmed] = await Promise.all([
    api(`${inst()}/dashboard`),
    api(`${inst()}/c2b/unconfirmed`).catch(() => []),
  ]);
  const p = d.portfolio || {};
  const exc = d.open_exceptions || {};
  const susp = exc.suspense || { count: 0, kes: 0 };
  const tx = d.transactions || {};
  const kpi = (label, value, sub, bad) => h("div", { class: "kpi" },
    h("span", { class: "eyebrow" }, label), h("span", { class: bad ? "v bad" : "v" }, value), h("span", { class: "s" }, sub));
  const kpis = h("div", { class: "kpis" },
    kpi("Matched automatically", d.auto_match_rate === null ? "–" : `${d.auto_match_rate}%`,
        `${(tx.allocated || {}).count || 0} payments allocated`),
    kpi("In suspense", String(susp.count), `${kes(susp.kes)} waiting for a person`, susp.count > 0),
    kpi("Portfolio at risk", `${(p.par_1 ?? 0).toFixed(1)}%`, `PAR 30 ${(p.par_30 ?? 0).toFixed(1)}% · PAR 90 ${(p.par_90 ?? 0).toFixed(1)}%`),
    kpi("In arrears", kes(p.arrears_kes), `${p.active_loans || 0} active loans, ${kes(p.portfolio_kes)} outstanding`));

  const excRows = Object.entries(exc).sort((a, b) => b[1].count - a[1].count);
  const excPanel = h("section", { class: "panel" },
    h("div", { class: "pad stack" }, h("h2", null, "Open items"),
      excRows.length === 0 ? h("p", { class: "muted" }, "Nothing waiting. Every payment is allocated.") :
        h("div", { class: "tbl-wrap" }, h("table", null,
          h("thead", null, h("tr", null, h("th", null, "Kind"), h("th", { class: "n" }, "Count"), h("th", { class: "n" }, "Amount"))),
          h("tbody", null, excRows.map(([k, v]) => h("tr", null,
            h("td", null, h("a", { href: k === "suspense" ? "#/suspense" : "#/exceptions" }, kindLabel(k))),
            h("td", { class: "n" }, v.count), h("td", { class: "n" }, kes(v.kes)))))))));

  const sources = ["mpesa", "bank", "checkoff"].map((src) => {
    const a = tx[`${src}_allocated`] || { count: 0, kes: 0 };
    const s = tx[`${src}_suspense`] || { count: 0, kes: 0 };
    const u = tx[`${src}_unmatched`] || { count: 0, kes: 0 };
    return { src, a, s, u };
  }).filter((r) => r.a.count + r.s.count + r.u.count > 0);
  const LABEL = { mpesa: "M-Pesa", bank: "Bank", checkoff: "Check-off" };
  const txPanel = h("section", { class: "panel" },
    h("div", { class: "pad stack" }, h("h2", null, "Payments by source"),
      sources.length === 0 ? h("p", { class: "muted" }, "No payments yet. ", can("reconcile") ? h("a", { href: "#/upload" }, "Upload a statement") : null, ".") :
        h("div", { class: "tbl-wrap" }, h("table", null,
          h("thead", null, h("tr", null, h("th", null, "Source"), h("th", { class: "n" }, "Allocated"),
            h("th", { class: "n" }, "Suspense"), h("th", { class: "n" }, "Not yet matched"))),
          h("tbody", null, sources.map((r) => h("tr", null, h("td", null, LABEL[r.src]),
            [r.a, r.s, r.u].map((v) => h("td", { class: "n" }, v.count, h("div", { class: "muted small" }, kes(v.kes)))))))))));

  return [
    h("div", { class: "head" }, h("h1", null, "Dashboard"), h("p", { class: "muted" }, d.institution)),
    kpis,
    unconfirmed.length ? note("warn", `${plural(unconfirmed.length, "real-time M-Pesa payment")} not yet on a paybill statement after 48 hours. Upload the latest statement to confirm them.`) : null,
    h("div", { class: "grid2" }, excPanel, txPanel),
  ];
}

// ------------------------------------------------------------------ suspense clearing

async function viewSuspense() {
  const items = await api(`${inst()}/exceptions`, { params: { kind: "suspense" } });
  const total = items.reduce((n, e) => n + e.amount_kes, 0);
  return [
    h("div", { class: "head" },
      h("div", { class: "stack" }, h("h1", null, "Suspense"),
        h("p", { class: "muted" }, "Payments Sawazi could not match with confidence. Check each one and clear it to the right member. Clearing allocates the money and is recorded against your name.")),
      h("span", { class: "num muted" }, `${items.length} · ${kes(total)}`)),
    items.length === 0 ? h("div", { class: "panel empty" }, "No payments in suspense.") :
      h("div", { class: "panel list" }, items.map(suspenseItem)),
  ];
}

function txnFacts(t) {
  if (!t) return null;
  const fact = (k, v) => v ? h("div", null, h("dt", null, k), h("dd", null, v)) : null;
  return h("dl", { class: "kv" },
    fact("Reference", h("span", { class: "num" }, `${t.source.toUpperCase()} ${t.reference}`)),
    fact("Paid", when(t.txn_time)),
    fact("Account typed", t.account_ref),
    fact("Payer", t.payer_name),
    fact("Phone", t.payer_phone ? h("span", { class: "num" }, t.payer_phone) : null),
    fact("Narrative", t.narrative));
}

function suspenseItem(e) {
  const slot = h("div");
  const body = h("div", { class: "item-body" },
    h("div", { class: "item-top" },
      h("span", { class: "eyebrow" }, `Suspense · ${e.severity}`),
      h("span", { class: "amt" }, kes(e.amount_kes))),
    txnFacts(e.transaction),
    h("p", { class: "detail muted" }, e.detail));

  if (can("resolve")) {
    if (e.suggested_member) {
      const m = e.suggested_member;
      body.append(h("div", { class: "suggest" },
        h("span", null, h("span", { class: "muted small" }, "Suggested: "), h("b", null, m.name), " ",
          h("span", { class: "num muted" }, m.member_no)),
        h("button", { type: "button", onclick: (ev) => clearTo(e, m, ev.currentTarget, slot) }, `Clear to ${m.member_no}`)));
    }
    body.append(memberSearch(e, slot));
  }
  body.append(slot);
  return h("article", { class: `item sev-${e.severity}` }, h("span", { class: "stripe" }), body);
}

function memberSearch(e, slot) {
  const results = h("div", { class: "results" });
  const q = h("input", { type: "search", placeholder: "Member number, name, phone or ID number", "aria-label": "Find member" });
  const go = h("button", { type: "submit" }, "Find");
  return h("details", null,
    h("summary", { class: "small" }, e.suggested_member ? "A different member" : "Find the member"),
    h("form", { class: "stack", onsubmit: (ev) => {
      ev.preventDefault();
      busy(go, results, async () => {
        const found = await api(`${inst()}/members`, { params: { q: q.value.trim() } });
        results.replaceChildren(...(found.length ? found.map((m) => h("div", { class: "member" },
          h("span", null, h("b", null, m.name), " ", h("span", { class: "num muted" }, m.member_no),
            m.phone ? h("span", { class: "num muted small" }, ` · ${m.phone}`) : null),
          h("button", { type: "button", onclick: (b) => clearTo(e, m, b.currentTarget, slot) }, "Clear to this member"),
          h("span", { class: "loans" }, m.loans.length ? m.loans.map((l) =>
            `${l.loan_no}: ${kes(l.arrears_kes)} overdue, ${kes(l.balance_kes)} balance`).join(" · ") : "No active loans")))
          : [h("p", { class: "muted small" }, "No member found.")]));
      });
    } }, h("div", { class: "row-form" }, h("label", { class: "sr" }, "Search"), q, go), results));
}

async function clearTo(e, m, button, slot) {
  const noteInput = h("textarea", { maxlength: "300", placeholder: "e.g. member called in and confirmed the payment" });
  const t = e.transaction || {};
  const ok = await confirmDialog("Clear this payment?",
    h("div", { class: "stack" },
      h("p", null, `${kes(e.amount_kes)} (${(t.source || "").toUpperCase()} ${t.reference || ""}) will be allocated to:`),
      h("p", null, h("b", null, m.name), " ", h("span", { class: "num" }, m.member_no)),
      h("p", { class: "muted small" }, "It is split by your institution's allocation rules. This cannot be undone from Sawazi."),
      h("label", null, "Why (recorded in the audit log)", noteInput)),
    `Clear to ${m.member_no}`);
  if (!ok) return;
  await busy(button, slot, async () => {
    const r = await api(`/exceptions/${e.id}/resolve`, { method: "POST", body: { member_no: m.member_no, note: noteInput.value.trim() || null } });
    const split = (r.allocations || []).map((a) => `${kes(a.amount_kes)} to ${(TARGET_LABEL[a.target] || a.target).toLowerCase()}`).join(", ");
    slot.replaceChildren(note("ok", `Cleared to ${m.member_no} ${m.name}: ${split}.`));
    button.closest("article").querySelectorAll("button, details").forEach((x) => { if (x.tagName === "BUTTON") x.disabled = true; else x.remove(); });
    await refreshCounts();
  });
}

// ------------------------------------------------------------------ other exceptions

let excFilter = "all";

async function viewExceptions() {
  const all = (await api(`${inst()}/exceptions`)).filter((e) => e.kind !== "suspense");
  const kinds = [...new Set(all.map((e) => e.kind))];
  if (excFilter !== "all" && !kinds.includes(excFilter)) excFilter = "all";
  const list = h("div", { class: "panel list" });
  const draw = () => {
    const shown = all.filter((e) => excFilter === "all" || e.kind === excFilter);
    list.replaceChildren(...(shown.length ? shown.map(exceptionItem) : [h("div", { class: "empty" }, "Nothing to look at here.")]));
    chips.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === excFilter)));
  };
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter by kind" },
    [["all", `All (${all.length})`], ...kinds.map((k) => [k, `${kindLabel(k)} (${all.filter((e) => e.kind === k).length})`])]
      .map(([k, label]) => h("button", { type: "button", "data-k": k, onclick: () => { excFilter = k; draw(); } }, label)));
  draw();
  return [
    h("div", { class: "head" }, h("div", { class: "stack" }, h("h1", null, "Exceptions"),
      h("p", { class: "muted" }, "Things that deserve a second look: check-off lines that are short, missing or unidentified, possible double payments, payments by someone else, unusually large amounts, and real-time M-Pesa that disagrees with the statement."))),
    chips, list,
  ];
}

function exceptionItem(e) {
  const slot = h("div");
  const body = h("div", { class: "item-body" },
    h("div", { class: "item-top" },
      h("span", { class: "eyebrow" }, `${kindLabel(e.kind)} · ${e.severity}`),
      h("span", { class: "amt" }, kes(e.amount_kes))),
    txnFacts(e.transaction),
    e.suggested_member ? h("p", { class: "small" }, "Member: ", h("b", null, e.suggested_member.name), " ",
      h("span", { class: "num muted" }, e.suggested_member.member_no)) : null,
    h("p", { class: "detail" }, e.detail));
  if (can("resolve")) {
    const reason = h("input", { type: "text", maxlength: "300", placeholder: "What you checked and found", "aria-label": "Reason" });
    const btn = h("button", { type: "submit" }, "Mark resolved");
    body.append(h("form", { class: "row-form", onsubmit: (ev) => {
      ev.preventDefault();
      if (reason.value.trim().length < 3) { slot.replaceChildren(note("err", "Say briefly what you checked; it goes in the audit log.")); return; }
      busy(btn, slot, async () => {
        await api(`/exceptions/${e.id}/resolve`, { method: "POST", body: { note: reason.value.trim() } });
        slot.replaceChildren(note("ok", "Resolved."));
        btn.disabled = true; reason.disabled = true;
        await refreshCounts();
      });
    } }, reason, btn));
  }
  body.append(slot);
  return h("article", { class: `item sev-${e.severity}` }, h("span", { class: "stripe" }), body);
}

// ------------------------------------------------------------------ collections

async function viewCollections() {
  const [queued, failed, sent] = await Promise.all([
    api(`${inst()}/reminders`, { params: { limit: 300 } }),
    api(`${inst()}/reminders`, { params: { limit: 300, status: "failed" } }),
    api(`${inst()}/sms`, { params: { limit: 50 } }),
  ]);
  const slot = h("div");
  const head = h("div", { class: "head" },
    h("div", { class: "stack" }, h("h1", null, "Collections"),
      h("p", { class: "muted" }, "Loans in arrears, ranked by how much acting now recovers. Tick the reminders to send by SMS; nothing goes to a member until you confirm.")));
  if (can("collections")) {
    const rebuild = h("button", { type: "button" }, "Rebuild queue");
    rebuild.addEventListener("click", async () => {
      const ok = await confirmDialog("Rebuild the collections queue?",
        h("p", null, "This replaces the reminders not yet sent with a fresh list from the latest arrears. Reminders already sent stay on record."),
        "Rebuild");
      if (ok) busy(rebuild, slot, async () => { await api(`${inst()}/collections/queue`, { method: "POST" }); route(); });
    });
    head.append(rebuild);
  }
  return [head, slot, queuePanel("To do", queued), failed.length ? queuePanel("Failed to send", failed) : null, sentPanel(sent)];
}

function queuePanel(title, reminders) {
  const canSend = can("send_sms");
  const picked = new Set();
  const slot = h("div", { class: "pad" });
  const count = h("span", { class: "muted small" });
  const sendBtn = h("button", { type: "button", class: "primary", disabled: true }, "Send SMS");
  const update = () => {
    count.textContent = picked.size ? `${plural(picked.size, "reminder")} selected` : "Select reminders to send";
    sendBtn.disabled = picked.size === 0;
  };
  const rows = reminders.map((r) => {
    const box = canSend && r.sms_allowed && r.phone
      ? h("input", { type: "checkbox", "aria-label": `Select ${r.member}`, onchange: (ev) => { ev.target.checked ? picked.add(r.id) : picked.delete(r.id); update(); } })
      : h("span", { title: !r.phone ? "No phone number" : r.sms_allowed ? "" : "Internal note, not sent to members" });
    return h("div", { class: "q" }, box,
      h("div", { class: "score" }, r.priority, h("small", null, "PRIORITY")),
      h("div", null,
        h("div", { class: "item-top" },
          h("span", null, h("b", null, r.member), " ", h("span", { class: "num muted" }, r.member_no)),
          h("span", { class: `pill ch-${r.channel}` }, CHANNEL[r.channel] || r.channel)),
        h("div", { class: "meta num" }, `${r.loan_no} · ${r.days_in_arrears} days · ${kes(r.arrears_kes)} overdue${r.phone ? ` · ${r.phone}` : " · no phone"}`),
        h("details", null, h("summary", { class: "small" }, r.sms_allowed ? "Message" : "Note"), h("div", { class: "msg" }, r.message))));
  });
  sendBtn.addEventListener("click", async () => {
    const chosen = reminders.filter((r) => picked.has(r.id));
    const ok = await confirmDialog(`Send ${plural(chosen.length, "SMS", "SMS")}?`,
      h("div", { class: "stack" },
        h("p", null, `Each member gets their message now. Sawazi skips anyone who opted out or had an SMS for the same loan in the last 3 days.`),
        h("ul", { class: "small" }, chosen.slice(0, 8).map((r) => h("li", null, `${r.member} (${r.member_no}) · ${r.phone}`)),
          chosen.length > 8 ? h("li", { class: "muted" }, `and ${chosen.length - 8} more`) : null)),
      "Send now");
    if (!ok) return;
    await busy(sendBtn, slot, async () => {
      const ids = [...picked];
      const results = [];
      for (let i = 0; i < ids.length; i += 100) {  // the API takes up to 100 at a time
        const r = await api(`${inst()}/reminders/send`, { method: "POST", body: { reminder_ids: ids.slice(i, i + 100) } });
        results.push(...r.results);
      }
      showSendResults(slot, results, reminders);
      picked.clear(); update();
    });
  });
  update();
  return h("section", { class: "panel" },
    h("div", { class: "pad item-top" }, h("h2", null, title), h("span", { class: "num muted" }, plural(reminders.length, "loan"))),
    reminders.length ? h("div", { class: "list" }, rows) : h("div", { class: "empty" }, "Nothing here."),
    slot,
    canSend && reminders.length ? h("div", { class: "bar" }, count, sendBtn) : null);
}

function showSendResults(slot, results, reminders) {
  const byId = Object.fromEntries(reminders.map((r) => [r.id, r]));
  const tally = {};
  results.forEach((r) => { tally[r.result] = (tally[r.result] || 0) + 1; });
  const summary = Object.entries(tally).map(([k, n]) => `${n} ${k}`).join(", ");
  const bad = results.filter((r) => r.result !== "sent" && r.result !== "simulated");
  slot.replaceChildren(
    note(tally.failed || tally.unknown ? "warn" : "ok", `Done: ${summary}.${tally.simulated ? " (Simulation mode: nothing was actually sent.)" : ""}`),
    bad.length ? h("ul", { class: "small" }, bad.map((r) => h("li", null,
      `${(byId[r.reminder_id] || {}).member || `Reminder ${r.reminder_id}`}: ${r.reason || r.detail || r.result}`))) : null,
    h("p", null, h("button", { type: "button", class: "link", onclick: route }, "Refresh the list")));
}

function sentPanel(sent) {
  return h("section", { class: "panel" },
    h("div", { class: "pad" }, h("h2", null, "Recent messages")),
    sent.length === 0 ? h("div", { class: "empty" }, "No SMS sent yet.") :
      h("div", { class: "tbl-wrap" }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, "Approved"), h("th", null, "Phone"), h("th", null, "Status"), h("th", null, "Message"))),
        h("tbody", null, sent.map((m) => h("tr", null,
          h("td", { class: "num" }, when(m.approved_at)),
          h("td", { class: "num" }, m.phone),
          h("td", null, h("span", { class: `pill st-${m.status}` }, m.status),
            m.delivery_status && m.status !== "delivered" ? h("div", { class: "muted small" }, m.delivery_status) : null,
            m.provider_description && ["failed", "unknown"].includes(m.status) ? h("div", { class: "muted small" }, m.provider_description) : null),
          h("td", { class: "small" }, m.body)))))));
}

// ------------------------------------------------------------------ uploads

const IMPORTS = [
  ["mpesa", "M-Pesa paybill statement", "From the M-PESA org portal (CSV)."],
  ["bank", "Bank statement", "Date, narrative, reference and credit columns."],
  ["checkoff_remittance", "Check-off remittance", "What the employer actually paid. Needs employer and month."],
  ["checkoff_schedule", "Check-off schedule", "What was due from the employer. Needs employer and month."],
  ["members", "Members", "Export from the core banking system. Date joined, deposits, share capital and pay columns are read if present."],
  ["core_guarantees", "Guarantees from the core system", "Loan No, Guarantor Member No, Amount Guaranteed. Tick \"complete list\" to release guarantees no longer in the file."],
  ["member_balances", "Member balances or payroll", "Deposits and share capital (monthly), or gross and net pay from payroll, for existing members."],
  ["loans", "Loans", "Export from the core banking system."],
];

function viewUpload() {
  return [
    h("div", { class: "head" }, h("div", { class: "stack" }, h("h1", null, "Upload and run"),
      h("p", { class: "muted" }, "Upload exports in any order. Uploading the same file twice never counts a payment twice."))),
    h("div", { class: "grid2" }, uploadPanel(), h("div", { class: "stack" }, matchPanel(), reconcilePanel())),
  ];
}

function uploadPanel() {
  const kind = h("select", { name: "kind" }, IMPORTS.map(([k, label]) => h("option", { value: k }, label)));
  const help = h("p", { class: "muted small" });
  const file = h("input", { type: "file", accept: ".csv,text/csv", required: true });
  const employer = h("input", { type: "text", placeholder: "e.g. Tumaini Schools Ltd" });
  const period = h("input", { type: "month" });
  const checkoffFields = h("div", { class: "row-form" }, h("label", null, "Employer", employer), h("label", null, "Month", period));
  const complete = h("input", { type: "checkbox" });
  const completeRow = h("label", { class: "check" }, complete, "This is the complete current list (release guarantees not in it)");
  const sync = () => {
    const [, , text] = IMPORTS.find(([k]) => k === kind.value);
    help.textContent = text;
    const co = kind.value.startsWith("checkoff");
    checkoffFields.hidden = !co; employer.required = co; period.required = co;
    completeRow.hidden = kind.value !== "core_guarantees";
  };
  kind.addEventListener("change", sync);
  sync();
  const slot = h("div");
  const btn = h("button", { type: "submit", class: "primary" }, "Upload");
  const form = h("form", { class: "panel pad stack", onsubmit: (e) => {
    e.preventDefault();
    if (!file.files.length) return;
    busy(btn, slot, async () => {
      const fd = new FormData();
      fd.append("file", file.files[0]);
      const params = kind.value.startsWith("checkoff") ? { employer: employer.value.trim(), period: period.value }
        : kind.value === "core_guarantees" ? { replace: complete.checked } : {};
      const r = await api(`${inst()}/import/${kind.value}`, { method: "POST", form: fd, params });
      const parts = r.updated !== undefined ? [`${r.updated} members updated`]
        : [`${r.created} new`, `${r.skipped_duplicates} already in Sawazi`];
      if (r.rejected_count) parts.push(`${plural(r.rejected_count, "row")} need a look (listed below)`);
      if (r.released) parts.push(`${r.released} guarantees no longer in the core system released`);
      if (r.guarantees_released) parts.push(`${r.guarantees_released} guarantees released on repaid loans`);
      if (r.callbacks_confirmed) parts.push(`${r.callbacks_confirmed} real-time payments confirmed`);
      if (r.callbacks_mismatched) parts.push(`${r.callbacks_mismatched} real-time payments DISAGREE with the statement (see Exceptions)`);
      slot.replaceChildren(note(r.callbacks_mismatched || r.rejected_count ? "warn" : "ok", `${file.files[0].name}: ${parts.join(", ")}.`),
        r.rejected.length ? h("ul", { class: "small muted" }, r.rejected.map((x) => h("li", null, x))) : null);
      file.value = "";
    });
  } },
    h("h2", null, "Upload a file"),
    h("label", null, "What is it?", kind), help, checkoffFields, completeRow,
    h("label", null, "CSV file", file), btn, slot);
  return form;
}

function matchPanel() {
  const slot = h("div");
  const btn = h("button", { type: "button", class: "primary" }, "Match payments now");
  btn.addEventListener("click", () => busy(btn, slot, async () => {
    const r = await api(`${inst()}/match`, { method: "POST" });
    slot.replaceChildren(note("ok", r.processed
      ? `${r.processed} payments checked: ${r.allocated || 0} allocated (${kes((r.allocated_cents || 0) / 100)}), ${r.suspense || 0} to suspense, ${r.anomalies || 0} flagged.`
      : "No new payments to match."));
    await refreshCounts();
  }));
  return h("section", { class: "panel pad stack" }, h("h2", null, "Match payments"),
    h("p", { class: "muted small" }, "Finds the member behind every new payment. Anything uncertain goes to suspense for a person to clear. Real-time M-Pesa payments are matched as they arrive."),
    h("div", null, btn), slot);
}

function reconcilePanel() {
  const employer = h("input", { type: "text", required: true, placeholder: "Employer name as uploaded" });
  const period = h("input", { type: "month", required: true });
  const slot = h("div");
  const btn = h("button", { type: "submit" }, "Reconcile");
  return h("form", { class: "panel pad stack", onsubmit: (e) => {
    e.preventDefault();
    busy(btn, slot, async () => {
      const r = await api(`${inst()}/checkoff/reconcile`, { method: "POST", params: { employer: employer.value.trim(), period: period.value } });
      const meter = h("i");
      meter.style.width = `${Math.min(r.collection_rate_pct || 0, 100)}%`;
      const t = (label, n, cls) => h("div", { class: cls }, h("b", null, n || 0), h("span", null, label));
      slot.replaceChildren(h("div", { class: "stack" },
        h("div", { class: "item-top" }, h("b", null, `${r.employer} · ${r.period}`),
          h("span", { class: "num" }, r.collection_rate_pct === null ? "No schedule" : `${r.collection_rate_pct}% collected`)),
        h("div", { class: "meter", role: "img", "aria-label": `${r.collection_rate_pct || 0}% collected` }, meter),
        h("p", { class: "small muted" }, `Expected ${kes(r.expected_kes)} · received ${kes(r.remitted_kes)} · difference ${kes(r.variance_kes)}`),
        h("div", { class: "tally" }, t("Exact", r.exact), t("Short", r.short), t("Missing", r.missing), t("Over", r.over),
          t("Not scheduled", r.not_on_schedule), t("Unknown", r.unidentified))));
    });
  } },
    h("h2", null, "Reconcile check-off"),
    h("p", { class: "muted small" }, "Compares what an employer was scheduled to deduct with what they remitted, member by member."),
    h("div", { class: "row-form" }, h("label", null, "Employer", employer), h("label", null, "Month", period)),
    h("div", null, btn), slot);
}

// ------------------------------------------------------------------ allocation rules

const TARGET_LABEL = {
  loan_penalty: "Penalty", loan_interest: "Interest", loan_principal: "Principal", loan_arrears: "Arrears",
  loan_installment: "Current instalment", deposits: "Deposits", shares: "Share capital",
};
const PART_LABEL = { penalty: "Penalty", interest: "Interest", principal: "Principal" };

async function viewRules() {
  const r = await api(`${inst()}/allocation-rules`);
  const editable = can("allocation_rules");
  const dis = editable ? null : true;

  const loanOrder = h("select", { disabled: dis }, Object.entries(r.choices.loan_order).map(([k, label]) =>
    h("option", { value: k, selected: k === r.loan_order ? true : null }, label)));
  const parts = r.arrears_order.map((p) => h("select", { disabled: dis, "aria-label": "Arrears part" },
    r.choices.arrears_parts.map((x) => h("option", { value: x, selected: x === p ? true : null }, PART_LABEL[x]))));
  const installment = h("input", { type: "checkbox", disabled: dis, checked: r.pay_current_installment ? true : null });

  // Excess: optionally one capped bucket, then the rest to the other target.
  const [first, last] = r.excess.length > 1 ? r.excess : [null, r.excess[0]];
  const split = h("select", { disabled: dis, "aria-label": "Split what is left" },
    h("option", { value: "none" }, `Everything to one account`),
    h("option", { value: "percent" }, "A percentage first"),
    h("option", { value: "fixed" }, "A fixed amount first"));
  split.value = !first ? "none" : first.percent !== null ? "percent" : "fixed";
  const firstTarget = h("select", { disabled: dis, "aria-label": "First account" },
    r.choices.excess_targets.map((x) => h("option", { value: x }, TARGET_LABEL[x])));
  firstTarget.value = first ? first.target : "shares";
  const firstValue = h("input", { type: "number", min: "0.01", step: "0.01", disabled: dis, "aria-label": "Percent or amount",
    value: first ? String(first.percent ?? first.max_kes) : "" });
  const lastTarget = h("select", { disabled: dis, "aria-label": "Account for the rest" },
    r.choices.excess_targets.map((x) => h("option", { value: x }, TARGET_LABEL[x])));
  lastTarget.value = last.target;
  const firstRow = h("div", { class: "row-form" }, h("label", null, "Account", firstTarget),
    h("label", null, "Percent / KES", firstValue));
  const syncSplit = () => { firstRow.hidden = split.value === "none"; };
  split.addEventListener("change", syncSplit);
  syncSplit();

  const draft = () => ({
    loan_order: loanOrder.value,
    arrears_order: parts.map((p) => p.value),
    pay_current_installment: installment.checked,
    excess: split.value === "none" ? [{ target: lastTarget.value }] : [
      split.value === "percent" ? { target: firstTarget.value, percent: Math.round(Number(firstValue.value)) }
        : { target: firstTarget.value, max_kes: firstValue.value },
      { target: lastTarget.value }],
  });

  const saveSlot = h("div");
  const save = h("button", { type: "submit", class: "primary" }, "Save rules");
  const form = h("form", { class: "panel pad stack", onsubmit: async (e) => {
    e.preventDefault();
    const d = draft();
    const ok = await confirmDialog("Save these allocation rules?",
      h("div", { class: "stack" },
        h("p", null, "Every payment allocated from now on will be split this way, including suspense you clear by hand. Payments already allocated are not changed."),
        h("p", { class: "muted small" }, "Try a few members in the preview first if you have not.")),
      "Save rules");
    if (!ok) return;
    busy(save, saveSlot, async () => {
      await api(`${inst()}/allocation-rules`, { method: "PUT", body: d });
      saveSlot.replaceChildren(note("ok", "Saved. New payments will be split this way."));
    });
  } },
    h("h2", null, "How a payment is split"),
    h("div", { class: "steps" },
      h("label", null, "1. Which loan first", loanOrder),
      h("div", { class: "stack" }, h("span", null, h("b", null, "2. Arrears on each loan, in this order")),
        h("div", { class: "row-form" }, parts),
        h("p", { class: "muted small" }, "Uses the penalty and interest arrears from your loans export. Loans without that breakdown are paid as one arrears amount.")),
      h("label", { class: "check" }, installment, "3. Then pay the current instalment on each loan"),
      h("div", { class: "stack" }, h("span", null, h("b", null, "4. What is left")), split, firstRow,
        h("label", null, "The rest goes to", lastTarget))),
    editable ? h("div", { class: "actions" }, save) : h("p", { class: "muted small" }, "Only an admin can change these rules."),
    saveSlot,
    h("p", { class: "muted small" }, r.is_default ? "These are Sawazi's default rules." : `Last changed ${when(r.updated_at)}.`));

  return [
    h("div", { class: "head" }, h("div", { class: "stack" }, h("h1", null, "Allocation rules"),
      h("p", { class: "muted" }, "How each matched payment is shared across a member's loans, deposits and shares. Sawazi does not calculate interest or penalties: it uses the figures from your core system."))),
    h("div", { class: "grid2" }, form, previewPanel(draft)),
  ];
}

function previewPanel(draft) {
  const memberNo = h("input", { type: "text", required: true, placeholder: "e.g. UT00104" });
  const amount = h("input", { type: "number", min: "1", step: "0.01", required: true, placeholder: "KES" });
  const slot = h("div");
  const btn = h("button", { type: "submit" }, "Preview");
  return h("form", { class: "panel pad stack", onsubmit: (e) => {
    e.preventDefault();
    busy(btn, slot, async () => {
      const r = await api(`${inst()}/allocation-rules/preview`, { method: "POST",
        body: { member_no: memberNo.value.trim(), amount_kes: amount.value, rules: draft() } });
      const before = Object.fromEntries(r.loans_before.map((l) => [l.loan_no, l]));
      slot.replaceChildren(h("div", { class: "stack" },
        h("p", null, h("b", null, r.member), " ", h("span", { class: "num muted" }, r.member_no), ` paying ${kes(r.amount_kes)}:`),
        h("div", { class: "tbl-wrap" }, h("table", null,
          h("thead", null, h("tr", null, h("th", null, "Goes to"), h("th", null, "Loan"), h("th", { class: "n" }, "Amount"))),
          h("tbody", null, r.lines.map((x) => h("tr", null, h("td", null, TARGET_LABEL[x.target] || x.target),
            h("td", { class: "num" }, x.loan_no || ""), h("td", { class: "n" }, kes(x.amount_kes))))))),
        r.loans_after.length ? h("p", { class: "muted small" }, r.loans_after.map((l) =>
          `${l.loan_no}: arrears ${kes(before[l.loan_no].arrears_kes)} → ${kes(l.arrears_kes)}`).join(" · ")) : null,
        h("p", { class: "muted small" }, "Preview only: nothing was changed.")));
    });
  } },
    h("h2", null, "Preview"),
    h("p", { class: "muted small" }, "See how a payment would be split with the rules on the left, before saving them."),
    h("div", { class: "row-form" }, h("label", null, "Member number", memberNo), h("label", null, "Amount", amount)),
    h("div", null, btn), slot);
}

// ------------------------------------------------------------------ loans

const APP_STATUS = {
  draft: "Draft", submitted: "Waiting for decision", approved: "Approved", declined: "Declined",
  withdrawn: "Withdrawn", exported: "Sent to core system", disbursed: "Disbursed",
};
const CHECK_LABEL = {
  amount: "Amount", term: "Term", membership: "Membership", arrears: "Existing arrears", deposits: "Deposits",
  one_third: "One-third take-home", guarantors: "Guarantors",
};
const CHECK_MARK = { pass: "✓", fail: "✕", warn: "!", unknown: "?", pending: "…" };
const OUTCOME = { passes: ["Passes every check", "ok"], fails: ["Fails a check", "err"], incomplete: ["Not complete yet", "warn"] };
let loanFilter = "open";

async function viewLoans(id) {
  if (id) return viewApplication(Number(id));
  const filters = { open: "draft,submitted", approved: "approved", done: "exported,disbursed", closed: "declined,withdrawn", all: "" };
  const apps = await api(`${inst()}/loan-applications`, { params: { status: filters[loanFilter], limit: 300 } });
  const head = h("div", { class: "head" }, h("div", { class: "stack" }, h("h1", null, "Loans"),
    h("p", { class: "muted" }, "Applications from capture to hand-over. Sawazi appraises and records each decision; the core system disburses.")));
  const actions = h("div", { class: "actions" });
  if (can("loan_apply")) actions.append(h("button", { type: "button", class: "primary", onclick: () => newApplication(head) }, "New application"));
  if (can("loan_export")) {
    const slot = h("div");
    const btn = h("button", { type: "button" }, "Send approved loans to core");
    btn.addEventListener("click", async () => {
      const ok = await confirmDialog("Hand approved loans to the core system?",
        h("p", null, "Downloads a file of every approved loan not yet sent, for the core system to disburse. Each loan is sent once and marked \"Sent to core system\"."),
        "Download file");
      if (ok) busy(btn, slot, async () => { await downloadExport(); route(); });
    });
    actions.append(btn, slot);
  }
  head.append(actions);
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter applications" },
    [["open", "Open"], ["approved", "Approved"], ["done", "Sent / disbursed"], ["closed", "Declined / withdrawn"], ["all", "All"]]
      .map(([k, label]) => h("button", { type: "button", "aria-pressed": String(k === loanFilter), onclick: () => { loanFilter = k; route(); } }, label)));
  const rows = apps.map((a) => h("tr", null,
    h("td", null, h("a", { href: `#/loans/${a.id}` }, `SWZ-${a.id}`)),
    h("td", null, a.member, " ", h("span", { class: "num muted" }, a.member_no)),
    h("td", null, a.product),
    h("td", { class: "n" }, kes(a.amount_kes)),
    h("td", null, h("span", { class: `pill app-${a.status}` }, APP_STATUS[a.status] || a.status)),
    h("td", null, a.appraisal_outcome ? h("span", { class: `pill out-${a.appraisal_outcome}` }, OUTCOME[a.appraisal_outcome][0]) : h("span", { class: "muted small" }, "not submitted")),
    h("td", { class: "small muted" }, when(a.submitted_at || a.created_at))));
  return [head, chips, h("section", { class: "panel" }, apps.length === 0 ? h("div", { class: "empty" }, "No applications here.") :
    h("div", { class: "tbl-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Ref"), h("th", null, "Member"), h("th", null, "Product"), h("th", { class: "n" }, "Amount"),
        h("th", null, "Status"), h("th", null, "Appraisal"), h("th", null, "Date"))),
      h("tbody", null, rows))))];
}

async function downloadExport() {
  const r = await fetch(`${inst()}/loan-applications/export.csv`, { method: "POST", headers: { Authorization: `Bearer ${state.token}` } });
  if (!r.ok) throw new ApiError(r.status, "Could not create the file");
  const blob = await r.blob();
  const name = (r.headers.get("content-disposition") || "").match(/filename=([\w.-]+)/)?.[1] || "sawazi_loans.csv";
  const a = h("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
}

async function newApplication(anchor) {
  const products = (await api(`${inst()}/loan-products`)).filter((p) => p.active);
  if (!products.length) {
    anchor.after(note("warn", can("loan_products") ? "Create a loan product first (Products)." : "No loan products yet. Ask an admin to create one."));
    return;
  }
  const memberNo = h("input", { type: "text", required: true, placeholder: "e.g. UT00104" });
  const product = h("select", null, products.map((p) => h("option", { value: p.id }, `${p.code} · ${p.name}`)));
  const amount = h("input", { type: "number", min: "1", step: "0.01", required: true });
  const term = h("input", { type: "number", min: "1", max: "240", required: true });
  const purpose = h("input", { type: "text", maxlength: "500", placeholder: "What the loan is for" });
  const limits = h("p", { class: "muted small" });
  const sync = () => {
    const p = products.find((x) => String(x.id) === product.value);
    limits.textContent = `${kes(p.min_amount_kes)} to ${kes(p.max_amount_kes)}, up to ${p.max_term_months} months at ${p.interest_rate_pct}% a year.`;
    term.max = p.max_term_months;
  };
  product.addEventListener("change", sync); sync();
  const slot = h("div");
  const go = h("button", { type: "submit", class: "primary" }, "Create draft");
  const dlg = h("dialog", { "aria-labelledby": "new-app" });
  const close = () => { dlg.close(); dlg.remove(); };
  dlg.append(h("form", { class: "dlg", onsubmit: (e) => {
    e.preventDefault();
    busy(go, slot, async () => {
      const a = await api(`${inst()}/loan-applications`, { method: "POST",
        body: { member_no: memberNo.value.trim(), product_id: Number(product.value), amount_kes: amount.value,
                term_months: Number(term.value), purpose: purpose.value.trim() || null } });
      close();
      location.hash = `#/loans/${a.id}`;
    });
  } },
    h("h2", { id: "new-app" }, "New loan application"),
    h("label", null, "Member number", memberNo),
    h("label", null, "Product", product), limits,
    h("div", { class: "row-form" }, h("label", null, "Amount (KES)", amount), h("label", null, "Months", term)),
    h("label", null, "Purpose", purpose), slot,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: close }, "Cancel"), go)));
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  document.body.append(dlg);
  dlg.showModal();
  memberNo.focus();
}

function appraisalPanel(ap) {
  if (!ap) return h("section", { class: "panel pad" }, h("p", { class: "muted" }, "Not appraised yet."));
  const [label, kind] = OUTCOME[ap.outcome];
  return h("section", { class: "panel pad stack" },
    h("div", { class: "item-top" }, h("h2", null, "Appraisal"), h("span", { class: `pill out-${ap.outcome}` }, label)),
    h("ul", { class: "checks" }, ap.checks.map((c) => h("li", { class: `ck ck-${c.status}` },
      h("span", { class: "ck-mark", "aria-hidden": "true" }, CHECK_MARK[c.status]),
      h("span", null, h("b", null, CHECK_LABEL[c.code] || c.code), h("span", { class: "sr" }, ` (${c.status})`), ": ", c.message)))),
    h("dl", { class: "kv" },
      h("div", null, h("dt", null, "Estimated instalment"), h("dd", { class: "num" }, kes(ap.instalment_kes))),
      h("div", null, h("dt", null, "Largest amount that qualifies"), h("dd", { class: "num" }, kes(ap.max_eligible_kes),
        ap.max_eligible_partial ? h("div", { class: "muted small" }, `could be lower: ${ap.unknown_limits.map((k) => ({ affordability: "pay", deposits: "deposits" }[k] || k)).join(", ")} not known`) : null)),
      h("div", null, h("dt", null, "Guarantor cover"), h("dd", { class: "num" }, `${kes(ap.accepted_cover_kes)} of ${kes(ap.required_cover_kes)}`))),
    kind === "ok" ? null : h("p", { class: "muted small" }, "The instalment is an estimate for affordability only; the core system's schedule is the real one."));
}

async function viewApplication(id) {
  const [a, gs] = await Promise.all([api(`${inst()}/loan-applications/${id}`), api(`${inst()}/loan-applications/${id}/guarantors`)]);
  const open = a.status === "draft" || a.status === "submitted";
  const head = h("div", { class: "head" },
    h("div", { class: "stack" },
      h("p", null, h("a", { href: "#/loans" }, "← Loans")),
      h("h1", null, `SWZ-${a.id}: ${a.member}`),
      h("p", { class: "muted" }, `${a.product_name} · ${kes(a.amount_kes)} over ${a.term_months} months`, a.purpose ? ` · ${a.purpose}` : "")),
    h("span", { class: `pill app-${a.status}` }, APP_STATUS[a.status] || a.status));
  const left = h("div", { class: "stack" }, appraisalPanel(a.appraisal), guarantorPanel(a, gs, open));
  const right = h("div", { class: "stack" }, actionPanel(a), historyPanel(a));
  return [head, a.override_reason ? note("warn", `Approved with exceptions: ${a.override_reason}`) : null, h("div", { class: "grid2" }, left, right)];
}

function guarantorPanel(a, gs, open) {
  const slot = h("div");
  const rows = gs.map((g) => h("tr", null,
    h("td", null, g.name, " ", h("span", { class: "num muted" }, g.member_no)),
    h("td", { class: "n" }, kes(g.amount_kes)),
    h("td", null, h("span", { class: `pill gs-${g.status}` }, g.status)),
    h("td", null, open && can("loan_apply") && (g.status === "requested" || g.status === "accepted")
      ? h("button", { type: "button", class: "link", onclick: async (e) => {
          const ok = await confirmDialog(`Cancel ${g.name}'s guarantee?`, h("p", null, g.status === "accepted" ? "They accepted; cancelling releases their deposits." : "Their link will stop working."), "Cancel guarantee", { danger: true });
          if (ok) busy(e.currentTarget, slot, async () => {
            await api(`${inst()}/loan-applications/${a.id}/guarantors/${g.id}/cancel`, { method: "POST", params: { note: "cancelled by staff in the console" } });
            route();
          });
        } }, "Cancel") : null)));
  const panel = h("section", { class: "panel pad stack" }, h("h2", null, "Guarantors"),
    gs.length ? h("div", { class: "tbl-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Guarantor"), h("th", { class: "n" }, "Amount"), h("th", null, "Answer"), h("th", null, ""))),
      h("tbody", null, rows))) : h("p", { class: "muted small" }, "No guarantors asked yet."));
  if (open && can("loan_apply")) {
    const memberNo = h("input", { type: "text", required: true, placeholder: "Member number", "aria-label": "Guarantor member number" });
    const amount = h("input", { type: "number", min: "1", step: "0.01", required: true, placeholder: "KES", "aria-label": "Amount to guarantee" });
    const info = h("p", { class: "muted small" });
    memberNo.addEventListener("change", async () => {
      info.textContent = "";
      if (memberNo.value.trim().length < 2) return;
      try {
        const x = await api(`${inst()}/members/${encodeURIComponent(memberNo.value.trim())}/guarantor-exposure`);
        info.textContent = x.free_kes === null ? `${x.name}: deposits not known, so they cannot guarantee yet.`
          : `${x.name} can guarantee up to ${kes(x.free_kes)} (deposits ${kes(x.deposits_kes)}, already guaranteeing ${kes(x.pledged_kes)}).`;
      } catch { info.textContent = "No member with that number."; }
    });
    const btn = h("button", { type: "submit" }, "Ask by SMS");
    panel.append(h("form", { class: "stack", onsubmit: (e) => {
      e.preventDefault();
      busy(btn, slot, async () => {
        const r = await api(`${inst()}/loan-applications/${a.id}/guarantors`, { method: "POST", body: { member_no: memberNo.value.trim(), amount_kes: amount.value } });
        slot.replaceChildren(note("ok", `${r.name} has been sent a link to accept or decline${r.sms === "simulated" ? " (simulation: no SMS actually sent)" : ""}.`));
        setTimeout(route, 1200);
      });
    } }, h("div", { class: "row-form" }, memberNo, amount, btn), info));
  }
  panel.append(slot);
  return panel;
}

function actionPanel(a) {
  const slot = h("div");
  const panel = h("section", { class: "panel pad stack" }, h("h2", null, "Next step"));
  const mine = state.me.id === a.prepared_by_user_id;
  const decided = a.decisions.some((d) => d.user_id === state.me.id);
  if (a.status === "draft" && can("loan_apply")) {
    const submit = h("button", { type: "button", class: "primary" }, "Submit for decision");
    submit.addEventListener("click", () => busy(submit, slot, async () => {
      await api(`${inst()}/loan-applications/${a.id}/submit`, { method: "POST" }); route();
    }));
    panel.append(h("p", { class: "muted small" }, "Submitting sends it to the approvers with the appraisal as it stands. Ask guarantors before or after."),
      h("div", { class: "actions" }, submit, withdrawButton(a, slot)));
  } else if (a.status === "submitted") {
    panel.append(h("p", null, `Waiting for ${a.approvals_needed === 2 ? "two approvers" : "an approver"}`,
      a.decisions.length ? ` (${a.decisions.filter((d) => d.decision === "approve").length} approved so far).` : "."));
    if (can("loan_approve") && !mine && !decided) panel.append(decisionForm(a, slot));
    else if (can("loan_approve") && mine) panel.append(note("info", "You prepared this application, so another approver must decide."));
    if (can("loan_apply")) panel.append(h("div", { class: "actions" }, withdrawButton(a, slot)));
  } else {
    const text = { approved: "Approved. An accountant sends it to the core system for disbursement.",
      exported: "Sent to the core system. It is marked disbursed when the loan appears in the next loans upload.",
      disbursed: `Disbursed${a.disbursed_at ? ` (seen ${when(a.disbursed_at)})` : ""}.`,
      declined: "Declined. Its guarantors have been released.", withdrawn: "Withdrawn. Its guarantors have been released." };
    panel.append(h("p", null, text[a.status] || a.status));
  }
  panel.append(slot);
  return panel;
}

function withdrawButton(a, slot) {
  const btn = h("button", { type: "button", class: "danger" }, "Withdraw");
  btn.addEventListener("click", async () => {
    const why = h("input", { type: "text", required: true, minlength: "3", placeholder: "e.g. member changed their mind" });
    const ok = await confirmDialog("Withdraw this application?", h("div", { class: "stack" },
      h("p", null, "It stops here and any guarantors are released."), h("label", null, "Why (recorded)", why)), "Withdraw", { danger: true });
    if (!ok) return;
    if (why.value.trim().length < 3) { slot.replaceChildren(note("err", "Give a short reason to withdraw.")); return; }
    busy(btn, slot, async () => {
      await api(`${inst()}/loan-applications/${a.id}/withdraw`, { method: "POST", params: { note: why.value.trim() } }); route();
    });
  });
  return btn;
}

function decisionForm(a, slot) {
  const passes = a.appraisal_outcome === "passes";
  const noteIn = h("textarea", { maxlength: "1000", placeholder: "Committee notes (recorded)" });
  const override = h("textarea", { maxlength: "1000", placeholder: "Why approve although a check failed or is unknown (at least 10 characters)" });
  const approve = h("button", { type: "button", class: "primary" }, "Approve");
  const decline = h("button", { type: "button", class: "danger" }, "Decline");
  const send = (decision) => busy(decision === "approve" ? approve : decline, slot, async () => {
    const body = { decision, note: noteIn.value.trim() || null };
    if (decision === "approve" && !passes) body.override_reason = override.value.trim();
    await api(`${inst()}/loan-applications/${a.id}/decide`, { method: "POST", body });
    route();
  });
  approve.addEventListener("click", async () => {
    const ok = await confirmDialog(`Approve ${kes(a.amount_kes)} for ${a.member}?`,
      h("p", null, passes ? "The appraisal passes every check." : "The appraisal does not pass. Your override reason will be recorded and the loan flagged as approved with exceptions."), "Approve");
    if (ok) send("approve");
  });
  decline.addEventListener("click", async () => {
    const ok = await confirmDialog("Decline this application?", h("p", null, "One decline ends it, and its guarantors are released."), "Decline", { danger: true });
    if (ok) send("decline");
  });
  return h("div", { class: "stack" },
    h("label", null, "Notes", noteIn),
    passes ? null : h("label", null, "Override reason (needed to approve)", override),
    h("div", { class: "actions" }, approve, decline));
}

function historyPanel(a) {
  const rows = [["Created", a.created_at], ["Submitted", a.submitted_at], ["Decided", a.decided_at], ["Sent to core", a.exported_at], ["Disbursed", a.disbursed_at]]
    .filter(([, t]) => t).map(([k, t]) => h("li", null, `${k}: ${when(t)}`));
  return h("section", { class: "panel pad stack" }, h("h2", null, "History"),
    h("ul", { class: "small" }, rows),
    a.decisions.length ? h("ul", { class: "small" }, a.decisions.map((d) =>
      h("li", null, `${d.decision === "approve" ? "Approved" : "Declined"} ${when(d.at)} (appraisal: ${d.appraisal_outcome})${d.note ? `: ${d.note}` : ""}`))) : null,
    h("p", { class: "muted small" }, "Every step is in the audit log."));
}

// ------------------------------------------------------------------ loan products

async function viewProducts(id) {
  const products = await api(`${inst()}/loan-products`);
  const editable = can("loan_products");
  const list = h("section", { class: "panel" }, products.length === 0 ? h("div", { class: "empty" }, "No loan products yet.") :
    h("div", { class: "tbl-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "Code"), h("th", null, "Name"), h("th", { class: "n" }, "Max amount"), h("th", { class: "n" }, "Max months"),
        h("th", { class: "n" }, "Rate"), h("th", { class: "n" }, "Deposits ×"), h("th", null, "Guarantors"), h("th", null, ""))),
      h("tbody", null, products.map((p) => h("tr", null, h("td", { class: "num" }, p.code), h("td", null, p.name, p.active ? "" : h("span", { class: "muted small" }, " (inactive)")),
        h("td", { class: "n" }, kes(p.max_amount_kes)), h("td", { class: "n" }, p.max_term_months), h("td", { class: "n" }, `${p.interest_rate_pct}%`),
        h("td", { class: "n" }, p.deposits_multiplier || "off"), h("td", null, { above_deposits: "Above deposits", full: "Full amount", none: "None" }[p.guarantor_cover]),
        h("td", null, editable ? h("a", { href: `#/products/${p.id}` }, "Edit") : null)))))));
  const current = id ? products.find((p) => p.id === Number(id)) : null;
  return [
    h("div", { class: "head" }, h("div", { class: "stack" }, h("h1", null, "Loan products"),
      h("p", { class: "muted" }, "Each product carries the rules its applications are appraised against."))),
    h("div", { class: "grid2" }, list, editable ? productForm(current) : null),
  ];
}

function productForm(p) {
  const d = p || { code: "", name: "", active: true, min_amount_kes: 1000, max_amount_kes: "", max_term_months: 36, interest_rate_pct: 12,
    interest_method: "reducing", deposits_multiplier: 3, min_membership_months: 6, max_arrears_days: 30, one_third_rule: true,
    guarantor_cover: "above_deposits", min_guarantors: 0, second_approval_above_kes: null };
  const f = {
    code: h("input", { type: "text", required: true, maxlength: "20", value: d.code }),
    name: h("input", { type: "text", required: true, maxlength: "100", value: d.name }),
    min_amount_kes: h("input", { type: "number", min: "1", step: "0.01", required: true, value: String(d.min_amount_kes) }),
    max_amount_kes: h("input", { type: "number", min: "1", step: "0.01", required: true, value: String(d.max_amount_kes) }),
    max_term_months: h("input", { type: "number", min: "1", max: "240", required: true, value: String(d.max_term_months) }),
    interest_rate_pct: h("input", { type: "number", min: "0", max: "100", step: "0.01", required: true, value: String(d.interest_rate_pct) }),
    interest_method: h("select", null, h("option", { value: "reducing" }, "Reducing balance"), h("option", { value: "flat" }, "Flat rate")),
    deposits_multiplier: h("input", { type: "number", min: "0", max: "20", step: "0.01", value: String(d.deposits_multiplier) }),
    min_membership_months: h("input", { type: "number", min: "0", max: "120", value: String(d.min_membership_months) }),
    max_arrears_days: h("input", { type: "number", min: "0", max: "365", value: String(d.max_arrears_days) }),
    guarantor_cover: h("select", null, h("option", { value: "above_deposits" }, "Amount above own deposits"), h("option", { value: "full" }, "Full amount"), h("option", { value: "none" }, "None")),
    min_guarantors: h("input", { type: "number", min: "0", max: "10", value: String(d.min_guarantors) }),
    second_approval_above_kes: h("input", { type: "number", min: "1", step: "0.01", value: d.second_approval_above_kes === null ? "" : String(d.second_approval_above_kes), placeholder: "Leave empty for one approver" }),
  };
  f.interest_method.value = d.interest_method; f.guarantor_cover.value = d.guarantor_cover;
  const oneThird = h("input", { type: "checkbox", checked: d.one_third_rule ? true : null });
  const active = h("input", { type: "checkbox", checked: d.active ? true : null });
  const slot = h("div");
  const save = h("button", { type: "submit", class: "primary" }, p ? "Save changes" : "Create product");
  const L = (label, el) => h("label", null, label, el);
  return h("form", { class: "panel pad stack", onsubmit: (e) => {
    e.preventDefault();
    busy(save, slot, async () => {
      const body = { code: f.code.value.trim(), name: f.name.value.trim(), active: active.checked,
        min_amount_kes: f.min_amount_kes.value, max_amount_kes: f.max_amount_kes.value, max_term_months: Number(f.max_term_months.value),
        interest_rate_pct: f.interest_rate_pct.value, interest_method: f.interest_method.value,
        deposits_multiplier: f.deposits_multiplier.value || 0, min_membership_months: Number(f.min_membership_months.value || 0),
        max_arrears_days: Number(f.max_arrears_days.value || 0), one_third_rule: oneThird.checked, guarantor_cover: f.guarantor_cover.value,
        min_guarantors: Number(f.min_guarantors.value || 0), second_approval_above_kes: f.second_approval_above_kes.value || null };
      await api(`${inst()}/loan-products${p ? `/${p.id}` : ""}`, { method: p ? "PUT" : "POST", body });
      location.hash = "#/products"; route();
    });
  } },
    h("h2", null, p ? `Edit ${p.code}` : "New product"),
    h("div", { class: "row-form" }, L("Code", f.code), L("Name", f.name)),
    h("div", { class: "row-form" }, L("Min amount (KES)", f.min_amount_kes), L("Max amount (KES)", f.max_amount_kes)),
    h("div", { class: "row-form" }, L("Max months", f.max_term_months), L("Rate % a year", f.interest_rate_pct), L("Method", f.interest_method)),
    h("p", { class: "muted small" }, "The rate only estimates the instalment for the affordability check. The core system's schedule is the real one."),
    h("div", { class: "row-form" }, L("Deposits multiplier (0 = off)", f.deposits_multiplier), L("Months of membership", f.min_membership_months), L("Max days in arrears", f.max_arrears_days)),
    h("label", { class: "check" }, oneThird, "One-third take-home rule"),
    h("div", { class: "row-form" }, L("Guarantor cover", f.guarantor_cover), L("Minimum guarantors", f.min_guarantors)),
    L("Two approvers above (KES)", f.second_approval_above_kes),
    h("label", { class: "check" }, active, "Active (new applications can use it)"),
    slot, h("div", { class: "actions" }, p ? h("a", { href: "#/products", class: "btn" }, "Cancel") : null, save));
}

// ------------------------------------------------------------------ risk and exposure

const FLAG_LABEL = {
  guarantor_in_arrears: "Guarantor is behind", over_pledged: "Pledged above deposits", many_guarantees: "Backs many loans",
  mutual_guarantee: "Guarantee each other", guarantee_circle: "Guarantee circle", chain_default: "Defaulter's guarantors also behind",
  concentration: "Large borrower",
};
const CLASS_LABEL = { performing: "Performing", watch: "Watch", substandard: "Substandard", doubtful: "Doubtful", loss: "Loss" };
const pct = (v) => `${(v || 0).toFixed(1)}%`;

async function viewRisk() {
  const r = await api(`${inst()}/risk`);
  const p = r.portfolio, g = r.guarantors;
  const kpi = (label, value, sub, bad) => h("div", { class: "kpi" },
    h("span", { class: "eyebrow" }, label), h("span", { class: bad ? "v bad" : "v" }, value), h("span", { class: "s" }, sub));
  const high = r.flags.filter((f) => f.severity === "high").length;
  const kpis = h("div", { class: "kpis" },
    kpi("Provision needed", kes(p.provision_kes), `on ${kes(p.balance_kes)} outstanding, ${p.loans} loans`),
    kpi("PAR 30", pct(p.par30_pct), `PAR 1 ${pct(p.par1_pct)} · PAR 90 ${pct(p.par90_pct)}`, p.par30_pct > 5),
    kpi("Guaranteed", kes(g.pledged_kes), `${g.members_guaranteeing} members guaranteeing`),
    kpi("Guarantees on loans behind", kes(g.on_loans_behind_kes), "what recovery may fall on guarantors", g.on_loans_behind_kes > 0),
    kpi("Flags", String(r.flags.length), high ? `${high} high` : "none high", high > 0));

  const counts = {};
  r.flags.forEach((f) => { counts[f.kind] = (counts[f.kind] || 0) + 1; });
  let showing = "all";
  const list = h("div", { class: "list" });
  const chips = h("div", { class: "chips pad", role: "group", "aria-label": "Filter flags" });
  const draw = () => {
    const shown = r.flags.filter((f) => showing === "all" || f.kind === showing);
    list.replaceChildren(...(shown.length ? shown.slice(0, 100).map(flagItem) : [h("div", { class: "empty" }, "No warning signs in the portfolio or the guarantor network.")]));
    chips.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === showing)));
  };
  chips.append(...[["all", `All (${r.flags.length})`], ...Object.entries(counts).map(([k, n]) => [k, `${FLAG_LABEL[k] || k} (${n})`])]
    .map(([k, label]) => h("button", { type: "button", "data-k": k, onclick: () => { showing = k; draw(); } }, label)));
  const flagItem = (f) => h("article", { class: `item sev-${f.severity}` },
        h("span", { class: "stripe" }),
        h("div", { class: "item-body" },
          h("div", { class: "item-top" }, h("span", { class: "eyebrow" }, `${FLAG_LABEL[f.kind] || f.kind} · ${f.severity}`),
            f.amount_kes ? h("span", { class: "amt" }, kes(f.amount_kes)) : null),
          h("p", { class: "detail" }, f.message),
          h("div", { class: "actions" }, f.member_nos.map((no) => h("button", { type: "button", class: "link small",
            onclick: () => showExposure(no) }, `${no}: guarantees`)))));
  draw();
  const flagList = h("section", { class: "panel" },
    h("div", { class: "pad item-top" }, h("h2", null, "Look at these"), h("span", { class: "muted small" }, "Prompts to check, not conclusions")),
    r.flags.length ? chips : null, list);

  const table = (title, head, rows) => h("section", { class: "panel" }, h("div", { class: "pad" }, h("h2", null, title)),
    h("div", { class: "tbl-wrap" }, h("table", null, h("thead", null, h("tr", null, head.map(([t, n]) => h("th", { class: n ? "n" : null }, t)))),
      h("tbody", null, rows))));
  const classTable = table("Loan classification and provisioning",
    [["Class"], ["Days behind"], ["Loans", 1], ["Outstanding", 1], ["Rate", 1], ["Provision", 1]],
    r.classification.map((c) => h("tr", null, h("td", null, CLASS_LABEL[c.class]), h("td", { class: "num" }, c.days),
      h("td", { class: "n" }, c.loans), h("td", { class: "n" }, kes(c.balance_kes)), h("td", { class: "n" }, pct(c.provision_pct)),
      h("td", { class: "n" }, kes(c.provision_kes)))));
  const parTable = (title, rows) => table(title, [["Name"], ["Loans", 1], ["Outstanding", 1], ["More than 30 days behind", 1], ["PAR 30", 1]],
    rows.slice(0, 15).map((x) => h("tr", null, h("td", null, x.name), h("td", { class: "n" }, x.loans), h("td", { class: "n" }, kes(x.balance_kes)),
      h("td", { class: "n" }, kes(x.at_risk_kes)), h("td", { class: "n" }, pct(x.par_pct)))));
  const topBorrowers = table(`Largest borrowers (top 10 hold ${pct(r.concentration.top10_pct)})`, [["Member"], ["Outstanding", 1], ["Share", 1]],
    r.concentration.top.map((x) => h("tr", null, h("td", null, x.name, " ", h("span", { class: "num muted" }, x.member_no)),
      h("td", { class: "n" }, kes(x.balance_kes)), h("td", { class: "n" }, pct(x.share_pct)))));
  const topGuarantors = table("Largest guarantors", [["Member"], ["Loans", 1], ["Guaranteed", 1], ["Deposits", 1]],
    g.top.map((x) => h("tr", null,
      h("td", null, h("button", { type: "button", class: "link", onclick: () => showExposure(x.member_no) }, x.name), " ",
        h("span", { class: "num muted" }, x.member_no)),
      h("td", { class: "n" }, x.loans), h("td", { class: "n" }, kes(x.pledged_kes)),
      h("td", { class: "n" }, x.deposits_kes === null ? "not known" : kes(x.deposits_kes)))));

  return [
    h("div", { class: "head" }, h("div", { class: "stack" }, h("h1", null, "Risk and exposure"),
      h("p", { class: "muted" }, "Where the portfolio and the guarantor network are weak. Includes guarantees from the core system (upload them on Upload) and those made in Sawazi, never counted twice."))),
    kpis, flagList,
    h("div", { class: "grid2" }, classTable, topGuarantors),
    h("div", { class: "grid2" }, parTable("PAR by product", r.par_by_product), parTable("PAR by employer", r.par_by_employer)),
    topBorrowers,
    h("p", { class: "muted small" }, "Classification and provision rates follow SASRA's risk classification of assets. Check them against the current SASRA form before filing."),
  ];
}

async function showExposure(memberNo) {
  const x = await api(`${inst()}/members/${encodeURIComponent(memberNo)}/guarantor-exposure`);
  const body = h("div", { class: "stack" },
    h("p", null, `Deposits ${x.deposits_kes === null ? "not known" : kes(x.deposits_kes)} · guarantees ${kes(x.pledged_kes)} · `,
      x.free_kes === null ? "free capacity not known" : `can still guarantee ${kes(Math.max(x.free_kes, 0))}`),
    x.guarantees.length ? h("div", { class: "tbl-wrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "For"), h("th", null, "Loan"), h("th", { class: "n" }, "Guaranteed"), h("th", { class: "n" }, "Still at risk"), h("th", null, "From"))),
      h("tbody", null, x.guarantees.map((r) => h("tr", null, h("td", null, r.name, " ", h("span", { class: "num muted" }, r.member_no)),
        h("td", { class: "num" }, r.loan_no || "applying"), h("td", { class: "n" }, kes(r.amount_kes)),
        h("td", { class: "n" }, r.at_risk_kes === undefined ? "–" : kes(r.at_risk_kes)), h("td", { class: "small" }, r.source === "core" ? "core system" : "Sawazi")))))) :
      h("p", { class: "muted" }, "Guarantees nobody's loan."));
  await confirmDialog(`${x.name} (${x.member_no}) as a guarantor`, body, "Close", { info: true });
}

// ------------------------------------------------------------------ go

start();

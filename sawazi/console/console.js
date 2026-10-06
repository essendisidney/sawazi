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

const kesFmt = new Intl.NumberFormat("en-KE", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const kes = (v) => `KES ${kesFmt.format(v || 0)}`;
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
const TARGET = { loan_arrears: "arrears", loan_installment: "instalment", deposits: "deposits" };
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
function confirmDialog(title, body, yesLabel, { danger = false } = {}) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { "aria-labelledby": "dlg-title" });
    const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    dlg.append(h("div", { class: "dlg" },
      h("h2", { id: "dlg-title" }, title),
      body,
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => close(false) }, "Cancel"),
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
    h("div", null, h("span", { class: "mark" }, "Sawazi"), " ", h("span", { class: "muted small" }, "by Pesara")),
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
  upload: { label: "Upload", need: "reconcile", render: viewUpload },
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
    h("div", { class: "brand" }, h("span", { class: "mark" }, "Sawazi"), h("span", { class: "inst" }, state.me.institution_name || "")),
    nav,
    h("div", { class: "who" },
      h("span", null, state.me.name, " ", h("span", { class: "muted" }, `· ${state.me.role.replace("_", " ")}`)),
      h("button", { type: "button", class: "link", onclick: () => signOut() }, "Log out"))));
  document.getElementById("app").replaceChildren(top, h("main", { id: "main" }, content));
}

async function route() {
  if (!state.me) return;
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  const view = VIEWS[name] && can(VIEWS[name].need) ? name : "dashboard";
  const main = h("div", { class: "stack" }, h("p", { class: "muted" }, "Loading…"));
  await refreshCounts();
  shell(view, main);
  document.title = `${VIEWS[view].label} · Sawazi`;
  try {
    main.replaceChildren(...[].concat(await VIEWS[view].render()).filter(Boolean));
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
      h("p", { class: "muted small" }, "Arrears first, then the current instalment, then deposits. This cannot be undone from Sawazi."),
      h("label", null, "Why (recorded in the audit log)", noteInput)),
    `Clear to ${m.member_no}`);
  if (!ok) return;
  await busy(button, slot, async () => {
    const r = await api(`/exceptions/${e.id}/resolve`, { method: "POST", body: { member_no: m.member_no, note: noteInput.value.trim() || null } });
    const split = (r.allocations || []).map((a) => `${kes(a.amount_kes)} to ${TARGET[a.target] || a.target}`).join(", ");
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
  ["members", "Members", "Export from the core banking system."],
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
  const sync = () => {
    const [, , text] = IMPORTS.find(([k]) => k === kind.value);
    help.textContent = text;
    const co = kind.value.startsWith("checkoff");
    checkoffFields.hidden = !co; employer.required = co; period.required = co;
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
      const params = kind.value.startsWith("checkoff") ? { employer: employer.value.trim(), period: period.value } : {};
      const r = await api(`${inst()}/import/${kind.value}`, { method: "POST", form: fd, params });
      const parts = [`${r.created} new`, `${r.skipped_duplicates} already in Sawazi`];
      if (r.rejected_count) parts.push(`${r.rejected_count} rows could not be read`);
      if (r.callbacks_confirmed) parts.push(`${r.callbacks_confirmed} real-time payments confirmed`);
      if (r.callbacks_mismatched) parts.push(`${r.callbacks_mismatched} real-time payments DISAGREE with the statement (see Exceptions)`);
      slot.replaceChildren(note(r.callbacks_mismatched || r.rejected_count ? "warn" : "ok", `${file.files[0].name}: ${parts.join(", ")}.`),
        r.rejected.length ? h("ul", { class: "small muted" }, r.rejected.map((x) => h("li", null, x))) : null);
      file.value = "";
    });
  } },
    h("h2", null, "Upload a file"),
    h("label", null, "What is it?", kind), help, checkoffFields,
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

// ------------------------------------------------------------------ go

start();

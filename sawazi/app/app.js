/* Sawazi member app. Phone first, English and Swahili.
 *
 * Signs in with an SMS code once per phone, then a PIN. Shows only the signed-in member's own records (the server
 * enforces that). Everything from the server is put on the page as text, never as HTML.
 * Swahili strings need a native-speaker check before pilots (see docs/PILOT_CHECKLIST.md).
 */
"use strict";

const DEVICE_KEY = "sawazi.device", LANG_KEY = "sawazi.lang";
const state = { session: null, member: null, lang: "en", badge: 0 };
state.lang = (navigator.language || "").startsWith("sw") ? "sw" : "en";
try { const saved = localStorage.getItem(LANG_KEY); if (saved === "en" || saved === "sw") state.lang = saved; } catch {}

const T = {
  en: {
    signin: "Sign in", phone: "Your phone number", phoneHint: "The number your SACCO has for you", sendCode: "Send me a code",
    codeSent: "If this number is registered with a SACCO on Sawazi, a code is on its way.", code: "Code from the SMS",
    verify: "Continue", choose: "Which account?", setPin: "Choose an app PIN", pinHint: "4 to 6 digits. Not your M-Pesa PIN, not 1234.",
    pinAgain: "Type it again", pinsDiffer: "The two PINs are different.", save: "Save and open", enterPin: "Enter your PIN",
    unlock: "Open", forgot: "Forgot your PIN or using a new phone?", useSms: "Sign in with an SMS code",
    money: "Money", guarantees: "Guarantees", loan: "Loan", hello: "Hello", savings: "Your savings", onTrack: "Up to date",
    payWith: "Pay with M-Pesa: Lipa na M-Pesa, then Pay Bill", nextDue: "Next due", deposits: "Deposits", shares: "Share capital", asAt: "as at",
    notKnown: "not known yet", loanBal: "Balance", overdue: "Overdue", daysLate: "days late", instalment: "Monthly instalment",
    howToPay: "How to pay", paybill: "Paybill", account: "Account", copy: "Copy", copied: "Copied",
    payDeposits: "To add to your deposits", recent: "Recent payments", noLoans: "You have no active loans.", noPays: "No payments yet.",
    waiting: "Requests waiting for you", waitingNone: "No requests waiting.", forWhom: "for", ofLoan: "of a loan of",
    accept: "Accept", decline: "Decline", acceptQ: "Guarantee this loan?", acceptBody: "If they do not repay, up to this amount can be recovered from your deposits.",
    declineQ: "Decline this request?", cancel: "Cancel", youGuarantee: "You guarantee", yourGuarantors: "Your guarantors",
    canStill: "You can still guarantee", none: "None.", apply: "Apply for a loan", product: "Loan type", amount: "Amount (KES)",
    months: "Months", check: "Check", guide: "This is a guide, not a decision. Your SACCO decides.", instalmentEst: "About",
    perMonth: "a month", upTo: "You may qualify for up to", couldBeLower: "could be lower: some figures are missing",
    needCover: "Guarantors needed for", purpose: "What is it for?", gNos: "Guarantors' member numbers (optional)",
    gHint: "Separate with commas. The SACCO will ask them.", send: "Send application", yourApps: "Your applications",
    noApps: "No applications yet.", sent: "Sent. A credit officer will check it and contact your guarantors.", signout: "Sign out", demo: "Demo SACCO: fictional data, not real accounts.", offline: "Cannot reach your SACCO. Check your connection.",
    looksGood: "Looks good so far.", notYet: "Some things need checking.", fails: "Not possible as it stands.",
    status: { draft: "With a credit officer", submitted: "Waiting for a decision", approved: "Approved", declined: "Declined",
      withdrawn: "Withdrawn", exported: "Being paid out", disbursed: "Paid out" },
    split: { loan_arrears: "arrears", loan_installment: "instalment", loan_penalty: "penalty", loan_interest: "interest",
      loan_principal: "principal", deposits: "deposits", shares: "shares" },
  },
  sw: {
    signin: "Ingia", phone: "Nambari yako ya simu", phoneHint: "Nambari ambayo SACCO yako inayo", sendCode: "Nitumie nambari ya siri",
    codeSent: "Ikiwa nambari hii imesajiliwa na SACCO kwenye Sawazi, nambari ya siri inakuja.", code: "Nambari kutoka kwenye SMS",
    verify: "Endelea", choose: "Akaunti ipi?", setPin: "Chagua PIN ya programu", pinHint: "Tarakimu 4 hadi 6. Si PIN yako ya M-Pesa, si 1234.",
    pinAgain: "Iandike tena", pinsDiffer: "PIN hizo mbili hazilingani.", save: "Hifadhi na ufungue", enterPin: "Weka PIN yako",
    unlock: "Fungua", forgot: "Umesahau PIN au unatumia simu mpya?", useSms: "Ingia kwa nambari ya SMS",
    money: "Pesa", guarantees: "Dhamana", loan: "Mkopo", hello: "Habari", savings: "Akiba yako", onTrack: "Hujachelewa",
    payWith: "Lipa kwa M-Pesa: Lipa na M-Pesa, kisha Pay Bill", nextDue: "Tarehe ya kulipa", deposits: "Akiba", shares: "Hisa", asAt: "hadi",
    notKnown: "bado haijulikani", loanBal: "Salio", overdue: "Imechelewa", daysLate: "siku za kuchelewa", instalment: "Rejesho la mwezi",
    howToPay: "Jinsi ya kulipa", paybill: "Paybill", account: "Akaunti", copy: "Nakili", copied: "Imenakiliwa",
    payDeposits: "Kuongeza akiba yako", recent: "Malipo ya hivi karibuni", noLoans: "Huna mkopo unaoendelea.", noPays: "Bado hakuna malipo.",
    waiting: "Maombi yanayokusubiri", waitingNone: "Hakuna ombi linalosubiri.", forWhom: "kwa", ofLoan: "ya mkopo wa",
    accept: "Kubali", decline: "Kataa", acceptQ: "Udhamini mkopo huu?", acceptBody: "Asipolipa, hadi kiasi hiki kinaweza kuchukuliwa kutoka kwa akiba yako.",
    declineQ: "Ukatae ombi hili?", cancel: "Ghairi", youGuarantee: "Unadhamini", yourGuarantors: "Wadhamini wako",
    canStill: "Bado unaweza kudhamini", none: "Hakuna.", apply: "Omba mkopo", product: "Aina ya mkopo", amount: "Kiasi (KES)",
    months: "Miezi", check: "Angalia", guide: "Huu ni mwongozo tu, si uamuzi. SACCO yako ndiyo inaamua.", instalmentEst: "Takriban",
    perMonth: "kwa mwezi", upTo: "Unaweza kustahili hadi", couldBeLower: "huenda ikawa chini: baadhi ya taarifa hazipo",
    needCover: "Wadhamini wanahitajika kwa", purpose: "Ni wa kufanyia nini?", gNos: "Nambari za uanachama za wadhamini (si lazima)",
    gHint: "Tenganisha kwa koma. SACCO itawaomba.", send: "Tuma ombi", yourApps: "Maombi yako",
    noApps: "Bado hakuna ombi.", sent: "Limetumwa. Afisa wa mikopo ataliangalia na kuwasiliana na wadhamini wako.", signout: "Toka", demo: "SACCO ya majaribio: taarifa za kubuni, si akaunti halisi.", offline: "Hatuwezi kufikia SACCO yako. Angalia mtandao wako.",
    looksGood: "Inaonekana vizuri kwa sasa.", notYet: "Baadhi ya mambo yanahitaji kuangaliwa.", fails: "Haiwezekani kama ilivyo.",
    status: { draft: "Kwa afisa wa mikopo", submitted: "Inasubiri uamuzi", approved: "Umeidhinishwa", declined: "Umekataliwa",
      withdrawn: "Umeondolewa", exported: "Unatolewa", disbursed: "Umetolewa" },
    split: { loan_arrears: "malimbikizo", loan_installment: "rejesho", loan_penalty: "faini", loan_interest: "riba",
      loan_principal: "mkopo", deposits: "akiba", shares: "hisa" },
  },
};
const t = (k) => T[state.lang][k] ?? T.en[k] ?? k;

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
const fmt = new Intl.NumberFormat("en-KE", { maximumFractionDigits: 0 });
const kes = (v) => (v === null || v === undefined) ? t("notKnown") : `KES ${fmt.format(v)}`;
const day = (iso) => iso ? new Date(iso).toLocaleDateString(state.lang === "sw" ? "sw-KE" : "en-KE", { day: "numeric", month: "short", year: "numeric" }) : "";
const alertBox = (kind, text) => h("div", { class: `alert ${kind}`, role: kind === "bad" ? "alert" : "status" }, text);
const getDevice = () => { try { return localStorage.getItem(DEVICE_KEY); } catch { return null; } };
const setDevice = (v) => { try { v ? localStorage.setItem(DEVICE_KEY, v) : localStorage.removeItem(DEVICE_KEY); } catch {} };

class ApiError extends Error { constructor(status, msg) { super(msg); this.status = status; } }

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Accept-Language": state.lang };
  if (state.session) headers.Authorization = `Bearer ${state.session}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let r;
  try { r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }); }
  catch { throw new ApiError(0, t("offline")); }
  const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : {};
  if (r.status === 401 && state.session) { state.session = null; showUnlock(); throw new ApiError(401, ""); }
  if (!r.ok) {
    const d = data.detail;
    throw new ApiError(r.status, Array.isArray(d) ? d.map((x) => x.msg).join("; ") : (d || "Something went wrong"));
  }
  return data;
}

async function busy(btn, slot, fn) {
  btn.disabled = true; slot.replaceChildren();
  try { await fn(); } catch (e) { if (e.status !== 401) slot.replaceChildren(alertBox("bad", e.message)); }
  finally { btn.disabled = false; }
}

function ask(title, body, yes, { danger = false } = {}) {
  return new Promise((resolve) => {
    const d = h("dialog", { "aria-labelledby": "q" });
    const done = (v) => { d.close(); d.remove(); resolve(v); };
    d.append(h("div", { class: "dlg" }, h("h2", { id: "q" }, title), body,
      h("div", { class: "two" }, h("button", { type: "button", onclick: () => done(false) }, t("cancel")),
        h("button", { type: "button", class: danger ? "" : "primary", onclick: () => done(true) }, yes))));
    d.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    document.body.append(d); d.showModal();
  });
}

function langButton(after) {
  return h("button", { type: "button", class: "lang", "aria-label": "Language / Lugha", onclick: () => {
    state.lang = state.lang === "en" ? "sw" : "en";
    try { localStorage.setItem(LANG_KEY, state.lang); } catch {}
    document.documentElement.lang = state.lang;
    after();
  } }, state.lang === "en" ? "Kiswahili" : "English");
}

function startScreen(...content) {
  const card = h("div", { class: "card" },
    h("div", { class: "row" }, h("div", { class: "brand" }, h("img", { src: "logo.svg", alt: "" }), h("b", null, "sawazi")),
      langButton(() => location.reload())),
    ...content);
  document.getElementById("app").replaceChildren(h("div", { class: "start" }, card));
  card.querySelector("input")?.focus();
}

// ------------------------------------------------------------------ signing in

function showPhone() {
  const phone = h("input", { type: "tel", inputmode: "tel", autocomplete: "tel", required: true, placeholder: "07XX XXX XXX" });
  const slot = h("div"), btn = h("button", { type: "submit", class: "primary" }, t("sendCode"));
  startScreen(h("h1", null, t("signin")), h("form", { class: "stack", onsubmit: (e) => {
    e.preventDefault();
    busy(btn, slot, async () => { await api("/m/otp", { method: "POST", body: { phone: phone.value } }); showCode(phone.value); });
  } }, h("label", null, t("phone"), phone), h("p", { class: "muted small" }, t("phoneHint")), slot, btn));
}

function showCode(phone) {
  const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", required: true, class: "pin" });
  const slot = h("div"), btn = h("button", { type: "submit", class: "primary" }, t("verify"));
  startScreen(alertBox("ok", t("codeSent")), h("form", { onsubmit: (e) => {
    e.preventDefault();
    busy(btn, slot, async () => {
      const v = await api("/m/otp/verify", { method: "POST", body: { phone, code: code.value } });
      if (v.memberships.length === 1) showSetPin(v.verify_token, v.memberships[0].id);
      else showChoose(v);
    });
  } }, h("label", null, t("code"), code), slot, btn), h("button", { type: "button", class: "link", onclick: showPhone }, t("sendCode")));
}

function showChoose(v) {
  startScreen(h("h1", null, t("choose")), h("div", { class: "list" }, v.memberships.map((m) =>
    h("button", { type: "button", onclick: () => showSetPin(v.verify_token, m.id) },
      `${m.first_name} · ${m.member_no} · ${m.institution}`))));
}

function showSetPin(verifyToken, membership) {
  const pin = h("input", { type: "password", inputmode: "numeric", maxlength: "6", required: true, class: "pin", autocomplete: "new-password" });
  const again = h("input", { type: "password", inputmode: "numeric", maxlength: "6", required: true, class: "pin", autocomplete: "new-password" });
  const slot = h("div"), btn = h("button", { type: "submit", class: "primary" }, t("save"));
  startScreen(h("h1", null, t("setPin")), h("p", { class: "muted small" }, t("pinHint")), h("form", { onsubmit: (e) => {
    e.preventDefault();
    if (pin.value !== again.value) { slot.replaceChildren(alertBox("bad", t("pinsDiffer"))); return; }
    busy(btn, slot, async () => {
      const r = await api("/m/devices", { method: "POST", body: { verify_token: verifyToken, membership, pin: pin.value } });
      setDevice(r.device_token); state.session = r.session_token; state.member = r.member;
      location.hash = "#/money"; route();
    });
  } }, h("label", null, t("setPin"), pin), h("label", null, t("pinAgain"), again), slot, btn));
}

function showUnlock() {
  const device = getDevice();
  if (!device) return showPhone();
  const pin = h("input", { type: "password", inputmode: "numeric", maxlength: "6", required: true, class: "pin", autocomplete: "current-password" });
  const slot = h("div"), btn = h("button", { type: "submit", class: "primary" }, t("unlock"));
  startScreen(h("h1", null, t("enterPin")), h("form", { onsubmit: (e) => {
    e.preventDefault();
    busy(btn, slot, async () => {
      const r = await api("/m/login", { method: "POST", body: { device_token: device, pin: pin.value } });
      state.session = r.session_token; state.member = r.member; route();
    });
  } }, pin, slot, btn), h("button", { type: "button", class: "link", onclick: () => { setDevice(null); showPhone(); } }, t("forgot")));
}

// ------------------------------------------------------------------ the app

const ICONS = {
  money: ["M3 7h18v12H3z", "M3 11h18", "M16 15h2"],
  guarantees: ["M12 3 4 6v6c0 4.5 3.4 7.7 8 9 4.6-1.3 8-4.5 8-9V6z", "M9 12l2 2 4-4"],
  loan: ["M12 3v18", "M16.5 7H10a3 3 0 0 0 0 6h4a3 3 0 0 1 0 6H7"],
  out: ["M10 4H5v16h5", "M14 8l4 4-4 4", "M18 12H9"],
  check: ["M5 12l5 5 9-10"],
};
function icon(name, size = 22) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", width: String(size), height: String(size), fill: "none", stroke: "currentColor",
    "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(k, v);
  for (const d of ICONS[name] || []) { const path = document.createElementNS(NS, "path"); path.setAttribute("d", d); svg.append(path); }
  return svg;
}
const initials = (name) => name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

async function signOutApp() {
  await api("/m/logout", { method: "POST" }).catch(() => {});
  state.session = null; showUnlock();
}

const TABS = [["money", viewMoney], ["guarantees", viewGuarantees], ["loan", viewLoan]];

async function route() {
  if (!state.session) return showUnlock();
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  const [key, view] = TABS.find(([k]) => k === name) || TABS[0];
  const main = h("main", null, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
  const top = h("header", { class: "top" },
    h("span", { class: "avatar", "aria-hidden": "true" }, initials(state.member.name)),
    h("div", { class: "who" }, h("b", null, state.member.name), h("span", null, `${state.member.member_no} · ${state.member.institution}`)),
    langButton(route),
    h("button", { type: "button", class: "icon-btn", "aria-label": t("signout"), title: t("signout"), onclick: signOutApp }, icon("out", 20)));
  const tabs = h("nav", { class: "tabs", "aria-label": "Sections" }, TABS.map(([k]) =>
    h("a", { href: `#/${k}`, "aria-current": k === key ? "page" : null }, icon(k), h("span", null, t(k)),
      k === "guarantees" && state.badge ? h("span", { class: "dot" }, state.badge) : null)));
  const demo = state.member.is_demo ? h("div", { class: "demo-banner", role: "note" }, t("demo")) : null;
  document.getElementById("app").replaceChildren(top, demo, main, tabs);
  try { main.replaceChildren(...[await view()].flat(Infinity).filter(Boolean)); }
  catch (e) { if (e.status !== 401) main.replaceChildren(alertBox("bad", e.message)); }
}
window.addEventListener("hashchange", route);

function copyRow(label, value) {
  const btn = h("button", { type: "button", class: "copy" }, t("copy"));
  btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(value); btn.textContent = t("copied"); setTimeout(() => { btn.textContent = t("copy"); }, 1500); } catch {}
  });
  return h("div", { class: "pay-row" }, h("span", { class: "pay-k" }, label), h("b", { class: "pay-v num" }, value), btn);
}

async function viewMoney() {
  const o = await api("/m/overview");
  const g = await api("/m/guarantees").catch(() => ({ waiting: [] }));
  state.badge = g.waiting.length;
  document.querySelector(".tabs .dot")?.remove();
  if (state.badge) document.querySelector('.tabs a[href="#/guarantees"]')?.append(h("span", { class: "dot" }, state.badge));
  const payBox = (pay) => pay.paybill ? h("div", { class: "pay" }, h("div", { class: "pay-head" }, t("payWith")),
    copyRow(t("paybill"), pay.paybill), copyRow(t("account"), pay.account)) : null;
  const loans = o.loans.map((l) => {
    const late = l.arrears_kes > 0;
    return h("section", { class: "card loan" },
      h("div", { class: "row" }, h("div", null, h("h2", null, l.product), h("span", { class: "muted small num" }, l.loan_no)),
        late ? h("span", { class: `chip ${l.days_in_arrears > 30 ? "bad" : "warn"}` }, `${l.days_in_arrears} ${t("daysLate")}`)
          : h("span", { class: "chip ok" }, icon("check", 14), t("onTrack"))),
      h("div", { class: "pair" },
        h("div", null, h("div", { class: "label" }, t("loanBal")), h("div", { class: "big" }, kes(l.balance_kes))),
        h("div", null, h("div", { class: "label" }, t("instalment")), h("div", { class: "big" }, kes(l.installment_kes)),
          l.next_due_on ? h("div", { class: "muted small" }, `${t("nextDue")}: ${day(l.next_due_on)}`) : null)),
      late ? alertBox(l.days_in_arrears > 30 ? "bad" : "warn", `${t("overdue")}: ${kes(l.arrears_kes)}`) : null,
      payBox(l.pay));
  });
  return [
    h("section", { class: "hero" },
      h("p", { class: "hello" }, `${t("hello")}, ${state.member.name.split(/\s+/)[0]}`),
      h("div", { class: "label" }, t("deposits")),
      h("div", { class: "hero-v num" }, kes(o.deposits_kes)),
      h("div", { class: "hero-row" },
        h("span", null, h("span", { class: "label" }, t("shares")), " ", h("b", { class: "num" }, kes(o.shares_kes))),
        o.balances_as_of ? h("span", { class: "small" }, `${t("asAt")} ${day(o.balances_as_of)}`) : null)),
    loans.length ? loans : h("section", { class: "card" }, h("p", { class: "muted" }, t("noLoans"))),
    o.pay_deposits.paybill ? h("section", { class: "card" }, h("h2", null, t("payDeposits")), payBox(o.pay_deposits)) : null,
    h("section", { class: "card" }, h("h2", null, t("recent")),
      o.payments.length ? h("div", { class: "list" }, o.payments.map((p) => h("div", { class: "item" },
        h("span", { class: "coin", "aria-hidden": "true" }, icon("money", 18)),
        h("div", { class: "grow" }, h("div", null, day(p.date)), h("div", { class: "muted small" },
          p.split.map((x) => `${kes(x.kes)} ${(T[state.lang].split[x.to] || x.to)}`).join(" · "))),
        h("b", { class: "num" }, kes(p.kes))))) : h("p", { class: "muted" }, t("noPays"))),
  ];
}

async function viewGuarantees() {
  const g = await api("/m/guarantees");
  state.badge = g.waiting.length;
  const slot = h("div");
  const waiting = g.waiting.map((r) => {
    const yes = h("button", { type: "button", class: "primary" }, t("accept"));
    const no = h("button", { type: "button" }, t("decline"));
    const answer = (answer) => busy(answer === "accept" ? yes : no, slot, async () => {
      const ok = answer === "accept"
        ? await ask(t("acceptQ"), h("div", null, h("p", null, h("b", null, kes(r.kes)), ` ${t("forWhom")} ${r.for}`), h("p", { class: "muted small" }, t("acceptBody"))), t("accept"))
        : await ask(t("declineQ"), h("p", null, `${r.for} · ${kes(r.kes)}`), t("decline"), { danger: true });
      if (!ok) return;
      await api(`/m/guarantees/${r.id}/answer`, { method: "POST", body: { answer } });
      route();
    });
    yes.addEventListener("click", () => answer("accept")); no.addEventListener("click", () => answer("decline"));
    return h("div", { class: "card" }, h("p", null, h("b", null, r.for), ` · ${kes(r.kes)} ${t("ofLoan")} ${kes(r.loan_kes)}`),
      h("p", { class: "muted small" }, t("acceptBody")), h("div", { class: "pair" }, no, yes));
  });
  const list = (rows, who) => rows.length ? h("div", { class: "list" }, rows.map((r) => h("div", { class: "item" },
    h("span", { class: "avatar sm", "aria-hidden": "true" }, initials(r[who])),
    h("span", { class: "grow" }, r[who], r.loan_no ? h("span", { class: "muted small" }, ` · ${r.loan_no}`) : null), h("b", { class: "num" }, kes(r.kes)))))
    : h("p", { class: "muted" }, t("none"));
  return [
    h("h1", null, t("waiting")), slot,
    waiting.length ? waiting : h("section", { class: "card" }, h("p", { class: "muted" }, t("waitingNone"))),
    h("section", { class: "card" }, h("div", { class: "row" }, h("h2", null, t("youGuarantee")), h("b", { class: "num" }, kes(g.pledged_kes))),
      list(g.giving, "for"), h("p", { class: "muted small" }, `${t("canStill")}: ${kes(g.can_still_guarantee_kes)}`)),
    h("section", { class: "card" }, h("h2", null, t("yourGuarantors")), list(g.mine, "by")),
  ];
}

async function viewLoan() {
  const [products, apps] = await Promise.all([api("/m/products"), api("/m/applications")]);
  const product = h("select", null, products.map((p) => h("option", { value: p.id }, p.name)));
  const amount = h("input", { type: "number", inputmode: "decimal", min: "1", step: "1", required: true });
  const months = h("input", { type: "number", inputmode: "numeric", min: "1", max: "240", required: true });
  const purpose = h("input", { type: "text", maxlength: "300" });
  const gnos = h("input", { type: "text", maxlength: "120", placeholder: "UT00123, UT00456" });
  const result = h("div"), slot = h("div");
  const checkBtn = h("button", { type: "button" }, t("check"));
  const sendBtn = h("button", { type: "submit", class: "primary" }, t("send"));
  const body = () => ({ product_id: Number(product.value), amount_kes: amount.value, term_months: Number(months.value) });
  checkBtn.addEventListener("click", () => busy(checkBtn, result, async () => {
    const c = await api("/m/loan-check", { method: "POST", body: body() });
    const head = { passes: ["ok", t("looksGood")], incomplete: ["warn", t("notYet")], fails: ["bad", t("fails")] }[c.outcome];
    result.replaceChildren(h("div", { class: "card" }, alertBox(head[0], head[1]),
      h("p", null, `${t("instalmentEst")} ${kes(c.instalment_kes)} ${t("perMonth")}.`),
      h("p", null, `${t("upTo")} ${kes(c.max_eligible_kes)}`, c.max_eligible_partial ? h("span", { class: "muted small" }, ` (${t("couldBeLower")})`) : null),
      c.guarantor_cover_needed_kes > 0 ? h("p", null, `${t("needCover")} ${kes(c.guarantor_cover_needed_kes)}.`) : null,
      h("ul", { class: "small" }, c.checks.filter((x) => x.status !== "pass").map((x) => h("li", null, x.message))),
      h("p", { class: "muted small" }, t("guide"))));
  }));
  const form = h("form", { class: "card", onsubmit: (e) => {
    e.preventDefault();
    busy(sendBtn, slot, async () => {
      const r = await api("/m/applications", { method: "POST", body: { ...body(), purpose: purpose.value || null,
        guarantors: gnos.value.split(",").map((x) => x.trim()).filter(Boolean) } });
      slot.replaceChildren(alertBox("ok", `${r.ref}: ${t("sent")}`));
      setTimeout(route, 1800);
    });
  } }, h("h1", null, t("apply")),
    products.length ? [h("label", null, t("product"), product), h("div", { class: "pair" }, h("label", null, t("amount"), amount), h("label", null, t("months"), months)),
      checkBtn, result, h("label", null, t("purpose"), purpose), h("label", null, t("gNos"), gnos), h("p", { class: "muted small" }, t("gHint")),
      slot, sendBtn] : h("p", { class: "muted" }, t("none")));
  return [form, h("section", { class: "card" }, h("h2", null, t("yourApps")),
    apps.length ? h("div", { class: "list" }, apps.map((a) => h("div", { class: "item" },
      h("div", null, h("div", null, `${a.product} · ${kes(a.kes)}`), h("div", { class: "muted small" }, `${a.ref} · ${day(a.created_at)}`)),
      h("span", { class: "status" }, T[state.lang].status[a.status] || a.status)))) : h("p", { class: "muted" }, t("noApps")))];
}

// ------------------------------------------------------------------ go

document.documentElement.lang = state.lang;
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
showUnlock();

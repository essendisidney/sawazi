"""Monthly board pack: one self-contained HTML file a board can read in any browser or print to PDF.

Figures come from the database for the month (payments, decisions, audit events) and from portfolio snapshots
for anything that needs history. Snapshots are never back-filled: a month without one is reported as missing,
not estimated. Every value from the database is escaped; the file has no scripts.
"""
from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import risk
from .auth import utcnow
from .engine.exposure import CLASSES
from .models import (AuditEvent, ExceptionItem, Institution, LoanApplication, LoanDecision, LoanProduct, Member,
                     PortfolioSnapshot, SmsMessage, StaffUser, Transaction)

FLAG_LABEL = {
    "guarantor_in_arrears": "Guarantor is behind on their own loan", "over_pledged": "Pledged above deposits",
    "many_guarantees": "Backs many loans", "mutual_guarantee": "Members guarantee each other",
    "guarantee_circle": "Guarantee circle", "chain_default": "Defaulted loan whose guarantors are also behind",
    "concentration": "Large borrower",
}
GOVERNANCE = {
    "allocation_rules.update": "Allocation rules changed", "loan_product.create": "Loan product created",
    "loan_product.update": "Loan product changed", "user.create": "Staff user added", "user.update": "Staff user changed",
    "api_key.create": "API key created", "api_key.revoke": "API key revoked", "sms.settings": "SMS settings changed",
    "sms.opt_in": "Member opted back in to SMS", "institution.create": "Institution created",
}


# ---------------------------------------------------------------- snapshots

def snapshot_figures(s: Session, institution_id: int) -> dict:
    r = risk.report(s, institution_id)
    susp = s.execute(select(func.count(), func.coalesce(func.sum(ExceptionItem.amount_cents), 0)).where(
        ExceptionItem.institution_id == institution_id, ExceptionItem.status == "open",
        ExceptionItem.kind == "suspense")).one()
    return {"portfolio": r.portfolio, "classification": r.classification,
            "par_by_product": r.par_by_product[:10], "par_by_employer": r.par_by_employer[:10],
            "guarantors": {k: r.guarantors[k] for k in ("members_guaranteeing", "pledged_cents", "on_loans_behind_cents")},
            "flags": dict(Counter(f.kind for f in r.flags)), "flags_high": sum(f.severity == "high" for f in r.flags),
            "suspense": {"count": susp[0], "cents": int(susp[1])}}


def take_snapshot(s: Session, institution_id: int, as_of: date | None = None) -> PortfolioSnapshot:
    """Record portfolio quality as it stands now. The same day twice replaces the earlier one. Caller commits."""
    as_of = as_of or utcnow().date()
    figures = snapshot_figures(s, institution_id)  # before any new row exists: its queries may flush the session
    snap = s.scalar(select(PortfolioSnapshot).where(PortfolioSnapshot.institution_id == institution_id,
                                                    PortfolioSnapshot.as_of == as_of))
    if snap is None:
        snap = PortfolioSnapshot(institution_id=institution_id, as_of=as_of, taken_at=utcnow(), figures=figures)
        s.add(snap)
    else:
        snap.taken_at, snap.figures = utcnow(), figures
    s.flush()
    return snap


def month_bounds(month: str) -> tuple[datetime, datetime]:
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1)
    return start, datetime(y + (m == 12), m % 12 + 1, 1)


def prev_month(month: str) -> str:
    start, _ = month_bounds(month)
    return (start - timedelta(days=1)).strftime("%Y-%m")


def latest_in(s: Session, institution_id: int, month: str) -> PortfolioSnapshot | None:
    start, end = month_bounds(month)
    return s.scalar(select(PortfolioSnapshot).where(
        PortfolioSnapshot.institution_id == institution_id, PortfolioSnapshot.as_of >= start.date(),
        PortfolioSnapshot.as_of < end.date()).order_by(PortfolioSnapshot.as_of.desc()).limit(1))


# ---------------------------------------------------------------- the month's figures

def gather(s: Session, institution_id: int, month: str) -> dict:
    start, end = month_bounds(month)
    in_month = lambda col: (col >= start) & (col < end)  # noqa: E731
    current, previous = latest_in(s, institution_id, month), latest_in(s, institution_id, prev_month(month))
    trend, m = [], month
    for _ in range(12):
        snap = latest_in(s, institution_id, m)
        trend.append((m, snap.figures["portfolio"] if snap else None))
        m = prev_month(m)
    trend.reverse()
    while trend and trend[0][1] is None:  # history starts at the first snapshot
        trend.pop(0)

    by_source = defaultdict(lambda: {"count": 0, "cents": 0, "allocated": 0, "suspense": 0})
    for source, status, n, total in s.execute(
            select(Transaction.source, Transaction.status, func.count(), func.coalesce(func.sum(Transaction.amount_cents), 0))
            .where(Transaction.institution_id == institution_id, in_month(Transaction.txn_time))
            .group_by(Transaction.source, Transaction.status)):
        row = by_source[source]
        row["count"] += n
        row["cents"] += int(total)
        if status in ("allocated", "suspense"):
            row[status] += n

    events = list(s.scalars(select(AuditEvent).where(AuditEvent.institution_id == institution_id,
                                                     in_month(AuditEvent.at)).order_by(AuditEvent.at)))
    clears = Counter(e.actor_name for e in events if e.action == "suspense.clear")
    sms = Counter(m.status for m in s.scalars(select(SmsMessage).where(
        SmsMessage.institution_id == institution_id, in_month(SmsMessage.approved_at))))

    apps = list(s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id)))
    created = [a for a in apps if start <= a.created_at < end]
    decided = [a for a in apps if a.decided_at and start <= a.decided_at < end]
    approved = [a for a in decided if a.status in ("approved", "exported", "disbursed")]
    disbursed = [a for a in apps if a.disbursed_at and start <= a.disbursed_at < end]
    users = {u.id: u.name for u in s.scalars(select(StaffUser).where(StaffUser.institution_id == institution_id))}
    exceptions = []
    for a in approved:
        if a.override_reason:
            mem, prod = s.get(Member, a.member_id), s.get(LoanProduct, a.product_id)
            who = [users.get(d.user_id, "?") for d in s.scalars(select(LoanDecision).where(
                LoanDecision.application_id == a.id, LoanDecision.decision == "approve"))]
            exceptions.append({"ref": f"SWZ-{a.id}", "member": f"{mem.name} ({mem.member_no})", "product": prod.code,
                               "cents": a.amount_cents, "outcome": a.appraisal_outcome, "reason": a.override_reason,
                               "approvers": ", ".join(who)})

    governance = [{"at": e.at, "who": e.actor_name, "what": GOVERNANCE[e.action], "note": e.note or "",
                   "detail": _change(e)} for e in events if e.action in GOVERNANCE]
    return {
        "month": month, "current": current, "previous": previous, "trend": trend,
        "payments": dict(by_source), "clears": clears, "sms": sms,
        "failed_logins": sum(e.action == "auth.login_failed" for e in events),
        "lending": {"created": len(created), "approved": len(approved), "approved_cents": sum(a.amount_cents for a in approved),
                    "declined": sum(a.status == "declined" for a in decided),
                    "withdrawn": sum(a.status == "withdrawn" for a in decided),
                    "disbursed": len(disbursed), "disbursed_cents": sum(a.amount_cents for a in disbursed),
                    "waiting": sum(a.status == "submitted" for a in apps), "exceptions": exceptions},
        "governance": governance,
    }


FIELD = {"role": "role", "is_active": "active", "name": "name", "loan_order": "which loan first",
         "arrears_order": "arrears order", "pay_current_installment": "pay current instalment", "excess": "what is left",
         "enabled": "SMS on", "service_name": "service name", "opt_out_text": "opt-out text",
         "interest_rate_pct": "rate %", "max_amount_kes": "max amount", "deposits_multiplier": "deposits multiplier",
         "max_term_months": "max months", "min_membership_months": "months of membership", "active": "active",
         "second_approval_above_kes": "two approvers above"}


def _plain(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(_plain(x) for x in v)
    if isinstance(v, dict):
        return " ".join(str(x) for x in v.values() if x is not None)
    return str(v).replace("_", " ")


def _change(e: AuditEvent) -> str:
    """What changed, in words a board member can read."""
    b, a = e.before or {}, e.after or {}
    if e.action == "institution.create":
        return a.get("name", "")
    if e.action == "user.create":
        return f"{a.get('name')} as {_plain(a.get('role', ''))}"
    if e.action in ("api_key.create", "api_key.revoke"):
        return f"{a.get('name', '')}" + (f" ({_plain(a['role'])})" if "role" in a else "")
    if e.action == "loan_product.create":
        return f"{a.get('code', '')} {a.get('name', '')}"
    if e.action == "sms.opt_in":
        return f"{b.get('phone', '')}"
    parts = []
    for k, new in a.items():
        if k == "password_reset":
            parts.append("password reset")
        elif k in b and b[k] != new:
            parts.append(f"{FIELD.get(k, k.replace('_', ' '))}: {_plain(b[k])} → {_plain(new)}")
    return "; ".join(parts[:4])


def _summary(d: dict) -> list[str]:
    """The headlines, in plain words, before any table."""
    out = []
    cur, prev = d["current"], d["previous"]
    if cur:
        p = cur.figures["portfolio"]
        line = f"{kes(p['balance_cents'])} is out on {p['loans']} loans; PAR 30 is {pct(p['par30_bps'])}"
        if prev:
            q = prev.figures["portfolio"]
            move = p["par30_bps"] - q["par30_bps"]
            line += (f", {'up' if move > 0 else 'down'} from {pct(q['par30_bps'])}" if move else ", unchanged")
        out.append(line + f". Provision needed: {kes(p['provision_cents'])}.")
        flags = cur.figures["flags"]
        g = cur.figures["guarantors"]
        if flags.get("guarantor_in_arrears"):
            out.append(f"{flags['guarantor_in_arrears']} guarantor(s) are more than 30 days behind on their own loans; "
                       f"{kes(g['on_loans_behind_cents'])} of guarantees sit on loans that are behind.")
        circles = flags.get("mutual_guarantee", 0) + flags.get("guarantee_circle", 0)
        if circles:
            out.append(f"{circles} group(s) of members guarantee each other's loans: worth checking they are genuine.")
    else:
        out.append("No portfolio snapshot exists for this month, so portfolio figures are not reported.")
    pay = d["payments"]
    n, cents = sum(v["count"] for v in pay.values()), sum(v["cents"] for v in pay.values())
    matched = sum(v["allocated"] for v in pay.values())
    seen = matched + sum(v["suspense"] for v in pay.values())
    if n:
        out.append(f"{n} payments worth {kes(cents)} came in"
                   + (f"; {matched * 100 / seen:.0f}% were matched without a person." if seen else "."))
    L = d["lending"]
    if L["created"] or L["approved"] or L["declined"]:
        out.append(f"{L['created']} loan application(s) received, {L['approved']} approved ({kes(L['approved_cents'])}), "
                   f"{L['declined']} declined" + (f"; {len(L['exceptions'])} approved with exceptions." if L["exceptions"] else "."))
    if d["governance"] or d["failed_logins"]:
        out.append(f"{len(d['governance'])} change(s) to rules, products or staff access; "
                   f"{d['failed_logins']} failed staff login(s).")
    return out


# ---------------------------------------------------------------- rendering

def esc(v) -> str:
    return html.escape(str(v), quote=True)


def kes(cents: int | None) -> str:
    return "–" if cents is None else f"KES {cents / 100:,.0f}"


def pct(bps: int | None) -> str:
    return "–" if bps is None else f"{bps / 100:.1f}%"


def _delta(now, before, fmt, worse_if_up: bool | None = True) -> str:
    """Change since the previous snapshot. worse_if_up=None: neither direction is good or bad in itself."""
    if now is None or before is None:
        return ""
    d = now - before
    if d == 0:
        return '<span class="muted">no change</span>'
    tone = "muted" if worse_if_up is None else "bad" if (d > 0) == worse_if_up else "good"
    return f'<span class="{tone}">{"▲" if d > 0 else "▼"} {fmt(abs(d))}</span>'


def _line_chart(trend: list) -> str:
    """PAR 1 and PAR 30 by month, as plain SVG: a scale, a dot per month, labels that never overlap."""
    pts = [(m, p["par1_bps"], p["par30_bps"]) for m, p in trend if p]
    if len(pts) < 2:
        return '<p class="muted">The trend appears once there are snapshots from two or more months.</p>'
    w, h, left, right, top_pad, bottom = 640, 220, 44, 96, 16, 30
    peak = max(max(a, b) for _, a, b in pts)
    step = next(s for s in (100, 200, 500, 1000, 2000, 2500, 5000) if peak <= s * 4)  # gridline every 1/2/5/10/20/25/50%
    top = max(step, -(-peak // step) * step)
    x = lambda i: left + i * (w - left - right) / (len(pts) - 1)  # noqa: E731
    y = lambda v: top_pad + (1 - v / top) * (h - top_pad - bottom)  # noqa: E731
    out = []
    for g in range(0, top + 1, step):
        out.append(f'<line class="grid" x1="{left}" y1="{y(g):.1f}" x2="{w - right}" y2="{y(g):.1f}"/>'
                   f'<text x="{left - 6}" y="{y(g) + 4:.1f}" text-anchor="end">{g / 100:g}%</text>')
    ends = []
    for idx, (cls, label) in enumerate([("par1", "PAR 1"), ("par30", "PAR 30")]):
        series = [(x(i), y(p[1 + idx])) for i, p in enumerate(pts)]
        out.append(f'<polyline class="{cls}" points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in series)}"/>')
        out += [f'<circle class="{cls}" cx="{a:.1f}" cy="{b:.1f}" r="3.5"/>' for a, b in series]
        ends.append([series[-1][1], cls, f"{label} {pts[-1][1 + idx] / 100:.1f}%"])
    ends.sort()
    if ends[1][0] - ends[0][0] < 14:  # keep the end labels apart
        mid = (ends[0][0] + ends[1][0]) / 2
        ends[0][0], ends[1][0] = mid - 7, mid + 7
    out += [f'<text class="{cls}" x="{w - right + 8}" y="{yy + 4:.1f}">{label}</text>' for yy, cls, label in ends]
    out += [f'<text x="{x(i):.1f}" y="{h - 8}" text-anchor="middle">{datetime.strptime(m, "%Y-%m"):%b %y}</text>'
            for i, (m, _, _) in enumerate(pts)]
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Portfolio at risk by month">'
            f'{"".join(out)}</svg>')


STYLE = """
:root { --ink:#18201c; --muted:#5d6a63; --line:#dde2dc; --accent:#0f6b4f; --bad:#b3261e; --good:#1d7a44; --soft:#f5f6f3; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif; color: var(--ink); font-size: 14px; line-height: 1.5; }
main { max-width: 900px; margin: 0 auto; padding: 32px 24px 64px; }
h1, h2 { font-family: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif; margin: 0 0 8px; }
h1 { font-size: 1.9rem; } h2 { font-size: 1.2rem; margin-top: 32px; padding-top: 16px; border-top: 2px solid var(--ink); }
.brand { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; color: var(--muted); font-size: .85rem; letter-spacing: .08em; }
.muted { color: var(--muted); } .bad { color: var(--bad); } .good { color: var(--good); }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 16px 0; }
.kpi { border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.kpi b { display: block; font-size: 1.35rem; font-variant-numeric: tabular-nums; }
.kpi span { font-size: .78rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
table { width: 100%; border-collapse: collapse; margin: 8px 0 4px; font-size: .9rem; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
.brand svg { width: 28px; height: 28px; flex: none; }
svg.chart { width: 100%; height: auto; margin: 8px 0; }
.chart polyline { fill: none; stroke-width: 2.5; } .chart .par30 { stroke: var(--bad); fill: var(--bad); }
.chart .par1 { stroke: var(--accent); fill: var(--accent); } .chart text { font-size: 11px; fill: var(--muted); stroke: none; }
.chart text.par30, .chart text.par1 { font-weight: 600; } .chart .grid { stroke: var(--line); }
.note { background: var(--soft); border-radius: 8px; padding: 10px 12px; font-size: .88rem; }
.brief { background: var(--soft); border-radius: 10px; padding: 4px 18px 10px; margin: 16px 0; }
.brief h2 { border: 0; margin-top: 12px; padding-top: 0; } .brief ul { margin: 0; padding-left: 18px; }
.brief li { margin: 6px 0; } h3 { font-size: 1rem; margin: 18px 0 4px; }
@media print { main { padding: 0; } h2 { break-before: auto; } section { break-inside: avoid; } @page { size: A4; margin: 16mm; } }
"""


def render(inst: Institution, d: dict, generated_by: str) -> str:
    cur, prev = d["current"], d["previous"]
    p = cur.figures["portfolio"] if cur else None
    pp = prev.figures["portfolio"] if prev else None
    g = cur.figures["guarantors"] if cur else None
    to_date = d["month"] == utcnow().strftime("%Y-%m")
    title = f"{inst.name}: board pack, {datetime.strptime(d['month'], '%Y-%m'):%B %Y}" + (" (to date)" if to_date else "")

    def kpi(label, value, delta=""):
        return f'<div class="kpi"><span>{esc(label)}</span><b>{value}</b>{delta}</div>'

    if p:
        head = (kpi("Outstanding loans", kes(p["balance_cents"]), _delta(p["balance_cents"], pp and pp["balance_cents"], kes, None))
                + kpi("PAR 30", pct(p["par30_bps"]), _delta(p["par30_bps"], pp and pp["par30_bps"], pct))
                + kpi("Provision needed", kes(p["provision_cents"]), _delta(p["provision_cents"], pp and pp["provision_cents"], kes))
                + kpi("Guarantees on loans behind", kes(g["on_loans_behind_cents"])))
        snap_note = (f'<p class="muted">Portfolio figures as at {cur.as_of:%d %B %Y}'
                     + (f', compared with {prev.as_of:%d %B %Y}.' if prev else '; no snapshot from the month before to compare with.')
                     + '</p>')
    else:
        head, snap_note = "", '<p class="note">No portfolio snapshot was taken this month, so portfolio figures are not shown.</p>'

    quality = ""
    if cur:
        rows = "".join(f'<tr><td>{esc(c["class"].title())}</td><td>{esc(c["days"])}</td><td class="n">{c["loans"]}</td>'
                       f'<td class="n">{kes(c["balance_cents"])}</td><td class="n">{c["provision_bps"] / 100:.0f}%</td>'
                       f'<td class="n">{kes(c["provision_cents"])}</td></tr>' for c in cur.figures["classification"])
        par_rows = "".join(f'<tr><td>{esc(r["name"])}</td><td class="n">{r["loans"]}</td><td class="n">{kes(r["balance_cents"])}</td>'
                           f'<td class="n">{pct(r["par_bps"])}</td></tr>' for r in cur.figures["par_by_product"])
        quality = (f'<table><tr><th>Class</th><th>Days behind</th><th class="n">Loans</th><th class="n">Outstanding</th>'
                   f'<th class="n">Rate</th><th class="n">Provision</th></tr>{rows}</table>'
                   f'<p class="muted">Classes and rates as in SASRA\'s risk classification of assets ({esc(", ".join(f"{c[3] // 100}%" for c in CLASSES))}).</p>'
                   f'<table><tr><th>Product</th><th class="n">Loans</th><th class="n">Outstanding</th><th class="n">PAR 30</th></tr>{par_rows}</table>')

    pay_rows = "".join(f'<tr><td>{esc({"mpesa": "M-Pesa", "bank": "Bank", "checkoff": "Check-off"}.get(k, k))}</td>'
                       f'<td class="n">{v["count"]}</td><td class="n">{kes(v["cents"])}</td><td class="n">{v["allocated"]}</td>'
                       f'<td class="n">{v["suspense"]}</td></tr>' for k, v in sorted(d["payments"].items()))
    total = sum(v["allocated"] + v["suspense"] for v in d["payments"].values())
    auto = sum(v["allocated"] for v in d["payments"].values())
    collections = (f'<table><tr><th>Source</th><th class="n">Payments</th><th class="n">Amount</th><th class="n">Allocated</th>'
                   f'<th class="n">To suspense</th></tr>{pay_rows or "<tr><td colspan=5 class=muted>No payments this month.</td></tr>"}</table>'
                   + (f'<p>{auto / total * 100:.1f}% of this month\'s payments were matched without a person. ' if total else "<p>")
                   + f'Suspense cleared by staff: {sum(d["clears"].values())}'
                   + (f' ({esc(", ".join(f"{k} {n}" for k, n in d["clears"].most_common()))})' if d["clears"] else "")
                   + (f'. Still open at the snapshot: {cur.figures["suspense"]["count"]} ({kes(cur.figures["suspense"]["cents"])}).' if cur else ".")
                   + f' Reminder SMS approved: {sum(d["sms"].values())}'
                   + (f' ({esc(", ".join(f"{n} {k}" for k, n in sorted(d["sms"].items())))})' if d["sms"] else "") + ".</p>")

    L = d["lending"]
    exc_rows = "".join(f'<tr><td>{esc(x["ref"])}</td><td>{esc(x["member"])}</td><td>{esc(x["product"])}</td>'
                       f'<td class="n">{kes(x["cents"])}</td><td>{esc(x["outcome"])}</td><td>{esc(x["reason"])}</td>'
                       f'<td>{esc(x["approvers"])}</td></tr>' for x in L["exceptions"])
    approved_txt = f"{L['approved']} · {kes(L['approved_cents'])}"
    closed_txt = f"{L['declined']} / {L['withdrawn']}"
    disbursed_txt = f"{L['disbursed']} · {kes(L['disbursed_cents'])}"
    lending = (f'<div class="kpis">{kpi("Applications received", L["created"])}{kpi("Approved", approved_txt)}'
               f'{kpi("Declined / withdrawn", closed_txt)}{kpi("Disbursed", disbursed_txt)}</div>'
               f'<p class="muted">{L["waiting"]} application(s) waiting for a decision at the time of this pack.</p>'
               + (f'<h3>Approved with exceptions ({len(L["exceptions"])})</h3><p class="muted">Approved although the appraisal did not pass; '
                  f'each needed a written reason.</p><table><tr><th>Ref</th><th>Member</th><th>Product</th><th class="n">Amount</th>'
                  f'<th>Appraisal</th><th>Reason</th><th>Approved by</th></tr>{exc_rows}</table>' if exc_rows
                  else '<p>No loans were approved with exceptions this month.</p>'))

    guar = ""
    if cur:
        flag_rows = "".join(f'<tr><td>{esc(FLAG_LABEL.get(k, k))}</td><td class="n">{n}</td></tr>'
                            for k, n in sorted(cur.figures["flags"].items(), key=lambda kv: -kv[1]))
        guar = (f'<p>{g["members_guaranteeing"]} members guarantee {kes(g["pledged_cents"])} in all; '
                f'{kes(g["on_loans_behind_cents"])} of it is on loans more than 30 days behind.</p>'
                + (f'<table><tr><th>Warning sign</th><th class="n">Cases</th></tr>{flag_rows}</table>'
                   f'<p class="muted">Each case is listed on the Risk page of the Sawazi console. They are prompts to check, not findings.</p>'
                   if flag_rows else '<p>No warning signs in the guarantor network.</p>'))

    gov_rows = "".join(f'<tr><td>{e["at"]:%d %b %H:%M}</td><td>{esc(e["who"])}</td><td>{esc(e["what"])}</td>'
                       f'<td>{esc(e["detail"])}{(" · " + esc(e["note"])) if e["note"] else ""}</td></tr>' for e in d["governance"])
    gov = ((f'<table><tr><th>When</th><th>Who</th><th>What</th><th>Detail</th></tr>{gov_rows}</table>' if gov_rows
            else '<p>No changes to rules, products, staff access or API keys this month.</p>')
           + f'<p>Failed logins against staff accounts: {d["failed_logins"]}.</p>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{STYLE}</style></head><body><main>
<div class="brand"><svg width="28" height="28" viewBox="0 0 100 100" aria-hidden="true"><rect width="100" height="100" rx="24" fill="#0d1f18"/>
<path d="M72 22H42a14 14 0 0 0 0 28h7" fill="none" stroke="#4fc79a" stroke-width="13"/><circle cx="72" cy="22" r="6.5" fill="#4fc79a"/>
<path d="M51 50h7a14 14 0 0 1 0 28H28" fill="none" stroke="#f2b84b" stroke-width="13"/><circle cx="28" cy="78" r="6.5" fill="#f2b84b"/></svg>
SAWAZI BOARD PACK</div>
<h1>{esc(title)}</h1>
<p class="muted">Prepared by {esc(generated_by)} on {utcnow():%d %B %Y}. Sawazi works from the institution's own exports and payment records;
the core banking system remains the book of record.</p>
<section class="brief"><h2>In brief</h2><ul>{"".join(f"<li>{esc(x)}</li>" for x in _summary(d))}</ul></section>
<section><div class="kpis">{head}</div>{snap_note}</section>
<section><h2>1. Portfolio quality</h2>{_line_chart(d["trend"])}{quality}</section>
<section><h2>2. Collections and payments</h2>{collections}</section>
<section><h2>3. Lending</h2>{lending}</section>
<section><h2>4. Guarantor exposure</h2>{guar or '<p class="muted">Shown when a snapshot exists for the month.</p>'}</section>
<section><h2>5. Governance</h2>{gov}</section>
</main></body></html>"""

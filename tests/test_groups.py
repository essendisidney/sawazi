"""MFI group lending: joint-liability groups, meeting sheets, banked cash and split payments. Cash is never money
until its bank deposit is matched exactly; a payment for several members is never split by guesswork."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from sawazi.models import ExceptionItem, LendingGroup, Loan, MeetingSheet, Member, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


@pytest.fixture()
def mfi(env):
    c, Session = env
    with Session() as s:
        for i, (no, name, phone) in enumerate([("M1", "Achieng Owino", "254711000001"), ("M2", "Otieno Kamau", "254711000002"),
                                               ("M3", "Wanjiru Njeri", "254711000003")], start=10):
            s.add(Member(id=i, institution_id=1, member_no=no, name=name, phone=phone))
        s.flush()
        for i, (mid, arrears, days) in enumerate([(10, 0, 0), (11, 2_000_00, 40), (12, 0, 0)], start=10):
            s.add(Loan(id=i, institution_id=1, member_id=mid, loan_no=f"GL{i}", principal_cents=20_000_00,
                       balance_cents=12_000_00, installment_cents=1_000_00, arrears_cents=arrears, days_in_arrears=days))
        s.commit()
    officer = login(c, "credit_officer@a.test")
    r = c.post("/institutions/1/import/groups", headers=login(c, "accountant@a.test"), files={"file": ("g.csv", (
        "Group No,Group Name,Meeting Day,Frequency,Officer Email\n"
        "UMJ01,Umoja Women,Tuesday,weekly,credit_officer@a.test\n"
        "BAD1,Bad Day Group,Funday,weekly,\n").encode())})
    assert r.status_code == 200 and r.json()["created"] == 1 and "Funday" in r.json()["rejected"][0]
    r = c.post("/institutions/1/import/group_members", headers=login(c, "accountant@a.test"), files={"file": ("m.csv", (
        "Group No,Member No,Role\nUMJ01,M1,chair\nUMJ01,M2,member\nUMJ01,M3,treasurer\n").encode())})
    assert r.json()["created"] == 3
    with Session() as s:
        gid = s.scalar(select(LendingGroup.id).where(LendingGroup.group_no == "UMJ01"))
    return c, Session, gid, officer


def test_a_group_shows_its_shared_risk(mfi):
    c, Session, gid, officer = mfi
    g = c.get(f"/institutions/1/groups/{gid}", headers=officer).json()
    assert (g["members"], g["loans"], g["members_behind"], g["meeting_day"], g["officer"]) == (3, 3, 1, "Tuesday", "credit_officer a")
    assert g["arrears_kes"] == 2000 and g["par30_pct"] == round(100 * 12_000 / 36_000, 1)
    assert [m["role"] for m in g["member_list"]] == ["chair", "member", "treasurer"]
    assert c.get(f"/institutions/2/groups/{gid}", headers=login(c, "admin@b.test")).status_code == 404
    assert c.post("/institutions/1/groups", headers=login(c, "viewer@a.test"),
                  json={"group_no": "X1", "name": "X"}).status_code == 403


def test_a_member_is_in_one_group_at_a_time(mfi):
    c, Session, gid, officer = mfi
    other = c.post("/institutions/1/groups", headers=officer, json={"group_no": "ttm02", "name": "Tumaini Men"}).json()
    assert other["group_no"] == "TTM02"
    r = c.post(f"/institutions/1/groups/{other['id']}/members", headers=officer, json={"member_no": "M1"})
    assert r.status_code == 409 and "UMJ01" in r.json()["detail"]
    assert c.post(f"/institutions/1/groups/{gid}/members/M1/leave", headers=officer, params={"note": "moved to Kisumu"}).status_code == 200
    assert c.post(f"/institutions/1/groups/{other['id']}/members", headers=officer, json={"member_no": "M1"}).status_code == 200


def _meeting(c, Session, gid, officer, cash):
    with Session() as s:  # M1 paid 1,000 by M-Pesa the day before the meeting
        s.add(Transaction(institution_id=1, source="mpesa", reference="QMP1", txn_time=datetime(2026, 9, 7, 9),
                          amount_cents=1_000_00, member_id=10, status="allocated"))
        s.commit()
    sheet = c.post(f"/institutions/1/groups/{gid}/sheets", headers=officer, json={"meeting_on": "2026-09-08"}).json()
    due = {ln["member_no"]: (ln["due_kes"], ln["mpesa_kes"]) for ln in sheet["lines"]}
    assert due == {"M1": (1000, 1000), "M2": (3000, 0), "M3": (1000, 0)}  # M2 owes the instalment and the arrears
    lines = [{"member_no": no, "cash_kes": kes} for no, kes in cash.items()]
    r = c.put(f"/institutions/1/groups/{gid}/sheets/{sheet['id']}", headers=officer, json={"lines": lines})
    assert r.status_code == 200 and r.json()["totals"]["cash"] == sum(cash.values())
    r = c.post(f"/institutions/1/groups/{gid}/sheets/{sheet['id']}/submit", headers=officer)
    assert r.json()["status"] == ("submitted" if cash else "banked")  # no cash collected: no deposit to wait for
    assert c.put(f"/institutions/1/groups/{gid}/sheets/{sheet['id']}", headers=officer, json={"lines": lines}).status_code == 409
    return sheet["id"]


def test_cash_becomes_money_only_when_its_deposit_matches_exactly(mfi):
    c, Session, gid, officer = mfi
    sid = _meeting(c, Session, gid, officer, {"M2": 3000, "M3": 1000})
    acc = login(c, "accountant@a.test")
    c.post("/institutions/1/import/bank", headers=acc, files={"file": ("b.csv", (
        "Date,Narrative,Reference,Credit\n09/09/2026,UMOJA WOMEN GROUP DEP,BK1,3500\n09/09/2026,UMOJA WOMEN GROUP DEP,BK2,4000\n").encode())})
    deposits = c.get(f"/institutions/1/groups/{gid}/sheets/{sid}/deposits", headers=acc).json()
    assert [d["reference"] for d in deposits] == ["BK2"]  # only the deposit of the right amount is offered
    with Session() as s:
        short = s.scalar(select(Transaction.id).where(Transaction.reference == "BK1"))
        right = s.scalar(select(Transaction.id).where(Transaction.reference == "BK2"))
    r = c.post(f"/institutions/1/groups/{gid}/sheets/{sid}/bank", headers=acc, json={"transaction_id": short})
    assert r.status_code == 409 and "Nothing was changed" in r.json()["detail"]
    assert c.post(f"/institutions/1/groups/{gid}/sheets/{sid}/bank", headers=officer, json={"transaction_id": right}).status_code == 403
    r = c.post(f"/institutions/1/groups/{gid}/sheets/{sid}/bank", headers=acc, json={"transaction_id": right})
    assert r.status_code == 200 and r.json()["status"] == "banked" and r.json()["banked_reference"] == "BK2"
    with Session() as s:
        cash = {t.account_ref: (t.amount_cents, t.status) for t in s.scalars(select(Transaction).where(Transaction.source == "group_cash"))}
        assert cash == {"M2": (3_000_00, "allocated"), "M3": (1_000_00, "allocated")}
        assert s.scalar(select(Transaction.status).where(Transaction.id == right)) == "banked_sheet"
        assert s.get(Loan, 11).arrears_cents == 0  # M2's cash cleared the arrears
    postings = c.get("/institutions/1/exports/postings.csv", headers=acc).text
    assert "group_cash" in postings and "BK2" not in postings.split("\n", 1)[1].split("Cash at")[0]  # the deposit itself is not posted again
    again = c.post(f"/institutions/1/groups/{gid}/sheets/{sid}/bank", headers=acc, json={"transaction_id": right})
    assert again.status_code == 409


def test_a_meeting_with_no_cash_needs_no_deposit(mfi):
    c, Session, gid, officer = mfi
    sid = _meeting(c, Session, gid, officer, {})
    with Session() as s:
        assert s.get(MeetingSheet, sid).status == "banked"


def test_money_paid_to_the_group_account_is_never_given_to_one_member(mfi):
    c, Session, gid, officer = mfi
    acc = login(c, "accountant@a.test")
    c.post("/institutions/1/import/mpesa", headers=acc, files={"file": ("m.csv", (
        "Receipt No,Completion Time,Paid In,A/C No,Other Party Info,Transaction Status\n"
        "QGRP000001,2026-09-08 18:00:00,2500,UMJ01,254711000003 - WANJIRU NJERI,Completed\n").encode())})
    c.post("/institutions/1/match", headers=acc)
    with Session() as s:
        t = s.scalar(select(Transaction).where(Transaction.reference == "QGRP000001"))
        e = s.scalar(select(ExceptionItem).where(ExceptionItem.transaction_id == t.id))
        assert t.status == "suspense" and "group UMJ01" in e.detail
        eid = e.id
    r = c.post(f"/exceptions/{eid}/split", headers=acc, json={"note": "treasurer paid for two",
               "lines": [{"member_no": "M2", "amount_kes": 1500}, {"member_no": "M3", "amount_kes": 900}]})
    assert r.status_code == 422 and "must agree exactly" in r.json()["detail"]
    r = c.post(f"/exceptions/{eid}/split", headers=acc, json={"note": "treasurer paid for two",
               "lines": [{"member_no": "M2", "amount_kes": 1500}, {"member_no": "M3", "amount_kes": 1000}]})
    assert r.status_code == 200 and [x["member_no"] for x in r.json()["shares"]] == ["M2", "M3"]
    with Session() as s:
        parts = {t.reference: (t.member_id, t.amount_cents, t.status) for t in s.scalars(
            select(Transaction).where(Transaction.reference.like("QGRP000001/%")))}
        assert parts == {"QGRP000001/1": (11, 1_500_00, "allocated"), "QGRP000001/2": (12, 1_000_00, "allocated")}
        assert s.scalar(select(Transaction.status).where(Transaction.reference == "QGRP000001")) == "split"
    assert c.post(f"/exceptions/{eid}/split", headers=acc, json={"note": "again", "lines": [
        {"member_no": "M2", "amount_kes": 1500}, {"member_no": "M3", "amount_kes": 1000}]}).status_code == 404


def test_groups_count_in_risk_board_pack_and_collections(mfi):
    from sawazi.models import Reminder

    c, Session, gid, officer = mfi
    acc = login(c, "accountant@a.test")
    r = c.get("/institutions/1/risk", headers=acc).json()["groups"]
    assert (r["groups"], r["groups_behind"], r["members"]) == (1, 1, 3)
    assert r["behind"][0]["group_no"] == "UMJ01" and r["behind"][0]["arrears_kes"] == 2000

    assert c.post("/institutions/1/collections/queue", headers=officer).status_code == 200
    with Session() as s:
        rem = s.scalar(select(Reminder).where(Reminder.loan_id == 11))  # M2: 40 days behind, in Umoja Women
        assert rem.channel == "group_meeting"
        assert "Raise at Umoja Women's Tuesday meeting" in rem.message and "credit_officer a" in rem.message
        rid = rem.id
    sent = c.post("/institutions/1/reminders/send", headers=officer, json={"reminder_ids": [rid]}).json()
    assert sent["results"][0]["result"] == "skipped" and "note for staff" in sent["results"][0]["reason"]  # never texted

    assert c.post("/institutions/1/snapshots", headers=acc, params={"as_of": "2026-09-30"}).status_code == 200
    html = c.get("/institutions/1/board-pack.html", headers=acc, params={"month": "2026-09"}).text
    assert "5. Group lending" in html and "6. Governance" in html and "Umoja Women" in html
    assert "borrowing group(s) have members behind" in html


def test_a_loan_to_the_whole_group_sits_on_the_groups_own_account(mfi):
    from sawazi.models import Reminder

    c, Session, gid, officer = mfi
    with Session() as s:
        s.add_all([Member(id=13, institution_id=1, member_no="UMJ01A", name="Umoja Women Group"),
                   Member(id=14, institution_id=1, member_no="M4", name="Other Account")])
        s.flush()
        s.add(Loan(id=13, institution_id=1, member_id=13, loan_no="GRP13", principal_cents=100_000_00,
                   balance_cents=80_000_00, installment_cents=5_000_00, arrears_cents=5_000_00, days_in_arrears=5))
        s.commit()
    r = c.post("/institutions/1/import/group_members", headers=login(c, "accountant@a.test"), files={"file": ("m.csv", (
        "Group No,Member No,Role\nUMJ01,UMJ01A,Group Account\nUMJ01,M4,group account\n").encode())})
    assert r.json()["created"] == 1 and "already has its own account" in r.json()["rejected"][0]
    assert c.post(f"/institutions/1/groups/{gid}/members", headers=officer,
                  json={"member_no": "M4", "role": "group_account"}).status_code == 409
    g = c.get(f"/institutions/1/groups/{gid}", headers=officer).json()
    assert (g["members"], g["loans"], g["group_loan_kes"], g["group_loan_behind"], g["members_behind"]) == (3, 4, 80_000, True, 1)

    assert c.post("/institutions/1/collections/queue", headers=officer).status_code == 200
    with Session() as s:
        rem = s.scalar(select(Reminder).where(Reminder.loan_id == 13))  # only 5 days behind, still the group's business
        assert rem.channel == "group_meeting" and "the group's own loan GRP13" in rem.message

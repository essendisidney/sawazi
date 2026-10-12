"""Money integrity: a figure Sawazi cannot read is never treated as zero, and no payment is lost or misposted
because of how an export happens to be written."""
import pytest
from sqlalchemy import select

from sawazi import api
from sawazi.importers.common import BadValue, to_cents
from sawazi.models import CoreGuarantee, ExceptionItem, Loan, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

MEMBERS = "Member No,Name,Phone,ID Number\nM1,Achieng Owino,0711000010,11111111\nM2,Otieno Kamau,0711000020,22222222\n" \
          "2026,Alice Wanjiru,0711000030,33333333\n"


def up(c, h, kind, text, **params):
    return c.post(f"/institutions/1/import/{kind}", headers=h, params=params, files={"file": (f"{kind}.csv", text.encode())})


@pytest.fixture()
def acc(env):
    c, Session = env
    h = login(c, "accountant@a.test")
    assert up(c, h, "members", MEMBERS).status_code == 200
    return c, Session, h


@pytest.mark.parametrize("raw,cents", [
    ("1500", 150000), ("1,500.00", 150000), ("KSH 1,500.00", 150000), ("Kshs 1,500", 150000), ("KES1500", 150000),
    ("Ksh.500", 50000), ("1 500.00", 150000), ("1,000.00 CR", 100000), ("250 DR", -25000), ("(500)", -50000),
    ("500-", -50000), ("12.5", 1250), ("", 0), ("-", 0), (" ", 0), ("1500/=", 150000),
])
def test_amounts_as_kenyan_exports_write_them(raw, cents):
    assert to_cents(raw) == cents


@pytest.mark.parametrize("raw", ["N/A", "1.234,50", "12abc", "1.234", "1,5,0.0.0", "KES", "nan"])
def test_unreadable_amounts_are_refused_not_zero(raw):
    if raw == "KES":  # only a currency word: blank, not an error
        assert to_cents(raw) == 0
        return
    with pytest.raises(BadValue):
        to_cents(raw)


def test_a_loans_file_without_a_balance_column_imports_nothing(acc):
    c, Session, h = acc
    r = up(c, h, "loans", "Loan No,Member No,Principal,Arrears\nL1,M1,100000,0\n")
    assert r.status_code == 422 and "no balance column" in r.json()["detail"]
    r = up(c, h, "loans", "Loan No,Member No,Principal,Balance\nL1,M1,100000,50000\n")
    assert r.status_code == 422 and "no arrears column" in r.json()["detail"]
    with Session() as s:
        assert s.scalar(select(Loan).where(Loan.loan_no == "L1")) is None


def test_a_blank_balance_never_closes_a_loan_or_frees_its_guarantors(acc):
    c, Session, h = acc
    assert up(c, h, "loans", "Loan No,Member No,Principal,Outstanding Balance,Arrears,Days In Arrears\n"
                             "L1,M1,100000,60000,5000,40\n").status_code == 200
    assert up(c, h, "core_guarantees", "Loan No,Guarantor No,Amount Guaranteed\nL1,M2,30000\n").status_code == 200
    r = up(c, h, "loans", "Loan No,Member No,Principal,Outstanding Balance,Arrears,Days In Arrears\nL1,M1,100000,,0,0\n")
    assert r.status_code == 200 and "blank balance" in r.json()["rejected"][0]
    r = up(c, h, "loans", "Loan No,Member No,Principal,Outstanding Balance,Arrears,Days In Arrears\nL1,M1,100000,N/A,0,0\n")
    assert "not an amount" in r.json()["rejected"][0]
    r = up(c, h, "loans", "Loan No,Member No,Principal,Outstanding Balance,Arrears,Days In Arrears\nL1,M1,100000,60000,0,N/A\n")
    assert "days in arrears" in r.json()["rejected"][0]
    with Session() as s:
        ln = s.scalar(select(Loan).where(Loan.loan_no == "L1"))
        assert (ln.status, ln.balance_cents, ln.days_in_arrears) == ("active", 6000000, 40)
        assert s.scalar(select(CoreGuarantee.status)) == "active"


def test_written_off_loans_keep_their_guarantors_liable(acc):
    c, Session, h = acc
    up(c, h, "loans", "Loan No,Member No,Principal,Balance,Arrears\nL1,M1,100000,60000,5000\n")
    up(c, h, "core_guarantees", "Loan No,Guarantor No,Amount Guaranteed\nL1,M2,30000\n")
    r = up(c, h, "loans", "Loan No,Member No,Principal,Balance,Arrears,Status\nL1,M1,100000,0,0,Written Off\n")
    assert r.status_code == 200 and r.json()["guarantees_released"] == 0
    with Session() as s:
        assert s.scalar(select(Loan.status).where(Loan.loan_no == "L1")) == "written_off"
        assert s.scalar(select(CoreGuarantee.status)) == "active"


def test_payments_in_kenyan_formats_and_unreadable_ones(acc):
    c, Session, h = acc
    r = up(c, h, "mpesa", "Receipt No,Completion Time,Paid In,A/C No,Transaction Status\n"
                          "QAB1,2026-09-05 10:00:00,\"KSH 1,500.00\",M1,Completed\n"
                          "QAB2,2026-09-05 10:05:00,N/A,M1,Completed\n")
    assert r.json()["created"] == 1 and "QAB2" in r.json()["rejected"][0]
    with Session() as s:
        assert s.scalar(select(Transaction.amount_cents).where(Transaction.reference == "QAB1")) == 150000


def test_identical_bank_lines_are_two_payments_and_a_reupload_adds_none(acc):
    c, Session, h = acc
    statement = "Date,Narrative,Credit\n05/09/2026,CASH DEPOSIT,5000\n05/09/2026,CASH DEPOSIT,5000\n"
    assert up(c, h, "bank", statement).json()["created"] == 2
    again = up(c, h, "bank", statement).json()
    assert (again["created"], again["skipped_duplicates"]) == (0, 2)
    with Session() as s:
        assert sum(s.scalars(select(Transaction.amount_cents).where(Transaction.source == "bank"))) == 1000000


def test_a_date_or_bare_number_in_a_narrative_is_not_proof(acc):
    c, Session, h = acc
    up(c, h, "bank", "Date,Narrative,Credit\n"
                     "13/03/2026,CASH DEP 13/03/2026 BOB OTIENO,1000\n"       # date contains 2026: must not hit member 2026
                     "13/03/2026,DEPOSIT: 2026,2000\n"                         # bare number only: a clue, not proof
                     "13/03/2026,DEPOSIT: 2026 ALICE WANJIRU,3000\n"           # number confirmed by the name
                     "13/03/2026,DEP: 11111111 & 22222222,4000\n")             # two members' IDs: a person decides
    assert c.post("/institutions/1/match", headers=h).status_code == 200
    with Session() as s:
        by_amount = {t.amount_cents: t for t in s.scalars(select(Transaction).where(Transaction.source == "bank"))}
    assert by_amount[100000].status == "suspense" and by_amount[100000].match_method != "member_no"
    assert by_amount[200000].status == "suspense" and by_amount[200000].match_method == "narrative_number"
    assert by_amount[300000].status == "allocated"
    assert by_amount[400000].status == "suspense" and by_amount[400000].match_method == "narrative_ambiguous"


def test_a_corrected_checkoff_file_posts_only_the_difference(acc):
    c, Session, h = acc
    p = {"employer": "Teachers Service Commission", "period": "2026-09"}
    up(c, h, "checkoff_schedule", "Member No,Expected\nM1,5000\nM2,5000\n", **p)
    up(c, h, "checkoff_remittance", "Member No,Amount\nM1,3000\nM2,5000\n", **p)
    c.post("/institutions/1/checkoff/reconcile", headers=h, params=p)
    up(c, h, "checkoff_remittance", "Member No,Amount\nM1,5000\nM2,4000\n", **p)  # corrected: M1 up, M2 down
    c.post("/institutions/1/checkoff/reconcile", headers=h, params=p)
    c.post("/institutions/1/checkoff/reconcile", headers=h, params=p)  # running it again adds nothing
    with Session() as s:
        rows = s.execute(select(Transaction.account_ref, Transaction.amount_cents).where(Transaction.source == "checkoff")).all()
        assert sorted(rows) == [("M1", 200000), ("M1", 300000), ("M2", 500000)]
        assert s.scalar(select(ExceptionItem).where(ExceptionItem.kind == "checkoff_reduced")) is not None
    other = {"employer": "Teachers Service Comm NRB", "period": "2026-09"}  # same first 12 letters, other employer
    up(c, h, "checkoff_schedule", "Member No,Expected\nM2,1000\n", **other)
    up(c, h, "checkoff_remittance", "Member No,Amount\nM2,1000\n", **other)
    assert c.post("/institutions/1/checkoff/reconcile", headers=h, params=other).json()["transactions_created"] == 1


def test_unsafe_files_are_refused_whole(acc, monkeypatch):
    c, Session, h = acc
    r = up(c, h, "mpesa", "Receipt No,Paid In,Paid In \nQ1,100,200\n")
    assert r.status_code == 422 and "two columns" in r.json()["detail"]
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 50)
    r = up(c, h, "members", MEMBERS)
    assert r.status_code == 413
    r = up(c, h, "members", "Member No,Name,ID Number\nM9,Too Long Id," + "9" * 30 + "\n")
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 20 * 1_048_576)
    r = up(c, h, "members", "Member No,Name,ID Number\nM9,Too Long Id," + "9" * 30 + "\n")
    assert r.status_code == 200 and "longer than 20" in r.json()["rejected"][0]


def test_a_safaricom_reversal_is_raised_for_a_person_once(acc):
    c, Session, h = acc
    up(c, h, "mpesa", "Receipt No,Completion Time,Paid In,A/C No,Transaction Status\n"
                      "QAB1234567,2026-09-05 10:00:00,1500,M1,Completed\n")
    c.post("/institutions/1/match", headers=h)
    reversal = ("Receipt No,Completion Time,Paid In,Withdrawn,Details,Transaction Status\n"
                "QRV7654321,2026-09-06 09:00:00,,1500,Reversal of transaction QAB1234567,Completed\n")
    up(c, h, "mpesa", reversal)
    up(c, h, "mpesa", reversal)  # the same statement again raises nothing new
    with Session() as s:
        items = s.scalars(select(ExceptionItem).where(ExceptionItem.kind == "mpesa_reversal")).all()
        assert len(items) == 1 and items[0].severity == "high" and items[0].amount_cents == 150000

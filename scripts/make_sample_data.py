"""Generate realistic, fully fictional SACCO data with the messiness real exports have.

Output: sample_data/*.csv
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(7)
OUT = Path(__file__).resolve().parent.parent / "sample_data"
OUT.mkdir(exist_ok=True)

FIRST = ["Wanjiku", "Otieno", "Achieng", "Kamau", "Mutua", "Njeri", "Wekesa", "Chebet", "Kiprop", "Atieno",
         "Mwangi", "Nyambura", "Omondi", "Wairimu", "Kibet", "Muthoni", "Odhiambo", "Jeptoo", "Nduta", "Barasa",
         "Halima", "Juma", "Amina", "Hassan", "Zawadi", "Baraka", "Neema", "Imani", "Faith", "Peter",
         "Grace", "John", "Mary", "James", "Esther", "David", "Ruth", "Samuel", "Joyce", "Daniel"]
LAST = ["Kariuki", "Ochieng", "Mutiso", "Wambui", "Kiplagat", "Njoroge", "Owino", "Kosgei", "Musyoka", "Wafula",
        "Gitau", "Akinyi", "Rotich", "Mbugua", "Onyango", "Cheruiyot", "Ndungu", "Wanyama", "Mohamed", "Kilonzo",
        "Nyaga", "Okoth", "Kimani", "Langat", "Muriuki", "Oduya", "Macharia", "Too", "Mwende", "Simiyu"]
EMPLOYERS = ["Mwangaza County Payroll", "Tumaini Schools Ltd"]
PERIOD = "2026-09"


def w(name, header, rows):
    with open(OUT / name, "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(header)
        cw.writerows(rows)


# ---- members
members = []
used_phones = set()
for i in range(1, 321):
    while True:
        phone = "07" + "".join(random.choice("0123456789") for _ in range(8))
        if phone not in used_phones:
            used_phones.add(phone)
            break
    name = f"{random.choice(FIRST)} {random.choice(LAST)}"
    employer = random.choice(EMPLOYERS) if random.random() < 0.55 else ""
    members.append({
        "member_no": f"UT{i:05d}",
        "name": name,
        "phone": phone,
        "id_number": str(random.randint(20_000_000, 39_999_999)),
        "employer": employer,
    })
# a husband and wife sharing one phone (common in real data)
members[41]["phone"] = members[40]["phone"]
w("members.csv", ["Member No", "Name", "Phone", "ID Number", "Employer"],
  [[m["member_no"], m["name"], m["phone"], m["id_number"], m["employer"]] for m in members])

# ---- loans
loans = []
n = 0
for m in members:
    if random.random() < 0.82:
        n += 1
        principal = random.choice([50, 80, 100, 150, 200, 300, 450, 600, 900, 1200]) * 1000
        months = random.choice([12, 18, 24, 36, 48])
        inst = round(principal * (1 + 0.012 * months) / months, -1)
        paid = random.randint(1, months - 1)
        balance = round(principal * (1 + 0.012 * months) - inst * paid, 2)
        r = random.random()
        if r < 0.70:
            dpd, arr_months = 0, 0
        elif r < 0.80:
            dpd, arr_months = random.randint(1, 7), 1
        elif r < 0.88:
            dpd, arr_months = random.randint(8, 30), 1
        elif r < 0.93:
            dpd, arr_months = random.randint(31, 60), 2
        elif r < 0.96:
            dpd, arr_months = random.randint(61, 90), 3
        else:
            dpd, arr_months = random.randint(91, 400), random.randint(4, 10)
        arrears = min(inst * arr_months, balance)
        via = "Check-off" if m["employer"] else random.choice(["M-Pesa", "M-Pesa", "M-Pesa", "Bank"])
        disb = datetime(2026, 9, 1) - timedelta(days=30 * paid)
        loans.append({
            "loan_no": f"LN{26000 + n}", "member_no": m["member_no"], "member": m,
            "product": random.choice(["Development Loan", "Emergency Loan", "School Fees Loan", "Biashara Loan"]),
            "principal": principal, "balance": balance, "inst": inst, "arrears": arrears, "dpd": dpd,
            "via": via, "disbursed": disb.strftime("%d/%m/%Y"),
        })
# Arrears breakdown as a core system would report it. Derived (no random draws) so the rest of the data is unchanged.
def arrears_parts(l):
    penalty = round(l["arrears"] * 0.05) if l["dpd"] > 30 else 0
    return penalty, round(l["arrears"] * 0.2)


w("loans.csv", ["Loan No", "Member No", "Product", "Principal", "Balance", "Installment", "Arrears",
                "Penalty Arrears", "Interest Arrears", "Days In Arrears", "Disbursement Date", "Repayment Mode"],
  [[l["loan_no"], l["member_no"], l["product"], f"{l['principal']:,.2f}", f"{l['balance']:,.2f}",
    f"{l['inst']:,.2f}", f"{l['arrears']:,.2f}", *(f"{x:,.2f}" for x in arrears_parts(l)),
    l["dpd"], l["disbursed"], l["via"]] for l in loans])

# ---- M-Pesa paybill statement (Safaricom org portal layout)
def typo(s):
    i = random.randint(2, len(s) - 1)
    return s[:i] + random.choice("0123456789") + s[i + 1:]

mpesa = []
truth = {}  # receipt -> member the money really belongs to (for accuracy testing only)
t0 = datetime(2026, 9, 1, 6, 0)
balance = 1_250_000.0
receipt_n = 0

def receipt():
    global receipt_n
    receipt_n += 1
    return "TI" + "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(8))

mpesa_loans = [l for l in loans if l["via"] == "M-Pesa"]
for l in mpesa_loans:
    if l["dpd"] > 60 and random.random() < 0.8:
        continue  # deep arrears: mostly nobody pays
    for _ in range(random.choice([1, 1, 1, 2])):
        m = l["member"]
        amt = random.choice([l["inst"], l["inst"], l["inst"] / 2, l["inst"] + l["arrears"], round(l["inst"] * 1.1, -2)])
        r = random.random()
        payer_phone, payer_name = "254" + m["phone"][1:], m["name"].upper()
        if r < 0.62:
            ref = m["member_no"]
        elif r < 0.70:
            ref = random.choice([m["member_no"].lower(), m["member_no"][:2] + " " + m["member_no"][2:], m["member_no"] + " "])
        elif r < 0.76:
            ref = l["loan_no"]
        elif r < 0.83:
            ref = typo(m["member_no"])
        elif r < 0.86:
            ref = m["id_number"]
        elif r < 0.93:
            ref = random.choice(["", "loan", "LOAN REPAYMENT", "Sept", "0"])
        else:  # spouse or relative pays from their own phone with no usable ref
            ref = random.choice(["", "for mama", "loan"])
            payer_phone = "2547" + "".join(random.choice("0123456789") for _ in range(8))
            payer_name = f"{random.choice(FIRST)} {random.choice(LAST)}".upper()
        when = t0 + timedelta(minutes=random.randint(0, 29 * 24 * 60))
        balance += amt
        rc = receipt()
        truth[rc] = m["member_no"]
        mpesa.append([rc, when.strftime("%d-%m-%Y %H:%M:%S"), when.strftime("%d-%m-%Y %H:%M:%S"),
                      "Pay Bill Online", "Completed", f"{amt:.2f}", "", f"{balance:.2f}", "true",
                      "Pay Bill Online", f"{payer_phone} - {payer_name}", "", ref])
# deposits-only savers
for m in random.sample(members, 60):
    amt = random.choice([500, 1000, 2000, 2500, 5000])
    when = t0 + timedelta(minutes=random.randint(0, 29 * 24 * 60))
    balance += amt
    rc = receipt()
    truth[rc] = m["member_no"]
    mpesa.append([rc, when.strftime("%d-%m-%Y %H:%M:%S"), when.strftime("%d-%m-%Y %H:%M:%S"),
                  "Pay Bill Online", "Completed", f"{amt:.2f}", "", f"{balance:.2f}", "true", "Pay Bill Online",
                  f"254{m['phone'][1:]} - {m['name'].upper()}", "", m["member_no"]])
# accidental double payments
for row in random.sample(mpesa, 4):
    when = datetime.strptime(row[1], "%d-%m-%Y %H:%M:%S") + timedelta(minutes=random.randint(1, 9))
    dup = row.copy()
    truth_owner = truth.get(row[0])
    dup[0], dup[1], dup[2] = receipt(), when.strftime("%d-%m-%Y %H:%M:%S"), when.strftime("%d-%m-%Y %H:%M:%S")
    if truth_owner:
        truth[dup[0]] = truth_owner
    mpesa.append(dup)
# outgoing lines and charges the importer must ignore
for _ in range(8):
    when = t0 + timedelta(minutes=random.randint(0, 29 * 24 * 60))
    mpesa.append([receipt(), when.strftime("%d-%m-%Y %H:%M:%S"), "", "Business Payment Charge", "Completed",
                  "", "-33.00", "", "true", "Business Payment Charge", "", "", ""])
# a failed transaction
mpesa.append([receipt(), "15-09-2026 10:11:12", "", "Pay Bill Online", "Failed", "3000.00", "", "", "false",
              "Pay Bill Online", "254700000000 - FAILED PAYER", "", "UT00010"])
mpesa.sort(key=lambda r: datetime.strptime(r[1], "%d-%m-%Y %H:%M:%S"))
w("mpesa_statement.csv",
  ["Receipt No.", "Completion Time", "Initiation Time", "Details", "Transaction Status", "Paid In", "Withdrawn",
   "Balance", "Balance Confirmed", "Reason Type", "Other Party Info", "Linked Transaction ID", "A/C No."], mpesa)

# ---- bank statement
bank = []
for l in [x for x in loans if x["via"] == "Bank"]:
    if l["dpd"] > 30 and random.random() < 0.7:
        continue
    m = l["member"]
    when = datetime(2026, 9, random.randint(1, 29))
    narrative = random.choice([
        f"CASH DEP {m['member_no']} {m['name'].upper()}",
        f"RTGS/{m['name'].upper()}/LOAN {l['loan_no']}",
        f"PESALINK {m['name'].upper()} REF {m['member_no']}",
        f"CHQ DEP {m['name'].upper()}",
    ])
    bank.append([when.strftime("%d/%m/%Y"), narrative, f"FT26{random.randint(10**8, 10**9 - 1)}", f"{l['inst']:,.2f}", ""])
bank.append(["30/09/2026", "INTEREST CAPITALISED", "", "1,204.55", ""])
bank.append(["30/09/2026", "LEDGER FEE", "", "", "350.00"])
w("bank_statement.csv", ["Date", "Narrative", "Reference", "Credit", "Debit"], bank)

# ---- check-off schedule and remittance per employer
for emp in EMPLOYERS:
    tag = "county" if "County" in emp else "tumaini"
    emp_loans = [l for l in loans if l["member"]["employer"] == emp]
    sched, remit = [], []
    for l in emp_loans:
        m = l["member"]
        expected = l["inst"] + random.choice([0, 0, 500, 1000])  # loan + deposit contribution
        sched.append([m["member_no"], m["name"], f"{expected:.2f}"])
        r = random.random()
        if r < 0.78:
            got = expected
        elif r < 0.88:
            got = round(expected * random.choice([0.5, 0.6, 0.75]), -1)  # one-third rule cap
        elif r < 0.94:
            continue  # missing: left employment or dropped from payroll
        else:
            got = expected + 1000
        if random.random() < 0.12:  # payroll sent name only, no SACCO number
            remit.append(["", m["name"].upper(), f"{got:.2f}"])
        else:
            remit.append([m["member_no"], m["name"].upper(), f"{got:.2f}"])
    remit.append(["", f"{random.choice(FIRST).upper()} {random.choice(LAST).upper()}", "4500.00"])  # unknown person
    remit.append(["UT99999", "UNKNOWN STAFF", "2000.00"])
    w(f"checkoff_schedule_{tag}.csv", ["Member No", "Name", "Amount"], sched)
    w(f"checkoff_remittance_{tag}.csv", ["Member No", "Payroll Name", "Amount"], remit)

w("mpesa_truth.csv", ["receipt", "true_member_no"], sorted(truth.items()))
print(f"members={len(members)} loans={len(loans)} mpesa_lines={len(mpesa)} bank_lines={len(bank)} -> {OUT}")

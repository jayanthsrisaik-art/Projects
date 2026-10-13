"""Deterministic synthetic core-banking flat files (pipe-delimited) + SHA-256 manifest.

Injected faults (so the audit has something real to catch):
  * 2026-09-02 CORE batch: one malformed amount row (goes to stg_rejects, batch then fails to balance)
  * 2026-09-03 ACH batch: one credit line off by $0.01 (ledger no longer balances)
"""
import hashlib
import random
from datetime import date, timedelta
from pathlib import Path

from .money import fmt

DAYS = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
PRODUCTS = ["Auto", "Mortgage", "HELOC", "Personal"]
BRANCHES = [("B01", "North"), ("B02", "North"), ("B03", "South"), ("B04", "West")]


def _ds(d):
    return d.strftime("%Y%m%d")


def generate(out: Path, seed: int = 42, n_members: int = 20) -> None:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    members = {}
    for i in range(1, n_members + 1):
        br, rg = rng.choice(BRANCHES)
        members[f"M{i:03d}"] = {"branch": br, "region": rg, "score": rng.randrange(580, 800, 10),
                                "addr": f"{rng.randint(10, 999)} Oak St", "shares": rng.randint(1, 3)}
    loans = {}
    for i, mid in enumerate(rng.sample(sorted(members), 14), 1):
        loans[f"LN{i:03d}"] = {"member": mid, "product": PRODUCTS[i % 4], "tier": rng.choice(["A", "B", "C"]),
                               "balance": rng.randint(2000, 90000) * 100, "dpd": 0}
    for day_idx, d in enumerate(DAYS):
        if day_idx == 2:  # member M001 moves branch and score changes; M002 score drops
            members["M001"].update(branch="B04", region="West", score=members["M001"]["score"] + 40)
            members["M002"]["score"] -= 60
        if day_idx == 1:
            loans["LN001"]["dpd"] = 30
            loans["LN002"]["dpd"] = 60
        if day_idx == 2:
            loans["LN001"]["dpd"] = 60
            loans["LN002"]["dpd"] = 90
            loans["LN003"]["dpd"] = 30
        if day_idx == 3:
            loans["LN001"]["dpd"] = 0   # cured
        s = _ds(d)
        with open(out / f"members_{s}.txt", "w") as f:
            f.write("member_id|branch_id|region|credit_score|address|share_accounts\n")
            for m, v in members.items():
                f.write(f"{m}|{v['branch']}|{v['region']}|{v['score']}|{v['addr']}|{v['shares']}\n")
        with open(out / f"loans_{s}.txt", "w") as f:
            f.write("loan_id|member_id|product|rate_tier|balance|days_past_due\n")
            for l, v in loans.items():
                f.write(f"{l}|{v['member']}|{v['product']}|{v['tier']}|{fmt(v['balance'])}|{v['dpd']}\n")

        journal, controls = [], []
        for channel in ("ACH", "CORE"):
            batch = f"{s}-{channel}"
            deb = cred = 0
            for t in range(1, rng.randint(6, 10)):
                mid = rng.choice(sorted(members))
                if loans and rng.random() < 0.4:
                    acct = rng.choice(sorted(loans))
                    mid = loans[acct]["member"]
                else:
                    acct = f"SH-{mid}"
                amt = rng.randint(1000, 250000)  # cents
                tid = f"{batch}-{t:03d}"
                journal.append(f"{batch}|{tid}-D|GL-CASH||D|{fmt(amt)}|{d.isoformat()}")
                journal.append(f"{batch}|{tid}-C|{acct}|{mid}|C|{fmt(amt)}|{d.isoformat()}")
                deb += amt
                cred += amt
            controls.append(f"{batch}|{fmt(deb)}|{fmt(cred)}")
            if d == date(2026, 9, 2) and channel == "CORE":
                journal[-1] = journal[-1].rsplit("|", 2)[0] + "|12x.00|" + d.isoformat()
            if d == date(2026, 9, 3) and channel == "ACH":
                parts = journal[-1].split("|")
                parts[5] = fmt(int(parts[5].replace(".", "")) + 1)
                journal[-1] = "|".join(parts)
        with open(out / f"journal_{s}.txt", "w") as f:
            f.write("batch_id|txn_id|account_id|member_id|entry_type|amount|txn_date\n")
            f.write("\n".join(journal) + "\n")
        with open(out / f"controls_{s}.txt", "w") as f:
            f.write("batch_id|control_debits|control_credits\n" + "\n".join(controls) + "\n")
    write_manifest(out)


def write_manifest(out: Path) -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
             for p in sorted(out.glob("*.txt"))]
    (out / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")

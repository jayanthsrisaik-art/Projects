"""Seeded synthetic corpus of support-style messages with gold PII spans.

Leakage controls for the evaluation:
* dev and test use *disjoint templates* (different phrasing and layouts);
* test persons are half gazetteer names and half held-out names that appear
  neither in the detector gazetteer nor in the dev split;
* every document also carries hard negatives (order ids, invalid card
  numbers, version strings, out-of-range SSNs, capitalised non-names) that
  must NOT be redacted.

The detector rules were tuned on ``dev`` only; ``test`` is reported.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass

from .detectors import FIRST_NAMES

# Given names: gazetteer ones and held-out ones (not in FIRST_NAMES).
GAZ_FIRST = sorted(FIRST_NAMES)
DEV_HELDOUT_FIRST = ["Tobias", "Ingrid", "Marek", "Leilani", "Rashid", "Svetlana", "Kofi", "Annika"]
TEST_HELDOUT_FIRST = ["Kwame", "Nadia", "Bjorn", "Thandiwe", "Esteban", "Mirela", "Haruto", "Oluwaseun", "Dagny", "Faisal"]
DEV_SURNAMES = ["Smith", "Garcia", "Chen", "Okafor", "Novak", "Haddad", "Kowalski", "Brennan", "Patel", "Lindqvist"]
TEST_SURNAMES = ["Mensah", "Ferreira", "Tanaka", "Abernathy", "Volkov", "Achterberg", "Nakamura", "Oyelaran", "Castellanos", "Dube"]
STREETS = ["Elm", "Maple", "Harbor View", "Old Mill", "Cedar", "Willow Creek", "Granite", "Juniper"]
SUFFIXES = ["Street", "Avenue", "Road", "Lane", "Drive", "Blvd", "Court", "Way"]
DOMAINS = ["example.com", "mail.example.org", "corp.example.net"]
IBAN_COUNTRIES = {"DE": 18, "GB": 18, "FR": 23, "NL": 14}
NON_NAME_CAPS = ["Paris", "Monday", "Visa", "Premium", "Chrome", "Austin", "Kubernetes", "October", "Gold"]

# Template mini-language: {SLOT} is a PII slot (gold), {!SLOT} a hard negative.
DEV_TEMPLATES = [
    "Hi, my name is {NAME} and my card {CARD} was charged twice. Order #{!ORDER} is the duplicate.",
    "Please update the email on file to {EMAIL}. You can call me on {PHONE} if needed. Thanks, {FIRST}.",
    "Customer {NAME} reports login failures from {IP} after upgrading to version {!VERSION}.",
    "Ship the replacement to {ADDRESS} for {NAME}. The old card ending {!BADCARD} was cancelled.",
    "SSN on the application is {SSN}; the form also lists {!BADSSN} which our system rejected.",
    "Refund to IBAN {IBAN} please. Account holder {NAME}, phone {PHONE}.",
    "Spoke with {NAME} on {!CAP}. {SURNAME} wants a callback at {PHONE} before noon.",
    "Escalation: Mr. {SURNAME} cannot reach {EMAIL} from the {!CAP} plan. Ticket {!ORDER}.",
    "Dear support, I am writing about invoice {!ORDER}. Regards,\n{NAME}",
    "Ticket from {EMAIL}: the server at {IP} returned 502. Contact {NAME}.",
]
TEST_TEMPLATES = [
    "Hello team, this is {NAME}. I moved to {ADDRESS} and need my billing address changed. Reference {!ORDER}.",
    "Patient {NAME} (SSN {SSN}) missed the appointment on {!CAP}. Reach them at {PHONE}.",
    "Chargeback dispute: card {CARD}, cardholder {NAME}. Our internal id {!BADCARD} is unrelated.",
    "The VPN log shows {IP} connecting with client build {!VERSION}. Owner: {EMAIL}.",
    "Please wire the deposit to {IBAN}. Questions go to {EMAIL} or {PHONE}. {FIRST} will confirm.",
    "Called {NAME} about tracking number {!ORDER}; {SURNAME} asked us to use {EMAIL} instead.",
    "Dr. {NAME} approved the request. The rejected identifier {!BADSSN} was a typo. {SURNAME} signed off.",
    "Tenant {NAME} at {ADDRESS} reports a leak. Best number: {PHONE}. Gold tier since {!CAP}.",
    "Thanks for the help!\nCheers,\n{NAME}\n{PHONE}",
    "New signup {EMAIL} from {IP}; profile name {NAME}. Card on file {CARD}.",
]


@dataclass
class Gold:
    start: int
    end: int
    type: str
    text: str


@dataclass
class Doc:
    id: str
    text: str
    entities: list[Gold]


def _luhn_complete(prefix: str) -> str:
    for d in "0123456789":
        cand = prefix + d
        total, dbl = 0, False
        for ch in reversed(cand):
            v = int(ch) * (2 if dbl else 1)
            total += v - 9 if v > 9 else v
            dbl = not dbl
        if total % 10 == 0:
            return cand
    raise AssertionError


def _card(rng: random.Random, valid: bool = True) -> str:
    prefix = rng.choice(["4", "51", "37", "6011"])
    length = 15 if prefix == "37" else 16
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(length - len(prefix) - 1))
    num = _luhn_complete(body)
    if not valid:
        num = num[:-1] + str((int(num[-1]) + 1) % 10)
    sep = rng.choice([" ", "-", ""])
    if length == 16:
        return sep.join(num[i:i + 4] for i in range(0, 16, 4))
    return sep.join([num[:4], num[4:10], num[10:]])


def _iban(rng: random.Random) -> str:
    cc = rng.choice(sorted(IBAN_COUNTRIES))
    bban = "".join(str(rng.randint(0, 9)) for _ in range(IBAN_COUNTRIES[cc]))
    if cc in ("GB", "NL"):
        bban = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)) + bban[4:]
    num = "".join(str(int(c, 36)) for c in bban + cc + "00")
    check = 98 - int(num) % 97
    raw = f"{cc}{check:02d}{bban}"
    return " ".join(raw[i:i + 4] for i in range(0, len(raw), 4)) if rng.random() < 0.6 else raw


def _phone(rng: random.Random) -> str:
    a, b, c = rng.randint(201, 989), rng.randint(200, 999), rng.randint(0, 9999)
    return rng.choice([
        f"({a}) {b}-{c:04d}", f"{a}-{b}-{c:04d}", f"{a}.{b}.{c:04d}", f"+1 {a} {b} {c:04d}",
        f"+44 20 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
    ])


def _ssn(rng: random.Random) -> str:
    area = rng.choice([rng.randint(1, 665), rng.randint(667, 899)])
    return f"{area:03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"


def _render(template: str, values: dict[str, str]) -> tuple[str, list[Gold]]:
    out, gold, pos = [], [], 0
    for m in re.finditer(r"\{(!?)([A-Z]+)\}", template):
        lit = template[pos:m.start()]
        out.append(lit)
        start = sum(len(x) for x in out)
        val = values[("!" if m.group(1) else "") + m.group(2)]
        out.append(val)
        if not m.group(1):
            gtype = {"NAME": "PERSON", "FIRST": "PERSON", "SURNAME": "PERSON", "CARD": "CREDIT_CARD"}.get(
                m.group(2), m.group(2))
            gtype = "IPV4" if gtype == "IP" else gtype
            gold.append(Gold(start, start + len(val), gtype, val))
        pos = m.end()
    out.append(template[pos:])
    return "".join(out), gold


def generate(split: str, n: int, seed: int) -> list[Doc]:
    rng = random.Random(f"{split}-{seed}")
    if split == "dev":
        templates, heldout, surnames = DEV_TEMPLATES, DEV_HELDOUT_FIRST, DEV_SURNAMES
    elif split == "test":
        templates, heldout, surnames = TEST_TEMPLATES, TEST_HELDOUT_FIRST, TEST_SURNAMES
    else:
        raise ValueError(split)
    docs = []
    for i in range(n):
        first = rng.choice(heldout) if rng.random() < 0.5 else rng.choice(GAZ_FIRST).capitalize()
        surname = rng.choice(surnames)
        values = {
            "NAME": f"{first} {surname}",
            "FIRST": first,
            "SURNAME": surname,
            "EMAIL": f"{first.lower()}.{surname.lower()}{rng.randint(1, 99)}@{rng.choice(DOMAINS)}",
            "PHONE": _phone(rng),
            "CARD": _card(rng),
            "SSN": _ssn(rng),
            "IBAN": _iban(rng),
            "IP": ".".join(str(rng.randint(1, 254)) for _ in range(4)),
            "ADDRESS": f"{rng.randint(1, 9999)} {rng.choice(STREETS)} {rng.choice(SUFFIXES)}",
            "!ORDER": str(rng.randint(10**9, 10**10 - 1)),
            "!BADCARD": _card(rng, valid=False),
            "!VERSION": f"{rng.randint(1, 12)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}.{rng.randint(0, 99)}",
            "!BADSSN": f"{rng.choice(['000', '666', '912'])}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
            "!CAP": rng.choice(NON_NAME_CAPS),
        }
        text, gold = _render(rng.choice(templates), values)
        docs.append(Doc(f"{split}-{i:04d}", text, gold))
    return docs


def write_jsonl(docs: list[Doc], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(asdict(d), ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list[Doc]:
    docs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = json.loads(line)
            docs.append(Doc(raw["id"], raw["text"], [Gold(**g) for g in raw["entities"]]))
    return docs

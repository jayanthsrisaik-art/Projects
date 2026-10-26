"""Deterministic synthetic customer data from three source systems, with ground truth.

The generator deliberately produces the cases that break naive matching:
nicknames, surname typos, swapped name fields, Gmail dot/+tag variants, phone
formats, US vs ISO dates, street abbreviations, address moves, new emails,
missing fields, same-source duplicates, and *households* (different people who
share a surname, address and landline) plus common-name collisions.

Ground truth goes to a separate file and is never read by the pipeline.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from goldenrec.normalize import FIELDS, NICKNAMES

FIRST = ["robert", "william", "elizabeth", "james", "michael", "katherine", "thomas", "david",
         "jennifer", "christopher", "alexander", "samuel", "daniel", "joseph", "matthew",
         "nicholas", "anthony", "steven", "richard", "margaret", "susan", "patricia", "andrew",
         "maria", "olivia", "emma", "noah", "priya", "wei", "fatima", "carlos", "aisha", "hiro",
         "lucas", "sofia", "grace", "ethan", "chloe", "omar", "nina"]
LAST = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
        "rodriguez", "martinez", "hernandez", "lopez", "wilson", "anderson", "thomas", "taylor",
        "moore", "jackson", "martin", "lee", "thompson", "white", "harris", "clark", "lewis",
        "robinson", "walker", "young", "allen", "king", "wright", "scott", "nguyen", "patel",
        "kim", "chen", "okafor", "kowalski", "schneider", "rossi"]
STREETS = ["oak", "maple", "pine", "cedar", "elm", "washington", "lake", "hill", "park", "main",
           "sunset", "river", "highland", "jefferson", "lincoln"]
SUFFIX = [("street", "st"), ("avenue", "ave"), ("road", "rd"), ("drive", "dr"), ("lane", "ln")]
CITIES = [("springfield", "62701"), ("riverton", "82501"), ("fairview", "97024"),
          ("madison", "53703"), ("georgetown", "78626"), ("franklin", "37064")]
DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "example.org", "mail.com"]
SOURCES = ["crm", "billing", "support"]
REVERSE_NICK: dict[str, list[str]] = {}
for nick, formal in NICKNAMES.items():
    REVERSE_NICK.setdefault(formal, []).append(nick)


def _typo(rng: random.Random, word: str) -> str:
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    kind = rng.choice(["swap", "drop", "sub"])
    if kind == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == "drop":
        return word[:i] + word[i + 1:]
    return word[:i] + rng.choice("aeiourstnl") + word[i + 1:]


def _address(rng: random.Random) -> dict:
    city, zipc = rng.choice(CITIES)
    long_s, short_s = rng.choice(SUFFIX)
    return {"number": rng.randint(10, 9999), "name": rng.choice(STREETS), "suffix": (long_s, short_s),
            "apt": rng.randint(1, 40) if rng.random() < 0.25 else None, "city": city, "zip": zipc}


def _render_street(rng: random.Random, addr: dict) -> str:
    long_s, short_s = addr["suffix"]
    suffix = rng.choice([long_s.title(), short_s.title(), short_s.title() + "."])
    street = f"{addr['number']} {addr['name'].title()} {suffix}"
    if addr["apt"]:
        street += rng.choice([f" Apt {addr['apt']}", f" #{addr['apt']}", f", Apartment {addr['apt']}"])
    return street


def _phone(rng: random.Random) -> str:
    return f"{rng.randint(201, 989)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}"


def _render_phone(rng: random.Random, p: str) -> str:
    return rng.choice([p, f"({p[:3]}) {p[3:6]}-{p[6:]}", f"{p[:3]}.{p[3:6]}.{p[6:]}",
                       f"+1 {p[:3]} {p[3:6]} {p[6:]}", f"1-{p[:3]}-{p[3:6]}-{p[6:]}"])


def _render_email(rng: random.Random, email: str) -> str:
    local, domain = email.split("@")
    if domain == "gmail.com" and rng.random() < 0.3:
        local = local.replace(".", "") if "." in local else local[:3] + "." + local[3:]
    if rng.random() < 0.15:
        local += "+" + rng.choice(["shop", "news", "billing"])
    out = f"{local}@{domain}"
    return out.upper() if rng.random() < 0.1 else out


def _render_dob(rng: random.Random, d: date) -> str:
    return rng.choice([d.isoformat(), d.strftime("%m/%d/%Y"), d.strftime("%Y%m%d")])


def make_entities(rng: random.Random, n: int) -> list[dict]:
    people = []
    while len(people) < n:
        last = rng.choice(LAST)
        addr = _address(rng)
        landline = _phone(rng)
        # ~30% of households have 2-3 members sharing surname, address and landline
        size = 1 if rng.random() < 0.7 else rng.choice([2, 2, 3])
        firsts = rng.sample(FIRST, size)
        for first in firsts:
            dob = date(1950, 1, 1) + timedelta(days=rng.randint(0, 365 * 55))
            sep = rng.choice([".", "_", ""])
            email = f"{first}{sep}{last}{rng.randint(1, 99)}@{rng.choice(DOMAINS)}"
            phone = landline if size > 1 and rng.random() < 0.7 else _phone(rng)
            people.append({"entity_id": f"E{len(people) + 1:05d}", "first": first, "last": last, "dob": dob,
                           "email": email, "phone": phone, "addr": addr})
    return people[:n]


def make_records(rng: random.Random, person: dict, counter: dict) -> list[dict]:
    n_records = rng.choices([1, 2, 3, 4], weights=[25, 35, 28, 12])[0]
    sources = [rng.choice(SOURCES) for _ in range(n_records)]
    moved = _address(rng) if rng.random() < 0.15 else None
    new_email = (f"{person['first'][0]}{person['last']}{rng.randint(100, 999)}@{rng.choice(DOMAINS)}"
                 if rng.random() < 0.15 else None)
    out = []
    for i, source in enumerate(sources):
        counter[source] = counter.get(source, 0) + 1
        updated = date(2024, 1, 1) + timedelta(days=rng.randint(0, 600))
        first, last = person["first"], person["last"]
        if rng.random() < 0.25 and REVERSE_NICK.get(first):
            first = rng.choice(REVERSE_NICK[first])
        if rng.random() < 0.08:
            last = _typo(rng, last)
        if rng.random() < 0.03:
            first, last = last, first
        addr = person["addr"]
        if moved and i > 0 and rng.random() < 0.6:
            addr, updated = moved, updated + timedelta(days=200)
        email = new_email if new_email and i > 0 and rng.random() < 0.6 else person["email"]
        row = {
            "source": source,
            "source_id": f"{source[:3].upper()}-{counter[source]:06d}",
            "first_name": first.title() if rng.random() < 0.85 else first.upper(),
            "last_name": last.title() if rng.random() < 0.85 else last.upper(),
            "email": "" if rng.random() < 0.18 else _render_email(rng, email),
            "phone": "" if rng.random() < 0.2 else _render_phone(rng, person["phone"]),
            "dob": "" if rng.random() < 0.25 else _render_dob(rng, person["dob"]),
            "street": "" if rng.random() < 0.1 else _render_street(rng, addr),
            "city": addr["city"].title(),
            "zip": addr["zip"] if rng.random() < 0.8 else f"{addr['zip']}-{rng.randint(1000, 9999)}",
            "updated_at": updated.isoformat(),
        }
        out.append((row, person["entity_id"]))
    return out


def generate(n_entities: int, seed: int = 7) -> tuple[list[dict], list[dict]]:
    """Return (records, truth). Records are shuffled so file order carries no signal."""
    rng = random.Random(seed)
    counter: dict[str, int] = {}
    pairs = []
    for person in make_entities(rng, n_entities):
        pairs.extend(make_records(rng, person, counter))
    rng.shuffle(pairs)
    records = [{"source": r["source"], "source_id": r["source_id"], **{f: r[f] for f in FIELDS}} for r, _ in pairs]
    truth = [{"source": r["source"], "source_id": r["source_id"], "entity_id": e} for r, e in pairs]
    return records, truth

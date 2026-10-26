"""Field normalisation: turn messy source values into comparable canonical forms.

Every function returns ``None`` for values that are missing or unusable, so the
matcher can tell "unknown" apart from "different".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

NICKNAMES = {
    "bob": "robert", "bobby": "robert", "rob": "robert", "robbie": "robert",
    "bill": "william", "billy": "william", "will": "william", "liam": "william",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth", "eliza": "elizabeth",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "mike": "michael", "mikey": "michael",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "tom": "thomas", "tommy": "thomas",
    "dave": "david", "davey": "david",
    "jen": "jennifer", "jenny": "jennifer",
    "chris": "christopher",
    "alex": "alexander",
    "sam": "samuel",
    "dan": "daniel", "danny": "daniel",
    "joe": "joseph", "joey": "joseph",
    "matt": "matthew",
    "nick": "nicholas",
    "tony": "anthony",
    "steve": "steven",
    "rick": "richard", "dick": "richard", "rich": "richard",
    "peggy": "margaret", "maggie": "margaret", "meg": "margaret",
    "sue": "susan", "susie": "susan",
    "pat": "patricia", "patty": "patricia",
    "andy": "andrew", "drew": "andrew",
}

STREET_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "lane": "ln", "boulevard": "blvd", "court": "ct", "place": "pl",
    "terrace": "ter", "highway": "hwy", "parkway": "pkwy", "circle": "cir",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "apartment": "apt", "suite": "ste", "unit": "apt", "#": "apt",
}

GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _blank(value: str | None) -> bool:
    return value is None or not str(value).strip()


def norm_name(value: str | None) -> str | None:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if _blank(value):
        return None
    text = _ascii_fold(str(value)).lower()
    text = re.sub(r"[^a-z\s]", " ", text.replace("'", ""))
    text = " ".join(text.split())
    return text or None


def canonical_first(value: str | None) -> str | None:
    """Normalised first name with common nicknames mapped to the formal name."""
    name = norm_name(value)
    if name is None:
        return None
    head = name.split()[0]
    return NICKNAMES.get(head, head)


def norm_email(value: str | None) -> str | None:
    """Lowercase; drop +tags everywhere and dots in the local part for Gmail."""
    if _blank(value):
        return None
    text = str(value).strip().lower()
    if text.count("@") != 1:
        return None
    local, domain = text.split("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    if not local or "." not in domain:
        return None
    return f"{local}@{domain}"


def norm_phone(value: str | None) -> str | None:
    """Ten-digit NANP number, or None if it can't be one."""
    if _blank(value):
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


_DOB_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), ("m", "d", "y")),  # US style
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), ("y", "m", "d")),
]


def norm_dob(value: str | None) -> str | None:
    """ISO date from ISO, YYYY/MM/DD, US MM/DD/YYYY or YYYYMMDD; None if invalid."""
    if _blank(value):
        return None
    text = str(value).strip()
    for pattern, order in _DOB_PATTERNS:
        match = pattern.match(text)
        if match:
            parts = dict(zip(order, (int(g) for g in match.groups())))
            try:
                return date(parts["y"], parts["m"], parts["d"]).isoformat()
            except ValueError:
                return None
    return None


def norm_street(value: str | None) -> str | None:
    if _blank(value):
        return None
    text = _ascii_fold(str(value)).lower().replace("#", " # ")
    text = re.sub(r"[^a-z0-9#\s]", " ", text)
    tokens = [STREET_ABBREV.get(tok, tok) for tok in text.split()]
    return " ".join(tokens) or None


def norm_zip(value: str | None) -> str | None:
    if _blank(value):
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[:5] if len(digits) >= 5 else None


@dataclass(frozen=True)
class NormRecord:
    """A source record with raw and normalised fields side by side."""

    source: str
    source_id: str
    raw: dict
    first: str | None
    first_canon: str | None
    last: str | None
    email: str | None
    phone: str | None
    dob: str | None
    street: str | None
    city: str | None
    zip: str | None
    updated_at: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)


FIELDS = ("first_name", "last_name", "email", "phone", "dob", "street", "city", "zip", "updated_at")


def normalize_record(row: dict) -> NormRecord:
    """Normalise one input row (dict with ``source``, ``source_id`` and FIELDS)."""
    for required in ("source", "source_id"):
        if _blank(row.get(required)):
            raise ValueError(f"record is missing {required!r}: {row}")
    last = norm_name(row.get("last_name"))
    return NormRecord(
        source=str(row["source"]).strip(),
        source_id=str(row["source_id"]).strip(),
        raw={f: (row.get(f) or "").strip() for f in FIELDS},
        first=norm_name(row.get("first_name")),
        first_canon=canonical_first(row.get("first_name")),
        last=last,
        email=norm_email(row.get("email")),
        phone=norm_phone(row.get("phone")),
        dob=norm_dob(row.get("dob")),
        street=norm_street(row.get("street")),
        city=norm_name(row.get("city")),
        zip=norm_zip(row.get("zip")),
        updated_at=(row.get("updated_at") or "").strip(),
    )

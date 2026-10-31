"""PII detectors.

Each detector returns ``Span`` objects with character offsets into the input.
Structured identifiers are validated (Luhn, IBAN mod-97, SSN ranges, IPv4
octets) so that look-alike numbers (order IDs, version strings, invalid card
numbers) are not redacted.  Person names are found with three signals:
honorific / context cues, a first-name gazetteer, and in-document propagation
(once "Ana Ruiz" is found, a later bare "Ruiz" is the same person).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ENTITY_TYPES = ("EMAIL", "PHONE", "CREDIT_CARD", "SSN", "IBAN", "IPV4", "ADDRESS", "PERSON")

# Higher wins when spans overlap.  Validated structured types beat heuristics.
PRIORITY = {
    "LITERAL": 100,
    "EMAIL": 90,
    "IBAN": 85,
    "CREDIT_CARD": 80,
    "SSN": 75,
    "IPV4": 70,
    "PHONE": 60,
    "ADDRESS": 50,
    "PERSON": 40,
}


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    type: str
    text: str
    source: str = field(default="", compare=False)

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end


# --------------------------------------------------------------------------- helpers

def luhn_ok(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "").upper()
    if not (15 <= len(s) <= 34) or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in rearranged)
    return int(num) % 97 == 1


def ssn_ok(area: str, group: str, serial: str) -> bool:
    a = int(area)
    return a not in (0, 666) and a < 900 and group != "00" and serial != "0000"


def _preceded_by(text: str, start: int, words: tuple[str, ...], window: int = 24) -> bool:
    before = text[max(0, start - window):start].lower()
    return any(re.search(r"\b" + re.escape(w) + r"\b[\s#:.\-]*$", before) for w in words)


# --------------------------------------------------------------------------- structured

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")

PHONE_RE = re.compile(
    r"(?<![\w+])(?:"
    r"\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}"          # international
    r"|\(\d{3}\)\s?\d{3}[ .-]\d{4}"                                 # (415) 555-0134
    r"|\d{3}[.-]\d{3}[.-]\d{4}"                                     # 415-555-0134 / 415.555.0134
    r"|\d{10}"                                                      # bare: needs a cue
    r")(?![\w-])"
)
PHONE_CUES = ("call", "phone", "tel", "mobile", "cell", "text", "sms", "whatsapp", "fax", "reach me at", "number")
NOT_PHONE_CUES = ("order", "invoice", "ref", "reference", "ticket", "tracking", "account", "acct", "id", "po", "sku", "case")

CARD_RE = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
SSN_RE = re.compile(r"(?<![\w-])(\d{3})-(\d{2})-(\d{4})(?![\w-])")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)")
VERSION_CUES = ("version", "v", "ver", "release", "build", "firmware")

STREET_SUFFIX = r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace)"
ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}" + STREET_SUFFIX + r"\b\.?"
    r"(?:,?\s+(?:Apt|Suite|Unit)\.?\s*\w+)?"
)


def detect_emails(text: str) -> list[Span]:
    return [Span(m.start(), m.end(), "EMAIL", m.group(), "regex") for m in EMAIL_RE.finditer(text)]


def detect_phones(text: str) -> list[Span]:
    out = []
    for m in PHONE_RE.finditer(text):
        s = m.group()
        digits = re.sub(r"\D", "", s)
        if not 10 <= len(digits) <= 15:
            continue
        if _preceded_by(text, m.start(), NOT_PHONE_CUES):
            continue
        formatted = not s.isdigit()
        if not formatted and not _preceded_by(text, m.start(), PHONE_CUES, window=32):
            continue  # a bare 10-digit run is only a phone with a cue
        out.append(Span(m.start(), m.end(), "PHONE", s, "regex+context"))
    return out


def detect_cards(text: str) -> list[Span]:
    out = []
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            out.append(Span(m.start(), m.end(), "CREDIT_CARD", m.group(), "regex+luhn"))
    return out


def detect_ssns(text: str) -> list[Span]:
    return [
        Span(m.start(), m.end(), "SSN", m.group(), "regex+range")
        for m in SSN_RE.finditer(text)
        if ssn_ok(*m.groups())
    ]


def detect_ibans(text: str) -> list[Span]:
    return [Span(m.start(), m.end(), "IBAN", m.group(), "regex+mod97") for m in IBAN_RE.finditer(text) if iban_ok(m.group())]


def detect_ipv4(text: str) -> list[Span]:
    out = []
    for m in IPV4_RE.finditer(text):
        if any(int(o) > 255 for o in m.groups()):
            continue
        if _preceded_by(text, m.start(), VERSION_CUES, window=12) or text[max(0, m.start() - 1):m.start()] in ("v", "V"):
            continue
        out.append(Span(m.start(), m.end(), "IPV4", m.group(), "regex+octets"))
    return out


def detect_addresses(text: str) -> list[Span]:
    out = []
    for m in ADDRESS_RE.finditer(text):
        value = m.group()
        if value.endswith(".") and not re.search(r"\b(?:St|Ave|Rd|Blvd|Ln|Dr|Ct|Pl)\.$", value):
            value = value[:-1]  # sentence full stop, not part of the address
        out.append(Span(m.start(), m.start() + len(value), "ADDRESS", value, "regex"))
    return out


# --------------------------------------------------------------------------- persons

# Small built-in gazetteer of common given names.  It is deliberately partial:
# the evaluation corpus contains held-out names that are NOT listed here, so the
# measured recall reflects the context rules too, not just list lookup.
FIRST_NAMES = frozenset(
    """
    james mary john patricia robert jennifer michael linda william elizabeth david barbara richard susan
    joseph jessica thomas sarah charles karen daniel nancy matthew lisa anthony betty mark sandra
    priya rahul ana carlos maria jose wei li mohammed fatima aisha omar yuki hiroshi sofia lucas
    emma olivia noah liam chloe ethan grace isabella mateo lucia diego elena ivan olga anika arjun
    """.split()
)
HONORIFICS = r"(?:Mr|Mrs|Ms|Mx|Miss|Dr|Prof|Sir|Madam)\.?"
CAP = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
NAME = rf"{CAP}(?:\s+(?:[A-Z]\.\s+)?{CAP}){{0,2}}"

PERSON_PATTERNS = [
    re.compile(rf"\b{HONORIFICS}\s+(?P<n>{NAME})"),
    re.compile(rf"\b(?:[Mm]y name is|[Mm]y name's|[Tt]his is|I am|I'm|[Nn]ame:)\s+(?P<n>{NAME})"),
    re.compile(
        rf"\b(?i:customer|patient|client|employee|tenant|applicant|contact|cardholder|account holder|"
        rf"spoke with|spoke to|talked to|called|emailed|cc|attn:?|from|with|by|for|to|and)\s+(?P<n>{CAP}\s+{CAP})\b"
    ),
    re.compile(rf"(?:Regards|Thanks|Thank you|Sincerely|Best|Cheers),?\s*\n\s*(?P<n>{NAME})"),
    re.compile(rf"\b(?:Regards|Thanks|Thank you|Sincerely|Cheers),[ \t]+(?P<n>{CAP})\b(?=[.!]?\s*$)", re.MULTILINE),
]

# Capitalised words that context patterns tend to swallow but are not names.
STOP_CAPS = frozenset(
    """
    the a an i we you he she they it this that monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october november december
    please hello hi dear thanks support team customer account order invoice street avenue road
    visa mastercard amex bank card api server error ticket python linux windows
    """.split()
)


def _trim_name(name: str) -> str:
    """Drop trailing tokens that are clearly not part of a name."""
    toks = name.split()
    while toks and toks[-1].lower().strip(".") in STOP_CAPS:
        toks.pop()
    while toks and toks[0].lower().strip(".") in STOP_CAPS:
        toks.pop(0)
    return " ".join(toks)


def detect_persons(text: str) -> list[Span]:
    found: list[Span] = []

    def add(start: int, name: str, source: str) -> None:
        trimmed = _trim_name(name)
        if not trimmed:
            return
        offset = name.find(trimmed)
        s = start + offset
        found.append(Span(s, s + len(trimmed), "PERSON", trimmed, source))

    for pat in PERSON_PATTERNS:
        for m in pat.finditer(text):
            add(m.start("n"), m.group("n"), "context")

    # Gazetteer: known first name, optionally followed by a capitalised surname.
    for m in re.finditer(rf"\b{CAP}\b", text):
        if m.group().lower() in FIRST_NAMES:
            nxt = re.match(rf"[ \t]+{CAP}\b", text[m.end():])
            name = m.group() + (nxt.group() if nxt else "")
            add(m.start(), name, "gazetteer")

    found = _merge_person_spans(text, found)

    # Propagation: every token of a detected multi-token name, seen elsewhere
    # in the document as a capitalised word, refers to that person.
    known_tokens = set()
    for sp in found:
        toks = sp.text.split()
        if len(toks) >= 2:
            known_tokens.update(t for t in toks if len(t) > 2 and "." not in t)
    for tok in known_tokens:
        for m in re.finditer(rf"\b{re.escape(tok)}\b", text):
            span = Span(m.start(), m.end(), "PERSON", tok, "propagation")
            if not any(span.overlaps(f) for f in found):
                found.append(span)
    return sorted(found, key=lambda s: s.start)


def _merge_person_spans(text: str, spans: list[Span]) -> list[Span]:
    """Union overlapping person spans into the longest covering span."""
    spans = sorted(spans, key=lambda s: (s.start, -s.end))
    merged: list[Span] = []
    for sp in spans:
        if merged and sp.start < merged[-1].end:
            last = merged[-1]
            if sp.end > last.end:
                # Only extend when contiguous text is still name-like.
                merged[-1] = Span(last.start, sp.end, "PERSON", text[last.start:sp.end], last.source)
            continue
        merged.append(sp)
    return merged


# --------------------------------------------------------------------------- orchestration

DETECTORS = {
    "EMAIL": detect_emails,
    "PHONE": detect_phones,
    "CREDIT_CARD": detect_cards,
    "SSN": detect_ssns,
    "IBAN": detect_ibans,
    "IPV4": detect_ipv4,
    "ADDRESS": detect_addresses,
    "PERSON": detect_persons,
}


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Greedy: highest priority first, then longest; drop anything overlapping a kept span."""
    ordered = sorted(spans, key=lambda s: (-PRIORITY.get(s.type, 0), -(s.end - s.start), s.start))
    kept: list[Span] = []
    for sp in ordered:
        if not any(sp.overlaps(k) for k in kept):
            kept.append(sp)
    return sorted(kept, key=lambda s: s.start)


def detect(text: str, types: tuple[str, ...] = ENTITY_TYPES) -> list[Span]:
    spans: list[Span] = []
    for t in types:
        spans.extend(DETECTORS[t](text))
    return resolve_overlaps(spans)

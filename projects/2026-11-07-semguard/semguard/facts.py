"""Facts that must agree before a cached answer may be reused.

Embeddings are good at "same topic" and bad at "same *specific* question": ``refund order 4521`` and
``refund order 4512`` are ~identical vectors. These cheap, explainable checks veto a candidate match:

* literals  - numbers, identifiers, e-mails, URLs and quoted strings must be identical sets
* negation  - one side negated, the other not
* polarity  - opposite sides of an antonym group (enable/disable, import/export, ...)
* terms     - different values of a domain term group (python/java, pro/basic plan, ...)
* volatile  - time-sensitive wording ("today", "right now", "latest"); handled by the cache as a bypass
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Iterable, Optional

from .text import normalize

GUARDS = ("literals", "negation", "polarity", "terms", "volatile")

_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "hundred": "100"})
_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_QUOTED_RE = re.compile(r"\"([^\"]{1,200})\"|`([^`]{1,200})`")
_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
_UNITS = frozenset("""second seconds sec min minute minutes hour hours hr hrs day days week weeks month months year years
    gb mb kb tb kg km mi lb ms px""".split())


def _phrase_regex(phrases: Iterable[str]) -> re.Pattern:
    alts = []
    for p in sorted(set(phrases), key=len, reverse=True):
        forms = {p, p + "s", p + "d", p + "ed", p + "ing"}
        if p.endswith("e"):
            forms.add(p[:-1] + "ing")
        alts.extend(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")


class Lexicon:
    """Word lists for the negation/polarity/terms/volatile guards (see default_lexicon.json)."""

    def __init__(self, data: dict):
        self.data = data
        self._memo: dict[str, "Facts"] = {}
        self.negation = frozenset(data.get("negation", []))
        self.volatile_re = _phrase_regex(data.get("volatile", [])) if data.get("volatile") else None
        self.polarity: list[tuple[str, str, re.Pattern]] = []
        for group, sides in data.get("polarity", {}).items():
            for side, phrases in sides.items():
                self.polarity.append((group, side, _phrase_regex(phrases)))
        self.terms: list[tuple[str, str, re.Pattern]] = []
        for group, values in data.get("terms", {}).items():
            for canon, aliases in values.items():
                # term aliases are matched exactly (no inflection): "go" must not match "going"
                pat = re.compile(r"(?<![a-z0-9])(?:" + "|".join(
                    re.escape(normalize(a)) for a in sorted(aliases, key=len, reverse=True)) + r")(?![a-z0-9])")
                self.terms.append((group, canon, pat))

    @classmethod
    def default(cls) -> "Lexicon":
        raw = resources.files("semguard").joinpath("default_lexicon.json").read_text(encoding="utf-8")
        return cls(json.loads(raw))

    @classmethod
    def from_file(cls, path: str) -> "Lexicon":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))


@dataclass(frozen=True)
class Facts:
    literals: frozenset = field(default_factory=frozenset)   # {(kind, value)}
    negated: bool = False
    polarity: frozenset = field(default_factory=frozenset)   # {(group, side)}
    terms: frozenset = field(default_factory=frozenset)      # {(group, canonical)}
    volatile: bool = False

    def to_dict(self) -> dict:
        return {"literals": sorted(map(list, self.literals)), "negated": self.negated,
                "polarity": sorted(map(list, self.polarity)), "terms": sorted(map(list, self.terms)),
                "volatile": self.volatile}


def _canon_number(tok: str) -> str:
    if "." in tok:
        tok = tok.rstrip("0").rstrip(".")
    return tok.lstrip("0") or "0"


def extract_facts(text: str, lexicon: Lexicon) -> Facts:
    hit = lexicon._memo.get(text)
    if hit is None:
        hit = _extract(text, lexicon)
        if len(lexicon._memo) < 200_000:
            lexicon._memo[text] = hit
    return hit


def _extract(text: str, lexicon: Lexicon) -> Facts:
    raw = text.lower()
    literals: set[tuple[str, str]] = set()
    for m in _EMAIL_RE.findall(raw):
        literals.add(("email", m))
    for m in _URL_RE.findall(raw):
        literals.add(("url", m.rstrip(".,;:!?)")))
    for a, b in _QUOTED_RE.findall(text):
        literals.add(("quoted", (a or b).strip().lower()))
    norm = normalize(text)
    # scan for numbers/ids with e-mails and URLs removed so their pieces aren't double-counted
    toks = normalize(_URL_RE.sub(" ", _EMAIL_RE.sub(" ", raw))).split()
    negated = False
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok in lexicon.negation:
            # "order no 4521" / "no. 7" is an abbreviation of "number", not a negation
            if not (tok == "no" and (_NUM_RE.match(nxt) or nxt in _NUMBER_WORDS)):
                negated = True
        if _NUM_RE.match(tok):
            literals.add(("num", _canon_number(tok)))
        elif tok in _NUMBER_WORDS:
            literals.add(("num", _NUMBER_WORDS[tok]))
        else:
            digits = sum(c.isdigit() for c in tok)
            # >=2 digits plus a letter is an identifier (ord-4521, a12b); "2fa", "s3", "mp3" are words
            if digits >= 2 and any(c.isalpha() for c in tok):
                literals.add(("id", tok))
            elif "-" in tok:
                # "6-hour", "10-day": a quantity glued to a unit is still a number
                parts = tok.split("-")
                for a, b in zip(parts, parts[1:]):
                    if _NUM_RE.match(a) and b in _UNITS:
                        literals.add(("num", _canon_number(a)))
    polarity = {(g, side) for g, side, pat in lexicon.polarity if pat.search(norm)}
    terms = {(g, canon) for g, canon, pat in lexicon.terms if pat.search(norm)}
    volatile = bool(lexicon.volatile_re and lexicon.volatile_re.search(norm))
    return Facts(frozenset(literals), negated, frozenset(polarity), frozenset(terms), volatile)


def _grouped(pairs: frozenset) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for g, v in pairs:
        out.setdefault(g, set()).add(v)
    return out


def conflict(a: Facts, b: Facts, guards: Iterable[str] = GUARDS) -> Optional[str]:
    """Return a human-readable reason if the two queries must not share an answer, else None."""
    guards = set(guards)
    if "literals" in guards and a.literals != b.literals:
        only_a = sorted(f"{k}:{v}" for k, v in a.literals - b.literals)
        only_b = sorted(f"{k}:{v}" for k, v in b.literals - a.literals)
        return f"literals differ ({', '.join(only_a) or '-'} vs {', '.join(only_b) or '-'})"
    if "negation" in guards and a.negated != b.negated:
        return "negation differs"
    if "polarity" in guards:
        ga, gb = _grouped(a.polarity), _grouped(b.polarity)
        for g in sorted(set(ga) & set(gb)):
            if ga[g] != gb[g]:
                return f"polarity '{g}' differs ({'/'.join(sorted(ga[g]))} vs {'/'.join(sorted(gb[g]))})"
    if "terms" in guards:
        ta, tb = _grouped(a.terms), _grouped(b.terms)
        for g in sorted(set(ta) & set(tb)):
            if ta[g] != tb[g]:
                return f"{g} differs ({'/'.join(sorted(ta[g]))} vs {'/'.join(sorted(tb[g]))})"
    return None

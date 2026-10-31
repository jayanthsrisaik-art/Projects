"""Reversible, session-scoped pseudonymization.

``Session.redact`` replaces detected PII with typed placeholders such as
``<PERSON_1>``; the mapping lives only in the session (it never leaves the
process).  The same real value always gets the same placeholder within a
session, and partial person mentions ("Ruiz", "Ms. Ruiz") are linked to the
full name ("Ana Ruiz") so the model still sees one person.

``Session.rehydrate`` maps placeholders in model output back to real values.
It tolerates the ways models mangle placeholders (``[PERSON_1]``,
``PERSON_1``, ``<person_1>``, ``&lt;PERSON_1&gt;``) and reports placeholders
the model invented that do not exist in the vault.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .detectors import ENTITY_TYPES, Span, detect, resolve_overlaps

PLACEHOLDER_TYPES = ENTITY_TYPES + ("LITERAL",)
_TYPES_ALT = "|".join(sorted(PLACEHOLDER_TYPES, key=len, reverse=True))

# Tolerant placeholder matcher used both for rehydration and for neutralising
# placeholder-looking text the *user* typed (so it cannot be used to pull
# another value out of the vault).
PLACEHOLDER_RE = re.compile(
    rf"(?:(?:&lt;|<|\[\[?|\{{\{{?)[ \t]*)?\b(?P<type>{_TYPES_ALT})_(?P<num>\d+)\b(?:[ \t]*(?:&gt;|>|\]\]?|\}}\}}?))?",
    re.IGNORECASE,
)


def placeholder(type_: str, n: int) -> str:
    return f"<{type_}_{n}>"


def normalize(type_: str, value: str) -> str:
    v = value.strip()
    if type_ == "EMAIL":
        return v.lower()
    if type_ in ("PHONE", "CREDIT_CARD", "SSN"):
        digits = re.sub(r"\D", "", v)
        if type_ == "PHONE" and len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits
    if type_ == "IBAN":
        return v.replace(" ", "").upper()
    if type_ in ("PERSON", "ADDRESS"):
        return " ".join(v.lower().replace(".", "").split())
    return v


@dataclass
class Entity:
    type: str
    placeholder: str
    display: str
    surfaces: set[str] = field(default_factory=set)
    mentions: int = 0


@dataclass
class Replacement:
    start: int
    end: int
    type: str
    original: str
    placeholder: str


@dataclass
class RedactResult:
    text: str
    replacements: list[Replacement]


@dataclass
class RehydrateResult:
    text: str
    restored: int
    unknown_placeholders: list[str]


class Session:
    def __init__(self, types: tuple[str, ...] = ENTITY_TYPES):
        self.types = types
        self.entities: dict[str, Entity] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------ redact
    def redact(self, text: str) -> RedactResult:
        literal_spans = [
            Span(m.start(), m.end(), "LITERAL", m.group(), "user-literal") for m in PLACEHOLDER_RE.finditer(text)
        ]
        spans = resolve_overlaps(detect(text, self.types) + literal_spans + self._known_spans(text))
        out, reps, cursor = [], [], 0
        for sp in spans:
            ph = self._placeholder_for(sp.type, sp.text)
            out.append(text[cursor:sp.start])
            out.append(ph)
            reps.append(Replacement(sp.start, sp.end, sp.type, sp.text, ph))
            cursor = sp.end
        out.append(text[cursor:])
        return RedactResult("".join(out), reps)

    def _known_spans(self, text: str) -> list[Span]:
        """Values already in the vault are redacted on later turns even when the
        detectors would not fire (e.g. a name mentioned without any context cue)."""
        spans = []
        for ent in self.entities.values():
            if ent.type == "LITERAL":
                continue
            for surface in ent.surfaces:
                for m in re.finditer(rf"(?<!\w){re.escape(surface)}(?!\w)", text):
                    spans.append(Span(m.start(), m.end(), ent.type, m.group(), "vault"))
        return spans

    def _new(self, type_: str, display: str) -> Entity:
        n = self._counters.get(type_, 0) + 1
        self._counters[type_] = n
        ent = Entity(type_, placeholder(type_, n), display)
        self.entities[ent.placeholder] = ent
        return ent

    def _placeholder_for(self, type_: str, value: str) -> str:
        key = (type_, value if type_ == "LITERAL" else normalize(type_, value))
        ph = self._by_key.get(key)
        ent = self.entities[ph] if ph else None
        if ent is None and type_ == "PERSON":
            ent = self._link_person(key[1])
        if ent is None:
            ent = self._new(type_, value)
        self._by_key[key] = ent.placeholder
        ent.surfaces.add(value)
        ent.mentions += 1
        if len(value) > len(ent.display):  # display the most complete form
            ent.display = value
        return ent.placeholder

    def _link_person(self, norm: str) -> Entity | None:
        """Link a person mention to exactly one known person sharing its tokens."""
        toks = set(norm.split())
        candidates = []
        for ent in self.entities.values():
            if ent.type != "PERSON":
                continue
            ent_toks = set(normalize("PERSON", ent.display).split())
            if toks <= ent_toks or ent_toks <= toks:
                candidates.append(ent)
        return candidates[0] if len(candidates) == 1 else None

    # --------------------------------------------------------------- rehydrate
    def rehydrate(self, text: str, left_context: str = "") -> RehydrateResult:
        """Replace placeholders in ``text``.

        ``left_context`` is raw text that precedes ``text`` (already emitted by
        a stream); it is used only so word boundaries are judged correctly.
        """
        restored = 0
        unknown: list[str] = []
        full = left_context + text
        out, cursor = [], len(left_context)

        def sub(m: re.Match) -> str:
            nonlocal restored
            ph = placeholder(m.group("type").upper(), int(m.group("num")))
            ent = self.entities.get(ph)
            if ent is None:
                unknown.append(m.group())
                return m.group()
            restored += 1
            return ent.display

        for m in PLACEHOLDER_RE.finditer(full, len(left_context)):
            out.append(full[cursor:m.start()])
            out.append(sub(m))
            cursor = m.end()
        out.append(full[cursor:])
        return RehydrateResult("".join(out), restored, unknown)

    def summary(self) -> dict[str, int]:
        """Entity counts per type -- safe to expose (no values)."""
        counts: dict[str, int] = {}
        for ent in self.entities.values():
            counts[ent.type] = counts.get(ent.type, 0) + 1
        return counts


# ---------------------------------------------------------------- streaming

_MAX_PLACEHOLDER = 4 + max(len(t) for t in PLACEHOLDER_TYPES) + 1 + 6 + 4  # brackets, spaces, digits
# Could the *end* of the buffer still grow into a placeholder?
_PARTIAL_RE = re.compile(
    r"(?:&(?:l(?:t;?)?)?|<|\[\[?|\{\{?)?[ \t]*([A-Za-z][A-Za-z0-9_]*)?[ \t]*(?:&(?:g(?:t;?)?)?|\]|\})?$"
)


def _could_be_partial(tail: str) -> bool:
    if not tail:
        return False
    m = _PARTIAL_RE.fullmatch(tail)
    if not m:
        return False
    word = m.group(1) or ""
    if not word:
        return True  # just an opening bracket / entity prefix
    w = word.upper()
    for t in PLACEHOLDER_TYPES:
        if t.startswith(w) or w == t + "_":
            return True  # still spelling the type name
        if w.startswith(t + "_") and w[len(t) + 1:].isdigit():
            return True  # complete id; more digits or a closing bracket may follow
    return False


class StreamRehydrator:
    """Rehydrate a token stream without ever emitting half a placeholder.

    Output of ``feed`` for any chunking, followed by ``flush``, equals
    ``session.rehydrate(full_text).text``.
    """

    def __init__(self, session: Session):
        self.session = session
        self.buffer = ""
        self._context = ""  # last raw char already emitted (for \b checks)
        self.unknown: list[str] = []
        self.restored = 0

    def _emit(self, text: str) -> str:
        res = self.session.rehydrate(text, left_context=self._context)
        self._context = (self._context + text)[-1:]
        self.unknown.extend(res.unknown_placeholders)
        self.restored += res.restored
        return res.text

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        # Hold back the longest suffix that might still become a placeholder,
        # starting at a token boundary.
        hold = 0
        limit = min(len(self.buffer), _MAX_PLACEHOLDER)
        for k in range(limit, 0, -1):
            start = len(self.buffer) - k
            prev, first = self.buffer[start - 1:start], self.buffer[start]
            if (first.isalnum() or first == "_") and (prev.isalnum() or prev == "_"):
                continue  # a bare placeholder must start at a word boundary
            if _could_be_partial(self.buffer[start:]):
                hold = k
                break
        cut = len(self.buffer) - hold
        # Never cut inside a complete match that extends into the held tail.
        for m in PLACEHOLDER_RE.finditer(self.buffer):
            if m.start() < cut < m.end():
                cut = m.start()
        ready, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return self._emit(ready) if ready else ""

    def flush(self) -> str:
        ready, self.buffer = self.buffer, ""
        return self._emit(ready) if ready else ""

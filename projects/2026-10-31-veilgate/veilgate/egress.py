"""Second-line egress check on the text that is about to leave the process.

The primary detectors are tuned for precision.  This check is deliberately
paranoid and independent of them: after redaction, anything that still looks
like an email, a long digit run (unless explicitly labelled as an order /
ticket / invoice id), or a gazetteer first name is flagged.  In
``block`` mode the gateway refuses to send the request (fail closed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .detectors import FIRST_NAMES, NOT_PHONE_CUES, _preceded_by
from .vault import PLACEHOLDER_RE


@dataclass(frozen=True)
class Finding:
    kind: str
    text: str
    start: int


class LeakBlocked(Exception):
    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__(f"egress blocked: {len(findings)} suspected PII fragment(s)")


_CHECKS = [
    ("email-like", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("long-digit-run", re.compile(r"(?<!\w)\d(?:[ .()-]?\d){8,}(?!\w)")),
]


def scan(text: str) -> list[Finding]:
    masked = PLACEHOLDER_RE.sub(lambda m: " " * len(m.group()), text)
    out = []
    for kind, rx in _CHECKS:
        for m in rx.finditer(masked):
            # A digit run explicitly labelled as a business identifier
            # ("order #4155550134", "ticket 123456789") is allowed through.
            if kind == "long-digit-run" and _preceded_by(masked, m.start(), NOT_PHONE_CUES):
                continue
            out.append(Finding(kind, m.group(), m.start()))
    for m in re.finditer(r"\b[A-Z][a-z]+\b", masked):
        if m.group().lower() in FIRST_NAMES:
            out.append(Finding("first-name", m.group(), m.start()))
    return sorted(out, key=lambda f: f.start)

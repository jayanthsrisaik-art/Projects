"""redact -> egress check -> LLM -> rehydrate, plus an audit record."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from .egress import Finding, LeakBlocked, scan
from .llm import LLM, SYSTEM_PROMPT
from .vault import Session, StreamRehydrator


@dataclass
class AuditRecord:
    """What left the process. Contains only redacted text, never vault values."""

    backend: str
    outbound: str
    entities_redacted: dict[str, int]
    egress_findings: list[Finding] = field(default_factory=list)
    unknown_placeholders: list[str] = field(default_factory=list)


@dataclass
class GatewayResult:
    reply: str
    audit: AuditRecord


class Gateway:
    def __init__(self, llm: LLM, on_leak: str = "block"):
        if on_leak not in ("block", "warn"):
            raise ValueError("on_leak must be 'block' or 'warn'")
        self.llm = llm
        self.on_leak = on_leak

    def _prepare(self, session: Session, prompt: str) -> AuditRecord:
        red = session.redact(prompt)
        counts: dict[str, int] = {}
        for r in red.replacements:
            counts[r.type] = counts.get(r.type, 0) + 1
        findings = scan(red.text)
        audit = AuditRecord(self.llm.name, red.text, counts, findings)
        if findings and self.on_leak == "block":
            raise LeakBlocked(findings)
        return audit

    def chat(self, session: Session, prompt: str) -> GatewayResult:
        audit = self._prepare(session, prompt)
        raw = self.llm.complete(SYSTEM_PROMPT, audit.outbound)
        res = session.rehydrate(raw)
        audit.unknown_placeholders = res.unknown_placeholders
        return GatewayResult(res.text, audit)

    def chat_stream(self, session: Session, prompt: str) -> tuple[AuditRecord, Iterator[str]]:
        audit = self._prepare(session, prompt)  # raises before anything is sent
        rehydrator = StreamRehydrator(session)

        def gen() -> Iterator[str]:
            for chunk in self.llm.stream(SYSTEM_PROMPT, audit.outbound):
                out = rehydrator.feed(chunk)
                if out:
                    yield out
            tail = rehydrator.flush()
            if tail:
                yield tail
            audit.unknown_placeholders = rehydrator.unknown

        return audit, gen()

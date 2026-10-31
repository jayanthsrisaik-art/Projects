"""veilgate: reversible PII pseudonymization gateway for LLM calls."""
from .detectors import ENTITY_TYPES, Span, detect
from .egress import LeakBlocked, scan
from .gateway import Gateway, GatewayResult
from .llm import MockLLM, make_llm
from .vault import Session, StreamRehydrator

__all__ = [
    "ENTITY_TYPES", "Span", "detect", "LeakBlocked", "scan", "Gateway", "GatewayResult",
    "MockLLM", "make_llm", "Session", "StreamRehydrator",
]

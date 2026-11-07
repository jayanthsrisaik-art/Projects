"""LLM backends. ``MockLLM`` is deterministic and offline; ``AnthropicLLM`` is optional."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from .text import normalize


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int


class LLM(Protocol):
    name: str

    def complete(self, prompt: str, system: str = "") -> Completion: ...


def _approx_tokens(text: str) -> int:
    return max(1, round(len(text.split()) * 1.3))


class MockLLM:
    """Deterministic stand-in: the answer is a function of the (normalised) prompt, so tests are stable."""

    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, system: str = "") -> Completion:
        self.calls += 1
        digest = hashlib.sha256((system + "\x00" + normalize(prompt)).encode()).hexdigest()[:8]
        text = f"[mock answer {digest}] Response to: {prompt.strip()}"
        return Completion(text, _approx_tokens(system + " " + prompt), _approx_tokens(text) + 40)


class AnthropicLLM:
    """Real backend via the Anthropic Messages API (requires `pip install anthropic` and ANTHROPIC_API_KEY)."""

    name = "anthropic"

    def __init__(self, model: Optional[str] = None, max_tokens: int = 512):
        import anthropic  # imported lazily so mock mode has no dependency on it

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("SEMGUARD_MODEL", "claude-haiku-5-5")
        self.max_tokens = max_tokens

    def complete(self, prompt: str, system: str = "") -> Completion:
        kwargs = {"system": system} if system else {}
        resp = self.client.messages.create(model=self.model, max_tokens=self.max_tokens,
                                           messages=[{"role": "user", "content": prompt}], **kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return Completion(text, resp.usage.input_tokens, resp.usage.output_tokens)


def get_backend(name: Optional[str] = None) -> LLM:
    name = (name or os.environ.get("SEMGUARD_BACKEND", "mock")).lower()
    if name == "mock":
        return MockLLM()
    if name == "anthropic":
        return AnthropicLLM()
    raise ValueError(f"unknown backend {name!r} (expected 'mock' or 'anthropic')")

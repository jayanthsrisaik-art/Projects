"""LLM backends.

``MockLLM`` is the default and needs no network: it answers deterministically
and *uses the placeholders it was given*, in deliberately mangled forms, so
the rehydration path is exercised exactly as with a real model.

``AnthropicLLM`` is optional (``pip install anthropic`` and set
``ANTHROPIC_API_KEY``).  It only ever receives redacted text.
"""
from __future__ import annotations

import os
import re
from typing import Iterator, Protocol

from .vault import PLACEHOLDER_RE

SYSTEM_PROMPT = (
    "Some values in the user's text were replaced with placeholders such as <PERSON_1> or <EMAIL_2>. "
    "Treat each placeholder as an opaque name for a real value. When you refer to one of these values, "
    "copy its placeholder exactly, including the angle brackets. Never guess or invent the real value."
)


class LLM(Protocol):
    name: str

    def complete(self, system: str, prompt: str) -> str: ...

    def stream(self, system: str, prompt: str) -> Iterator[str]: ...


class MockLLM:
    """Deterministic stand-in for a chat model.

    mode="echo"  -> returns the prompt unchanged (round-trip testing).
    mode="reply" -> a short templated reply that mentions every placeholder,
                    cycling through mangled spellings, plus one hallucinated
                    placeholder id when ``hallucinate`` is set.
    """

    def __init__(self, mode: str = "reply", chunk_size: int = 5, hallucinate: bool = False):
        self.mode = mode
        self.chunk_size = chunk_size
        self.hallucinate = hallucinate
        self.name = f"mock-{mode}"
        self.last_prompt: str | None = None

    def complete(self, system: str, prompt: str) -> str:
        self.last_prompt = prompt
        if self.mode == "echo":
            return prompt
        seen: list[str] = []
        for m in PLACEHOLDER_RE.finditer(prompt):
            ph = f"{m.group('type').upper()}_{m.group('num')}"
            if ph not in seen:
                seen.append(ph)
        styles = ["<{}>", "[{}]", "{}", "<{}>"]
        mentions = [styles[i % len(styles)].format(ph) for i, ph in enumerate(seen)]
        words = len(re.findall(r"\w+", prompt))
        reply = f"Summary of a {words}-word message."
        if mentions:
            reply += " It refers to " + ", ".join(mentions) + "."
        if self.hallucinate:
            reply += " See also <PERSON_99>."
        return reply

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        text = self.complete(system, prompt)
        for i in range(0, len(text), self.chunk_size):
            yield text[i:i + self.chunk_size]


class AnthropicLLM:  # pragma: no cover - requires network + API key
    def __init__(self, model: str | None = None, max_tokens: int = 4096):
        import anthropic  # optional dependency

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("VEILGATE_MODEL", "claude-opus-5-5")
        self.max_tokens = max_tokens
        self.name = f"anthropic:{self.model}"

    def _stream_ctx(self, system: str, prompt: str):
        return self.client.beta.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        with self._stream_ctx(system, prompt) as stream:
            yield from stream.text_stream
            final = stream.get_final_message()
        if final.stop_reason == "refusal":
            yield "\n[model declined this request]"

    def complete(self, system: str, prompt: str) -> str:
        return "".join(self.stream(system, prompt))


def make_llm(backend: str = "mock") -> LLM:
    if backend == "anthropic":
        return AnthropicLLM()
    if backend.startswith("mock"):
        _, _, mode = backend.partition(":")
        return MockLLM(mode=mode or "reply")
    raise ValueError(f"unknown backend {backend!r}")

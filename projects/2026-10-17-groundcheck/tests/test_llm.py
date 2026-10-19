import json

import pytest

from groundcheck.lexical import CONTRADICTED, SUPPORTED, UNSUPPORTED
from groundcheck.llm import AnthropicVerifier, parse_label

SRC = {"doc": "The pack is warranted for eight years. It does not support bidirectional charging."}


def fake(text, seen=None):
    def transport(payload, key):
        if seen is not None:
            seen.append((payload, key))
        return {"content": [{"type": "text", "text": text}]}
    return transport


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicVerifier()


def test_verify_parses_label_and_sends_evidence():
    seen = []
    v = AnthropicVerifier(api_key="k", transport=fake('{"label": "contradicted", "reason": "x"}', seen))
    j = v.verify("The pack supports bidirectional charging.", SRC)
    assert j.label == CONTRADICTED
    payload, key = seen[0]
    assert key == "k" and payload["temperature"] == 0
    assert "bidirectional" in payload["messages"][0]["content"]


def test_parse_label_tolerates_wrapping_text():
    assert parse_label('Sure! ```json\n{"label": "Supported", "reason": "ok"}\n```')[0] == SUPPORTED


@pytest.mark.parametrize("bad", ["", "no json", '{"label": "maybe"}', '{"x": 1}', "{broken"])
def test_parse_label_falls_back_to_unsupported(bad):
    assert parse_label(bad)[0] == UNSUPPORTED

import pytest

from veilgate.corpus import generate
from veilgate.egress import LeakBlocked, scan
from veilgate.evaluate import baseline_detect, evaluate_detector, run
from veilgate.detectors import detect
from veilgate.gateway import Gateway
from veilgate.llm import MockLLM
from veilgate.vault import Session

PROMPT = "Customer Ana Ruiz (ana.ruiz@example.com, 415-555-0134) disputes card 4111 1111 1111 1111."


def test_gateway_never_sends_pii_and_rehydrates_reply():
    llm = MockLLM()
    res = Gateway(llm).chat(Session(), PROMPT)
    for value in ("Ana Ruiz", "ana.ruiz@example.com", "415-555-0134", "4111 1111 1111 1111"):
        assert value not in llm.last_prompt
        assert value in res.reply                       # mock mentions every placeholder
    assert res.audit.outbound == llm.last_prompt
    assert res.audit.entities_redacted == {"PERSON": 1, "EMAIL": 1, "PHONE": 1, "CREDIT_CARD": 1}


def test_gateway_streaming_equals_batch():
    gw = Gateway(MockLLM(chunk_size=3, hallucinate=True))
    batch = gw.chat(Session(), PROMPT)
    audit, chunks = gw.chat_stream(Session(), PROMPT)
    assert "".join(chunks) == batch.reply
    assert audit.unknown_placeholders == ["<PERSON_99>"] == batch.audit.unknown_placeholders


def test_egress_blocks_before_llm_is_called():
    llm = MockLLM()
    # An unlabelled 12-digit run is not a recognised identifier -> guard fires.
    with pytest.raises(LeakBlocked):
        Gateway(llm, on_leak="block").chat(Session(), "my member code is 123456789012")
    assert llm.last_prompt is None
    res = Gateway(llm, on_leak="warn").chat(Session(), "my member code is 123456789012")
    assert res.audit.egress_findings and llm.last_prompt is not None


def test_egress_scan_ignores_placeholders_and_labelled_ids():
    assert scan("Hi <PERSON_1>, order #4155550134 for <EMAIL_1>") == []
    kinds = {f.kind for f in scan("Thomas wrote from x@y.io about 4155550134")}
    assert kinds == {"first-name", "email-like", "long-digit-run"}


def test_corpus_is_deterministic_and_gold_offsets_are_exact():
    a, b = generate("test", 30, 7), generate("test", 30, 7)
    assert [d.text for d in a] == [d.text for d in b]
    for d in a:
        for g in d.entities:
            assert d.text[g.start:g.end] == g.text


def test_test_split_uses_unseen_names_and_templates():
    from veilgate.corpus import DEV_TEMPLATES, TEST_TEMPLATES, TEST_HELDOUT_FIRST, DEV_HELDOUT_FIRST
    from veilgate.detectors import FIRST_NAMES
    assert not set(DEV_TEMPLATES) & set(TEST_TEMPLATES)
    assert not {n.lower() for n in TEST_HELDOUT_FIRST} & FIRST_NAMES
    assert not set(TEST_HELDOUT_FIRST) & set(DEV_HELDOUT_FIRST)


def test_veilgate_beats_regex_baseline_on_leakage():
    docs = generate("test", 120, 11)
    ours, base = evaluate_detector(docs, detect), evaluate_detector(docs, baseline_detect)
    assert ours["leak_rate"] < base["leak_rate"]
    assert ours["f1"] > base["f1"]
    assert ours["false_positive_redactions"] < base["false_positive_redactions"]


def test_roundtrip_restores_every_value():
    res = run(generate("dev", 60, 3))
    assert res["roundtrip_echo"]["all_values_restored_rate"] == 1.0

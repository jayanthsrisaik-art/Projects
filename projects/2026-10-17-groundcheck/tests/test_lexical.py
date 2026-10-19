import pytest

from groundcheck.lexical import (
    CONTRADICTED, SUPPORTED, UNSUPPORTED, LexicalConfig, LexicalVerifier, OverlapBaseline, retrieve,
)

SRC = {"doc": "Revenue grew 14 percent in 2024. The company opened offices in Lisbon and Toronto. "
               "No dividends were paid."}


def verify(claim, **cfg):
    return LexicalVerifier(LexicalConfig(**cfg) if cfg else None).verify(claim, SRC)


def test_supported_paraphrase():
    j = verify("The company opened offices in Toronto and Lisbon.")
    assert j.label == SUPPORTED and "Lisbon" in j.evidence.sentence


@pytest.mark.parametrize("claim,reason", [
    ("Revenue grew 4 percent in 2024.", "number"),
    ("The company opened offices in Lisbon and Madrid.", "madrid"),
    ("The company paid a dividend.", "negation"),
])
def test_contradictions(claim, reason):
    j = verify(claim)
    assert j.label == CONTRADICTED and reason in j.reason.lower()


def test_unrelated_claim_is_unsupported():
    assert verify("The CEO resigned last week.").label == UNSUPPORTED


def test_evidence_names_the_source_document():
    j = LexicalVerifier().verify("Revenue grew 14 percent.", {"a": "Nothing here.", "b": SRC["doc"]})
    assert j.evidence.source == "b"


def test_retrieve_ties_prefer_earlier_sentence():
    hits = retrieve("alpha beta", {"d": "Alpha beta one. Alpha beta two."})
    assert hits[0].sentence == "Alpha beta one."


def test_stricter_threshold_flips_borderline_to_unsupported():
    claim = "The company opened offices in Lisbon."
    assert verify(claim).label == SUPPORTED
    assert verify(claim, support_threshold=1.1, conflict_threshold=1.1).label == UNSUPPORTED


def test_empty_sources_unsupported():
    assert LexicalVerifier().verify("Anything at all.", {}).label == UNSUPPORTED


def test_baseline_never_contradicts():
    assert OverlapBaseline().verify("Revenue grew 4 percent in 2024.", SRC).label == SUPPORTED

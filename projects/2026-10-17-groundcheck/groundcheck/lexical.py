"""Offline verifier: best-matching-sentence retrieval plus conflict rules.

For a claim it finds the source sentence covering most of the claim's content tokens.
If a related sentence is found, rules look for a conflict (different number, unmatched
proper noun, negation mismatch, antonym); otherwise high coverage means "supported".
"""
from dataclasses import dataclass

from .text import (
    antonym_clash, content_tokens, negation_count, numbers, proper_nouns, split_sentences,
)

SUPPORTED, UNSUPPORTED, CONTRADICTED = "supported", "unsupported", "contradicted"
LABELS = (SUPPORTED, UNSUPPORTED, CONTRADICTED)


@dataclass(frozen=True)
class Evidence:
    source: str
    sentence: str
    coverage: float


@dataclass(frozen=True)
class LexicalConfig:
    # Chosen by grid search on the dev split only (see groundcheck.evaluate).
    support_threshold: float = 0.55
    conflict_threshold: float = 0.45


@dataclass(frozen=True)
class Judgement:
    label: str
    evidence: Evidence | None
    reason: str


def coverage(claim_tokens: set[str], sentence: str) -> float:
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & content_tokens(sentence)) / len(claim_tokens)


def retrieve(claim: str, sources: dict[str, str], k: int = 1) -> list[Evidence]:
    """Top-k source sentences by claim-token coverage (ties: earlier source/sentence wins)."""
    ct = content_tokens(claim)
    scored = []
    for name, text in sources.items():
        for sent in split_sentences(text):
            scored.append(Evidence(name, sent, coverage(ct, sent)))
    scored.sort(key=lambda e: -e.coverage)  # stable sort keeps source order on ties
    return scored[:k]


def find_conflict(claim: str, sentence: str) -> str | None:
    cn, sn = numbers(claim), numbers(sentence)
    if sn and cn - sn:
        return f"number {sorted(cn - sn)[0]} not in evidence"
    unmatched = proper_nouns(claim) - content_tokens(sentence) - proper_nouns(sentence)
    if unmatched:
        return f"'{sorted(unmatched)[0]}' not in evidence"
    if (negation_count(claim) > 0) != (negation_count(sentence) > 0):
        return "negation mismatch"
    if antonym_clash(claim, sentence):
        return "opposite wording"
    return None


class LexicalVerifier:
    name = "lexical"

    def __init__(self, config: LexicalConfig | None = None):
        self.config = config or LexicalConfig()

    def verify(self, claim: str, sources: dict[str, str]) -> Judgement:
        hits = retrieve(claim, sources)
        if not hits:
            return Judgement(UNSUPPORTED, None, "no source text")
        best = hits[0]
        if best.coverage >= self.config.conflict_threshold:
            conflict = find_conflict(claim, best.sentence)
            if conflict:
                return Judgement(CONTRADICTED, best, conflict)
        if best.coverage >= self.config.support_threshold:
            return Judgement(SUPPORTED, best, f"coverage {best.coverage:.2f}")
        return Judgement(UNSUPPORTED, best, f"coverage {best.coverage:.2f} below threshold")


class OverlapBaseline:
    """Baseline: supported iff token coverage >= threshold; can never say 'contradicted'."""

    name = "overlap-baseline"

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold

    def verify(self, claim: str, sources: dict[str, str]) -> Judgement:
        hits = retrieve(claim, sources)
        best = hits[0] if hits else None
        ok = best is not None and best.coverage >= self.threshold
        return Judgement(SUPPORTED if ok else UNSUPPORTED, best, "overlap")

"""Online duplicate finder: BM25 candidate generation + reranking.

The same code path is used by the offline replay and the API, so measured
numbers describe what the service actually does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .features import explain, pair_features
from .index import BM25Index
from .model import Reranker
from .store import Report

CANDIDATES = 50


@dataclass
class Candidate:
    report: Report
    bm25: float
    rank: int
    features: list[float]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def candidates(index: BM25Index, q: Report, k: int = CANDIDATES) -> list[Candidate]:
    hits = index.search(q, k)
    if not hits:
        return []
    best, self_s = hits[0][1], index.self_score(q)
    return [Candidate(c, s, i + 1, pair_features(q, c, s, best, i + 1, self_s)) for i, (c, s) in enumerate(hits)]


def rerank(model: Reranker | None, cands: list[Candidate]) -> list[Candidate]:
    """Score and sort candidates. Without a model, fall back to BM25 order with bm25_rel as score."""
    if not cands:
        return []
    if model is None:
        for c in cands:
            c.score = c.features[0]
        return list(cands)
    probs = model.score(np.array([c.features for c in cands]))
    for c, p in zip(cands, probs):
        c.score = float(p)
    return sorted(cands, key=lambda c: (-c.score, c.rank))


class DuplicateFinder:
    def __init__(self, model: Reranker | None = None, k: int = CANDIDATES):
        self.index = BM25Index()
        self.model = model
        self.k = k
        self._ids: set[str] = set()

    def __contains__(self, report_id: str) -> bool:
        return report_id in self._ids

    def add(self, rep: Report) -> None:
        if rep.id in self._ids:
            return
        self.index.add(rep)
        self._ids.add(rep.id)

    def suggest(self, q: Report, top: int = 5, min_score: float = 0.01) -> tuple[list[Candidate], bool]:
        """Return (top suggestions, is_likely_duplicate). Only reports created before q are eligible;
        with a model loaded, candidates scoring below ``min_score`` are not shown."""
        eligible = [c for c in candidates(self.index, q, self.k) if c.report.created_at < q.created_at or
                    (c.report.created_at == q.created_at and c.report.id < q.id)]
        ranked = rerank(self.model, eligible)
        if self.model is not None:
            ranked = [c for c in ranked if c.score >= min_score]
        ranked = ranked[:top]
        for c in ranked:
            c.reasons = explain(q, c.report)
        threshold = self.model.threshold if self.model else 1.0
        likely = bool(ranked) and self.model is not None and ranked[0].score >= threshold
        return ranked, likely

"""Incremental BM25 index.

Documents are added in arrival order and every query sees only documents added
before it, so document frequencies and average length are computed from the
past alone.  That is what a live tracker can know, and it is what keeps the
offline replay free of look-ahead leakage.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Callable, Iterable

from .normalize import raw_tokens, tokens
from .store import Report


def report_tokens(rep: Report) -> list[str]:
    """Normalised retrieval document: title twice (it is the densest field), body, frames, exception."""
    toks = tokens(rep.title) * 2 + tokens(rep.body)
    toks += [f"frame:{f}" for f in rep.frames]
    if rep.exc_type:
        toks.append(f"exc:{rep.exc_type.lower()}")
    return toks


def report_raw_tokens(rep: Report) -> list[str]:
    return raw_tokens(rep.title + "\n" + rep.body)


class BM25Index:
    def __init__(self, tokenizer: Callable[[Report], list[str]] = report_tokens, k1: float = 1.2, b: float = 0.75):
        self.tokenizer = tokenizer
        self.k1, self.b = k1, b
        self.postings: dict[str, dict[int, int]] = defaultdict(dict)
        self.doc_len: list[int] = []
        self.docs: list[Report] = []
        self._total_len = 0

    def __len__(self) -> int:
        return len(self.docs)

    def add(self, rep: Report) -> None:
        doc_id = len(self.docs)
        toks = self.tokenizer(rep)
        for t, c in Counter(toks).items():
            self.postings[t][doc_id] = c
        self.docs.append(rep)
        self.doc_len.append(len(toks))
        self._total_len += len(toks)

    def add_all(self, reps: Iterable[Report]) -> None:
        for r in reps:
            self.add(r)

    def idf(self, term: str) -> float:
        n, df = len(self.docs), len(self.postings.get(term, ()))
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def self_score(self, rep: Report) -> float:
        """BM25 score the query would get against an identical document: a per-query scale
        that makes candidate scores comparable across queries and corpus sizes."""
        if not self.docs:
            return 0.0
        toks = self.tokenizer(rep)
        avgdl = self._total_len / len(self.docs)
        norm_len = 1 - self.b + self.b * len(toks) / avgdl
        return sum(self.idf(t) * tf * (self.k1 + 1) / (tf + self.k1 * norm_len) for t, tf in Counter(toks).items())

    def search(self, rep: Report, k: int = 50) -> list[tuple[Report, float]]:
        """Top-k (report, score) for a query report, best first; ties broken by recency."""
        if not self.docs:
            return []
        avgdl = self._total_len / len(self.docs)
        scores: dict[int, float] = defaultdict(float)
        for term in set(self.tokenizer(rep)):
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = self.idf(term)
            for d, tf in plist.items():
                norm = tf + self.k1 * (1 - self.b + self.b * self.doc_len[d] / avgdl)
                scores[d] += idf * tf * (self.k1 + 1) / norm
        top = sorted(scores.items(), key=lambda kv: (-kv[1], -kv[0]))[:k]
        return [(self.docs[d], s) for d, s in top]

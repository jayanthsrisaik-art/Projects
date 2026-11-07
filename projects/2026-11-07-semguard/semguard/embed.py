"""A dependency-free text embedder: signed feature hashing of words, word bigrams and char n-grams.

It is deliberately simple so the project runs offline. Any embedder with
``embed(text) -> unit-norm float32 vector`` can be plugged into :class:`SemanticCache`.
"""
from __future__ import annotations

import zlib

import numpy as np

from .text import STOPWORDS, normalize, stem, tokens


class HashingEmbedder:
    def __init__(self, dim: int = 4096, char_ngrams: tuple[int, ...] = (3, 4, 5),
                 word_weight: float = 1.0, bigram_weight: float = 0.6, char_weight: float = 0.35,
                 stop_weight: float = 0.15):
        self.dim = dim
        self.char_ngrams = char_ngrams
        self.word_weight = word_weight
        self.bigram_weight = bigram_weight
        self.char_weight = char_weight
        self.stop_weight = stop_weight

    def _add(self, vec: np.ndarray, feature: str, weight: float) -> None:
        h = zlib.crc32(feature.encode("utf-8"))
        sign = 1.0 if (h >> 31) & 1 else -1.0
        vec[h % self.dim] += sign * weight

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        words = [stem(w) for w in tokens(normalize(text))]
        content = [w for w in words if w not in STOPWORDS]
        for w in words:
            self._add(vec, "w:" + w, self.word_weight if w not in STOPWORDS else self.stop_weight)
        for a, b in zip(content, content[1:]):
            self._add(vec, f"b:{a}|{b}", self.bigram_weight)
        for w in content:
            padded = f" {w} "
            for n in self.char_ngrams:
                for i in range(max(1, len(padded) - n + 1)):
                    self._add(vec, "c:" + padded[i:i + n], self.char_weight)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec

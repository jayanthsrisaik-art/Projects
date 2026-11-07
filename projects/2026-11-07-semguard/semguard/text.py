"""Text normalisation and tokenisation shared by the embedder, fact extractor and exact-match key."""
from __future__ import annotations

import re
import unicodedata

_CONTRACTIONS = [
    ("can't", "can not"), ("cannot", "can not"), ("won't", "will not"), ("n't", " not"),
    ("'re", " are"), ("'m", " am"), ("'ve", " have"), ("'ll", " will"), ("'d", " would"), ("'s", ""),
]
# Tokens keep inner separators so identifiers survive: ord-1234, a@b.com, v2.3, 3.5
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_.@/:][a-z0-9]+)*")

STOPWORDS = frozenset(
    """a an the i me my we our you your it its is are was were be been am do does did to of in on for
    at by with from as and or if so this that these those there here what which who how can could would
    should will shall may might must please hi hey hello thanks thank team quick question help just any
    some about into get got have has had want need like way""".split()
)


def normalize(text: str) -> str:
    """Lower-case, NFKC-fold, expand contractions and keep only word/id tokens."""
    t = unicodedata.normalize("NFKC", text).lower().replace("’", "'").replace("`", "'")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)  # 1,000 -> 1000
    for src, dst in _CONTRACTIONS:
        t = t.replace(src, dst)
    return " ".join(TOKEN_RE.findall(t))


def tokens(text: str) -> list[str]:
    """Tokens of an already-normalised string."""
    return text.split()


def stem(word: str) -> str:
    """Very light suffix stripping (enough to match plural/-ing/-ed variants)."""
    if len(word) <= 4 or not word.isalpha():
        return word
    for suf in ("ing", "ed", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word

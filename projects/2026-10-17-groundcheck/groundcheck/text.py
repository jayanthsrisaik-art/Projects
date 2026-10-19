"""Text normalisation shared by every verifier."""
import re

STOPWORDS = frozenset(
    """a an the and or but of to in on at by for with from as is are was were be been being
    it its this that these those there their they them he she his her we you your i can may might
    will would should could shall do does did has have had than then so such also into over under
    about up out per any all each more most some which who whom what when where while""".split()
)
NEGATIONS = frozenset({"no", "not", "never", "none", "neither", "nor", "without", "cannot", "n't"})
ANTONYMS = [
    ("open", "closed"), ("increase", "decrease"), ("rise", "fall"), ("grew", "fell"),
    ("before", "after"), ("allow", "prohibit"), ("more", "fewer"), ("higher", "lower"),
    ("start", "end"), ("accept", "reject"), ("win", "lose"), ("above", "below"),
]
_ANTONYM_MAP = {}
for _a, _b in ANTONYMS:
    _ANTONYM_MAP.setdefault(_a, set()).add(_b)
    _ANTONYM_MAP.setdefault(_b, set()).add(_a)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*|\d[\d,]*(?:\.\d+)?")


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def normalise_number(token: str) -> str:
    token = token.replace(",", "")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token


def numbers(text: str) -> set[str]:
    text = re.sub(r"(?<=\d)[.,](?=\s|$)", "", text)
    return {normalise_number(m) for m in _NUMBER.findall(text)}


def _prep(text: str) -> str:
    return re.sub(r"\b([ap])\.m\b\.?", r"\1m", text, flags=re.IGNORECASE)


def tokens(text: str) -> list[str]:
    """Raw lowercase word/number tokens (negations kept)."""
    out = []
    for w in _WORD.findall(_prep(text)):
        out.append(normalise_number(w.rstrip(",")) if w[0].isdigit() else w.lower())
    return out


def content_tokens(text: str) -> set[str]:
    """Stemmed non-stopword, non-negation tokens, numbers included."""
    out = set()
    for w in tokens(text):
        if w in STOPWORDS or w in NEGATIONS:
            continue
        out.add(w if w[0].isdigit() else stem(w))
    return out


def negation_count(text: str) -> int:
    text = text.replace("n't", " not ")
    return sum(1 for w in tokens(text) if w in NEGATIONS)


def proper_nouns(sentence: str) -> set[str]:
    """Capitalised words that are not the first word of the sentence."""
    words = _WORD.findall(_prep(sentence))
    return {stem(w.lower()) for w in words[1:] if w[0].isupper() and w.lower() not in STOPWORDS}


def antonym_clash(claim: str, evidence: str) -> bool:
    """True when claim and evidence use opposite members of an antonym pair."""
    c = {stem(w) for w in tokens(claim)} | set(tokens(claim))
    e = {stem(w) for w in tokens(evidence)} | set(tokens(evidence))
    for word, opposites in _ANTONYM_MAP.items():
        if word in c and word not in e and (opposites & e) and not (opposites & c):
            return True
    return False

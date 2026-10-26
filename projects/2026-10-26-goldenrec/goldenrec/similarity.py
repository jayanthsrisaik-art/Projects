"""String comparators used by blocking and scoring (pure Python, no dependencies)."""

from __future__ import annotations


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if not a or not b:
        return 0.0
    window = max(max(len(a), len(b)) // 2 - 1, 0)
    a_flags = [False] * len(a)
    b_flags = [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        lo, hi = max(0, i - window), min(len(b), i + window + 1)
        for j in range(lo, hi):
            if not b_flags[j] and b[j] == ch:
                a_flags[i] = b_flags[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    a_matched = [ch for ch, f in zip(a, a_flags) if f]
    b_matched = [ch for ch, f in zip(b, b_flags) if f]
    transpositions = sum(x != y for x, y in zip(a_matched, b_matched)) / 2
    m = matches
    return (m / len(a) + m / len(b) + (m - transpositions) / m) / 3


def jaro_winkler(a: str, b: str, prefix_scale: float = 0.1) -> float:
    score = jaro(a, b)
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return score + prefix * prefix_scale * (1 - score)


_SOUNDEX_CODES = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}


def soundex(word: str | None) -> str | None:
    """American Soundex (H and W don't separate equal codes; vowels do)."""
    if not word:
        return None
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return None
    first = letters[0]
    out = [first.upper()]
    prev = _SOUNDEX_CODES.get(first, "")
    for ch in letters[1:]:
        code = _SOUNDEX_CODES.get(ch, "")
        if code and code != prev:
            out.append(code)
        if ch not in "hw":
            prev = code
        if len(out) == 4:
            break
    return "".join(out).ljust(4, "0")


def token_jaccard(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / len(ta | tb)

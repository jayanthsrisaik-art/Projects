"""npm-style version range parsing.

Supported syntax: ``1.2.3``, ``=1.2.3``, ``>``/``>=``/``<``/``<=``, partial and
wildcard versions (``1``, ``1.2``, ``1.x``, ``1.2.*``, ``*``), tilde (``~1.2.3``),
caret (``^0.2.3``), hyphen ranges (``1.2 - 2.3.4``), whitespace for AND and
``||`` for OR.

Prerelease rule (as in npm): a prerelease version such as ``2.0.0-rc.1`` only
satisfies a comparator set when every comparator matches *and* at least one
comparator in that set names a prerelease of the same ``major.minor.patch``.
So ``^1.0.0`` never selects ``1.5.0-beta`` but ``>=1.5.0-beta <2`` does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .semver import Version, parse_version


class RangeError(ValueError):
    pass


_PART = r"(?:x|X|\*|0|[1-9]\d*)"
_PARTIAL_RE = re.compile(
    rf"^v?({_PART})(?:\.({_PART})(?:\.({_PART})"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)?)?$"
)
_OP_RE = re.compile(r"^(<=|>=|<|>|=|\^|~>?|)(.*)$")
_HYPHEN_RE = re.compile(r"^(\S+)\s+-\s+(\S+)$")

_ZERO = Version(0, 0, 0)


@dataclass(frozen=True)
class Comparator:
    op: str  # one of < <= > >= =
    version: Version

    def test(self, v: Version) -> bool:
        if self.op == "<":
            return v < self.version
        if self.op == "<=":
            return v <= self.version
        if self.op == ">":
            return v > self.version
        if self.op == ">=":
            return v >= self.version
        return v == self.version

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


ANY = (Comparator(">=", _ZERO),)
NOTHING = (Comparator("<", _ZERO),)


def _partial(text: str) -> tuple[int | None, int | None, int | None, tuple[str, ...]]:
    m = _PARTIAL_RE.match(text)
    if not m:
        raise RangeError(f"invalid version in range: {text!r}")
    parts: list[int | None] = []
    for g in m.groups()[:3]:
        parts.append(None if g is None or g in ("x", "X", "*") else int(g))
    # A wildcard makes every later component a wildcard too ("1.x.3" == "1.x").
    for i in range(3):
        if parts[i] is None:
            parts[i + 1 :] = [None] * (2 - i)
            break
    pre = tuple(m.group(4).split(".")) if m.group(4) and parts[2] is not None else ()
    return parts[0], parts[1], parts[2], pre


def _v(major: int, minor: int = 0, patch: int = 0, pre: tuple[str, ...] = ()) -> Version:
    return Version(major, minor, patch, pre)


def _xrange(M, m, p, pre) -> tuple[Comparator, ...]:
    if M is None:
        return ANY
    if m is None:
        return (Comparator(">=", _v(M)), Comparator("<", _v(M + 1)))
    if p is None:
        return (Comparator(">=", _v(M, m)), Comparator("<", _v(M, m + 1)))
    return (Comparator("=", _v(M, m, p, pre)),)


def _tilde(M, m, p, pre) -> tuple[Comparator, ...]:
    if M is None:
        return ANY
    if m is None:
        return (Comparator(">=", _v(M)), Comparator("<", _v(M + 1)))
    return (Comparator(">=", _v(M, m, p or 0, pre)), Comparator("<", _v(M, m + 1)))


def _caret(M, m, p, pre) -> tuple[Comparator, ...]:
    if M is None:
        return ANY
    if m is None:
        return (Comparator(">=", _v(M)), Comparator("<", _v(M + 1)))
    lower = Comparator(">=", _v(M, m, p or 0, pre))
    if M > 0:
        upper = _v(M + 1)
    elif m > 0 or p is None:
        upper = _v(0, m + 1)
    else:
        upper = _v(0, 0, p + 1)
    return (lower, Comparator("<", upper))


def _primitive(op: str, M, m, p, pre) -> tuple[Comparator, ...]:
    if op == ">":
        if M is None:
            return NOTHING
        if m is None:
            return (Comparator(">=", _v(M + 1)),)
        if p is None:
            return (Comparator(">=", _v(M, m + 1)),)
        return (Comparator(">", _v(M, m, p, pre)),)
    if op == ">=":
        if M is None:
            return ANY
        return (Comparator(">=", _v(M, m or 0, p or 0, pre)),)
    if op == "<":
        if M is None:
            return NOTHING
        return (Comparator("<", _v(M, m or 0, p or 0, pre)),)
    if op == "<=":
        if M is None:
            return ANY
        if m is None:
            return (Comparator("<", _v(M + 1)),)
        if p is None:
            return (Comparator("<", _v(M, m + 1)),)
        return (Comparator("<=", _v(M, m, p, pre)),)
    return _xrange(M, m, p, pre)


def _hyphen(lo: str, hi: str) -> tuple[Comparator, ...]:
    lower = _primitive(">=", *_partial(lo))
    upper = _primitive("<=", *_partial(hi))
    return tuple(c for c in lower + upper if c not in ANY)


def _comparator_set(text: str) -> tuple[Comparator, ...]:
    text = text.strip()
    if text == "":
        return ANY
    hm = _HYPHEN_RE.match(text)
    if hm:
        return _hyphen(hm.group(1), hm.group(2)) or ANY
    out: list[Comparator] = []
    for token in text.split():
        om = _OP_RE.match(token)
        assert om is not None
        op, rest = om.groups()
        parts = _partial(rest)
        if op == "^":
            out.extend(_caret(*parts))
        elif op.startswith("~"):
            out.extend(_tilde(*parts))
        else:
            out.extend(_primitive(op, *parts))
    return tuple(out)


@dataclass(frozen=True)
class Range:
    raw: str
    sets: tuple[tuple[Comparator, ...], ...]

    def satisfies(self, v: Version) -> bool:
        return any(_set_ok(cs, v) for cs in self.sets)

    def filter(self, versions: Iterable[Version]) -> list[Version]:
        return [v for v in versions if self.satisfies(v)]

    def __str__(self) -> str:
        return self.raw


def _set_ok(cs: tuple[Comparator, ...], v: Version) -> bool:
    if not all(c.test(v) for c in cs):
        return False
    if v.is_prerelease:
        return any(c.version.is_prerelease and c.version.core == v.core for c in cs)
    return True


def parse_range(text: str) -> Range:
    if not isinstance(text, str):
        raise RangeError(f"range must be a string, got {type(text).__name__}")
    # Allow "> = 1.2" style spacing between an operator and its version.
    norm = re.sub(r"(<=|>=|<|>|=|\^|~>?)\s+", r"\1", text.strip())
    sets = tuple(_comparator_set(part) for part in norm.split("||"))
    return Range(text.strip() or "*", sets)


def exact(v: Version | str) -> Range:
    version = parse_version(v) if isinstance(v, str) else v
    return Range(f"={version}", ((Comparator("=", version),),))

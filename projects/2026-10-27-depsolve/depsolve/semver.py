"""Semantic Versioning 2.0.0 parsing and precedence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_NUM = r"0|[1-9]\d*"
_IDENT = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION_RE = re.compile(
    rf"^v?({_NUM})\.({_NUM})\.({_NUM})"
    rf"(?:-({_IDENT}(?:\.{_IDENT})*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class VersionError(ValueError):
    pass


def _ident_key(ident: str) -> tuple:
    # Numeric identifiers sort before alphanumeric ones and compare numerically.
    return (0, int(ident), "") if ident.isdigit() else (1, 0, ident)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def _key(self) -> tuple:
        # A release outranks any prerelease of the same core; build metadata is ignored.
        pre = (1,) if not self.prerelease else (0, tuple(_ident_key(i) for i in self.prerelease))
        return (self.core, pre)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(self.prerelease)
        if self.build:
            s += "+" + ".".join(self.build)
        return s

    def __repr__(self) -> str:
        return f"Version('{self}')"


def parse_version(text: str) -> Version:
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise VersionError(f"invalid semantic version: {text!r}")
    major, minor, patch, pre, build = m.groups()
    return Version(
        int(major),
        int(minor),
        int(patch),
        tuple(pre.split(".")) if pre else (),
        tuple(build.split(".")) if build else (),
    )

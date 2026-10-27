"""In-memory package index loaded from JSON.

Format::

    {"packages": {
        "web": {
            "2.1.0": {"dependencies": {"log": "^1.0.0"}},
            "2.0.0": {"dependencies": {}, "yanked": true}
        }
    }}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .ranges import Range, parse_range
from .semver import Version, parse_version


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class Release:
    name: str
    version: Version
    dependencies: dict[str, Range] = field(default_factory=dict)
    yanked: bool = False


class Registry:
    def __init__(self, releases: list[Release] | None = None) -> None:
        self._releases: dict[str, dict[Version, Release]] = {}
        self._sorted: dict[str, list[Version]] = {}
        for r in releases or []:
            self.add(r)

    def add(self, release: Release) -> None:
        by_version = self._releases.setdefault(release.name, {})
        if release.version in by_version:
            raise RegistryError(f"duplicate release {release.name}@{release.version}")
        by_version[release.version] = release
        self._sorted[release.name] = sorted(by_version, reverse=True)

    def __contains__(self, name: str) -> bool:
        return name in self._releases

    def names(self) -> list[str]:
        return sorted(self._releases)

    def versions(self, name: str) -> list[Version]:
        """All published versions, newest first (yanked included)."""
        return list(self._sorted.get(name, []))

    def get(self, name: str, version: Version) -> Release:
        try:
            return self._releases[name][version]
        except KeyError:
            raise RegistryError(f"unknown release {name}@{version}") from None

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        packages = data.get("packages")
        if not isinstance(packages, dict):
            raise RegistryError("registry JSON must contain a 'packages' object")
        reg = cls()
        for name, versions in sorted(packages.items()):
            for vtext, meta in versions.items():
                meta = meta or {}
                deps = {d: parse_range(r) for d, r in (meta.get("dependencies") or {}).items()}
                reg.add(Release(name, parse_version(vtext), deps, bool(meta.get("yanked", False))))
        return reg

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        return cls.from_dict(json.loads(Path(path).read_text()))


def load_manifest(path: str | Path) -> tuple[str, dict[str, Range]]:
    data = json.loads(Path(path).read_text())
    deps = {d: parse_range(r) for d, r in (data.get("dependencies") or {}).items()}
    return data.get("name", "root"), deps


def load_lock(path: str | Path) -> dict[str, Version]:
    data = json.loads(Path(path).read_text())
    return {name: parse_version(meta["version"]) for name, meta in data.get("packages", {}).items()}

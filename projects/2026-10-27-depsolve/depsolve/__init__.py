"""depsolve: a semantic-versioning dependency resolver with explanations."""

from .ranges import Range, parse_range
from .registry import Registry, Release
from .resolver import Resolution, ResolutionError, Resolver, resolve
from .semver import Version, parse_version

__all__ = [
    "Range",
    "Registry",
    "Release",
    "Resolution",
    "ResolutionError",
    "Resolver",
    "Version",
    "parse_range",
    "parse_version",
    "resolve",
]

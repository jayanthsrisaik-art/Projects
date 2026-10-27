from pathlib import Path

from depsolve.ranges import parse_range
from depsolve.semver import parse_version
from depsolve.synth import backjump_problem, make_registry, random_problem  # noqa: F401

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def deps(d: dict) -> dict:
    return {k: parse_range(v) for k, v in d.items()}


def versions(d: dict) -> dict:
    return {k: parse_version(v) for k, v in d.items()}

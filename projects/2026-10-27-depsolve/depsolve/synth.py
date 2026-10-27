"""Seeded synthetic registries for property tests and the benchmark."""

from __future__ import annotations

import random

from .ranges import parse_range
from .registry import Registry


def make_registry(spec: dict) -> Registry:
    """{"a": {"1.0.0": {"b": "^1"}, "2.0.0!": {}}} — a trailing "!" marks a yanked release."""
    packages = {}
    for name, versions in spec.items():
        packages[name] = {}
        for v, deps in versions.items():
            packages[name][v.rstrip("!")] = {"dependencies": deps, "yanked": v.endswith("!")}
    return Registry.from_dict({"packages": packages})


def _deps(d: dict) -> dict:
    return {k: parse_range(v) for k, v in d.items()}


VERSION_POOL = ["1.0.0", "1.1.0", "1.2.0", "2.0.0", "2.1.0", "3.0.0"]
RANGE_POOL = ["^1.0.0", "^2.0.0", "~1.1.0", ">=1.1.0", "<2.0.0", "*", "1.x || 3.x", ">=2.1.0", "=1.0.0"]


def random_problem(rng: random.Random):
    names = [f"p{i}" for i in range(rng.randint(3, 5))]
    spec = {}
    for n in names:
        spec[n] = {}
        for v in rng.sample(VERSION_POOL, rng.randint(1, 3)):
            ds = {}
            for q in rng.sample(names, rng.randint(0, 2)):
                ds[q] = rng.choice(RANGE_POOL)
            if rng.random() < 0.03:
                ds["ghost"] = "*"
            spec[n][v + ("!" if rng.random() < 0.1 else "")] = ds
    root = {q: rng.choice(RANGE_POOL) for q in rng.sample(names, rng.randint(1, 3))}
    reg = make_registry(spec)
    locked = {}
    for n in names:
        if rng.random() < 0.3:
            locked[n] = rng.choice(reg.versions(n))
    return reg, _deps(root), locked


def backjump_problem(k: int):
    """a@2 drags in q whose every version needs w ^2, but the root pins w ^1.
    k independent packages m0..m(k-1) (4 versions each) are decided before q."""
    spec = {
        "a": {"2.0.0": {"q": "^1"}, "1.0.0": {}},
        "q": {f"1.{i}.0": {"w": "^2"} for i in range(5)},
        "w": {"1.0.0": {}, "2.0.0": {}},
    }
    for j in range(k):
        spec[f"m{j}"] = {f"1.{i}.0": {} for i in range(4)}
    root = {"a": "*", "w": "^1", **{f"m{j}": "*" for j in range(k)}}
    return make_registry(spec), _deps(root)

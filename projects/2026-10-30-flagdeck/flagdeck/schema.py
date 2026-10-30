"""Flag document validation.

Pydantic handles shape; ``semantic_errors`` handles the cross-field rules a JSON
schema cannot express (indices in range, weights summing to 100%, regexes that
compile, prerequisite graph acyclic).
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evaluator import BUCKET_SCALE, _kind

ENVIRONMENTS = ("dev", "prod")
OPS = ("in", "startsWith", "endsWith", "contains", "matches", "lt", "lte", "gt", "gte")
KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Clause(Strict):
    attribute: str = Field(min_length=1)
    op: Literal[OPS]  # type: ignore[valid-type]
    values: list[Any] = Field(min_length=1)
    negate: bool = False


class Rollout(Strict):
    bucket_by: str = "key"
    weights: list[int]


class Serve(Strict):
    variation: Optional[int] = None
    rollout: Optional[Rollout] = None

    @model_validator(mode="after")
    def exactly_one(self) -> "Serve":
        if (self.variation is None) == (self.rollout is None):
            raise ValueError("serve needs exactly one of 'variation' or 'rollout'")
        return self


class Rule(Strict):
    id: str = Field(min_length=1, max_length=64)
    description: str = ""
    clauses: list[Clause] = Field(min_length=1)
    serve: Serve


class Target(Strict):
    variation: int
    values: list[str]


class EnvConfig(Strict):
    on: bool = False
    off_variation: int = 0
    targets: list[Target] = []
    rules: list[Rule] = []
    fallthrough: Serve = Serve(variation=0)


class Prerequisite(Strict):
    key: str
    variation: int


class FlagIn(Strict):
    key: str = Field(pattern=KEY_PATTERN)
    description: str = ""
    variations: list[Any] = Field(min_length=2)
    salt: str = ""
    prerequisites: list[Prerequisite] = []
    environments: dict[str, EnvConfig] = {}

    @model_validator(mode="after")
    def fill_envs(self) -> "FlagIn":
        unknown = set(self.environments) - set(ENVIRONMENTS)
        if unknown:
            raise ValueError(f"unknown environments: {sorted(unknown)}")
        for env in ENVIRONMENTS:
            self.environments.setdefault(env, EnvConfig())
        if not self.salt:
            self.salt = self.key
        return self

    def doc(self) -> dict:
        return self.model_dump(mode="json")


def _check_serve(serve: dict, n: int, where: str, errors: list[str]) -> None:
    if serve.get("variation") is not None:
        if not 0 <= serve["variation"] < n:
            errors.append(f"{where}.variation {serve['variation']} out of range 0..{n - 1}")
        return
    weights = serve["rollout"]["weights"]
    if len(weights) != n:
        errors.append(f"{where}.rollout.weights has {len(weights)} entries, expected {n}")
    if any(w < 0 for w in weights):
        errors.append(f"{where}.rollout.weights must be non-negative")
    if sum(weights) != BUCKET_SCALE:
        errors.append(f"{where}.rollout.weights sum to {sum(weights)}, expected {BUCKET_SCALE}")


def semantic_errors(doc: dict, all_flags: dict[str, dict]) -> list[str]:
    """Return human-readable errors for ``doc`` given the other stored flags (keyed by flag key)."""
    errors: list[str] = []
    variations = doc["variations"]
    n = len(variations)
    kinds = {_kind(v) for v in variations}
    if len(kinds) != 1:
        errors.append(f"variations must share one JSON type, got {sorted(kinds)}")
    for i, v in enumerate(variations):
        if any(_kind(v) == _kind(w) and v == w for w in variations[:i]):
            errors.append(f"variation {i} duplicates an earlier variation")

    for env, cfg in doc["environments"].items():
        p = f"environments.{env}"
        if not 0 <= cfg["off_variation"] < n:
            errors.append(f"{p}.off_variation {cfg['off_variation']} out of range 0..{n - 1}")
        seen_keys: set[str] = set()
        for ti, t in enumerate(cfg["targets"]):
            if not 0 <= t["variation"] < n:
                errors.append(f"{p}.targets[{ti}].variation {t['variation']} out of range")
            dup = seen_keys & set(t["values"])
            if dup:
                errors.append(f"{p}.targets[{ti}] repeats context keys {sorted(dup)}")
            seen_keys |= set(t["values"])
        rule_ids: set[str] = set()
        for ri, r in enumerate(cfg["rules"]):
            if r["id"] in rule_ids:
                errors.append(f"{p}.rules[{ri}].id '{r['id']}' is not unique")
            rule_ids.add(r["id"])
            for ci, c in enumerate(r["clauses"]):
                cp = f"{p}.rules[{ri}].clauses[{ci}]"
                if c["op"] == "matches":
                    for v in c["values"]:
                        if not isinstance(v, str):
                            errors.append(f"{cp} regex must be a string")
                            continue
                        try:
                            re.compile(v)
                        except re.error as exc:
                            errors.append(f"{cp} invalid regex {v!r}: {exc}")
                if c["op"] in ("lt", "lte", "gt", "gte") and any(_kind(v) != "number" for v in c["values"]):
                    errors.append(f"{cp} '{c['op']}' needs numeric values")
                if c["op"] in ("startsWith", "endsWith", "contains") and any(not isinstance(v, str) for v in c["values"]):
                    errors.append(f"{cp} '{c['op']}' needs string values")
            _check_serve(r["serve"], n, f"{p}.rules[{ri}].serve", errors)
        _check_serve(cfg["fallthrough"], n, f"{p}.fallthrough", errors)

    graph = {k: [pr["key"] for pr in f.get("prerequisites", [])] for k, f in all_flags.items()}
    graph[doc["key"]] = [pr["key"] for pr in doc["prerequisites"]]
    for pr in doc["prerequisites"]:
        target = all_flags.get(pr["key"]) if pr["key"] != doc["key"] else doc
        if target is None:
            errors.append(f"prerequisite '{pr['key']}' does not exist")
        elif not 0 <= pr["variation"] < len(target["variations"]):
            errors.append(f"prerequisite '{pr['key']}' variation {pr['variation']} out of range")
    cycle = find_cycle(graph, doc["key"])
    if cycle:
        errors.append("prerequisite cycle: " + " -> ".join(cycle))
    return errors


def find_cycle(graph: dict[str, list[str]], start: str) -> list[str] | None:
    """Return a cycle reachable from ``start`` as a path list, or None."""
    path: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        if node in on_path:
            return path[path.index(node):] + [node]
        if node in done:
            return None
        path.append(node)
        on_path.add(node)
        for nxt in graph.get(node, []):
            found = dfs(nxt)
            if found:
                return found
        path.pop()
        on_path.discard(node)
        done.add(node)
        return None

    return dfs(start)

"""Load models, seeds and tests from a project directory and build the dependency graph."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REF_RE = re.compile(r"\{\{\s*ref\(\s*'([A-Za-z_]\w*)'\s*\)\s*\}\}")
CONFIG_RE = re.compile(r"\{\{\s*config\((.*?)\)\s*\}\}", re.S)
THIS_RE = re.compile(r"\{\{\s*this\s*\}\}")
INCR_BLOCK_RE = re.compile(r"\{%\s*if is_incremental\s*%\}(.*?)\{%\s*endif\s*%\}", re.S)
KV_RE = re.compile(r"(\w+)\s*=\s*'([^']*)'")
MATERIALIZATIONS = {"view", "table", "incremental"}


class ProjectError(Exception):
    pass


@dataclass
class Model:
    name: str
    raw_sql: str
    materialized: str = "view"
    unique_key: str | None = None
    deps: list[str] = field(default_factory=list)


@dataclass
class Project:
    models: dict[str, Model]
    seeds: dict[str, Path]
    tests: list[dict]


def parse_model(name: str, text: str) -> Model:
    cfg = {}
    m = CONFIG_RE.search(text)
    if m:
        cfg = dict(KV_RE.findall(m.group(1)))
        text = CONFIG_RE.sub("", text)
    mat = cfg.get("materialized", "view")
    if mat not in MATERIALIZATIONS:
        raise ProjectError(f"model {name}: unknown materialization {mat!r}")
    if mat == "incremental" and "unique_key" not in cfg:
        raise ProjectError(f"model {name}: incremental models need unique_key")
    deps = list(dict.fromkeys(REF_RE.findall(text)))
    return Model(name, text.strip(), mat, cfg.get("unique_key"), deps)


def load_project(root: str | Path) -> Project:
    root = Path(root)
    models = {p.stem: parse_model(p.stem, p.read_text()) for p in sorted((root / "models").glob("*.sql"))}
    seeds = {p.stem: p for p in sorted((root / "seeds").glob("*.csv"))}
    dup = set(models) & set(seeds)
    if dup:
        raise ProjectError(f"names used by both model and seed: {sorted(dup)}")
    known = set(models) | set(seeds)
    for m in models.values():
        for d in m.deps:
            if d not in known:
                raise ProjectError(f"model {m.name} refs unknown relation {d!r}")
    tests = []
    tf = root / "tests.toml"
    if tf.exists():
        tests = tomllib.loads(tf.read_text()).get("test", [])
    for t in tests:
        if t.get("model") not in known:
            raise ProjectError(f"test references unknown relation {t.get('model')!r}")
    return Project(models, seeds, tests)


def render(model: Model, incremental_run: bool) -> str:
    """Resolve ref(), this and the is_incremental block into plain SQL."""
    sql = INCR_BLOCK_RE.sub(lambda m: m.group(1) if incremental_run else "", model.raw_sql)
    sql = REF_RE.sub(lambda m: m.group(1), sql)
    return THIS_RE.sub(model.name, sql).strip().rstrip(";")


def build_order(project: Project) -> list[str]:
    """Deterministic topological order of models (Kahn); raises on cycles."""
    indeg = {n: sum(d in project.models for d in m.deps) for n, m in project.models.items()}
    children: dict[str, list[str]] = {n: [] for n in project.models}
    for n, m in project.models.items():
        for d in m.deps:
            if d in project.models:
                children[d].append(n)
    ready = sorted(n for n, k in indeg.items() if k == 0)
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in sorted(children[n]):
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
        ready.sort()
    if len(order) != len(project.models):
        cyc = sorted(set(project.models) - set(order))
        raise ProjectError(f"dependency cycle among: {cyc}")
    return order


def select(project: Project, spec: str) -> set[str]:
    """Selector: 'name', '+name' (with ancestors), 'name+' (with descendants)."""
    up, down = spec.startswith("+"), spec.endswith("+")
    name = spec.strip("+")
    if name not in project.models:
        raise ProjectError(f"unknown model {name!r}")
    out = {name}
    if up:
        stack = [name]
        while stack:
            for d in project.models[stack.pop()].deps:
                if d in project.models and d not in out:
                    out.add(d)
                    stack.append(d)
    if down:
        stack = [name]
        while stack:
            cur = stack.pop()
            for n, m in project.models.items():
                if cur in m.deps and n not in out:
                    out.add(n)
                    stack.append(n)
    return out

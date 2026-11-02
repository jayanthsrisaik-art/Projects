"""Pipeline spec loading and validation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .partitions import ALL, GRAINS, Calendar, Mapping

Node = tuple[str, str]


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Dep:
    table: str
    mapping: Mapping


@dataclass
class Table:
    name: str
    grain: str
    columns: list[str]
    source: bool = False
    sql: str = ""
    deps: list[Dep] = field(default_factory=list)


@dataclass
class Pipeline:
    cal: Calendar
    tables: dict[str, Table]
    order: list[str]  # table-level topological order (self-deps ignored)
    downstream: dict[str, list[tuple[str, Mapping]]]

    @property
    def index(self) -> dict[str, int]:
        return {t: i for i, t in enumerate(self.order)}

    def grain(self, table: str) -> str:
        return self.tables[table].grain

    def all_nodes(self, derived_only: bool = False) -> list[Node]:
        return [
            (t, k)
            for t in self.order
            if not (derived_only and self.tables[t].source)
            for k in self.cal.partitions(self.tables[t].grain)
        ]

    def sort_nodes(self, nodes) -> list[Node]:
        """(table topo index, key) is a valid topological order for nodes."""
        idx = self.index
        return sorted(nodes, key=lambda n: (idx[n[0]], n[1]))


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING = re.compile(r"'(?:[^']|'')*'")
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def referenced_tables(sql: str, known: set[str]) -> set[str]:
    """Known table names mentioned in SQL (strings/comments stripped)."""
    body = _STRING.sub("''", _COMMENT.sub(" ", sql))
    return {m.group(0) for m in _IDENT.finditer(body)} & known


def _topo(tables: dict[str, Table]) -> list[str]:
    indeg = {t: 0 for t in tables}
    children: dict[str, list[str]] = {t: [] for t in tables}
    for t in tables.values():
        for up in sorted({d.table for d in t.deps if d.table != t.name}):
            indeg[t.name] += 1
            children[up].append(t.name)
    ready = sorted(t for t, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        t = ready.pop(0)
        order.append(t)
        for c in sorted(children[t]):
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
        ready.sort()
    if len(order) != len(tables):
        cyc = sorted(t for t, d in indeg.items() if d > 0)
        raise SpecError(f"dependency cycle between tables: {', '.join(cyc)}")
    return order


def build_pipeline(raw: dict, base_dir: Path | None = None) -> Pipeline:
    try:
        cal = Calendar(raw["range"]["start"], raw["range"]["end"])
    except (KeyError, TypeError) as e:
        raise SpecError("spec needs range.start and range.end") from e
    except ValueError as e:
        raise SpecError(str(e)) from e

    tables: dict[str, Table] = {}
    for name, t in raw.get("tables", {}).items():
        if not _IDENT.fullmatch(name):
            raise SpecError(f"invalid table name {name!r}")
        grain = t.get("grain")
        if grain not in GRAINS:
            raise SpecError(f"{name}: grain must be one of {GRAINS}, got {grain!r}")
        cols = t.get("columns") or []
        if not cols:
            raise SpecError(f"{name}: needs a non-empty 'columns' list")
        source = bool(t.get("source", False))
        sql = t.get("sql", "")
        if sql.endswith(".sql"):
            path = (base_dir or Path(".")) / sql
            try:
                sql = path.read_text()
            except OSError as e:
                raise SpecError(f"{name}: cannot read SQL file {path}") from e
        deps = []
        for d in t.get("deps", []):
            try:
                deps.append(Dep(d["table"], Mapping(d["mapping"], int(d.get("n", 1)))))
            except KeyError as e:
                raise SpecError(f"{name}: dep needs 'table' and 'mapping'") from e
        if source and (deps or sql):
            raise SpecError(f"{name}: source tables cannot have deps or sql")
        if not source and not sql.strip():
            raise SpecError(f"{name}: derived table needs 'sql'")
        if not source and not deps:
            raise SpecError(f"{name}: derived table needs at least one dep")
        tables[name] = Table(name, grain, list(cols), source, sql, deps)

    for t in tables.values():
        for d in t.deps:
            if d.table not in tables:
                raise SpecError(f"{t.name}: depends on unknown table {d.table!r}")
            try:
                d.mapping.validate(t.grain, tables[d.table].grain)
            except ValueError as e:
                raise SpecError(f"{t.name} -> {d.table}: {e}") from e
            if d.table == t.name and d.mapping.kind != "lag":
                raise SpecError(
                    f"{t.name}: a self-dependency must be 'lag' (n >= 1) to stay acyclic"
                )
        if not t.source:
            refs = referenced_tables(t.sql, set(tables))
            declared = {d.table for d in t.deps}
            if refs - declared:
                raise SpecError(
                    f"{t.name}: SQL reads undeclared table(s) {sorted(refs - declared)}"
                )
            if declared - refs:
                raise SpecError(
                    f"{t.name}: declared dep(s) never referenced in SQL {sorted(declared - refs)}"
                )

    order = _topo(tables)
    downstream: dict[str, list[tuple[str, Mapping]]] = {t: [] for t in tables}
    for t in order:
        for d in tables[t].deps:
            downstream[d.table].append((t, d.mapping))
    return Pipeline(cal, tables, order, downstream)


def load_pipeline(path: str | Path) -> Pipeline:
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SpecError(f"cannot load spec {path}: {e}") from e
    return build_pipeline(raw, path.parent)


def parse_change(p: Pipeline, text: str) -> list[Node]:
    """Parse ``table:key``, ``table:start..end`` or ``table:*`` into nodes."""
    if ":" not in text:
        raise SpecError(f"change {text!r} must look like table:key")
    table, spec = text.split(":", 1)
    if table not in p.tables:
        raise SpecError(f"change {text!r}: unknown table {table!r}")
    grain = p.grain(table)
    parts = p.cal.partitions(grain)
    if spec == ALL:
        return [(table, k) for k in parts]
    if ".." in spec:
        lo, hi = spec.split("..", 1)
        for k in (lo, hi):
            if not p.cal.contains(grain, k):
                raise SpecError(f"change {text!r}: {k!r} is not a {grain} partition in range")
        if hi < lo:
            raise SpecError(f"change {text!r}: empty range")
        return [(table, k) for k in parts if lo <= k <= hi]
    if not p.cal.contains(grain, spec):
        raise SpecError(f"change {text!r}: {spec!r} is not a {grain} partition in range")
    return [(table, spec)]

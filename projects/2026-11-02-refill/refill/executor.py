"""SQLite executor: idempotent partition overwrite + content digests."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .planner import forward_deps
from .spec import Node, Pipeline


def connect(path: str = ":memory:") -> sqlite3.Connection:
    return sqlite3.connect(path)


def create_schema(conn: sqlite3.Connection, p: Pipeline) -> None:
    for name in p.order:
        cols = ", ".join(p.tables[name].columns)
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} (part TEXT NOT NULL, {cols})")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{name}_part ON {name}(part)")
    conn.commit()


def load_sources(conn: sqlite3.Connection, p: Pipeline, data: dict[str, list[tuple]]) -> None:
    """Replace the full contents of the given source tables."""
    for name, rows in data.items():
        if not p.tables[name].source:
            raise ValueError(f"{name} is not a source table")
        ncols = len(p.tables[name].columns) + 1
        conn.execute(f"DELETE FROM {name}")
        conn.executemany(
            f"INSERT INTO {name} VALUES ({', '.join('?' * ncols)})", rows
        )
    conn.commit()


def _params(p: Pipeline, node: Node) -> dict:
    table, key = node
    grain = p.grain(table)
    lo, hi = p.cal.bounds(grain, key)
    return {
        "p": key,
        "p_start": lo,
        "p_end": hi,
        "prev": p.cal.previous(grain, key),
        "month": key[:7] if grain == "day" else key,
    }


def _col_names(p: Pipeline, table: str) -> list[str]:
    return [c.split()[0] for c in p.tables[table].columns]


def run_task(conn: sqlite3.Connection, p: Pipeline, node: Node) -> None:
    """Overwrite one partition (DELETE + INSERT ... SELECT) atomically."""
    table, key = node
    t = p.tables[table]
    cols = ", ".join(_col_names(p, table))
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE part = ?", (key,))
        conn.execute(
            f"INSERT INTO {table} (part, {cols}) SELECT :p, * FROM ({t.sql})",
            _params(p, node),
        )


def partition_digest(conn: sqlite3.Connection, table: str, key: str) -> str:
    rows = conn.execute(f"SELECT * FROM {table} WHERE part = ?", (key,)).fetchall()
    h = hashlib.sha256()
    for r in sorted(repr(r[1:]) for r in rows):
        h.update(r.encode())
        h.update(b"\n")
    return h.hexdigest()


def snapshot(conn: sqlite3.Connection, p: Pipeline) -> dict[Node, str]:
    """Digest of every derived partition (missing partitions digest as empty)."""
    return {n: partition_digest(conn, *n) for n in p.all_nodes(derived_only=True)}


def full_build(conn: sqlite3.Connection, p: Pipeline) -> int:
    nodes = p.all_nodes(derived_only=True)
    for n in nodes:
        run_task(conn, p, n)
    return len(nodes)


@dataclass
class RunStats:
    planned: int
    executed: int
    skipped: int
    changed: int


def run_tasks(conn, p: Pipeline, tasks, roots=(), cutoff: bool = False) -> RunStats:
    """Execute tasks in order.

    With ``cutoff=True`` a task only runs when one of its inputs is *dirty*
    (a changed root, or a recomputed partition whose content digest actually
    changed).  This is early cutoff: identical outputs stop propagation.
    """
    tasks = list(tasks)
    dirty: set[Node] = set(roots)
    forced = {r for r in roots if not p.tables[r[0]].source}
    executed = skipped = changed = 0
    for n in tasks:
        if cutoff and n not in forced and not any(d in dirty for d in forward_deps(p, n)):
            skipped += 1
            continue
        before = partition_digest(conn, *n)
        run_task(conn, p, n)
        executed += 1
        if partition_digest(conn, *n) != before:
            changed += 1
            dirty.add(n)
    return RunStats(len(tasks), executed, skipped, changed)

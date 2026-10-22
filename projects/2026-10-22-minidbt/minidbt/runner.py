"""Execute a project against SQLite: seeds, models, then data tests."""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass

from .project import Project, build_order, render, select

IDENT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def q(ident: str) -> str:
    if not ident or set(ident) - IDENT_OK:
        raise ValueError(f"unsafe identifier {ident!r}")
    return ident


@dataclass
class Result:
    name: str
    status: str  # ok | error | skipped | pass | fail
    detail: str = ""


def _convert(v: str):
    if v == "":
        return None
    for f in (int, float):
        try:
            return f(v)
        except ValueError:
            pass
    return v


def load_seed(conn: sqlite3.Connection, name: str, path) -> int:
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    conn.execute(f"DROP TABLE IF EXISTS {q(name)}")
    conn.execute(f"CREATE TABLE {q(name)} ({', '.join(q(c) for c in header)})")
    conn.executemany(
        f"INSERT INTO {q(name)} VALUES ({','.join('?' * len(header))})",
        [[_convert(v) for v in r] for r in body],
    )
    return len(body)


def _exists(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _kind(conn, name):
    r = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return r[0] if r else None


def run_model(conn: sqlite3.Connection, model, full_refresh: bool = False) -> str:
    n = q(model.name)
    if model.materialized == "incremental":
        if full_refresh or _kind(conn, n) != "table":
            conn.execute(f"DROP VIEW IF EXISTS {n}")
            conn.execute(f"DROP TABLE IF EXISTS {n}")
            conn.execute(f"CREATE TABLE {n} AS {render(model, False)}")
            return "created"
        key = q(model.unique_key)
        conn.execute(f"CREATE TEMP TABLE _delta AS {render(model, True)}")
        try:
            conn.execute(f"DELETE FROM {n} WHERE {key} IN (SELECT {key} FROM _delta)")
            cur = conn.execute(f"INSERT INTO {n} SELECT * FROM _delta")
        finally:
            conn.execute("DROP TABLE IF EXISTS _delta")
        return f"merged {cur.rowcount} rows"
    sql = render(model, False)
    conn.execute(f"DROP VIEW IF EXISTS {n}")
    conn.execute(f"DROP TABLE IF EXISTS {n}")
    kw = "TABLE" if model.materialized == "table" else "VIEW"
    conn.execute(f"CREATE {kw} {n} AS {sql}")
    if kw == "VIEW":  # SQLite resolves views lazily; force validation so bad SQL fails here
        conn.execute(f"SELECT * FROM {n} LIMIT 0")
    return model.materialized


def run(conn, project: Project, selector: str | None = None, full_refresh: bool = False) -> list[Result]:
    results: list[Result] = []
    for name, path in project.seeds.items():
        results.append(Result(name, "ok", f"seed, {load_seed(conn, name, path)} rows"))
    chosen = select(project, selector) if selector else set(project.models)
    failed: set[str] = set()
    for name in build_order(project):
        if name not in chosen:
            continue
        m = project.models[name]
        bad = [d for d in m.deps if d in failed]
        if bad:
            failed.add(name)
            results.append(Result(name, "skipped", f"upstream failed: {bad}"))
            continue
        try:
            with conn:
                detail = run_model(conn, m, full_refresh)
            results.append(Result(name, "ok", detail))
        except sqlite3.Error as e:
            failed.add(name)
            results.append(Result(name, "error", str(e)))
    return results


def _test_sql(t: dict) -> str:
    m, kind = q(t["model"]), t["type"]
    if kind == "not_null":
        c = q(t["column"])
        return f"SELECT * FROM {m} WHERE {c} IS NULL"
    if kind == "unique":
        c = q(t["column"])
        return f"SELECT {c} FROM {m} WHERE {c} IS NOT NULL GROUP BY {c} HAVING COUNT(*) > 1"
    if kind == "accepted_values":
        c = q(t["column"])
        vals = ",".join("'" + str(v).replace("'", "''") + "'" for v in t["values"])
        return f"SELECT * FROM {m} WHERE {c} IS NOT NULL AND {c} NOT IN ({vals})"
    if kind == "relationships":
        c, tt, tc = q(t["column"]), q(t["to"]), q(t["to_column"])
        return f"SELECT * FROM {m} WHERE {c} IS NOT NULL AND {c} NOT IN (SELECT {tc} FROM {tt})"
    if kind == "sql":  # singular test: query returns offending rows
        return t["query"].strip().rstrip(";")
    raise ValueError(f"unknown test type {kind!r}")


def run_tests(conn, project: Project) -> list[Result]:
    out = []
    for t in project.tests:
        label = f"{t['type']}:{t['model']}" + (f".{t['column']}" if "column" in t else "")
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM ({_test_sql(t)})").fetchone()[0]
        except (sqlite3.Error, ValueError) as e:
            out.append(Result(label, "fail", f"could not run: {e}"))
            continue
        out.append(Result(label, "pass" if n == 0 else "fail", "" if n == 0 else f"{n} failing rows"))
    return out

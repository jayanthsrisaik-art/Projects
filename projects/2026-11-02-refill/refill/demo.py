"""End-to-end measurement: every strategy vs a from-scratch rebuild oracle."""
from __future__ import annotations

import time

from . import executor as ex
from .planner import plan, strategy_from_earliest, strategy_full, strategy_same_key
from .spec import Pipeline, parse_change
from .synth import SCENARIOS, generate

STRATEGIES = ("full_rebuild", "from_earliest", "same_key", "refill", "refill_cutoff")


def _copy(conn):
    dst = ex.connect()
    conn.backup(dst)
    return dst


def _fresh_build(p: Pipeline, data) -> dict:
    conn = ex.connect()
    ex.create_schema(conn, p)
    ex.load_sources(conn, p, data)
    ex.full_build(conn, p)
    snap = ex.snapshot(conn, p)
    conn.close()
    return snap


def run_demo(p: Pipeline, seed: int = 7, scenarios=SCENARIOS) -> dict:
    data = generate(p.cal, seed)
    base = ex.connect()
    ex.create_schema(base, p)
    ex.load_sources(base, p, data)
    ex.full_build(base, p)

    results = []
    for sc in scenarios:
        mutated = sc.mutate(data)
        oracle = _fresh_build(p, mutated)
        roots = [n for c in sc.changes for n in parse_change(p, c)]
        pl = plan(p, roots)
        candidates = {
            "full_rebuild": (strategy_full(p, roots), False),
            "from_earliest": (strategy_from_earliest(p, roots), False),
            "same_key": (strategy_same_key(p, roots), False),
            "refill": (pl.tasks, False),
            "refill_cutoff": (pl.tasks, True),
        }
        rows = {}
        for name in STRATEGIES:
            tasks, cutoff = candidates[name]
            conn = _copy(base)
            ex.load_sources(conn, p, mutated)
            t0 = time.perf_counter()
            stats = ex.run_tasks(conn, p, tasks, roots, cutoff=cutoff)
            elapsed = time.perf_counter() - t0
            snap = ex.snapshot(conn, p)
            conn.close()
            wrong = sum(1 for n, h in oracle.items() if snap.get(n) != h)
            rows[name] = {
                "planned": stats.planned,
                "executed": stats.executed,
                "skipped": stats.skipped,
                "wrong_partitions": wrong,
                "seconds": round(elapsed, 4),
            }
        results.append({
            "scenario": sc.name,
            "description": sc.description,
            "changes": sc.changes,
            "partitions_actually_changed": sum(
                1 for n, h in oracle.items() if h != ex.partition_digest(base, *n)
            ),
            "plan_waves": pl.waves,
            "strategies": rows,
        })
    base.close()
    return {"seed": seed, "derived_partitions": len(p.all_nodes(derived_only=True)),
            "scenarios": results}


def format_report(rep: dict) -> str:
    lines = [f"derived partitions in pipeline: {rep['derived_partitions']}  (seed={rep['seed']})", ""]
    for sc in rep["scenarios"]:
        lines.append(f"## {sc['scenario']}: {sc['description']}")
        lines.append(f"   changes={sc['changes']}  partitions that truly changed="
                     f"{sc['partitions_actually_changed']}  plan waves={sc['plan_waves']}")
        lines.append(f"   {'strategy':<15}{'planned':>8}{'executed':>9}{'wrong':>7}{'seconds':>9}")
        for name, r in sc["strategies"].items():
            lines.append(f"   {name:<15}{r['planned']:>8}{r['executed']:>9}"
                         f"{r['wrong_partitions']:>7}{r['seconds']:>9.3f}")
        lines.append("")
    return "\n".join(lines)

"""Command line interface: ``python -m pitjoin <command> ...``."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from . import online, synth
from .engines import MODES, oracle_join, sql_join, sweep_join
from .repo import RepoError, format_ts, load_entities, load_repo, parse_ts
from .training import ALL_JOINS, build_training_set, leakage_report, write_csv


def _cmd_join(a) -> int:
    views = load_repo(a.repo)
    ents = load_entities(a.entities, a.timestamp_field)
    header, rows = build_training_set(views, ents, a.engine, a.mode)
    write_csv(a.out, header, rows)
    print(f"wrote {len(rows)} rows x {len(header)} columns to {a.out} (engine={a.engine}, mode={a.mode})")
    return 0


def _cmd_leakage(a) -> int:
    views = load_repo(a.repo)
    ents = load_entities(a.entities, a.timestamp_field)
    print(json.dumps(leakage_report(views, ents), indent=2))
    return 0


def _cmd_materialize(a) -> int:
    views = load_repo(a.repo)
    conn = online.connect(a.db)
    try:
        stats = online.materialize(conn, views, parse_ts(a.end), full=a.full)
    except online.MaterializeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(stats, indent=2))
    return 0


def _cmd_get(a) -> int:
    views = load_repo(a.repo)
    if a.view not in views:
        print(f"error: unknown view {a.view!r}", file=sys.stderr)
        return 2
    conn = online.connect(a.db)
    try:
        res = online.get_online(conn, views[a.view], a.keys, parse_ts(a.now) if a.now else None)
    except online.MaterializeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(res, indent=2))
    return 0


def _cmd_skew(a) -> int:
    views = load_repo(a.repo)
    conn = online.connect(a.db)
    try:
        res = online.skew_check(conn, views)
    finally:
        conn.close()
    print(json.dumps(res, indent=2))
    return 0 if all(r["status"] == "ok" for r in res.values()) else 1


def _cmd_synth(a) -> int:
    counts = synth.generate(a.out_dir, seed=a.seed, customers=a.customers, days=a.days,
                            transactions=a.transactions)
    print(json.dumps(counts, indent=2))
    return 0


def _timed(fn, *args):
    t = time.perf_counter()
    res = fn(*args)
    return res, time.perf_counter() - t


def _cmd_demo(a) -> int:
    """Generate synthetic data, cross-check engines, measure leakage, check skew."""
    with tempfile.TemporaryDirectory() as tmp:
        counts = synth.generate(tmp, seed=a.seed)
        views = load_repo(Path(tmp) / "feature_repo.json")
        ents = load_entities(Path(tmp) / "transactions.csv")
        print("synthetic data:", json.dumps(counts))

        ok = True
        print("\nengine agreement (bitemporal and event mode):")
        for mode in MODES:
            for name, v in views.items():
                keys = ents.keys(v.entity)
                sw, t_sw = _timed(sweep_join, v, keys, ents.ts, mode)
                sq, t_sq = _timed(sql_join, v, keys, ents.ts, mode)
                n = a.oracle_rows
                orc = oracle_join(v, keys[:n], ents.ts[:n], mode)
                agree = sw == sq and sw[:n] == orc
                ok &= agree
                print(f"  {mode:9s} {name:16s} sweep={t_sw*1000:7.1f}ms sql={t_sq*1000:7.1f}ms "
                      f"oracle_rows={n} agree={agree}")

        rep = leakage_report(views, ents)
        print(f"\nleakage vs bitemporal point-in-time join ({rep['entity_rows']} entity rows):")
        for spec, r in rep["strategies"].items():
            t = r["total"]
            print(f"  {spec:16s} rows_affected={t['rows_affected']:5d} ({t['rows_affected_pct']:5.1f}%) "
                  f"future_event={t['future_event']} not_yet_known={t['not_yet_known']} "
                  f"stale={t['stale']} value_mismatch={t['value_mismatch']}")

        conn = online.connect(Path(tmp) / "online.db")
        start = min(ents.ts)
        end = max(ents.ts)
        steps = 6
        for s in range(1, steps + 1):
            online.materialize(conn, views, start + (end - start) * s // steps)
        skew = online.skew_check(conn, views)
        conn.close()
        print(f"\nonline/offline skew after {steps} incremental materializations to {format_ts(end)}:")
        for name, r in skew.items():
            print(f"  {name:16s} {r['status']} keys={r['keys_checked']} mismatches={len(r['mismatches'])}")
            ok &= r["status"] == "ok"
    print("\nALL CHECKS PASSED" if ok else "\nCHECKS FAILED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitjoin", description="Point-in-time correct feature joins.")
    sub = p.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("join", help="build a training set from an entity spine")
    j.add_argument("repo")
    j.add_argument("entities")
    j.add_argument("--out", required=True)
    j.add_argument("--engine", default="sweep", choices=sorted(ALL_JOINS))
    j.add_argument("--mode", default="knowledge", choices=MODES)
    j.add_argument("--timestamp-field", default="event_timestamp")
    j.set_defaults(fn=_cmd_join)

    lk = sub.add_parser("leakage", help="measure leakage of naive joins vs the PIT join")
    lk.add_argument("repo")
    lk.add_argument("entities")
    lk.add_argument("--timestamp-field", default="event_timestamp")
    lk.set_defaults(fn=_cmd_leakage)

    m = sub.add_parser("materialize", help="advance the online store to END")
    m.add_argument("repo")
    m.add_argument("--db", required=True)
    m.add_argument("--end", required=True, help="ISO timestamp")
    m.add_argument("--full", action="store_true", help="rebuild from scratch")
    m.set_defaults(fn=_cmd_materialize)

    g = sub.add_parser("get", help="serve online features")
    g.add_argument("repo")
    g.add_argument("--db", required=True)
    g.add_argument("--view", required=True)
    g.add_argument("--now", help="ISO timestamp for TTL checks (default: watermark)")
    g.add_argument("keys", nargs="+")
    g.set_defaults(fn=_cmd_get)

    s = sub.add_parser("skew-check", help="compare online store with offline PIT join")
    s.add_argument("repo")
    s.add_argument("--db", required=True)
    s.set_defaults(fn=_cmd_skew)

    sy = sub.add_parser("synth", help="write a synthetic feature repository")
    sy.add_argument("out_dir")
    sy.add_argument("--seed", type=int, default=7)
    sy.add_argument("--customers", type=int, default=300)
    sy.add_argument("--days", type=int, default=60)
    sy.add_argument("--transactions", type=int, default=5000)
    sy.set_defaults(fn=_cmd_synth)

    d = sub.add_parser("demo", help="end-to-end run on synthetic data")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--oracle-rows", type=int, default=400)
    d.set_defaults(fn=_cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

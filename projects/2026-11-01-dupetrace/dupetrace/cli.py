"""Command line: generate | ingest | evaluate | train | query | serve."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import evaluate, store, synth
from .engine import DuplicateFinder
from .model import Reranker


def _cmd_generate(a):
    rp, lp = synth.write(Path(a.out), n_bugs=a.bugs, seed=a.seed)
    print(f"wrote {rp} and {lp}")


def _cmd_ingest(a):
    conn = store.connect(a.db)
    print(json.dumps(store.ingest_file(conn, a.reports).as_dict()))


def _cmd_evaluate(a):
    conn = store.connect(a.db)
    rep, model = evaluate.run(store.load_reports(conn), evaluate.load_labels(a.labels), seed=a.seed)
    print(evaluate.format_report(rep))
    if a.report:
        evaluate.save_report(rep, a.report)
        print(f"\nmetrics -> {a.report}")
    if a.model:
        model.save(a.model)
        print(f"model   -> {a.model} (threshold {model.threshold:.3f}, chosen on validation)")


def _cmd_sweep(a):
    """Re-generate data with several seeds and evaluate each: shows variance, not a single lucky draw."""
    rows = []
    for seed in a.seeds:
        reports, labels = synth.generate(n_bugs=a.bugs, seed=seed)
        conn = store.connect(":memory:")
        store.ingest_lines(conn, (json.dumps(r) for r in reports))
        rep, _ = evaluate.run(store.load_reports(conn), labels)
        rows.append((seed, rep))
    names = list(rows[0][1]["methods"])
    print(f"{'seed':>4} " + " ".join(f"{n + ' R@1/F1':>28}" for n in names))
    for seed, rep in rows:
        cells = [f"{rep['methods'][n]['ranking']['recall@1']:.3f}/{rep['methods'][n]['decision']['f1']:.3f}" for n in names]
        print(f"{seed:>4} " + " ".join(f"{c:>28}" for c in cells))


def _cmd_query(a):
    conn = store.connect(a.db)
    finder = DuplicateFinder(Reranker.load(a.model) if a.model else None)
    for r in store.load_reports(conn):
        finder.add(r)
    out = []
    with open(a.input, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            reason = store.validate(rec)
            if reason:
                out.append({"id": rec.get("id") if isinstance(rec, dict) else None, "error": reason})
                continue
            q = store.build_report(rec)
            ranked, likely = finder.suggest(q, top=a.top)
            out.append({
                "id": q.id, "likely_duplicate": likely,
                "suggestions": [{"id": c.report.id, "title": c.report.title, "score": round(c.score, 4),
                                 "reasons": c.reasons} for c in ranked],
            })
    json.dump(out, sys.stdout, indent=2)
    print()


def _cmd_serve(a):
    import uvicorn

    from .api import create_app
    uvicorn.run(create_app(a.db, a.model), host=a.host, port=a.port)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dupetrace")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic reports.jsonl + labels.csv")
    g.add_argument("--out", default="data")
    g.add_argument("--bugs", type=int, default=420)
    g.add_argument("--seed", type=int, default=7)
    g.set_defaults(fn=_cmd_generate)

    i = sub.add_parser("ingest", help="validate + load a JSONL file into SQLite (idempotent)")
    i.add_argument("reports")
    i.add_argument("--db", default="data/dupetrace.db")
    i.set_defaults(fn=_cmd_ingest)

    e = sub.add_parser("evaluate", help="chronological replay: train reranker, compare with baselines")
    e.add_argument("--db", default="data/dupetrace.db")
    e.add_argument("--labels", default="data/labels.csv")
    e.add_argument("--report", default="artifacts/metrics.json")
    e.add_argument("--model", default="artifacts/reranker.json")
    e.add_argument("--seed", type=int, default=0)
    e.set_defaults(fn=_cmd_evaluate)

    w = sub.add_parser("sweep", help="evaluate over several synthetic seeds")
    w.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    w.add_argument("--bugs", type=int, default=420)
    w.set_defaults(fn=_cmd_sweep)

    q = sub.add_parser("query", help="suggest duplicates for reports in a JSONL file (read-only)")
    q.add_argument("input")
    q.add_argument("--db", default="data/dupetrace.db")
    q.add_argument("--model", default="artifacts/reranker.json")
    q.add_argument("--top", type=int, default=3)
    q.set_defaults(fn=_cmd_query)

    s = sub.add_parser("serve", help="run the FastAPI service")
    s.add_argument("--db", default="data/dupetrace.db")
    s.add_argument("--model", default="artifacts/reranker.json")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=_cmd_serve)

    a = p.parse_args(argv)
    if getattr(a, "db", None):
        Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    main()

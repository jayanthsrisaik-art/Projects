"""Command line: ``python -m persuade evaluate|score|sample``."""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from persuade import data, models, pipeline


def _table(report: dict) -> str:
    budget = report["config"]["budget"]
    top = f"observed_uplift_top{int(round(budget * 100))}"
    cols = ("qini_area", top, "capture_of_best", "sleeping_dog_share", "spearman_true_uplift")
    lines = ["model".ljust(22) + "".join(c[:22].rjust(24) for c in cols)]
    for name, m in report["summary"].items():
        cells = "".join(f"{m[c]['mean']:>15.4f} ±{m[c]['std']:.4f}" for c in cols)
        lines.append(name.ljust(22) + cells)
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="persuade", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("evaluate", help="compare baselines and uplift models over seeds")
    e.add_argument("--n", type=int, default=30_000)
    e.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2, 3, 4])
    e.add_argument("--budget", type=float, default=0.2, help="share of customers that can be contacted")
    e.add_argument("--out", help="write the JSON report here")

    s = sub.add_parser("score", help="rank a customer CSV by predicted uplift")
    s.add_argument("--input", required=True)
    s.add_argument("--output", required=True)
    s.add_argument("--model", default="t_learner", choices=models.UPLIFT_MODELS)
    s.add_argument("--budget", type=float, default=0.2)
    s.add_argument("--train-n", type=int, default=30_000)
    s.add_argument("--seed", type=int, default=0)

    g = sub.add_parser("sample", help="write a feature-only customer CSV from a fresh seed")
    g.add_argument("--n", type=int, default=20)
    g.add_argument("--seed", type=int, default=99)
    g.add_argument("--output", required=True)

    a = p.parse_args(argv)
    if a.cmd == "evaluate":
        report = pipeline.run(n=a.n, seeds=tuple(a.seeds), budget=a.budget)
        print(_table(report))
        if a.out:
            with open(a.out, "w") as f:
                json.dump(report, f, indent=2)
                f.write("\n")
    elif a.cmd == "score":
        customers = pd.read_csv(a.input)
        try:
            out = pipeline.score_customers(customers, a.model, a.budget, a.train_n, a.seed)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out.to_csv(a.output, index=False)
        print(f"scored {len(out)} customers, {int(out['treat'].sum())} selected -> {a.output}")
    else:
        df = data.generate(n=a.n, seed=a.seed)
        df[["customer_id", *data.FEATURES]].to_csv(a.output, index=False)
        print(f"wrote {a.n} customers -> {a.output}")
    return 0

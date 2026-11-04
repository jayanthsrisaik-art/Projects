"""Command line: make-sample | audit | score | bench."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

from .confident import METHODS, find_label_issues
from .data import encode_labels, make_split, read_table, write_sample
from .evaluate import bench, score_flags
from .models import MODELS, out_of_fold_proba


def cmd_make_sample(a) -> int:
    s = make_split("digits", a.noise, a.kind, seed=a.seed)
    d, t = write_sample(s, a.out)
    print(f"wrote {d} ({len(s.y_train_noisy)} rows, {int(s.is_flipped.sum())} flipped labels) and {t}")
    return 0


def audit_table(path: str, label_col: str, method: str, model: str, folds: int, seed: int):
    x, raw, _, ids = read_table(path, label_col)
    y, classes = encode_labels(raw)
    if len(classes) < 2:
        raise ValueError("need at least two classes")
    probs, _ = out_of_fold_proba(x, y, model, folds, seed)
    return find_label_issues(y, probs, method), y, classes, ids


def cmd_audit(a) -> int:
    audit, y, classes, ids = audit_table(a.csv, a.label, a.method, a.model, a.folds, a.seed)
    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "id", "given_label", "suggested_label", "self_confidence", "margin"])
        for rank, i in enumerate(audit.ranking, start=1):
            w.writerow([rank, ids[i], classes[y[i]], classes[audit.suggested[i]],
                        f"{audit.self_confidence[i]:.4f}", f"{audit.margin[i]:.4f}"])
    k = len(classes)
    off = [(classes[i], classes[j], round(float(audit.joint[i, j]) * len(y), 1))
           for i in range(k) for j in range(k) if i != j and audit.joint[i, j] * len(y) >= 1.0]
    off.sort(key=lambda t: -t[2])
    report = {
        "rows": int(len(y)), "classes": classes, "method": audit.method, "model": a.model,
        "folds": a.folds, "seed": a.seed,
        "flagged": audit.n_issues,
        "estimated_noise_rate": round(audit.estimated_noise_rate, 4),
        "top_confusions": [{"given": g, "likely_true": t, "est_rows": n} for g, t, n in off[:10]],
        "per_class_threshold": {c: round(float(v), 4) for c, v in zip(classes, audit.thresholds)},
    }
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['flagged']} of {report['rows']} rows flagged "
          f"(estimated noise rate {report['estimated_noise_rate']:.1%}); issues -> {a.out}")
    for c in report["top_confusions"][:5]:
        print(f"  labelled {c['given']!s:>4} but likely {c['likely_true']!s:>4}: ~{c['est_rows']} rows")
    return 0


def cmd_score(a) -> int:
    with open(a.truth, newline="") as fh:
        truth = {r["id"]: r["is_flipped"] == "1" for r in csv.DictReader(fh)}
    with open(a.issues, newline="") as fh:
        flagged = {r["id"] for r in csv.DictReader(fh)}
    unknown = flagged - truth.keys()
    if unknown:
        print(f"error: {len(unknown)} flagged ids are not in the truth file", file=sys.stderr)
        return 2
    ids = sorted(truth)
    mask = np.array([i in flagged for i in ids])
    t = np.array([truth[i] for i in ids])
    d = score_flags(mask, t)
    out = {"flagged": d.flagged, "true_flips": int(t.sum()), "precision": d.precision,
           "recall": d.recall, "f1": d.f1}
    print(json.dumps(out, indent=2))
    return 0


def _fmt(ms) -> str:
    return f"{ms[0]:.3f}±{ms[1]:.3f}"


def cmd_bench(a) -> int:
    res = bench(a.dataset, tuple(a.kinds), tuple(a.rates), tuple(a.seeds), a.model, a.folds)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=2) + "\n")
    for row in res["summary"]:
        print(f"\n== {row['noise_kind']} noise, rate {row['noise_rate']} ({row['seeds']} seeds) ==")
        print(f"   noise rate true {_fmt(row['true_noise_rate'])}  est {_fmt(row['est_noise_rate'])}  "
              f"noise-matrix MAE {_fmt(row['noise_matrix_mae'])}")
        print(f"   {'detector':<26}{'precision':>16}{'recall':>16}{'F1':>16}")
        for d in row["f1"]:
            print(f"   {d:<26}{_fmt(row['precision'][d]):>16}{_fmt(row['recall'][d]):>16}{_fmt(row['f1'][d]):>16}")
        print("   clean-test accuracy: " + ", ".join(f"{k} {_fmt(v)}" for k, v in row["test_accuracy"].items()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labelaudit", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("make-sample", help="write a noisy digits training CSV + ground truth")
    s.add_argument("--out", default="samples")
    s.add_argument("--noise", type=float, default=0.2)
    s.add_argument("--kind", choices=["asymmetric", "uniform"], default="asymmetric")
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(func=cmd_make_sample)

    s = sub.add_parser("audit", help="flag likely mislabeled rows in a CSV")
    s.add_argument("--csv", required=True)
    s.add_argument("--label", default="label")
    s.add_argument("--out", default="issues.csv")
    s.add_argument("--report")
    s.add_argument("--method", choices=METHODS, default="prune_by_noise_rate")
    s.add_argument("--model", choices=MODELS, default="rf")
    s.add_argument("--folds", type=int, default=5)
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("score", help="precision/recall of an issues file against ground truth")
    s.add_argument("--issues", required=True)
    s.add_argument("--truth", required=True)
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("bench", help="detection + downstream benchmark with injected noise")
    s.add_argument("--dataset", choices=["digits", "synthetic"], default="digits")
    s.add_argument("--kinds", nargs="+", default=["asymmetric", "uniform"])
    s.add_argument("--rates", nargs="+", type=float, default=[0.1, 0.2, 0.3])
    s.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    s.add_argument("--model", choices=MODELS, default="rf")
    s.add_argument("--folds", type=int, default=5)
    s.add_argument("--out")
    s.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

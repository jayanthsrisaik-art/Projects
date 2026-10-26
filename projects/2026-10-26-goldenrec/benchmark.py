"""Measured comparison: goldenrec vs exact email/phone baseline, plus ablations.

Weights and thresholds were set by hand and only inspected on the dev seed (7).
The test seed (2026) is a fresh draw from the same generator and is reported as-is.

    python benchmark.py [--entities 1000] [--scale 5000]
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace

from goldenrec.evaluate import blocking_metrics, pairwise_metrics
from goldenrec.matcher import MatchConfig, baseline_clusters, resolve
from goldenrec.normalize import normalize_record
from goldenrec.synth import generate


def evaluate_split(n: int, seed: int) -> dict:
    rows, truth = generate(n, seed)
    records = [normalize_record(r) for r in rows]
    cfg = MatchConfig()
    t0 = time.perf_counter()
    result = resolve(records, cfg)
    elapsed = time.perf_counter() - t0
    no_constraint = resolve(records, replace(cfg, enforce_dob_constraint=False))
    return {
        "seed": seed,
        "records": len(records),
        "goldenrec": pairwise_metrics(result.clusters, truth),
        "goldenrec_no_dob_constraint": pairwise_metrics(no_constraint.clusters, truth),
        "baseline_exact_email_or_phone": pairwise_metrics(baseline_clusters(records), truth),
        "blocking": blocking_metrics(result.candidate_set, truth),
        "blocked_merges": result.blocked_merges,
        "review_pairs": len(result.review),
        "resolve_seconds": round(elapsed, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", type=int, default=1000)
    ap.add_argument("--scale", type=int, default=5000, help="entities for the timing run (0 to skip)")
    args = ap.parse_args()
    report = {"dev": evaluate_split(args.entities, 7), "test": evaluate_split(args.entities, 2026)}
    if args.scale:
        rows, _ = generate(args.scale, 99)
        records = [normalize_record(r) for r in rows]
        t0 = time.perf_counter()
        result = resolve(records)
        report["scale"] = {"entities": args.scale, "records": len(records),
                           "candidate_pairs": result.blocking.candidate_pairs,
                           "resolve_seconds": round(time.perf_counter() - t0, 3)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

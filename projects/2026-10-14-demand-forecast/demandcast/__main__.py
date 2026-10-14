import argparse
import json

from .data import generate_demand
from .evaluate import run_experiment, save_report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="demandcast")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="train + evaluate all models")
    r.add_argument("--days", type=int, default=365)
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--test-frac", type=float, default=0.2)
    r.add_argument("--out", default="outputs")
    g = sub.add_parser("generate", help="write synthetic data CSV")
    g.add_argument("--days", type=int, default=7)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "generate":
        generate_demand(a.days, a.seed).to_csv(a.out, index=False)
        print(f"wrote {a.out}")
        return 0
    report = run_experiment(a.days, a.seed, a.test_frac)
    path = save_report(report, a.out)
    print(json.dumps(report["metrics"], indent=2))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

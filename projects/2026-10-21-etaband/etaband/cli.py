from __future__ import annotations

import argparse
import json

from .pipeline import run


def main(argv=None):
    p = argparse.ArgumentParser(prog="etaband", description=__doc__)
    p.add_argument("--n", type=int, default=12000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--alpha", type=float, default=0.1)
    p.add_argument("--out", help="write JSON report here")
    a = p.parse_args(argv)
    res = run(a.n, a.seed, a.alpha)
    print(f"target coverage {res['config']['target_coverage']:.2f}; splits {res['config']['split_sizes']}")
    print(f"{'model':24}{'cov':>7}{'width':>8}{'score':>8}{'MAE':>7}{'worst-seg':>11}")
    for k, v in res["models"].items():
        print(f"{k:24}{v['coverage']:7.3f}{v['mean_width_min']:8.2f}{v['interval_score']:8.2f}"
              f"{v['point_mae_min']:7.2f}{v['worst_segment_coverage']:11.3f}")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)


if __name__ == "__main__":
    main()

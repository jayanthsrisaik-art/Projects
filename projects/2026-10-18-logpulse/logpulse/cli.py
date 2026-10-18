"""python -m logpulse.cli generate|eval"""
import argparse
import json

from .detect import detect, evaluate
from .generator import START, generate
from .store import connect, ingest


def run_eval(minutes=4320, seed=7, quantile=0.99) -> dict:
    lines, incidents = generate(minutes, seed)
    con = connect()
    ingest(con, lines)
    base = START // 60
    sets = [{base + m for m in range(i.start_min, i.start_min + i.length)} for i in incidents]
    allw = set().union(*sets)
    out = {"lines": len(lines), "incidents": len(incidents)}
    for method in ("zscore", "iforest"):
        out[method] = evaluate(detect(con, method=method, quantile=quantile), allw, sets)
    return out


def main():
    ap = argparse.ArgumentParser(prog="logpulse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("--minutes", type=int, default=4320)
    g.add_argument("--seed", type=int, default=7); g.add_argument("--out", required=True)
    e = sub.add_parser("eval"); e.add_argument("--minutes", type=int, default=4320); e.add_argument("--seed", type=int, default=7)
    e.add_argument("--quantile", type=float, default=0.99)
    a = ap.parse_args()
    if a.cmd == "generate":
        lines, _ = generate(a.minutes, a.seed)
        with open(a.out, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"wrote {len(lines)} lines to {a.out}")
    else:
        print(json.dumps(run_eval(a.minutes, a.seed, a.quantile), indent=2))


if __name__ == "__main__":
    main()

import argparse
import json
import sys
from pathlib import Path

from .evaluate import SETS, evaluate, format_report
from .gen import make_set
from .local import LocalExtractor
from .pipeline import run


def _extractor(name: str):
    if name == "claude":
        from .llm import ClaudeExtractor
        return ClaudeExtractor()
    return LocalExtractor()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="invoiceiq")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="extract one invoice text file to JSON")
    e.add_argument("file")
    e.add_argument("--extractor", choices=["local", "claude"], default="local")
    v = sub.add_parser("eval", help="measured evaluation on a seeded synthetic set")
    v.add_argument("--set", choices=sorted(SETS), default="test")
    v.add_argument("--extractor", choices=["local", "claude"], default="local")
    v.add_argument("--json", action="store_true")
    g = sub.add_parser("samples", help="write sample invoices + expected JSON")
    g.add_argument("outdir")
    a = p.parse_args(argv)

    if a.cmd == "extract":
        r = run(_extractor(a.extractor), Path(a.file).read_text())
        print(json.dumps({"status": r.status, "attempts": r.attempts, "errors": r.errors, "data": r.data}, indent=2))
        return 0 if r.status == "ok" else 2
    if a.cmd == "eval":
        rep = evaluate(_extractor(a.extractor), a.set)
        print(json.dumps(rep, indent=2) if a.json else format_report(rep))
        return 0
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    seen = {}
    for d in make_set(1000, 40, corrupt_p=0.5):
        key = (d.template, d.corrupted is not None)
        if key in seen:
            continue
        name = f"{d.template}{'_corrupt' if d.corrupted else ''}"
        seen[key] = name
        (out / f"{name}.txt").write_text(d.text)
        (out / f"{name}.expected.json").write_text(json.dumps(d.gold, indent=2) + "\n")
    print(f"wrote {len(seen)} samples to {out}", file=sys.stderr)
    return 0

"""Command line: redact / chat / gen-data / eval."""
from __future__ import annotations

import argparse
import json
import sys

from .corpus import generate, read_jsonl, write_jsonl
from .egress import LeakBlocked
from .evaluate import format_report, run
from .gateway import Gateway
from .llm import make_llm
from .vault import Session


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="veilgate", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("redact", help="print redacted text and (optionally) the vault")
    p.add_argument("path", help="input file or - for stdin")
    p.add_argument("--show-vault", action="store_true", help="also print placeholder -> value map (local only!)")

    p = sub.add_parser("chat", help="redact -> LLM -> rehydrate for one prompt")
    p.add_argument("path")
    p.add_argument("--backend", default="mock", help="mock | mock:echo | anthropic")
    p.add_argument("--on-leak", default="block", choices=["block", "warn"])
    p.add_argument("--stream", action="store_true")

    p = sub.add_parser("gen-data", help="write the seeded synthetic dev/test corpora")
    p.add_argument("--out", default="data")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--dev", type=int, default=200)
    p.add_argument("--test", type=int, default=400)

    p = sub.add_parser("eval", help="evaluate on a labelled JSONL corpus")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "redact":
        s = Session()
        res = s.redact(_read(a.path))
        print(res.text)
        if a.show_vault:
            print(json.dumps({ph: e.display for ph, e in s.entities.items()}, indent=2), file=sys.stderr)
        return 0

    if a.cmd == "chat":
        gw = Gateway(make_llm(a.backend), on_leak=a.on_leak)
        s = Session()
        try:
            if a.stream:
                audit, chunks = gw.chat_stream(s, _read(a.path))
                for c in chunks:
                    sys.stdout.write(c)
                    sys.stdout.flush()
                print()
            else:
                result = gw.chat(s, _read(a.path))
                audit = result.audit
                print(result.reply)
        except LeakBlocked as e:
            print(f"BLOCKED: {e}", file=sys.stderr)
            for f in e.findings:
                print(f"  {f.kind}: {f.text!r}", file=sys.stderr)
            return 2
        print("--- audit (what left the process) ---", file=sys.stderr)
        print(audit.outbound, file=sys.stderr)
        print(f"redacted={audit.entities_redacted} egress_findings={len(audit.egress_findings)} "
              f"unknown_placeholders={audit.unknown_placeholders}", file=sys.stderr)
        return 0

    if a.cmd == "gen-data":
        import os

        os.makedirs(a.out, exist_ok=True)
        write_jsonl(generate("dev", a.dev, a.seed), f"{a.out}/dev.jsonl")
        write_jsonl(generate("test", a.test, a.seed), f"{a.out}/test.jsonl")
        print(f"wrote {a.out}/dev.jsonl ({a.dev}) and {a.out}/test.jsonl ({a.test})")
        return 0

    if a.cmd == "eval":
        res = run(read_jsonl(a.path))
        print(json.dumps(res, indent=2) if a.json else format_report(a.path, res))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

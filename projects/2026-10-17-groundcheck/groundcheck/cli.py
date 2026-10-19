import argparse
import json
import sys
from pathlib import Path

from .evaluate import run as run_eval
from .llm import AnthropicVerifier
from .lexical import LexicalVerifier
from .report import check_answer, render_text


def build_verifier(backend: str, model: str | None):
    if backend == "anthropic":
        return AnthropicVerifier(model=model) if model else AnthropicVerifier()
    return LexicalVerifier()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="groundcheck", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check an answer against source files")
    c.add_argument("--answer", required=True, type=Path)
    c.add_argument("--sources", required=True, nargs="+", type=Path)
    c.add_argument("--backend", choices=["lexical", "anthropic"], default="lexical")
    c.add_argument("--model")
    c.add_argument("--json", action="store_true", help="emit JSON instead of text")
    c.add_argument("--fail-on-contradiction", action="store_true")
    sub.add_parser("eval", help="run the labelled evaluation (lexical backend)")
    args = ap.parse_args(argv)

    if args.cmd == "eval":
        print(json.dumps(run_eval(), indent=2))
        return 0
    sources = {p.name: p.read_text() for p in args.sources}
    try:
        verifier = build_verifier(args.backend, args.model)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    report = check_answer(args.answer.read_text(), sources, verifier)
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 1 if args.fail_on_contradiction and report["counts"]["contradicted"] else 0

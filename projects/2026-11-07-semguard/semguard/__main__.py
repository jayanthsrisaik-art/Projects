"""Command line: python -m semguard {gen-data,eval,check,ask,serve}."""
from __future__ import annotations

import argparse
import json
import sys

from .cache import CacheConfig, SemanticCache, scope_key
from .datagen import generate, read_jsonl, write_jsonl
from .embed import HashingEmbedder
from .evaluate import format_report, run_eval
from .facts import GUARDS, Lexicon, conflict, extract_facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="semguard", description="Guarded semantic cache for LLM calls")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen-data", help="write the synthetic labelled query stream as JSONL")
    g.add_argument("--n", type=int, default=3000)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--out", required=True)

    e = sub.add_parser("eval", help="calibrate on dev topics and evaluate cache policies on test topics")
    e.add_argument("--data", help="JSONL stream (default: generate with --n/--seed)")
    e.add_argument("--n", type=int, default=3000)
    e.add_argument("--seed", type=int, default=7)
    e.add_argument("--target", type=float, default=0.99, help="hit precision target used for calibration")
    e.add_argument("--json", help="also write the full report here")
    e.add_argument("--timing", action="store_true", help="include mean lookup latency (machine dependent)")

    c = sub.add_parser("check", help="explain whether a cached prompt may answer a new prompt")
    c.add_argument("cached")
    c.add_argument("query")
    c.add_argument("--threshold", type=float, default=0.68)
    c.add_argument("--lexicon", help="custom lexicon JSON")

    a = sub.add_parser("ask", help="send prompts through a persistent cache backed by the configured LLM")
    a.add_argument("prompts", nargs="+")
    a.add_argument("--db", default="semguard.db")
    a.add_argument("--system", default="")
    a.add_argument("--threshold", type=float, default=0.68)
    a.add_argument("--backend", default=None, help="mock (default) or anthropic")

    s = sub.add_parser("serve", help="run the FastAPI service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = ap.parse_args(argv)

    if args.cmd == "gen-data":
        write_jsonl(generate(args.n, args.seed), args.out)
        print(f"wrote {args.n} queries to {args.out}")
    elif args.cmd == "eval":
        records = read_jsonl(args.data) if args.data else generate(args.n, args.seed)
        report = run_eval(records, args.target, timing=args.timing)
        print(format_report(report))
        if args.timing:
            print("\nmean lookup ms (test):",
                  {k: v["test"]["mean_lookup_ms"] for k, v in report["policies"].items()})
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
                fh.write("\n")
    elif args.cmd == "check":
        lex = Lexicon.from_file(args.lexicon) if args.lexicon else Lexicon.default()
        emb = HashingEmbedder()
        fa, fb = extract_facts(args.cached, lex), extract_facts(args.query, lex)
        sim = float(emb.embed(args.cached) @ emb.embed(args.query))
        why = conflict(fa, fb, GUARDS)
        if fa.volatile or fb.volatile:
            verdict, why = "bypass", "time-sensitive wording"
        elif sim < args.threshold:
            verdict, why = "miss", f"similarity below threshold {args.threshold}"
        else:
            verdict = "miss" if why else "hit"
        print(json.dumps({"similarity": round(sim, 4), "verdict": verdict, "reason": why or "compatible",
                          "cached_facts": fa.to_dict(), "query_facts": fb.to_dict()}, indent=2))
    elif args.cmd == "ask":
        from .llm import get_backend

        llm = get_backend(args.backend)
        cache = SemanticCache(args.db, CacheConfig(threshold=args.threshold))
        scope = scope_key(getattr(llm, "model", llm.name), args.system)
        for p in args.prompts:
            lk = cache.lookup(p, scope)
            if lk.hit:
                text = lk.entry["response"]
            else:
                text = llm.complete(p, args.system).text
                if lk.status == "miss":
                    cache.store(p, text, scope)
            print(json.dumps({"prompt": p, "cache": lk.status, "score": round(lk.score, 4), "reason": lk.reason,
                              "matched": lk.entry["prompt"] if lk.entry else None, "text": text}))
    elif args.cmd == "serve":
        import uvicorn

        from .service import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

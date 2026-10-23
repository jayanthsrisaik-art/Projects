"""CLI: apidiff OLD.json NEW.json [--format text|json] [--fail-on breaking|warning|never]"""
from __future__ import annotations

import argparse
import json
import sys

from .differ import diff_specs, BREAKING, WARNING

_RANK = {"breaking": 2, "warning": 1}


def render_text(changes) -> str:
    if not changes:
        return "No changes.\n"
    lines = [f"[{c.severity.upper():8}] {c.code}: {c.where} - {c.message}" for c in changes]
    counts = {s: sum(c.severity == s for c in changes) for s in ("breaking", "warning", "safe")}
    lines.append(f"\n{counts['breaking']} breaking, {counts['warning']} warning, {counts['safe']} safe")
    return "\n".join(lines) + "\n"


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="apidiff")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--fail-on", choices=["breaking", "warning", "never"], default="breaking")
    a = ap.parse_args(argv)
    try:
        with open(a.old) as f:
            old = json.load(f)
        with open(a.new) as f:
            new = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    changes = diff_specs(old, new)
    if a.format == "json":
        out.write(json.dumps([c.to_dict() for c in changes], indent=2) + "\n")
    else:
        out.write(render_text(changes))
    threshold = {"breaking": 2, "warning": 1, "never": 99}[a.fail_on]
    return 1 if any(_RANK.get(c.severity, 0) >= threshold for c in changes) else 0

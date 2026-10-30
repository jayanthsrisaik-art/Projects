"""Python and TypeScript evaluators must agree exactly.

``samples/parity_expected.json`` is the checked-in contract (also asserted by the Node
tests). The fuzz test runs the compiled TS evaluator over thousands of synthetic
contexts and compares every result; it is skipped if Node or the build is missing.
"""
import json
import random
import shutil
import subprocess

import pytest

from conftest import ROOT, load_sample_flags
from flagdeck.evaluator import evaluate_all
from flagdeck.population import synthetic_contexts
from flagdeck.schema import ENVIRONMENTS

DIST = ROOT / "web/dist/evaluator.js"


def test_python_matches_checked_in_expectations():
    flags = load_sample_flags()
    contexts = json.loads((ROOT / "samples/contexts.json").read_text())
    expected = json.loads((ROOT / "samples/parity_expected.json").read_text())
    for env in ENVIRONMENTS:
        for ctx in contexts:
            assert evaluate_all(flags, env, ctx) == expected[env][ctx["key"]], (env, ctx["key"])


def _messy_contexts(n: int) -> list[dict]:
    """Synthetic users plus type-confusing attributes to stress cross-language edge cases."""
    rng = random.Random(11)
    odd_values = [None, True, False, 0, 7, 7.0, 4.5, -1, "", "US", "4.1", ["beta"], ["x", "beta"], [], {"a": 1},
                  "ü-ß-☃", "😀emoji", 2**53 - 1, 2**53 + 2, 1e21, None, "DE", "enterprise", 4.1,
                  "x@eng.acme.com", "y@qa.acme.com", "z@acme.com", "q@eng.acme.com.evil"]
    out = []
    for ctx in synthetic_contexts(n, seed=3):
        for attr in ("country", "plan", "app_version", "org", "device_id", "groups", "email"):
            if rng.random() < 0.3:
                ctx[attr] = rng.choice(odd_values)
        if rng.random() < 0.05:
            ctx["email"] = ctx["email"].upper() if isinstance(ctx["email"], str) else ctx["email"]
        out.append(ctx)
    return out


@pytest.mark.skipif(shutil.which("node") is None or not DIST.exists(), reason="needs node and `npm run build`")
def test_typescript_evaluator_matches_python_on_fuzzed_contexts():
    flags = load_sample_flags()
    contexts = _messy_contexts(3000)
    for env in ENVIRONMENTS:
        proc = subprocess.run(["node", str(ROOT / "web/test/eval_stdin.mjs")],
                              input=json.dumps({"flags": flags, "env": env, "contexts": contexts}),
                              capture_output=True, text=True, check=True, timeout=120)
        ts = json.loads(proc.stdout)
        py = [evaluate_all(flags, env, c) for c in contexts]
        mismatches = [(c, a, b) for c, a, b in zip(contexts, py, ts) if a != b]
        assert not mismatches, mismatches[:3]

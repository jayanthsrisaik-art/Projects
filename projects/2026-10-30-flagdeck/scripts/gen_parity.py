"""Regenerate samples/parity_expected.json from the Python evaluator.

The TypeScript test suite asserts it produces the identical file, so any drift
between the two implementations fails CI. Run: ``python scripts/gen_parity.py``.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flagdeck.evaluator import evaluate_all  # noqa: E402
from flagdeck.schema import ENVIRONMENTS, FlagIn  # noqa: E402


def build() -> dict:
    flags = {d["key"]: FlagIn.model_validate(d).doc() for d in json.loads((ROOT / "samples/flags.json").read_text())}
    contexts = json.loads((ROOT / "samples/contexts.json").read_text())
    return {env: {c["key"]: evaluate_all(flags, env, c) for c in contexts} for env in ENVIRONMENTS}


if __name__ == "__main__":
    out = ROOT / "samples/parity_expected.json"
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")

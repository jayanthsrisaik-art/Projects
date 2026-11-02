import copy
import json
from pathlib import Path

from refill.spec import build_pipeline

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example_raw(start=None, end=None):
    raw = json.loads((EXAMPLES / "pipeline.json").read_text())
    if start:
        raw["range"] = {"start": start, "end": end}
    return raw


def example_pipeline(start=None, end=None):
    return build_pipeline(example_raw(start, end), EXAMPLES)


def mutated(raw, fn):
    r = copy.deepcopy(raw)
    fn(r)
    return r

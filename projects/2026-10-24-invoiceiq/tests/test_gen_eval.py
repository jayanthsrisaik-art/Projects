import json
from pathlib import Path

import pytest

from invoiceiq.evaluate import evaluate
from invoiceiq.gen import make_set
from invoiceiq.local import LocalExtractor
from invoiceiq.pipeline import run
from invoiceiq.validate import validate

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_generator_deterministic():
    assert [d.text for d in make_set(5, 20)] == [d.text for d in make_set(5, 20)]
    assert [d.text for d in make_set(5, 20)] != [d.text for d in make_set(6, 20)]


def test_gold_is_internally_consistent():
    for d in make_set(3, 100, corrupt_p=0):
        assert validate(d.gold) == []


def test_clean_docs_all_extracted_exactly():
    ex = LocalExtractor()
    for d in make_set(11, 150, corrupt_p=0):
        r = run(ex, d.text)
        assert r.status == "ok" and r.data == d.gold, d.text


def test_corruption_never_silently_accepted():
    """If a corrupted doc is accepted, the output must still equal gold."""
    ex = LocalExtractor()
    docs = [d for d in make_set(12, 300, corrupt_p=1.0)]
    for d in docs:
        r = run(ex, d.text)
        if r.status == "ok":
            assert r.data == d.gold, (d.corrupted, d.text)


def test_eval_modes_ordering():
    rep = evaluate(LocalExtractor(), "dev")
    m = rep["modes"]
    assert m["validated"]["silent_errors"] == 0 == m["repair"]["silent_errors"]
    assert m["repair"]["coverage"] > m["validated"]["coverage"]
    assert m["repair"]["accuracy"] >= m["single_pass"]["accuracy"]
    assert m["single_pass"]["silent_errors"] > 0


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.txt")), ids=lambda p: p.stem)
def test_samples(path):
    gold = json.loads(path.with_suffix(".expected.json").read_text())
    r = run(LocalExtractor(), path.read_text())
    if "corrupt" in path.stem:
        assert r.status == "needs_review" or r.data == gold
    else:
        assert r.status == "ok" and r.data == gold
